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
	"time"

	"golang.org/x/exp/maps"

	gwruntime "github.com/grpc-ecosystem/grpc-gateway/v2/runtime"

	"github.com/golang-jwt/jwt/v5"
	"github.com/google/uuid"
	"github.com/grpc-ecosystem/go-grpc-middleware/v2/interceptors/recovery"
	"github.com/jumpstarter-dev/jumpstarter-controller/internal/authentication"
	"github.com/jumpstarter-dev/jumpstarter-controller/internal/authorization"
	"github.com/jumpstarter-dev/jumpstarter-controller/internal/config"
	jlog "github.com/jumpstarter-dev/jumpstarter-controller/internal/log"
	"github.com/jumpstarter-dev/jumpstarter-controller/internal/oidc"
	cpb "github.com/jumpstarter-dev/jumpstarter-controller/internal/protocol/jumpstarter/client/v1"
	pb "github.com/jumpstarter-dev/jumpstarter-controller/internal/protocol/jumpstarter/v1"
	"github.com/jumpstarter-dev/jumpstarter-controller/internal/service/auth"
	clientsvcv1 "github.com/jumpstarter-dev/jumpstarter-controller/internal/service/client/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/peer"
	"google.golang.org/grpc/reflection"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/timestamppb"
	corev1 "k8s.io/api/core/v1"
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

	jumpstarterdevv1alpha1 "github.com/jumpstarter-dev/jumpstarter-controller/api/v1alpha1"
	"github.com/jumpstarter-dev/jumpstarter-controller/internal/controller"
	"google.golang.org/protobuf/proto"
)

// ControllerService exposes a gRPC service
type ControllerService struct {
	pb.UnimplementedControllerServiceServer
	Client       client.WithWatch
	Scheme       *runtime.Scheme
	Authn        authentication.ContextAuthenticator
	Authz        authorizer.Authorizer
	Attr         authorization.ContextAttributesGetter
	ServerOption grpc.ServerOption
	Router       config.Router
	listenQueues sync.Map
}

type wrappedStream struct {
	grpc.ServerStream
}

func logContext(ctx context.Context) context.Context {
	p, ok := peer.FromContext(ctx)
	if ok {
		return log.IntoContext(ctx, log.FromContext(ctx, "peer", p.Addr))
	}
	return ctx
}

func (w *wrappedStream) Context() context.Context {
	return logContext(w.ServerStream.Context())
}

func (s *ControllerService) authenticateClient(ctx context.Context) (*jumpstarterdevv1alpha1.Client, error) {
	return oidc.VerifyClientObjectToken(
		ctx,
		s.Authn,
		s.Authz,
		s.Attr,
		s.Client,
	)
}

func (s *ControllerService) authenticateExporter(ctx context.Context) (*jumpstarterdevv1alpha1.Exporter, error) {
	return oidc.VerifyExporterObjectToken(
		ctx,
		s.Authn,
		s.Authz,
		s.Attr,
		s.Client,
	)
}

func (s *ControllerService) Register(ctx context.Context, req *pb.RegisterRequest) (*pb.RegisterResponse, error) {
	logger := log.FromContext(ctx)

	exporter, err := s.authenticateExporter(ctx)
	if err != nil {
		logger.Info("unable to authenticate exporter", "error", err.Error())
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
		logger.Error(err, "unable to authenticate exporter")
		return nil, err
	}

	logger = logger.WithValues("exporter", types.NamespacedName{
		Namespace: exporter.Namespace,
		Name:      exporter.Name,
	})

	original := client.MergeFrom(exporter.DeepCopy())
	exporter.Status.Devices = nil

	if err := s.Client.Status().Patch(ctx, exporter, original); err != nil {
		logger.Error(err, "unable to update exporter status")
		return nil, status.Errorf(codes.Internal, "unable to update exporter status: %s", err)
	}

	logger.Info("exporter unregistered, updated as unavailable")

	return &pb.UnregisterResponse{}, nil
}

func (s *ControllerService) ReportStatus(
	ctx context.Context,
	req *pb.ReportStatusRequest,
) (*pb.ReportStatusResponse, error) {
	logger := log.FromContext(ctx)

	exporter, err := s.authenticateExporter(ctx)
	if err != nil {
		logger.Info("unable to authenticate exporter", "error", err.Error())
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

	logger.WithValues("lease", types.NamespacedName{
		Namespace: exporter.Namespace,
		Name:      leaseName,
	})

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

	queue, _ := s.listenQueues.LoadOrStore(leaseName, make(chan *pb.ListenResponse, 8))
	for {
		select {
		case <-ctx.Done():
			return nil
		case msg := <-queue.(chan *pb.ListenResponse):
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
		logger.Error(err, "unable to authenticate exporter")
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
		online()
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
				}

				status := pb.StatusResponse{
					Leased:     leased,
					LeaseName:  leaseName,
					ClientName: clientName,
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
		logger.Error(err, "unable to authenticate client")
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

	logger = logger.WithValues("lease", types.NamespacedName{
		Namespace: client.Namespace,
		Name:      leaseName,
	})

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

	// Check if exporter status allows driver calls
	// This validates before the client connects, so we can reject immediately
	// even if the exporter is offline or in an invalid state
	if err := checkExporterStatusForDriverCalls(exporter.Status.ExporterStatusValue); err != nil {
		logger.Info("Dial rejected due to exporter status",
			"status", exporter.Status.ExporterStatusValue,
			"error", err.Error())
		return nil, err
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

	queue, _ := s.listenQueues.LoadOrStore(leaseName, make(chan *pb.ListenResponse, 8))
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case queue.(chan *pb.ListenResponse) <- response:
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

func (s *ControllerService) ReleaseLease(
	ctx context.Context,
	req *pb.ReleaseLeaseRequest,
) (*pb.ReleaseLeaseResponse, error) {
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

	// Load external certificate if provided via environment variables.
	// Environment variables EXTERNAL_CERT_PEM and EXTERNAL_KEY_PEM should contain the PEM-encoded
	// certificate and private key respectively. If both are set, they are used; otherwise
	// a self-signed certificate is generated.
	var cert *tls.Certificate
	certPEMPath := os.Getenv("EXTERNAL_CERT_PEM")
	keyPEMPath := os.Getenv("EXTERNAL_KEY_PEM")
	if certPEMPath != "" && keyPEMPath != "" {
		certPEMBytes, err := os.ReadFile(certPEMPath)
		if err != nil {
			return fmt.Errorf("failed to read external certificate file: %w", err)
		}
		keyPEMBytes, err := os.ReadFile(keyPEMPath)
		if err != nil {
			return fmt.Errorf("failed to read external key file: %w", err)
		}
		parsedCert, err := tls.X509KeyPair(certPEMBytes, keyPEMBytes)
		if err != nil {
			return fmt.Errorf("failed to parse external certificate: %w", err)
		}
		cert = &parsedCert
	} else {
		cert, err = NewSelfSignedCertificate("jumpstarter controller", dnsnames, ipaddresses)
		if err != nil {
			return err
		}
	}

	server := grpc.NewServer(
		s.ServerOption,
		grpc.ChainUnaryInterceptor(func(
			gctx context.Context,
			req any,
			_ *grpc.UnaryServerInfo,
			handler grpc.UnaryHandler,
		) (resp any, err error) {
			return handler(logContext(gctx), req)
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

	pb.RegisterControllerServiceServer(server, s)
	cpb.RegisterClientServiceServer(
		server,
		clientsvcv1.NewClientService(s.Client, *auth.NewAuth(s.Client, s.Authn, s.Authz, s.Attr)),
	)

	// Register reflection service on gRPC server.
	reflection.Register(server)

	// Register gRPC gateway
	gwmux := gwruntime.NewServeMux()

	listener, err := tls.Listen("tcp", ":8082", &tls.Config{
		Certificates: []tls.Certificate{*cert},
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
