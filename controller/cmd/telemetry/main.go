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

// jumpstarter-telemetry reverse-scrapes exporter metrics via MetricsStream and
// receives structured log entries via PushLogs. Logs are written to structured
// stdout for downstream log shippers (Promtail, Grafana Alloy, Vector).
// Loki push is a later Phase 3 PR.
//
// TLS: always enabled. Set EXTERNAL_CERT_PEM and EXTERNAL_KEY_PEM to file paths of
// operator-mounted cert/key (e.g. from a cert-manager Secret); when absent a
// self-signed certificate is generated. The self-signed cert PEM is logged at
// startup — copy it into the controller ConfigMap's telemetry.certificate field
// so exporters can verify the TLS connection.
//
// Endpoint: GRPC_TELEMETRY_ENDPOINT must be set on BOTH this pod and the controller
// pod to the same value (e.g. "jumpstarter-telemetry.jumpstarter.svc:9093").
// The telemetry service uses it to generate the correct SAN in the self-signed
// certificate; the controller uses it to advertise the address to exporters via
// GetServiceEndpoints. A mismatch causes TLS hostname verification failures.
//
// HTTP: GET /metrics, /healthz, and /readyz bind separately (default :8080).
package main

import (
	"context"
	"flag"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"

	"github.com/jumpstarter-dev/jumpstarter/controller/internal/oidc"
	"github.com/jumpstarter-dev/jumpstarter/controller/internal/service"
)

var (
	// Version information — set via ldflags at build time.
	version   = "dev"
	gitCommit = "unknown"
	buildDate = "unknown"
)

func splitCSV(s string) []string {
	if s == "" {
		return nil
	}
	parts := strings.Split(s, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		p = strings.TrimSpace(p)
		if p != "" {
			out = append(out, p)
		}
	}
	return out
}

func main() {
	var bindAddr string
	var metricsAddr string
	var scrapeTimeout time.Duration
	var driverTypeEnum string
	var exemplarKeys string
	flag.StringVar(&bindAddr, "grpc-bind", ":9093", "TCP address to bind the gRPC server to")
	flag.StringVar(&metricsAddr, "metrics-bind-address", ":8080",
		"TCP address for HTTP GET /metrics, /healthz, and /readyz. Use 0 to disable.")
	flag.DurationVar(&scrapeTimeout, "scrape-timeout", 7*time.Second,
		"Max wait for parallel MetricsStream scrape responses")
	flag.StringVar(&driverTypeEnum, "driver-type-enum", strings.Join(service.DefaultDriverTypeEnum, ","),
		"Comma-separated allowlist of driver_type values; others are remapped to other")
	flag.StringVar(&exemplarKeys, "exemplar-keys", strings.Join(service.DefaultExemplarKeys, ","),
		"Comma-separated allowlist of Prometheus exemplar keys")

	opts := zap.Options{}
	opts.BindFlags(flag.CommandLine)
	flag.Parse()

	ctrl.SetLogger(zap.New(zap.UseFlagOptions(&opts)))
	logger := ctrl.Log.WithName("setup").WithValues("component", "telemetry")

	logger.Info("Jumpstarter Telemetry starting",
		"version", version,
		"gitCommit", gitCommit,
		"buildDate", buildDate,
		"bindAddr", bindAddr,
		"metricsBindAddr", metricsAddr,
		"scrapeTimeout", scrapeTimeout,
	)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	signer, err := oidc.NewSignerFromSeed(
		[]byte(os.Getenv("CONTROLLER_KEY")),
		"https://localhost:8085",
		"jumpstarter",
	)
	if err != nil {
		logger.Error(err, "unable to create token verifier")
		os.Exit(1)
	}

	svc := &service.TelemetryService{
		BindAddr:        bindAddr,
		MetricsBindAddr: metricsAddr,
		ScrapeTimeout:   scrapeTimeout,
		DriverTypeEnum:  splitCSV(driverTypeEnum),
		ExemplarKeys:    splitCSV(exemplarKeys),
		Signer:          signer,
	}

	// Register signal handler before starting the service so no signal
	// is missed in the window between goroutine start and Notify.
	sigs := make(chan os.Signal, 1)
	signal.Notify(sigs, syscall.SIGINT, syscall.SIGTERM)

	errCh := make(chan error, 1)
	go func() {
		errCh <- svc.Start(ctx)
	}()

	select {
	case sig := <-sigs:
		logger.Info("received signal, shutting down", "signal", sig)
		cancel()
		// Wait for the service to finish its graceful stop before exiting.
		if err := <-errCh; err != nil {
			logger.Error(err, "telemetry service exited with error")
			os.Exit(1)
		}
	case err := <-errCh:
		if err != nil {
			logger.Error(err, "telemetry service exited with error")
			os.Exit(1)
		}
	}
}
