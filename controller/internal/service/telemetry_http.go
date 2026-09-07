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
	"context"
	"net"
	"net/http"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/common/expfmt"
	ctrl "sigs.k8s.io/controller-runtime"
)

func (s *TelemetryService) initScrapeTimeouts() {
	s.stateMu.Lock()
	defer s.stateMu.Unlock()
	if s.metricsRegistry != nil {
		return
	}
	s.metricsRegistry = prometheus.NewRegistry()
	s.scrapeTimeouts = prometheus.NewCounter(prometheus.CounterOpts{
		Name: scrapeTimeoutsMetric,
		Help: "Exporter MetricsStream scrapes that exceeded scrapeTimeout.",
	})
	s.parseErrors = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: metricsParseErrorsMetric,
		Help: "Exporter MetricsStream snapshots omitted because OpenMetrics parse failed.",
	}, []string{labelExporter})
	s.droppedTotal = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: droppedTotalMetric,
		Help: "Log entries dropped due to Loki backpressure.",
	}, []string{"destination"})
	s.metricsRegistry.MustRegister(s.scrapeTimeouts, s.parseErrors, s.droppedTotal)
	s.droppedTotal.WithLabelValues(droppedDestination).Add(0)
}

func (s *TelemetryService) recordMetricsParseError(exporter string, err error) {
	if s.parseErrors != nil {
		s.parseErrors.WithLabelValues(exporter).Inc()
	}
	ctrl.Log.WithName("telemetry").Error(err, "exporter metrics snapshot omitted", logFieldExporter, exporter)
}

func metricsHTTPEnabled(addr string) bool {
	return addr != "" && addr != "0"
}

func (s *TelemetryService) startMetricsHTTP() (func(context.Context) error, error) {
	addr := s.MetricsBindAddr
	if !metricsHTTPEnabled(addr) {
		return nil, nil
	}
	s.initMetricsState()
	s.initScrapeTimeouts()

	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return nil, err
	}
	s.metricsAddr = ln.Addr().String()

	mux := http.NewServeMux()
	mux.HandleFunc("/metrics", s.handleMetrics)
	mux.HandleFunc("/healthz", s.handleHealthz)
	mux.HandleFunc("/readyz", s.handleReadyz)

	writeTimeout := s.scrapeTimeoutDuration() + 15*time.Second
	if writeTimeout < 30*time.Second {
		writeTimeout = 30 * time.Second
	}

	srv := &http.Server{
		Handler:           mux,
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      writeTimeout,
		IdleTimeout:       5 * time.Minute,
	}
	go func() {
		if err := srv.Serve(ln); err != nil && err != http.ErrServerClosed {
			ctrl.Log.WithName("telemetry").Error(err, "metrics HTTP server stopped unexpectedly")
		}
	}()
	ctrl.Log.WithName("telemetry").Info("Telemetry metrics HTTP listening",
		"addr", s.metricsAddr,
	)
	return srv.Shutdown, nil
}

func (s *TelemetryService) handleHealthz(w http.ResponseWriter, _ *http.Request) {
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte("ok\n"))
}

// handleReadyz is 200 once the gRPC listener is bound.
// Loki is optional (metrics-only is valid) and is not a readiness gate.
func (s *TelemetryService) handleReadyz(w http.ResponseWriter, _ *http.Request) {
	if !s.grpcReady.Load() {
		http.Error(w, "grpc not ready\n", http.StatusServiceUnavailable)
		return
	}
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte("ok\n"))
}

func (s *TelemetryService) handleMetrics(w http.ResponseWriter, r *http.Request) {
	s.initScrapeTimeouts()
	snaps := s.fanoutScrapes(r.Context())
	// Merge exporter snapshots first so parse failures increment parseErrors
	// before hub metrics are gathered into the same /metrics response.
	families := mergeSnapshots(snaps, nil, s.mergeConfigFor, s.recordMetricsParseError)

	if s.metricsRegistry != nil {
		gathered, err := s.metricsRegistry.Gather()
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		families = mergeSnapshots(nil, append(gathered, families...), s.mergeConfigFor, nil)
	}

	var buf bytes.Buffer
	if err := encodeMetricFamilies(&buf, families); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", string(expfmt.NewFormat(expfmt.TypeOpenMetrics)))
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(buf.Bytes())
}
