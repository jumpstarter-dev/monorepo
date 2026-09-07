/*
Copyright 2024.

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
	"cmp"
	"context"
	"crypto/tls"
	"fmt"
	"net/http"
	"os"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"golang.org/x/exp/maps"

	gwruntime "github.com/grpc-ecosystem/grpc-gateway/v2/runtime"

	"github.com/golang-jwt/jwt/v5"
	"github.com/google/uuid"
	"github.com/grpc-ecosystem/go-grpc-middleware/v2/interceptors/recovery"
	"github.com/jumpstarter-dev/jumpstarter/controller/internal/authentication"
	"github.com/jumpstarter-dev/jumpstarter/controller/internal/authorization"
	"github.com/jumpstarter-dev/jumpstarter/controller/internal/config"
	jlog "github.com/jumpstarter-dev/jumpstarter/controller/internal/log"
	"github.com/jumpstarter-dev/jumpstarter/controller/internal/oidc"
	cpb "github.com/jumpstarter-dev/jumpstarter/controller/internal/protocol/jumpstarter/client/v1"
	pb "github.com/jumpstarter-dev/jumpstarter/controller/internal/protocol/jumpstarter/v1"
	"github.com/jumpstarter-dev/jumpstarter/controller/internal/service/auth"
	clientsvcv1 "github.com/jumpstarter-dev/jumpstarter/controller/internal/service/client/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/reflection"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/timestamppb"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/fields"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	k8suuid "k8s.io/apimachinery/pkg/util/uuid"
	"k8s.io/apimachinery/pkg/watch"
	"k8s.io/apiserver/pkg/authorization/authorizer"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	jumpstarterdevv1alpha1 "github.com/jumpstarter-dev/jumpstarter/controller/api/v1alpha1"
	"github.com/jumpstarter-dev/jumpstarter/controller/internal/controller"
	"google.golang.org/protobuf/proto"
)

// ControllerService exposes a gRPC service
type ControllerService struct {
	pb.UnimplementedControllerServiceServer
	Client           client.WithWatch
	Scheme           *runtime.Scheme
	Authn            authentication.ContextAuthenticator
	Authz            authorizer.Authorizer
	Attr             authorization.ContextAttributesGetter
	ServerOptions    []grpc.ServerOption
	Router           config.Router
	LeasePolicy      *config.LeasePolicy
	HiddenLabels     *config.HiddenLabels
	DeprecatedLabels *config.DeprecatedLabels
	Signer           *oidc.Signer
	TelemetryConfig  *config.Telemetry
	listenQueues     sync.Map
	leaseLocks       sync.Map
	authOnce         sync.Once
	auth             *auth.Auth
}

type listenQueue struct {
	ch        chan *pb.ListenResponse // buffered channel for router tokens
	done      chan struct{}           // closed when this queue is superseded or the listener exits
	closeOnce sync.Once               // prevents double-close panic on done
}

func (q *listenQueue) closeDone() {
	q.closeOnce.Do(func() { close(q.done) })
}

// leaseLock is a reference-counted mutex for a single lease. It lives in the
// leaseLocks sync.Map and is cleaned up when all goroutines release it.
type leaseLock struct {
	mu   sync.Mutex
	refs int32 // active users; cleaned up via CompareAndDelete when it hits 0
}

// acquireLeaseLock returns the per-lease mutex, creating one if needed.
// The caller MUST call releaseLeaseLock when done.
func (s *ControllerService) acquireLeaseLock(leaseName string) *sync.Mutex {
	for {
		v, loaded := s.leaseLocks.LoadOrStore(leaseName, &leaseLock{refs: 1})
		ll := v.(*leaseLock)
		if !loaded {
			return &ll.mu
		}
		newRefs := atomic.AddInt32(&ll.refs, 1)
		if newRefs <= 1 {
			// refs was 0 before our increment: a concurrent releaseLeaseLock
			// is tearing this entry down. Back out and retry so we don't end
			// up with a stale mutex that's no longer in the map.
			atomic.AddInt32(&ll.refs, -1)
			continue
		}
		return &ll.mu
	}
}

// releaseLeaseLock decrements the reference count for a lease's lock. When the
// last reference is released (refs hits 0), the entry is removed from the map
// via CompareAndDelete, which is safe against a concurrent acquireLeaseLock
// that may have already stored a new entry for the same key.
func (s *ControllerService) releaseLeaseLock(leaseName string) {
	v, ok := s.leaseLocks.Load(leaseName)
	if !ok {
		return
	}
	ll := v.(*leaseLock)
	if atomic.AddInt32(&ll.refs, -1) == 0 {
		s.leaseLocks.CompareAndDelete(leaseName, ll)
	}
}

// swapListenQueue atomically replaces the listen queue for a lease and signals
// the previous queue to stop. The per-lease lock serializes this with
// sendToListener so that Dial never sends a token to a superseded queue.
func (s *ControllerService) swapListenQueue(leaseName string, newQueue *listenQueue) {
	mu := s.acquireLeaseLock(leaseName)
	mu.Lock()
	old, loaded := s.listenQueues.Swap(leaseName, newQueue)
	if loaded {
		old.(*listenQueue).closeDone()
	}
	mu.Unlock()
	s.releaseLeaseLock(leaseName)
}

// sendToListener delivers a router token to the active listener for a lease.
// Called by Dial() to pass the (endpoint, token) pair to the exporter's
// Listen() goroutine so both sides can connect to the router.
//
// The per-lease mutex guarantees that the queue loaded here cannot be
// superseded between the Load and the channel send, eliminating the TOCTOU
// race where Dial loads a queue reference, a reconnecting Listen swaps in a
// new queue, and Dial sends to the old (now dead) queue.
//
// The send is non-blocking: if the channel buffer is full, we return
// ResourceExhausted immediately rather than blocking while holding the mutex
// (which would prevent a reconnecting Listen from swapping the queue,
// causing a deadlock chain).
func (s *ControllerService) sendToListener(_ context.Context, leaseName string, response *pb.ListenResponse) error {
	mu := s.acquireLeaseLock(leaseName)
	defer s.releaseLeaseLock(leaseName)
	mu.Lock()
	defer mu.Unlock()
	v, ok := s.listenQueues.Load(leaseName)
	if !ok {
		return status.Errorf(codes.Unavailable, "exporter is not listening on lease %s", leaseName)
	}
	q := v.(*listenQueue)
	// Reject if this queue was already superseded (done channel closed).
	select {
	case <-q.done:
		return status.Errorf(codes.Unavailable, "exporter is not listening on lease %s", leaseName)
	default:
	}
	// Non-blocking send: avoids holding the mutex while waiting for the
	// listener to drain the buffer.
	select {
	case q.ch <- response:
		return nil
	default:
		return status.Errorf(codes.ResourceExhausted, "listener buffer full on lease %s", leaseName)
	}
}

const defaultMaxTags int32 = 10

func (s *ControllerService) effectiveMaxTags() int32 {
	if s.LeasePolicy != nil && s.LeasePolicy.MaxTags > 0 {
		return s.LeasePolicy.MaxTags
	}
	return defaultMaxTags
}

func (s *ControllerService) effectiveHiddenLabelKeys() []string {
	if s.HiddenLabels != nil {
		return s.HiddenLabels.Keys
	}
	return nil
}

func (s *ControllerService) effectiveDeprecatedLabelKeys() map[string]string {
	if s.DeprecatedLabels != nil {
		return s.DeprecatedLabels.Keys
	}
	return nil
}

type wrappedStream struct {
	grpc.ServerStream
}

func (w *wrappedStream) Context() context.Context {
	return jlog.LogContext(w.ServerStream.Context())
}

// getAuth lazily builds the shared *auth.Auth from the service's immutable
// dependencies on first use. Lazy init (rather than init in Start) keeps bare
// &ControllerService{...} construction working, which tests rely on to call
// RPC methods without Start; sync.Once makes the first concurrent RPCs safe.
func (s *ControllerService) getAuth() *auth.Auth {
	s.authOnce.Do(func() {
		s.auth = auth.NewAuth(s.Client, s.Authn, s.Authz, s.Attr)
	})
	return s.auth
}

// authenticateClient and authenticateExporter delegate to the auth package,
// which owns auth-failure logging (with peer address) for all services.

func (s *ControllerService) authenticateClient(ctx context.Context) (*jumpstarterdevv1alpha1.Client, error) {
	return s.getAuth().VerifyClient(ctx)
}

func (s *ControllerService) authenticateExporter(ctx context.Context) (*jumpstarterdevv1alpha1.Exporter, error) {
	return s.getAuth().VerifyExporter(ctx)
}

func (s *ControllerService) Register(ctx context.Context, req *pb.RegisterRequest) (*pb.RegisterResponse, error) {
	logger := log.FromContext(ctx)

	exporter, err := s.authenticateExporter(ctx)
	if err != nil {
		return nil, err
	}

	logger = logger.WithValues("exporter", types.NamespacedName{
		Namespace: exporter.Namespace,
		Name:      exporter.Name,
	})

	logger.Info("Registering exporter")

	original := client.MergeFrom(exporter.DeepCopy())

	if exporter.Labels == nil {
		exporter.Labels = make(map[string]string)
	}

	for k := range exporter.Labels {
		if strings.HasPrefix(k, "jumpstarter.dev/") {
			delete(exporter.Labels, k)
		}
	}

	for k, v := range req.Labels {
		if strings.HasPrefix(k, "jumpstarter.dev/") {
			exporter.Labels[k] = v
		}
	}

	if err := s.Client.Patch(ctx, exporter, original); err != nil {
		logger.Error(err, "unable to update exporter")
		return nil, status.Errorf(codes.Internal, "unable to update exporter: %s", err)
	}

	original = client.MergeFrom(exporter.DeepCopy())

	devices := []jumpstarterdevv1alpha1.Device{}
	for _, device := range req.Reports {
		devices = append(devices, jumpstarterdevv1alpha1.Device{
			Uuid:       device.Uuid,
			ParentUuid: device.ParentUuid,
			Labels:     device.Labels,
		})
	}
	exporter.Status.Devices = devices

	if err := s.Client.Status().Patch(ctx, exporter, original); err != nil {
		logger.Error(err, "unable to update exporter status")
		return nil, status.Errorf(codes.Internal, "unable to update exporter status: %s", err)
	}

	return &pb.RegisterResponse{
		Uuid: string(exporter.UID),
	}, nil
}

// GetServiceEndpoints returns optional service endpoints (e.g. telemetry) for the caller to discover.
// Exporters call this after registration to find the telemetry service.
// An empty list means no optional services are deployed.
func (s *ControllerService) GetServiceEndpoints(
	ctx context.Context,
	req *pb.GetServiceEndpointsRequest,
) (*pb.GetServiceEndpointsResponse, error) {
	// Require a valid exporter token — endpoint discovery is not public.
	if _, err := s.authenticateExporter(ctx); err != nil {
		return nil, err
	}

	resp := &pb.GetServiceEndpointsResponse{}

	if s.TelemetryConfig != nil && s.TelemetryConfig.Enabled {
		// Endpoint is resolved at config-load time (ConfigMap value or GRPC_TELEMETRY_ENDPOINT
		// env var fallback), so TelemetryConfig.Endpoint is always the complete value here.
		if s.TelemetryConfig.Endpoint == "" {
			return nil, status.Error(codes.FailedPrecondition, "telemetry is enabled but no endpoint is configured; set telemetry.endpoint in the ConfigMap or GRPC_TELEMETRY_ENDPOINT on the controller pod")
		}
		resp.TelemetryEndpoints = append(resp.TelemetryEndpoints, &pb.TelemetryEndpoint{
			Endpoint:    s.TelemetryConfig.Endpoint,
			Certificate: s.TelemetryConfig.Certificate,
			MinSeverity: cmp.Or(s.TelemetryConfig.Logging.Filter.MinSeverity, "info"),
		})
	}

	return resp, nil
}

// RotateToken signs a new token for the authenticated exporter, updates the
// corresponding Secret in Kubernetes, and returns the new token and its expiry.
func (s *ControllerService) RotateToken(
	ctx context.Context,
	req *pb.RotateTokenRequest,
) (*pb.RotateTokenResponse, error) {
	exporter, err := s.authenticateExporter(ctx)
	if err != nil {
		return nil, err
	}

	if s.Signer == nil {
		return nil, status.Errorf(codes.FailedPrecondition, "token signer not configured")
	}

	token, err := s.Signer.Token(exporter.InternalSubject())
	if err != nil {
		return nil, status.Errorf(codes.Internal, "failed to sign token: %s", err)
	}

	secretName := exporter.Name + "-exporter"
	var secret corev1.Secret
	if err := s.Client.Get(ctx, types.NamespacedName{
		Namespace: exporter.Namespace,
		Name:      secretName,
	}, &secret); err != nil {
		return nil, status.Errorf(codes.Internal, "failed to get credential secret: %s", err)
	}

	original := client.MergeFrom(secret.DeepCopy())
	if secret.Data == nil {
		secret.Data = map[string][]byte{}
	}
	secret.Data[controller.TokenKey] = []byte(token)
	if err := s.Client.Patch(ctx, &secret, original); err != nil {
		return nil, status.Errorf(codes.Internal, "failed to update credential secret: %s", err)
	}

	log.FromContext(ctx).Info("token rotated", "exporter", exporter.Name, "namespace", exporter.Namespace)

	claims := &struct {
		jwt.RegisteredClaims
	}{}
	parser := jwt.NewParser(jwt.WithoutClaimsValidation())
	if _, _, err := parser.ParseUnverified(token, claims); err != nil {
		return nil, status.Errorf(codes.Internal, "failed to parse token claims: %s", err)
	}

	var expiry *timestamppb.Timestamp
	if claims.ExpiresAt != nil {
		expiry = timestamppb.New(claims.ExpiresAt.Time)
	}

	return &pb.RotateTokenResponse{
		Token:  token,
		Expiry: expiry,
	}, nil
}

func (s *ControllerService) Unregister(
	ctx context.Context,
	req *pb.UnregisterRequest,
) (
	*pb.UnregisterResponse,
	error,
) {
	logger := log.FromContext(ctx)

	exporter, err := s.authenticateExporter(ctx)
	if err != nil {
		return nil, err
	}

	logger = logger.WithValues("exporter", types.NamespacedName{
		Namespace: exporter.Namespace,
		Name:      exporter.Name,
	})

	original := client.MergeFrom(exporter.DeepCopy())
	exporter.Status.Devices = nil

	applyUnregisterStatus(exporter, req.GetReason())

	if err := s.Client.Status().Patch(ctx, exporter, original); err != nil {
		logger.Error(err, "unable to update exporter status")
		return nil, status.Errorf(codes.Internal, "unable to update exporter status: %s", err)
	}

	logger.Info("exporter unregistered, marked offline")

	return &pb.UnregisterResponse{}, nil
}

func (s *ControllerService) ReportStatus(
	ctx context.Context,
	req *pb.ReportStatusRequest,
) (*pb.ReportStatusResponse, error) {
	logger := log.FromContext(ctx)

	exporter, err := s.authenticateExporter(ctx)
	if err != nil {
		return nil, err
	}

	logger = logger.WithValues("exporter", types.NamespacedName{
		Namespace: exporter.Namespace,
		Name:      exporter.Name,
	})

	// Convert proto enum to CRD string value
	exporterStatus := protoStatusToString(req.Status)

	logger.Info("Exporter reporting status", "state", exporterStatus, "message", req.GetMessage())

	original := client.MergeFrom(exporter.DeepCopy())

	exporter.Status.ExporterStatusValue = exporterStatus
	exporter.Status.StatusMessage = req.GetMessage()
	// Also update LastSeen to keep the exporter marked as online
	exporter.Status.LastSeen = metav1.Now()

	// Sync the Online condition with the reported status for consistency
	// This ensures the deprecated Online boolean field stays consistent with ExporterStatusValue
	syncOnlineConditionWithStatus(exporter)

	if err := s.Client.Status().Patch(ctx, exporter, original); err != nil {
		logger.Error(err, "unable to update exporter status")
		return nil, status.Errorf(codes.Internal, "unable to update exporter status: %s", err)
	}

	// Handle lease release request from exporter
	// This allows the exporter to signal that the lease should be released after
	// the afterLease hook completes, ensuring leases are always released even if
	// the client disconnects unexpectedly.
	if req.GetReleaseLease() {
		if err := s.handleExporterLeaseRelease(ctx, exporter); err != nil {
			logger.Error(err, "failed to release lease for exporter")
			// Don't fail the status report, just log the error
			// The client can still release the lease as a fallback
		}
	}

	return &pb.ReportStatusResponse{}, nil
}

// handleExporterLeaseRelease handles a lease release request from an exporter.
// This is called when the exporter sets release_lease=true in ReportStatus,
// typically after the afterLease hook completes.
func (s *ControllerService) handleExporterLeaseRelease(
	ctx context.Context,
	exporter *jumpstarterdevv1alpha1.Exporter,
) error {
	logger := log.FromContext(ctx)

	// Check if exporter has an active lease
	if exporter.Status.LeaseRef == nil {
		logger.Info("No active lease to release for exporter")
		return nil
	}

	// Get the lease
	var lease jumpstarterdevv1alpha1.Lease
	if err := s.Client.Get(ctx, types.NamespacedName{
		Namespace: exporter.Namespace,
		Name:      exporter.Status.LeaseRef.Name,
	}, &lease); err != nil {
		return fmt.Errorf("failed to get lease: %w", err)
	}

	// Verify the lease is actually held by this exporter
	if lease.Status.ExporterRef == nil || lease.Status.ExporterRef.Name != exporter.Name {
		return fmt.Errorf("lease %s is not held by exporter %s", lease.Name, exporter.Name)
	}

	// Check if lease is already ended or marked for release
	if lease.Status.Ended || lease.Spec.Release {
		logger.Info("Lease already ended or marked for release", "lease", lease.Name)
		return nil
	}

	// Set the release flag to trigger lease end via the reconciler
	original := client.MergeFrom(lease.DeepCopy())
	lease.Spec.Release = true

	if err := s.Client.Patch(ctx, &lease, original); err != nil {
		return fmt.Errorf("failed to mark lease for release: %w", err)
	}

	logger.Info("Lease marked for release by exporter",
		"lease", lease.Name,
		"exporter", exporter.Name)

	return nil
}

// protoStatusToString converts the proto ExporterStatus enum to the CRD string value
func protoStatusToString(status pb.ExporterStatus) string {
	switch status {
	case pb.ExporterStatus_EXPORTER_STATUS_OFFLINE:
		return jumpstarterdevv1alpha1.ExporterStatusOffline
	case pb.ExporterStatus_EXPORTER_STATUS_AVAILABLE:
		return jumpstarterdevv1alpha1.ExporterStatusAvailable
	case pb.ExporterStatus_EXPORTER_STATUS_BEFORE_LEASE_HOOK:
		return jumpstarterdevv1alpha1.ExporterStatusBeforeLeaseHook
	case pb.ExporterStatus_EXPORTER_STATUS_LEASE_READY:
		return jumpstarterdevv1alpha1.ExporterStatusLeaseReady
	case pb.ExporterStatus_EXPORTER_STATUS_AFTER_LEASE_HOOK:
		return jumpstarterdevv1alpha1.ExporterStatusAfterLeaseHook
	case pb.ExporterStatus_EXPORTER_STATUS_BEFORE_LEASE_HOOK_FAILED:
		return jumpstarterdevv1alpha1.ExporterStatusBeforeLeaseHookFailed
	case pb.ExporterStatus_EXPORTER_STATUS_AFTER_LEASE_HOOK_FAILED:
		return jumpstarterdevv1alpha1.ExporterStatusAfterLeaseHookFailed
	default:
		return jumpstarterdevv1alpha1.ExporterStatusUnspecified
	}
}

func applyUnregisterStatus(exporter *jumpstarterdevv1alpha1.Exporter, reason string) {
	exporter.Status.ExporterStatusValue = jumpstarterdevv1alpha1.ExporterStatusOffline
	statusMessage := reason
	if statusMessage == "" {
		statusMessage = "Exporter unregistered"
	}
	exporter.Status.StatusMessage = statusMessage
	syncOnlineConditionWithStatus(exporter)
	meta.SetStatusCondition(&exporter.Status.Conditions, metav1.Condition{
		Type:               string(jumpstarterdevv1alpha1.ExporterConditionTypeRegistered),
		Status:             metav1.ConditionFalse,
		ObservedGeneration: exporter.Generation,
		Reason:             "Unregister",
		Message:            "Exporter unregistered from the controller and became offline.",
	})
}

// syncOnlineConditionWithStatus updates the Online condition based on ExporterStatusValue.
// This ensures the deprecated Online boolean field in the protobuf API stays consistent
// with the new ExporterStatusValue field.
func syncOnlineConditionWithStatus(exporter *jumpstarterdevv1alpha1.Exporter) {
	isOnline := exporter.Status.ExporterStatusValue != jumpstarterdevv1alpha1.ExporterStatusOffline &&
		exporter.Status.ExporterStatusValue != jumpstarterdevv1alpha1.ExporterStatusUnspecified &&
		exporter.Status.ExporterStatusValue != ""

	if isOnline {
		meta.SetStatusCondition(&exporter.Status.Conditions, metav1.Condition{
			Type:               string(jumpstarterdevv1alpha1.ExporterConditionTypeOnline),
			Status:             metav1.ConditionTrue,
			ObservedGeneration: exporter.Generation,
			Reason:             "StatusReported",
			Message:            fmt.Sprintf("Exporter reported status: %s", exporter.Status.ExporterStatusValue),
		})
	} else {
		meta.SetStatusCondition(&exporter.Status.Conditions, metav1.Condition{
			Type:               string(jumpstarterdevv1alpha1.ExporterConditionTypeOnline),
			Status:             metav1.ConditionFalse,
			ObservedGeneration: exporter.Generation,
			Reason:             "Offline",
			Message:            exporter.Status.StatusMessage,
		})
	}
}

// checkExporterStatusForDriverCalls validates that the exporter is in a status
// that allows driver calls. This check is performed by the controller before
// allowing clients to dial, so we can reject immediately even if the exporter
// is offline or in an invalid state.
//
// Allowed statuses:
//   - LeaseReady: Normal operation, lease is active
//   - BeforeLeaseHook: Hook is running, allows j commands from hooks
//   - AfterLeaseHook: Hook is running, allows j commands from hooks
//   - Unspecified/"": Backwards compatibility with old exporters that don't report status
func checkExporterStatusForDriverCalls(exporterStatus string) error {
	switch exporterStatus {
	case jumpstarterdevv1alpha1.ExporterStatusLeaseReady,
		jumpstarterdevv1alpha1.ExporterStatusBeforeLeaseHook,
		jumpstarterdevv1alpha1.ExporterStatusAfterLeaseHook:
		return nil
	case jumpstarterdevv1alpha1.ExporterStatusUnspecified, "":
		// Allow for backwards compatibility with old exporters that don't report status.
		// The exporter-side check will still validate if it's a new exporter.
		return nil
	case jumpstarterdevv1alpha1.ExporterStatusOffline:
		return status.Errorf(codes.FailedPrecondition, "exporter is offline")
	case jumpstarterdevv1alpha1.ExporterStatusAvailable:
		return status.Errorf(codes.FailedPrecondition, "exporter is not ready (status: Available)")
	case jumpstarterdevv1alpha1.ExporterStatusBeforeLeaseHookFailed:
		return status.Errorf(codes.FailedPrecondition, "exporter beforeLease hook failed")
	case jumpstarterdevv1alpha1.ExporterStatusAfterLeaseHookFailed:
		return status.Errorf(codes.FailedPrecondition, "exporter afterLease hook failed")
	default:
		return status.Errorf(codes.FailedPrecondition, "exporter not ready (status: %s)", exporterStatus)
	}
}

func (s *ControllerService) Listen(req *pb.ListenRequest, stream pb.ControllerService_ListenServer) error {
	ctx := stream.Context()
	logger := log.FromContext(ctx)

	exporter, err := s.authenticateExporter(ctx)
	if err != nil {
		return err
	}

	logger = logger.WithValues("exporter", types.NamespacedName{
		Namespace: exporter.Namespace,
		Name:      exporter.Name,
	})

	leaseName := req.GetLeaseName()
	if leaseName == "" {
		err := fmt.Errorf("empty lease name")
		logger.Error(err, "lease name not specified in dial request")
		return err
	}

	logger = logger.WithValues("lease_id", leaseName)

	var lease jumpstarterdevv1alpha1.Lease
	if err := s.Client.Get(
		ctx,
		types.NamespacedName{Namespace: exporter.Namespace, Name: leaseName},
		&lease,
	); err != nil {
		logger.Error(err, "unable to get lease")
		return err
	}

	if lease.Status.ExporterRef == nil || lease.Status.ExporterRef.Name != exporter.Name {
		err := fmt.Errorf("permission denied")
		logger.Error(err, "lease not held by exporter")
		return err
	}

	// Each Listen() goroutine gets its own listenQueue with a fresh channel,
	// so a reconnecting exporter never shares a channel with the old goroutine
	// (which would cause tokens to be delivered to the wrong reader).
	wrapper := &listenQueue{
		ch:   make(chan *pb.ListenResponse, 8),
		done: make(chan struct{}),
	}
	listenMu := s.acquireLeaseLock(leaseName)
	// Swap our queue in, signaling any previous listener to exit.
	s.swapListenQueue(leaseName, wrapper)
	defer func() {
		listenMu.Lock()
		wrapper.closeDone()
		listenMu.Unlock()
		s.listenQueues.CompareAndDelete(leaseName, wrapper)
		s.releaseLeaseLock(leaseName)
	}()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-wrapper.done:
			// We've been superseded by a reconnecting Listen. Drain any
			// tokens already buffered in our channel so they still get
			// delivered to the exporter before we exit.
			for {
				select {
				case msg := <-wrapper.ch:
					if err := stream.Send(msg); err != nil {
						return err
					}
				default:
					return nil
				}
			}
		case msg := <-wrapper.ch:
			if err := stream.Send(msg); err != nil {
				return err
			}
		}
	}
}

// Status is a stream of status updates for the exporter.
// It is used to:
//   - Notify the exporter of the current status of the lease
//   - Track the exporter's last seen time
func (s *ControllerService) Status(req *pb.StatusRequest, stream pb.ControllerService_StatusServer) error {
	ctx := stream.Context()
	logger := log.FromContext(ctx)

	exporter, err := s.authenticateExporter(ctx)
	if err != nil {
		return err
	}

	logger = logger.WithValues("exporter", types.NamespacedName{
		Namespace: exporter.Namespace,
		Name:      exporter.Name,
	})

	watcher, err := s.Client.Watch(ctx, &jumpstarterdevv1alpha1.ExporterList{}, &client.ListOptions{
		FieldSelector: fields.OneTermEqualSelector("metadata.name", exporter.Name),
		Namespace:     exporter.Namespace,
	})
	if err != nil {
		logger.Error(err, "failed to watch exporter")
		return err
	}

	defer watcher.Stop()

	ticker := time.NewTicker(time.Second * 10)

	defer ticker.Stop()

	// use this to track that we are getting updates from the k8s watcher
	var watchedLastSeen *metav1.Time

	online := func() {
		original := client.MergeFrom(exporter.DeepCopy())
		exporter.Status.LastSeen = metav1.Now()

		if err = s.Client.Status().Patch(ctx, exporter, original); err != nil {
			logger.Error(err, "unable to update exporter status.lastSeen")
		}
	}

	// ticker does not tick instantly, thus calling online immediately once
	// https://github.com/golang/go/issues/17601
	select {
	case <-ctx.Done():
		return nil
	default:
		// When the Status stream first connects, the exporter is alive.
		// Clear any "Offline" ExporterStatusValue that may have been set by the
		// reconciler when the exporter was temporarily disconnected.
		// This is essential for old exporters (v0.7.x) that never call
		// ReportStatus and thus can't clear their own Offline status.
		original := client.MergeFrom(exporter.DeepCopy())
		exporter.Status.LastSeen = metav1.Now()
		if exporter.Status.ExporterStatusValue == jumpstarterdevv1alpha1.ExporterStatusOffline {
			logger.Info("Clearing stale Offline status on Status stream connect")
			exporter.Status.ExporterStatusValue = ""
			exporter.Status.StatusMessage = ""
		}
		if err = s.Client.Status().Patch(ctx, exporter, original); err != nil {
			logger.Error(err, "unable to update exporter status on initial connect")
		}
	}

	var lastPbStatusResponse *pb.StatusResponse
	for {
		select {
		case <-ctx.Done():
			logger.Info("Status stream terminated normally")
			return nil
		case <-ticker.C:
			// the k8s watchers sometimes stop functioning silently, so we need to detect it
			// by comparing the last seen time from the k8s watcher with the last seen time
			// from the exporter object we set in the online() function
			if watchedLastSeen != nil && !watchedLastSeen.Equal(&exporter.Status.LastSeen) {
				logger.Info("The exporter watcher seems to have stopped, terminating status stream")
				return fmt.Errorf("last seen time mismatch")
			}
			online()
		case result, ok := <-watcher.ResultChan():
			// Check if the watch channel has been closed
			if !ok {
				logger.Info("Watch channel closed, terminating status stream")
				return fmt.Errorf("watch channel closed")
			}

			switch result.Type {
			case watch.Added, watch.Modified, watch.Deleted:
				exporter = result.Object.(*jumpstarterdevv1alpha1.Exporter)
				// track the last seen time from the k8s watcher, so we can detect if
				// the watcher stops functioning
				watchedLastSeen = exporter.Status.LastSeen.DeepCopy()

				leased := exporter.Status.LeaseRef != nil
				leaseName := (*string)(nil)
				clientName := (*string)(nil)
				var leaseContext map[string]string

				if leased {
					leaseName = &exporter.Status.LeaseRef.Name
					var lease jumpstarterdevv1alpha1.Lease
					if err := s.Client.Get(
						ctx,
						types.NamespacedName{Namespace: exporter.Namespace, Name: *leaseName},
						&lease,
					); err != nil {
						logger.Error(err, "failed to get lease on exporter")
						return err
					}
					clientName = &lease.Spec.ClientRef.Name
					leaseContext = lease.Spec.Context
				}

				status := pb.StatusResponse{
					Leased:     leased,
					LeaseName:  leaseName,
					ClientName: clientName,
					Context:    leaseContext,
				}
				if proto.Equal(lastPbStatusResponse, &status) {
					jlog.Verbose(logger, "Not sending status update to exporter, it is the same as the last one")
				} else {
					logger.Info("Sending status update to exporter", "status", fmt.Sprintf("%+v", &status))
					if err = stream.Send(&status); err != nil {
						logger.Error(err, "Failed to send status update to exporter")
						return err
					}
					lastPbStatusResponse = proto.Clone(&status).(*pb.StatusResponse)
				}
			case watch.Error:
				logger.Error(fmt.Errorf("%+v", result.Object), "Received error when watching exporter")
				return fmt.Errorf("received error when watching exporter")
			}
		}
	}
}

func (s *ControllerService) Dial(ctx context.Context, req *pb.DialRequest) (*pb.DialResponse, error) {
	logger := log.FromContext(ctx)

	client, err := s.authenticateClient(ctx)
	if err != nil {
		return nil, err
	}

	logger = logger.WithValues("client", types.NamespacedName{
		Namespace: client.Namespace,
		Name:      client.Name,
	})

	leaseName := req.GetLeaseName()
	if leaseName == "" {
		err := fmt.Errorf("empty lease name")
		logger.Error(err, "lease name not specified in dial request")
		return nil, err
	}

	logger = logger.WithValues("lease_id", leaseName)

	var lease jumpstarterdevv1alpha1.Lease
	if err := s.Client.Get(
		ctx,
		types.NamespacedName{Namespace: client.Namespace, Name: leaseName},
		&lease,
	); err != nil {
		logger.Error(err, "unable to get lease")
		return nil, err
	}

	if lease.Spec.ClientRef.Name != client.Name {
		err := fmt.Errorf("permission denied")
		logger.Error(err, "lease not held by client")
		return nil, err
	}

	if lease.Status.ExporterRef == nil {
		err := fmt.Errorf("lease not active")
		logger.Error(err, "unable to get exporter referenced by lease")
		return nil, err
	}

	var exporter jumpstarterdevv1alpha1.Exporter
	if err := s.Client.Get(ctx,
		types.NamespacedName{Namespace: client.Namespace, Name: lease.Status.ExporterRef.Name}, &exporter); err != nil {
		logger.Error(err, "unable to get exporter referenced by lease")
		return nil, err
	}

	// Check if exporter status allows driver calls.
	// If the exporter is in "Available" status, this is a transient state during
	// lease setup (the exporter hasn't yet transitioned to LeaseReady). We retry
	// briefly server-side to handle the race condition, which also protects old
	// clients that don't have client-side Dial retry logic (issue #309).
	{
		retryDelay := 500 * time.Millisecond
		maxDelay := 3 * time.Second
		maxTotalWait := 30 * time.Second
		deadline := time.Now().Add(maxTotalWait)
		var statusErr error
		for attempt := 0; ; attempt++ {
			statusErr = checkExporterStatusForDriverCalls(exporter.Status.ExporterStatusValue)
			if statusErr == nil {
				break
			}
			// Only retry for Available status (transient during lease setup).
			// Other error statuses (Offline, HookFailed, etc.) are not transient.
			if exporter.Status.ExporterStatusValue != jumpstarterdevv1alpha1.ExporterStatusAvailable {
				break
			}
			if time.Now().Add(retryDelay).After(deadline) {
				// Next sleep would exceed the deadline; stop retrying.
				break
			}
			logger.Info("Exporter in Available status, waiting for lease setup",
				"attempt", attempt+1, "retryDelay", retryDelay)
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			case <-time.After(retryDelay):
			}
			// Exponential backoff capped at maxDelay.
			retryDelay = min(retryDelay*2, maxDelay)
			// Re-fetch exporter status
			if err := s.Client.Get(ctx,
				types.NamespacedName{Namespace: client.Namespace, Name: lease.Status.ExporterRef.Name}, &exporter); err != nil {
				logger.Error(err, "unable to re-fetch exporter during Dial retry")
				return nil, err
			}
		}
		if statusErr != nil {
			logger.Info("Dial rejected due to exporter status",
				"status", exporter.Status.ExporterStatusValue,
				"error", statusErr.Error())
			return nil, statusErr
		}
	}

	candidates := maps.Values(s.Router)
	slices.SortFunc(candidates, func(a config.RouterEntry, b config.RouterEntry) int {
		return -cmp.Compare(MatchLabels(a.Labels, exporter.Labels), MatchLabels(b.Labels, exporter.Labels))
	})

	if len(candidates) == 0 {
		err := fmt.Errorf("no router available")
		logger.Error(err, "no router available")
		return nil, err
	}

	logger.Info("selected router", "endpoint", candidates[0].Endpoint, "labels", candidates[0].Labels)

	endpoint := candidates[0].Endpoint

	stream := k8suuid.NewUUID()

	token, err := jwt.NewWithClaims(jwt.SigningMethodHS256, jwt.RegisteredClaims{
		Issuer:    "https://jumpstarter.dev/stream",
		Subject:   string(stream),
		Audience:  []string{"https://jumpstarter.dev/router"},
		ExpiresAt: jwt.NewNumericDate(time.Now().Add(time.Minute * 30)),
		NotBefore: jwt.NewNumericDate(time.Now()),
		IssuedAt:  jwt.NewNumericDate(time.Now()),
		ID:        string(k8suuid.NewUUID()),
	}).SignedString([]byte(os.Getenv("ROUTER_KEY")))

	if err != nil {
		logger.Error(err, "unable to sign token")
		return nil, status.Errorf(codes.Internal, "unable to sign token")
	}

	response := &pb.ListenResponse{
		RouterEndpoint: endpoint,
		RouterToken:    token,
	}

	if err := s.sendToListener(ctx, leaseName, response); err != nil {
		return nil, err
	}

	logger.Info("Client dial assigned stream", "stream", stream)
	return &pb.DialResponse{
		RouterEndpoint: endpoint,
		RouterToken:    token,
	}, nil
}

func (s *ControllerService) GetLease(
	ctx context.Context,
	req *pb.GetLeaseRequest,
) (*pb.GetLeaseResponse, error) {
	client, err := s.authenticateClient(ctx)
	if err != nil {
		return nil, err
	}

	var lease jumpstarterdevv1alpha1.Lease
	if err := s.Client.Get(ctx, types.NamespacedName{
		Namespace: client.Namespace,
		Name:      req.Name,
	}, &lease); err != nil {
		return nil, err
	}

	if lease.Spec.ClientRef.Name != client.Name {
		return nil, fmt.Errorf("GetLease permission denied")
	}

	var matchExpressions []*pb.LabelSelectorRequirement
	for _, exp := range lease.Spec.Selector.MatchExpressions {
		matchExpressions = append(matchExpressions, &pb.LabelSelectorRequirement{
			Key:      exp.Key,
			Operator: string(exp.Operator),
			Values:   exp.Values,
		})
	}

	var beginTime *timestamppb.Timestamp
	if lease.Status.BeginTime != nil {
		beginTime = timestamppb.New(lease.Status.BeginTime.Time)
	}
	var endTime *timestamppb.Timestamp
	if lease.Status.EndTime != nil {
		endTime = timestamppb.New(lease.Status.EndTime.Time)
	}
	var exporterUuid *string
	if lease.Status.ExporterRef != nil {
		var exporter jumpstarterdevv1alpha1.Exporter
		if err := s.Client.Get(
			ctx,
			types.NamespacedName{Namespace: client.Namespace, Name: lease.Status.ExporterRef.Name},
			&exporter,
		); err != nil {
			return nil, fmt.Errorf("GetLease fetch exporter uuid failed")
		}
		exporterUuid = (*string)(&exporter.UID)
	}

	var conditions []*pb.Condition
	for _, condition := range lease.Status.Conditions {
		conditions = append(conditions, &pb.Condition{
			Type:               &condition.Type,
			Status:             (*string)(&condition.Status),
			ObservedGeneration: &condition.ObservedGeneration,
			LastTransitionTime: &pb.Time{
				Seconds: &condition.LastTransitionTime.ProtoTime().Seconds,
				Nanos:   &condition.LastTransitionTime.ProtoTime().Nanos,
			},
			Reason:  &condition.Reason,
			Message: &condition.Message,
		})
	}

	resp := &pb.GetLeaseResponse{
		Selector:     &pb.LabelSelector{MatchExpressions: matchExpressions, MatchLabels: lease.Spec.Selector.MatchLabels},
		BeginTime:    beginTime,
		EndTime:      endTime,
		ExporterUuid: exporterUuid,
		Conditions:   conditions,
	}
	if lease.Spec.Duration != nil {
		resp.Duration = durationpb.New(lease.Spec.Duration.Duration)
	}
	return resp, nil
}

func (s *ControllerService) RequestLease(
	ctx context.Context,
	req *pb.RequestLeaseRequest,
) (*pb.RequestLeaseResponse, error) {
	client, err := s.authenticateClient(ctx)
	if err != nil {
		return nil, err
	}

	var matchLabels map[string]string
	var matchExpressions []metav1.LabelSelectorRequirement
	if req.Selector != nil {
		matchLabels = req.Selector.MatchLabels
		for _, exp := range req.Selector.MatchExpressions {
			matchExpressions = append(matchExpressions, metav1.LabelSelectorRequirement{
				Key:      exp.Key,
				Operator: metav1.LabelSelectorOperator(exp.Operator),
				Values:   exp.Values,
			})
		}
	}

	leaseName, err := uuid.NewV7()
	if err != nil {
		return nil, err
	}

	var lease = jumpstarterdevv1alpha1.Lease{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: client.Namespace,
			Name:      leaseName.String(),
		},
		Spec: jumpstarterdevv1alpha1.LeaseSpec{
			ClientRef: corev1.LocalObjectReference{
				Name: client.Name,
			},
			Selector: metav1.LabelSelector{
				MatchLabels:      matchLabels,
				MatchExpressions: matchExpressions,
			},
		},
	}
	if req.Duration != nil {
		lease.Spec.Duration = &metav1.Duration{Duration: req.Duration.AsDuration()}
	}
	if err := s.Client.Create(ctx, &lease); err != nil {
		return nil, err
	}

	return &pb.RequestLeaseResponse{
		Name: lease.Name,
	}, nil
}

func (s *ControllerService) releaseLeaseAsExporter(
	ctx context.Context,
	exporter *jumpstarterdevv1alpha1.Exporter,
	req *pb.ReleaseLeaseRequest,
) (*pb.ReleaseLeaseResponse, error) {
	var lease jumpstarterdevv1alpha1.Lease
	if err := s.Client.Get(ctx, types.NamespacedName{
		Namespace: exporter.Namespace,
		Name:      req.Name,
	}, &lease); err != nil {
		if apierrors.IsNotFound(err) {
			return nil, status.Errorf(codes.NotFound, "lease %s not found", req.Name)
		}
		return nil, status.Errorf(codes.Internal, "failed to get lease: %s", err)
	}

	if lease.Status.ExporterRef == nil || lease.Status.ExporterRef.Name != exporter.Name {
		return nil, status.Errorf(codes.FailedPrecondition,
			"lease %s is not held by exporter %s", req.Name, exporter.Name)
	}

	// Idempotent: already ended or marked for release
	if lease.Status.Ended || lease.Spec.Release {
		return &pb.ReleaseLeaseResponse{}, nil
	}

	original := client.MergeFrom(lease.DeepCopy())
	lease.Spec.Release = true

	if err := s.Client.Patch(ctx, &lease, original); err != nil {
		return nil, status.Errorf(codes.Internal, "failed to mark lease for release: %s", err)
	}

	return &pb.ReleaseLeaseResponse{}, nil
}

func (s *ControllerService) ReleaseLease(
	ctx context.Context,
	req *pb.ReleaseLeaseRequest,
) (*pb.ReleaseLeaseResponse, error) {
	// Route based on jumpstarter-kind metadata to avoid spurious auth failures
	if s.getAuth().IsExporter(ctx) {
		exporter, err := s.authenticateExporter(ctx)
		if err != nil {
			return nil, err
		}
		return s.releaseLeaseAsExporter(ctx, exporter, req)
	}

	jclient, err := s.authenticateClient(ctx)
	if err != nil {
		return nil, err
	}

	var lease jumpstarterdevv1alpha1.Lease
	if err := s.Client.Get(ctx, types.NamespacedName{
		Namespace: jclient.Namespace,
		Name:      req.Name,
	}, &lease); err != nil {
		return nil, err
	}

	if lease.Spec.ClientRef.Name != jclient.Name {
		return nil, fmt.Errorf("ReleaseLease permission denied")
	}

	// Idempotent: already ended or marked for release
	if lease.Status.Ended || lease.Spec.Release {
		return &pb.ReleaseLeaseResponse{}, nil
	}

	original := client.MergeFrom(lease.DeepCopy())
	lease.Spec.Release = true

	if err := s.Client.Patch(ctx, &lease, original); err != nil {
		return nil, err
	}

	return &pb.ReleaseLeaseResponse{}, nil
}

func (s *ControllerService) ListLeases(
	ctx context.Context,
	req *pb.ListLeasesRequest,
) (*pb.ListLeasesResponse, error) {
	jclient, err := s.authenticateClient(ctx)
	if err != nil {
		return nil, err
	}

	var leases jumpstarterdevv1alpha1.LeaseList
	if err := s.Client.List(
		ctx,
		&leases,
		client.InNamespace(jclient.Namespace),
		controller.MatchingActiveLeases(),
	); err != nil {
		return nil, err
	}

	var leaseNames []string
	for _, lease := range leases.Items {
		if lease.Spec.ClientRef.Name == jclient.Name {
			leaseNames = append(leaseNames, lease.Name)
		}
	}

	return &pb.ListLeasesResponse{
		Names: leaseNames,
	}, nil
}

func (s *ControllerService) Start(ctx context.Context) error {
	logger := log.FromContext(ctx)

	dnsnames, ipaddresses, err := endpointToSAN(controllerEndpoint())
	if err != nil {
		return err
	}

	cert, _, err := LoadTLSCertificate("jumpstarter controller", dnsnames, ipaddresses)
	if err != nil {
		return err
	}

	opts := append(s.ServerOptions,
		grpc.ChainUnaryInterceptor(func(
			gctx context.Context,
			req any,
			_ *grpc.UnaryServerInfo,
			handler grpc.UnaryHandler,
		) (resp any, err error) {
			return handler(jlog.LogContext(gctx), req)
		}, recovery.UnaryServerInterceptor()),
		grpc.ChainStreamInterceptor(func(
			srv any,
			ss grpc.ServerStream,
			_ *grpc.StreamServerInfo,
			handler grpc.StreamHandler,
		) error {
			return handler(srv, &wrappedStream{ServerStream: ss})
		}, recovery.StreamServerInterceptor()),
	)
	server := grpc.NewServer(opts...)

	pb.RegisterControllerServiceServer(server, s)
	cpb.RegisterClientServiceServer(
		server,
		clientsvcv1.NewClientService(s.Client, *s.getAuth(), s.effectiveMaxTags(), s.Signer, s.effectiveHiddenLabelKeys(), s.effectiveDeprecatedLabelKeys()),
	)

	// Register the standard gRPC health checking service (grpc.health.v1.Health).
	// This is unauthenticated by design so that monitoring tools (Kubernetes
	// liveness/readiness probes, load balancers, etc.) can call Check/Watch
	// without credentials.
	hs := health.NewServer()
	healthpb.RegisterHealthServer(server, hs)
	hs.SetServingStatus("", healthpb.HealthCheckResponse_SERVING)

	// Register reflection service on gRPC server.
	reflection.Register(server)

	// Register gRPC gateway
	gwmux := gwruntime.NewServeMux()

	// The controller multiplexes gRPC (h2) and REST (http/1.1) on a single port,
	// so it needs NextProtos — which LoadTLSCredentials doesn't expose.
	listener, err := tls.Listen("tcp", ":8082", &tls.Config{
		Certificates: []tls.Certificate{*cert},
		MinVersion:   tls.VersionTLS12,
		NextProtos:   []string{"http/1.1", "h2"},
	})
	if err != nil {
		return err
	}

	logger.Info("Starting Controller grpc service on port 8082")

	go func() {
		<-ctx.Done()
		logger.Info("Stopping Controller gRPC service")
		server.Stop()
	}()

	return http.Serve(listener, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.ProtoMajor == 2 && strings.HasPrefix(
			r.Header.Get("Content-Type"), "application/grpc") {
			server.ServeHTTP(w, r)
		} else {
			gwmux.ServeHTTP(w, r)
		}
	}))
}

// SetupWithManager sets up the controller with the Manager.
func (s *ControllerService) SetupWithManager(mgr ctrl.Manager) error {
	return mgr.Add(s)
}
