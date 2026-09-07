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
	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
	corev1 "k8s.io/api/core/v1"
)

const (
	jepDefaultLokiQueueDepthFlag = "-loki-queue-depth=10000"
	jepLokiCAFileFlag            = "-loki-ca-file=/loki-ca/ca.crt"
)

var _ = Describe("createTelemetryDeployment JEP-0013 Loki push", func() {
	It("omits Loki flags when spec.telemetry.loki.url is unset (metrics-only)", func() {
		c := createTelemetryDeployment(phase3TelemetryJS("js", "ns"), "").Spec.Template.Spec.Containers[0]
		for _, arg := range c.Args {
			Expect(arg).NotTo(HavePrefix("-loki-url="))
			Expect(arg).NotTo(HavePrefix("-loki-queue-depth="))
			Expect(arg).NotTo(HavePrefix("-loki-ca-file="))
			Expect(arg).NotTo(HavePrefix("-loki-insecure-skip-verify"))
		}
		Expect(hasEnv(c, "LOKI_USERNAME")).To(BeFalse())
		Expect(hasEnv(c, "LOKI_PASSWORD")).To(BeFalse())
		Expect(hasEnv(c, "LOKI_TOKEN")).To(BeFalse())
	})

	It("passes loki.url and default queueDepth when Loki is configured", func() {
		js := phase3TelemetryJS("js", "ns")
		js.Spec.Telemetry.Loki.URL = "https://loki-gateway.monitoring.svc:3100/loki/api/v1/push"

		c := createTelemetryDeployment(js, "").Spec.Template.Spec.Containers[0]
		Expect(c.Args).To(ContainElement("-loki-url=https://loki-gateway.monitoring.svc:3100/loki/api/v1/push"))
		Expect(c.Args).To(ContainElement(jepDefaultLokiQueueDepthFlag))
	})

	It("passes custom spec.telemetry.backpressure.queueDepth", func() {
		js := phase3TelemetryJS("js", "ns")
		js.Spec.Telemetry.Loki.URL = "http://loki:3100"
		js.Spec.Telemetry.Backpressure.QueueDepth = 20000

		c := createTelemetryDeployment(js, "").Spec.Template.Spec.Containers[0]
		Expect(c.Args).To(ContainElement("-loki-queue-depth=20000"))
	})

	It("wires Loki credentials from spec.telemetry.loki.secretRef", func() {
		js := phase3TelemetryJS("js", "ns")
		js.Spec.Telemetry.Loki.URL = "https://loki:3100/loki/api/v1/push"
		js.Spec.Telemetry.Loki.SecretRef = "loki-credentials"

		c := createTelemetryDeployment(js, "").Spec.Template.Spec.Containers[0]
		Expect(envSecretRef(c, "LOKI_USERNAME")).To(Equal(secretKeyRef{"loki-credentials", "username"}))
		Expect(envSecretRef(c, "LOKI_PASSWORD")).To(Equal(secretKeyRef{"loki-credentials", "password"}))
		Expect(envSecretRef(c, "LOKI_TOKEN")).To(Equal(secretKeyRef{"loki-credentials", "token"}))
	})

	It("mounts loki.tls.caSecretRef and passes -loki-ca-file", func() {
		js := phase3TelemetryJS("js", "ns")
		js.Spec.Telemetry.Loki.URL = "https://loki:3100/loki/api/v1/push"
		js.Spec.Telemetry.Loki.TLS.CASecretRef = "loki-ca-bundle"

		dep := createTelemetryDeployment(js, "")
		c := dep.Spec.Template.Spec.Containers[0]
		Expect(c.Args).To(ContainElement(jepLokiCAFileFlag))

		var mount *corev1.VolumeMount
		for i := range c.VolumeMounts {
			if c.VolumeMounts[i].Name == "loki-ca" {
				mount = &c.VolumeMounts[i]
			}
		}
		Expect(mount).NotTo(BeNil(), "expected loki-ca volume mount")
		Expect(mount.MountPath).To(Equal("/loki-ca"))
		Expect(mount.ReadOnly).To(BeTrue())

		var vol *corev1.Volume
		for i := range dep.Spec.Template.Spec.Volumes {
			if dep.Spec.Template.Spec.Volumes[i].Name == "loki-ca" {
				vol = &dep.Spec.Template.Spec.Volumes[i]
			}
		}
		Expect(vol).NotTo(BeNil())
		Expect(vol.Secret).NotTo(BeNil())
		Expect(vol.Secret.SecretName).To(Equal("loki-ca-bundle"))
	})

	It("passes -loki-insecure-skip-verify when tls.insecureSkipVerify is set", func() {
		js := phase3TelemetryJS("js", "ns")
		js.Spec.Telemetry.Loki.URL = "https://loki:3100/loki/api/v1/push"
		js.Spec.Telemetry.Loki.TLS.InsecureSkipVerify = true

		c := createTelemetryDeployment(js, "").Spec.Template.Spec.Containers[0]
		Expect(c.Args).To(ContainElement("-loki-insecure-skip-verify=true"))
	})
})

type secretKeyRef struct {
	name string
	key  string
}

func envSecretRef(c corev1.Container, name string) secretKeyRef {
	for _, e := range c.Env {
		if e.Name == name && e.ValueFrom != nil && e.ValueFrom.SecretKeyRef != nil {
			return secretKeyRef{
				name: e.ValueFrom.SecretKeyRef.Name,
				key:  e.ValueFrom.SecretKeyRef.Key,
			}
		}
	}
	return secretKeyRef{}
}
