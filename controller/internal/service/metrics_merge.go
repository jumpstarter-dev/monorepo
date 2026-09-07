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
	"io"
	"sort"
	"strings"

	dto "github.com/prometheus/client_model/go"
	"github.com/prometheus/common/expfmt"
)

const (
	labelExporter   = "exporter"
	labelDriverType = "driver_type"
	driverTypeOther = "other"

	// scrapeTimeoutsMetric is incremented once per exporter that does not
	// answer a MetricsStream scrape within scrapeTimeout (JEP-0013).
	scrapeTimeoutsMetric = "jumpstarter_scrape_timeouts_total"

	// metricsParseErrorsMetric is incremented once per exporter snapshot that
	// MetricsStream delivered but OpenMetrics parse rejected (JEP-0013).
	metricsParseErrorsMetric = "jumpstarter_metrics_parse_errors_total"
)

// DefaultDriverTypeEnum is the JEP-0013 default allowlist for driver_type.
var DefaultDriverTypeEnum = []string{
	"power", "storage", "network", "serial", "console", "video", "composite",
}

// DefaultExemplarKeys is the JEP-0013 default exemplar allowlist.
var DefaultExemplarKeys = []string{"client", "lease_id"}

type mergeConfig struct {
	exporterName string
	driverTypes  map[string]struct{}
	exemplarKeys map[string]struct{}
}

type exporterSnapshot struct {
	name string
	text []byte
}

func setToMap(values []string) map[string]struct{} {
	out := make(map[string]struct{}, len(values))
	for _, v := range values {
		if v == "" {
			continue
		}
		out[v] = struct{}{}
	}
	return out
}

func (s *TelemetryService) mergeConfigFor(name string) mergeConfig {
	types := s.DriverTypeEnum
	if len(types) == 0 {
		types = DefaultDriverTypeEnum
	}
	keys := s.ExemplarKeys
	if len(keys) == 0 {
		keys = DefaultExemplarKeys
	}
	return mergeConfig{
		exporterName: name,
		driverTypes:  setToMap(types),
		exemplarKeys: setToMap(keys),
	}
}

func parseMetricFamilies(text []byte) ([]*dto.MetricFamily, error) {
	dec := expfmt.NewDecoder(bytes.NewReader(text), expfmt.NewFormat(expfmt.TypeOpenMetrics))
	var out []*dto.MetricFamily
	for {
		mf := new(dto.MetricFamily)
		err := dec.Decode(mf)
		if errors.Is(err, io.EOF) {
			return out, nil
		}
		if err != nil {
			return nil, err
		}
		out = append(out, mf)
	}
}

func encodeMetricFamilies(w io.Writer, families []*dto.MetricFamily) error {
	sorted := append([]*dto.MetricFamily(nil), families...)
	sort.Slice(sorted, func(i, j int) bool {
		return sorted[i].GetName() < sorted[j].GetName()
	})
	enc := expfmt.NewEncoder(w, expfmt.NewFormat(expfmt.TypeOpenMetrics))
	for _, f := range sorted {
		if f == nil {
			continue
		}
		if err := enc.Encode(f); err != nil {
			return err
		}
	}
	if closer, ok := enc.(expfmt.Closer); ok {
		return closer.Close()
	}
	return nil
}

func mergeSnapshots(snapshots []exporterSnapshot, extra []*dto.MetricFamily, cfgFor func(string) mergeConfig, onParseError func(exporter string, err error)) []*dto.MetricFamily {
	byName := map[string]*dto.MetricFamily{}
	for _, f := range extra {
		if f == nil || f.GetName() == "" {
			continue
		}
		byName[f.GetName()] = f
	}
	for _, snap := range snapshots {
		if len(snap.text) == 0 {
			continue
		}
		families, err := parseMetricFamilies(snap.text)
		if err != nil {
			if onParseError != nil {
				onParseError(snap.name, err)
			}
			continue
		}
		cfg := cfgFor(snap.name)
		for _, f := range families {
			applyFamily(f, cfg)
			existing, ok := byName[f.GetName()]
			if !ok {
				byName[f.GetName()] = f
				continue
			}
			existing.Metric = append(existing.Metric, f.Metric...)
		}
	}
	out := make([]*dto.MetricFamily, 0, len(byName))
	for _, f := range byName {
		out = append(out, f)
	}
	return out
}

func applyFamily(f *dto.MetricFamily, cfg mergeConfig) {
	for _, m := range f.GetMetric() {
		applyMetric(m, cfg)
	}
}

func applyMetric(m *dto.Metric, cfg mergeConfig) {
	setLabel(m, labelExporter, cfg.exporterName)
	if v, ok := getLabel(m, labelDriverType); ok {
		setLabel(m, labelDriverType, remapDriverType(v, cfg.driverTypes))
	}
	if m.Counter != nil {
		m.Counter.Exemplar = filterExemplar(m.Counter.Exemplar, cfg.exemplarKeys)
	}
	if m.Histogram != nil {
		for _, b := range m.Histogram.Bucket {
			if b != nil {
				b.Exemplar = filterExemplar(b.Exemplar, cfg.exemplarKeys)
			}
		}
		if len(m.Histogram.Exemplars) > 0 {
			filtered := make([]*dto.Exemplar, 0, len(m.Histogram.Exemplars))
			for _, ex := range m.Histogram.Exemplars {
				if fe := filterExemplar(ex, cfg.exemplarKeys); fe != nil {
					filtered = append(filtered, fe)
				}
			}
			m.Histogram.Exemplars = filtered
		}
	}
}

func remapDriverType(value string, allow map[string]struct{}) string {
	if _, ok := allow[value]; ok {
		return value
	}
	return driverTypeOther
}

func getLabel(m *dto.Metric, name string) (string, bool) {
	for _, lp := range m.GetLabel() {
		if lp.GetName() == name {
			return lp.GetValue(), true
		}
	}
	return "", false
}

func setLabel(m *dto.Metric, name, value string) {
	for _, lp := range m.GetLabel() {
		if lp.GetName() == name {
			v := value
			lp.Value = &v
			return
		}
	}
	m.Label = append(m.Label, labelPair(name, value))
}

func labelPair(name, value string) *dto.LabelPair {
	n, v := name, value
	return &dto.LabelPair{Name: &n, Value: &v}
}

func filterExemplar(ex *dto.Exemplar, allow map[string]struct{}) *dto.Exemplar {
	if ex == nil {
		return nil
	}
	kept := make([]*dto.LabelPair, 0, len(ex.GetLabel()))
	for _, lp := range ex.GetLabel() {
		if _, ok := allow[lp.GetName()]; !ok {
			continue
		}
		if strings.TrimSpace(lp.GetValue()) == "" {
			continue
		}
		kept = append(kept, lp)
	}
	if len(kept) == 0 {
		return nil
	}
	ex.Label = kept
	return ex
}
