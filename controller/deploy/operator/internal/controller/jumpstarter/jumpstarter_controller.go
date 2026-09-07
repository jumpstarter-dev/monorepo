/*
Copyright 2025.

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

package jumpstarter

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"hash"
	"net"
	"sort"
	"strings"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/util/intstr"
	apiserverv1beta1 "k8s.io/apiserver/pkg/apis/apiserver/v1beta1"
	"k8s.io/client-go/tools/record"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/yaml"

	operatorv1alpha1 "github.com/jumpstarter-dev/jumpstarter/controller/deploy/operator/api/v1alpha1"
	"github.com/jumpstarter-dev/jumpstarter/controller/deploy/operator/internal/controller/jumpstarter/endpoints"
	loglevels "github.com/jumpstarter-dev/jumpstarter/controller/deploy/operator/internal/log"
	"github.com/jumpstarter-dev/jumpstarter/controller/internal/config"
)

const (
	// appProtocolH2C is the application protocol for HTTP/2 Cleartext
	appProtocolH2C = "h2c"
)

// JumpstarterReconciler reconciles a Jumpstarter object
type JumpstarterReconciler struct {
	client.Client
	Scheme             *runtime.Scheme
	EndpointReconciler *endpoints.Reconciler
	Recorder           record.EventRecorder
	Version            string
}

// resolveImage replaces the :latest tag with the operator's own version tag.
// If the operator version is unknown ("dev") or the image uses a non-latest tag
// (admin override), the image is returned unchanged.
func (r *JumpstarterReconciler) resolveImage(image string) string {
	if r.Version == "" || r.Version == "dev" {
		return image
	}
	version := strings.TrimPrefix(r.Version, "v")
	if base, ok := strings.CutSuffix(image, ":latest"); ok {
		return base + ":" + version
	}
	return image
}

// +kubebuilder:rbac:groups=operator.jumpstarter.dev,resources=jumpstarters,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=operator.jumpstarter.dev,resources=jumpstarters/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=operator.jumpstarter.dev,resources=jumpstarters/finalizers,verbs=update

// Core Kubernetes resources
// +kubebuilder:rbac:groups=apps,resources=deployments,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=apps,resources=deployments/status,verbs=get;update;patch
// +kubebuilder:rbac:groups="",resources=services,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=services/status,verbs=get;update;patch
// +kubebuilder:rbac:groups="",resources=configmaps,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=secrets,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=serviceaccounts,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=events,verbs=create;patch
// +kubebuilder:rbac:groups="",resources=pods,verbs=get;list;watch;create;update;patch;delete

// RBAC resources
// +kubebuilder:rbac:groups=rbac.authorization.k8s.io,resources=roles,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=rbac.authorization.k8s.io,resources=rolebindings,verbs=get;list;watch;create;update;patch;delete

// Leader election
// +kubebuilder:rbac:groups=coordination.k8s.io,resources=leases,verbs=get;list;watch;create;update;patch;delete

// Networking resources
// +kubebuilder:rbac:groups=networking.k8s.io,resources=ingresses,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=networking.k8s.io,resources=ingresses/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=route.openshift.io,resources=routes,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=route.openshift.io,resources=routes/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=route.openshift.io,resources=routes/custom-host,verbs=get;create;update;patch

// OpenShift cluster config (for baseDomain auto-detection)
// +kubebuilder:rbac:groups=config.openshift.io,resources=ingresses,verbs=get;list;watch

// Monitoring resources
// +kubebuilder:rbac:groups=monitoring.coreos.com,resources=servicemonitors,verbs=get;list;watch;create;update;patch;delete

// cert-manager resources (for TLS certificate management)
// +kubebuilder:rbac:groups=cert-manager.io,resources=issuers,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=cert-manager.io,resources=issuers/status,verbs=get
// +kubebuilder:rbac:groups=cert-manager.io,resources=clusterissuers,verbs=get;list;watch
// +kubebuilder:rbac:groups=cert-manager.io,resources=clusterissuers/status,verbs=get
// +kubebuilder:rbac:groups=cert-manager.io,resources=certificates,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=cert-manager.io,resources=certificates/status,verbs=get

// Jumpstarter CRD resources (needed to grant permissions to managed controllers)
// +kubebuilder:rbac:groups=jumpstarter.dev,resources=clients,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=jumpstarter.dev,resources=clients/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=jumpstarter.dev,resources=clients/finalizers,verbs=update
// +kubebuilder:rbac:groups=jumpstarter.dev,resources=exporters,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=jumpstarter.dev,resources=exporters/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=jumpstarter.dev,resources=exporters/finalizers,verbs=update
// +kubebuilder:rbac:groups=jumpstarter.dev,resources=leases,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=jumpstarter.dev,resources=leases/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=jumpstarter.dev,resources=leases/finalizers,verbs=update
// +kubebuilder:rbac:groups=jumpstarter.dev,resources=exporteraccesspolicies,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=jumpstarter.dev,resources=exporteraccesspolicies/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=jumpstarter.dev,resources=exporteraccesspolicies/finalizers,verbs=update

// virtualtarget.jumpstarter.dev CRD resources (needed to grant permissions to managed
// exporter-set provisioner controllers, see exporterSetPolicyRules)
// +kubebuilder:rbac:groups=virtualtarget.jumpstarter.dev,resources=exportersets,verbs=get;list;watch
// +kubebuilder:rbac:groups=virtualtarget.jumpstarter.dev,resources=exportersets/status;exportersets/scale,verbs=get;update;patch
// +kubebuilder:rbac:groups=virtualtarget.jumpstarter.dev,resources=exportersets/finalizers,verbs=update
// +kubebuilder:rbac:groups=virtualtarget.jumpstarter.dev,resources=virtualtargetclasses,verbs=get;list;watch

// For more details, check Reconcile and its Result here:
// - https://pkg.go.dev/sigs.k8s.io/controller-runtime@v0.21.0/pkg/reconcile
func (r *JumpstarterReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	// Fetch the Jumpstarter instance
	var jumpstarter operatorv1alpha1.Jumpstarter
	if err := r.Get(ctx, req.NamespacedName, &jumpstarter); err != nil {
		if errors.IsNotFound(err) {
			// Request object not found, could have been deleted after reconcile request.
			// Owned objects are automatically garbage collected. For additional cleanup logic use finalizers.
			log.Info("Jumpstarter resource not found. Ignoring since object must be deleted.")
			return ctrl.Result{}, nil
		}
		// Error reading the object - requeue the request.
		log.Error(err, "Failed to get Jumpstarter")
		return ctrl.Result{}, err
	}

	// Check if the instance is marked to be deleted
	if jumpstarter.GetDeletionTimestamp() != nil {
		// Handle finalizer logic here if needed
		return ctrl.Result{}, nil
	}

	// Apply runtime-computed defaults (endpoints based on baseDomain and cluster capabilities)
	// Static defaults are handled by kubebuilder annotations in the CRD schema
	r.EndpointReconciler.ApplyDefaults(&jumpstarter.Spec, jumpstarter.Namespace)

	// Clamp controller replicas to 1: the controller uses in-memory state for
	// gRPC stream coordination (Dial/Listen pairing), so only one replica can
	// serve traffic correctly. Multiple replicas would cause connection failures
	// when Dial and Listen land on different pods.
	if jumpstarter.Spec.Controller.Replicas > 1 {
		log.Info("WARNING: controller.replicas > 1 is not yet supported — the controller "+
			"uses in-memory state for gRPC stream coordination. Clamping to 1.",
			"requested", jumpstarter.Spec.Controller.Replicas)
		r.emitEventf(&jumpstarter, corev1.EventTypeWarning, "ReplicasClamped",
			"controller.replicas=%d is not yet supported (in-memory gRPC state requires a single replica), clamping to 1",
			jumpstarter.Spec.Controller.Replicas)
		jumpstarter.Spec.Controller.Replicas = 1
	}

	// Reconcile RBAC resources first
	if err := r.reconcileRBAC(ctx, &jumpstarter); err != nil {
		log.Error(err, "Failed to reconcile RBAC")
		return ctrl.Result{}, err
	}

	// Reconcile cert-manager resources (Issuers, Certificates, CA ConfigMap) before deployments
	// This ensures the CA ConfigMap exists before the controller deployment starts,
	// so the CA_BUNDLE_PEM environment variable is properly populated
	if err := r.reconcileCertificates(ctx, &jumpstarter); err != nil {
		log.Error(err, "Failed to reconcile Certificates")
		return ctrl.Result{}, err
	}

	// Build the desired controller ConfigMap once and compute its hash up front.
	// The hash is embedded in the controller pod template annotation so that a config
	// change (e.g. OIDC CA rotation) triggers a rolling restart without waiting for the
	// next full reconcile cycle. Building here avoids calling buildConfig twice and
	// guarantees the hash and the content that is later written are identical.
	desiredConfigMap, err := r.createConfigMap(ctx, &jumpstarter)
	if err != nil {
		log.Error(err, "Failed to build controller ConfigMap")
		return ctrl.Result{}, err
	}
	configMapHash := configMapDataHash(desiredConfigMap)

	// Compute TLS secret hashes so that certificate renewals trigger rolling restarts.
	controllerTLSHash, err := r.getControllerTLSSecretHash(ctx, &jumpstarter)
	if err != nil {
		log.Error(err, "Failed to compute controller TLS secret hash")
		return ctrl.Result{}, err
	}
	// Reconcile Controller Deployment
	if err := r.reconcileControllerDeployment(ctx, &jumpstarter, configMapHash, controllerTLSHash); err != nil {
		log.Error(err, "Failed to reconcile Controller Deployment")
		return ctrl.Result{}, err
	}

	// Reconcile Router Deployment
	if err := r.reconcileRouterDeployment(ctx, &jumpstarter); err != nil {
		log.Error(err, "Failed to reconcile Router Deployment")
		return ctrl.Result{}, err
	}

	// Reconcile ExporterSet provisioner controller Deployments
	if err := r.reconcileExporterSetControllers(ctx, &jumpstarter); err != nil {
		log.Error(err, "Failed to reconcile ExporterSet controllers")
		return ctrl.Result{}, err
	}

	// Reconcile Telemetry Deployment (Service is reconciled below in the networking stage)
	if err := r.reconcileTelemetryDeploymentStage(ctx, &jumpstarter); err != nil {
		log.Error(err, "Failed to reconcile Telemetry deployment")
		return ctrl.Result{}, err
	}

	// Reconcile Services (controller, router, login endpoints, and telemetry ClusterIP)
	if err := r.reconcileServices(ctx, &jumpstarter); err != nil {
		log.Error(err, "Failed to reconcile Services")
		return ctrl.Result{}, err
	}

	// Reconcile Telemetry ClusterIP Service (part of the networking stage)
	if err := r.reconcileTelemetryServiceStage(ctx, &jumpstarter); err != nil {
		log.Error(err, "Failed to reconcile Telemetry service")
		return ctrl.Result{}, err
	}

	// Reconcile ConfigMaps (after deployments and services, before secrets)
	if err := r.reconcileConfigMaps(ctx, &jumpstarter, desiredConfigMap); err != nil {
		log.Error(err, "Failed to reconcile ConfigMaps")
		return ctrl.Result{}, err
	}

	// Reconcile Secrets
	if err := r.reconcileSecrets(ctx, &jumpstarter); err != nil {
		log.Error(err, "Failed to reconcile Secrets")
		return ctrl.Result{}, err
	}

	// Update status
	if err := r.updateStatus(ctx, &jumpstarter); err != nil {
		log.Error(err, "Failed to update status")
		return ctrl.Result{}, err
	}

	// Requeue periodically to pick up changes. Use a shorter interval while the
	// telemetry CA secret is not yet ready so the controller ConfigMap converges quickly.
	requeueAfter := 30 * time.Minute
	if r.telemetryCANeedsRequeue(ctx, &jumpstarter) {
		requeueAfter = telemetryCARequeueInterval
	}
	return ctrl.Result{RequeueAfter: requeueAfter}, nil
}

// emitEventf emits a Kubernetes event on the Jumpstarter object.
func (r *JumpstarterReconciler) emitEventf(js *operatorv1alpha1.Jumpstarter, eventType, reason, msgFmt string, args ...interface{}) {
	if r.Recorder == nil {
		return
	}
	r.Recorder.Eventf(js, eventType, reason, msgFmt, args...)
}

// getControllerTLSSecretHash resolves the controller TLS secret name and returns its data hash.
func (r *JumpstarterReconciler) getControllerTLSSecretHash(ctx context.Context, jumpstarter *operatorv1alpha1.Jumpstarter) (string, error) {
	var tlsSecretName string
	if jumpstarter.Spec.CertManager.Enabled {
		tlsSecretName = GetControllerCertSecretName(jumpstarter)
	} else if jumpstarter.Spec.Controller.GRPC.TLS.CertSecret != "" {
		tlsSecretName = jumpstarter.Spec.Controller.GRPC.TLS.CertSecret
	}
	return r.getTLSSecretHash(ctx, jumpstarter.Namespace, tlsSecretName)
}

// routerTLSSecretName returns the TLS secret name for a router replica.
// An empty string means no TLS secret is configured.
func routerTLSSecretName(jumpstarter *operatorv1alpha1.Jumpstarter, replicaIndex int32) string {
	if jumpstarter.Spec.CertManager.Enabled {
		return GetRouterCertSecretName(jumpstarter, replicaIndex)
	}
	return jumpstarter.Spec.Routers.GRPC.TLS.CertSecret
}

// getRouterTLSSecretHash resolves the router TLS secret name for a given replica and returns its data hash.
func (r *JumpstarterReconciler) getRouterTLSSecretHash(ctx context.Context, jumpstarter *operatorv1alpha1.Jumpstarter, replicaIndex int32) (string, error) {
	return r.getTLSSecretHash(ctx, jumpstarter.Namespace, routerTLSSecretName(jumpstarter, replicaIndex))
}

// reconcileControllerDeployment reconciles the controller deployment
func (r *JumpstarterReconciler) reconcileControllerDeployment(ctx context.Context, jumpstarter *operatorv1alpha1.Jumpstarter, configMapHash, tlsSecretHash string) error {
	log := logf.FromContext(ctx)
	desiredDeployment := r.createControllerDeployment(jumpstarter, configMapHash, tlsSecretHash)

	existingDeployment := &appsv1.Deployment{}
	existingDeployment.Name = desiredDeployment.Name
	existingDeployment.Namespace = desiredDeployment.Namespace

	op, err := controllerutil.CreateOrUpdate(ctx, r.Client, existingDeployment, func() error {
		// Check if this is a new deployment or an existing one
		if existingDeployment.CreationTimestamp.IsZero() {
			// Deployment is being created, copy all fields from desired
			existingDeployment.Labels = desiredDeployment.Labels
			existingDeployment.Annotations = desiredDeployment.Annotations
			existingDeployment.Spec = desiredDeployment.Spec
			return controllerutil.SetControllerReference(jumpstarter, existingDeployment, r.Scheme)
		}

		desiredDeployment.Spec.Template.Spec.DeprecatedServiceAccount = existingDeployment.Spec.Template.Spec.DeprecatedServiceAccount
		desiredDeployment.Spec.Template.Spec.SchedulerName = existingDeployment.Spec.Template.Spec.SchedulerName

		// Check if deployment needs update using compare function
		if !deploymentNeedsUpdate(existingDeployment, desiredDeployment) {
			log.V(1).Info("Controller deployment specs are equal, skipping update",
				"name", existingDeployment.Name,
				"namespace", existingDeployment.Namespace)
			return nil
		}

		// Deployment exists, generate and log diff
		diff, err := generateDiff(existingDeployment, desiredDeployment)
		if err != nil {
			log.V(1).Info("Failed to generate deployment diff", "error", err)
		} else if diff != "" {
			fmt.Printf("\n=== Controller deployment differences detected ===\n")
			fmt.Printf("Name: %s\n", existingDeployment.Name)
			fmt.Printf("Namespace: %s\n", existingDeployment.Namespace)
			fmt.Printf("\n%s\n", diff)
			fmt.Printf("========================================\n\n")
		}

		// Apply changes
		existingDeployment.Labels = desiredDeployment.Labels
		existingDeployment.Annotations = desiredDeployment.Annotations
		existingDeployment.Spec.Replicas = desiredDeployment.Spec.Replicas
		existingDeployment.Spec.Selector = desiredDeployment.Spec.Selector
		existingDeployment.Spec.Template = desiredDeployment.Spec.Template
		return controllerutil.SetControllerReference(jumpstarter, existingDeployment, r.Scheme)
	})

	if err != nil {
		log.Error(err, "Failed to reconcile controller deployment",
			"name", desiredDeployment.Name,
			"namespace", desiredDeployment.Namespace)
		return err
	}

	log.Info("Controller deployment reconciled",
		"name", existingDeployment.Name,
		"namespace", existingDeployment.Namespace,
		"operation", op)

	switch op {
	case controllerutil.OperationResultCreated:
		r.emitEventf(jumpstarter, corev1.EventTypeNormal, "ControllerDeploymentCreated",
			"Controller deployment created: name=%s namespace=%s",
			existingDeployment.Name, existingDeployment.Namespace)
	case controllerutil.OperationResultUpdated:
		r.emitEventf(jumpstarter, corev1.EventTypeNormal, "ControllerDeploymentUpdated",
			"Controller deployment updated: name=%s namespace=%s",
			existingDeployment.Name, existingDeployment.Namespace)
	}

	return nil
}

// reconcileRouterDeployment reconciles router deployments (one per replica)
func (r *JumpstarterReconciler) reconcileRouterDeployment(ctx context.Context, jumpstarter *operatorv1alpha1.Jumpstarter) error {
	log := logf.FromContext(ctx)

	// Cache hashes by secret name so a shared CertSecret is fetched once across replicas.
	tlsHashBySecret := make(map[string]string)

	// Create one deployment per replica
	for i := int32(0); i < jumpstarter.Spec.Routers.Replicas; i++ {
		secretName := routerTLSSecretName(jumpstarter, i)
		routerTLSHash, ok := tlsHashBySecret[secretName]
		if !ok {
			var err error
			routerTLSHash, err = r.getTLSSecretHash(ctx, jumpstarter.Namespace, secretName)
			if err != nil {
				log.Error(err, "Failed to compute router TLS secret hash", "replica", i)
				return err
			}
			tlsHashBySecret[secretName] = routerTLSHash
		}
		desiredDeployment := r.createRouterDeployment(jumpstarter, i, routerTLSHash)

		existingDeployment := &appsv1.Deployment{}
		existingDeployment.Name = desiredDeployment.Name
		existingDeployment.Namespace = desiredDeployment.Namespace

		op, err := controllerutil.CreateOrUpdate(ctx, r.Client, existingDeployment, func() error {
			// Check if this is a new deployment or an existing one
			if existingDeployment.CreationTimestamp.IsZero() {
				// Deployment is being created, copy all fields from desired
				existingDeployment.Labels = desiredDeployment.Labels
				existingDeployment.Annotations = desiredDeployment.Annotations
				existingDeployment.Spec = desiredDeployment.Spec
				return controllerutil.SetControllerReference(jumpstarter, existingDeployment, r.Scheme)
			}
			desiredDeployment.Spec.Template.Spec.SchedulerName = existingDeployment.Spec.Template.Spec.SchedulerName
			desiredDeployment.Spec.Template.Spec.DeprecatedServiceAccount = existingDeployment.Spec.Template.Spec.DeprecatedServiceAccount

			if !deploymentNeedsUpdate(existingDeployment, desiredDeployment) {
				log.V(1).Info("Router deployment specs are equal, skipping update",
					"name", existingDeployment.Name,
					"namespace", existingDeployment.Namespace,
					"replica", i)
				return nil
			}
			// Deployment exists, generate and log diff
			diff, err := generateDiff(existingDeployment, desiredDeployment)
			if err != nil {
				log.V(1).Info("Failed to generate deployment diff", "error", err)
			} else if diff != "" {
				fmt.Printf("\n=== Router deployment differences detected ===\n")
				fmt.Printf("Name: %s\n", existingDeployment.Name)
				fmt.Printf("Namespace: %s\n", existingDeployment.Namespace)
				fmt.Printf("Replica: %d\n", i)
				fmt.Printf("\n%s\n", diff)
				fmt.Printf("==============================================\n\n")
			}

			// Apply changes
			existingDeployment.Labels = desiredDeployment.Labels
			existingDeployment.Annotations = desiredDeployment.Annotations
			existingDeployment.Spec.Replicas = desiredDeployment.Spec.Replicas
			existingDeployment.Spec.Selector = desiredDeployment.Spec.Selector
			existingDeployment.Spec.Template = desiredDeployment.Spec.Template
			return controllerutil.SetControllerReference(jumpstarter, existingDeployment, r.Scheme)
		})

		if err != nil {
			log.Error(err, "Failed to reconcile router deployment",
				"name", desiredDeployment.Name,
				"namespace", desiredDeployment.Namespace,
				"replica", i)
			return err
		}

		log.Info("Router deployment reconciled",
			"name", existingDeployment.Name,
			"namespace", existingDeployment.Namespace,
			"replica", i,
			"operation", op)

		switch op {
		case controllerutil.OperationResultCreated:
			r.emitEventf(jumpstarter, corev1.EventTypeNormal, "RouterDeploymentCreated",
				"Router deployment created: name=%s namespace=%s replica=%d",
				existingDeployment.Name, existingDeployment.Namespace, i)
		case controllerutil.OperationResultUpdated:
			r.emitEventf(jumpstarter, corev1.EventTypeNormal, "RouterDeploymentUpdated",
				"Router deployment updated: name=%s namespace=%s replica=%d",
				existingDeployment.Name, existingDeployment.Namespace, i)
		}
	}

	// Clean up deployments for scaled-down replicas
	if err := r.cleanupExcessRouterDeployments(ctx, jumpstarter); err != nil {
		log.Error(err, "Failed to cleanup excess router deployments")
		return err
	}

	return nil
}

// reconcileServices reconciles all services
func (r *JumpstarterReconciler) reconcileServices(ctx context.Context, jumpstarter *operatorv1alpha1.Jumpstarter) error {
	log := logf.FromContext(ctx)

	// Reconcile controller services
	for _, endpoint := range jumpstarter.Spec.Controller.GRPC.Endpoints {
		appProtocol := appProtocolH2C
		svcPort := corev1.ServicePort{
			Name:        "controller-grpc",
			Port:        8082,
			TargetPort:  intstr.FromInt(8082),
			Protocol:    corev1.ProtocolTCP,
			AppProtocol: &appProtocol,
		}
		// Set NodePort if configured
		if endpoint.NodePort != nil && endpoint.NodePort.Enabled && endpoint.NodePort.Port > 0 {
			svcPort.NodePort = endpoint.NodePort.Port
		}
		if err := r.EndpointReconciler.ReconcileControllerEndpoint(ctx, jumpstarter, &endpoint, svcPort); err != nil {
			return err
		}
	}

	// Reconcile router services - one per replica, all endpoints per replica
	for i := int32(0); i < jumpstarter.Spec.Routers.Replicas; i++ {
		if len(jumpstarter.Spec.Routers.GRPC.Endpoints) > 0 {
			// Each replica gets ALL configured endpoints with replica substitution
			for endpointIdx, baseEndpoint := range jumpstarter.Spec.Routers.GRPC.Endpoints {
				endpoint := r.buildEndpointForReplica(jumpstarter, i, endpointIdx, &baseEndpoint)

				// Build unique service name for this replica AND endpoint
				// This allows multiple service types (NodePort, LoadBalancer, etc.) per replica
				serviceName := r.buildServiceNameForReplicaEndpoint(jumpstarter, i, endpointIdx)

				appProtocol := appProtocolH2C
				svcPort := corev1.ServicePort{
					Name:        serviceName, // Unique name per replica+endpoint
					Port:        8083,
					TargetPort:  intstr.FromInt(8083),
					Protocol:    corev1.ProtocolTCP,
					AppProtocol: &appProtocol,
				}
				// Set NodePort if configured
				if endpoint.NodePort != nil && endpoint.NodePort.Enabled && endpoint.NodePort.Port > 0 {
					// increase nodeport numbers based in replica, not perfect because it needs to be
					// consecutive, but this is mostly for E2E testing.
					svcPort.NodePort = endpoint.NodePort.Port + i
				}
				if err := r.EndpointReconciler.ReconcileRouterReplicaEndpoint(ctx, jumpstarter, i, endpointIdx, &endpoint, svcPort); err != nil {
					return err
				}
			}
		} else {
			// No endpoints configured, create a default service without ingress/route
			endpoint := operatorv1alpha1.Endpoint{
				Address: fmt.Sprintf("router-%d.%s", i, jumpstarter.Spec.BaseDomain),
			}

			serviceName := fmt.Sprintf("%s-router-%d", jumpstarter.Name, i)
			appProtocol := appProtocolH2C
			svcPort := corev1.ServicePort{
				Name:        serviceName,
				Port:        8083,
				TargetPort:  intstr.FromInt(8083),
				Protocol:    corev1.ProtocolTCP,
				AppProtocol: &appProtocol,
			}
			if err := r.EndpointReconciler.ReconcileRouterReplicaEndpoint(ctx, jumpstarter, i, 0, &endpoint, svcPort); err != nil {
				return err
			}
		}
	}

	// Clean up services for scaled-down replicas
	if err := r.cleanupExcessRouterServices(ctx, jumpstarter); err != nil {
		log.Error(err, "Failed to cleanup excess router services")
		return err
	}

	// Reconcile login endpoints (if configured)
	for _, endpoint := range jumpstarter.Spec.Controller.Login.Endpoints {
		svcPort := corev1.ServicePort{
			Name:       "login",
			Port:       8086,
			TargetPort: intstr.FromInt(8086),
			Protocol:   corev1.ProtocolTCP,
		}
		if err := r.EndpointReconciler.ReconcileLoginEndpoint(ctx, jumpstarter, &endpoint, svcPort,
			jumpstarter.Spec.CertManager.Enabled, jumpstarter.Spec.Controller.Login.TLS); err != nil {
			return err
		}
	}

	return nil
}

// reconcileConfigMaps reconciles all configmaps.
// desiredConfigMap is the pre-built desired state, already resolved (including any
// JWT CA Secret/ConfigMap references). Callers must build it via createConfigMap before
// calling this function so that the config hash and the written content are identical.
func (r *JumpstarterReconciler) reconcileConfigMaps(ctx context.Context, jumpstarter *operatorv1alpha1.Jumpstarter, desiredConfigMap *corev1.ConfigMap) error {
	log := logf.FromContext(ctx)

	existingConfigMap := &corev1.ConfigMap{}
	existingConfigMap.Name = desiredConfigMap.Name
	existingConfigMap.Namespace = desiredConfigMap.Namespace

	op, err := controllerutil.CreateOrUpdate(ctx, r.Client, existingConfigMap, func() error {
		// Check if this is a new configmap or an existing one
		if existingConfigMap.CreationTimestamp.IsZero() {
			// ConfigMap is being created, copy all fields from desired
			existingConfigMap.Labels = desiredConfigMap.Labels
			existingConfigMap.Annotations = desiredConfigMap.Annotations
			existingConfigMap.Data = desiredConfigMap.Data
			existingConfigMap.BinaryData = desiredConfigMap.BinaryData
			return controllerutil.SetControllerReference(jumpstarter, existingConfigMap, r.Scheme)
		}

		// ConfigMap exists, check if update is needed
		if !configMapNeedsUpdate(existingConfigMap, desiredConfigMap) {
			log.V(1).Info("ConfigMap is up to date, skipping update",
				"name", existingConfigMap.Name,
				"namespace", existingConfigMap.Namespace)
			return nil
		}

		// Update needed - apply changes
		existingConfigMap.Labels = desiredConfigMap.Labels
		existingConfigMap.Annotations = desiredConfigMap.Annotations
		existingConfigMap.Data = desiredConfigMap.Data
		existingConfigMap.BinaryData = desiredConfigMap.BinaryData
		return controllerutil.SetControllerReference(jumpstarter, existingConfigMap, r.Scheme)
	})

	if err != nil {
		log.Error(err, "Failed to reconcile configmap",
			"name", desiredConfigMap.Name,
			"namespace", desiredConfigMap.Namespace)
		return err
	}

	log.Info("ConfigMap reconciled",
		"name", existingConfigMap.Name,
		"namespace", existingConfigMap.Namespace,
		"operation", op)

	return nil
}

// reconcileSecrets reconciles all secrets
// Secrets are only created if they don't exist. They are not updated or deleted
// to preserve secret keys across CR updates and deletions.
func (r *JumpstarterReconciler) reconcileSecrets(ctx context.Context, jumpstarter *operatorv1alpha1.Jumpstarter) error {
	log := logf.FromContext(ctx)

	// Create controller secret if it doesn't exist
	// Use fixed name for stable secret references across CR updates
	controllerSecretName := "jumpstarter-controller-secret"
	if err := r.ensureSecretExists(ctx, jumpstarter, controllerSecretName); err != nil {
		log.Error(err, "Failed to ensure controller secret exists", "secret", controllerSecretName)
		return err
	}

	// Create router secret if it doesn't exist
	// Use fixed name for stable secret references across CR updates
	routerSecretName := "jumpstarter-router-secret"
	if err := r.ensureSecretExists(ctx, jumpstarter, routerSecretName); err != nil {
		log.Error(err, "Failed to ensure router secret exists", "secret", routerSecretName)
		return err
	}

	return nil
}

// ensureSecretExists creates a secret only if it doesn't already exist
func (r *JumpstarterReconciler) ensureSecretExists(ctx context.Context, jumpstarter *operatorv1alpha1.Jumpstarter, name string) error {
	log := logf.FromContext(ctx)

	// Check if secret already exists
	existingSecret := &corev1.Secret{}
	err := r.Get(ctx, client.ObjectKey{
		Namespace: jumpstarter.Namespace,
		Name:      name,
	}, existingSecret)

	if err == nil {
		// Secret already exists, don't update it
		log.V(loglevels.LevelTrace).Info("Secret already exists, skipping creation", "secret", name)
		return nil
	}

	if !errors.IsNotFound(err) {
		// Some other error occurred
		return err
	}

	// Secret doesn't exist, create it with a random key
	randomKey, err := generateRandomKey(32)
	if err != nil {
		return fmt.Errorf("failed to generate random key: %w", err)
	}

	secret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: jumpstarter.Namespace,
			Labels: map[string]string{
				"app":                          jumpstarter.Name,
				"app.kubernetes.io/managed-by": "jumpstarter-operator",
			},
			Annotations: map[string]string{
				"jumpstarter.dev/orphan": "true",
			},
		},
		StringData: map[string]string{
			"key": randomKey,
		},
	}

	// Note: We intentionally do NOT set owner reference here so that
	// secrets are not deleted when the Jumpstarter CR is deleted.
	// This preserves the secret keys across CR deletions and recreations.

	if err := r.Create(ctx, secret); err != nil {
		// Handle race condition where secret was created between Get and Create
		if errors.IsAlreadyExists(err) {
			log.V(loglevels.LevelDebug).Info("Secret was created by another reconciliation", "secret", name)
			return nil
		}
		return fmt.Errorf("failed to create secret: %w", err)
	}

	log.Info("Created new secret with random key", "secret", name)
	r.emitEventf(jumpstarter, corev1.EventTypeNormal, "SecretCreated",
		"Secret created: name=%s namespace=%s", name, jumpstarter.Namespace)
	return nil
}

// generateRandomKey generates a cryptographically secure random key
func generateRandomKey(length int) (string, error) {
	bytes := make([]byte, length)
	if _, err := rand.Read(bytes); err != nil {
		return "", err
	}
	return base64.URLEncoding.EncodeToString(bytes), nil
}

// updateStatus is implemented in status.go

// writeHashField writes a length-prefixed field so adjacent key/value concatenations
// cannot collide (e.g. {"a":"xb","b":"y"} vs {"a":"x","b":"by"}).
func writeHashField(h hash.Hash, data []byte) {
	var lenBuf [8]byte
	binary.BigEndian.PutUint64(lenBuf[:], uint64(len(data)))
	_, _ = h.Write(lenBuf[:])
	_, _ = h.Write(data)
}

// configMapDataHash computes a deterministic SHA-256 hash over the Data keys and values
// of a ConfigMap. Used as a pod template annotation to trigger rolling restarts when
// the controller config changes (e.g. OIDC CA rotation).
func configMapDataHash(cm *corev1.ConfigMap) string {
	h := sha256.New()
	keys := make([]string, 0, len(cm.Data))
	for k := range cm.Data {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		writeHashField(h, []byte(k))
		writeHashField(h, []byte(cm.Data[k]))
	}
	return hex.EncodeToString(h.Sum(nil))
}

// secretDataHash computes a deterministic SHA-256 hash over the Data keys and values
// of a Secret. Used as a pod template annotation to trigger rolling restarts when
// TLS certificates are renewed.
func secretDataHash(secret *corev1.Secret) string {
	h := sha256.New()
	keys := make([]string, 0, len(secret.Data))
	for k := range secret.Data {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		writeHashField(h, []byte(k))
		writeHashField(h, secret.Data[k])
	}
	return hex.EncodeToString(h.Sum(nil))
}

// getTLSSecretHash fetches a TLS secret by name and returns its data hash.
// Returns an empty string if the secret does not exist yet.
func (r *JumpstarterReconciler) getTLSSecretHash(ctx context.Context, namespace, name string) (string, error) {
	if name == "" {
		return "", nil
	}
	var secret corev1.Secret
	if err := r.Get(ctx, client.ObjectKey{Namespace: namespace, Name: name}, &secret); err != nil {
		if errors.IsNotFound(err) {
			return "", nil
		}
		return "", fmt.Errorf("failed to get TLS secret %s: %w", name, err)
	}
	return secretDataHash(&secret), nil
}

// createControllerDeployment creates a deployment for the controller
func (r *JumpstarterReconciler) createControllerDeployment(jumpstarter *operatorv1alpha1.Jumpstarter, configMapHash, tlsSecretHash string) *appsv1.Deployment {
	labels := map[string]string{
		"component":  "controller",
		"app":        "jumpstarter-controller",
		"controller": jumpstarter.Name,
	}

	// Build GRPC endpoint from first controller endpoint
	// Default to port 443 for TLS gRPC endpoints
	grpcEndpoint := ""
	if len(jumpstarter.Spec.Controller.GRPC.Endpoints) > 0 {
		ep := jumpstarter.Spec.Controller.GRPC.Endpoints[0]
		if ep.Address != "" {
			grpcEndpoint = ensurePort(ep.Address, "443")
		} else {
			grpcEndpoint = fmt.Sprintf("grpc.%s:443", jumpstarter.Spec.BaseDomain)
		}
	}

	// Build Login endpoint from first login endpoint
	// Default to port 443 for HTTPS login endpoints
	loginEndpoint := ""
	if len(jumpstarter.Spec.Controller.Login.Endpoints) > 0 {
		ep := jumpstarter.Spec.Controller.Login.Endpoints[0]
		if ep.Address != "" {
			loginEndpoint = ep.Address
		} else {
			loginEndpoint = fmt.Sprintf("login.%s", jumpstarter.Spec.BaseDomain)
		}
	}

	// Base environment variables
	envVars := []corev1.EnvVar{
		{
			Name:  "GRPC_ENDPOINT",
			Value: grpcEndpoint,
		},
		{
			Name:  "LOGIN_ENDPOINT",
			Value: loginEndpoint,
		},
		{
			Name: "CONTROLLER_KEY",
			ValueFrom: &corev1.EnvVarSource{
				SecretKeyRef: &corev1.SecretKeySelector{
					LocalObjectReference: corev1.LocalObjectReference{
						Name: "jumpstarter-controller-secret",
					},
					Key: "key",
				},
			},
		},
		{
			Name: "ROUTER_KEY",
			ValueFrom: &corev1.EnvVarSource{
				SecretKeyRef: &corev1.SecretKeySelector{
					LocalObjectReference: corev1.LocalObjectReference{
						Name: "jumpstarter-router-secret",
					},
					Key: "key",
				},
			},
		},
		{
			Name: "NAMESPACE",
			ValueFrom: &corev1.EnvVarSource{
				FieldRef: &corev1.ObjectFieldSelector{
					FieldPath:  "metadata.namespace",
					APIVersion: "v1",
				},
			},
		},
		{
			Name:  "GIN_MODE",
			Value: "release",
		},
		// CA_BUNDLE_PEM for the login service to return to clients
		// Only optional if cert-manager is not enabled (when enabled, we know the CA ConfigMap exists)
		{
			Name: "CA_BUNDLE_PEM",
			ValueFrom: &corev1.EnvVarSource{
				ConfigMapKeyRef: &corev1.ConfigMapKeySelector{
					LocalObjectReference: corev1.LocalObjectReference{
						Name: GetCAConfigMapName(jumpstarter),
					},
					Key:      "ca.crt",
					Optional: boolPtr(!jumpstarter.Spec.CertManager.Enabled),
				},
			},
		},
	}

	var volumeMounts []corev1.VolumeMount
	var volumes []corev1.Volume

	// Add TLS certificate mount when cert-manager is enabled OR when manual cert secret is provided
	var tlsSecretName string
	if jumpstarter.Spec.CertManager.Enabled {
		tlsSecretName = GetControllerCertSecretName(jumpstarter)
	} else if jumpstarter.Spec.Controller.GRPC.TLS.CertSecret != "" {
		tlsSecretName = jumpstarter.Spec.Controller.GRPC.TLS.CertSecret
	}

	if tlsSecretName != "" {
		envVars = append(envVars,
			corev1.EnvVar{Name: "EXTERNAL_CERT_PEM", Value: "/tls/tls.crt"},
			corev1.EnvVar{Name: "EXTERNAL_KEY_PEM", Value: "/tls/tls.key"},
		)
		volumeMounts = append(volumeMounts, corev1.VolumeMount{
			Name:      "tls-certs",
			MountPath: "/tls",
			ReadOnly:  true,
		})
		// Set DefaultMode explicitly to avoid reconciliation loop (K8s defaults to 420/0644)
		defaultMode := int32(420)
		volumes = append(volumes, corev1.Volume{
			Name: "tls-certs",
			VolumeSource: corev1.VolumeSource{
				Secret: &corev1.SecretVolumeSource{
					SecretName:  tlsSecretName,
					DefaultMode: &defaultMode,
				},
			},
		})
	}

	return &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{
			Name:      fmt.Sprintf("%s-controller", jumpstarter.Name),
			Namespace: jumpstarter.Namespace,
			Labels:    labels,
		},
		Spec: appsv1.DeploymentSpec{
			Replicas:                &jumpstarter.Spec.Controller.Replicas,
			ProgressDeadlineSeconds: ptr.To(int32(600)),
			RevisionHistoryLimit:    ptr.To(int32(10)),
			Strategy: appsv1.DeploymentStrategy{
				Type: appsv1.RollingUpdateDeploymentStrategyType,
				RollingUpdate: &appsv1.RollingUpdateDeployment{
					MaxSurge:       &intstr.IntOrString{Type: intstr.String, StrVal: "25%"},
					MaxUnavailable: &intstr.IntOrString{Type: intstr.String, StrVal: "25%"},
				},
			},
			Selector: &metav1.LabelSelector{
				MatchLabels: labels,
			},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					Labels:      labels,
					Annotations: r.buildControllerPodAnnotations(jumpstarter, configMapHash, tlsSecretHash),
				},
				Spec: corev1.PodSpec{
					RestartPolicy:                 corev1.RestartPolicyAlways,
					DNSPolicy:                     corev1.DNSClusterFirst,
					TerminationGracePeriodSeconds: ptr.To(int64(30)),
					Containers: []corev1.Container{
						{
							Name:            "manager",
							Image:           r.resolveImage(jumpstarter.Spec.Controller.Image),
							ImagePullPolicy: jumpstarter.Spec.Controller.ImagePullPolicy,
							Args: []string{
								"--leader-elect",
								"--health-probe-bind-address=:8081",
								"-metrics-bind-address=:8080",
							},
							Env:          envVars,
							VolumeMounts: volumeMounts,
							Ports: []corev1.ContainerPort{
								{
									ContainerPort: 8082,
									Name:          "grpc",
									Protocol:      corev1.ProtocolTCP,
								},
								{
									ContainerPort: 8080,
									Name:          "metrics",
									Protocol:      corev1.ProtocolTCP,
								},
								{
									ContainerPort: 8081,
									Name:          "health",
									Protocol:      corev1.ProtocolTCP,
								},
								{
									ContainerPort: 8086,
									Name:          "login",
									Protocol:      corev1.ProtocolTCP,
								},
							},
							LivenessProbe: &corev1.Probe{
								ProbeHandler: corev1.ProbeHandler{
									HTTPGet: &corev1.HTTPGetAction{
										Path:   "/healthz",
										Port:   intstr.FromInt(8081),
										Scheme: corev1.URISchemeHTTP,
									},
								},
								InitialDelaySeconds: 15,
								PeriodSeconds:       20,
								TimeoutSeconds:      1,
								SuccessThreshold:    1,
								FailureThreshold:    3,
							},
							ReadinessProbe: &corev1.Probe{
								ProbeHandler: corev1.ProbeHandler{
									HTTPGet: &corev1.HTTPGetAction{
										Path:   "/readyz",
										Port:   intstr.FromInt(8081),
										Scheme: corev1.URISchemeHTTP,
									},
								},
								InitialDelaySeconds: 5,
								PeriodSeconds:       10,
								TimeoutSeconds:      1,
								SuccessThreshold:    1,
								FailureThreshold:    3,
							},
							Resources:                defaultControllerResources(jumpstarter.Spec.Controller.Resources),
							TerminationMessagePath:   "/dev/termination-log",
							TerminationMessagePolicy: corev1.TerminationMessageReadFile,
							SecurityContext: &corev1.SecurityContext{
								AllowPrivilegeEscalation: boolPtr(false),
								Capabilities: &corev1.Capabilities{
									Drop: []corev1.Capability{"ALL"},
								},
							},
						},
					},
					Volumes: volumes,
					SecurityContext: &corev1.PodSecurityContext{
						RunAsNonRoot: boolPtr(true),
						SeccompProfile: &corev1.SeccompProfile{
							Type: corev1.SeccompProfileTypeRuntimeDefault,
						},
					},
					ServiceAccountName: fmt.Sprintf("%s-controller-manager", jumpstarter.Name),
				},
			},
		},
	}
}

func boolPtr(b bool) *bool {
	return &b
}

// buildControllerPodAnnotations builds the pod template annotations for the controller deployment.
// Includes config/TLS hashes for rolling restart on changes, plus any user-provided pod annotations.
func (r *JumpstarterReconciler) buildControllerPodAnnotations(jumpstarter *operatorv1alpha1.Jumpstarter, configMapHash, tlsSecretHash string) map[string]string {
	annotations := make(map[string]string)
	for k, v := range jumpstarter.Spec.Controller.PodAnnotations {
		annotations[k] = v
	}
	annotations["jumpstarter.dev/configmap-sha256"] = configMapHash
	if tlsSecretHash != "" {
		annotations["jumpstarter.dev/tls-secret-sha256"] = tlsSecretHash
	}
	return annotations
}

// buildRouterPodAnnotations builds the pod template annotations for a router deployment.
// Includes TLS hash for rolling restart on cert renewal, plus any user-provided pod annotations.
func (r *JumpstarterReconciler) buildRouterPodAnnotations(jumpstarter *operatorv1alpha1.Jumpstarter, tlsSecretHash string) map[string]string {
	annotations := make(map[string]string)
	for k, v := range jumpstarter.Spec.Routers.PodAnnotations {
		annotations[k] = v
	}
	if tlsSecretHash != "" {
		annotations["jumpstarter.dev/tls-secret-sha256"] = tlsSecretHash
	}
	if len(annotations) == 0 {
		return nil
	}
	return annotations
}

// createRouterDeployment creates a deployment for a specific router replica
func (r *JumpstarterReconciler) createRouterDeployment(jumpstarter *operatorv1alpha1.Jumpstarter, replicaIndex int32, tlsSecretHash string) *appsv1.Deployment {
	// Base app label that ALL services for this replica will select
	// Individual services will be named with endpoint suffixes, but all select the same pods
	baseAppLabel := fmt.Sprintf("%s-router-%d", jumpstarter.Name, replicaIndex)

	labels := map[string]string{
		"component":    "router",
		"app":          baseAppLabel, // All services for this replica select by this label
		"router":       jumpstarter.Name,
		"router-index": fmt.Sprintf("%d", replicaIndex),
	}

	// Build router endpoint for this specific replica
	routerEndpoint := r.buildRouterEndpointForReplica(jumpstarter, replicaIndex)

	// Base environment variables
	envVars := []corev1.EnvVar{
		{
			Name:  "GRPC_ROUTER_ENDPOINT",
			Value: routerEndpoint,
		},
		{
			Name: "ROUTER_KEY",
			ValueFrom: &corev1.EnvVarSource{
				SecretKeyRef: &corev1.SecretKeySelector{
					LocalObjectReference: corev1.LocalObjectReference{
						Name: "jumpstarter-router-secret",
					},
					Key: "key",
				},
			},
		},
		{
			Name: "NAMESPACE",
			ValueFrom: &corev1.EnvVarSource{
				FieldRef: &corev1.ObjectFieldSelector{
					FieldPath:  "metadata.namespace",
					APIVersion: "v1",
				},
			},
		},
	}

	var volumeMounts []corev1.VolumeMount
	var volumes []corev1.Volume

	// Add TLS certificate mount when cert-manager is enabled OR when manual cert secret is provided.
	// Use routerTLSSecretName so the mounted secret matches the hash annotation.
	tlsSecretName := routerTLSSecretName(jumpstarter, replicaIndex)

	if tlsSecretName != "" {
		envVars = append(envVars,
			corev1.EnvVar{Name: "EXTERNAL_CERT_PEM", Value: "/tls/tls.crt"},
			corev1.EnvVar{Name: "EXTERNAL_KEY_PEM", Value: "/tls/tls.key"},
		)
		volumeMounts = append(volumeMounts, corev1.VolumeMount{
			Name:      "tls-certs",
			MountPath: "/tls",
			ReadOnly:  true,
		})
		// Set DefaultMode explicitly to avoid reconciliation loop (K8s defaults to 420/0644)
		defaultMode := int32(420)
		volumes = append(volumes, corev1.Volume{
			Name: "tls-certs",
			VolumeSource: corev1.VolumeSource{
				Secret: &corev1.SecretVolumeSource{
					SecretName:  tlsSecretName,
					DefaultMode: &defaultMode,
				},
			},
		})
	}

	return &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{
			Name:      fmt.Sprintf("%s-router-%d", jumpstarter.Name, replicaIndex),
			Namespace: jumpstarter.Namespace,
			Labels:    labels,
		},
		Spec: appsv1.DeploymentSpec{
			Replicas:                ptr.To(int32(1)), // Each deployment for the router needs to have exactly 1 replica
			ProgressDeadlineSeconds: ptr.To(int32(600)),
			RevisionHistoryLimit:    ptr.To(int32(10)),
			Strategy: appsv1.DeploymentStrategy{
				Type: appsv1.RollingUpdateDeploymentStrategyType,
				RollingUpdate: &appsv1.RollingUpdateDeployment{
					MaxSurge:       &intstr.IntOrString{Type: intstr.String, StrVal: "25%"},
					MaxUnavailable: &intstr.IntOrString{Type: intstr.String, StrVal: "25%"},
				},
			},
			Selector: &metav1.LabelSelector{
				MatchLabels: labels,
			},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					Labels:      labels,
					Annotations: r.buildRouterPodAnnotations(jumpstarter, tlsSecretHash),
				},
				Spec: corev1.PodSpec{
					RestartPolicy:                 corev1.RestartPolicyAlways,
					DNSPolicy:                     corev1.DNSClusterFirst,
					TerminationGracePeriodSeconds: ptr.To(int64(30)),
					Containers: []corev1.Container{
						{
							Name:            "router",
							Image:           r.resolveImage(jumpstarter.Spec.Routers.Image),
							ImagePullPolicy: jumpstarter.Spec.Routers.ImagePullPolicy,
							Command:         []string{"/router"},
							Args: []string{
								"-metrics-bind-address=:8080",
							},
							Env:          envVars,
							VolumeMounts: volumeMounts,
							Ports: []corev1.ContainerPort{
								{
									ContainerPort: 8083,
									Name:          "grpc",
									Protocol:      corev1.ProtocolTCP,
								},
								{
									ContainerPort: 8080,
									Name:          "metrics",
									Protocol:      corev1.ProtocolTCP,
								},
								{
									ContainerPort: 8081,
									Name:          "health",
									Protocol:      corev1.ProtocolTCP,
								},
							},
							Resources:                defaultRouterResources(jumpstarter.Spec.Routers.Resources),
							TerminationMessagePath:   "/dev/termination-log",
							TerminationMessagePolicy: corev1.TerminationMessageReadFile,
							SecurityContext: &corev1.SecurityContext{
								AllowPrivilegeEscalation: boolPtr(false),
								Capabilities: &corev1.Capabilities{
									Drop: []corev1.Capability{"ALL"},
								},
							},
						},
					},
					Volumes: volumes,
					SecurityContext: &corev1.PodSecurityContext{
						RunAsNonRoot: boolPtr(true),
						SeccompProfile: &corev1.SeccompProfile{
							Type: corev1.SeccompProfileTypeRuntimeDefault,
						},
					},
					ServiceAccountName:        fmt.Sprintf("%s-controller-manager", jumpstarter.Name),
					TopologySpreadConstraints: jumpstarter.Spec.Routers.TopologySpreadConstraints,
				},
			},
		},
	}
}

// createConfigMap creates a configmap for jumpstarter configuration
func (r *JumpstarterReconciler) createConfigMap(ctx context.Context, jumpstarter *operatorv1alpha1.Jumpstarter) (*corev1.ConfigMap, error) {
	// Build config struct from spec
	cfg, err := r.buildConfig(ctx, jumpstarter)
	if err != nil {
		return nil, fmt.Errorf("failed to build config: %w", err)
	}

	// Marshal to YAML
	configYAML, err := yaml.Marshal(cfg)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal config to YAML: %w", err)
	}

	// Build router configuration for all replicas
	router := r.buildRouter(jumpstarter)

	// Marshal router to YAML
	routerYAML, err := yaml.Marshal(router)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal router to YAML: %w", err)
	}

	return &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "jumpstarter-controller",
			Namespace: jumpstarter.Namespace,
			Labels: map[string]string{
				"app":           "jumpstarter-controller",
				"control-plane": "controller-manager",
			},
		},
		Data: map[string]string{
			"config": string(configYAML),
			"router": string(routerYAML),
		},
	}, nil
}

// buildConfig builds the controller configuration struct from the CR spec.
// It resolves any CA certificate references from Kubernetes Secrets or ConfigMaps
// and inlines the PEM content so the controller ConfigMap is self-contained.
func (r *JumpstarterReconciler) buildConfig(ctx context.Context, jumpstarter *operatorv1alpha1.Jumpstarter) (config.Config, error) {
	cfg := config.Config{
		Provisioning: config.Provisioning{
			Enabled: jumpstarter.Spec.Authentication.AutoProvisioning.Enabled,
		},
		Grpc: config.Grpc{
			Keepalive: config.Keepalive{
				MinTime:             "1s",
				PermitWithoutStream: true,
			},
		},
	}

	// Authentication configuration — resolve any Secret/ConfigMap CA references.
	resolvedJWT, err := r.resolveJWTAuthenticators(ctx, jumpstarter)
	if err != nil {
		return config.Config{}, err
	}

	auth := config.Authentication{
		JWT: resolvedJWT,
	}

	// Internal authentication
	if jumpstarter.Spec.Authentication.Internal.Enabled {
		prefix := jumpstarter.Spec.Authentication.Internal.Prefix
		if prefix == "" {
			prefix = "internal:"
		}
		auth.Internal.Prefix = prefix

		if jumpstarter.Spec.Authentication.Internal.TokenLifetime != nil {
			auth.Internal.TokenLifetime = jumpstarter.Spec.Authentication.Internal.TokenLifetime.Duration.String()
		}
	}

	// Kubernetes authentication
	if jumpstarter.Spec.Authentication.K8s.Enabled {
		auth.K8s.Enabled = true
	}

	// Ensure JWT is an empty array, not null
	if auth.JWT == nil {
		auth.JWT = []apiserverv1beta1.JWTAuthenticator{}
	}

	cfg.Authentication = auth

	// Lease policy configuration
	cfg.LeasePolicy = config.LeasePolicy{
		MaxTags: jumpstarter.Spec.LeasePolicy.MaxTags,
	}

	cfg.HiddenLabels = config.HiddenLabels{
		Keys: jumpstarter.Spec.HiddenLabels.Keys,
	}

	cfg.DeprecatedLabels = config.DeprecatedLabels{
		Keys: jumpstarter.Spec.DeprecatedLabels.Keys,
	}

	// Telemetry configuration.
	if jumpstarter.Spec.Telemetry != nil && jumpstarter.Spec.Telemetry.Enabled {
		t := jumpstarter.Spec.Telemetry
		telemetryCfg := &config.Telemetry{
			Enabled:  true,
			Endpoint: telemetryEndpointFor(jumpstarter.Namespace),
		}
		if t.Logging.Filter.MinSeverity != "" {
			telemetryCfg.Logging.Filter.MinSeverity = t.Logging.Filter.MinSeverity
		}
		// Include CA certificate when cert-manager is enabled so exporters can verify TLS
		if jumpstarter.Spec.CertManager.Enabled {
			caCert, err := r.resolveTelemetryCA(ctx, jumpstarter)
			if err != nil {
				// Log at default verbosity so operators notice during initial cert-manager setup.
				// Reconciliation continues without a certificate; telemetryCANeedsRequeue
				// triggers a short requeue until the CA secret is ready.
				logf.FromContext(ctx).Info("Could not resolve telemetry CA certificate; exporters cannot verify telemetry TLS until the CA is available",
					"error", err)
			} else if caCert != "" {
				telemetryCfg.Certificate = caCert
			}
		}
		cfg.Telemetry = telemetryCfg
	}

	// gRPC keepalive configuration
	if jumpstarter.Spec.Controller.GRPC.Keepalive != nil {
		ka := &cfg.Grpc.Keepalive

		if jumpstarter.Spec.Controller.GRPC.Keepalive.MinTime != nil {
			ka.MinTime = jumpstarter.Spec.Controller.GRPC.Keepalive.MinTime.Duration.String()
		}

		ka.PermitWithoutStream = jumpstarter.Spec.Controller.GRPC.Keepalive.PermitWithoutStream

		if jumpstarter.Spec.Controller.GRPC.Keepalive.Timeout != nil {
			ka.Timeout = jumpstarter.Spec.Controller.GRPC.Keepalive.Timeout.Duration.String()
		}

		if jumpstarter.Spec.Controller.GRPC.Keepalive.IntervalTime != nil {
			ka.IntervalTime = jumpstarter.Spec.Controller.GRPC.Keepalive.IntervalTime.Duration.String()
		}

		if jumpstarter.Spec.Controller.GRPC.Keepalive.MaxConnectionIdle != nil {
			ka.MaxConnectionIdle = jumpstarter.Spec.Controller.GRPC.Keepalive.MaxConnectionIdle.Duration.String()
		}

		if jumpstarter.Spec.Controller.GRPC.Keepalive.MaxConnectionAge != nil {
			ka.MaxConnectionAge = jumpstarter.Spec.Controller.GRPC.Keepalive.MaxConnectionAge.Duration.String()
		}

		if jumpstarter.Spec.Controller.GRPC.Keepalive.MaxConnectionAgeGrace != nil {
			ka.MaxConnectionAgeGrace = jumpstarter.Spec.Controller.GRPC.Keepalive.MaxConnectionAgeGrace.Duration.String()
		}
	}

	return cfg, nil
}

// resolveJWTAuthenticators converts the CRD-level JWTAuthenticatorConfig list into
// the standard apiserverv1beta1.JWTAuthenticator list consumed by the controller.
// For each entry that carries a certificateAuthoritySecret or certificateAuthorityConfigMap
// reference, the operator fetches the referenced resource and inlines the PEM content
// into Issuer.CertificateAuthority, overriding any value already set on the field.
func (r *JumpstarterReconciler) resolveJWTAuthenticators(
	ctx context.Context,
	jumpstarter *operatorv1alpha1.Jumpstarter,
) ([]apiserverv1beta1.JWTAuthenticator, error) {
	log := logf.FromContext(ctx)
	result := make([]apiserverv1beta1.JWTAuthenticator, 0, len(jumpstarter.Spec.Authentication.JWT))

	for i, jwtCfg := range jumpstarter.Spec.Authentication.JWT {
		authn := jwtCfg.JWTAuthenticator // copy the embedded struct

		// Secret reference takes precedence over ConfigMap reference.
		switch {
		case jwtCfg.CertificateAuthoritySecret != nil:
			ref := jwtCfg.CertificateAuthoritySecret
			key := ref.Key
			if key == "" {
				key = "tls.crt"
			}
			var secret corev1.Secret
			if err := r.Get(ctx, client.ObjectKey{Name: ref.Name, Namespace: jumpstarter.Namespace}, &secret); err != nil {
				return nil, fmt.Errorf("jwt[%d]: failed to fetch certificateAuthoritySecret %s/%s: %w", i, jumpstarter.Namespace, ref.Name, err)
			}
			pemBytes, ok := secret.Data[key]
			if !ok {
				return nil, fmt.Errorf("jwt[%d]: key %q not found in Secret %s/%s", i, key, jumpstarter.Namespace, ref.Name)
			}
			authn.Issuer.CertificateAuthority = string(pemBytes)
			log.V(1).Info("Resolved CA from Secret",
				"jwt_index", i, "secret", jumpstarter.Namespace+"/"+ref.Name, "key", key)

		case jwtCfg.CertificateAuthorityConfigMap != nil:
			ref := jwtCfg.CertificateAuthorityConfigMap
			key := ref.Key
			if key == "" {
				key = "ca.crt"
			}
			var cm corev1.ConfigMap
			if err := r.Get(ctx, client.ObjectKey{Name: ref.Name, Namespace: jumpstarter.Namespace}, &cm); err != nil {
				return nil, fmt.Errorf("jwt[%d]: failed to fetch certificateAuthorityConfigMap %s/%s: %w", i, jumpstarter.Namespace, ref.Name, err)
			}
			pemStr, ok := cm.Data[key]
			if !ok {
				return nil, fmt.Errorf("jwt[%d]: key %q not found in ConfigMap %s/%s", i, key, jumpstarter.Namespace, ref.Name)
			}
			authn.Issuer.CertificateAuthority = pemStr
			log.V(1).Info("Resolved CA from ConfigMap",
				"jwt_index", i, "configmap", jumpstarter.Namespace+"/"+ref.Name, "key", key)
		}

		result = append(result, authn)
	}

	return result, nil
}

// buildRouter builds the router configuration with entries for all replicas
func (r *JumpstarterReconciler) buildRouter(jumpstarter *operatorv1alpha1.Jumpstarter) config.Router {
	router := make(config.Router)

	// Create router entry for each replica
	for i := int32(0); i < jumpstarter.Spec.Routers.Replicas; i++ {
		// First replica is named "default" for backwards compatibility
		routerName := "default"
		if i > 0 {
			routerName = fmt.Sprintf("router-%d", i)
		}

		entry := config.RouterEntry{
			Endpoint: r.buildRouterEndpointForReplica(jumpstarter, i),
		}

		// Add labels if this is not the default router (replica 0)
		// Additional routers get labels to distinguish them
		if i > 0 {
			entry.Labels = map[string]string{
				"router-index": fmt.Sprintf("%d", i),
			}
		}

		router[routerName] = entry
	}

	return router
}

// buildRouterEndpointForReplica builds the GRPC_ROUTER_ENDPOINT for a specific replica
// This is the primary endpoint the router advertises itself as
func (r *JumpstarterReconciler) buildRouterEndpointForReplica(jumpstarter *operatorv1alpha1.Jumpstarter, replicaIndex int32) string {
	// If endpoints are specified, use the first one as the primary endpoint
	if len(jumpstarter.Spec.Routers.GRPC.Endpoints) > 0 {
		ep := jumpstarter.Spec.Routers.GRPC.Endpoints[0]
		address := ep.Address
		if address != "" {
			address = r.substituteReplica(address, replicaIndex)
			return ensurePort(address, "443")
		}
	}
	// Default pattern: router-N.baseDomain
	return fmt.Sprintf("router-%d.%s:443", replicaIndex, jumpstarter.Spec.BaseDomain)
}

// substituteReplica replaces $(replica) placeholder with actual replica index
func (r *JumpstarterReconciler) substituteReplica(address string, replicaIndex int32) string {
	return strings.ReplaceAll(address, "$(replica)", fmt.Sprintf("%d", replicaIndex))
}

// ensurePort adds a default port to an address if it doesn't already have one
// Handles IPv4, IPv6, and hostnames correctly using net.SplitHostPort
func ensurePort(address, defaultPort string) string {
	// Try to split the address into host and port
	_, _, err := net.SplitHostPort(address)
	if err == nil {
		// Address already has a port, return as-is
		return address
	}

	// No port found, need to add one
	// net.JoinHostPort handles IPv6 addresses correctly (adds brackets if needed)
	return net.JoinHostPort(address, defaultPort)
}

// buildServiceNameForReplicaEndpoint creates a unique service name for a router replica and endpoint
func (r *JumpstarterReconciler) buildServiceNameForReplicaEndpoint(jumpstarter *operatorv1alpha1.Jumpstarter, replicaIndex int32, endpointIdx int) string {
	if endpointIdx == 0 {
		// First endpoint uses base name for backwards compatibility
		return fmt.Sprintf("%s-router-%d", jumpstarter.Name, replicaIndex)
	}
	// Additional endpoints get a suffix
	return fmt.Sprintf("%s-router-%d-%d", jumpstarter.Name, replicaIndex, endpointIdx)
}

// buildEndpointForReplica creates an Endpoint struct for a specific router replica and endpoint
func (r *JumpstarterReconciler) buildEndpointForReplica(jumpstarter *operatorv1alpha1.Jumpstarter, replicaIndex int32, endpointIdx int, baseEndpoint *operatorv1alpha1.Endpoint) operatorv1alpha1.Endpoint {
	// Copy the base endpoint
	endpoint := *baseEndpoint

	// Set or substitute address
	if endpoint.Address != "" {
		endpoint.Address = r.substituteReplica(endpoint.Address, replicaIndex)
	} else {
		// Default address pattern when none specified
		if endpointIdx == 0 {
			endpoint.Address = fmt.Sprintf("router-%d.%s", replicaIndex, jumpstarter.Spec.BaseDomain)
		} else {
			endpoint.Address = fmt.Sprintf("router-%d-%d.%s", replicaIndex, endpointIdx, jumpstarter.Spec.BaseDomain)
		}
	}

	return endpoint
}

// cleanupExcessRouterDeployments deletes router deployments that exceed the current replica count
func (r *JumpstarterReconciler) cleanupExcessRouterDeployments(ctx context.Context, jumpstarter *operatorv1alpha1.Jumpstarter) error {
	log := logf.FromContext(ctx)

	// List all deployments with our router label
	deploymentList := &appsv1.DeploymentList{}
	listOpts := []client.ListOption{
		client.InNamespace(jumpstarter.Namespace),
		client.MatchingLabels{
			"router": jumpstarter.Name,
		},
	}

	if err := r.List(ctx, deploymentList, listOpts...); err != nil {
		return fmt.Errorf("failed to list router deployments: %w", err)
	}

	// Delete deployments with replica index >= current replica count
	for i := range deploymentList.Items {
		deployment := &deploymentList.Items[i]

		// Check if this deployment's name indicates it's beyond the current replica count
		// We need to check all indices from current replicas onwards
		for idx := jumpstarter.Spec.Routers.Replicas; idx < 100; idx++ { // reasonable upper bound
			excessName := fmt.Sprintf("%s-router-%d", jumpstarter.Name, idx)
			if deployment.Name == excessName {
				log.Info("Deleting excess router deployment", "deployment", deployment.Name, "replicaIndex", idx)
				if err := r.Delete(ctx, deployment); err != nil {
					if !errors.IsNotFound(err) {
						return fmt.Errorf("failed to delete excess deployment %s: %w", deployment.Name, err)
					}
				} else {
					r.emitEventf(jumpstarter, corev1.EventTypeNormal, "RouterDeploymentDeleted",
						"Excess router deployment deleted: name=%s replicaIndex=%d", deployment.Name, idx)
				}
				break
			}
		}
	}

	return nil
}

// cleanupExcessRouterServices deletes router services that exceed the current replica count
// or endpoint count. This ensures that when replicas or endpoints are scaled down, the
// corresponding services are removed.
func (r *JumpstarterReconciler) cleanupExcessRouterServices(ctx context.Context, jumpstarter *operatorv1alpha1.Jumpstarter) error {
	log := logf.FromContext(ctx)

	// Services can have suffixes for different service types
	// ClusterIP has no suffix, LoadBalancer has "-lb", NodePort has "-np"
	suffixes := []string{"", "-lb", "-np"}

	// 1. Delete services for excess replicas (replica index >= current replica count)
	for idx := jumpstarter.Spec.Routers.Replicas; idx < 100; idx++ { // reasonable upper bound
		foundAny := false

		// Try to delete services for all endpoints and service types for this replica
		for endpointIdx := 0; endpointIdx < 10; endpointIdx++ { // reasonable upper bound for endpoints
			for _, suffix := range suffixes {
				var serviceName string
				if endpointIdx == 0 {
					serviceName = fmt.Sprintf("%s-router-%d%s", jumpstarter.Name, idx, suffix)
				} else {
					serviceName = fmt.Sprintf("%s-router-%d-%d%s", jumpstarter.Name, idx, endpointIdx, suffix)
				}

				service := &corev1.Service{
					ObjectMeta: metav1.ObjectMeta{
						Name:      serviceName,
						Namespace: jumpstarter.Namespace,
					},
				}

				err := r.Delete(ctx, service)
				if err != nil {
					if !errors.IsNotFound(err) {
						return fmt.Errorf("failed to delete excess service %s: %w", serviceName, err)
					}
				} else {
					foundAny = true
					log.Info("Deleted excess router service", "service", serviceName, "replicaIndex", idx, "endpointIdx", endpointIdx)
				}
			}
		}

		// If we didn't find any services for this replica index, we've gone past all excess services
		if !foundAny {
			break
		}
	}

	// 2. Delete services for excess endpoints within valid replicas
	numEndpoints := len(jumpstarter.Spec.Routers.GRPC.Endpoints)
	if numEndpoints == 0 {
		numEndpoints = 1 // default endpoint
	}

	for replicaIdx := int32(0); replicaIdx < jumpstarter.Spec.Routers.Replicas; replicaIdx++ {
		for endpointIdx := numEndpoints; endpointIdx < 10; endpointIdx++ { // reasonable upper bound
			foundAny := false

			for _, suffix := range suffixes {
				var serviceName string
				if endpointIdx == 0 {
					serviceName = fmt.Sprintf("%s-router-%d%s", jumpstarter.Name, replicaIdx, suffix)
				} else {
					serviceName = fmt.Sprintf("%s-router-%d-%d%s", jumpstarter.Name, replicaIdx, endpointIdx, suffix)
				}

				service := &corev1.Service{
					ObjectMeta: metav1.ObjectMeta{
						Name:      serviceName,
						Namespace: jumpstarter.Namespace,
					},
				}

				err := r.Delete(ctx, service)
				if err != nil {
					if !errors.IsNotFound(err) {
						return fmt.Errorf("failed to delete excess endpoint service %s: %w", serviceName, err)
					}
				} else {
					foundAny = true
					log.Info("Deleted excess endpoint service", "service", serviceName, "replicaIndex", replicaIdx, "endpointIdx", endpointIdx)
				}
			}

			// If we didn't find any services for this endpoint index, we've gone past all excess endpoints
			if !foundAny {
				break
			}
		}
	}

	return nil
}

// defaultControllerResources returns the given resource requirements if they are
// non-empty, otherwise it returns sensible defaults for the controller pod.
func defaultControllerResources(spec corev1.ResourceRequirements) corev1.ResourceRequirements {
	if len(spec.Requests) == 0 && len(spec.Limits) == 0 && len(spec.Claims) == 0 {
		return corev1.ResourceRequirements{
			Requests: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("200m"),
				corev1.ResourceMemory: resource.MustParse("512Mi"),
			},
			Limits: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("1"),
				corev1.ResourceMemory: resource.MustParse("1Gi"),
			},
		}
	}
	return spec
}

// defaultRouterResources returns the given resource requirements if they are
// non-empty, otherwise it returns sensible defaults for a router pod.
func defaultRouterResources(spec corev1.ResourceRequirements) corev1.ResourceRequirements {
	if len(spec.Requests) == 0 && len(spec.Limits) == 0 && len(spec.Claims) == 0 {
		return corev1.ResourceRequirements{
			Requests: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("100m"),
				corev1.ResourceMemory: resource.MustParse("256Mi"),
			},
			Limits: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("1"),
				corev1.ResourceMemory: resource.MustParse("512Mi"),
			},
		}
	}
	return spec
}

// Index field names used to look up Jumpstarter CRs from referenced resources.
const (
	// indexReferencedSecret is the field index that maps each Jumpstarter CR to the
	// "namespace/name" keys of all Secrets it references (JWT CA certs + TLS certs).
	indexReferencedSecret = ".spec.referencedSecrets"
	// indexCAConfigMap is the field index that maps each Jumpstarter CR to the
	// "namespace/name" keys of ConfigMaps referenced as JWT CA certificates.
	indexCAConfigMap = ".spec.authentication.jwt.certificateAuthorityConfigMap"
)

// SetupWithManager sets up the controller with the Manager.
// In addition to watching owned resources, it watches Secrets (JWT CA certs and TLS certs)
// and ConfigMaps referenced as JWT CA certificates so that rotations trigger reconciliation.
func (r *JumpstarterReconciler) SetupWithManager(mgr ctrl.Manager) error {
	// Index Jumpstarter CRs by all Secrets they reference (JWT CA certs + TLS certs).
	if err := mgr.GetFieldIndexer().IndexField(
		context.Background(),
		&operatorv1alpha1.Jumpstarter{},
		indexReferencedSecret,
		func(obj client.Object) []string {
			jumpstarter := obj.(*operatorv1alpha1.Jumpstarter)
			var keys []string

			// JWT CA certificate secrets
			for _, jwtCfg := range jumpstarter.Spec.Authentication.JWT {
				if ref := jwtCfg.CertificateAuthoritySecret; ref != nil {
					keys = append(keys, jumpstarter.Namespace+"/"+ref.Name)
				}
			}

			// Controller TLS cert secret
			if jumpstarter.Spec.CertManager.Enabled {
				keys = append(keys, jumpstarter.Namespace+"/"+GetControllerCertSecretName(jumpstarter))
			} else if s := jumpstarter.Spec.Controller.GRPC.TLS.CertSecret; s != "" {
				keys = append(keys, jumpstarter.Namespace+"/"+s)
			}

			// Router TLS cert secrets
			if jumpstarter.Spec.CertManager.Enabled {
				for i := int32(0); i < jumpstarter.Spec.Routers.Replicas; i++ {
					keys = append(keys, jumpstarter.Namespace+"/"+GetRouterCertSecretName(jumpstarter, i))
				}
			} else if s := jumpstarter.Spec.Routers.GRPC.TLS.CertSecret; s != "" {
				keys = append(keys, jumpstarter.Namespace+"/"+s)
			}

			// Telemetry TLS cert secret
			if jumpstarter.Spec.Telemetry != nil && jumpstarter.Spec.Telemetry.Enabled {
				if jumpstarter.Spec.CertManager.Enabled {
					keys = append(keys, jumpstarter.Namespace+"/"+GetTelemetryCertSecretName(jumpstarter))
				} else if s := jumpstarter.Spec.Telemetry.GRPC.TLS.CertSecret; s != "" {
					keys = append(keys, jumpstarter.Namespace+"/"+s)
				}
			}

			return keys
		},
	); err != nil {
		return fmt.Errorf("failed to set up %s index: %w", indexReferencedSecret, err)
	}

	// Index Jumpstarter CRs by the ConfigMaps they reference as CA certificates.
	if err := mgr.GetFieldIndexer().IndexField(
		context.Background(),
		&operatorv1alpha1.Jumpstarter{},
		indexCAConfigMap,
		func(obj client.Object) []string {
			jumpstarter := obj.(*operatorv1alpha1.Jumpstarter)
			var keys []string
			for _, jwtCfg := range jumpstarter.Spec.Authentication.JWT {
				if ref := jwtCfg.CertificateAuthorityConfigMap; ref != nil {
					keys = append(keys, jumpstarter.Namespace+"/"+ref.Name)
				}
			}
			return keys
		},
	); err != nil {
		return fmt.Errorf("failed to set up %s index: %w", indexCAConfigMap, err)
	}

	// mapSecretToJumpstarters returns a reconcile request for every Jumpstarter
	// CR that references the changed Secret (JWT CA cert or TLS cert).
	mapSecretToJumpstarters := func(ctx context.Context, obj client.Object) []ctrl.Request {
		secret := obj.(*corev1.Secret)
		key := secret.Namespace + "/" + secret.Name

		var jumpstarterList operatorv1alpha1.JumpstarterList
		if err := mgr.GetClient().List(ctx, &jumpstarterList, client.MatchingFields{
			indexReferencedSecret: key,
		}); err != nil {
			logf.FromContext(ctx).Error(err, "Failed to list Jumpstarters for Secret ref", "secret", key)
			return nil
		}

		requests := make([]ctrl.Request, len(jumpstarterList.Items))
		for i, js := range jumpstarterList.Items {
			requests[i] = ctrl.Request{NamespacedName: client.ObjectKeyFromObject(&js)}
		}
		return requests
	}

	// mapConfigMapToJumpstarters returns a reconcile request for every Jumpstarter
	// CR that references the changed ConfigMap as a JWT CA certificate.
	mapConfigMapToJumpstarters := func(ctx context.Context, obj client.Object) []ctrl.Request {
		cm := obj.(*corev1.ConfigMap)
		key := cm.Namespace + "/" + cm.Name

		var jumpstarterList operatorv1alpha1.JumpstarterList
		if err := mgr.GetClient().List(ctx, &jumpstarterList, client.MatchingFields{
			indexCAConfigMap: key,
		}); err != nil {
			logf.FromContext(ctx).Error(err, "Failed to list Jumpstarters for ConfigMap CA ref", "configmap", key)
			return nil
		}

		requests := make([]ctrl.Request, len(jumpstarterList.Items))
		for i, js := range jumpstarterList.Items {
			requests[i] = ctrl.Request{NamespacedName: client.ObjectKeyFromObject(&js)}
		}
		return requests
	}

	return ctrl.NewControllerManagedBy(mgr).
		For(&operatorv1alpha1.Jumpstarter{}).
		Named("jumpstarter").
		Owns(&appsv1.Deployment{}).
		Owns(&corev1.Service{}).
		Owns(&corev1.ConfigMap{}).
		Owns(&rbacv1.Role{}).
		Owns(&rbacv1.RoleBinding{}).
		// Note: Secrets and ServiceAccounts are intentionally NOT owned to prevent deletion.
		// We watch Secrets referenced as JWT CA certificates and TLS certs so that
		// rotations trigger reconciliation and rolling restarts via hash annotations.
		Watches(&corev1.Secret{}, handler.EnqueueRequestsFromMapFunc(mapSecretToJumpstarters)).
		Watches(&corev1.ConfigMap{}, handler.EnqueueRequestsFromMapFunc(mapConfigMapToJumpstarters)).
		Complete(r)
}
