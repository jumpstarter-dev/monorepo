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
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	pb "github.com/jumpstarter-dev/jumpstarter/controller/internal/protocol/jumpstarter/v1"
	"github.com/prometheus/client_golang/prometheus"
	dto "github.com/prometheus/client_model/go"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func testDroppedCounter() *prometheus.CounterVec {
	return prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: droppedTotalMetric,
		Help: "Log entries dropped due to Loki backpressure.",
	}, []string{"destination"})
}

func logEntry(msg string) *pb.LogEntry {
	return &pb.LogEntry{
		Timestamp: timestamppb.New(time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)),
		Severity:  "info",
		Message:   msg,
		Component: "exporter",
		Exporter:  "lab-1",
		Namespace: "jumpstarter",
		Lease:     "lease-1",
		Client:    "ci",
		Operation: "flash",
		Result:    "success",
	}
}

func TestNewLokiPusher_EmptyURLIsNil(t *testing.T) {
	p, err := NewLokiPusher(LokiConfig{}, testDroppedCounter())
	if err != nil {
		t.Fatalf("empty URL: %v", err)
	}
	if p != nil {
		t.Fatal("metrics-only deployments must not construct a Loki pusher")
	}
}

func TestNewLokiPusher_GRPCSchemeNotImplemented(t *testing.T) {
	p, err := NewLokiPusher(LokiConfig{URL: "grpc://loki.monitoring.svc:9095"}, testDroppedCounter())
	if p != nil {
		t.Fatal("gRPC Loki must not start an HTTP pusher")
	}
	if err == nil {
		t.Fatal("expected error for unimplemented grpc:// Loki URL")
	}
}

func TestNormalizeLokiPushURL_AppendsPath(t *testing.T) {
	got, err := normalizeLokiPushURL("http://loki:3100")
	if err != nil {
		t.Fatal(err)
	}
	if got != "http://loki:3100/loki/api/v1/push" {
		t.Errorf("got %q", got)
	}
}

func TestNormalizeLokiPushURL_KeepsExistingPath(t *testing.T) {
	raw := "https://loki-gateway.monitoring.svc:3100/loki/api/v1/push"
	got, err := normalizeLokiPushURL(raw)
	if err != nil {
		t.Fatal(err)
	}
	if got != raw {
		t.Errorf("got %q", got)
	}
}

func TestDefaultQueueDepthIs10000(t *testing.T) {
	if d := normalizeQueueDepth(0); d != defaultLokiQueueDepth {
		t.Errorf("normalizeQueueDepth(0) = %d, want %d", d, defaultLokiQueueDepth)
	}
	if defaultLokiQueueDepth != 10000 {
		t.Errorf("default Loki queue depth = %d, want 10000 (JEP-0013)", defaultLokiQueueDepth)
	}
}

func TestLokiBuffer_OverflowEmitsDropMarker(t *testing.T) {
	dropped := testDroppedCounter()
	p, err := NewLokiPusher(LokiConfig{URL: "http://127.0.0.1:1", QueueDepth: 3}, dropped)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	p.now = func() time.Time { return now }

	p.Enqueue(logEntry("a"))
	p.Enqueue(logEntry("b"))
	p.Enqueue(logEntry("c")) // overflow: reserved marker slot
	now = now.Add(12 * time.Second)
	p.Enqueue(logEntry("d")) // accumulate into the same marker

	queued := p.queued()
	if len(queued) != 3 {
		t.Fatalf("queued = %d, want 2 real entries + 1 drop marker", len(queued))
	}
	if queued[0].Message != "a" || queued[1].Message != "b" {
		t.Fatalf("real entries = %q, %q; want a, b", queued[0].Message, queued[1].Message)
	}
	marker := queued[2]
	if marker.Severity != "warning" {
		t.Errorf("marker severity = %q, want warning", marker.Severity)
	}
	if marker.Component != "telemetry" {
		t.Errorf("marker component = %q, want telemetry", marker.Component)
	}
	if marker.Operation != "backpressure" {
		t.Errorf("marker operation = %q, want backpressure", marker.Operation)
	}
	if marker.ExtraFields["count"] != "2" {
		t.Errorf("marker count = %q, want 2", marker.ExtraFields["count"])
	}
	if marker.ExtraFields["window_seconds"] != "12" {
		t.Errorf("marker window_seconds = %q, want 12", marker.ExtraFields["window_seconds"])
	}

	got := counterValue(t, dropped, "loki")
	if got != 2 {
		t.Errorf("dropped_total{destination=loki} = %v, want 2", got)
	}
}

func TestLokiPush_HTTPPostsOpenMetricsCompatibleJSON(t *testing.T) {
	var (
		mu      sync.Mutex
		gotPath string
		gotAuth string
		gotCT   string
		gotBody []byte
	)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		mu.Lock()
		gotPath = r.URL.Path
		gotAuth = r.Header.Get("Authorization")
		gotCT = r.Header.Get("Content-Type")
		gotBody = body
		mu.Unlock()
		w.WriteHeader(http.StatusNoContent)
	}))
	t.Cleanup(srv.Close)

	dropped := testDroppedCounter()
	p, err := NewLokiPusher(LokiConfig{
		URL:        srv.URL,
		Username:   "loki",
		Password:   "secret",
		QueueDepth: 10,
	}, dropped)
	if err != nil {
		t.Fatal(err)
	}
	p.Enqueue(logEntry("hello loki"))
	if err := p.Flush(context.Background()); err != nil {
		t.Fatalf("Flush: %v", err)
	}

	mu.Lock()
	defer mu.Unlock()
	if gotPath != "/loki/api/v1/push" {
		t.Errorf("path = %q, want /loki/api/v1/push", gotPath)
	}
	if !strings.HasPrefix(gotCT, "application/json") {
		t.Errorf("Content-Type = %q, want application/json", gotCT)
	}
	if !strings.HasPrefix(gotAuth, "Basic ") {
		t.Errorf("Authorization = %q, want Basic", gotAuth)
	}

	var payload lokiPushPayload
	if err := json.Unmarshal(gotBody, &payload); err != nil {
		t.Fatalf("unmarshal %s: %v", gotBody, err)
	}
	if len(payload.Streams) != 1 {
		t.Fatalf("streams = %d, want 1", len(payload.Streams))
	}
	stream := payload.Streams[0].Stream
	if stream["component"] != "exporter" || stream["exporter"] != "lab-1" || stream["namespace"] != "jumpstarter" {
		t.Errorf("stream labels = %v, want component/exporter/namespace", stream)
	}
	for _, highCard := range []string{"client", "lease", "lease_id", "operation"} {
		if _, ok := stream[highCard]; ok {
			t.Errorf("high-cardinality field %q must not be a Loki stream label", highCard)
		}
	}
	if len(payload.Streams[0].Values) != 1 {
		t.Fatalf("values = %d, want 1", len(payload.Streams[0].Values))
	}
	ts, line := payload.Streams[0].Values[0][0], payload.Streams[0].Values[0][1]
	if _, err := strconv.ParseInt(ts, 10, 64); err != nil {
		t.Errorf("timestamp %q is not unix nanoseconds", ts)
	}
	var body map[string]string
	if err := json.Unmarshal([]byte(line), &body); err != nil {
		t.Fatalf("log line JSON: %v", err)
	}
	if body["msg"] != "hello loki" {
		t.Errorf("msg = %q", body["msg"])
	}
	if body["severity"] != "info" {
		t.Errorf("severity = %q", body["severity"])
	}
	if body["client"] != "ci" || body["lease"] != "lease-1" {
		t.Errorf("high-cardinality fields missing from JSON body: %v", body)
	}
}

func TestLokiPush_FlushFailureKeepsBuffer(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
	}))
	t.Cleanup(srv.Close)

	p, err := NewLokiPusher(LokiConfig{URL: srv.URL, QueueDepth: 10}, testDroppedCounter())
	if err != nil {
		t.Fatal(err)
	}
	p.Enqueue(logEntry("keep me"))
	if err := p.Flush(context.Background()); err == nil {
		t.Fatal("expected flush error")
	}
	if len(p.queued()) != 1 {
		t.Fatalf("failed flush must keep the buffer, queued = %d", len(p.queued()))
	}
}

func TestLokiPush_BearerToken(t *testing.T) {
	var gotAuth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		w.WriteHeader(http.StatusNoContent)
	}))
	t.Cleanup(srv.Close)

	p, err := NewLokiPusher(LokiConfig{URL: srv.URL, Token: "s3cret", QueueDepth: 8}, testDroppedCounter())
	if err != nil {
		t.Fatal(err)
	}
	p.Enqueue(logEntry("tok"))
	if err := p.Flush(context.Background()); err != nil {
		t.Fatal(err)
	}
	if gotAuth != "Bearer s3cret" {
		t.Errorf("Authorization = %q, want Bearer s3cret", gotAuth)
	}
}

func TestDroppedTotalRegisteredOnMetrics(t *testing.T) {
	svc := &TelemetryService{}
	svc.initScrapeTimeouts()
	mfs, err := svc.metricsRegistry.Gather()
	if err != nil {
		t.Fatal(err)
	}
	var found bool
	var dest string
	for _, mf := range mfs {
		if mf.GetName() != droppedTotalMetric {
			continue
		}
		found = true
		if len(mf.Metric) > 0 {
			for _, lp := range mf.Metric[0].GetLabel() {
				if lp.GetName() == "destination" {
					dest = lp.GetValue()
				}
			}
		}
	}
	if !found {
		t.Fatalf("%s missing from /metrics registry", droppedTotalMetric)
	}
	if dest != "loki" {
		t.Errorf("destination label = %q, want loki", dest)
	}
}

func TestPushLogs_ForwardsAcceptedEntriesToLoki(t *testing.T) {
	var gotBody []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotBody, _ = io.ReadAll(r.Body)
		w.WriteHeader(http.StatusNoContent)
	}))
	t.Cleanup(srv.Close)

	signer := testSigner(t)
	dropped := testDroppedCounter()
	pusher, err := NewLokiPusher(LokiConfig{URL: srv.URL, QueueDepth: 10}, dropped)
	if err != nil {
		t.Fatal(err)
	}
	svc := &TelemetryService{BindAddr: ":0", Signer: signer, Loki: pusher}
	ctx := authedCtx(t, signer, "exporter:jumpstarter:real-exporter:uid1")

	resp, err := svc.PushLogs(ctx, &pb.PushLogsRequest{Entries: []*pb.LogEntry{{
		Severity:  "info",
		Message:   "from exporter",
		Component: "exporter",
		Exporter:  "spoofed",
		Namespace: "jumpstarter",
	}}})
	if err != nil {
		t.Fatal(err)
	}
	if resp.Dropped != 1 {
		t.Fatalf("Dropped = %d, want 1 (identity mismatch is not Loki backpressure)", resp.Dropped)
	}
	if n := len(pusher.queued()); n != 0 {
		t.Fatalf("identity-mismatch entries must not be queued for Loki, queued=%d", n)
	}

	resp, err = svc.PushLogs(ctx, &pb.PushLogsRequest{Entries: []*pb.LogEntry{{
		Severity:  "info",
		Message:   "ok",
		Component: "exporter",
		Exporter:  "real-exporter",
		Namespace: "jumpstarter",
		Client:    "ci",
	}}})
	if err != nil {
		t.Fatal(err)
	}
	if resp.Accepted != 1 {
		t.Fatalf("Accepted = %d, want 1", resp.Accepted)
	}
	if err := pusher.Flush(context.Background()); err != nil {
		t.Fatal(err)
	}
	var payload lokiPushPayload
	if err := json.Unmarshal(gotBody, &payload); err != nil {
		t.Fatalf("body %s: %v", gotBody, err)
	}
	if payload.Streams[0].Stream["exporter"] != "real-exporter" {
		t.Errorf("stream exporter = %q (must come from token, not the payload)", payload.Streams[0].Stream["exporter"])
	}
}

func counterValue(t *testing.T, vec *prometheus.CounterVec, dest string) float64 {
	t.Helper()
	m, err := vec.GetMetricWithLabelValues(dest)
	if err != nil {
		t.Fatal(err)
	}
	var pb dto.Metric
	if err := m.Write(&pb); err != nil {
		t.Fatal(err)
	}
	return pb.GetCounter().GetValue()
}

func TestLokiPush_InsecureSkipVerify(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	t.Cleanup(srv.Close)

	p, err := NewLokiPusher(LokiConfig{
		URL:                srv.URL,
		InsecureSkipVerify: true,
		QueueDepth:         4,
	}, testDroppedCounter())
	if err != nil {
		t.Fatal(err)
	}
	p.Enqueue(logEntry("tls"))
	if err := p.Flush(context.Background()); err != nil {
		t.Fatalf("Flush with insecureSkipVerify: %v", err)
	}
}
