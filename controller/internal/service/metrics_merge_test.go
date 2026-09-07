/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package service

import (
	"bytes"
	"errors"
	"strings"
	"testing"

	dto "github.com/prometheus/client_model/go"
	"github.com/prometheus/common/expfmt"
)

func labeledCounterValue(t *testing.T, mfs []*dto.MetricFamily, name, label, want string) float64 {
	t.Helper()
	for _, mf := range mfs {
		if mf.GetName() != name && mf.GetName()+"_total" != name {
			continue
		}
		for _, m := range mf.Metric {
			for _, lp := range m.GetLabel() {
				if lp.GetName() == label && lp.GetValue() == want {
					return m.GetCounter().GetValue()
				}
			}
		}
	}
	t.Fatalf("%s{%s=%q} missing from gathered families", name, label, want)
	return 0
}

func TestApplyMetric_OverwritesExporterRemapsDriverTypeAndFiltersExemplars(t *testing.T) {
	one := 1.0
	m := &dto.Metric{
		Label: []*dto.LabelPair{
			labelPair("exporter", "spoofed"),
			labelPair("driver_type", "tuya"),
			labelPair("operation", "on"),
		},
		Counter: &dto.Counter{
			Value: &one,
			Exemplar: &dto.Exemplar{
				Label: []*dto.LabelPair{
					labelPair("client", "ci"),
					labelPair("lease_id", "abc"),
					labelPair("trace_id", "drop-me"),
				},
			},
		},
	}
	applyMetric(m, mergeConfig{
		exporterName: "sidekick",
		driverTypes:  setToMap(DefaultDriverTypeEnum),
		exemplarKeys: setToMap(DefaultExemplarKeys),
	})

	if got, _ := getLabel(m, "exporter"); got != "sidekick" {
		t.Errorf("exporter label = %q, want sidekick", got)
	}
	if got, _ := getLabel(m, "driver_type"); got != "other" {
		t.Errorf("driver_type = %q, want other", got)
	}
	ex := m.Counter.GetExemplar()
	if ex == nil {
		t.Fatal("expected exemplar to be kept")
	}
	got := map[string]string{}
	for _, lp := range ex.GetLabel() {
		got[lp.GetName()] = lp.GetValue()
	}
	if got["client"] != "ci" || got["lease_id"] != "abc" {
		t.Errorf("exemplar labels = %v, want client=ci lease_id=abc", got)
	}
	if _, ok := got["trace_id"]; ok {
		t.Errorf("trace_id should have been dropped, got %v", got)
	}
}

func TestApplyMetric_KeepsAllowlistedDriverType(t *testing.T) {
	m := &dto.Metric{
		Label: []*dto.LabelPair{
			labelPair("driver_type", "power"),
		},
	}
	applyMetric(m, mergeConfig{
		exporterName: "exp-a",
		driverTypes:  setToMap(DefaultDriverTypeEnum),
		exemplarKeys: setToMap(DefaultExemplarKeys),
	})
	if got, _ := getLabel(m, "driver_type"); got != "power" {
		t.Errorf("driver_type = %q, want power", got)
	}
	if got, _ := getLabel(m, "exporter"); got != "exp-a" {
		t.Errorf("exporter = %q, want exp-a", got)
	}
}

// pythonOpenMetricsWithExemplar is the prometheus_client OpenMetrics form
// that prometheus/common v0.62's text fallback cannot parse (it treats '#' as
// a timestamp). This is the body exporters send after driver operations.
const pythonOpenMetricsWithExemplar = `# TYPE jumpstarter_operation_duration_seconds histogram
jumpstarter_operation_duration_seconds_bucket{exporter="sidekick",le="0.005",operation="on",result="success",driver_type="power"} 1.0 # {lease_id="lease-1"} 0.00262 1788299670.149
jumpstarter_operation_duration_seconds_sum{exporter="sidekick",operation="on",result="success",driver_type="power"} 0.00262
jumpstarter_operation_duration_seconds_count{exporter="sidekick",operation="on",result="success",driver_type="power"} 1.0
# EOF
`

func testMergeCfg(name string) mergeConfig {
	return mergeConfig{
		exporterName: name,
		driverTypes:  setToMap(DefaultDriverTypeEnum),
		exemplarKeys: setToMap(DefaultExemplarKeys),
	}
}

func TestParseMetricFamilies_OpenMetricsExemplarSuffixFails(t *testing.T) {
	_, err := parseMetricFamilies([]byte(pythonOpenMetricsWithExemplar))
	if err == nil {
		t.Fatal("expected parse error for OpenMetrics exemplar suffix")
	}
}

func TestMergeSnapshots_CombinesExportersAndReportsInvalidText(t *testing.T) {
	textA := []byte(`# TYPE jumpstarter_operations_total counter
# HELP jumpstarter_operations_total Total operations performed.
jumpstarter_operations_total{exporter="a",operation="on",result="success",driver_type="power"} 2.0
# EOF
`)
	textB := []byte(`# TYPE jumpstarter_operations_total counter
jumpstarter_operations_total{exporter="b",operation="off",result="success",driver_type="power"} 3.0
# EOF
`)
	type parseErrCall struct {
		exporter string
		err      error
	}
	var calls []parseErrCall
	families := mergeSnapshots([]exporterSnapshot{
		{name: "exp-a", text: textA},
		{name: "exp-b", text: textB},
		{name: "bad", text: []byte("not metrics")},
	}, nil, testMergeCfg, func(exporter string, err error) {
		calls = append(calls, parseErrCall{exporter: exporter, err: err})
	})

	var ops *dto.MetricFamily
	for _, f := range families {
		if f.GetName() == "jumpstarter_operations_total" {
			ops = f
			break
		}
	}
	if ops == nil {
		t.Fatal("missing jumpstarter_operations_total")
	}
	if len(ops.Metric) != 2 {
		t.Fatalf("got %d series, want 2 (invalid snapshot omitted)", len(ops.Metric))
	}
	exporters := map[string]bool{}
	for _, m := range ops.Metric {
		name, _ := getLabel(m, "exporter")
		exporters[name] = true
	}
	if !exporters["exp-a"] || !exporters["exp-b"] {
		t.Errorf("exporters = %v, want exp-a and exp-b", exporters)
	}
	if len(calls) != 1 {
		t.Fatalf("parse-error callbacks = %d, want 1", len(calls))
	}
	if calls[0].exporter != "bad" {
		t.Errorf("parse-error exporter = %q, want bad", calls[0].exporter)
	}
	if calls[0].err == nil {
		t.Fatal("parse-error callback err is nil")
	}
}

func TestMergeSnapshots_ReportsOpenMetricsExemplarParseError(t *testing.T) {
	var gotExporter string
	var gotErr error
	families := mergeSnapshots([]exporterSnapshot{
		{name: "sidekick", text: []byte(pythonOpenMetricsWithExemplar)},
	}, nil, testMergeCfg, func(exporter string, err error) {
		gotExporter = exporter
		gotErr = err
	})
	for _, f := range families {
		if strings.HasPrefix(f.GetName(), "jumpstarter_operation_duration_seconds") {
			t.Fatalf("unparsable snapshot must be omitted, got family %s", f.GetName())
		}
	}
	if gotExporter != "sidekick" {
		t.Errorf("parse-error exporter = %q, want sidekick", gotExporter)
	}
	if gotErr == nil {
		t.Fatal("parse-error callback err is nil")
	}
}

func TestRecordMetricsParseError_IncrementsLabeledCounter(t *testing.T) {
	svc := &TelemetryService{}
	svc.initScrapeTimeouts()
	svc.recordMetricsParseError("sidekick", errors.New("parse failed"))

	mfs, err := svc.metricsRegistry.Gather()
	if err != nil {
		t.Fatalf("Gather: %v", err)
	}
	value := labeledCounterValue(t, mfs, metricsParseErrorsMetric, labelExporter, "sidekick")
	if value != 1 {
		t.Fatalf("%s{exporter=sidekick} = %v, want 1", metricsParseErrorsMetric, value)
	}
}

func TestEncodeMetricFamilies_WritesOpenMetricsEOF(t *testing.T) {
	name := "jumpstarter_scrape_timeouts_total"
	help := "Exporter MetricsStream scrapes that exceeded scrapeTimeout."
	metricType := dto.MetricType_COUNTER
	zero := 0.0
	families := []*dto.MetricFamily{{
		Name: &name,
		Help: &help,
		Type: &metricType,
		Metric: []*dto.Metric{{
			Counter: &dto.Counter{Value: &zero},
		}},
	}}
	var buf bytes.Buffer
	if err := encodeMetricFamilies(&buf, families); err != nil {
		t.Fatalf("encode: %v", err)
	}
	body := buf.String()
	if !strings.Contains(body, scrapeTimeoutsMetric) {
		t.Errorf("encoded body missing %s:\n%s", scrapeTimeoutsMetric, body)
	}
	if !strings.Contains(body, "# EOF") {
		t.Errorf("OpenMetrics body missing # EOF:\n%s", body)
	}
	dec := expfmt.NewDecoder(strings.NewReader(body), expfmt.NewFormat(expfmt.TypeOpenMetrics))
	mf := new(dto.MetricFamily)
	if err := dec.Decode(mf); err != nil {
		t.Fatalf("re-decode: %v", err)
	}
	if mf.GetName() != scrapeTimeoutsMetric {
		t.Errorf("decoded name = %q", mf.GetName())
	}
}
