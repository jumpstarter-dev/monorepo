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
	"sync"
	"time"

	pb "github.com/jumpstarter-dev/jumpstarter/controller/internal/protocol/jumpstarter/v1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"sigs.k8s.io/controller-runtime/pkg/log"
)

var errScrapeTimeout = errors.New("metrics scrape timeout")

const defaultScrapeTimeout = 7 * time.Second

// metricsConn is one authenticated MetricsStream. Scrapes are serialized
// because MetricsScrapeRequest has no scrape id (JEP-0013).
type metricsConn struct {
	id       exporterIdentity
	scrapeMu sync.Mutex
	mu       sync.Mutex
	send     func(*pb.MetricsStreamResponse) error
	// pending is non-nil only while a scrape is waiting for a response.
	pending chan *pb.MetricsScrapeResponse
	done    <-chan struct{}
	// started is closed when the Recv loop is running.
	started chan struct{}
}

func (s *TelemetryService) scrapeTimeoutDuration() time.Duration {
	if s.ScrapeTimeout > 0 {
		return s.ScrapeTimeout
	}
	return defaultScrapeTimeout
}

func (s *TelemetryService) initMetricsState() {
	s.stateMu.Lock()
	defer s.stateMu.Unlock()
	if s.conns == nil {
		s.conns = make(map[string]*metricsConn)
	}
}

func (s *TelemetryService) registerConn(c *metricsConn) {
	s.stateMu.Lock()
	defer s.stateMu.Unlock()
	if s.conns == nil {
		s.conns = make(map[string]*metricsConn)
	}
	s.conns[c.id.key()] = c
}

func (s *TelemetryService) unregisterConn(c *metricsConn) {
	s.stateMu.Lock()
	defer s.stateMu.Unlock()
	if s.conns == nil {
		return
	}
	if cur, ok := s.conns[c.id.key()]; ok && cur == c {
		delete(s.conns, c.id.key())
	}
}

func (s *TelemetryService) snapshotConns() []*metricsConn {
	s.stateMu.Lock()
	defer s.stateMu.Unlock()
	out := make([]*metricsConn, 0, len(s.conns))
	for _, c := range s.conns {
		out = append(out, c)
	}
	return out
}

// MetricsStream reverse-scrapes one exporter. The first message must be
// MetricsRegister whose identity matches the bearer token.
func (s *TelemetryService) MetricsStream(stream pb.TelemetryService_MetricsStreamServer) error {
	s.initMetricsState()
	id, err := s.authenticateExporter(stream.Context())
	if err != nil {
		return err
	}

	first, err := stream.Recv()
	if err != nil {
		return err
	}
	reg := first.GetRegister()
	if reg == nil {
		return status.Error(codes.InvalidArgument, "first MetricsStream message must be MetricsRegister")
	}
	if reg.GetIdentity() != id.name {
		return status.Errorf(codes.PermissionDenied, "register identity %q does not match authenticated exporter %q", reg.GetIdentity(), id.name)
	}

	done := make(chan struct{})
	conn := &metricsConn{
		id:      id,
		send:    stream.Send,
		done:    done,
		started: make(chan struct{}),
	}
	s.registerConn(conn)
	defer s.unregisterConn(conn)

	logger := log.FromContext(stream.Context()).WithName("telemetry").WithValues(
		"exporter", id.name,
		"namespace", id.namespace,
	)
	logger.Info("MetricsStream registered")

	var closeDone sync.Once
	finish := func() { closeDone.Do(func() { close(done) }) }
	defer finish()
	close(conn.started)

	for {
		msg, err := stream.Recv()
		if err != nil {
			return err
		}
		resp := msg.GetScrapeResponse()
		if resp == nil {
			return status.Error(codes.InvalidArgument, "expected MetricsScrapeResponse after register")
		}
		conn.mu.Lock()
		pending := conn.pending
		conn.pending = nil
		conn.mu.Unlock()
		if pending == nil {
			continue
		}
		select {
		case pending <- resp:
		case <-done:
			return nil
		case <-stream.Context().Done():
			return stream.Context().Err()
		}
	}
}

func (c *metricsConn) scrape(ctx context.Context, timeout time.Duration) ([]byte, error) {
	c.scrapeMu.Lock()
	defer c.scrapeMu.Unlock()

	reply := make(chan *pb.MetricsScrapeResponse, 1)
	c.mu.Lock()
	c.pending = reply
	c.mu.Unlock()
	defer func() {
		c.mu.Lock()
		if c.pending == reply {
			c.pending = nil
		}
		c.mu.Unlock()
	}()

	err := c.send(&pb.MetricsStreamResponse{
		Msg: &pb.MetricsStreamResponse_ScrapeRequest{
			ScrapeRequest: &pb.MetricsScrapeRequest{},
		},
	})
	if err != nil {
		return nil, err
	}

	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case resp := <-reply:
		if resp == nil {
			return nil, errScrapeTimeout
		}
		return resp.GetMetricsText(), nil
	case <-timer.C:
		return nil, errScrapeTimeout
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-c.done:
		return nil, context.Canceled
	}
}

func (s *TelemetryService) fanoutScrapes(ctx context.Context) []exporterSnapshot {
	s.initMetricsState()
	s.initScrapeTimeouts()
	timeout := s.scrapeTimeoutDuration()
	conns := s.snapshotConns()
	if len(conns) == 0 {
		return nil
	}

	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	var mu sync.Mutex
	var snaps []exporterSnapshot
	var wg sync.WaitGroup
	for _, c := range conns {
		wg.Add(1)
		go func(c *metricsConn) {
			defer wg.Done()
			text, err := c.scrape(ctx, timeout)
			if err != nil {
				if errors.Is(err, errScrapeTimeout) || errors.Is(err, context.DeadlineExceeded) {
					s.scrapeTimeouts.Inc()
				}
				log.FromContext(ctx).WithName("telemetry").V(1).Info("exporter scrape omitted",
					"exporter", c.id.name,
					"error", err.Error(),
				)
				return
			}
			mu.Lock()
			snaps = append(snaps, exporterSnapshot{name: c.id.name, text: text})
			mu.Unlock()
		}(c)
	}
	wg.Wait()
	return snaps
}
