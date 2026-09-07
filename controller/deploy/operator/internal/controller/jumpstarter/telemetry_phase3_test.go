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
	"time"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	operatorv1alpha1 "github.com/jumpstarter-dev/jumpstarter/controller/deploy/operator/api/v1alpha1"
)

// JEP-0013 operator defaults (Phase 3). Keep these literals in the tests so
// they encode the JEP rather than the implementation.
const (
	jepDefaultScrapeTimeoutFlag  = "-scrape-timeout=7s"
	jepDefaultDriverTypeEnumFlag = "-driver-type-enum=power,storage,network,serial,console,video,composite"
	jepDefaultExemplarKeysFlag   = "-exemplar-keys=client,lease_id"
	jepMetricsBindFlag           = "-metrics-bind-address=:8080"
)

func phase3TelemetryJS(name, namespace string) *operatorv1alpha1.Jumpstarter {
	return &operatorv1alpha1.Jumpstarter{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: namespace},
		Spec: operatorv1alpha1.JumpstarterSpec{
			CertManager: operatorv1alpha1.CertManagerConfig{Enabled: false},
			Telemetry: &operatorv1alpha1.TelemetryConfig{
				Enabled:         true,
				Image:           "quay.io/jumpstarter-dev/jumpstarter-telemetry:latest",
				ImagePullPolicy: corev1.PullIfNotPresent,
			},
		},
	}
}

var _ = Describe("createTelemetryDeployment JEP-0013 Phase 3", func() {
	It("passes default scrapeTimeout, driverTypeEnum, and exemplarKeys as flags", func() {
		dep := createTelemetryDeployment(phase3TelemetryJS("js", "ns"), "")
		c := dep.Spec.Template.Spec.Containers[0]

		Expect(c.Args).To(ContainElement(jepMetricsBindFlag))
		Expect(c.Args).To(ContainElement(jepDefaultScrapeTimeoutFlag))
		Expect(c.Args).To(ContainElement(jepDefaultDriverTypeEnumFlag))
		Expect(c.Args).To(ContainElement(jepDefaultExemplarKeysFlag))
	})

	It("passes custom spec.telemetry.metrics fields as flags", func() {
		js := phase3TelemetryJS("js", "ns")
		js.Spec.Telemetry.Metrics = operatorv1alpha1.TelemetryMetricsConfig{
			ExemplarKeys:   []string{"client", "board-type"},
			DriverTypeEnum: []string{"power", "can"},
			ScrapeTimeout:  &metav1.Duration{Duration: 3 * time.Second},
		}

		c := createTelemetryDeployment(js, "").Spec.Template.Spec.Containers[0]
		Expect(c.Args).To(ContainElement("-scrape-timeout=3s"))
		Expect(c.Args).To(ContainElement("-driver-type-enum=power,can"))
		Expect(c.Args).To(ContainElement("-exemplar-keys=client,board-type"))
	})

	It("sets GRPC_TELEMETRY_ENDPOINT to the in-cluster telemetry Service", func() {
		js := phase3TelemetryJS("js", "jumpstarter-lab")
		c := createTelemetryDeployment(js, "").Spec.Template.Spec.Containers[0]
		Expect(envValue(c, "GRPC_TELEMETRY_ENDPOINT")).To(Equal(telemetryEndpointFor(js.Namespace)))
	})

	It("exposes container port metrics on 8080", func() {
		c := createTelemetryDeployment(phase3TelemetryJS("js", "ns"), "").Spec.Template.Spec.Containers[0]
		p := namedContainerPort(c.Ports, metricsPortName)
		Expect(p).NotTo(BeNil(), "expected container port named metrics")
		Expect(p.ContainerPort).To(Equal(int32(telemetryMetricsPort)))
		Expect(p.Protocol).To(Equal(corev1.ProtocolTCP))
	})
})

var _ = Describe("Telemetry ConfigMap certificate (JEP-0013 TLS)", func() {
	const crName = "test-telemetry-ca"

	var crNamespace string
	ctx := context.Background()

	BeforeEach(func() {
		ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{GenerateName: "telemetry-ca-"}}
		Expect(k8sClient.Create(ctx, ns)).To(Succeed())
		crNamespace = ns.Name
	})

	AfterEach(func() {
		_ = k8sClient.Delete(ctx, &corev1.Namespace{
			ObjectMeta: metav1.ObjectMeta{Name: crNamespace},
		})
	})

	It("includes the CA PEM when cert-manager is enabled and the CA secret exists", func() {
		js := &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: crName, Namespace: crNamespace},
			Spec: operatorv1alpha1.JumpstarterSpec{
				CertManager: operatorv1alpha1.CertManagerConfig{Enabled: true},
				Telemetry: &operatorv1alpha1.TelemetryConfig{
					Enabled: true,
					Image:   "quay.io/jumpstarter-dev/jumpstarter-telemetry:latest",
				},
			},
		}
		Expect(k8sClient.Create(ctx, &corev1.Secret{
			ObjectMeta: metav1.ObjectMeta{
				Name:      crName + caCertificateSuffix,
				Namespace: crNamespace,
			},
			Data: map[string][]byte{"tls.crt": []byte(testPEM)},
		})).To(Succeed())

		r := &JumpstarterReconciler{Client: k8sClient, Scheme: k8sClient.Scheme()}
		cfg, err := r.buildConfig(ctx, js)
		Expect(err).NotTo(HaveOccurred())
		Expect(cfg.Telemetry).NotTo(BeNil())
		Expect(cfg.Telemetry.Enabled).To(BeTrue())
		Expect(cfg.Telemetry.Endpoint).To(Equal(telemetryEndpointFor(crNamespace)))
		Expect(cfg.Telemetry.Certificate).To(Equal(testPEM))
	})

	It("omits the telemetry certificate when cert-manager is disabled", func() {
		js := &operatorv1alpha1.Jumpstarter{
			ObjectMeta: metav1.ObjectMeta{Name: crName, Namespace: crNamespace},
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
		Expect(cfg.Telemetry.Certificate).To(BeEmpty())
	})
})
