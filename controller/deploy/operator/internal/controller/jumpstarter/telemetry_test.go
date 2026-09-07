/*
Copyright 2026. The Jumpstarter Authors.

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
	"fmt"
	"strings"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	operatorv1alpha1 "github.com/jumpstarter-dev/jumpstarter/controller/deploy/operator/api/v1alpha1"
	"github.com/jumpstarter-dev/jumpstarter/controller/deploy/operator/internal/controller/jumpstarter/endpoints"
)

var _ = Describe("Telemetry Lifecycle", func() {
	const crName = "test-telemetry"

	var crNamespace string
	ctx := context.Background()

	makeJumpstarterSpec := func() operatorv1alpha1.JumpstarterSpec {
		return operatorv1alpha1.JumpstarterSpec{
			BaseDomain: "example.com",
			CertManager: operatorv1alpha1.CertManagerConfig{
				Enabled: false,
			},
			Controller: operatorv1alpha1.ControllerConfig{
				Image:    "quay.io/jumpstarter/jumpstarter:latest",
				Replicas: 1,
				GRPC: operatorv1alpha1.GRPCConfig{
					Endpoints: []operatorv1alpha1.Endpoint{{Address: "controller"}},
				},
			},
			Routers: operatorv1alpha1.RoutersConfig{
				Image:    "quay.io/jumpstarter/jumpstarter:latest",
				Replicas: 1,
				GRPC: operatorv1alpha1.GRPCConfig{
					Endpoints: []operatorv1alpha1.Endpoint{{Address: "router"}},
				},
			},
		}
	}

	newReconciler := func() *JumpstarterReconciler {
		return &JumpstarterReconciler{
			Client:             k8sClient,
			Scheme:             k8sClient.Scheme(),
			EndpointReconciler: endpoints.NewReconciler(k8sClient, k8sClient.Scheme(), cfg),
		}
	}

	doReconcile := func() {
		_, err := newReconciler().Reconcile(ctx, reconcile.Request{
			NamespacedName: types.NamespacedName{Name: crName, Namespace: crNamespace},
		})
		Expect(err).NotTo(HaveOccurred())
	}

	getConfigData := func() string {
		cm := &corev1.ConfigMap{}
		err := k8sClient.Get(ctx, types.NamespacedName{
			Name:      "jumpstarter-controller",
			Namespace: crNamespace,
		}, cm)
		Expect(err).NotTo(HaveOccurred())
		return cm.Data["config"]
	}

	BeforeEach(func() {
		ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{GenerateName: "telemetry-test-"}}
		Expect(k8sClient.Create(ctx, ns)).To(Succeed())
		crNamespace = ns.Name
	})

	AfterEach(func() {
		_ = k8sClient.Delete(ctx, &corev1.Namespace{
			ObjectMeta: metav1.ObjectMeta{Name: crNamespace},
		})
	})

	It("creates Deployment and Service when telemetry is enabled", func() {
		By("creating a Jumpstarter CR with telemetry enabled")
		spec := makeJumpstarterSpec()
		spec.Telemetry = &operatorv1alpha1.TelemetryConfig{
			Enabled:         true,
			Image:           "quay.io/jumpstarter-dev/jumpstarter-telemetry:latest",
			ImagePullPolicy: corev1.PullIfNotPresent,
		}
		Expect(k8sClient.Create(ctx, &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: crName, Namespace: crNamespace},
			Spec:       spec,
		})).To(Succeed())

		By("reconciling")
		doReconcile()

		By("verifying the Deployment exists with correct settings")
		deployment := &appsv1.Deployment{}
		Expect(k8sClient.Get(ctx, types.NamespacedName{
			Name:      crName + "-telemetry",
			Namespace: crNamespace,
		}, deployment)).To(Succeed())

		Expect(deployment.Spec.Template.Spec.Containers).To(HaveLen(1))
		container := deployment.Spec.Template.Spec.Containers[0]
		Expect(container.Name).To(Equal("telemetry"))
		Expect(container.Image).To(Equal("quay.io/jumpstarter-dev/jumpstarter-telemetry:latest"))
		Expect(container.ImagePullPolicy).To(Equal(corev1.PullIfNotPresent))
		Expect(container.Command).To(Equal([]string{"/telemetry"}))
		Expect(container.Args).To(ContainElement(fmt.Sprintf("--grpc-bind=:%d", telemetryPort)))

		By("verifying labels")
		Expect(deployment.Labels).To(HaveKeyWithValue("component", "telemetry"))
		Expect(deployment.Labels).To(HaveKeyWithValue("app", "jumpstarter-telemetry"))
		Expect(deployment.Labels).To(HaveKeyWithValue("controller", crName))

		By("verifying replicas default to 1")
		Expect(*deployment.Spec.Replicas).To(Equal(int32(1)))

		By("verifying security context")
		Expect(container.SecurityContext.AllowPrivilegeEscalation).NotTo(BeNil())
		Expect(*container.SecurityContext.AllowPrivilegeEscalation).To(BeFalse())
		Expect(container.SecurityContext.Capabilities.Drop).To(ContainElement(corev1.Capability("ALL")))
		Expect(deployment.Spec.Template.Spec.SecurityContext.RunAsNonRoot).NotTo(BeNil())
		Expect(*deployment.Spec.Template.Spec.SecurityContext.RunAsNonRoot).To(BeTrue())

		By("verifying the Service exists with gRPC and metrics ports")
		svc := &corev1.Service{}
		Expect(k8sClient.Get(ctx, types.NamespacedName{
			Name:      telemetryServiceName,
			Namespace: crNamespace,
		}, svc)).To(Succeed())
		Expect(svc.Spec.Type).To(Equal(corev1.ServiceTypeClusterIP))
		Expect(svc.Spec.Selector).To(HaveKeyWithValue("app", "jumpstarter-telemetry"))

		grpcP := namedServicePort(svc.Spec.Ports, grpcPortName)
		Expect(grpcP).NotTo(BeNil(), "expected Service port named grpc")
		Expect(grpcP.Port).To(Equal(int32(telemetryPort)))
		Expect(grpcP.TargetPort.IntValue()).To(Equal(telemetryPort))

		metricsP := namedServicePort(svc.Spec.Ports, metricsPortName)
		Expect(metricsP).NotTo(BeNil(), "expected Service port named metrics (JEP-0013 /metrics scrape)")
		Expect(metricsP.Port).To(Equal(int32(telemetryMetricsPort)))
		Expect(metricsP.TargetPort.IntValue()).To(Equal(telemetryMetricsPort))
	})

	It("uses HTTP /healthz and /readyz on the metrics port (JEP-0013 DD-7)", func() {
		By("creating a Jumpstarter CR with telemetry enabled")
		spec := makeJumpstarterSpec()
		spec.Telemetry = &operatorv1alpha1.TelemetryConfig{
			Enabled: true,
			Image:   "quay.io/jumpstarter-dev/jumpstarter-telemetry:latest",
		}
		Expect(k8sClient.Create(ctx, &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: crName, Namespace: crNamespace},
			Spec:       spec,
		})).To(Succeed())

		doReconcile()

		deployment := &appsv1.Deployment{}
		Expect(k8sClient.Get(ctx, types.NamespacedName{
			Name:      crName + "-telemetry",
			Namespace: crNamespace,
		}, deployment)).To(Succeed())

		container := deployment.Spec.Template.Spec.Containers[0]

		By("verifying liveness probe is HTTP GET /healthz on the metrics port")
		Expect(container.LivenessProbe).NotTo(BeNil())
		Expect(container.LivenessProbe.HTTPGet).NotTo(BeNil())
		Expect(container.LivenessProbe.HTTPGet.Path).To(Equal("/healthz"))
		Expect(container.LivenessProbe.HTTPGet.Port.IntValue()).To(Equal(telemetryMetricsPort))

		By("verifying readiness probe is HTTP GET /readyz on the metrics port")
		Expect(container.ReadinessProbe).NotTo(BeNil())
		Expect(container.ReadinessProbe.HTTPGet).NotTo(BeNil())
		Expect(container.ReadinessProbe.HTTPGet.Path).To(Equal("/readyz"))
		Expect(container.ReadinessProbe.HTTPGet.Port.IntValue()).To(Equal(telemetryMetricsPort))
	})

	It("respects the replicas field", func() {
		By("creating a Jumpstarter CR with telemetry replicas=3")
		spec := makeJumpstarterSpec()
		spec.Telemetry = &operatorv1alpha1.TelemetryConfig{
			Enabled:  true,
			Image:    "quay.io/jumpstarter-dev/jumpstarter-telemetry:latest",
			Replicas: ptr.To(int32(3)),
		}
		Expect(k8sClient.Create(ctx, &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: crName, Namespace: crNamespace},
			Spec:       spec,
		})).To(Succeed())

		doReconcile()

		deployment := &appsv1.Deployment{}
		Expect(k8sClient.Get(ctx, types.NamespacedName{
			Name:      crName + "-telemetry",
			Namespace: crNamespace,
		}, deployment)).To(Succeed())
		Expect(*deployment.Spec.Replicas).To(Equal(int32(3)))
	})

	It("does not create telemetry resources when telemetry is disabled", func() {
		By("creating a Jumpstarter CR without telemetry")
		spec := makeJumpstarterSpec()
		Expect(k8sClient.Create(ctx, &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: crName, Namespace: crNamespace},
			Spec:       spec,
		})).To(Succeed())

		doReconcile()

		By("verifying no telemetry Deployment exists")
		deployment := &appsv1.Deployment{}
		err := k8sClient.Get(ctx, types.NamespacedName{
			Name:      crName + "-telemetry",
			Namespace: crNamespace,
		}, deployment)
		Expect(errors.IsNotFound(err)).To(BeTrue(), "telemetry deployment should not exist")

		By("verifying no telemetry Service exists")
		svc := &corev1.Service{}
		err = k8sClient.Get(ctx, types.NamespacedName{
			Name:      telemetryServiceName,
			Namespace: crNamespace,
		}, svc)
		Expect(errors.IsNotFound(err)).To(BeTrue(), "telemetry service should not exist")

		By("verifying no telemetry ServiceAccount exists")
		sa := &corev1.ServiceAccount{}
		err = k8sClient.Get(ctx, types.NamespacedName{
			Name:      crName + telemetrySASuffix,
			Namespace: crNamespace,
		}, sa)
		Expect(errors.IsNotFound(err)).To(BeTrue(), "telemetry service account should not exist")
	})

	It("cleans up telemetry resources when telemetry is disabled after being enabled", func() {
		By("creating a Jumpstarter CR with telemetry enabled")
		spec := makeJumpstarterSpec()
		spec.Telemetry = &operatorv1alpha1.TelemetryConfig{
			Enabled: true,
			Image:   "quay.io/jumpstarter-dev/jumpstarter-telemetry:latest",
		}
		Expect(k8sClient.Create(ctx, &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: crName, Namespace: crNamespace},
			Spec:       spec,
		})).To(Succeed())

		By("first reconcile — resources should be created")
		doReconcile()

		deployment := &appsv1.Deployment{}
		Expect(k8sClient.Get(ctx, types.NamespacedName{
			Name:      crName + "-telemetry",
			Namespace: crNamespace,
		}, deployment)).To(Succeed())

		svc := &corev1.Service{}
		Expect(k8sClient.Get(ctx, types.NamespacedName{
			Name:      telemetryServiceName,
			Namespace: crNamespace,
		}, svc)).To(Succeed())

		By("disabling telemetry")
		js := &operatorv1alpha1.Jumpstarter{}
		Expect(k8sClient.Get(ctx, types.NamespacedName{Name: crName, Namespace: crNamespace}, js)).To(Succeed())
		js.Spec.Telemetry.Enabled = false
		Expect(k8sClient.Update(ctx, js)).To(Succeed())

		By("second reconcile — resources should be cleaned up")
		doReconcile()

		err := k8sClient.Get(ctx, types.NamespacedName{
			Name:      crName + "-telemetry",
			Namespace: crNamespace,
		}, deployment)
		Expect(errors.IsNotFound(err)).To(BeTrue(), "telemetry deployment should be deleted")

		err = k8sClient.Get(ctx, types.NamespacedName{
			Name:      telemetryServiceName,
			Namespace: crNamespace,
		}, svc)
		Expect(errors.IsNotFound(err)).To(BeTrue(), "telemetry service should be deleted")

		By("verifying the dedicated ServiceAccount is also deleted")
		sa := &corev1.ServiceAccount{}
		err = k8sClient.Get(ctx, types.NamespacedName{
			Name:      crName + telemetrySASuffix,
			Namespace: crNamespace,
		}, sa)
		Expect(errors.IsNotFound(err)).To(BeTrue(), "telemetry service account should be deleted")

		By("verifying telemetry is absent from the controller ConfigMap")
		configData := getConfigData()
		Expect(configData).NotTo(ContainSubstring("telemetry"),
			"telemetry should not appear in ConfigMap after disabling")
	})

	It("propagates telemetry config into the controller ConfigMap", func() {
		By("creating a Jumpstarter CR with telemetry and a custom minSeverity")
		spec := makeJumpstarterSpec()
		spec.Telemetry = &operatorv1alpha1.TelemetryConfig{
			Enabled: true,
			Image:   "quay.io/jumpstarter-dev/jumpstarter-telemetry:latest",
			Logging: operatorv1alpha1.TelemetryLoggingConfig{
				Filter: operatorv1alpha1.TelemetryLoggingFilterConfig{
					MinSeverity: "warning",
				},
			},
		}
		Expect(k8sClient.Create(ctx, &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: crName, Namespace: crNamespace},
			Spec:       spec,
		})).To(Succeed())

		doReconcile()

		configData := getConfigData()
		Expect(configData).To(ContainSubstring("telemetry"))
		Expect(configData).To(ContainSubstring("enabled: true"))
		Expect(configData).To(ContainSubstring(telemetryServiceName))
		Expect(configData).To(ContainSubstring("warning"))
	})

	It("does not include telemetry certificate in ConfigMap when cert-manager is disabled", func() {
		By("creating a Jumpstarter CR with telemetry enabled but cert-manager disabled")
		spec := makeJumpstarterSpec()
		spec.Telemetry = &operatorv1alpha1.TelemetryConfig{
			Enabled: true,
			Image:   "quay.io/jumpstarter-dev/jumpstarter-telemetry:latest",
		}
		Expect(k8sClient.Create(ctx, &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: crName, Namespace: crNamespace},
			Spec:       spec,
		})).To(Succeed())

		doReconcile()

		configData := getConfigData()
		Expect(configData).To(ContainSubstring("telemetry"))
		Expect(configData).NotTo(ContainSubstring("certificate:"))
	})

	It("does not include telemetry in ConfigMap when disabled", func() {
		By("creating a Jumpstarter CR without telemetry")
		spec := makeJumpstarterSpec()
		Expect(k8sClient.Create(ctx, &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: crName, Namespace: crNamespace},
			Spec:       spec,
		})).To(Succeed())

		doReconcile()

		configData := getConfigData()
		Expect(configData).NotTo(ContainSubstring("telemetry"))
	})

	It("sets TelemetryDeploymentReady status condition", func() {
		By("creating a Jumpstarter CR with telemetry enabled")
		spec := makeJumpstarterSpec()
		spec.Telemetry = &operatorv1alpha1.TelemetryConfig{
			Enabled: true,
			Image:   "quay.io/jumpstarter-dev/jumpstarter-telemetry:latest",
		}
		Expect(k8sClient.Create(ctx, &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: crName, Namespace: crNamespace},
			Spec:       spec,
		})).To(Succeed())

		By("first reconcile — deployment exists but not Available yet")
		doReconcile()

		js := &operatorv1alpha1.Jumpstarter{}
		Expect(k8sClient.Get(ctx, types.NamespacedName{Name: crName, Namespace: crNamespace}, js)).To(Succeed())
		cond := meta.FindStatusCondition(js.Status.Conditions, operatorv1alpha1.ConditionTypeTelemetryDeploymentReady)
		Expect(cond).NotTo(BeNil(), "TelemetryDeploymentReady condition should be set")
		Expect(cond.Status).To(Equal(metav1.ConditionFalse),
			"should be False while deployment is not Available")

		By("marking the telemetry deployment as Available")
		deployment := &appsv1.Deployment{}
		Expect(k8sClient.Get(ctx, types.NamespacedName{
			Name:      crName + "-telemetry",
			Namespace: crNamespace,
		}, deployment)).To(Succeed())
		deployment.Status.Conditions = []appsv1.DeploymentCondition{
			{
				Type:   appsv1.DeploymentAvailable,
				Status: corev1.ConditionTrue,
				Reason: "MinimumReplicasAvailable",
			},
		}
		Expect(k8sClient.Status().Update(ctx, deployment)).To(Succeed())

		By("second reconcile — condition should become True")
		doReconcile()

		Expect(k8sClient.Get(ctx, types.NamespacedName{Name: crName, Namespace: crNamespace}, js)).To(Succeed())
		cond = meta.FindStatusCondition(js.Status.Conditions, operatorv1alpha1.ConditionTypeTelemetryDeploymentReady)
		Expect(cond).NotTo(BeNil())
		Expect(cond.Status).To(Equal(metav1.ConditionTrue))
		Expect(cond.Reason).To(Equal("DeploymentAvailable"))
	})

	It("mounts TLS certs when cert-manager is enabled", func() {
		js := &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: "test-tls", Namespace: "default"},
			Spec: operatorv1alpha1.JumpstarterSpec{
				CertManager: operatorv1alpha1.CertManagerConfig{Enabled: true},
				Telemetry: &operatorv1alpha1.TelemetryConfig{
					Enabled:         true,
					Image:           "quay.io/jumpstarter-dev/jumpstarter-telemetry:latest",
					ImagePullPolicy: corev1.PullIfNotPresent,
				},
			},
		}

		dep := createTelemetryDeployment(js, "fake-tls-hash")

		container := dep.Spec.Template.Spec.Containers[0]

		// CONTROLLER_KEY + GRPC_TELEMETRY_ENDPOINT + TLS env vars
		Expect(container.Env).To(HaveLen(4))
		envNames := make(map[string]string)
		for _, env := range container.Env {
			envNames[env.Name] = env.Value
		}
		Expect(envNames).To(HaveKey("CONTROLLER_KEY"))
		Expect(envNames).To(HaveKey("GRPC_TELEMETRY_ENDPOINT"))
		Expect(envNames).To(HaveKeyWithValue("EXTERNAL_CERT_PEM", "/tls/tls.crt"))
		Expect(envNames).To(HaveKeyWithValue("EXTERNAL_KEY_PEM", "/tls/tls.key"))

		// Should have TLS volume mount
		Expect(container.VolumeMounts).To(HaveLen(1))
		Expect(container.VolumeMounts[0].Name).To(Equal("tls-certs"))
		Expect(container.VolumeMounts[0].MountPath).To(Equal("/tls"))
		Expect(container.VolumeMounts[0].ReadOnly).To(BeTrue())

		// Should have TLS volume
		Expect(dep.Spec.Template.Spec.Volumes).To(HaveLen(1))
		Expect(dep.Spec.Template.Spec.Volumes[0].Name).To(Equal("tls-certs"))
		Expect(dep.Spec.Template.Spec.Volumes[0].Secret.SecretName).To(Equal("test-tls-telemetry-tls"))

		// Should have TLS hash annotation for rolling restart on cert renewal
		Expect(dep.Spec.Template.Annotations).To(HaveKeyWithValue("jumpstarter.dev/tls-secret-sha256", "fake-tls-hash"))
	})

	It("mounts TLS certs with manual CertSecret when cert-manager is disabled", func() {
		js := &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: "test-manual-tls", Namespace: "default"},
			Spec: operatorv1alpha1.JumpstarterSpec{
				CertManager: operatorv1alpha1.CertManagerConfig{Enabled: false},
				Telemetry: &operatorv1alpha1.TelemetryConfig{
					Enabled:         true,
					Image:           "quay.io/jumpstarter-dev/jumpstarter-telemetry:latest",
					ImagePullPolicy: corev1.PullIfNotPresent,
					GRPC: operatorv1alpha1.TelemetryGRPCConfig{
						TLS: operatorv1alpha1.TLSConfig{
							CertSecret: "my-custom-telemetry-tls",
						},
					},
				},
			},
		}

		dep := createTelemetryDeployment(js, "manual-tls-hash")

		container := dep.Spec.Template.Spec.Containers[0]

		// CONTROLLER_KEY + GRPC_TELEMETRY_ENDPOINT + TLS env vars
		Expect(container.Env).To(HaveLen(4))
		envNames := make(map[string]string)
		for _, env := range container.Env {
			envNames[env.Name] = env.Value
		}
		Expect(envNames).To(HaveKey("CONTROLLER_KEY"))
		Expect(envNames).To(HaveKey("GRPC_TELEMETRY_ENDPOINT"))
		Expect(envNames).To(HaveKeyWithValue("EXTERNAL_CERT_PEM", "/tls/tls.crt"))
		Expect(envNames).To(HaveKeyWithValue("EXTERNAL_KEY_PEM", "/tls/tls.key"))

		// Should have TLS volume mount
		Expect(container.VolumeMounts).To(HaveLen(1))
		Expect(container.VolumeMounts[0].Name).To(Equal("tls-certs"))
		Expect(container.VolumeMounts[0].MountPath).To(Equal("/tls"))

		// Should have TLS volume with manual secret name
		Expect(dep.Spec.Template.Spec.Volumes).To(HaveLen(1))
		Expect(dep.Spec.Template.Spec.Volumes[0].Name).To(Equal("tls-certs"))
		Expect(dep.Spec.Template.Spec.Volumes[0].Secret.SecretName).To(Equal("my-custom-telemetry-tls"))

		// Should have TLS hash annotation
		Expect(dep.Spec.Template.Annotations).To(HaveKeyWithValue("jumpstarter.dev/tls-secret-sha256", "manual-tls-hash"))
	})

	It("does not mount TLS certs when no TLS is configured", func() {
		js := &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: "test-no-tls", Namespace: "default"},
			Spec: operatorv1alpha1.JumpstarterSpec{
				CertManager: operatorv1alpha1.CertManagerConfig{Enabled: false},
				Telemetry: &operatorv1alpha1.TelemetryConfig{
					Enabled:         true,
					Image:           "quay.io/jumpstarter-dev/jumpstarter-telemetry:latest",
					ImagePullPolicy: corev1.PullIfNotPresent,
					// No TLS.CertSecret configured
				},
			},
		}

		// Empty hash when no TLS is configured
		dep := createTelemetryDeployment(js, "")

		container := dep.Spec.Template.Spec.Containers[0]

		// CONTROLLER_KEY + GRPC_TELEMETRY_ENDPOINT (no TLS env vars)
		Expect(container.Env).To(HaveLen(2))
		envNames := make(map[string]string)
		for _, env := range container.Env {
			envNames[env.Name] = env.Value
		}
		Expect(envNames).To(HaveKey("CONTROLLER_KEY"))
		Expect(envNames).To(HaveKey("GRPC_TELEMETRY_ENDPOINT"))
		Expect(envNames).NotTo(HaveKey("EXTERNAL_CERT_PEM"))

		// No volume mounts or volumes
		Expect(container.VolumeMounts).To(BeEmpty())
		Expect(dep.Spec.Template.Spec.Volumes).To(BeEmpty())

		// No TLS hash annotation when no TLS is configured
		Expect(dep.Spec.Template.Annotations).To(BeNil())
	})

	It("uses a dedicated service account separate from the controller", func() {
		spec := makeJumpstarterSpec()
		spec.Telemetry = &operatorv1alpha1.TelemetryConfig{
			Enabled: true,
			Image:   "quay.io/jumpstarter-dev/jumpstarter-telemetry:latest",
		}
		Expect(k8sClient.Create(ctx, &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: crName, Namespace: crNamespace},
			Spec:       spec,
		})).To(Succeed())

		doReconcile()

		telemetryDep := &appsv1.Deployment{}
		Expect(k8sClient.Get(ctx, types.NamespacedName{
			Name:      crName + "-telemetry",
			Namespace: crNamespace,
		}, telemetryDep)).To(Succeed())

		expectedSA := crName + telemetrySASuffix
		Expect(telemetryDep.Spec.Template.Spec.ServiceAccountName).To(Equal(expectedSA))

		By("verifying the dedicated ServiceAccount exists with no RBAC bindings")
		sa := &corev1.ServiceAccount{}
		Expect(k8sClient.Get(ctx, types.NamespacedName{
			Name:      expectedSA,
			Namespace: crNamespace,
		}, sa)).To(Succeed())
	})

	It("updates the deployment when the CR spec changes", func() {
		By("creating a Jumpstarter CR with telemetry enabled")
		spec := makeJumpstarterSpec()
		spec.Telemetry = &operatorv1alpha1.TelemetryConfig{
			Enabled: true,
			Image:   "quay.io/jumpstarter-dev/jumpstarter-telemetry:v1",
		}
		Expect(k8sClient.Create(ctx, &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: crName, Namespace: crNamespace},
			Spec:       spec,
		})).To(Succeed())

		doReconcile()

		By("verifying initial image")
		deployment := &appsv1.Deployment{}
		Expect(k8sClient.Get(ctx, types.NamespacedName{
			Name:      crName + "-telemetry",
			Namespace: crNamespace,
		}, deployment)).To(Succeed())
		Expect(deployment.Spec.Template.Spec.Containers[0].Image).To(
			Equal("quay.io/jumpstarter-dev/jumpstarter-telemetry:v1"))

		By("updating the image in the CR")
		js := &operatorv1alpha1.Jumpstarter{}
		Expect(k8sClient.Get(ctx, types.NamespacedName{Name: crName, Namespace: crNamespace}, js)).To(Succeed())
		js.Spec.Telemetry.Image = "quay.io/jumpstarter-dev/jumpstarter-telemetry:v2"
		Expect(k8sClient.Update(ctx, js)).To(Succeed())

		By("reconciling again")
		doReconcile()

		By("verifying image is updated")
		Expect(k8sClient.Get(ctx, types.NamespacedName{
			Name:      crName + "-telemetry",
			Namespace: crNamespace,
		}, deployment)).To(Succeed())
		Expect(deployment.Spec.Template.Spec.Containers[0].Image).To(
			Equal("quay.io/jumpstarter-dev/jumpstarter-telemetry:v2"))
	})
})

var _ = Describe("telemetryEndpointFor", func() {
	It("returns the correct in-cluster endpoint format", func() {
		endpoint := telemetryEndpointFor("my-namespace")
		Expect(endpoint).To(Equal(fmt.Sprintf("%s.my-namespace.svc:%d", telemetryServiceName, telemetryPort)))
	})
})

var _ = Describe("telemetryLabels", func() {
	It("returns correct labels", func() {
		js := &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: "my-js"},
		}
		labels := telemetryLabels(js)
		Expect(labels).To(HaveKeyWithValue("component", "telemetry"))
		Expect(labels).To(HaveKeyWithValue("app", telemetryComponentApp))
		Expect(labels).To(HaveKeyWithValue("controller", "my-js"))
	})
})

var _ = Describe("GetTelemetryCertSecretName", func() {
	It("returns the correct secret name", func() {
		js := &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: "jumpstarter"},
		}
		Expect(GetTelemetryCertSecretName(js)).To(Equal("jumpstarter-telemetry-tls"))
	})
})

var _ = Describe("defaultTelemetryResources", func() {
	It("should return defaults when spec is empty", func() {
		result := defaultTelemetryResources(corev1.ResourceRequirements{})

		Expect(result.Requests).To(HaveKeyWithValue(corev1.ResourceCPU, resource.MustParse("50m")))
		Expect(result.Requests).To(HaveKeyWithValue(corev1.ResourceMemory, resource.MustParse("128Mi")))
		Expect(result.Limits).To(HaveKeyWithValue(corev1.ResourceCPU, resource.MustParse("500m")))
		Expect(result.Limits).To(HaveKeyWithValue(corev1.ResourceMemory, resource.MustParse("256Mi")))
	})

	It("should return user-specified resources when requests are set", func() {
		custom := corev1.ResourceRequirements{
			Requests: corev1.ResourceList{
				corev1.ResourceCPU: resource.MustParse("100m"),
			},
		}

		result := defaultTelemetryResources(custom)

		Expect(result.Requests).To(HaveKeyWithValue(corev1.ResourceCPU, resource.MustParse("100m")))
		Expect(result.Limits).To(BeNil())
	})

	It("should return user-specified resources when limits are set", func() {
		custom := corev1.ResourceRequirements{
			Limits: corev1.ResourceList{
				corev1.ResourceMemory: resource.MustParse("512Mi"),
			},
		}

		result := defaultTelemetryResources(custom)

		Expect(result.Limits).To(HaveKeyWithValue(corev1.ResourceMemory, resource.MustParse("512Mi")))
		Expect(result.Requests).To(BeNil())
	})

	It("should preserve claims-only input without applying defaults", func() {
		custom := corev1.ResourceRequirements{
			Claims: []corev1.ResourceClaim{{Name: "gpu"}},
		}

		result := defaultTelemetryResources(custom)

		Expect(result.Claims).To(HaveLen(1))
		Expect(result.Claims[0].Name).To(Equal("gpu"))
		Expect(result.Requests).To(BeNil())
		Expect(result.Limits).To(BeNil())
	})
})

var _ = Describe("collectTelemetryDNSNames", func() {
	It("includes internal DNS names when includeInternalNames is true", func() {
		js := &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: "test", Namespace: "my-ns"},
		}
		r := &JumpstarterReconciler{}
		dnsNames := r.collectTelemetryDNSNames(js, true)

		Expect(dnsNames).To(HaveLen(4))
		Expect(dnsNames).To(ContainElement(telemetryServiceName))
		Expect(dnsNames).To(ContainElement(telemetryServiceName + ".my-ns"))
		Expect(dnsNames).To(ContainElement(telemetryServiceName + ".my-ns.svc"))
		Expect(dnsNames).To(ContainElement(telemetryServiceName + ".my-ns.svc.cluster.local"))
	})

	It("returns empty when includeInternalNames is false", func() {
		js := &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: "test", Namespace: "my-ns"},
		}
		r := &JumpstarterReconciler{}
		dnsNames := r.collectTelemetryDNSNames(js, false)

		Expect(dnsNames).To(BeEmpty())
	})
})

var _ = Describe("resolveTelemetryCA", func() {
	var crNamespace string
	ctx := context.Background()

	BeforeEach(func() {
		ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{GenerateName: "tel-ca-test-"}}
		Expect(k8sClient.Create(ctx, ns)).To(Succeed())
		crNamespace = ns.Name
	})

	AfterEach(func() {
		_ = k8sClient.Delete(ctx, &corev1.Namespace{
			ObjectMeta: metav1.ObjectMeta{Name: crNamespace},
		})
	})

	It("returns the caBundle from an external issuer when provided", func() {
		js := &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: "test-ca", Namespace: crNamespace},
			Spec: operatorv1alpha1.JumpstarterSpec{
				CertManager: operatorv1alpha1.CertManagerConfig{
					Enabled: true,
					Server: &operatorv1alpha1.ServerCertConfig{
						IssuerRef: &operatorv1alpha1.IssuerReference{
							Name:     "my-issuer",
							Kind:     "ClusterIssuer",
							CABundle: []byte("fake-ca-bundle"),
						},
					},
				},
			},
		}

		r := &JumpstarterReconciler{Client: k8sClient, Scheme: k8sClient.Scheme()}
		ca, err := r.resolveTelemetryCA(ctx, js)
		Expect(err).NotTo(HaveOccurred())
		Expect(ca).To(Equal("fake-ca-bundle"))
	})

	It("reads the CA from the self-signed CA secret", func() {
		By("creating the CA secret")
		caSecretName := "test-ca-self" + caCertificateSuffix
		Expect(k8sClient.Create(ctx, &corev1.Secret{
			ObjectMeta: metav1.ObjectMeta{Name: caSecretName, Namespace: crNamespace},
			Data:       map[string][]byte{"tls.crt": []byte(testPEM)},
		})).To(Succeed())

		js := &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: "test-ca-self", Namespace: crNamespace},
			Spec: operatorv1alpha1.JumpstarterSpec{
				CertManager: operatorv1alpha1.CertManagerConfig{Enabled: true},
			},
		}

		r := &JumpstarterReconciler{Client: k8sClient, Scheme: k8sClient.Scheme()}
		ca, err := r.resolveTelemetryCA(ctx, js)
		Expect(err).NotTo(HaveOccurred())
		Expect(ca).To(ContainSubstring("BEGIN CERTIFICATE"))
	})

	It("returns an error when the CA secret does not exist", func() {
		js := &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: "test-ca-missing", Namespace: crNamespace},
			Spec: operatorv1alpha1.JumpstarterSpec{
				CertManager: operatorv1alpha1.CertManagerConfig{Enabled: true},
			},
		}

		r := &JumpstarterReconciler{Client: k8sClient, Scheme: k8sClient.Scheme()}
		_, err := r.resolveTelemetryCA(ctx, js)
		Expect(err).To(HaveOccurred())
		Expect(strings.ToLower(err.Error())).To(ContainSubstring("not found"))
	})

	It("returns an error when the CA secret exists but is missing tls.crt", func() {
		By("creating a CA secret without tls.crt key")
		caSecretName := "test-ca-no-cert" + caCertificateSuffix
		Expect(k8sClient.Create(ctx, &corev1.Secret{
			ObjectMeta: metav1.ObjectMeta{Name: caSecretName, Namespace: crNamespace},
			Data:       map[string][]byte{"other-key": []byte("some-data")},
		})).To(Succeed())

		js := &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: "test-ca-no-cert", Namespace: crNamespace},
			Spec: operatorv1alpha1.JumpstarterSpec{
				CertManager: operatorv1alpha1.CertManagerConfig{Enabled: true},
			},
		}

		r := &JumpstarterReconciler{Client: k8sClient, Scheme: k8sClient.Scheme()}
		_, err := r.resolveTelemetryCA(ctx, js)
		Expect(err).To(HaveOccurred())
		Expect(err.Error()).To(ContainSubstring("missing tls.crt"))
	})

	It("returns ('', nil) when an external IssuerRef has a nil CABundle", func() {
		js := &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: "test-ca-no-bundle", Namespace: crNamespace},
			Spec: operatorv1alpha1.JumpstarterSpec{
				CertManager: operatorv1alpha1.CertManagerConfig{
					Enabled: true,
					Server: &operatorv1alpha1.ServerCertConfig{
						IssuerRef: &operatorv1alpha1.IssuerReference{
							Name: "my-issuer",
							Kind: "ClusterIssuer",
							// CABundle intentionally nil
						},
					},
				},
			},
		}

		r := &JumpstarterReconciler{Client: k8sClient, Scheme: k8sClient.Scheme()}
		ca, err := r.resolveTelemetryCA(ctx, js)
		Expect(err).NotTo(HaveOccurred())
		Expect(ca).To(BeEmpty())
	})

	It("returns ('', nil) when an external IssuerRef has an empty CABundle", func() {
		js := &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: "test-ca-empty-bundle", Namespace: crNamespace},
			Spec: operatorv1alpha1.JumpstarterSpec{
				CertManager: operatorv1alpha1.CertManagerConfig{
					Enabled: true,
					Server: &operatorv1alpha1.ServerCertConfig{
						IssuerRef: &operatorv1alpha1.IssuerReference{
							Name:     "my-issuer",
							Kind:     "ClusterIssuer",
							CABundle: []byte{},
						},
					},
				},
			},
		}

		r := &JumpstarterReconciler{Client: k8sClient, Scheme: k8sClient.Scheme()}
		ca, err := r.resolveTelemetryCA(ctx, js)
		Expect(err).NotTo(HaveOccurred())
		Expect(ca).To(BeEmpty())
	})
})

var _ = Describe("buildConfig telemetry certificate", func() {
	var crNamespace string
	ctx := context.Background()

	BeforeEach(func() {
		ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{GenerateName: "config-test-"}}
		Expect(k8sClient.Create(ctx, ns)).To(Succeed())
		crNamespace = ns.Name
	})

	AfterEach(func() {
		_ = k8sClient.Delete(ctx, &corev1.Namespace{
			ObjectMeta: metav1.ObjectMeta{Name: crNamespace},
		})
	})

	It("includes certificate in telemetry config when cert-manager is enabled and CA exists", func() {
		By("creating the CA secret")
		caSecretName := "test-cfg" + caCertificateSuffix
		Expect(k8sClient.Create(ctx, &corev1.Secret{
			ObjectMeta: metav1.ObjectMeta{Name: caSecretName, Namespace: crNamespace},
			Data:       map[string][]byte{"tls.crt": []byte(testPEM)},
		})).To(Succeed())

		js := &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: "test-cfg", Namespace: crNamespace},
			Spec: operatorv1alpha1.JumpstarterSpec{
				CertManager: operatorv1alpha1.CertManagerConfig{Enabled: true},
				Telemetry: &operatorv1alpha1.TelemetryConfig{
					Enabled: true,
					Image:   "quay.io/jumpstarter-dev/jumpstarter-telemetry:latest",
				},
			},
		}

		r := &JumpstarterReconciler{Client: k8sClient, Scheme: k8sClient.Scheme()}
		cfg, err := r.buildConfig(ctx, js)
		Expect(err).NotTo(HaveOccurred())

		Expect(cfg.Telemetry).NotTo(BeNil())
		Expect(cfg.Telemetry.Enabled).To(BeTrue())
		Expect(cfg.Telemetry.Certificate).To(ContainSubstring("BEGIN CERTIFICATE"))
	})

	It("includes CABundle from external issuer in telemetry config", func() {
		externalCABundle := "-----BEGIN CERTIFICATE-----\nEXTERNAL-CA-BUNDLE\n-----END CERTIFICATE-----"
		js := &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: "test-cfg-external", Namespace: crNamespace},
			Spec: operatorv1alpha1.JumpstarterSpec{
				CertManager: operatorv1alpha1.CertManagerConfig{
					Enabled: true,
					Server: &operatorv1alpha1.ServerCertConfig{
						IssuerRef: &operatorv1alpha1.IssuerReference{
							Name:     "my-external-issuer",
							Kind:     "ClusterIssuer",
							CABundle: []byte(externalCABundle),
						},
					},
				},
				Telemetry: &operatorv1alpha1.TelemetryConfig{
					Enabled: true,
					Image:   "quay.io/jumpstarter-dev/jumpstarter-telemetry:latest",
				},
			},
		}

		r := &JumpstarterReconciler{Client: k8sClient, Scheme: k8sClient.Scheme()}
		cfg, err := r.buildConfig(ctx, js)
		Expect(err).NotTo(HaveOccurred())

		Expect(cfg.Telemetry).NotTo(BeNil())
		Expect(cfg.Telemetry.Enabled).To(BeTrue())
		Expect(cfg.Telemetry.Certificate).To(Equal(externalCABundle))
	})

	It("does not include certificate when cert-manager is disabled", func() {
		js := &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: "test-cfg-no-tls", Namespace: crNamespace},
			Spec: operatorv1alpha1.JumpstarterSpec{
				CertManager: operatorv1alpha1.CertManagerConfig{Enabled: false},
				Telemetry: &operatorv1alpha1.TelemetryConfig{
					Enabled: true,
					Image:   "quay.io/jumpstarter-dev/jumpstarter-telemetry:latest",
				},
			},
		}

		r := &JumpstarterReconciler{Client: k8sClient, Scheme: k8sClient.Scheme()}
		cfg, err := r.buildConfig(ctx, js)
		Expect(err).NotTo(HaveOccurred())

		Expect(cfg.Telemetry).NotTo(BeNil())
		Expect(cfg.Telemetry.Enabled).To(BeTrue())
		Expect(cfg.Telemetry.Certificate).To(BeEmpty())
	})

	It("does not fail when CA secret is missing (logs at default verbosity instead)", func() {
		js := &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: "test-cfg-missing-ca", Namespace: crNamespace},
			Spec: operatorv1alpha1.JumpstarterSpec{
				CertManager: operatorv1alpha1.CertManagerConfig{Enabled: true},
				Telemetry: &operatorv1alpha1.TelemetryConfig{
					Enabled: true,
					Image:   "quay.io/jumpstarter-dev/jumpstarter-telemetry:latest",
				},
			},
		}

		r := &JumpstarterReconciler{Client: k8sClient, Scheme: k8sClient.Scheme()}
		cfg, err := r.buildConfig(ctx, js)
		// Should not fail - logs at default verbosity but continues
		Expect(err).NotTo(HaveOccurred())

		Expect(cfg.Telemetry).NotTo(BeNil())
		Expect(cfg.Telemetry.Enabled).To(BeTrue())
		// Certificate is empty because CA secret doesn't exist
		Expect(cfg.Telemetry.Certificate).To(BeEmpty())
	})
})

var _ = Describe("telemetryCANeedsRequeue", func() {
	var crNamespace string
	ctx := context.Background()

	BeforeEach(func() {
		ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{GenerateName: "tel-requeue-test-"}}
		Expect(k8sClient.Create(ctx, ns)).To(Succeed())
		crNamespace = ns.Name
	})

	AfterEach(func() {
		_ = k8sClient.Delete(ctx, &corev1.Namespace{
			ObjectMeta: metav1.ObjectMeta{Name: crNamespace},
		})
	})

	telemetryEnabledSpec := func() operatorv1alpha1.JumpstarterSpec {
		return operatorv1alpha1.JumpstarterSpec{
			CertManager: operatorv1alpha1.CertManagerConfig{Enabled: true},
			Telemetry: &operatorv1alpha1.TelemetryConfig{
				Enabled: true,
				Image:   "quay.io/jumpstarter-dev/jumpstarter-telemetry:latest",
			},
		}
	}

	It("returns false when telemetry is disabled", func() {
		js := &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: "no-telemetry", Namespace: crNamespace},
			Spec: operatorv1alpha1.JumpstarterSpec{
				CertManager: operatorv1alpha1.CertManagerConfig{Enabled: true},
			},
		}
		r := &JumpstarterReconciler{Client: k8sClient, Scheme: k8sClient.Scheme()}
		Expect(r.telemetryCANeedsRequeue(ctx, js)).To(BeFalse())
	})

	It("returns false when cert-manager is disabled", func() {
		spec := telemetryEnabledSpec()
		spec.CertManager.Enabled = false
		js := &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: "no-cm", Namespace: crNamespace},
			Spec:       spec,
		}
		r := &JumpstarterReconciler{Client: k8sClient, Scheme: k8sClient.Scheme()}
		Expect(r.telemetryCANeedsRequeue(ctx, js)).To(BeFalse())
	})

	It("returns false for external issuer without CABundle", func() {
		spec := telemetryEnabledSpec()
		spec.CertManager.Server = &operatorv1alpha1.ServerCertConfig{
			IssuerRef: &operatorv1alpha1.IssuerReference{
				Name: "letsencrypt-prod",
				Kind: "ClusterIssuer",
			},
		}
		js := &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: "external-issuer", Namespace: crNamespace},
			Spec:       spec,
		}
		r := &JumpstarterReconciler{Client: k8sClient, Scheme: k8sClient.Scheme()}
		Expect(r.telemetryCANeedsRequeue(ctx, js)).To(BeFalse())
	})

	It("returns true when self-signed CA secret is missing", func() {
		js := &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: "missing-ca", Namespace: crNamespace},
			Spec:       telemetryEnabledSpec(),
		}
		r := &JumpstarterReconciler{Client: k8sClient, Scheme: k8sClient.Scheme()}
		Expect(r.telemetryCANeedsRequeue(ctx, js)).To(BeTrue())
	})

	It("returns false when self-signed CA secret exists", func() {
		caSecretName := "ready-ca" + caCertificateSuffix
		Expect(k8sClient.Create(ctx, &corev1.Secret{
			ObjectMeta: metav1.ObjectMeta{Name: caSecretName, Namespace: crNamespace},
			Data:       map[string][]byte{"tls.crt": []byte(testPEM)},
		})).To(Succeed())

		js := &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: "ready-ca", Namespace: crNamespace},
			Spec:       telemetryEnabledSpec(),
		}
		r := &JumpstarterReconciler{Client: k8sClient, Scheme: k8sClient.Scheme()}
		Expect(r.telemetryCANeedsRequeue(ctx, js)).To(BeFalse())
	})
})

func hasEnv(c corev1.Container, name string) bool {
	for _, e := range c.Env {
		if e.Name == name {
			return true
		}
	}
	return false
}

func envValue(c corev1.Container, name string) string {
	for _, e := range c.Env {
		if e.Name == name {
			return e.Value
		}
	}
	return ""
}

func namedContainerPort(ports []corev1.ContainerPort, name string) *corev1.ContainerPort {
	for i := range ports {
		if ports[i].Name == name {
			return &ports[i]
		}
	}
	return nil
}

func namedServicePort(ports []corev1.ServicePort, name string) *corev1.ServicePort {
	for i := range ports {
		if ports[i].Name == name {
			return &ports[i]
		}
	}
	return nil
}
