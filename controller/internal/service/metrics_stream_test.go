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
	"errors"
	"io"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"

	pb "github.com/jumpstarter-dev/jumpstarter/controller/internal/protocol/jumpstarter/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
)

const bufconnSize = 1024 * 1024

func startTestHub(t *testing.T, timeout time.Duration) (*TelemetryService, pb.TelemetryServiceClient, string) {
	t.Helper()
	signer := testSigner(t)
	svc := &TelemetryService{
		Signer:          signer,
		MetricsBindAddr: "127.0.0.1:0",
		ScrapeTimeout:   timeout,
		DriverTypeEnum:  DefaultDriverTypeEnum,
		ExemplarKeys:    DefaultExemplarKeys,
	}
	svc.grpcReady.Store(true)

	shutdown, err := svc.startMetricsHTTP()
	if err != nil {
		t.Fatalf("startMetricsHTTP: %v", err)
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_ = shutdown(ctx)
	})

	lis := bufconn.Listen(bufconnSize)
	gs := grpc.NewServer()
	pb.RegisterTelemetryServiceServer(gs, svc)
	go func() { _ = gs.Serve(lis) }()
	t.Cleanup(func() {
		gs.Stop()
		_ = lis.Close()
	})

	conn, err := grpc.NewClient("passthrough:///bufnet",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
			return lis.DialContext(ctx)
		}),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		t.Fatalf("grpc.NewClient: %v", err)
	}
	t.Cleanup(func() { _ = conn.Close() })

	return svc, pb.NewTelemetryServiceClient(conn), svc.metricsAddr
}

func exporterStreamCtx(t *testing.T, svc *TelemetryService, subject string) context.Context {
	t.Helper()
	token, err := svc.Signer.Token(subject)
	if err != nil {
		t.Fatalf("Token: %v", err)
	}
	return metadata.NewOutgoingContext(
		context.Background(),
		metadata.Pairs("authorization", "Bearer "+token),
	)
}

func waitRegistered(t *testing.T, svc *TelemetryService, n int) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		conns := svc.snapshotConns()
		if len(conns) != n {
			time.Sleep(10 * time.Millisecond)
			continue
		}
		ready := true
		for _, c := range conns {
			if c.started == nil {
				ready = false
				break
			}
			select {
			case <-c.started:
			default:
				ready = false
			}
			if !ready {
				break
			}
		}
		if ready {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %d MetricsStream connections, have %d", n, len(svc.snapshotConns()))
}

func httpGet(t *testing.T, url string) (int, string) {
	t.Helper()
	client := &http.Client{Timeout: 3 * time.Second}
	var resp *http.Response
	var lastErr error
	for i := 0; i < 30; i++ {
		resp, lastErr = client.Get(url)
		if lastErr == nil {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if lastErr != nil {
		t.Fatalf("GET %s: %v", url, lastErr)
	}
	defer func() { _ = resp.Body.Close() }()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("read body: %v", err)
	}
	return resp.StatusCode, string(body)
}

func TestHealthzAndReadyz(t *testing.T) {
	_, _, addr := startTestHub(t, 200*time.Millisecond)
	code, body := httpGet(t, "http://"+addr+"/healthz")
	if code != http.StatusOK || !strings.Contains(body, "ok") {
		t.Fatalf("healthz status=%d body=%q", code, body)
	}
	code, body = httpGet(t, "http://"+addr+"/readyz")
	if code != http.StatusOK || !strings.Contains(body, "ok") {
		t.Fatalf("readyz status=%d body=%q", code, body)
	}
}

func TestReadyzNotReadyWhenGRPCDown(t *testing.T) {
	svc := &TelemetryService{MetricsBindAddr: "127.0.0.1:0"}
	shutdown, err := svc.startMetricsHTTP()
	if err != nil {
		t.Fatalf("startMetricsHTTP: %v", err)
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_ = shutdown(ctx)
	})
	code, _ := httpGet(t, "http://"+svc.metricsAddr+"/readyz")
	if code != http.StatusServiceUnavailable {
		t.Fatalf("readyz status=%d, want 503", code)
	}
}

func TestMetricsStream_RejectsIdentityMismatch(t *testing.T) {
	svc, client, _ := startTestHub(t, time.Second)
	ctx := exporterStreamCtx(t, svc, "exporter:jumpstarter:alice:uid1")
	stream, err := client.MetricsStream(ctx)
	if err != nil {
		t.Fatalf("MetricsStream: %v", err)
	}
	if err := stream.Send(&pb.MetricsStreamRequest{
		Msg: &pb.MetricsStreamRequest_Register{
			Register: &pb.MetricsRegister{Identity: "bob"},
		},
	}); err != nil {
		t.Fatalf("Send register: %v", err)
	}
	_, err = stream.Recv()
	if err == nil {
		t.Fatal("expected PermissionDenied, got nil")
	}
	if status.Code(err) != codes.PermissionDenied {
		t.Fatalf("code = %v, want PermissionDenied (%v)", status.Code(err), err)
	}
}

func TestMetricsStream_RejectsNonRegisterFirstMessage(t *testing.T) {
	svc, client, _ := startTestHub(t, time.Second)
	ctx := exporterStreamCtx(t, svc, "exporter:jumpstarter:alice:uid1")
	stream, err := client.MetricsStream(ctx)
	if err != nil {
		t.Fatalf("MetricsStream: %v", err)
	}
	if err := stream.Send(&pb.MetricsStreamRequest{
		Msg: &pb.MetricsStreamRequest_ScrapeResponse{
			ScrapeResponse: &pb.MetricsScrapeResponse{MetricsText: []byte("# EOF\n")},
		},
	}); err != nil {
		t.Fatalf("Send: %v", err)
	}
	_, err = stream.Recv()
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("code = %v, want InvalidArgument (%v)", status.Code(err), err)
	}
}

func TestMetricsStream_RejectsNonExporterToken(t *testing.T) {
	svc, client, _ := startTestHub(t, time.Second)
	ctx := exporterStreamCtx(t, svc, "client:jumpstarter:ci:uid1")
	stream, err := client.MetricsStream(ctx)
	if err != nil {
		t.Fatalf("MetricsStream: %v", err)
	}
	if err := stream.Send(&pb.MetricsStreamRequest{
		Msg: &pb.MetricsStreamRequest_Register{
			Register: &pb.MetricsRegister{Identity: "ci"},
		},
	}); err != nil {
		t.Fatalf("Send: %v", err)
	}
	_, err = stream.Recv()
	if status.Code(err) != codes.PermissionDenied {
		t.Fatalf("code = %v, want PermissionDenied (%v)", status.Code(err), err)
	}
}

func TestMetricsConn_ScrapeTimeout(t *testing.T) {
	done := make(chan struct{})
	c := &metricsConn{
		send: func(*pb.MetricsStreamResponse) error { return nil },
		done: done,
	}
	_, err := c.scrape(context.Background(), 40*time.Millisecond)
	if !errors.Is(err, errScrapeTimeout) {
		t.Fatalf("err = %v, want errScrapeTimeout", err)
	}
}

func TestFanout_TimeoutIncrementsCounterWithoutGRPC(t *testing.T) {
	svc := &TelemetryService{ScrapeTimeout: 40 * time.Millisecond}
	svc.initScrapeTimeouts()
	done := make(chan struct{})
	c := &metricsConn{
		id:   exporterIdentity{namespace: "jumpstarter", name: "slow"},
		send: func(*pb.MetricsStreamResponse) error { return nil },
		done: done,
	}
	svc.registerConn(c)
	snaps := svc.fanoutScrapes(context.Background())
	if len(snaps) != 0 {
		t.Fatalf("timed-out scrape must be omitted, got %d snapshots", len(snaps))
	}
	mfs, err := svc.metricsRegistry.Gather()
	if err != nil {
		t.Fatalf("Gather: %v", err)
	}
	var value float64
	var found bool
	for _, mf := range mfs {
		if mf.GetName() == scrapeTimeoutsMetric || mf.GetName()+"_total" == scrapeTimeoutsMetric {
			found = true
			if len(mf.Metric) > 0 {
				value = mf.Metric[0].GetCounter().GetValue()
			}
		}
	}
	if !found || value < 1 {
		t.Fatalf("scrape timeout counter = %v found=%v, want >= 1", value, found)
	}
}

func TestFanout_TimeoutOmitsExporterAndIncrementsCounter(t *testing.T) {
	svc, client, addr := startTestHub(t, 150*time.Millisecond)
	ctx := exporterStreamCtx(t, svc, "exporter:jumpstarter:slow:uid1")
	stream, err := client.MetricsStream(ctx)
	if err != nil {
		t.Fatalf("MetricsStream: %v", err)
	}
	if err := stream.Send(&pb.MetricsStreamRequest{
		Msg: &pb.MetricsStreamRequest_Register{
			Register: &pb.MetricsRegister{Identity: "slow"},
		},
	}); err != nil {
		t.Fatalf("Send register: %v", err)
	}
	// Drain scrape requests so Send on the server does not block, but never reply.
	go func() {
		for {
			if _, err := stream.Recv(); err != nil {
				return
			}
		}
	}()
	waitRegistered(t, svc, 1)

	code, body := httpGet(t, "http://"+addr+"/metrics")
	if code != http.StatusOK {
		t.Fatalf("GET /metrics status=%d body=%s", code, body)
	}
	if strings.Contains(body, "jumpstarter_operations_total") {
		t.Errorf("timed-out exporter metrics must be omitted, body:\n%s", body)
	}
	mfs, err := svc.metricsRegistry.Gather()
	if err != nil {
		t.Fatalf("Gather: %v", err)
	}
	var value float64
	var found bool
	for _, mf := range mfs {
		if mf.GetName() == scrapeTimeoutsMetric || mf.GetName()+"_total" == scrapeTimeoutsMetric {
			found = true
			if len(mf.Metric) > 0 {
				value = mf.Metric[0].GetCounter().GetValue()
			}
		}
	}
	if !found || value < 1 {
		t.Fatalf("scrape timeout counter = %v found=%v body:\n%s", value, found, body)
	}
}

func TestFanout_MergesOpenMetricsFromConnectedExporter(t *testing.T) {
	svc, client, addr := startTestHub(t, time.Second)
	ctx := exporterStreamCtx(t, svc, "exporter:jumpstarter:sidekick:uid1")
	stream, err := client.MetricsStream(ctx)
	if err != nil {
		t.Fatalf("MetricsStream: %v", err)
	}
	if err := stream.Send(&pb.MetricsStreamRequest{
		Msg: &pb.MetricsStreamRequest_Register{
			Register: &pb.MetricsRegister{Identity: "sidekick"},
		},
	}); err != nil {
		t.Fatalf("Send register: %v", err)
	}

	snapshot := []byte(`# TYPE jumpstarter_operations_total counter
# HELP jumpstarter_operations_total Total operations performed.
jumpstarter_operations_total{exporter="spoofed",operation="on",result="success",driver_type="tuya"} 4.0
# EOF
`)
	errCh := make(chan error, 1)
	go func() {
		for {
			msg, err := stream.Recv()
			if err != nil {
				errCh <- err
				return
			}
			if msg.GetScrapeRequest() == nil {
				continue
			}
			if err := stream.Send(&pb.MetricsStreamRequest{
				Msg: &pb.MetricsStreamRequest_ScrapeResponse{
					ScrapeResponse: &pb.MetricsScrapeResponse{MetricsText: snapshot},
				},
			}); err != nil {
				errCh <- err
				return
			}
		}
	}()
	waitRegistered(t, svc, 1)

	code, body := httpGet(t, "http://"+addr+"/metrics")
	if code != http.StatusOK {
		t.Fatalf("GET /metrics status=%d body=%s", code, body)
	}
	if !strings.Contains(body, `exporter="sidekick"`) {
		t.Errorf("authenticated exporter label missing:\n%s", body)
	}
	if strings.Contains(body, `exporter="spoofed"`) {
		t.Errorf("spoofed exporter label must be overwritten:\n%s", body)
	}
	if !strings.Contains(body, `driver_type="other"`) {
		t.Errorf("unknown driver_type must remap to other:\n%s", body)
	}
	select {
	case err := <-errCh:
		if err != nil && err != io.EOF && status.Code(err) != codes.Canceled && status.Code(err) != codes.Unavailable {
			t.Fatalf("stream goroutine: %v", err)
		}
	default:
	}
}

func TestFanout_UnparseableSnapshotIncrementsParseErrorsOnSameResponse(t *testing.T) {
	svc, client, addr := startTestHub(t, time.Second)
	ctx := exporterStreamCtx(t, svc, "exporter:jumpstarter:sidekick:uid1")
	stream, err := client.MetricsStream(ctx)
	if err != nil {
		t.Fatalf("MetricsStream: %v", err)
	}
	if err := stream.Send(&pb.MetricsStreamRequest{
		Msg: &pb.MetricsStreamRequest_Register{
			Register: &pb.MetricsRegister{Identity: "sidekick"},
		},
	}); err != nil {
		t.Fatalf("Send register: %v", err)
	}

	errCh := make(chan error, 1)
	go func() {
		for {
			msg, err := stream.Recv()
			if err != nil {
				errCh <- err
				return
			}
			if msg.GetScrapeRequest() == nil {
				continue
			}
			if err := stream.Send(&pb.MetricsStreamRequest{
				Msg: &pb.MetricsStreamRequest_ScrapeResponse{
					ScrapeResponse: &pb.MetricsScrapeResponse{MetricsText: []byte(pythonOpenMetricsWithExemplar)},
				},
			}); err != nil {
				errCh <- err
				return
			}
		}
	}()
	waitRegistered(t, svc, 1)

	code, body := httpGet(t, "http://"+addr+"/metrics")
	if code != http.StatusOK {
		t.Fatalf("GET /metrics status=%d body=%s", code, body)
	}
	if strings.Contains(body, "jumpstarter_operation_duration_seconds") {
		t.Errorf("unparseable exporter snapshot must be omitted, body:\n%s", body)
	}
	if !strings.Contains(body, metricsParseErrorsMetric) || !strings.Contains(body, `exporter="sidekick"`) {
		t.Errorf("parse-error counter missing from same /metrics response:\n%s", body)
	}

	mfs, err := svc.metricsRegistry.Gather()
	if err != nil {
		t.Fatalf("Gather: %v", err)
	}
	value := labeledCounterValue(t, mfs, metricsParseErrorsMetric, labelExporter, "sidekick")
	if value < 1 {
		t.Fatalf("%s{exporter=sidekick} = %v, want >= 1 body:\n%s", metricsParseErrorsMetric, value, body)
	}

	select {
	case err := <-errCh:
		if err != nil && err != io.EOF && status.Code(err) != codes.Canceled && status.Code(err) != codes.Unavailable {
			t.Fatalf("stream goroutine: %v", err)
		}
	default:
	}
}
