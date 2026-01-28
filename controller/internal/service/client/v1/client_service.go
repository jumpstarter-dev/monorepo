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

package v1

import (
	"context"
	"fmt"

	"github.com/google/uuid"
	jumpstarterdevv1alpha1 "github.com/jumpstarter-dev/jumpstarter-controller/api/v1alpha1"
	cpb "github.com/jumpstarter-dev/jumpstarter-controller/internal/protocol/jumpstarter/client/v1"
	"github.com/jumpstarter-dev/jumpstarter-controller/internal/service/auth"
	"github.com/jumpstarter-dev/jumpstarter-controller/internal/service/utils"
	"google.golang.org/protobuf/types/known/emptypb"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/selection"
	"k8s.io/apimachinery/pkg/types"
	kclient "sigs.k8s.io/controller-runtime/pkg/client"
)

type ClientService struct {
	cpb.UnimplementedClientServiceServer
	kclient.Client
	auth.Auth
}

func NewClientService(client kclient.Client, auth auth.Auth) *ClientService {
	return &ClientService{
		Client: client,
		Auth:   auth,
	}
}

func (s *ClientService) GetExporter(
	ctx context.Context,
	req *cpb.GetExporterRequest,
) (*cpb.Exporter, error) {
	key, err := utils.ParseExporterIdentifier(req.Name)
	if err != nil {
		return nil, err
	}

	_, err = s.AuthClient(ctx, key.Namespace)
	if err != nil {
		return nil, err
	}

	var jexporter jumpstarterdevv1alpha1.Exporter
	if err := s.Get(ctx, *key, &jexporter); err != nil {
		return nil, err
	}

	return jexporter.ToProtobuf(), nil
}

func (s *ClientService) ListExporters(
	ctx context.Context,
	req *cpb.ListExportersRequest,
) (*cpb.ListExportersResponse, error) {
	namespace, err := utils.ParseNamespaceIdentifier(req.Parent)
	if err != nil {
		return nil, err
	}

	_, err = s.AuthClient(ctx, namespace)
	if err != nil {
		return nil, err
	}

	selector, err := labels.Parse(req.Filter)
	if err != nil {
		return nil, err
	}

	var jexporters jumpstarterdevv1alpha1.ExporterList
	if err := s.List(ctx, &jexporters, &kclient.ListOptions{
		Namespace:     namespace,
		LabelSelector: selector,
		Limit:         int64(req.PageSize),
		Continue:      req.PageToken,
	}); err != nil {
		return nil, err
	}

	return jexporters.ToProtobuf(), nil
}

func (s *ClientService) GetLease(ctx context.Context, req *cpb.GetLeaseRequest) (*cpb.Lease, error) {
	key, err := utils.ParseLeaseIdentifier(req.Name)
	if err != nil {
		return nil, err
	}

	_, err = s.AuthClient(ctx, key.Namespace)
	if err != nil {
		return nil, err
	}

	var jlease jumpstarterdevv1alpha1.Lease
	if err := s.Get(ctx, *key, &jlease); err != nil {
		return nil, err
	}

	return jlease.ToProtobuf(), nil
}

func (s *ClientService) ListLeases(ctx context.Context, req *cpb.ListLeasesRequest) (*cpb.ListLeasesResponse, error) {
	namespace, err := utils.ParseNamespaceIdentifier(req.Parent)
	if err != nil {
		return nil, err
	}

	_, err = s.AuthClient(ctx, namespace)
	if err != nil {
		return nil, err
	}

	selector, err := labels.Parse(req.Filter)
	if err != nil {
		return nil, err
	}

	// Apply active-only filter by default (when only_active is nil or true)
	// We must combine this with the user's filter selector into a single
	// MatchingLabelsSelector, because multiple MatchingLabelsSelector options
	// would override each other instead of being ANDed together.
	if req.OnlyActive == nil || *req.OnlyActive {
		requirement, err := labels.NewRequirement(
			string(jumpstarterdevv1alpha1.LeaseLabelEnded),
			selection.DoesNotExist,
			[]string{},
		)
		if err != nil {
			return nil, err
		}
		selector = selector.Add(*requirement)
	}

	listOptions := []kclient.ListOption{
		kclient.InNamespace(namespace),
		kclient.MatchingLabelsSelector{Selector: selector},
		kclient.Limit(int64(req.PageSize)),
		kclient.Continue(req.PageToken),
	}

	var jleases jumpstarterdevv1alpha1.LeaseList
	if err := s.List(ctx, &jleases, listOptions...); err != nil {
		return nil, err
	}

	var results []*cpb.Lease
	for _, lease := range jleases.Items {
		results = append(results, lease.ToProtobuf())
	}

	return &cpb.ListLeasesResponse{
		Leases:        results,
		NextPageToken: jleases.Continue,
	}, nil
}

func (s *ClientService) CreateLease(ctx context.Context, req *cpb.CreateLeaseRequest) (*cpb.Lease, error) {
	namespace, err := utils.ParseNamespaceIdentifier(req.Parent)
	if err != nil {
		return nil, err
	}

	jclient, err := s.AuthClient(ctx, namespace)
	if err != nil {
		return nil, err
	}

	// Use provided lease_id if specified, otherwise generate a UUIDv7
	name := req.LeaseId
	if name == "" {
		id, err := uuid.NewV7()
		if err != nil {
			return nil, err
		}
		name = id.String()
	}

	jlease, err := jumpstarterdevv1alpha1.LeaseFromProtobuf(req.Lease, types.NamespacedName{
		Namespace: namespace,
		Name:      name,
	}, corev1.LocalObjectReference{
		Name: jclient.Name,
	})
	if err != nil {
		return nil, err
	}

	if err := s.Create(ctx, jlease); err != nil {
		return nil, err
	}

	return jlease.ToProtobuf(), nil
}

func (s *ClientService) UpdateLease(ctx context.Context, req *cpb.UpdateLeaseRequest) (*cpb.Lease, error) {
	key, err := utils.ParseLeaseIdentifier(req.Lease.Name)
	if err != nil {
		return nil, err
	}

	jclient, err := s.AuthClient(ctx, key.Namespace)
	if err != nil {
		return nil, err
	}

	var jlease jumpstarterdevv1alpha1.Lease
	if err := s.Get(ctx, *key, &jlease); err != nil {
		return nil, err
	}

	if jlease.Spec.ClientRef.Name != jclient.Name {
		return nil, fmt.Errorf("UpdateLease permission denied")
	}

	original := kclient.MergeFrom(jlease.DeepCopy())

	// Only parse time fields from protobuf if any are being updated
	if req.Lease.BeginTime != nil || req.Lease.Duration != nil || req.Lease.EndTime != nil {
		desired, err := jumpstarterdevv1alpha1.LeaseFromProtobuf(req.Lease, *key,
			corev1.LocalObjectReference{
				Name: jclient.Name,
			},
		)
		if err != nil {
			return nil, err
		}

		// BeginTime can only be updated before lease starts; only if explicitly provided
		if req.Lease.BeginTime != nil {
			if jlease.Status.ExporterRef != nil {
				if jlease.Spec.BeginTime == nil || !jlease.Spec.BeginTime.Equal(desired.Spec.BeginTime) {
					return nil, fmt.Errorf("cannot update BeginTime: lease has already started")
				}
			}
			jlease.Spec.BeginTime = desired.Spec.BeginTime
		}
		// Update Duration only if provided; preserve existing otherwise
		if req.Lease.Duration != nil {
			jlease.Spec.Duration = desired.Spec.Duration
		}
		// Update EndTime only if provided; preserve existing otherwise
		if req.Lease.EndTime != nil {
			jlease.Spec.EndTime = desired.Spec.EndTime
		}
	}

	// Transfer lease to a new client if specified
	if req.Lease.Client != nil && *req.Lease.Client != "" {
		// Only active leases can be transferred (has exporter, not ended)
		if jlease.Status.ExporterRef == nil {
			return nil, fmt.Errorf("cannot transfer lease: lease has not started yet")
		}
		if jlease.Status.Ended {
			return nil, fmt.Errorf("cannot transfer lease: lease has already ended")
		}
		newClientKey, err := utils.ParseClientIdentifier(*req.Lease.Client)
		if err != nil {
			return nil, err
		}
		if newClientKey.Namespace != key.Namespace {
			return nil, fmt.Errorf("cannot transfer lease to client in different namespace")
		}
		var newClient jumpstarterdevv1alpha1.Client
		if err := s.Get(ctx, *newClientKey, &newClient); err != nil {
			return nil, fmt.Errorf("target client not found: %w", err)
		}
		jlease.Spec.ClientRef.Name = newClientKey.Name
	}

	// Recalculate missing field or validate consistency (only if time fields were updated)
	if req.Lease.BeginTime != nil || req.Lease.Duration != nil || req.Lease.EndTime != nil {
		if err := jumpstarterdevv1alpha1.ReconcileLeaseTimeFields(&jlease.Spec.BeginTime, &jlease.Spec.EndTime, &jlease.Spec.Duration); err != nil {
			return nil, err
		}
	}

	if err := s.Patch(ctx, &jlease, original); err != nil {
		return nil, err
	}

	return jlease.ToProtobuf(), nil
}

func (s *ClientService) DeleteLease(ctx context.Context, req *cpb.DeleteLeaseRequest) (*emptypb.Empty, error) {
	key, err := utils.ParseLeaseIdentifier(req.Name)
	if err != nil {
		return nil, err
	}

	jclient, err := s.AuthClient(ctx, key.Namespace)
	if err != nil {
		return nil, err
	}

	var jlease jumpstarterdevv1alpha1.Lease
	if err := s.Get(ctx, *key, &jlease); err != nil {
		return nil, err
	}

	if jlease.Spec.ClientRef.Name != jclient.Name {
		return nil, fmt.Errorf("DeleteLease permission denied")
	}

	original := kclient.MergeFrom(jlease.DeepCopy())

	jlease.Spec.Release = true

	if err := s.Patch(ctx, &jlease, original); err != nil {
		return nil, err
	}

	return &emptypb.Empty{}, nil
}
