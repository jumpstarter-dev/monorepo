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
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	pb "github.com/jumpstarter-dev/jumpstarter/controller/internal/protocol/jumpstarter/v1"
	"github.com/prometheus/client_golang/prometheus"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const (
	defaultLokiQueueDepth = 10000
	droppedTotalMetric    = "jumpstarter_telemetry_dropped_total"
	droppedDestination    = "loki"
	lokiPushPath          = "/loki/api/v1/push"
	lokiFlushInterval     = 500 * time.Millisecond
	lokiHTTPTimeout       = 10 * time.Second
)

// LokiConfig is the telemetry-side Loki HTTP push configuration.
type LokiConfig struct {
	URL                string
	Username           string
	Password           string
	Token              string
	CAFile             string
	InsecureSkipVerify bool
	QueueDepth         int
}

type lokiPushPayload struct {
	Streams []lokiStream `json:"streams"`
}

type lokiStream struct {
	Stream map[string]string `json:"stream"`
	Values [][]string        `json:"values"`
}

// LokiPusher is a bounded ring buffer that POSTs LogEntry batches to Loki.
type LokiPusher struct {
	url        string
	username   string
	password   string
	token      string
	client     *http.Client
	queueDepth int
	dropped    prometheus.Counter
	now        func() time.Time

	mu        sync.Mutex
	entries   []*pb.LogEntry
	dropCount int
	dropFirst time.Time
}

func normalizeQueueDepth(d int) int {
	if d <= 0 {
		return defaultLokiQueueDepth
	}
	if d < 2 {
		return 2
	}
	return d
}

func normalizeLokiPushURL(raw string) (string, error) {
	u, err := url.Parse(raw)
	if err != nil {
		return "", fmt.Errorf("loki url: %w", err)
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return "", fmt.Errorf("loki url: unsupported scheme %q (use http or https)", u.Scheme)
	}
	if u.Path == "" || u.Path == "/" {
		u.Path = lokiPushPath
	}
	return u.String(), nil
}

func lokiHTTPClient(cfg LokiConfig) (*http.Client, error) {
	transport, ok := http.DefaultTransport.(*http.Transport)
	if !ok {
		return &http.Client{Timeout: lokiHTTPTimeout}, nil
	}
	transport = transport.Clone()
	tlsCfg := &tls.Config{MinVersion: tls.VersionTLS12}
	customTLS := false
	if cfg.InsecureSkipVerify {
		tlsCfg.InsecureSkipVerify = true
		customTLS = true
	}
	if cfg.CAFile != "" {
		pem, err := os.ReadFile(cfg.CAFile)
		if err != nil {
			return nil, fmt.Errorf("loki ca file: %w", err)
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(pem) {
			return nil, fmt.Errorf("loki ca file: no certificates found in %s", cfg.CAFile)
		}
		tlsCfg.RootCAs = pool
		customTLS = true
	}
	if customTLS {
		transport.TLSClientConfig = tlsCfg
	}
	return &http.Client{Timeout: lokiHTTPTimeout, Transport: transport}, nil
}

// NewLokiPusher returns nil, nil when URL is empty (metrics-only). grpc:// is rejected.
func NewLokiPusher(cfg LokiConfig, dropped *prometheus.CounterVec) (*LokiPusher, error) {
	raw := strings.TrimSpace(cfg.URL)
	if raw == "" {
		return nil, nil
	}
	u, err := url.Parse(raw)
	if err != nil {
		return nil, fmt.Errorf("loki url: %w", err)
	}
	if u.Scheme == "grpc" || u.Scheme == "grpcs" {
		return nil, fmt.Errorf("loki gRPC scheme %q is not implemented; use http:// or https://", u.Scheme)
	}
	pushURL, err := normalizeLokiPushURL(raw)
	if err != nil {
		return nil, err
	}
	client, err := lokiHTTPClient(cfg)
	if err != nil {
		return nil, err
	}
	var counter prometheus.Counter
	if dropped != nil {
		counter = dropped.WithLabelValues(droppedDestination)
	}
	return &LokiPusher{
		url:        pushURL,
		username:   cfg.Username,
		password:   cfg.Password,
		token:      cfg.Token,
		client:     client,
		queueDepth: normalizeQueueDepth(cfg.QueueDepth),
		dropped:    counter,
		now:        time.Now,
	}, nil
}

func (p *LokiPusher) maxReal() int {
	n := p.queueDepth - 1
	if n < 1 {
		return 1
	}
	return n
}

// Enqueue is non-blocking. On overflow, entries are replaced by a single drop marker.
func (p *LokiPusher) Enqueue(entry *pb.LogEntry) {
	if p == nil || entry == nil {
		return
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if len(p.entries) < p.maxReal() {
		p.entries = append(p.entries, entry)
		return
	}
	if p.dropCount == 0 {
		p.dropFirst = p.now()
	}
	p.dropCount++
	if p.dropped != nil {
		p.dropped.Inc()
	}
}

func makeDropMarker(count int, first, now time.Time) *pb.LogEntry {
	window := 0
	if !first.IsZero() {
		window = int(now.Sub(first).Seconds())
	}
	return &pb.LogEntry{
		Timestamp: timestamppb.New(now),
		Severity:  "warning",
		Message:   "log entries dropped due to Loki backpressure",
		Component: "telemetry",
		Operation: "backpressure",
		ExtraFields: map[string]string{
			"count":          strconv.Itoa(count),
			"window_seconds": strconv.Itoa(window),
		},
	}
}

func (p *LokiPusher) dropMarkerLocked() *pb.LogEntry {
	return makeDropMarker(p.dropCount, p.dropFirst, p.now())
}

func (p *LokiPusher) queued() []*pb.LogEntry {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := append([]*pb.LogEntry(nil), p.entries...)
	if p.dropCount > 0 {
		out = append(out, p.dropMarkerLocked())
	}
	return out
}

func (p *LokiPusher) takeBatch() (entries []*pb.LogEntry, dropCount int, dropFirst time.Time) {
	p.mu.Lock()
	defer p.mu.Unlock()
	entries = p.entries
	p.entries = nil
	dropCount = p.dropCount
	dropFirst = p.dropFirst
	p.dropCount = 0
	p.dropFirst = time.Time{}
	return entries, dropCount, dropFirst
}

func (p *LokiPusher) restoreBatch(entries []*pb.LogEntry, dropCount int, dropFirst time.Time) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.entries = append(entries, p.entries...)
	if dropCount > 0 {
		if p.dropCount == 0 || (!dropFirst.IsZero() && (p.dropFirst.IsZero() || dropFirst.Before(p.dropFirst))) {
			p.dropFirst = dropFirst
		}
		p.dropCount += dropCount
	}
}

// Flush POSTs queued entries to Loki. On HTTP failure the buffer is restored.
func (p *LokiPusher) Flush(ctx context.Context) error {
	if p == nil {
		return nil
	}
	entries, dropCount, dropFirst := p.takeBatch()
	batch := append([]*pb.LogEntry(nil), entries...)
	if dropCount > 0 {
		batch = append(batch, makeDropMarker(dropCount, dropFirst, p.now()))
	}
	if len(batch) == 0 {
		return nil
	}
	if err := p.push(ctx, batch); err != nil {
		p.restoreBatch(entries, dropCount, dropFirst)
		return err
	}
	return nil
}

func (p *LokiPusher) push(ctx context.Context, entries []*pb.LogEntry) error {
	body, err := json.Marshal(buildLokiPayload(entries, p.now()))
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, p.url, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	if p.token != "" {
		req.Header.Set("Authorization", "Bearer "+p.token)
	} else if p.username != "" || p.password != "" {
		req.SetBasicAuth(p.username, p.password)
	}
	resp, err := p.client.Do(req)
	if err != nil {
		return err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode/100 != 2 {
		slurp, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
		return fmt.Errorf("loki push %s: %s", resp.Status, bytes.TrimSpace(slurp))
	}
	return nil
}

// Run flushes the buffer periodically until ctx is cancelled, then flushes once more.
func (p *LokiPusher) Run(ctx context.Context) {
	if p == nil {
		return
	}
	ticker := time.NewTicker(lokiFlushInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			flushCtx, cancel := context.WithTimeout(context.Background(), lokiHTTPTimeout)
			_ = p.Flush(flushCtx)
			cancel()
			return
		case <-ticker.C:
			_ = p.Flush(ctx)
		}
	}
}

func buildLokiPayload(entries []*pb.LogEntry, now time.Time) lokiPushPayload {
	type acc struct {
		labels map[string]string
		values [][]string
	}
	order := make([]string, 0)
	grouped := make(map[string]*acc)
	for _, e := range entries {
		labels := lokiStreamLabels(e)
		key := labels["component"] + "\x00" + labels["exporter"] + "\x00" + labels["namespace"]
		g, ok := grouped[key]
		if !ok {
			g = &acc{labels: labels}
			grouped[key] = g
			order = append(order, key)
		}
		g.values = append(g.values, []string{unixNanoString(e, now), logLineJSON(e)})
	}
	streams := make([]lokiStream, 0, len(order))
	for _, key := range order {
		g := grouped[key]
		streams = append(streams, lokiStream{Stream: g.labels, Values: g.values})
	}
	return lokiPushPayload{Streams: streams}
}

func lokiStreamLabels(e *pb.LogEntry) map[string]string {
	m := make(map[string]string, 3)
	if e.Component != "" {
		m["component"] = e.Component
	}
	if e.Exporter != "" {
		m["exporter"] = e.Exporter
	}
	if e.Namespace != "" {
		m["namespace"] = e.Namespace
	}
	return m
}

func unixNanoString(e *pb.LogEntry, now time.Time) string {
	ts := now
	if e.Timestamp != nil && e.Timestamp.IsValid() {
		ts = e.Timestamp.AsTime()
	}
	return strconv.FormatInt(ts.UnixNano(), 10)
}

func logLineJSON(e *pb.LogEntry) string {
	body := map[string]string{}
	if e.Message != "" {
		body["msg"] = e.Message
	}
	if e.Severity != "" {
		body["severity"] = e.Severity
	}
	if e.Component != "" {
		body["component"] = e.Component
	}
	if e.Exporter != "" {
		body["exporter"] = e.Exporter
	}
	if e.Namespace != "" {
		body["namespace"] = e.Namespace
	}
	if e.Lease != "" {
		body["lease"] = e.Lease
	}
	if e.Client != "" {
		body["client"] = e.Client
	}
	if e.Operation != "" {
		body["operation"] = e.Operation
	}
	if e.Result != "" {
		body["result"] = e.Result
	}
	if e.DriverType != "" {
		body["driver_type"] = e.DriverType
	}
	if e.Timestamp != nil && e.Timestamp.IsValid() {
		body["ts"] = e.Timestamp.AsTime().Format(time.RFC3339Nano)
	}
	for k, v := range e.ExtraFields {
		if _, reserved := reservedExtraFieldKeys[k]; reserved {
			continue
		}
		if k == "" {
			continue
		}
		body[k] = v
	}
	b, err := json.Marshal(body)
	if err != nil {
		return `{"msg":"marshal error"}`
	}
	return string(b)
}
