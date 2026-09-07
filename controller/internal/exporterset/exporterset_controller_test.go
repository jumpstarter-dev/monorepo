/*
Copyright 2026 The Jumpstarter Authors

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

package exporterset

import (
	"time"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	jumpstarterdevv1alpha1 "github.com/jumpstarter-dev/jumpstarter/controller/api/v1alpha1"
	virtualtargetv1alpha1 "github.com/jumpstarter-dev/jumpstarter/controller/api/virtualtarget/v1alpha1"
	"github.com/jumpstarter-dev/jumpstarter/controller/internal/exporterset/provisioners/qemu"
)

var _ = Describe("ExporterSet Controller", func() {
	const (
		timeout      = 10 * time.Second
		interval     = 250 * time.Millisecond
		testEndpoint = "grpc.test.example.com:8082"
	)

	var (
		reconciler *ExporterSetReconciler
		vtc        *virtualtargetv1alpha1.VirtualTargetClass
		ns         string
	)

	BeforeEach(func() {
		ns = "default"

		reconciler = &ExporterSetReconciler{
			Client:      envTestClient,
			Scheme:      envTestClient.Scheme(),
			Provisioner: qemu.New("dev"),
		}

		vtc = &virtualtargetv1alpha1.VirtualTargetClass{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "qemu-class",
				Namespace: ns,
			},
			Spec: virtualtargetv1alpha1.VirtualTargetClassSpec{
				Provisioner: qemu.ProvisionerName,
			},
		}
		Expect(envTestClient.Create(envTestCtx, vtc)).To(Succeed())
	})

	AfterEach(func() {
		Expect(envTestClient.Delete(envTestCtx, vtc)).To(Succeed())
	})

	Context("When creating an ExporterSet with minReplicas", func() {
		It("should create Exporters (phase 1) and Pods after credentials (phase 2)", func() {
			es := &virtualtargetv1alpha1.ExporterSet{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-scale-up",
					Namespace: ns,
				},
				Spec: virtualtargetv1alpha1.ExporterSetSpec{
					MinReplicas:            2,
					MaxReplicas:            5,
					MinAvailableReplicas:   1,
					VirtualTargetClassName: "qemu-class",
					Selector: metav1.LabelSelector{
						MatchLabels: map[string]string{"exporterset": "test-scale-up"},
					},
					Template: virtualtargetv1alpha1.ExporterSetTemplate{
						Metadata: virtualtargetv1alpha1.EmbeddedObjectMeta{
							Labels: map[string]string{"exporterset": "test-scale-up"},
						},
					},
				},
			}
			Expect(envTestClient.Create(envTestCtx, es)).To(Succeed())

			defer func() {
				Expect(envTestClient.Delete(envTestCtx, es)).To(Succeed())
			}()

			By("Phase 1: Reconciling creates Exporters (awaiting credentials)")
			req := reconcile.Request{NamespacedName: types.NamespacedName{Name: es.Name, Namespace: ns}}
			for range 2 {
				_, err := reconciler.Reconcile(envTestCtx, req)
				Expect(err).NotTo(HaveOccurred())
			}

			By("Verifying Exporters were created")
			var exporterList jumpstarterdevv1alpha1.ExporterList
			Eventually(func() int {
				err := envTestClient.List(envTestCtx, &exporterList,
					client.InNamespace(ns),
					client.MatchingLabels{"exporterset": "test-scale-up"},
				)
				if err != nil {
					return 0
				}
				return len(exporterList.Items)
			}, timeout, interval).Should(Equal(2))

			By("Verifying ownership chain: Exporter owned by ExporterSet")
			for _, exp := range exporterList.Items {
				ownerFound := false
				for _, ref := range exp.OwnerReferences {
					if ref.Kind == "ExporterSet" && ref.Name == es.Name &&
						ref.Controller != nil && *ref.Controller {
						ownerFound = true
					}
				}
				Expect(ownerFound).To(BeTrue(),
					"Exporter %s should be controller-owned by ExporterSet", exp.Name)
			}

			By("Verifying no Pods exist yet (credentials not ready)")
			var podList corev1.PodList
			Expect(envTestClient.List(envTestCtx, &podList,
				client.InNamespace(ns),
				client.MatchingLabels{labelExporterSetName: "test-scale-up"},
			)).To(Succeed())
			Expect(podList.Items).To(BeEmpty(),
				"No Pods should exist before credentials are provisioned")

			By("Phase 2: Simulating credentials and endpoint on Exporters")
			caCM := &corev1.ConfigMap{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "jumpstarter-service-ca-cert",
					Namespace: ns,
				},
				Data: map[string]string{
					"ca.crt": "-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n",
				},
			}
			Expect(envTestClient.Create(envTestCtx, caCM)).To(Succeed())
			defer func() { _ = envTestClient.Delete(envTestCtx, caCM) }()

			Expect(envTestClient.List(envTestCtx, &exporterList,
				client.InNamespace(ns),
				client.MatchingLabels{"exporterset": "test-scale-up"},
			)).To(Succeed())

			for i := range exporterList.Items {
				exp := &exporterList.Items[i]
				credSecret := &corev1.Secret{
					ObjectMeta: metav1.ObjectMeta{
						Name:      "cred-" + exp.Name,
						Namespace: ns,
					},
					Data: map[string][]byte{
						"token": []byte("test-token-" + exp.Name),
					},
				}
				Expect(envTestClient.Create(envTestCtx, credSecret)).To(Succeed())

				exp.Status.Credential = &corev1.LocalObjectReference{Name: credSecret.Name}
				exp.Status.Endpoint = testEndpoint
				Expect(envTestClient.Status().Update(envTestCtx, exp)).To(Succeed())
			}

			By("Reconciling again — should create config Secrets and Pods")
			_, err := reconciler.Reconcile(envTestCtx, req)
			Expect(err).NotTo(HaveOccurred())

			By("Verifying Pods were created with correct ownership")
			Eventually(func() int {
				err := envTestClient.List(envTestCtx, &podList,
					client.InNamespace(ns),
					client.MatchingLabels{labelExporterSetName: "test-scale-up"},
				)
				if err != nil {
					return 0
				}
				return len(podList.Items)
			}, timeout, interval).Should(Equal(2))

			exporterUIDs := make(map[types.UID]bool)
			for _, exp := range exporterList.Items {
				exporterUIDs[exp.UID] = true
			}

			for _, pod := range podList.Items {
				ownedByExporter := false
				ownedByES := false
				for _, ref := range pod.OwnerReferences {
					if ref.Kind == "Exporter" && ref.Controller != nil && *ref.Controller {
						Expect(exporterUIDs).To(HaveKey(ref.UID))
						ownedByExporter = true
					}
					if ref.Kind == "ExporterSet" {
						ownedByES = true
					}
				}
				Expect(ownedByExporter).To(BeTrue(),
					"Pod %s should be controller-owned by an Exporter", pod.Name)
				Expect(ownedByES).To(BeFalse(),
					"Pod %s should NOT be directly owned by ExporterSet", pod.Name)
			}

			By("Verifying Pod labels include ExporterSet name for watch mapping")
			for _, pod := range podList.Items {
				Expect(pod.Labels[labelExporterSetName]).To(Equal("test-scale-up"))
			}

			By("Verifying config Secrets were created")
			var secretList corev1.SecretList
			Expect(envTestClient.List(envTestCtx, &secretList,
				client.InNamespace(ns),
				client.MatchingLabels{labelExporterSetName: "test-scale-up"},
			)).To(Succeed())
			Expect(secretList.Items).To(HaveLen(2))
		})
	})

	Context("When an ExporterSet has excess idle replicas", func() {
		It("should disable one exporter after cooldown and delete it on next reconcile", func() {
			cooldown := 1 * time.Second
			es := &virtualtargetv1alpha1.ExporterSet{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-scale-down",
					Namespace: ns,
				},
				Spec: virtualtargetv1alpha1.ExporterSetSpec{
					MinReplicas:            1,
					MaxReplicas:            10,
					MinAvailableReplicas:   1,
					ScaleDownCooldown:      &metav1.Duration{Duration: cooldown},
					VirtualTargetClassName: "qemu-class",
					Selector: metav1.LabelSelector{
						MatchLabels: map[string]string{"exporterset": "test-scale-down"},
					},
					Template: virtualtargetv1alpha1.ExporterSetTemplate{
						Metadata: virtualtargetv1alpha1.EmbeddedObjectMeta{
							Labels: map[string]string{"exporterset": "test-scale-down"},
						},
					},
				},
			}
			Expect(envTestClient.Create(envTestCtx, es)).To(Succeed())

			defer func() {
				_ = envTestClient.Delete(envTestCtx, es)
			}()

			By("Creating 3 exporters (surplus of 1 over minAvailableReplicas)")
			for _, name := range []string{"sd-1", "sd-2", "sd-3"} {
				exp := &jumpstarterdevv1alpha1.Exporter{
					ObjectMeta: metav1.ObjectMeta{
						Name:      name,
						Namespace: ns,
						Labels:    map[string]string{"exporterset": "test-scale-down"},
					},
					Spec: jumpstarterdevv1alpha1.ExporterSpec{
						Enabled: new(true),
					},
				}
				Expect(envTestClient.Create(envTestCtx, exp)).To(Succeed())

				// Set ownership to ExporterSet
				exp.OwnerReferences = []metav1.OwnerReference{{
					APIVersion: "virtualtarget.jumpstarter.dev/v1alpha1",
					Kind:       "ExporterSet",
					Name:       es.Name,
					UID:        es.UID,
					Controller: new(true),
				}}
				Expect(envTestClient.Update(envTestCtx, exp)).To(Succeed())

				// Set Online condition
				exp.Status.Conditions = []metav1.Condition{{
					Type:               string(jumpstarterdevv1alpha1.ExporterConditionTypeOnline),
					Status:             metav1.ConditionTrue,
					LastTransitionTime: metav1.Now(),
					Reason:             "Online",
				}}
				Expect(envTestClient.Status().Update(envTestCtx, exp)).To(Succeed())
			}

			By("First reconcile: sets surplus annotation and requeues")
			result, err := reconciler.Reconcile(envTestCtx, reconcile.Request{
				NamespacedName: types.NamespacedName{Name: es.Name, Namespace: ns},
			})
			Expect(err).NotTo(HaveOccurred())
			Expect(result.RequeueAfter).To(BeNumerically(">", 0))

			By("Waiting for cooldown to elapse")
			time.Sleep(cooldown + 500*time.Millisecond)

			By("Second reconcile: disables one exporter")
			result, err = reconciler.Reconcile(envTestCtx, reconcile.Request{
				NamespacedName: types.NamespacedName{Name: es.Name, Namespace: ns},
			})
			Expect(err).NotTo(HaveOccurred())
			// Controller disables one exporter and returns RequeueAfter: cooldown.
			Expect(result.RequeueAfter).To(BeNumerically(">", 0))

			By("Verifying one exporter is disabled")
			var exporterList jumpstarterdevv1alpha1.ExporterList
			Expect(envTestClient.List(envTestCtx, &exporterList,
				client.InNamespace(ns),
				client.MatchingLabels{"exporterset": "test-scale-down"},
			)).To(Succeed())

			disabledCount := 0
			for _, exp := range exporterList.Items {
				if !exp.IsEnabled() {
					disabledCount++
				}
			}
			Expect(disabledCount).To(Equal(1))

			By("Third reconcile: deletes the disabled, unleased exporter")
			result, err = reconciler.Reconcile(envTestCtx, reconcile.Request{
				NamespacedName: types.NamespacedName{Name: es.Name, Namespace: ns},
			})
			Expect(err).NotTo(HaveOccurred())

			Expect(envTestClient.List(envTestCtx, &exporterList,
				client.InNamespace(ns),
				client.MatchingLabels{"exporterset": "test-scale-down"},
			)).To(Succeed())
			Expect(exporterList.Items).To(HaveLen(2))

			for _, exp := range exporterList.Items {
				Expect(exp.IsEnabled()).To(BeTrue(),
					"remaining exporters should all be enabled")
			}
		})
	})

	Context("When reconciling is idempotent", func() {
		It("should not create extra resources on repeated reconciles", func() {
			es := &virtualtargetv1alpha1.ExporterSet{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-idempotent",
					Namespace: ns,
				},
				Spec: virtualtargetv1alpha1.ExporterSetSpec{
					MinReplicas:            2,
					MaxReplicas:            5,
					MinAvailableReplicas:   1,
					VirtualTargetClassName: "qemu-class",
					Selector: metav1.LabelSelector{
						MatchLabels: map[string]string{"exporterset": "test-idempotent"},
					},
					Template: virtualtargetv1alpha1.ExporterSetTemplate{
						Metadata: virtualtargetv1alpha1.EmbeddedObjectMeta{
							Labels: map[string]string{"exporterset": "test-idempotent"},
						},
					},
				},
			}
			Expect(envTestClient.Create(envTestCtx, es)).To(Succeed())

			defer func() {
				Expect(envTestClient.Delete(envTestCtx, es)).To(Succeed())
			}()

			By("First two reconciles: each creates 1 Exporter (one-per-reconcile policy)")
			req := reconcile.Request{NamespacedName: types.NamespacedName{Name: es.Name, Namespace: ns}}
			for range 2 {
				_, err := reconciler.Reconcile(envTestCtx, req)
				Expect(err).NotTo(HaveOccurred())
			}

			var exporterList jumpstarterdevv1alpha1.ExporterList
			Expect(envTestClient.List(envTestCtx, &exporterList,
				client.InNamespace(ns),
				client.MatchingLabels{"exporterset": "test-idempotent"},
			)).To(Succeed())
			firstCount := len(exporterList.Items)
			Expect(firstCount).To(Equal(2))

			By("Simulating exporters coming online")
			for i := range exporterList.Items {
				exp := &exporterList.Items[i]
				exp.Status.Conditions = []metav1.Condition{{
					Type:               string(jumpstarterdevv1alpha1.ExporterConditionTypeOnline),
					Status:             metav1.ConditionTrue,
					LastTransitionTime: metav1.Now(),
					Reason:             "Online",
				}}
				Expect(envTestClient.Status().Update(envTestCtx, exp)).To(Succeed())
			}

			By("Third reconcile: no additional resources")
			_, err := reconciler.Reconcile(envTestCtx, reconcile.Request{
				NamespacedName: types.NamespacedName{Name: es.Name, Namespace: ns},
			})
			Expect(err).NotTo(HaveOccurred())

			Expect(envTestClient.List(envTestCtx, &exporterList,
				client.InNamespace(ns),
				client.MatchingLabels{"exporterset": "test-idempotent"},
			)).To(Succeed())
			Expect(exporterList.Items).To(HaveLen(firstCount))
		})
	})
})
