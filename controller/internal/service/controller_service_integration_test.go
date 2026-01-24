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
	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"

	jumpstarterdevv1alpha1 "github.com/jumpstarter-dev/jumpstarter-controller/api/v1alpha1"
)

var _ = Describe("ControllerService Integration", func() {
	const (
		testNamespace = "default"
	)

	Context("handleExporterLeaseRelease", func() {
		var (
			controllerService *ControllerService
			exporter          *jumpstarterdevv1alpha1.Exporter
			lease             *jumpstarterdevv1alpha1.Lease
		)

		BeforeEach(func() {
			controllerService = &ControllerService{
				Client: k8sClient,
			}
		})

		AfterEach(func() {
			// Clean up resources
			if exporter != nil {
				_ = k8sClient.Delete(ctx, exporter)
			}
			if lease != nil {
				_ = k8sClient.Delete(ctx, lease)
			}
		})

		It("should return nil when exporter has no active lease", func() {
			exporter = &jumpstarterdevv1alpha1.Exporter{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "exporter-no-lease",
					Namespace: testNamespace,
				},
				Status: jumpstarterdevv1alpha1.ExporterStatus{
					LeaseRef: nil, // No active lease
				},
			}
			Expect(k8sClient.Create(ctx, exporter)).To(Succeed())

			err := controllerService.handleExporterLeaseRelease(ctx, exporter)
			Expect(err).NotTo(HaveOccurred())
		})

		It("should mark lease for release when exporter has active lease", func() {
			// Create lease first
			lease = &jumpstarterdevv1alpha1.Lease{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "lease-to-release",
					Namespace: testNamespace,
				},
				Spec: jumpstarterdevv1alpha1.LeaseSpec{
					ClientRef: corev1.LocalObjectReference{Name: "test-client"},
					Release:   false,
				},
			}
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())

			// Update lease status to have ExporterRef
			lease.Status = jumpstarterdevv1alpha1.LeaseStatus{
				ExporterRef: &corev1.LocalObjectReference{Name: "exporter-with-lease"},
				Ended:       false,
			}
			Expect(k8sClient.Status().Update(ctx, lease)).To(Succeed())

			// Create exporter with lease reference
			exporter = &jumpstarterdevv1alpha1.Exporter{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "exporter-with-lease",
					Namespace: testNamespace,
				},
				Status: jumpstarterdevv1alpha1.ExporterStatus{
					LeaseRef: &corev1.LocalObjectReference{Name: "lease-to-release"},
				},
			}
			Expect(k8sClient.Create(ctx, exporter)).To(Succeed())
			Expect(k8sClient.Status().Update(ctx, exporter)).To(Succeed())

			err := controllerService.handleExporterLeaseRelease(ctx, exporter)
			Expect(err).NotTo(HaveOccurred())

			// Verify lease is marked for release
			var updatedLease jumpstarterdevv1alpha1.Lease
			Expect(k8sClient.Get(ctx, types.NamespacedName{
				Namespace: testNamespace,
				Name:      "lease-to-release",
			}, &updatedLease)).To(Succeed())
			Expect(updatedLease.Spec.Release).To(BeTrue())
		})

		It("should return nil when lease is already marked for release", func() {
			// Create lease that's already marked for release
			lease = &jumpstarterdevv1alpha1.Lease{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "lease-already-releasing",
					Namespace: testNamespace,
				},
				Spec: jumpstarterdevv1alpha1.LeaseSpec{
					ClientRef: corev1.LocalObjectReference{Name: "test-client"},
					Release:   true, // Already marked for release
				},
			}
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())

			// Update lease status
			lease.Status = jumpstarterdevv1alpha1.LeaseStatus{
				ExporterRef: &corev1.LocalObjectReference{Name: "exporter-lease-releasing"},
				Ended:       false,
			}
			Expect(k8sClient.Status().Update(ctx, lease)).To(Succeed())

			// Create exporter
			exporter = &jumpstarterdevv1alpha1.Exporter{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "exporter-lease-releasing",
					Namespace: testNamespace,
				},
				Status: jumpstarterdevv1alpha1.ExporterStatus{
					LeaseRef: &corev1.LocalObjectReference{Name: "lease-already-releasing"},
				},
			}
			Expect(k8sClient.Create(ctx, exporter)).To(Succeed())
			Expect(k8sClient.Status().Update(ctx, exporter)).To(Succeed())

			err := controllerService.handleExporterLeaseRelease(ctx, exporter)
			Expect(err).NotTo(HaveOccurred())
		})

		It("should return nil when lease is already ended", func() {
			// Create lease that's already ended
			lease = &jumpstarterdevv1alpha1.Lease{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "lease-already-ended",
					Namespace: testNamespace,
				},
				Spec: jumpstarterdevv1alpha1.LeaseSpec{
					ClientRef: corev1.LocalObjectReference{Name: "test-client"},
					Release:   false,
				},
			}
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())

			// Update lease status to ended
			lease.Status = jumpstarterdevv1alpha1.LeaseStatus{
				ExporterRef: &corev1.LocalObjectReference{Name: "exporter-lease-ended"},
				Ended:       true, // Already ended
			}
			Expect(k8sClient.Status().Update(ctx, lease)).To(Succeed())

			// Create exporter
			exporter = &jumpstarterdevv1alpha1.Exporter{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "exporter-lease-ended",
					Namespace: testNamespace,
				},
				Status: jumpstarterdevv1alpha1.ExporterStatus{
					LeaseRef: &corev1.LocalObjectReference{Name: "lease-already-ended"},
				},
			}
			Expect(k8sClient.Create(ctx, exporter)).To(Succeed())
			Expect(k8sClient.Status().Update(ctx, exporter)).To(Succeed())

			err := controllerService.handleExporterLeaseRelease(ctx, exporter)
			Expect(err).NotTo(HaveOccurred())
		})

		It("should return error when lease is not held by the exporter", func() {
			// Create lease held by a different exporter
			lease = &jumpstarterdevv1alpha1.Lease{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "lease-wrong-owner",
					Namespace: testNamespace,
				},
				Spec: jumpstarterdevv1alpha1.LeaseSpec{
					ClientRef: corev1.LocalObjectReference{Name: "test-client"},
				},
			}
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())

			// Lease is held by a different exporter
			lease.Status = jumpstarterdevv1alpha1.LeaseStatus{
				ExporterRef: &corev1.LocalObjectReference{Name: "different-exporter"},
				Ended:       false,
			}
			Expect(k8sClient.Status().Update(ctx, lease)).To(Succeed())

			// Create exporter that thinks it has the lease
			exporter = &jumpstarterdevv1alpha1.Exporter{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "exporter-wrong-owner",
					Namespace: testNamespace,
				},
				Status: jumpstarterdevv1alpha1.ExporterStatus{
					LeaseRef: &corev1.LocalObjectReference{Name: "lease-wrong-owner"},
				},
			}
			Expect(k8sClient.Create(ctx, exporter)).To(Succeed())
			Expect(k8sClient.Status().Update(ctx, exporter)).To(Succeed())

			err := controllerService.handleExporterLeaseRelease(ctx, exporter)
			Expect(err).To(HaveOccurred())
			Expect(err.Error()).To(ContainSubstring("not held by exporter"))
		})

		It("should return error when lease ExporterRef is nil", func() {
			// Create lease with no exporter reference
			lease = &jumpstarterdevv1alpha1.Lease{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "lease-no-exporter-ref",
					Namespace: testNamespace,
				},
				Spec: jumpstarterdevv1alpha1.LeaseSpec{
					ClientRef: corev1.LocalObjectReference{Name: "test-client"},
				},
			}
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())

			// Lease has no ExporterRef set (nil)
			lease.Status = jumpstarterdevv1alpha1.LeaseStatus{
				ExporterRef: nil,
				Ended:       false,
			}
			Expect(k8sClient.Status().Update(ctx, lease)).To(Succeed())

			// Create exporter that thinks it has the lease
			exporter = &jumpstarterdevv1alpha1.Exporter{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "exporter-nil-ref",
					Namespace: testNamespace,
				},
				Status: jumpstarterdevv1alpha1.ExporterStatus{
					LeaseRef: &corev1.LocalObjectReference{Name: "lease-no-exporter-ref"},
				},
			}
			Expect(k8sClient.Create(ctx, exporter)).To(Succeed())
			Expect(k8sClient.Status().Update(ctx, exporter)).To(Succeed())

			err := controllerService.handleExporterLeaseRelease(ctx, exporter)
			Expect(err).To(HaveOccurred())
			Expect(err.Error()).To(ContainSubstring("not held by exporter"))
		})
	})
})
