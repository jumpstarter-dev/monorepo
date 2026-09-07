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

package main

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	dto "github.com/prometheus/client_model/go"
	"github.com/prometheus/common/expfmt"
)

func TestMetricsEndpointServesPrometheusText(t *testing.T) {
	addr, shutdown, err := startMetricsServer("127.0.0.1:0")
	if err != nil {
		t.Fatalf("startMetricsServer: %v", err)
	}
	if addr == "" {
		t.Fatal("expected non-empty listen address")
	}
	if shutdown == nil {
		t.Fatal("expected non-nil shutdown func when server is enabled")
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_ = shutdown(ctx)
	})

	client := &http.Client{Timeout: 2 * time.Second}
	var resp *http.Response
	var lastErr error
	for range 20 {
		resp, lastErr = client.Get("http://" + addr + "/metrics")
		if lastErr == nil {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	if lastErr != nil {
		t.Fatalf("GET /metrics: %v", lastErr)
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	ct := resp.Header.Get("Content-Type")
	if !strings.Contains(ct, "text/plain") && !strings.Contains(ct, "openmetrics") {
		t.Fatalf("unexpected Content-Type %q", ct)
	}
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("read body: %v", err)
	}

	// Validate Prometheus exposition contract (not substring heuristics).
	var parser expfmt.TextParser
	families, err := parser.TextToMetricFamilies(strings.NewReader(string(body)))
	if err != nil {
		t.Fatalf("parse Prometheus exposition: %v\nbody:\n%s", err, body)
	}
	if len(families) == 0 {
		t.Fatal("expected at least one metric family from default promhttp handler")
	}

	// Default Go process metrics should appear; reject undocumented jumpstarter_* series.
	hasGo, hasProcess := false, false
	for name := range families {
		if strings.HasPrefix(name, "jumpstarter_") {
			t.Fatalf("unexpected undocumented metric family %q; got families: %v", name, familyNames(families))
		}
		switch {
		case strings.HasPrefix(name, "go_"):
			hasGo = true
		case strings.HasPrefix(name, "process_"):
			hasProcess = true
		}
	}
	if !hasGo {
		t.Fatalf("expected go_* metric family, got families: %v", familyNames(families))
	}
	if !hasProcess {
		t.Fatalf("expected process_* metric family, got families: %v", familyNames(families))
	}
}

func familyNames(families map[string]*dto.MetricFamily) []string {
	names := make([]string, 0, len(families))
	for name := range families {
		names = append(names, name)
	}
	return names
}

func TestMetricsServerDisabledWhenAddrZero(t *testing.T) {
	addr, shutdown, err := startMetricsServer("0")
	if err != nil {
		t.Fatalf("startMetricsServer(0): %v", err)
	}
	if addr != "" {
		t.Fatalf("expected empty addr when disabled, got %q", addr)
	}
	if shutdown != nil {
		t.Fatal("expected nil shutdown func when server is disabled")
	}
}

func TestMetricsServerShutdown(t *testing.T) {
	addr, shutdown, err := startMetricsServer("127.0.0.1:0")
	if err != nil {
		t.Fatalf("startMetricsServer: %v", err)
	}
	if shutdown == nil {
		t.Fatal("expected non-nil shutdown func")
	}

	client := &http.Client{Timeout: 2 * time.Second}
	var lastErr error
	for range 20 {
		var resp *http.Response
		resp, lastErr = client.Get("http://" + addr + "/metrics")
		if lastErr == nil {
			_ = resp.Body.Close()
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	if lastErr != nil {
		t.Fatalf("GET /metrics before shutdown: %v", lastErr)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := shutdown(ctx); err != nil {
		t.Fatalf("shutdown: %v", err)
	}

	_, err = client.Get("http://" + addr + "/metrics")
	if err == nil {
		t.Fatal("expected GET /metrics to fail after shutdown")
	}
}
