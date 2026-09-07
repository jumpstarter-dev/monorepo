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

package v1alpha1

import (
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	apiserverv1beta1 "k8s.io/apiserver/pkg/apis/apiserver/v1beta1"
)

// Condition type constants for JumpstarterStatus.Conditions
const (
	// ConditionTypeCertManagerAvailable indicates whether cert-manager CRDs are installed
	ConditionTypeCertManagerAvailable = "CertManagerAvailable"

	// ConditionTypeIssuerReady indicates whether the cert-manager Issuer is ready
	ConditionTypeIssuerReady = "IssuerReady"

	// ConditionTypeControllerCertificateReady indicates whether the controller TLS certificate is ready
	ConditionTypeControllerCertificateReady = "ControllerCertificateReady"

	// ConditionTypeRouterCertificatesReady indicates whether all router TLS certificates are ready
	ConditionTypeRouterCertificatesReady = "RouterCertificatesReady"

	// ConditionTypeControllerDeploymentReady indicates whether the controller deployment is available
	ConditionTypeControllerDeploymentReady = "ControllerDeploymentReady"

	// ConditionTypeRouterDeploymentsReady indicates whether all router deployments are available
	ConditionTypeRouterDeploymentsReady = "RouterDeploymentsReady"

	// ConditionTypeExporterSetControllersReady indicates whether all enabled ExporterSet
	// provisioner controller deployments are available
	ConditionTypeExporterSetControllersReady = "ExporterSetControllersReady"

	// ConditionTypeTelemetryDeploymentReady indicates whether the telemetry deployment is available
	ConditionTypeTelemetryDeploymentReady = "TelemetryDeploymentReady"

	// ConditionTypeReady indicates whether the overall Jumpstarter system is ready
	ConditionTypeReady = "Ready"
)

// yaml mockup of the JumpstarterSpec
// spec:
//   baseDomain: example.com
//   controller:
//     image: quay.io/jumpstarter/jumpstarter:0.7.2
//     imagePullPolicy: IfNotPresent
//     resources:
//       requests:
//         cpu: 100m
//         memory: 100Mi
//     replicas: 2
//     exporterOptions:
//       offlineTimeout: 180s
//     restApi:
//       tls:
//         certSecret: jumpstarter-rest-api-tls
//       endpoints:
//         - hostname: rest-api.example.com
//           route:
//             class: default
//     grpc:
//       tls:
//         certSecret: jumpstarter-tls
//       endpoints:
//         - hostname: grpc.example.com
//  	     route:
//    	     	enabled: true
//         - hostname: grpc2.example.com
//   	     ingress:
//    	     	enabled: true
//         		annotations:
//         		labels:
//         - hostname: this.one.is.optional.com
// 			 nodeport:
//         		enabled: true
//         		port: 9090
//         		annotations:
//         		labels:
//         - hostname: this.one.is.optional.too.com
// 			 loadBalancer:
//         		enabled: true
//         		port: 9090
//         		annotations:
//         		labels:
//       keepalive:
//         minTime: 1s
//         permitWithoutStream: true
//         timeout: 180s
//         intervalTime: 10s
//   routers:
//     image: quay.io/jumpstarter/jumpstarter:0.7.2
//     imagePullPolicy: IfNotPresent
//     resources:
//       requests:
//         cpu: 100m
//         memory: 100Mi
//     replicas: 3
//     topologySpreadConstraints:
//       - topologyKey: "kubernetes.io/hostname"
//         whenUnsatisfiable: ScheduleAnyway
//       - topologyKey: "kubernetes.io/zone"
//         whenUnsatisfiable: ScheduleAnyway
//     grpc:
//       tls:
//         certSecret: jumpstarter-router-tls
//       endpoints:
//         - hostname: router-$(replica).router.example.com
//           route:
//             enabled: true
//           ingress:
//             enabled: true
//             class: default
//           nodeport:
//             enabled: true
//             port: 9090
//           loadBalancer:
//             annotations:
//             labels:
//             enabled: true
//       keepalive:
//         minTime: 1s
//         permitWithoutStream: true
//         timeout: 180s
//         intervalTime: 10s
//   authentication:
//     internal:
//       prefix: "internal:"
//       enabled: true
//     k8s:
//       enabled: true
//     jwt:
//        - issuer:
//            url: https://auth.example.com/auth/realms/EmployeeIDP
//          audiences:
//            - account
//          claimMappings:
//            username:
//              claim: "preferred_username"
//              prefix: "corp:"
//   certManager:
//     enabled: true
//     server:
//       selfSigned:
//         enabled: true

// JumpstarterSpec defines the desired state of a Jumpstarter deployment. A deployment
// can be created in a namespace of the cluster, and that's where all the Jumpstarter
// resources and services will reside.
type JumpstarterSpec struct {
	// Base domain used to construct FQDNs for all service endpoints.
	// This domain will be used to generate the default hostnames for Routes, Ingresses, and certificates.
	// Example: "example.com" will generate endpoints like "grpc.example.com", "router.example.com"
	// +kubebuilder:validation:Pattern=^[a-z0-9]([a-z0-9\-\.]*[a-z0-9])?$
	BaseDomain string `json:"baseDomain,omitempty"`

	// CertManager configuration for automatic TLS certificate management.
	// When enabled, jumpstarter will interact with cert-manager to automatically provision
	// and renew TLS certificates for all endpoints. Requires cert-manager to be installed in the cluster.
	CertManager CertManagerConfig `json:"certManager,omitempty"`

	// Controller configuration for the main Jumpstarter API and gRPC services.
	// The controller handles gRPC and REST API requests from clients and exporters.
	// +kubebuilder:default={}
	Controller ControllerConfig `json:"controller,omitempty"`

	// Router configuration for the Jumpstarter router service.
	// Routers handle gRPC traffic routing and load balancing.
	// +kubebuilder:default={}
	Routers RoutersConfig `json:"routers,omitempty"`

	// Authentication configuration for client and exporter authentication.
	// Supports multiple authentication methods including internal tokens, Kubernetes tokens, and JWT.
	Authentication AuthenticationConfig `json:"authentication,omitempty"`

	// Lease policy configuration for controlling lease behavior.
	// +kubebuilder:default={}
	LeasePolicy LeasePolicyConfig `json:"leasePolicy,omitempty"`

	// Hidden labels configuration for hiding specific label keys from exporter listings.
	// +optional
	HiddenLabels HiddenLabelsConfig `json:"hiddenLabels,omitempty"`

	// ExporterSets configuration for virtual scalable exporter provisioner controllers.
	// When provisioners are listed and enabled, the operator creates a Deployment per
	// provisioner using the same exporter-set-controller image with a --provisioner flag.
	// +optional
	ExporterSets *ExporterSetsConfig `json:"exporterSets,omitempty"`

	// Deprecated labels configuration for warning users about label keys that should no longer be used.
	// +optional
	DeprecatedLabels DeprecatedLabelsConfig `json:"deprecatedLabels,omitempty"`

	// Telemetry configuration for the optional telemetry service.
	// When enabled, the operator deploys a jumpstarter-telemetry service that receives
	// structured log entries from exporters via gRPC. The controller advertises the
	// telemetry endpoint to exporters so they can push logs without cluster credentials.
	// +optional
	Telemetry *TelemetryConfig `json:"telemetry,omitempty"`
}

// HiddenLabelsConfig defines label keys to hide from exporter listings by default.
type HiddenLabelsConfig struct {
	// List of exact label keys to hide from ListExporters/GetExporter responses.
	// Clients can pass show_hidden_labels=true to see all labels.
	// +optional
	Keys []string `json:"keys,omitempty"`
}

// ExporterSetsConfig defines the configuration for ExporterSet provisioner controller deployments.
type ExporterSetsConfig struct {
	// Image for all provisioner controller Deployments.
	// +kubebuilder:default="quay.io/jumpstarter-dev/jumpstarter-exporterset-controller:latest"
	Image string `json:"image,omitempty"`

	// ImagePullPolicy for provisioner controller containers.
	// +kubebuilder:default="IfNotPresent"
	// +kubebuilder:validation:Enum=Always;IfNotPresent;Never
	ImagePullPolicy corev1.PullPolicy `json:"imagePullPolicy,omitempty"`

	// Resources for provisioner controller pods.
	Resources corev1.ResourceRequirements `json:"resources,omitempty"`

	// Provisioners is the list of provisioner controllers to deploy.
	Provisioners []ProvisionerConfig `json:"provisioners,omitempty"`
}

// ProvisionerConfig defines a single provisioner controller to deploy.
type ProvisionerConfig struct {
	// Name is the provisioner identifier (e.g. "qemu.jumpstarter.dev").
	// Must be a DNS-subdomain-safe value: lowercase alphanumeric characters, '-' or '.',
	// starting and ending with an alphanumeric character.
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=63
	// +kubebuilder:validation:Pattern=`^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$`
	Name string `json:"name"`

	// Enabled controls whether a Deployment is created for this provisioner.
	// +kubebuilder:default=true
	Enabled *bool `json:"enabled,omitempty"`

	// Image overrides the global exporterSets.image for this provisioner.
	// +optional
	Image string `json:"image,omitempty"`

	// Replicas for this provisioner controller Deployment.
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=1
	Replicas *int32 `json:"replicas,omitempty"`

	// Resources overrides the global exporterSets.resources for this provisioner.
	// +optional
	Resources *corev1.ResourceRequirements `json:"resources,omitempty"`
}

// DeprecatedLabelsConfig defines label keys that are deprecated and should trigger warnings.
type DeprecatedLabelsConfig struct {
	// Map of deprecated label keys to human-readable deprecation messages.
	// When an exporter has any of these labels, clients will receive a deprecation warning
	// with the corresponding message.
	// +optional
	Keys map[string]string `json:"keys,omitempty"`
}

// TelemetryConfig defines configuration for the telemetry service deployment.
// When enabled, the operator creates a Deployment and ClusterIP Service for
// jumpstarter-telemetry, which receives structured log entries from exporters.
type TelemetryConfig struct {
	// Enable the telemetry service deployment.
	// When enabled, the operator deploys a jumpstarter-telemetry pod and a ClusterIP
	// Service, and configures the controller to advertise the endpoint to exporters.
	// +kubebuilder:default=false
	Enabled bool `json:"enabled,omitempty"`

	// Container image for the telemetry pod in 'registry/repository/image:tag' format.
	// +kubebuilder:default="quay.io/jumpstarter-dev/jumpstarter-telemetry:latest"
	Image string `json:"image,omitempty"`

	// Image pull policy for the telemetry container.
	// +kubebuilder:default="IfNotPresent"
	// +kubebuilder:validation:Enum=Always;IfNotPresent;Never
	ImagePullPolicy corev1.PullPolicy `json:"imagePullPolicy,omitempty"`

	// Number of telemetry replicas to run.
	// Multiple replicas provide HA; each exporter connects to exactly one replica
	// via a persistent MetricsStream, so Prometheus sum-by queries across replicas
	// yield exact totals without double-counting (see JEP-0013 DD-8).
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=1
	Replicas *int32 `json:"replicas,omitempty"`

	// Resource requirements for the telemetry pod.
	Resources corev1.ResourceRequirements `json:"resources,omitempty"`

	// Logging configuration for the telemetry log ingestion path.
	Logging TelemetryLoggingConfig `json:"logging,omitempty"`

	// gRPC configuration for the telemetry service.
	// Use this to configure TLS when not using cert-manager.
	GRPC TelemetryGRPCConfig `json:"grpc,omitempty"`

	// Metrics configures reverse-scrape fan-out and Prometheus exposition
	// (JEP-0013). ServiceMonitor fields are a later phase.
	Metrics TelemetryMetricsConfig `json:"metrics,omitempty"`

	// Loki configures optional HTTP push of ingested logs to a Loki-compatible
	// endpoint. When url is empty, telemetry runs metrics-only.
	Loki TelemetryLokiConfig `json:"loki,omitempty"`

	// Backpressure configures the Loki log push ring buffer.
	Backpressure TelemetryBackpressureConfig `json:"backpressure,omitempty"`
}

// TelemetryLokiConfig configures Loki HTTP push from the telemetry service.
type TelemetryLokiConfig struct {
	// Loki push endpoint (http:// or https://). Optional — telemetry can run
	// metrics-only without Loki. grpc:// is reserved but not implemented yet.
	URL string `json:"url,omitempty"`

	// Secret with Loki credentials (username/password and/or token keys).
	// See JEP-0013 DD-5: only the telemetry pod holds Loki credentials.
	SecretRef string `json:"secretRef,omitempty"`

	// TLS settings for the Loki endpoint.
	TLS TelemetryLokiTLSConfig `json:"tls,omitempty"`
}

// TelemetryLokiTLSConfig configures TLS for the Loki push endpoint.
type TelemetryLokiTLSConfig struct {
	// Secret containing a CA bundle (ca.crt key) to trust for the Loki endpoint.
	CASecretRef string `json:"caSecretRef,omitempty"`

	// Disable TLS certificate verification (development/testing only).
	// +kubebuilder:default=false
	InsecureSkipVerify bool `json:"insecureSkipVerify,omitempty"`
}

// TelemetryBackpressureConfig configures the Loki log push ring buffer.
type TelemetryBackpressureConfig struct {
	// Ring buffer depth for Loki log push. On overflow, dropped entries are
	// replaced by a single drop-marker LogEntry.
	// +kubebuilder:default=10000
	// +kubebuilder:validation:Minimum=1
	QueueDepth int32 `json:"queueDepth,omitempty"`
}

// TelemetryGRPCConfig defines gRPC configuration for the telemetry service.
// This is a simplified version of GRPCConfig since telemetry is internal-only
// (ClusterIP) and doesn't need external endpoints or keepalive settings.
type TelemetryGRPCConfig struct {
	// TLS configuration for secure gRPC communication with the telemetry service.
	// When spec.certManager.enabled is true, this is ignored and certificates are
	// automatically managed by cert-manager.
	// When spec.certManager.enabled is false, you can provide your own TLS secret here.
	TLS TLSConfig `json:"tls,omitempty"`
}

// TelemetryMetricsConfig configures telemetry /metrics reverse-scrape behavior.
type TelemetryMetricsConfig struct {
	// Allowlist of keys to include in Prometheus exemplars. Unlisted keys are omitted.
	// +kubebuilder:default={"client","lease_id"}
	ExemplarKeys []string `json:"exemplarKeys,omitempty"`

	// Allowed driver_type label values. Unlisted types are remapped to "other".
	// +kubebuilder:default={"power","storage","network","serial","console","video","composite"}
	DriverTypeEnum []string `json:"driverTypeEnum,omitempty"`

	// Max wait for parallel exporter MetricsStream responses during a /metrics fan-out.
	// Should be lower than the Prometheus scrape_timeout.
	// +kubebuilder:default="7s"
	ScrapeTimeout *metav1.Duration `json:"scrapeTimeout,omitempty"`
}

// TelemetryLoggingConfig configures the log push path to the telemetry service.
type TelemetryLoggingConfig struct {
	// Filter controls which log entries are forwarded to the telemetry service.
	Filter TelemetryLoggingFilterConfig `json:"filter,omitempty"`
}

// TelemetryLoggingFilterConfig controls which log entries are forwarded to the telemetry service.
type TelemetryLoggingFilterConfig struct {
	// Minimum log severity to forward.
	// Accepted values: debug, info, warning, error, critical. Defaults to "info".
	// +kubebuilder:default="info"
	// +kubebuilder:validation:Enum=debug;info;warning;error;critical
	MinSeverity string `json:"minSeverity,omitempty"`
}

// LeasePolicyConfig defines policy constraints for leases.
type LeasePolicyConfig struct {
	// Maximum number of user-defined tags allowed per lease.
	// +kubebuilder:default=10
	// +kubebuilder:validation:Minimum=0
	MaxTags int32 `json:"maxTags,omitempty"`
}

// RoutersConfig defines the configuration for Jumpstarter router pods.
// Routers handle gRPC traffic routing and load balancing between clients and exporters.
type RoutersConfig struct {
	// Container image for the router pods in 'registry/repository/image:tag' format.
	// If not specified, defaults to the latest stable version of the Jumpstarter router.
	// +kubebuilder:default="quay.io/jumpstarter-dev/jumpstarter-controller:latest"
	Image string `json:"image,omitempty"`

	// Image pull policy for the router container.
	// Controls when the container image should be pulled from the registry.
	// +kubebuilder:default="IfNotPresent"
	// +kubebuilder:validation:Enum=Always;IfNotPresent;Never
	ImagePullPolicy corev1.PullPolicy `json:"imagePullPolicy,omitempty"`

	// Resource requirements for router pods.
	// Defines CPU and memory requests and limits for each router pod.
	Resources corev1.ResourceRequirements `json:"resources,omitempty"`

	// Number of router replicas to run.
	// Must be a positive integer. Minimum recommended value is 3 for high availability.
	// +kubebuilder:default=3
	// +kubebuilder:validation:Minimum=1
	Replicas int32 `json:"replicas,omitempty"`

	// Custom annotations to add to router pod templates.
	PodAnnotations map[string]string `json:"podAnnotations,omitempty"`

	// Topology spread constraints for router pod distribution.
	// Ensures router pods are distributed evenly across nodes and zones.
	// Useful for high availability and fault tolerance.
	TopologySpreadConstraints []corev1.TopologySpreadConstraint `json:"topologySpreadConstraints,omitempty"`

	// gRPC configuration for router endpoints.
	// Defines how router gRPC services are exposed and configured.
	GRPC GRPCConfig `json:"grpc,omitempty"`
}

// ControllerConfig defines the configuration for Jumpstarter controller pods.
// The controller is responsible for the gRPC and REST API services used by clients
// and exporters to interact with Jumpstarter.
type ControllerConfig struct {
	// Container image for the controller pods in 'registry/repository/image:tag' format.
	// If not specified, defaults to the latest stable version of the Jumpstarter controller.
	// +kubebuilder:default="quay.io/jumpstarter-dev/jumpstarter-controller:latest"
	Image string `json:"image,omitempty"`

	// Image pull policy for the controller container.
	// Controls when the container image should be pulled from the registry.
	// +kubebuilder:default="IfNotPresent"
	// +kubebuilder:validation:Enum=Always;IfNotPresent;Never
	ImagePullPolicy corev1.PullPolicy `json:"imagePullPolicy,omitempty"`

	// Resource requirements for controller pods.
	// Defines CPU and memory requests and limits for each controller pod.
	Resources corev1.ResourceRequirements `json:"resources,omitempty"`

	// Number of controller replicas to run.
	// Currently only 1 replica is supported because the controller uses in-memory
	// state for gRPC stream coordination (Dial/Listen). Values greater than 1 will
	// be clamped to 1 with a warning. See https://github.com/jumpstarter-dev/jumpstarter/issues/1013
	// for the tracking issue on HA controller support.
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=1
	Replicas int32 `json:"replicas,omitempty"`

	// Custom annotations to add to controller pod templates.
	PodAnnotations map[string]string `json:"podAnnotations,omitempty"`

	// Exporter options configuration.
	// Controls how exporters connect and behave when communicating with the controller.
	ExporterOptions ExporterOptions `json:"exporterOptions,omitempty"`

	// REST API configuration for HTTP-based clients.
	// Enables non-gRPC clients to interact with Jumpstarter for listing leases,
	// managing exporters, and creating new leases. Use this when you need HTTP/JSON access.
	RestAPI RestAPIConfig `json:"restApi,omitempty"`

	// gRPC configuration for controller endpoints.
	// Defines how controller gRPC services are exposed and configured.
	GRPC GRPCConfig `json:"grpc,omitempty"`

	// Login endpoint configuration for simplified CLI login.
	// Provides authentication configuration discovery for the jmp login command.
	// The login service runs on HTTP and expects TLS to be terminated at the Route/Ingress level.
	Login LoginConfig `json:"login,omitempty"`
}

// ExporterOptions defines configuration options for exporter behavior.
type ExporterOptions struct {
	// Offline timeout duration for exporters.
	// After this duration without communication, an exporter is considered offline.
	// This drives the online/offline status field of exporters, and offline exporters
	// won't be considered for leases.
	// +kubebuilder:default="180s"
	OfflineTimeout *metav1.Duration `json:"offlineTimeout,omitempty"`
}

// GRPCConfig defines gRPC service configuration.
// Configures how gRPC services are exposed and their connection behavior.
type GRPCConfig struct {
	// TLS configuration for secure gRPC communication.
	// Requires a Kubernetes secret containing the TLS certificate and private key.
	// If spec.certManager.enabled is true, this secret will be automatically managed and
	// configured by cert-manager.
	TLS TLSConfig `json:"tls,omitempty"`

	// List of gRPC endpoints to expose.
	// Each endpoint can use different networking methods (Route, Ingress, NodePort, or LoadBalancer)
	// based on your cluster setup. Example: Use Route for OpenShift, Ingress for standard Kubernetes.
	Endpoints []Endpoint `json:"endpoints,omitempty"`

	// Keepalive configuration for gRPC connections.
	// Controls connection health checks and idle connection management.
	// Helps maintain stable connections in load-balanced environments.
	Keepalive *GRPCKeepaliveConfig `json:"keepalive,omitempty"`
}

// GRPCKeepaliveConfig defines keepalive settings for gRPC connections.
// These settings help maintain stable connections in load-balanced environments
// and detect connection issues early.
type GRPCKeepaliveConfig struct {
	// Minimum time between keepalives that the connection will accept, under this threshold
	// the other side will get a GOAWAY signal.
	// Prevents excessive keepalive traffic on the network.
	// +kubebuilder:default="1s"
	MinTime *metav1.Duration `json:"minTime,omitempty"`

	// Allow keepalive pings even when there are no active RPC streams.
	// Useful for detecting connection issues in idle connections.
	// This is important to keep TCP gRPC connections alive when traversing
	// load balancers and proxies.
	// +kubebuilder:default=true
	PermitWithoutStream bool `json:"permitWithoutStream,omitempty"`

	// Timeout for keepalive ping acknowledgment.
	// If a ping is not acknowledged within this time, the connection is considered broken.
	// The default is high to avoid issues when the network on an exporter is overloaded, i.e.
	// during flashing.
	// +kubebuilder:default="180s"
	Timeout *metav1.Duration `json:"timeout,omitempty"`

	// Maximum time a connection can remain idle before being closed.
	// It defaults to infinity.
	MaxConnectionIdle *metav1.Duration `json:"maxConnectionIdle,omitempty"`

	// Maximum age of a connection before it is closed and recreated.
	// Helps prevent issues with long-lived connections. It defaults to infinity.
	MaxConnectionAge *metav1.Duration `json:"maxConnectionAge,omitempty"`

	// Grace period for closing connections that exceed MaxConnectionAge.
	// Allows ongoing RPCs to complete before closing the connection.
	MaxConnectionAgeGrace *metav1.Duration `json:"maxConnectionAgeGrace,omitempty"`

	// Interval between keepalive pings.
	// How often to send keepalive pings to check connection health. This is important
	// to keep TCP gRPC connections alive when traversing load balancers and proxies.
	// +kubebuilder:default="10s"
	IntervalTime *metav1.Duration `json:"intervalTime,omitempty"`
}

// AuthenticationConfig defines authentication methods for Jumpstarter.
// Supports multiple authentication methods that can be enabled simultaneously.
type AuthenticationConfig struct {
	// Internal authentication configuration.
	// Built-in authenticator that issues tokens for clients and exporters.
	// This is the simplest authentication method and is enabled by default.
	Internal InternalAuthConfig `json:"internal,omitempty"`

	// Kubernetes authentication configuration.
	// Enables authentication using Kubernetes service account tokens.
	// Useful for integrating with existing Kubernetes RBAC policies.
	K8s K8sAuthConfig `json:"k8s,omitempty"`

	// JWT authentication configuration.
	// Enables authentication using external JWT tokens from OIDC providers.
	// Supports multiple JWT authenticators for different identity providers.
	// Each entry may optionally reference a CA certificate from a Kubernetes
	// Secret or ConfigMap instead of inlining the PEM content.
	JWT []JWTAuthenticatorConfig `json:"jwt,omitempty"`

	// Automatic user provisioning configuration, this is useful for creating
	// users authenticated by external identity providers in Jumpstarter.
	AutoProvisioning AutoProvisioningConfig `json:"autoProvisioning,omitempty"`
}

// JWTAuthenticatorConfig extends the standard Kubernetes JWTAuthenticator with
// support for referencing CA certificates from Kubernetes Secrets or ConfigMaps.
// The operator resolves the reference at reconcile time and injects the PEM content
// into the controller ConfigMap, so CA rotations are picked up automatically.
type JWTAuthenticatorConfig struct {
	apiserverv1beta1.JWTAuthenticator `json:",inline"`

	// CertificateAuthoritySecret references a Kubernetes Secret containing the CA
	// certificate PEM for the OIDC issuer. The operator reads the specified key and
	// injects the PEM content as the certificateAuthority for this authenticator.
	// When the Secret changes, the operator reconciles and updates the ConfigMap.
	// Takes precedence over CertificateAuthorityConfigMap when both are set.
	// +optional
	CertificateAuthoritySecret *SecretKeySelector `json:"certificateAuthoritySecret,omitempty"`

	// CertificateAuthorityConfigMap references a Kubernetes ConfigMap containing the
	// CA certificate PEM for the OIDC issuer. The operator reads the specified key and
	// injects the PEM content as the certificateAuthority for this authenticator.
	// When the ConfigMap changes, the operator reconciles and updates the ConfigMap.
	// +optional
	CertificateAuthorityConfigMap *ConfigMapKeySelector `json:"certificateAuthorityConfigMap,omitempty"`
}

// SecretKeySelector references a key within a Kubernetes Secret.
type SecretKeySelector struct {
	// Name of the Secret containing the CA certificate.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`

	// Key within the Secret that holds the PEM-encoded CA certificate.
	// Defaults to "tls.crt", which is the standard key used by cert-manager.
	// +kubebuilder:default=tls.crt
	// +optional
	Key string `json:"key,omitempty"`
}

// ConfigMapKeySelector references a key within a Kubernetes ConfigMap.
type ConfigMapKeySelector struct {
	// Name of the ConfigMap containing the CA certificate.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`

	// Key within the ConfigMap that holds the PEM-encoded CA certificate.
	// Defaults to "ca.crt", which is the standard key used by kube-root-ca.crt
	// and cert-manager CA bundles.
	// +kubebuilder:default=ca.crt
	// +optional
	Key string `json:"key,omitempty"`
}

// AutoProvisioningConfig defines auto provisioning configuration.
type AutoProvisioningConfig struct {
	// Enable auto provisioning.
	// When disabled, users authenticated by external identity providers will
	// not be automatically created in Jumpstarter.
	// +kubebuilder:default=false
	Enabled bool `json:"enabled,omitempty"`
}

// InternalAuthConfig defines the built-in authentication configuration.
// The internal authenticator issues tokens for clients and exporters to authenticate
// with Jumpstarter. This is the simplest authentication method.
type InternalAuthConfig struct {
	// Prefix to add to the subject claim of issued tokens.
	// Helps distinguish internal tokens from other authentication methods.
	// Example: "internal:" will result in subjects like "internal:user123"
	// +kubebuilder:default="internal:"
	// +kubebuilder:validation:MaxLength=50
	Prefix string `json:"prefix,omitempty"`

	// Enable the internal authentication method.
	// When disabled, clients cannot use internal tokens for authentication.
	// +kubebuilder:default=true
	Enabled bool `json:"enabled,omitempty"`

	// Token validity duration for issued tokens.
	// After this duration, tokens expire and must be renewed.
	// +kubebuilder:default="43800h"
	TokenLifetime *metav1.Duration `json:"tokenLifetime,omitempty"`
}

// K8sAuthConfig defines Kubernetes service account authentication.
// Enables authentication using Kubernetes service account tokens.
type K8sAuthConfig struct {
	// Enable Kubernetes authentication.
	// When enabled, clients can authenticate using Kubernetes service account tokens.
	// +kubebuilder:default=false
	Enabled bool `json:"enabled,omitempty"`
}

// TLSConfig defines TLS configuration for secure communication.
type TLSConfig struct {
	// Name of the Kubernetes secret containing the TLS certificate and private key.
	// The secret must contain 'tls.crt' and 'tls.key' keys.
	// If spec.certManager.enabled is true, this secret will be automatically managed and
	// configured by cert-manager.
	// +kubebuilder:validation:Pattern=^[a-z0-9]([a-z0-9\-\.]*[a-z0-9])?$
	CertSecret string `json:"certSecret,omitempty"`
}

// RestAPIConfig defines REST API configuration for HTTP-based clients.
// Provides HTTP/JSON access to Jumpstarter functionality.
type RestAPIConfig struct {
	// TLS configuration for secure HTTP communication.
	// Requires a Kubernetes secret containing the TLS certificate and private key.
	TLS TLSConfig `json:"tls,omitempty"`

	// List of REST API endpoints to expose.
	// Each endpoint can use different networking methods (Route, Ingress, NodePort, or LoadBalancer)
	// based on your cluster setup.
	Endpoints []Endpoint `json:"endpoints,omitempty"`
}

// LoginConfig defines configuration for the login endpoint.
// The login service provides authentication configuration discovery for simplified CLI login.
// It runs on HTTP with TLS terminated at the Route/Ingress level (edge termination).
type LoginConfig struct {
	// TLS configuration for the login endpoint.
	// Specifies the Kubernetes secret containing the TLS certificate for edge termination.
	// If not specified and certManager is enabled, a default secret name will be generated.
	TLS *LoginTLSConfig `json:"tls,omitempty"`

	// List of login endpoints to expose.
	// Each endpoint can use different networking methods (Route, Ingress, NodePort, or LoadBalancer)
	// based on your cluster setup.
	// Note: Unlike gRPC endpoints, login endpoints use edge TLS termination (not passthrough).
	Endpoints []Endpoint `json:"endpoints,omitempty"`
}

// LoginTLSConfig defines TLS configuration for login endpoints.
// This is used for edge TLS termination at the Ingress/Route level.
type LoginTLSConfig struct {
	// Name of the Kubernetes secret containing the TLS certificate and private key.
	// The secret must contain 'tls.crt' and 'tls.key' keys.
	// Used for edge TLS termination at the Ingress/Route level.
	// +kubebuilder:validation:Pattern=^[a-z0-9]([a-z0-9\-\.]*[a-z0-9])?$
	SecretName string `json:"secretName,omitempty"`
}

// Endpoint defines a single endpoint configuration.
// An endpoint can use one or more networking methods: Route, Ingress, NodePort, or LoadBalancer.
// Multiple methods can be configured simultaneously for the same address.
type Endpoint struct {
	// Address for this endpoint in the format "hostname", "hostname:port", "IPv4", "IPv4:port", "[IPv6]", or "[IPv6]:port".
	// Required for Route and Ingress endpoints. Optional for NodePort and LoadBalancer endpoints.
	// When optional, the address is used for certificate generation and DNS resolution.
	// Supports templating with $(replica) for replica-specific addresses.
	// Examples: "grpc.example.com", "grpc.example.com:9090", "192.168.1.1:8080", "[2001:db8::1]:8443", "router-$(replica).example.com"
	// +kubebuilder:validation:Pattern=`^(\[[0-9a-fA-F:\.]+\]|[0-9]+(\.[0-9]+){3}|[a-z0-9$]([a-z0-9\-\.\$\(\)]*[a-z0-9\)])?)(:[0-9]+)?$`
	Address string `json:"address,omitempty"`

	// Route configuration for OpenShift clusters.
	// Creates an OpenShift Route resource for this endpoint.
	// Only applicable in OpenShift environments.
	Route *RouteConfig `json:"route,omitempty"`

	// Ingress configuration for standard Kubernetes clusters.
	// Creates an Ingress resource for this endpoint.
	// Requires an ingress controller to be installed.
	Ingress *IngressConfig `json:"ingress,omitempty"`

	// NodePort configuration for direct node access.
	// Exposes the service on a specific port on each node.
	// Useful for bare-metal or simple cluster setups.
	NodePort *NodePortConfig `json:"nodeport,omitempty"`

	// LoadBalancer configuration for cloud environments.
	// Creates a LoadBalancer service for this endpoint.
	// Requires cloud provider support for LoadBalancer services.
	LoadBalancer *LoadBalancerConfig `json:"loadBalancer,omitempty"`

	// ClusterIP configuration for internal service access.
	// Creates a ClusterIP service for this endpoint.
	// Useful for internal service-to-service communication or when
	// using a different method to expose the service externally.
	ClusterIP *ClusterIPConfig `json:"clusterIP,omitempty"`
}

// RouteConfig defines OpenShift Route configuration.
type RouteConfig struct {
	// Enable the OpenShift Route for this endpoint.
	// When disabled, no Route resource will be created for this endpoint.
	// When not specified, the operator will determine the best networking option for your cluster.
	Enabled bool `json:"enabled,omitempty"`

	// Annotations to add to the OpenShift Route resource.
	// Useful for configuring route-specific behavior and TLS settings.
	Annotations map[string]string `json:"annotations,omitempty"`

	// Labels to add to the OpenShift Route resource.
	// Useful for monitoring, cost allocation, and resource organization.
	Labels map[string]string `json:"labels,omitempty"`
}

// IngressConfig defines Kubernetes Ingress configuration.
type IngressConfig struct {
	// Enable the Kubernetes Ingress for this endpoint.
	// When disabled, no Ingress resource will be created for this endpoint.
	// When not specified, the operator will determine the best networking option for your cluster.
	Enabled bool `json:"enabled,omitempty"`

	// Ingress class name for the Kubernetes Ingress.
	// Specifies which ingress controller should handle this ingress.
	// +kubebuilder:default="default"
	Class string `json:"class,omitempty"`

	// Annotations to add to the Kubernetes Ingress resource.
	// Useful for configuring ingress-specific behavior, TLS settings, and load balancer options.
	Annotations map[string]string `json:"annotations,omitempty"`

	// Labels to add to the Kubernetes Ingress resource.
	// Useful for monitoring, cost allocation, and resource organization.
	Labels map[string]string `json:"labels,omitempty"`
}

// NodePortConfig defines Kubernetes NodePort service configuration.
type NodePortConfig struct {
	// Enable the NodePort service for this endpoint.
	// When disabled, no NodePort service will be created for this endpoint.
	// When not specified, the operator will determine the best networking option for your cluster.
	Enabled bool `json:"enabled,omitempty"`

	// NodePort port number to expose on each node.
	// Must be a valid port number (1-65535). Kubernetes typically allocates NodePorts in the range 30000-32767 by default.
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=65535
	Port int32 `json:"port,omitempty"`

	// Annotations to add to the NodePort service.
	// Useful for configuring service-specific behavior and load balancer options.
	Annotations map[string]string `json:"annotations,omitempty"`

	// Labels to add to the NodePort service.
	// Useful for monitoring, cost allocation, and resource organization.
	Labels map[string]string `json:"labels,omitempty"`
}

// LoadBalancerConfig defines Kubernetes LoadBalancer service configuration.
type LoadBalancerConfig struct {
	// Enable the LoadBalancer service for this endpoint.
	// When disabled, no LoadBalancer service will be created for this endpoint.
	// When not specified, the operator will determine the best networking option for your cluster.
	Enabled bool `json:"enabled,omitempty"`

	// Port number for the LoadBalancer service.
	// Must be a valid port number (1-65535).
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=65535
	Port int32 `json:"port,omitempty"`

	// Annotations to add to the LoadBalancer service.
	// Useful for configuring cloud provider-specific load balancer options.
	// Example: "service.beta.kubernetes.io/aws-load-balancer-type: nlb"
	Annotations map[string]string `json:"annotations,omitempty"`

	// Labels to add to the LoadBalancer service.
	// Useful for monitoring, cost allocation, and resource organization.
	Labels map[string]string `json:"labels,omitempty"`
}

// ClusterIPConfig defines Kubernetes ClusterIP service configuration.
type ClusterIPConfig struct {
	// Enable the ClusterIP service for this endpoint.
	// When disabled, no ClusterIP service will be created for this endpoint.
	Enabled bool `json:"enabled,omitempty"`

	// Annotations to add to the ClusterIP service.
	// Useful for configuring service-specific behavior and load balancer options.
	Annotations map[string]string `json:"annotations,omitempty"`

	// Labels to add to the ClusterIP service.
	// Useful for monitoring, cost allocation, and resource organization.
	Labels map[string]string `json:"labels,omitempty"`
}

// CertManagerConfig defines certificate management configuration using cert-manager.
// When enabled, the operator will create Certificate resources to automatically
// provision and renew TLS certificates for controller and router endpoints.
type CertManagerConfig struct {
	// Enable cert-manager integration for automatic TLS certificate management.
	// When disabled, TLS certificates must be provided manually via secrets.
	// +kubebuilder:default=false
	Enabled bool `json:"enabled,omitempty"`

	// Server certificate configuration for controller and router endpoints.
	// Defines how server TLS certificates are issued.
	Server *ServerCertConfig `json:"server,omitempty"`
}

// ServerCertConfig defines how server certificates are issued.
// Only one of SelfSigned or IssuerRef should be specified.
// If neither is specified and cert-manager is enabled, SelfSigned with defaults is used.
type ServerCertConfig struct {
	// Create a self-signed CA managed by the operator.
	// The operator will create a self-signed Issuer and CA certificate,
	// then use that CA to issue server certificates.
	SelfSigned *SelfSignedConfig `json:"selfSigned,omitempty"`

	// Reference an existing cert-manager Issuer or ClusterIssuer.
	// Use this to integrate with existing PKI infrastructure (ACME, Vault, etc.).
	// This overrides the default selfSigned.enabled=true setting.
	IssuerRef *IssuerReference `json:"issuerRef,omitempty"`
}

// SelfSignedConfig configures operator-managed self-signed CA for certificate issuance.
// When enabled, the operator creates:
// 1. A SelfSigned Issuer (bootstrap)
// 2. A CA Certificate signed by the self-signed issuer
// 3. A CA Issuer that uses the CA certificate's secret
// 4. Server Certificates signed by the CA Issuer
type SelfSignedConfig struct {
	// Enable self-signed CA mode.
	// +kubebuilder:default=true
	Enabled bool `json:"enabled,omitempty"`

	// Duration of the CA certificate validity.
	// The CA certificate is used to sign server certificates.
	// +kubebuilder:default="87600h"
	CADuration *metav1.Duration `json:"caDuration,omitempty"`

	// Duration of server certificate validity.
	// Server certificates are issued for controller and router endpoints.
	// +kubebuilder:default="8760h"
	CertDuration *metav1.Duration `json:"certDuration,omitempty"`

	// Time before certificate expiration to trigger renewal.
	// Certificates will be renewed this duration before they expire.
	// +kubebuilder:default="360h"
	RenewBefore *metav1.Duration `json:"renewBefore,omitempty"`
}

// IssuerReference references an existing cert-manager Issuer or ClusterIssuer.
// This allows integration with any cert-manager issuer type (CA, ACME, Vault, Venafi, etc.).
type IssuerReference struct {
	// Name of the Issuer or ClusterIssuer resource.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`

	// Kind of the issuer: "Issuer" for namespace-scoped or "ClusterIssuer" for cluster-scoped.
	// +kubebuilder:validation:Enum=Issuer;ClusterIssuer
	// +kubebuilder:default="Issuer"
	Kind string `json:"kind,omitempty"`

	// Group of the issuer resource. Defaults to cert-manager.io.
	// Only change this if using a custom issuer from a different API group.
	// +kubebuilder:default="cert-manager.io"
	Group string `json:"group,omitempty"`

	// CABundle is an optional base64-encoded PEM CA certificate bundle for this issuer.
	// Required when using external issuers with non-publicly-trusted CAs.
	// This will be published to the {name}-service-ca-cert ConfigMap for clients to use.
	// For self-signed CA mode, this is automatically calculated from the CA secret.
	// +optional
	CABundle []byte `json:"caBundle,omitempty"`
}

// JumpstarterStatus defines the observed state of Jumpstarter.
// Status information is reported through conditions following Kubernetes conventions.
type JumpstarterStatus struct {
	// Conditions represent the latest available observations of the Jumpstarter state.
	// Condition types include:
	// - CertManagerAvailable: cert-manager CRDs are installed in the cluster
	// - IssuerReady: The referenced or created issuer is ready to issue certificates
	// - ControllerCertificateReady: Controller TLS certificate is issued and secret exists
	// - RouterCertificatesReady: All router TLS certificates are issued and secrets exist
	// - ControllerDeploymentReady: Controller deployment is available
	// - RouterDeploymentsReady: All router deployments are available
	// - Ready: Overall system ready (aggregates all other conditions)
	// +listType=map
	// +listMapKey=type
	// +optional
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status

// Jumpstarter is the Schema for the jumpstarters API.
type Jumpstarter struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   JumpstarterSpec   `json:"spec,omitempty"`
	Status JumpstarterStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// JumpstarterList contains a list of Jumpstarter deployments.
// This is used by kubectl to list multiple Jumpstarter resources.
type JumpstarterList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []Jumpstarter `json:"items"`
}

func init() {
	SchemeBuilder.Register(&Jumpstarter{}, &JumpstarterList{})
}
