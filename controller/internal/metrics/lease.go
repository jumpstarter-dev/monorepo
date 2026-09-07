/*
Copyright 2026. The Jumpstarter Authors.

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

// Package metrics provides Jumpstarter controller Prometheus metrics (JEP-0013 Phase 2).
package metrics

import (
	"context"
	"unicode/utf8"

	jlog "github.com/jumpstarter-dev/jumpstarter/controller/internal/log"
	"github.com/prometheus/client_golang/prometheus"
	"sigs.k8s.io/controller-runtime/pkg/log"
	ctrlmetrics "sigs.k8s.io/controller-runtime/pkg/metrics"
)

const (
	// LeaseAcquisitionsTotal is the JEP-0013 counter for lease acquire attempts.
	LeaseAcquisitionsTotal = "jumpstarter_lease_acquisitions_total"

	ResultSuccess = "success"
	ResultFailure = "failure"

	// maxExemplarRunes is the OpenMetrics 1.0 combined label name+value rune limit.
	// Exceeding it makes prometheus.ExemplarAdder.AddWithExemplar panic.
	maxExemplarRunes = 128
)

// DefaultExemplarKeys are the JEP-0013 default exemplar allowlist keys.
var DefaultExemplarKeys = []string{"client", "lease_id"}

// LeaseMetrics holds lease-related Prometheus collectors.
type LeaseMetrics struct {
	acquisitions *prometheus.CounterVec
}

// NewLeaseMetrics constructs lease metrics. Call Register before RecordAcquisition.
func NewLeaseMetrics() *LeaseMetrics {
	return &LeaseMetrics{
		acquisitions: prometheus.NewCounterVec(
			prometheus.CounterOpts{
				Name: LeaseAcquisitionsTotal,
				Help: "Lease acquire attempts on the Jumpstarter controller.",
			},
			[]string{"result"},
		),
	}
}

// Register registers collectors with the given registerer and pre-creates
// the success/failure series at zero so scrapes are never missing samples.
func (m *LeaseMetrics) Register(r prometheus.Registerer) error {
	if err := r.Register(m.acquisitions); err != nil {
		return err
	}
	m.initializeSeries()
	return nil
}

// MustRegister registers collectors and panics on error.
func (m *LeaseMetrics) MustRegister(r prometheus.Registerer) {
	r.MustRegister(m.acquisitions)
	m.initializeSeries()
}

// MustRegisterWithControllerRuntime registers on the controller-runtime metrics registry.
func (m *LeaseMetrics) MustRegisterWithControllerRuntime() {
	m.MustRegister(ctrlmetrics.Registry)
}

// initializeSeries ensures result=success and result=failure time series exist at 0.
// CounterVec is lazily created; without this, /metrics shows HELP/TYPE but no samples
// until the first observation (https://prometheus.io/docs/practices/instrumentation/#avoid-missing-metrics).
func (m *LeaseMetrics) initializeSeries() {
	if m == nil || m.acquisitions == nil {
		return
	}
	_, _ = m.acquisitions.GetMetricWithLabelValues(ResultSuccess)
	_, _ = m.acquisitions.GetMetricWithLabelValues(ResultFailure)
}

// RecordAcquisition increments jumpstarter_lease_acquisitions_total for result
// and attaches exemplar labels from the allowlist (client, lease_id when present).
// Invalid or over-budget exemplar values are truncated or dropped; the counter
// always increments. Budget truncation/drops are logged at Warning (logr V(1)).
func (m *LeaseMetrics) RecordAcquisition(ctx context.Context, result string, exemplars map[string]string) {
	if m == nil || m.acquisitions == nil {
		return
	}
	metric, err := m.acquisitions.GetMetricWithLabelValues(result)
	if err != nil {
		return
	}
	filtered := filterExemplarLabels(exemplars)
	labels, constrained := constrainExemplarLabels(filtered)
	if constrained {
		jlog.Warning(log.FromContext(ctx),
			"lease acquisition exemplar exceeded OpenMetrics budget; truncated or dropped labels",
			"budget_runes", maxExemplarRunes,
			"result", result,
			"original", filtered,
			"constrained", labels,
		)
	}
	if len(labels) > 0 {
		if adder, ok := metric.(prometheus.ExemplarAdder); ok {
			adder.AddWithExemplar(1, labels)
			return
		}
	}
	metric.Inc()
}

func filterExemplarLabels(in map[string]string) prometheus.Labels {
	if len(in) == 0 {
		return nil
	}
	out := prometheus.Labels{}
	for _, key := range DefaultExemplarKeys {
		if v, ok := in[key]; ok && v != "" {
			out[key] = v
		}
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// constrainExemplarLabels validates UTF-8 and fits labels into the OpenMetrics
// 128-rune exemplar budget in DefaultExemplarKeys order. Values are truncated
// when needed; keys that cannot fit (even truncated) are dropped.
// The bool is true when any label was truncated or dropped for the budget.
func constrainExemplarLabels(in prometheus.Labels) (prometheus.Labels, bool) {
	if len(in) == 0 {
		return nil, false
	}
	out := prometheus.Labels{}
	constrained := false
	remaining := maxExemplarRunes
	for _, key := range DefaultExemplarKeys {
		v, ok := in[key]
		if !ok || v == "" {
			continue
		}
		if !utf8.ValidString(key) || !utf8.ValidString(v) {
			constrained = true
			continue
		}
		keyRunes := utf8.RuneCountInString(key)
		if keyRunes >= remaining {
			constrained = true
			continue
		}
		valueBudget := remaining - keyRunes
		valueRunes := utf8.RuneCountInString(v)
		if valueRunes > valueBudget {
			constrained = true
			v = truncateRunes(v, valueBudget)
			if v == "" {
				continue
			}
			valueRunes = utf8.RuneCountInString(v)
		}
		out[key] = v
		remaining -= keyRunes + valueRunes
	}
	if len(out) == 0 {
		return nil, constrained
	}
	return out, constrained
}

func truncateRunes(s string, maxRunes int) string {
	if maxRunes <= 0 {
		return ""
	}
	if utf8.RuneCountInString(s) <= maxRunes {
		return s
	}
	i := 0
	for range maxRunes {
		_, size := utf8.DecodeRuneInString(s[i:])
		i += size
	}
	return s[:i]
}

// Default is the process-wide lease metrics instance used by the controller.
var Default = NewLeaseMetrics()

// RegisterDefaults registers Default with the controller-runtime registry.
func RegisterDefaults() {
	Default.MustRegisterWithControllerRuntime()
}
