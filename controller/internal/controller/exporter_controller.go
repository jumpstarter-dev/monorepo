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

package controller

import (
	"context"
	"fmt"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	jumpstarterdevv1alpha1 "github.com/jumpstarter-dev/jumpstarter-controller/api/v1alpha1"
	"github.com/jumpstarter-dev/jumpstarter-controller/internal/oidc"
)

// ExporterReconciler reconciles a Exporter object
type ExporterReconciler struct {
	client.Client
	Scheme *runtime.Scheme
	Signer *oidc.Signer
}

// +kubebuilder:rbac:groups=jumpstarter.dev,resources=exporters,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=jumpstarter.dev,resources=exporters/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=jumpstarter.dev,resources=exporters/finalizers,verbs=update
// +kubebuilder:rbac:groups=jumpstarter.dev,resources=exporteraccesspolicies,verbs=get;list;watch
// +kubebuilder:rbac:groups=core,resources=secrets,verbs=get;list;watch;create;update;patch;delete

// Reconcile is part of the main kubernetes reconciliation loop which aims to
// move the current state of the cluster closer to the desired state.
// TODO(user): Modify the Reconcile function to compare the state specified by
// the Exporter object against the actual cluster state, and then
// perform operations to make the cluster state reflect the state specified by
// the user.
//
// For more details, check Reconcile and its Result here:
// - https://pkg.go.dev/sigs.k8s.io/controller-runtime@v0.18.2/pkg/reconcile
func (r *ExporterReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx)

	var exporter jumpstarterdevv1alpha1.Exporter
	if err := r.Get(ctx, req.NamespacedName, &exporter); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(
			fmt.Errorf("Reconcile: unable to get exporter: %w", err),
		)
	}

	original := client.MergeFrom(exporter.DeepCopy())

	if err := r.reconcileStatusCredential(ctx, &exporter); err != nil {
		return ctrl.Result{}, err
	}

	if err := r.reconcileStatusLeaseRef(ctx, &exporter); err != nil {
		return ctrl.Result{}, err
	}

	result, err := r.reconcileStatusConditionsOnline(ctx, &exporter)
	if err != nil {
		return ctrl.Result{}, err
	}

	if err := r.reconcileStatusEndpoint(ctx, &exporter); err != nil {
		return ctrl.Result{}, err
	}

	if err := r.Status().Patch(ctx, &exporter, original); err != nil {
		return RequeueConflict(logger, ctrl.Result{}, err)
	}

	return result, nil
}

func (r *ExporterReconciler) reconcileStatusCredential(
	ctx context.Context,
	exporter *jumpstarterdevv1alpha1.Exporter,
) error {
	secret, err := ensureSecret(ctx, client.ObjectKey{
		Name:      exporter.Name + "-exporter",
		Namespace: exporter.Namespace,
	}, r.Client, r.Scheme, r.Signer, exporter.InternalSubject(), exporter)
	if err != nil {
		return fmt.Errorf("reconcileStatusCredential: failed to prepare credential for exporter: %w", err)
	}
	exporter.Status.Credential = &corev1.LocalObjectReference{
		Name: secret.Name,
	}
	return nil
}

func (r *ExporterReconciler) reconcileStatusLeaseRef(
	ctx context.Context,
	exporter *jumpstarterdevv1alpha1.Exporter,
) error {
	var leases jumpstarterdevv1alpha1.LeaseList
	if err := r.List(
		ctx,
		&leases,
		client.InNamespace(exporter.Namespace),
		MatchingActiveLeases(),
	); err != nil {
		return fmt.Errorf("reconcileStatusLeaseRef: failed to list active leases: %w", err)
	}

	exporter.Status.LeaseRef = nil
	for _, lease := range leases.Items {
		if !lease.Status.Ended && lease.Status.ExporterRef != nil {
			if lease.Status.ExporterRef.Name == exporter.Name {
				exporter.Status.LeaseRef = &corev1.LocalObjectReference{
					Name: lease.Name,
				}
			}
		}
	}

	return nil
}

// nolint:unparam
func (r *ExporterReconciler) reconcileStatusEndpoint(
	ctx context.Context,
	exporter *jumpstarterdevv1alpha1.Exporter,
) error {
	logger := log.FromContext(ctx)

	endpoint := controllerEndpoint()
	if exporter.Status.Endpoint != endpoint {
		logger.Info("reconcileStatusEndpoint: updating controller endpoint")
		exporter.Status.Endpoint = endpoint
	}

	return nil
}

// nolint:unparam
func (r *ExporterReconciler) reconcileStatusConditionsOnline(
	_ context.Context,
	exporter *jumpstarterdevv1alpha1.Exporter,
) (ctrl.Result, error) {
	var requeueAfter time.Duration = 0

	if exporter.Status.LastSeen.IsZero() {
		meta.SetStatusCondition(&exporter.Status.Conditions, metav1.Condition{
			Type:               string(jumpstarterdevv1alpha1.ExporterConditionTypeOnline),
			Status:             metav1.ConditionFalse,
			ObservedGeneration: exporter.Generation,
			Reason:             "Seen",
			Message:            "Never seen",
		})
		// Reset status to OFFLINE when never seen
		exporter.Status.ExporterStatusValue = jumpstarterdevv1alpha1.ExporterStatusOffline
		exporter.Status.StatusMessage = "Never seen"
		// marking the exporter offline, no need to requeue
	} else if time.Since(exporter.Status.LastSeen.Time) > time.Minute {
		meta.SetStatusCondition(&exporter.Status.Conditions, metav1.Condition{
			Type:               string(jumpstarterdevv1alpha1.ExporterConditionTypeOnline),
			Status:             metav1.ConditionFalse,
			ObservedGeneration: exporter.Generation,
			Reason:             "Seen",
			Message:            "Last seen more than 1 minute ago",
		})
		// Reset status to OFFLINE when exporter hasn't been seen recently
		if exporter.Status.ExporterStatusValue != jumpstarterdevv1alpha1.ExporterStatusOffline {
			exporter.Status.ExporterStatusValue = jumpstarterdevv1alpha1.ExporterStatusOffline
			exporter.Status.StatusMessage = "Connection lost - last seen more than 1 minute ago"
		}
		// marking the exporter offline, no need to requeue
	} else {
		// Check if exporter explicitly reported Offline status even though LastSeen is recent
		// This happens when an exporter gracefully shuts down
		if exporter.Status.ExporterStatusValue == jumpstarterdevv1alpha1.ExporterStatusOffline {
			meta.SetStatusCondition(&exporter.Status.Conditions, metav1.Condition{
				Type:               string(jumpstarterdevv1alpha1.ExporterConditionTypeOnline),
				Status:             metav1.ConditionFalse,
				ObservedGeneration: exporter.Generation,
				Reason:             "Offline",
				Message:            exporter.Status.StatusMessage,
			})
			// exporter reported offline, no need to requeue
		} else {
			meta.SetStatusCondition(&exporter.Status.Conditions, metav1.Condition{
				Type:               string(jumpstarterdevv1alpha1.ExporterConditionTypeOnline),
				Status:             metav1.ConditionTrue,
				ObservedGeneration: exporter.Generation,
				Reason:             "Seen",
				Message:            "Last seen less than 1 minute ago",
			})
			// marking the exporter online, requeue after 30 seconds
			requeueAfter = time.Second * 30
		}
	}

	if exporter.Status.Devices == nil {
		meta.SetStatusCondition(&exporter.Status.Conditions, metav1.Condition{
			Type:               string(jumpstarterdevv1alpha1.ExporterConditionTypeRegistered),
			Status:             metav1.ConditionFalse,
			ObservedGeneration: exporter.Generation,
			Reason:             "Unregister",
		})
	} else {
		meta.SetStatusCondition(&exporter.Status.Conditions, metav1.Condition{
			Type:               string(jumpstarterdevv1alpha1.ExporterConditionTypeRegistered),
			Status:             metav1.ConditionTrue,
			ObservedGeneration: exporter.Generation,
			Reason:             "Register",
		})
	}

	return ctrl.Result{
		RequeueAfter: requeueAfter,
	}, nil
}

// SetupWithManager sets up the controller with the Manager.
func (r *ExporterReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&jumpstarterdevv1alpha1.Exporter{}).
		Owns(&jumpstarterdevv1alpha1.Lease{}).
		Owns(&corev1.Secret{}).
		Complete(r)
}
