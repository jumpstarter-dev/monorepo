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
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/tools/record"
	ctrl "sigs.k8s.io/controller-runtime"
	kclient "sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	jumpstarterdevv1alpha1 "github.com/jumpstarter-dev/jumpstarter/controller/api/v1alpha1"
	"github.com/jumpstarter-dev/jumpstarter/controller/internal/oidc"
)

// ClientReconciler reconciles a Client object
type ClientReconciler struct {
	kclient.Client
	Scheme   *runtime.Scheme
	Signer   *oidc.Signer
	Recorder record.EventRecorder
}

// +kubebuilder:rbac:groups=jumpstarter.dev,resources=clients,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=jumpstarter.dev,resources=clients/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=jumpstarter.dev,resources=clients/finalizers,verbs=update
// +kubebuilder:rbac:groups="",resources=events,verbs=create;patch

// For more details, check Reconcile and its Result here:
// - https://pkg.go.dev/sigs.k8s.io/controller-runtime@v0.18.2/pkg/reconcile
func (r *ClientReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx)

	var client jumpstarterdevv1alpha1.Client
	if err := r.Get(ctx, req.NamespacedName, &client); err != nil {
		return ctrl.Result{}, kclient.IgnoreNotFound(
			fmt.Errorf("Reconcile: failed to get client: %w", err),
		)
	}

	original := kclient.MergeFrom(client.DeepCopy())
	prevCredential := client.Status.Credential
	prevTokenExpiring := meta.IsStatusConditionTrue(client.Status.Conditions, string(jumpstarterdevv1alpha1.ClientConditionTypeTokenExpiring))

	tokenExpiry, err := r.reconcileStatusCredential(ctx, &client)
	if err != nil {
		return ctrl.Result{}, err
	}

	if err := r.reconcileStatusEndpoint(ctx, &client); err != nil {
		return ctrl.Result{}, err
	}

	reconcileTokenExpiry(
		&client.Status.Conditions,
		&client.Status.TokenExpiresAt,
		tokenExpiry,
		client.Generation,
		string(jumpstarterdevv1alpha1.ClientConditionTypeTokenExpiring),
	)

	if err := r.Status().Patch(ctx, &client, original); err != nil {
		return RequeueConflict(logger, ctrl.Result{}, err)
	}

	// Emit only after status patch succeeds.
	if prevCredential == nil && client.Status.Credential != nil {
		r.emitEventf(&client, corev1.EventTypeNormal, "CredentialCreated",
			"Credential secret created for client: secret=%s", client.Status.Credential.Name)
	}

	newTokenExpiring := meta.IsStatusConditionTrue(client.Status.Conditions, string(jumpstarterdevv1alpha1.ClientConditionTypeTokenExpiring))
	if !prevTokenExpiring && newTokenExpiring {
		r.emitEventf(&client, corev1.EventTypeWarning, "TokenExpiringSoon",
			"Token for client %s is expiring soon: expires=%s", client.Name, tokenExpiry.UTC().Format(time.RFC3339))
	}

	return ctrl.Result{RequeueAfter: tokenExpiryRequeueInterval}, nil
}

func (r *ClientReconciler) reconcileStatusCredential(
	ctx context.Context,
	client *jumpstarterdevv1alpha1.Client,
) (time.Time, error) {
	secret, expiry, err := ensureSecret(ctx, kclient.ObjectKey{
		Name:      client.Name + "-client",
		Namespace: client.Namespace,
	}, r.Client, r.Scheme, r.Signer, client.InternalSubject(), client)
	if err != nil {
		return time.Time{}, fmt.Errorf("reconcileStatusCredential: failed to prepare credential for client: %w", err)
	}
	client.Status.Credential = &corev1.LocalObjectReference{
		Name: secret.Name,
	}
	return expiry, nil
}

// nolint:unparam
func (r *ClientReconciler) reconcileStatusEndpoint(
	ctx context.Context,
	client *jumpstarterdevv1alpha1.Client,
) error {
	logger := log.FromContext(ctx)

	endpoint := controllerEndpoint()
	if client.Status.Endpoint != endpoint {
		logger.Info("reconcileStatusEndpoint: updating controller endpoint")
		client.Status.Endpoint = endpoint
	}

	return nil
}

func (r *ClientReconciler) emitEventf(client *jumpstarterdevv1alpha1.Client, eventType, reason, msgFmt string, args ...any) {
	if r.Recorder == nil {
		return
	}
	r.Recorder.Eventf(client, eventType, reason, msgFmt, args...)
}

// SetupWithManager sets up the controller with the Manager.
func (r *ClientReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&jumpstarterdevv1alpha1.Client{}).
		Owns(&corev1.Secret{}).
		Complete(r)
}
