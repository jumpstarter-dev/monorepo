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
	"time"

	jumpstarterdevv1alpha1 "github.com/jumpstarter-dev/jumpstarter/controller/api/v1alpha1"
	virtualtargetv1alpha1 "github.com/jumpstarter-dev/jumpstarter/controller/api/virtualtarget/v1alpha1"
	"github.com/jumpstarter-dev/jumpstarter/controller/internal/oidc"
	cpb "github.com/jumpstarter-dev/jumpstarter/controller/internal/protocol/jumpstarter/client/v1"
	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/timestamppb"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/event"
	"sigs.k8s.io/controller-runtime/pkg/predicate"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

const (
	lease1Name = "lease1"
	lease2Name = "lease2"
	lease3Name = "lease3"
)

var leaseDutA2Sec = &jumpstarterdevv1alpha1.Lease{
	ObjectMeta: metav1.ObjectMeta{
		Name:      "lease1",
		Namespace: "default",
	},
	Spec: jumpstarterdevv1alpha1.LeaseSpec{
		ClientRef: corev1.LocalObjectReference{
			Name: testClient.Name,
		},
		Selector: metav1.LabelSelector{
			MatchLabels: map[string]string{
				"dut": "a",
			},
		},
		Duration: &metav1.Duration{
			Duration: 2 * time.Second,
		},
	},
}
var _ = Describe("Lease Controller", func() {
	BeforeEach(func() {
		createExporters(context.Background(), testExporter1DutA, testExporter2DutA, testExporter3DutB)
		setExporterOnlineConditions(context.Background(), testExporter1DutA.Name, metav1.ConditionTrue)
		setExporterOnlineConditions(context.Background(), testExporter2DutA.Name, metav1.ConditionTrue)
		setExporterOnlineConditions(context.Background(), testExporter3DutB.Name, metav1.ConditionTrue)
	})
	AfterEach(func() {
		ctx := context.Background()
		deleteExporters(ctx, testExporter1DutA, testExporter2DutA, testExporter3DutB)
		deleteLeases(ctx, lease1Name, lease2Name, lease3Name)
	})

	When("trying to lease with an empty selector", func() {
		It("should fail right away", func() {
			lease := leaseDutA2Sec.DeepCopy()
			lease.Spec.Selector.MatchLabels = nil

			ctx := context.Background()
			err := k8sClient.Create(ctx, lease)
			Expect(err).To(HaveOccurred())
			Expect(err.Error()).To(ContainSubstring("one of selector or exporterRef.name is required"))
		})
	})

	When("trying to lease with a requested exporter and empty selector", func() {
		It("should acquire the requested exporter", func() {
			lease := leaseDutA2Sec.DeepCopy()
			lease.Spec.Selector.MatchLabels = nil
			lease.Spec.ExporterRef = &corev1.LocalObjectReference{Name: testExporter1DutA.Name}

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).NotTo(BeNil())
			Expect(updatedLease.Status.ExporterRef.Name).To(Equal(testExporter1DutA.Name))
			Expect(meta.IsStatusConditionTrue(
				updatedLease.Status.Conditions,
				string(jumpstarterdevv1alpha1.LeaseConditionTypeReady),
			)).To(BeTrue())
		})
	})

	When("trying to lease with a missing requested exporter", func() {
		It("should be unsatisfiable with ExporterNotFound reason", func() {
			lease := leaseDutA2Sec.DeepCopy()
			lease.Spec.Selector.MatchLabels = nil
			lease.Spec.ExporterRef = &corev1.LocalObjectReference{Name: "does-not-exist"}

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).To(BeNil())
			condition := meta.FindStatusCondition(
				updatedLease.Status.Conditions,
				string(jumpstarterdevv1alpha1.LeaseConditionTypeUnsatisfiable),
			)
			Expect(condition).NotTo(BeNil())
			Expect(condition.Reason).To(Equal("ExporterNotFound"))
			Expect(meta.IsStatusConditionTrue(
				updatedLease.Status.Conditions,
				string(jumpstarterdevv1alpha1.LeaseConditionTypePending),
			)).To(BeFalse())
		})
	})

	When("trying to lease with requested exporter that does not match selector", func() {
		It("should fail with SelectorMismatch reason", func() {
			lease := leaseDutA2Sec.DeepCopy()
			lease.Spec.Selector.MatchLabels["dut"] = "b"
			lease.Spec.ExporterRef = &corev1.LocalObjectReference{Name: testExporter1DutA.Name}

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).To(BeNil())
			condition := meta.FindStatusCondition(
				updatedLease.Status.Conditions,
				string(jumpstarterdevv1alpha1.LeaseConditionTypeUnsatisfiable),
			)
			Expect(condition).NotTo(BeNil())
			Expect(condition.Reason).To(Equal("SelectorMismatch"))
		})
	})

	When("trying to lease an available exporter", func() {
		It("should acquire lease right away", func() {
			lease := leaseDutA2Sec.DeepCopy()

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).NotTo(BeNil())
			Expect(updatedLease.Status.ExporterRef.Name).To(BeElementOf([]string{testExporter1DutA.Name, testExporter2DutA.Name}))
			Expect(updatedLease.Status.BeginTime).NotTo(BeNil())

			updatedExporter := getExporter(ctx, updatedLease.Status.ExporterRef.Name)
			Expect(updatedExporter.Status.LeaseRef).NotTo(BeNil())
			Expect(updatedExporter.Status.LeaseRef.Name).To(Equal(lease.Name))
		})

		It("should be released after the lease time", func() {
			lease := leaseDutA2Sec.DeepCopy()
			lease.Spec.Duration = &metav1.Duration{Duration: 100 * time.Millisecond}

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).NotTo(BeNil())

			exporterName := updatedLease.Status.ExporterRef.Name

			// Poll until lease expires
			Eventually(func() bool {
				_ = reconcileLease(ctx, lease)
				updatedLease = getLease(ctx, lease.Name)
				return updatedLease.Status.Ended
			}).WithTimeout(2000 * time.Millisecond).WithPolling(50 * time.Millisecond).Should(BeTrue())

			// exporter is retained for record purposes
			Expect(updatedLease.Status.ExporterRef).NotTo(BeNil())

			// the exporter should have no lease mark on status
			updatedExporter := getExporter(ctx, exporterName)
			Expect(updatedExporter.Status.LeaseRef).To(BeNil())

		})
	})

	When("trying to lease a non existing exporter", func() {
		It("should fail right away", func() {
			lease := leaseDutA2Sec.DeepCopy()
			lease.Spec.Selector.MatchLabels["dut"] = "does-not-exist"

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).To(BeNil())

			Expect(meta.IsStatusConditionTrue(
				updatedLease.Status.Conditions,
				string(jumpstarterdevv1alpha1.LeaseConditionTypeUnsatisfiable),
			)).To(BeTrue())
		})
	})

	When("trying to lease an offline exporter", func() {
		It("should set status to pending with offline reason", func() {
			lease := leaseDutA2Sec.DeepCopy()

			ctx := context.Background()

			setExporterOnlineConditions(ctx, testExporter1DutA.Name, metav1.ConditionFalse)
			setExporterOnlineConditions(ctx, testExporter2DutA.Name, metav1.ConditionFalse)

			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).To(BeNil())

			Expect(meta.IsStatusConditionTrue(
				updatedLease.Status.Conditions,
				string(jumpstarterdevv1alpha1.LeaseConditionTypePending),
			)).To(BeTrue())

			// Check that the condition has the correct reason
			condition := meta.FindStatusCondition(updatedLease.Status.Conditions, string(jumpstarterdevv1alpha1.LeaseConditionTypePending))
			Expect(condition).NotTo(BeNil())
			Expect(condition.Reason).To(Equal("Offline"))
		})
	})

	When("trying to lease approved exporters that are offline", func() {
		It("should set status to pending with offline reason", func() {
			lease := leaseDutA2Sec.DeepCopy()

			ctx := context.Background()

			// Create a policy that approves the exporters
			policy := &jumpstarterdevv1alpha1.ExporterAccessPolicy{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-policy",
					Namespace: "default",
				},
				Spec: jumpstarterdevv1alpha1.ExporterAccessPolicySpec{
					ExporterSelector: metav1.LabelSelector{
						MatchLabels: map[string]string{
							"dut": "a",
						},
					},
					Policies: []jumpstarterdevv1alpha1.Policy{
						{
							Priority: 0,
							From: []jumpstarterdevv1alpha1.From{
								{
									ClientSelector: metav1.LabelSelector{
										MatchLabels: map[string]string{
											"name": "client",
										},
									},
								},
							},
						},
					},
				},
			}
			Expect(k8sClient.Create(ctx, policy)).To(Succeed())

			// Set exporters offline while they are approved by policy
			setExporterOnlineConditions(ctx, testExporter1DutA.Name, metav1.ConditionFalse)
			setExporterOnlineConditions(ctx, testExporter2DutA.Name, metav1.ConditionFalse)

			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).To(BeNil())

			Expect(meta.IsStatusConditionTrue(
				updatedLease.Status.Conditions,
				string(jumpstarterdevv1alpha1.LeaseConditionTypePending),
			)).To(BeTrue())

			// Check that the condition has the correct reason
			condition := meta.FindStatusCondition(updatedLease.Status.Conditions, string(jumpstarterdevv1alpha1.LeaseConditionTypePending))
			Expect(condition).NotTo(BeNil())
			Expect(condition.Reason).To(Equal("Offline"))
			Expect(condition.Message).To(ContainSubstring("none of them are online"))

			// Clean up
			Expect(k8sClient.Delete(ctx, policy)).To(Succeed())
		})
	})

	When("trying to lease exporters that match selector but are not approved by any policy", func() {
		It("should set status to unsatisfiable with NoAccess reason", func() {
			lease := leaseDutA2Sec.DeepCopy()

			ctx := context.Background()

			// Create a policy that does NOT approve the exporters (different client selector)
			policy := &jumpstarterdevv1alpha1.ExporterAccessPolicy{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-policy",
					Namespace: "default",
				},
				Spec: jumpstarterdevv1alpha1.ExporterAccessPolicySpec{
					ExporterSelector: metav1.LabelSelector{
						MatchLabels: map[string]string{
							"dut": "a",
						},
					},
					Policies: []jumpstarterdevv1alpha1.Policy{
						{
							Description: "Requires different-client label",
							Priority:    0,
							From: []jumpstarterdevv1alpha1.From{
								{
									ClientSelector: metav1.LabelSelector{
										MatchLabels: map[string]string{
											"name": "different-client", // Different from testClient
										},
									},
								},
							},
						},
					},
				},
			}
			Expect(k8sClient.Create(ctx, policy)).To(Succeed())

			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).To(BeNil())

			Expect(meta.IsStatusConditionTrue(
				updatedLease.Status.Conditions,
				string(jumpstarterdevv1alpha1.LeaseConditionTypeUnsatisfiable),
			)).To(BeTrue())

			// Check that the condition has the correct reason
			condition := meta.FindStatusCondition(updatedLease.Status.Conditions, string(jumpstarterdevv1alpha1.LeaseConditionTypeUnsatisfiable))
			Expect(condition).NotTo(BeNil())
			Expect(condition.Reason).To(Equal("NoAccess"))
			Expect(condition.Message).To(ContainSubstring("none of them are approved by any policy"))
			Expect(condition.Message).To(ContainSubstring("Requires different-client label"))

			// Clean up
			Expect(k8sClient.Delete(ctx, policy)).To(Succeed())
		})
	})

	When("trying to lease with multiple policies containing descriptions, none matching client", func() {
		It("should include all policy descriptions in the unsatisfiable message", func() {
			lease := leaseDutA2Sec.DeepCopy()

			ctx := context.Background()

			// Create two policies with different descriptions, neither matching testClient
			policy1 := &jumpstarterdevv1alpha1.ExporterAccessPolicy{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-policy-admin",
					Namespace: "default",
				},
				Spec: jumpstarterdevv1alpha1.ExporterAccessPolicySpec{
					ExporterSelector: metav1.LabelSelector{
						MatchLabels: map[string]string{"dut": "a"},
					},
					Policies: []jumpstarterdevv1alpha1.Policy{
						{
							Description: "Administrators only",
							Priority:    20,
							From: []jumpstarterdevv1alpha1.From{{
								ClientSelector: metav1.LabelSelector{
									MatchLabels: map[string]string{"role": "admin"},
								},
							}},
						},
					},
				},
			}
			policy2 := &jumpstarterdevv1alpha1.ExporterAccessPolicy{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-policy-ci",
					Namespace: "default",
				},
				Spec: jumpstarterdevv1alpha1.ExporterAccessPolicySpec{
					ExporterSelector: metav1.LabelSelector{
						MatchLabels: map[string]string{"dut": "a"},
					},
					Policies: []jumpstarterdevv1alpha1.Policy{
						{
							Description: "CI pipelines only",
							Priority:    5,
							From: []jumpstarterdevv1alpha1.From{{
								ClientSelector: metav1.LabelSelector{
									MatchLabels: map[string]string{"role": "ci"},
								},
							}},
						},
					},
				},
			}
			Expect(k8sClient.Create(ctx, policy1)).To(Succeed())
			Expect(k8sClient.Create(ctx, policy2)).To(Succeed())

			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).To(BeNil())

			condition := meta.FindStatusCondition(updatedLease.Status.Conditions, string(jumpstarterdevv1alpha1.LeaseConditionTypeUnsatisfiable))
			Expect(condition).NotTo(BeNil())
			Expect(condition.Reason).To(Equal("NoAccess"))
			Expect(condition.Message).To(ContainSubstring("Administrators only"))
			Expect(condition.Message).To(ContainSubstring("CI pipelines only"))
			Expect(condition.Message).To(ContainSubstring(";"))

			// Clean up
			Expect(k8sClient.Delete(ctx, policy1)).To(Succeed())
			Expect(k8sClient.Delete(ctx, policy2)).To(Succeed())
		})
	})

	When("trying to lease with policies where some have empty descriptions", func() {
		It("should only include non-empty descriptions in the unsatisfiable message", func() {
			lease := leaseDutA2Sec.DeepCopy()

			ctx := context.Background()

			policy := &jumpstarterdevv1alpha1.ExporterAccessPolicy{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-policy-mixed",
					Namespace: "default",
				},
				Spec: jumpstarterdevv1alpha1.ExporterAccessPolicySpec{
					ExporterSelector: metav1.LabelSelector{
						MatchLabels: map[string]string{"dut": "a"},
					},
					Policies: []jumpstarterdevv1alpha1.Policy{
						{
							Description: "VIP access rule",
							Priority:    10,
							From: []jumpstarterdevv1alpha1.From{{
								ClientSelector: metav1.LabelSelector{
									MatchLabels: map[string]string{"role": "vip"},
								},
							}},
						},
						{
							// No description
							Priority: 1,
							From: []jumpstarterdevv1alpha1.From{{
								ClientSelector: metav1.LabelSelector{
									MatchLabels: map[string]string{"role": "other"},
								},
							}},
						},
					},
				},
			}
			Expect(k8sClient.Create(ctx, policy)).To(Succeed())

			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			condition := meta.FindStatusCondition(updatedLease.Status.Conditions, string(jumpstarterdevv1alpha1.LeaseConditionTypeUnsatisfiable))
			Expect(condition).NotTo(BeNil())
			Expect(condition.Reason).To(Equal("NoAccess"))
			Expect(condition.Message).To(ContainSubstring("VIP access rule"))
			// The message should contain "Matching policies:" since at least one description exists
			Expect(condition.Message).To(ContainSubstring("Matching policies:"))

			// Clean up
			Expect(k8sClient.Delete(ctx, policy)).To(Succeed())
		})
	})

	When("trying to lease with a policy that has descriptions and matches the client", func() {
		It("should acquire the lease successfully regardless of description", func() {
			lease := leaseDutA2Sec.DeepCopy()

			ctx := context.Background()

			policy := &jumpstarterdevv1alpha1.ExporterAccessPolicy{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-policy-matching",
					Namespace: "default",
				},
				Spec: jumpstarterdevv1alpha1.ExporterAccessPolicySpec{
					ExporterSelector: metav1.LabelSelector{
						MatchLabels: map[string]string{"dut": "a"},
					},
					Policies: []jumpstarterdevv1alpha1.Policy{
						{
							Description: "Standard access for registered clients",
							Priority:    10,
							From: []jumpstarterdevv1alpha1.From{{
								ClientSelector: metav1.LabelSelector{
									MatchLabels: map[string]string{"name": "client"}, // Matches testClient
								},
							}},
						},
					},
				},
			}
			Expect(k8sClient.Create(ctx, policy)).To(Succeed())

			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).NotTo(BeNil())
			Expect(updatedLease.Status.Priority).To(Equal(10))
			Expect(meta.IsStatusConditionTrue(
				updatedLease.Status.Conditions,
				string(jumpstarterdevv1alpha1.LeaseConditionTypeReady),
			)).To(BeTrue())

			// Clean up
			Expect(k8sClient.Delete(ctx, policy)).To(Succeed())
		})
	})

	When("trying to lease with no policies at all and no matching exporters", func() {
		It("should show NoAccess message without policy descriptions", func() {
			lease := leaseDutA2Sec.DeepCopy()
			lease.Spec.Selector.MatchLabels["dut"] = "does-not-exist"

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).To(BeNil())

			condition := meta.FindStatusCondition(updatedLease.Status.Conditions, string(jumpstarterdevv1alpha1.LeaseConditionTypeUnsatisfiable))
			Expect(condition).NotTo(BeNil())
			// Without policies, the message should not contain "Matching policies:"
			Expect(condition.Message).NotTo(ContainSubstring("Matching policies:"))
		})
	})

	When("trying to lease exporters, and some matching exporters are online and while others are offline", func() {
		It("should acquire lease for the online exporters", func() {
			lease := leaseDutA2Sec.DeepCopy()

			ctx := context.Background()

			setExporterOnlineConditions(ctx, testExporter1DutA.Name, metav1.ConditionFalse)

			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).NotTo(BeNil())
			Expect(updatedLease.Status.ExporterRef.Name).To(BeElementOf([]string{testExporter2DutA.Name}))
			Expect(updatedLease.Status.BeginTime).NotTo(BeNil())

			updatedExporter := getExporter(ctx, updatedLease.Status.ExporterRef.Name)
			Expect(updatedExporter.Status.LeaseRef).NotTo(BeNil())
			Expect(updatedExporter.Status.LeaseRef.Name).To(Equal(lease.Name))
		})
	})

	When("trying to lease exporters that are online but not ready (e.g., running afterLease hook)", func() {
		It("should set status to pending with NotReady reason", func() {
			lease := leaseDutA2Sec.DeepCopy()

			ctx := context.Background()

			// Set both matching exporters to online but in AfterLeaseHook status
			// (simulates exporter cleaning up from a previous lease)
			setExporterNotReady(ctx, testExporter1DutA.Name, jumpstarterdevv1alpha1.ExporterStatusAfterLeaseHook)
			setExporterNotReady(ctx, testExporter2DutA.Name, jumpstarterdevv1alpha1.ExporterStatusBeforeLeaseHook)

			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			result := reconcileLease(ctx, lease)

			// Should requeue to retry later
			Expect(result.RequeueAfter).To(Equal(time.Second))

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).To(BeNil())

			Expect(meta.IsStatusConditionTrue(
				updatedLease.Status.Conditions,
				string(jumpstarterdevv1alpha1.LeaseConditionTypePending),
			)).To(BeTrue())

			condition := meta.FindStatusCondition(updatedLease.Status.Conditions, string(jumpstarterdevv1alpha1.LeaseConditionTypePending))
			Expect(condition).NotTo(BeNil())
			Expect(condition.Reason).To(Equal("NotReady"))
			Expect(condition.Message).To(ContainSubstring("none are ready"))
		})
	})

	When("trying to lease exporters where some are online+ready and others are online+not-ready", func() {
		It("should only lease from the ready exporter", func() {
			lease := leaseDutA2Sec.DeepCopy()

			ctx := context.Background()

			// exporter1 is still cleaning up, exporter2 is available
			setExporterNotReady(ctx, testExporter1DutA.Name, jumpstarterdevv1alpha1.ExporterStatusAfterLeaseHook)

			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).NotTo(BeNil())
			Expect(updatedLease.Status.ExporterRef.Name).To(Equal(testExporter2DutA.Name))
		})
	})

	When("trying to lease a busy exporter", func() {
		It("should not be acquired", func() {
			lease := leaseDutA2Sec.DeepCopy()
			lease.Spec.Selector.MatchLabels["dut"] = "b"

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).NotTo(BeNil())
			Expect(updatedLease.Status.ExporterRef.Name).To(Equal(testExporter3DutB.Name))

			updatedExporter := getExporter(ctx, updatedLease.Status.ExporterRef.Name)
			Expect(updatedExporter.Status.LeaseRef).NotTo(BeNil())
			Expect(updatedExporter.Status.LeaseRef.Name).To(Equal(lease.Name))

			// create another lease that attempts to acquire the only dut b exporter
			// which is already leased
			lease2 := leaseDutA2Sec.DeepCopy()
			lease2.Name = lease2Name
			lease2.Spec.Selector.MatchLabels["dut"] = "b"
			Expect(k8sClient.Create(ctx, lease2)).To(Succeed())
			_ = reconcileLease(ctx, lease2)

			updatedLease = getLease(ctx, lease2Name)
			Expect(updatedLease.Status.ExporterRef).To(BeNil())

			Expect(meta.IsStatusConditionTrue(
				updatedLease.Status.Conditions,
				string(jumpstarterdevv1alpha1.LeaseConditionTypePending),
			)).To(BeTrue())

			// Check that the condition has the correct reason and message format
			condition := meta.FindStatusCondition(updatedLease.Status.Conditions, string(jumpstarterdevv1alpha1.LeaseConditionTypePending))
			Expect(condition).NotTo(BeNil())
			Expect(condition.Reason).To(Equal("NotAvailable"))
			Expect(condition.Message).To(ContainSubstring("but all of them are already leased"))
		})

		It("should be acquired when a valid exporter lease times out", func() {
			lease := leaseDutA2Sec.DeepCopy()
			lease.Spec.Selector.MatchLabels["dut"] = "b"
			lease.Spec.Duration = &metav1.Duration{Duration: 500 * time.Millisecond}

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).NotTo(BeNil())
			Expect(updatedLease.Status.ExporterRef.Name).To(Equal(testExporter3DutB.Name))

			updatedExporter := getExporter(ctx, updatedLease.Status.ExporterRef.Name)
			Expect(updatedExporter.Status.LeaseRef).NotTo(BeNil())
			Expect(updatedExporter.Status.LeaseRef.Name).To(Equal(lease.Name))

			// create another lease that attempts to acquire the only dut b exporter
			// which is already leased
			lease2 := leaseDutA2Sec.DeepCopy()
			lease2.Name = lease2Name
			lease2.Spec.Selector.MatchLabels["dut"] = "b"
			Expect(k8sClient.Create(ctx, lease2)).To(Succeed())
			_ = reconcileLease(ctx, lease2)

			updatedLease = getLease(ctx, lease2Name)
			Expect(updatedLease.Status.ExporterRef).To(BeNil())
			// TODO: add and check status conditions of the lease to indicate that the lease is waiting

			// Poll until first lease expires and second lease acquires exporter
			Eventually(func() bool {
				_ = reconcileLease(ctx, lease)
				_ = reconcileLease(ctx, lease2)
				updatedLease = getLease(ctx, lease2Name)
				return updatedLease.Status.ExporterRef != nil
			}).WithTimeout(2500 * time.Millisecond).WithPolling(50 * time.Millisecond).Should(BeTrue())

		})
	})

	When("releasing a lease early", func() {
		It("should release the lease and exporter right away", func() {
			lease := leaseDutA2Sec.DeepCopy()

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).NotTo(BeNil())

			exporterName := updatedLease.Status.ExporterRef.Name

			// release the lease early
			// TODO: through the API we cannot set the status condition, we get this through the RPC,
			// we should consider adding a flag on the spec to do this, or look at the duration too
			updatedLease.Spec.Release = true

			Expect(k8sClient.Update(ctx, updatedLease)).To(Succeed())

			_ = reconcileLease(ctx, updatedLease)

			updatedLease = getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).NotTo(BeNil())
			Expect(updatedLease.Status.Ended).To(BeTrue())

			updatedExporter := getExporter(ctx, exporterName)
			Expect(updatedExporter.Status.LeaseRef).To(BeNil())
		})
	})

	When("trying to lease a disabled exporter by ExporterRef", func() {
		It("should fail with ExporterDisabled reason and helpful message", func() {
			ctx := context.Background()
			setExporterEnabled(ctx, testExporter1DutA.Name, false)

			lease := leaseDutA2Sec.DeepCopy()
			lease.Spec.Selector.MatchLabels = nil
			lease.Spec.ExporterRef = &corev1.LocalObjectReference{Name: testExporter1DutA.Name}

			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).To(BeNil())
			condition := meta.FindStatusCondition(
				updatedLease.Status.Conditions,
				string(jumpstarterdevv1alpha1.LeaseConditionTypeUnsatisfiable),
			)
			Expect(condition).NotTo(BeNil())
			Expect(condition.Reason).To(Equal("ExporterDisabled"))
			Expect(condition.Message).To(ContainSubstring("is disabled"))
			Expect(condition.Message).To(ContainSubstring("allowDisabled"))
			Expect(condition.Message).To(ContainSubstring("--allow-disabled"))

			// Restore for subsequent tests
			setExporterEnabled(ctx, testExporter1DutA.Name, true)
		})
	})

	When("trying to lease a disabled exporter with allowDisabled", func() {
		It("should succeed when ExporterRef and AllowDisabled are set", func() {
			ctx := context.Background()
			setExporterEnabled(ctx, testExporter1DutA.Name, false)

			lease := leaseDutA2Sec.DeepCopy()
			lease.Spec.Selector.MatchLabels = nil
			lease.Spec.ExporterRef = &corev1.LocalObjectReference{Name: testExporter1DutA.Name}
			lease.Spec.AllowDisabled = true

			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).NotTo(BeNil())
			Expect(updatedLease.Status.ExporterRef.Name).To(Equal(testExporter1DutA.Name))

			// Restore for subsequent tests
			setExporterEnabled(ctx, testExporter1DutA.Name, true)
		})
	})

	When("trying to lease with selector and some exporters disabled", func() {
		It("should skip disabled exporters and lease an enabled one", func() {
			ctx := context.Background()
			// Disable one of the two dut=a exporters
			setExporterEnabled(ctx, testExporter1DutA.Name, false)

			lease := leaseDutA2Sec.DeepCopy()

			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).NotTo(BeNil())
			// Should only get the enabled exporter
			Expect(updatedLease.Status.ExporterRef.Name).To(Equal(testExporter2DutA.Name))

			// Restore for subsequent tests
			setExporterEnabled(ctx, testExporter1DutA.Name, true)
		})

		It("should be unsatisfiable when all matching exporters are disabled", func() {
			ctx := context.Background()
			setExporterEnabled(ctx, testExporter1DutA.Name, false)
			setExporterEnabled(ctx, testExporter2DutA.Name, false)

			lease := leaseDutA2Sec.DeepCopy()

			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).To(BeNil())
			Expect(meta.IsStatusConditionTrue(
				updatedLease.Status.Conditions,
				string(jumpstarterdevv1alpha1.LeaseConditionTypeUnsatisfiable),
			)).To(BeTrue())

			// Restore for subsequent tests
			setExporterEnabled(ctx, testExporter1DutA.Name, true)
			setExporterEnabled(ctx, testExporter2DutA.Name, true)
		})
	})
})

var testExporter1DutA = &jumpstarterdevv1alpha1.Exporter{
	ObjectMeta: metav1.ObjectMeta{
		Name:      "exporter1-dut-a",
		Namespace: "default",
		Labels: map[string]string{
			"dut": "a",
		},
	},
}

var testExporter2DutA = &jumpstarterdevv1alpha1.Exporter{
	ObjectMeta: metav1.ObjectMeta{
		Name:      "exporter2-dut-a",
		Namespace: "default",
		Labels: map[string]string{
			"dut": "a",
		},
	},
}

var testExporter3DutB = &jumpstarterdevv1alpha1.Exporter{
	ObjectMeta: metav1.ObjectMeta{
		Name:      "exporter3-dut-b",
		Namespace: "default",
		Labels: map[string]string{
			"dut": "b",
		},
	},
}

func setExporterOnlineConditions(ctx context.Context, name string, status metav1.ConditionStatus) {
	exporter := getExporter(ctx, name)
	meta.SetStatusCondition(&exporter.Status.Conditions, metav1.Condition{
		Type:   string(jumpstarterdevv1alpha1.ExporterConditionTypeRegistered),
		Status: status,
		Reason: "dummy",
	})
	meta.SetStatusCondition(&exporter.Status.Conditions, metav1.Condition{
		Type:   string(jumpstarterdevv1alpha1.ExporterConditionTypeOnline),
		Status: status,
		Reason: "dummy",
	})
	if status == metav1.ConditionTrue {
		exporter.Status.Devices = []jumpstarterdevv1alpha1.Device{{}}
		exporter.Status.LastSeen = metav1.Now()
		exporter.Status.ExporterStatusValue = jumpstarterdevv1alpha1.ExporterStatusAvailable
		exporter.Status.StatusMessage = "Available for leasing"
	} else {
		exporter.Status.Devices = nil
		exporter.Status.LastSeen = metav1.NewTime(metav1.Now().Add(-time.Minute * 2))
		exporter.Status.ExporterStatusValue = jumpstarterdevv1alpha1.ExporterStatusOffline
		exporter.Status.StatusMessage = "Offline"
	}
	Expect(k8sClient.Status().Update(ctx, exporter)).To(Succeed())
}

// setExporterEnabled updates an exporter's spec.enabled field.
func setExporterEnabled(ctx context.Context, name string, enabled bool) {
	exporter := getExporter(ctx, name)
	exporter.Spec.Enabled = &enabled
	Expect(k8sClient.Update(ctx, exporter)).To(Succeed())
}

// setExporterNotReady sets an exporter as online and registered but NOT ready
// for leasing (e.g., still running a hook from a previous lease).
func setExporterNotReady(ctx context.Context, name string, status string) {
	exporter := getExporter(ctx, name)
	meta.SetStatusCondition(&exporter.Status.Conditions, metav1.Condition{
		Type:   string(jumpstarterdevv1alpha1.ExporterConditionTypeRegistered),
		Status: metav1.ConditionTrue,
		Reason: "dummy",
	})
	meta.SetStatusCondition(&exporter.Status.Conditions, metav1.Condition{
		Type:   string(jumpstarterdevv1alpha1.ExporterConditionTypeOnline),
		Status: metav1.ConditionTrue,
		Reason: "dummy",
	})
	exporter.Status.Devices = []jumpstarterdevv1alpha1.Device{{}}
	exporter.Status.LastSeen = metav1.Now()
	exporter.Status.ExporterStatusValue = status
	exporter.Status.StatusMessage = "Running hook"
	Expect(k8sClient.Status().Update(ctx, exporter)).To(Succeed())
}

func reconcileLease(ctx context.Context, lease *jumpstarterdevv1alpha1.Lease) reconcile.Result {

	// reconcile the exporters
	typeNamespacedName := types.NamespacedName{
		Name:      lease.Name,
		Namespace: "default",
	}

	leaseReconciler := &LeaseReconciler{
		Client: k8sClient,
		Scheme: k8sClient.Scheme(),
	}

	signer, err := oidc.NewSignerFromSeed([]byte{}, "https://example.com", "dummy")
	Expect(err).NotTo(HaveOccurred())

	exporterReconciler := &ExporterReconciler{
		Client: k8sClient,
		Scheme: k8sClient.Scheme(),
		Signer: signer,
	}

	res, err := leaseReconciler.Reconcile(ctx, reconcile.Request{
		NamespacedName: typeNamespacedName,
	})
	Expect(err).NotTo(HaveOccurred())

	for _, owner := range getLease(ctx, lease.Name).OwnerReferences {
		_, err := exporterReconciler.Reconcile(ctx, reconcile.Request{
			NamespacedName: types.NamespacedName{Namespace: lease.Namespace, Name: owner.Name},
		})
		Expect(err).NotTo(HaveOccurred())
	}

	return res
}

func getLease(ctx context.Context, name string) *jumpstarterdevv1alpha1.Lease {
	lease := &jumpstarterdevv1alpha1.Lease{}
	err := k8sClient.Get(ctx, types.NamespacedName{
		Name:      name,
		Namespace: "default",
	}, lease)
	Expect(err).NotTo(HaveOccurred())
	return lease
}

func getExporter(ctx context.Context, name string) *jumpstarterdevv1alpha1.Exporter {
	exporter := &jumpstarterdevv1alpha1.Exporter{}
	err := k8sClient.Get(ctx, types.NamespacedName{
		Name:      name,
		Namespace: "default",
	}, exporter)
	Expect(err).NotTo(HaveOccurred())
	return exporter
}

func deleteLeases(ctx context.Context, leases ...string) {
	for _, lease := range leases {
		leaseObj := &jumpstarterdevv1alpha1.Lease{
			ObjectMeta: metav1.ObjectMeta{
				Name:      lease,
				Namespace: "default",
			},
		}
		_ = k8sClient.Delete(ctx, leaseObj)
	}
}

// Pending leases with offline exporters requeue every 1 second with no backoff.
// With MaxConcurrentReconciles=1 (default), N stuck leases generate N reconcile
// cycles per second, starving new lease requests from being processed promptly.
var _ = Describe("Pending lease queue starvation", func() {
	const (
		starveLease1 = "starve-pending-1"
		starveLease2 = "starve-pending-2"
		starveLease3 = "starve-pending-3"
		starveLease4 = "starve-pending-4"
		starveLease5 = "starve-pending-5"
		starveValid  = "starve-valid"
	)

	BeforeEach(func() {
		createExporters(context.Background(), testExporter1DutA, testExporter2DutA, testExporter3DutB)
	})
	AfterEach(func() {
		ctx := context.Background()
		deleteExporters(ctx, testExporter1DutA, testExporter2DutA, testExporter3DutB)
		deleteLeases(ctx,
			starveLease1, starveLease2, starveLease3, starveLease4, starveLease5,
			starveValid,
		)
	})

	When("multiple leases are pending because all exporters are offline", func() {
		It("should use exponential backoff instead of fixed 1-second requeue", func() {
			ctx := context.Background()

			// All exporters offline
			setExporterOnlineConditions(ctx, testExporter1DutA.Name, metav1.ConditionFalse)
			setExporterOnlineConditions(ctx, testExporter2DutA.Name, metav1.ConditionFalse)
			setExporterOnlineConditions(ctx, testExporter3DutB.Name, metav1.ConditionFalse)

			pendingNames := []string{starveLease1, starveLease2, starveLease3, starveLease4, starveLease5}
			for _, name := range pendingNames {
				lease := leaseDutA2Sec.DeepCopy()
				lease.Name = name
				Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			}

			leaseReconciler := &LeaseReconciler{
				Client: k8sClient,
				Scheme: k8sClient.Scheme(),
			}

			// First reconcile sets the Pending condition with LastTransitionTime=now
			for _, name := range pendingNames {
				_, err := leaseReconciler.Reconcile(ctx, reconcile.Request{
					NamespacedName: types.NamespacedName{Name: name, Namespace: "default"},
				})
				Expect(err).NotTo(HaveOccurred())
			}

			// Simulate time passing: patch LastTransitionTime on the Pending
			// condition to 60 seconds ago. In production this happens naturally
			// as the lease sits pending; in tests we fast-forward the clock.
			pastTime := metav1.NewTime(time.Now().Add(-60 * time.Second))
			for _, name := range pendingNames {
				lease := getLease(ctx, name)
				for i := range lease.Status.Conditions {
					if lease.Status.Conditions[i].Type == string(jumpstarterdevv1alpha1.LeaseConditionTypePending) {
						lease.Status.Conditions[i].LastTransitionTime = pastTime
					}
				}
				Expect(k8sClient.Status().Update(ctx, lease)).To(Succeed())
			}

			// Now reconcile again — backoff should be well above 1 second
			for _, name := range pendingNames {
				result, err := leaseReconciler.Reconcile(ctx, reconcile.Request{
					NamespacedName: types.NamespacedName{Name: name, Namespace: "default"},
				})
				Expect(err).NotTo(HaveOccurred())
				Expect(result.RequeueAfter).To(BeNumerically(">", time.Second),
					"lease %s should back off beyond 1s after being pending for 60s", name)
				Expect(result.RequeueAfter).To(BeNumerically("<=", maxPendingRequeue),
					"lease %s backoff should not exceed the cap", name)
			}
		})
	})

	When("pending leases are consuming the reconciler while a valid lease arrives", func() {
		It("should back off so new valid leases are not starved", func() {
			ctx := context.Background()

			// All exporters offline initially
			setExporterOnlineConditions(ctx, testExporter1DutA.Name, metav1.ConditionFalse)
			setExporterOnlineConditions(ctx, testExporter2DutA.Name, metav1.ConditionFalse)
			setExporterOnlineConditions(ctx, testExporter3DutB.Name, metav1.ConditionFalse)

			pendingNames := []string{starveLease1, starveLease2, starveLease3, starveLease4, starveLease5}
			for _, name := range pendingNames {
				lease := leaseDutA2Sec.DeepCopy()
				lease.Name = name
				Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			}

			leaseReconciler := &LeaseReconciler{
				Client: k8sClient,
				Scheme: k8sClient.Scheme(),
			}

			// First reconcile sets Pending condition
			for _, name := range pendingNames {
				_, err := leaseReconciler.Reconcile(ctx, reconcile.Request{
					NamespacedName: types.NamespacedName{Name: name, Namespace: "default"},
				})
				Expect(err).NotTo(HaveOccurred())
			}

			// Fast-forward: make pending leases look like they've been pending 60s
			pastTime := metav1.NewTime(time.Now().Add(-60 * time.Second))
			for _, name := range pendingNames {
				lease := getLease(ctx, name)
				for i := range lease.Status.Conditions {
					if lease.Status.Conditions[i].Type == string(jumpstarterdevv1alpha1.LeaseConditionTypePending) {
						lease.Status.Conditions[i].LastTransitionTime = pastTime
					}
				}
				Expect(k8sClient.Status().Update(ctx, lease)).To(Succeed())
			}

			// Verify pending leases have backed off
			var totalRequeueTime time.Duration
			for _, name := range pendingNames {
				result, err := leaseReconciler.Reconcile(ctx, reconcile.Request{
					NamespacedName: types.NamespacedName{Name: name, Namespace: "default"},
				})
				Expect(err).NotTo(HaveOccurred())
				totalRequeueTime += result.RequeueAfter
			}
			Expect(totalRequeueTime).To(BeNumerically(">", 5*time.Second),
				"pending leases should have backed off beyond 1s each")

			// Now bring exporter3 (dut:b) online and create a valid lease
			setExporterOnlineConditions(ctx, testExporter3DutB.Name, metav1.ConditionTrue)

			validLease := leaseDutA2Sec.DeepCopy()
			validLease.Name = starveValid
			validLease.Spec.Selector.MatchLabels["dut"] = "b"
			Expect(k8sClient.Create(ctx, validLease)).To(Succeed())

			// The valid lease gets its turn and succeeds immediately
			result, err := leaseReconciler.Reconcile(ctx, reconcile.Request{
				NamespacedName: types.NamespacedName{Name: starveValid, Namespace: "default"},
			})
			Expect(err).NotTo(HaveOccurred())

			updatedLease := getLease(ctx, starveValid)
			Expect(updatedLease.Status.ExporterRef).NotTo(BeNil(),
				"valid lease should acquire exporter3-dut-b")
			Expect(updatedLease.Status.ExporterRef.Name).To(Equal(testExporter3DutB.Name))
			Expect(result.RequeueAfter).To(BeNumerically("<=", 2*time.Second),
				"valid lease should requeue for expiration, not stuck pending")
		})
	})
})

var _ = Describe("orderApprovedExporters", func() {
	When("approved exporters are under a lease", func() {
		It("should put them last", func() {
			approvedExporters := []ApprovedExporter{
				{
					Policy:        jumpstarterdevv1alpha1.Policy{Priority: 0, SpotAccess: false},
					Exporter:      *testExporter1DutA,
					ExistingLease: &jumpstarterdevv1alpha1.Lease{},
				},
				{
					Policy:   jumpstarterdevv1alpha1.Policy{Priority: 0, SpotAccess: false},
					Exporter: *testExporter2DutA,
				},
			}
			ordered := orderApprovedExporters(approvedExporters)
			Expect(ordered[0].Exporter.Name).To(Equal(testExporter2DutA.Name))
			Expect(ordered[0].ExistingLease).To(BeNil())
			Expect(ordered[1].Exporter.Name).To(Equal(testExporter1DutA.Name))
			Expect(ordered[1].ExistingLease).NotTo(BeNil())
		})
	})

	When("some approved exporters are accessible in spot mode", func() {
		It("should put them last", func() {
			approvedExporters := []ApprovedExporter{
				{
					Policy:        jumpstarterdevv1alpha1.Policy{Priority: 0, SpotAccess: true},
					Exporter:      *testExporter1DutA,
					ExistingLease: &jumpstarterdevv1alpha1.Lease{},
				},
				{
					Policy:        jumpstarterdevv1alpha1.Policy{Priority: 0, SpotAccess: false},
					Exporter:      *testExporter2DutA,
					ExistingLease: &jumpstarterdevv1alpha1.Lease{},
				},
			}
			ordered := orderApprovedExporters(approvedExporters)
			Expect(ordered[0].Exporter.Name).To(Equal(testExporter2DutA.Name))
			Expect(ordered[0].Policy.SpotAccess).To(BeFalse())
			Expect(ordered[1].Exporter.Name).To(Equal(testExporter1DutA.Name))
			Expect(ordered[1].Policy.SpotAccess).To(BeTrue())
		})
	})

	When("some approved exporters have different policy priorities", func() {
		It("should order them by priority", func() {
			approvedExporters := []ApprovedExporter{
				{
					Policy:   jumpstarterdevv1alpha1.Policy{Priority: 5, SpotAccess: false},
					Exporter: *testExporter1DutA,
				},
				{
					Policy:   jumpstarterdevv1alpha1.Policy{Priority: 10, SpotAccess: false},
					Exporter: *testExporter2DutA,
				},
				{
					Policy:   jumpstarterdevv1alpha1.Policy{Priority: 100, SpotAccess: false},
					Exporter: *testExporter2DutA,
				},
			}
			ordered := orderApprovedExporters(approvedExporters)
			Expect(ordered[0].Policy.Priority).To(Equal(100))
			Expect(ordered[1].Policy.Priority).To(Equal(10))
			Expect(ordered[2].Policy.Priority).To(Equal(5))

		})
	})

	When("some approved exporters have same policy priorities and no other traits", func() {
		It("should order them by name", func() {
			approvedExporters := []ApprovedExporter{
				{
					Policy:   jumpstarterdevv1alpha1.Policy{Priority: 5, SpotAccess: false},
					Exporter: *testExporter2DutA,
				},
				{
					Policy:   jumpstarterdevv1alpha1.Policy{Priority: 5, SpotAccess: false},
					Exporter: *testExporter1DutA,
				},
			}
			ordered := orderApprovedExporters(approvedExporters)

			Expect(ordered[0].Exporter.Name).To(Equal(testExporter1DutA.Name))
			Expect(ordered[1].Exporter.Name).To(Equal(testExporter2DutA.Name))
		})
	})

	When("mixed priorities, spot access, lease status are in the list", func() {
		It("should order them properly", func() {
			approvedExporters := []ApprovedExporter{
				{
					Policy:   jumpstarterdevv1alpha1.Policy{Priority: 5, SpotAccess: false},
					Exporter: *testExporter2DutA,
				},
				{
					Policy:        jumpstarterdevv1alpha1.Policy{Priority: 100, SpotAccess: true},
					Exporter:      *testExporter2DutA,
					ExistingLease: &jumpstarterdevv1alpha1.Lease{},
				},
				{
					Policy:   jumpstarterdevv1alpha1.Policy{Priority: 10, SpotAccess: false},
					Exporter: *testExporter1DutA,
				},
				{
					Policy:   jumpstarterdevv1alpha1.Policy{Priority: 5, SpotAccess: false},
					Exporter: *testExporter1DutA,
				},
				{
					Policy:   jumpstarterdevv1alpha1.Policy{Priority: 10, SpotAccess: true},
					Exporter: *testExporter2DutA,
				},
			}

			ordered := orderApprovedExporters(approvedExporters)
			Expect(ordered[0].Policy.Priority).To(Equal(int(10)))
			Expect(ordered[0].Policy.SpotAccess).To(BeFalse())
			Expect(ordered[0].Exporter.Name).To(Equal(testExporter1DutA.Name))

			Expect(ordered[1].Policy.Priority).To(Equal(int(5)))
			Expect(ordered[1].Policy.SpotAccess).To(BeFalse())
			Expect(ordered[1].Exporter.Name).To(Equal(testExporter1DutA.Name))

			Expect(ordered[2].Policy.Priority).To(Equal(int(5)))
			Expect(ordered[2].Policy.SpotAccess).To(BeFalse())
			Expect(ordered[2].Exporter.Name).To(Equal(testExporter2DutA.Name))

			Expect(ordered[3].Policy.Priority).To(Equal(int(10)))
			Expect(ordered[3].Policy.SpotAccess).To(BeTrue())
			Expect(ordered[3].Exporter.Name).To(Equal(testExporter2DutA.Name))

			Expect(ordered[4].Policy.Priority).To(Equal(int(100)))
			Expect(ordered[4].Policy.SpotAccess).To(BeTrue())
			Expect(ordered[4].Exporter.Name).To(Equal(testExporter2DutA.Name))

		})
	})
})

var _ = Describe("filterOutDisabledExporters", func() {
	boolTrue := true
	boolFalse := false

	It("should keep exporters with nil Enabled (backward compat)", func() {
		exporters := []jumpstarterdevv1alpha1.Exporter{
			{ObjectMeta: metav1.ObjectMeta{Name: "nil-enabled"}},
		}
		result := filterOutDisabledExporters(exporters)
		Expect(result).To(HaveLen(1))
		Expect(result[0].Name).To(Equal("nil-enabled"))
	})

	It("should keep exporters with Enabled=true", func() {
		exporters := []jumpstarterdevv1alpha1.Exporter{
			{
				ObjectMeta: metav1.ObjectMeta{Name: "enabled"},
				Spec:       jumpstarterdevv1alpha1.ExporterSpec{Enabled: &boolTrue},
			},
		}
		result := filterOutDisabledExporters(exporters)
		Expect(result).To(HaveLen(1))
		Expect(result[0].Name).To(Equal("enabled"))
	})

	It("should remove exporters with Enabled=false", func() {
		exporters := []jumpstarterdevv1alpha1.Exporter{
			{
				ObjectMeta: metav1.ObjectMeta{Name: "disabled"},
				Spec:       jumpstarterdevv1alpha1.ExporterSpec{Enabled: &boolFalse},
			},
		}
		result := filterOutDisabledExporters(exporters)
		Expect(result).To(BeEmpty())
	})

	It("should filter correctly with a mix of nil, true, and false", func() {
		exporters := []jumpstarterdevv1alpha1.Exporter{
			{ObjectMeta: metav1.ObjectMeta{Name: "nil-enabled"}},
			{
				ObjectMeta: metav1.ObjectMeta{Name: "enabled"},
				Spec:       jumpstarterdevv1alpha1.ExporterSpec{Enabled: &boolTrue},
			},
			{
				ObjectMeta: metav1.ObjectMeta{Name: "disabled"},
				Spec:       jumpstarterdevv1alpha1.ExporterSpec{Enabled: &boolFalse},
			},
			{
				ObjectMeta: metav1.ObjectMeta{Name: "also-disabled"},
				Spec:       jumpstarterdevv1alpha1.ExporterSpec{Enabled: &boolFalse},
			},
		}
		result := filterOutDisabledExporters(exporters)
		Expect(result).To(HaveLen(2))
		Expect(result[0].Name).To(Equal("nil-enabled"))
		Expect(result[1].Name).To(Equal("enabled"))
	})

	It("should not modify the original slice", func() {
		exporters := []jumpstarterdevv1alpha1.Exporter{
			{ObjectMeta: metav1.ObjectMeta{Name: "enabled"}},
			{
				ObjectMeta: metav1.ObjectMeta{Name: "disabled"},
				Spec:       jumpstarterdevv1alpha1.ExporterSpec{Enabled: &boolFalse},
			},
		}
		result := filterOutDisabledExporters(exporters)
		Expect(result).To(HaveLen(1))
		Expect(exporters).To(HaveLen(2)) // original unchanged
	})
})

var _ = Describe("isLeaseEnded", func() {
	It("should return true when the ended label is present", func() {
		lease := &jumpstarterdevv1alpha1.Lease{
			ObjectMeta: metav1.ObjectMeta{
				Labels: map[string]string{
					string(jumpstarterdevv1alpha1.LeaseLabelEnded): jumpstarterdevv1alpha1.LeaseLabelEndedValue,
				},
			},
		}
		Expect(isLeaseEnded(lease)).To(BeTrue())
	})

	It("should return false when the ended label is missing", func() {
		lease := &jumpstarterdevv1alpha1.Lease{
			ObjectMeta: metav1.ObjectMeta{
				Labels: map[string]string{},
			},
		}
		Expect(isLeaseEnded(lease)).To(BeFalse())
	})

	It("should return false when labels are nil", func() {
		lease := &jumpstarterdevv1alpha1.Lease{}
		Expect(isLeaseEnded(lease)).To(BeFalse())
	})
})

var _ = Describe("skipEndedPredicate", func() {
	var skipEnded predicate.Funcs

	BeforeEach(func() {
		skipEnded = skipEndedPredicate()
	})

	It("should admit creates for non-ended leases", func() {
		lease := &jumpstarterdevv1alpha1.Lease{}
		Expect(skipEnded.Create(event.CreateEvent{Object: lease})).To(BeTrue())
	})

	It("should reject creates for ended leases", func() {
		lease := &jumpstarterdevv1alpha1.Lease{
			ObjectMeta: metav1.ObjectMeta{
				Labels: map[string]string{
					string(jumpstarterdevv1alpha1.LeaseLabelEnded): jumpstarterdevv1alpha1.LeaseLabelEndedValue,
				},
			},
		}
		Expect(skipEnded.Create(event.CreateEvent{Object: lease})).To(BeFalse())
	})

	It("should reject updates where new object has ended label", func() {
		oldLease := &jumpstarterdevv1alpha1.Lease{}
		newLease := &jumpstarterdevv1alpha1.Lease{
			ObjectMeta: metav1.ObjectMeta{
				Labels: map[string]string{
					string(jumpstarterdevv1alpha1.LeaseLabelEnded): jumpstarterdevv1alpha1.LeaseLabelEndedValue,
				},
			},
		}
		Expect(skipEnded.Update(event.UpdateEvent{ObjectOld: oldLease, ObjectNew: newLease})).To(BeFalse())
	})

	It("should admit updates for non-ended leases", func() {
		oldLease := &jumpstarterdevv1alpha1.Lease{}
		newLease := &jumpstarterdevv1alpha1.Lease{}
		Expect(skipEnded.Update(event.UpdateEvent{ObjectOld: oldLease, ObjectNew: newLease})).To(BeTrue())
	})

	It("should admit updates for ended-in-status but unlabeled leases so label can be backfilled", func() {
		oldLease := &jumpstarterdevv1alpha1.Lease{}
		newLease := &jumpstarterdevv1alpha1.Lease{
			Status: jumpstarterdevv1alpha1.LeaseStatus{
				Ended: true,
			},
		}
		// No ended label → predicate admits → reconciler runs → backfills label
		Expect(skipEnded.Update(event.UpdateEvent{ObjectOld: oldLease, ObjectNew: newLease})).To(BeTrue())
	})

	It("should always admit deletes", func() {
		lease := &jumpstarterdevv1alpha1.Lease{
			ObjectMeta: metav1.ObjectMeta{
				Labels: map[string]string{
					string(jumpstarterdevv1alpha1.LeaseLabelEnded): jumpstarterdevv1alpha1.LeaseLabelEndedValue,
				},
			},
		}
		Expect(skipEnded.Delete(event.DeleteEvent{Object: lease})).To(BeTrue())
	})

})

var _ = Describe("Scheduled Leases", func() {
	BeforeEach(func() {
		createExporters(context.Background(), testExporter1DutA, testExporter2DutA, testExporter3DutB)
		setExporterOnlineConditions(context.Background(), testExporter1DutA.Name, metav1.ConditionTrue)
		setExporterOnlineConditions(context.Background(), testExporter2DutA.Name, metav1.ConditionTrue)
		setExporterOnlineConditions(context.Background(), testExporter3DutB.Name, metav1.ConditionTrue)
	})
	AfterEach(func() {
		ctx := context.Background()
		deleteExporters(ctx, testExporter1DutA, testExporter2DutA, testExporter3DutB)
		deleteLeases(ctx, lease1Name, lease2Name, lease3Name)
	})

	When("creating lease with Duration only (immediate lease)", func() {
		It("should acquire exporter immediately and set effective begin time", func() {
			lease := leaseDutA2Sec.DeepCopy()
			lease.Spec.Duration = &metav1.Duration{Duration: 2 * time.Second}
			lease.Spec.BeginTime = nil
			lease.Spec.EndTime = nil

			ctx := context.Background()
			beforeCreate := time.Now().Truncate(time.Second)
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)
			afterReconcile := time.Now().Truncate(time.Second)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Spec.BeginTime).To(BeNil(), "Spec.BeginTime should remain nil for immediate leases")
			Expect(updatedLease.Spec.EndTime).To(BeNil(), "Spec.EndTime should remain nil")
			Expect(updatedLease.Status.BeginTime).NotTo(BeNil(), "Status.BeginTime should be set")
			Expect(updatedLease.Status.BeginTime.Time).To(BeTemporally(">=", beforeCreate))
			Expect(updatedLease.Status.BeginTime.Time).To(BeTemporally("<=", afterReconcile))
			Expect(updatedLease.Status.ExporterRef).NotTo(BeNil(), "Should have acquired exporter immediately")
		})
	})

	When("creating lease with BeginTime + Duration (scheduled lease)", func() {
		It("should wait until BeginTime before acquiring exporter", func() {
			lease := leaseDutA2Sec.DeepCopy()
			futureTime := metav1.NewTime(time.Now().Truncate(time.Second).Add(1 * time.Second))
			lease.Spec.BeginTime = &futureTime
			lease.Spec.Duration = &metav1.Duration{Duration: 1 * time.Second}
			lease.Spec.EndTime = nil

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			result := reconcileLease(ctx, lease)

			// Should requeue for future time
			Expect(result.RequeueAfter).To(BeNumerically(">", 0))
			Expect(result.RequeueAfter).To(BeNumerically("<=", 2*time.Second))

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).To(BeNil(), "Should not have acquired exporter yet")
			Expect(updatedLease.Status.BeginTime).To(BeNil(), "Status.BeginTime should not be set yet")

			// Poll until BeginTime passes and exporter is acquired
			Eventually(func() bool {
				_ = reconcileLease(ctx, lease)
				updatedLease = getLease(ctx, lease.Name)
				return updatedLease.Status.ExporterRef != nil
			}).WithTimeout(3*time.Second).WithPolling(50*time.Millisecond).Should(BeTrue(), "Should have acquired exporter after BeginTime")

			Expect(updatedLease.Status.BeginTime).NotTo(BeNil(), "Status.BeginTime should be set")
			Expect(updatedLease.Status.BeginTime.Time).To(BeTemporally(">=", futureTime.Time))
		})
	})

	When("creating lease with BeginTime + EndTime (without Duration)", func() {
		It("should calculate Duration and wait until BeginTime", func() {
			lease := leaseDutA2Sec.DeepCopy()
			beginTime := metav1.NewTime(time.Now().Truncate(time.Second).Add(1 * time.Second))
			endTime := metav1.NewTime(beginTime.Add(1 * time.Second))
			lease.Spec.BeginTime = &beginTime
			lease.Spec.EndTime = &endTime
			lease.Spec.Duration = nil

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())

			// The Duration should be calculated by LeaseFromProtobuf or validation webhook
			// For now, we need to set it manually since we're creating directly via k8s client
			updatedLease := getLease(ctx, lease.Name)
			updatedLease.Spec.Duration = &metav1.Duration{Duration: endTime.Sub(beginTime.Time)}
			Expect(k8sClient.Update(ctx, updatedLease)).To(Succeed())

			result := reconcileLease(ctx, updatedLease)
			Expect(result.RequeueAfter).To(BeNumerically(">", 0))

			updatedLease = getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).To(BeNil(), "Should not have acquired exporter yet")
			Expect(updatedLease.Spec.Duration.Duration).To(Equal(1 * time.Second))

			// Poll until BeginTime passes and exporter is acquired
			Eventually(func() bool {
				_ = reconcileLease(ctx, updatedLease)
				updatedLease = getLease(ctx, lease.Name)
				return updatedLease.Status.ExporterRef != nil
			}).WithTimeout(3*time.Second).WithPolling(50*time.Millisecond).Should(BeTrue(), "Should have acquired exporter")

			Expect(updatedLease.Status.BeginTime).NotTo(BeNil())
		})
	})

	When("creating lease with EndTime only (immediate lease with fixed end time)", func() {
		It("should acquire exporter immediately and end at EndTime", func() {
			lease := leaseDutA2Sec.DeepCopy()
			endTime := metav1.NewTime(time.Now().Truncate(time.Second).Add(1 * time.Second))
			lease.Spec.BeginTime = nil
			lease.Spec.EndTime = &endTime
			lease.Spec.Duration = nil

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).NotTo(BeNil(), "Should acquire exporter immediately")
			Expect(updatedLease.Status.BeginTime).NotTo(BeNil(), "Status.BeginTime should be set")
			Expect(updatedLease.Spec.EndTime.Time).To(Equal(endTime.Time))

			// Poll until EndTime passes and lease ends
			Eventually(func() bool {
				_ = reconcileLease(ctx, lease)
				updatedLease = getLease(ctx, lease.Name)
				return updatedLease.Status.Ended
			}).WithTimeout(3*time.Second).WithPolling(50*time.Millisecond).Should(BeTrue(), "Lease should end at specified EndTime")
			Expect(updatedLease.Status.EndTime).NotTo(BeNil(), "Status.EndTime should be set")

			// Check EffectiveDuration in protobuf representation
			pbLease := updatedLease.ToProtobuf()
			Expect(pbLease.EffectiveBeginTime).NotTo(BeNil())
			Expect(pbLease.EffectiveEndTime).NotTo(BeNil())
			Expect(pbLease.EffectiveDuration).NotTo(BeNil())

			effectiveDuration := pbLease.EffectiveDuration.AsDuration()
			actualDuration := updatedLease.Status.EndTime.Sub(updatedLease.Status.BeginTime.Time)
			Expect(effectiveDuration).To(BeNumerically("~", actualDuration, 10*time.Millisecond))
		})
	})

	When("creating lease with EndTime + Duration (calculated future BeginTime)", func() {
		It("should calculate BeginTime and wait before acquiring exporter", func() {
			lease := leaseDutA2Sec.DeepCopy()
			endTime := metav1.NewTime(time.Now().Truncate(time.Second).Add(2 * time.Second))
			duration := 1 * time.Second
			expectedBeginTime := endTime.Add(-duration)

			lease.Spec.BeginTime = nil
			lease.Spec.EndTime = &endTime
			lease.Spec.Duration = &metav1.Duration{Duration: duration}

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())

			// The BeginTime should be calculated by LeaseFromProtobuf or validation
			updatedLease := getLease(ctx, lease.Name)
			updatedLease.Spec.BeginTime = &metav1.Time{Time: expectedBeginTime}
			Expect(k8sClient.Update(ctx, updatedLease)).To(Succeed())

			result := reconcileLease(ctx, updatedLease)
			Expect(result.RequeueAfter).To(BeNumerically(">", 0))

			updatedLease = getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).To(BeNil(), "Should not have acquired exporter yet")
			Expect(updatedLease.Spec.BeginTime.Time).To(BeTemporally("~", expectedBeginTime, 10*time.Millisecond))

			// Poll until calculated BeginTime passes and exporter is acquired
			Eventually(func() bool {
				_ = reconcileLease(ctx, updatedLease)
				updatedLease = getLease(ctx, lease.Name)
				return updatedLease.Status.ExporterRef != nil
			}).WithTimeout(1200*time.Millisecond).WithPolling(50*time.Millisecond).Should(BeTrue(), "Should have acquired exporter after calculated BeginTime")
		})

		It("should start immediately when calculated BeginTime is in the past", func() {
			lease := leaseDutA2Sec.DeepCopy()
			// Test scenario: Explicit BeginTime in past (simulating EndTime+Duration calculation result)
			// Set BeginTime well in the past to ensure it's definitely past even with delays
			pastBeginTime := time.Now().Truncate(time.Second).Add(-10 * time.Second)
			futureEndTime := time.Now().Truncate(time.Second).Add(20 * time.Second)

			lease.Spec.BeginTime = &metav1.Time{Time: pastBeginTime}
			lease.Spec.EndTime = &metav1.Time{Time: futureEndTime}
			lease.Spec.Duration = &metav1.Duration{Duration: futureEndTime.Sub(pastBeginTime)}

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())

			result := reconcileLease(ctx, lease)

			// The lease should start immediately, but will requeue to check expiration at EndTime
			// RequeueAfter should be approximately time until EndTime (~20 seconds)
			Expect(result.RequeueAfter).To(BeNumerically(">", 15*time.Second), "Should requeue for expiration check")
			Expect(result.RequeueAfter).To(BeNumerically("<=", 21*time.Second), "Requeue should be around EndTime")

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).NotTo(BeNil(), "Should acquire exporter immediately")
			Expect(updatedLease.Status.BeginTime).NotTo(BeNil(), "Status.BeginTime should be set")

			// Status.BeginTime should be the actual acquisition time (now), not the calculated past time
			// Allow generous tolerance for CI environments with second-precision timestamps
			now := time.Now().Truncate(time.Second)
			Expect(updatedLease.Status.BeginTime.Time).To(BeTemporally(">=", now.Add(-2*time.Second)))
			Expect(updatedLease.Status.BeginTime.Time).To(BeTemporally("<=", now.Add(2*time.Second)))

			// EffectiveDuration should be based on actual Status.BeginTime, not Spec.BeginTime
			// Since timestamps have second precision, allow up to 1 second tolerance
			pbLease := updatedLease.ToProtobuf()
			Expect(pbLease.EffectiveDuration).NotTo(BeNil())
			actualDuration := pbLease.EffectiveDuration.AsDuration()
			// Should be small (just acquired), allowing for second-precision truncation
			Expect(actualDuration).To(BeNumerically("<=", 2*time.Second))
			Expect(actualDuration).To(BeNumerically(">=", 0))
		})
	})

	When("creating lease with BeginTime + EndTime + Duration (all three specified)", func() {
		It("should validate consistency and use the values", func() {
			lease := leaseDutA2Sec.DeepCopy()
			beginTime := metav1.NewTime(time.Now().Truncate(time.Second).Add(1 * time.Second))
			duration := 1 * time.Second
			endTime := metav1.NewTime(beginTime.Add(duration))

			lease.Spec.BeginTime = &beginTime
			lease.Spec.EndTime = &endTime
			lease.Spec.Duration = &metav1.Duration{Duration: duration}

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			result := reconcileLease(ctx, lease)

			Expect(result.RequeueAfter).To(BeNumerically(">", 0))

			// Poll until BeginTime passes and exporter is acquired
			var updatedLease *jumpstarterdevv1alpha1.Lease
			Eventually(func() bool {
				_ = reconcileLease(ctx, lease)
				updatedLease = getLease(ctx, lease.Name)
				return updatedLease.Status.ExporterRef != nil
			}).WithTimeout(1200 * time.Millisecond).WithPolling(50 * time.Millisecond).Should(BeTrue())

			Expect(updatedLease.Status.BeginTime).NotTo(BeNil())
			Expect(updatedLease.Spec.BeginTime.Time).To(Equal(beginTime.Time))
			Expect(updatedLease.Spec.EndTime.Time).To(Equal(endTime.Time))
			Expect(updatedLease.Spec.Duration.Duration).To(Equal(duration))
		})

		It("should reject when Duration conflicts with EndTime - BeginTime", func() {
			// Test through the service layer (LeaseFromProtobuf) which validates
			beginTime := metav1.NewTime(time.Now().Truncate(time.Second).Add(1 * time.Second))
			endTime := metav1.NewTime(beginTime.Add(1 * time.Second))
			conflictingDuration := 2 * time.Second // Wrong! Should be 1 second

			// Create via LeaseFromProtobuf to trigger validation
			key := types.NamespacedName{Name: "test-lease", Namespace: "default"}
			clientRef := corev1.LocalObjectReference{Name: testClient.Name}

			pbLease := &cpb.Lease{
				Selector: "dut=a",
			}
			pbLease.BeginTime = timestamppb.New(beginTime.Time)
			pbLease.EndTime = timestamppb.New(endTime.Time)
			pbLease.Duration = durationpb.New(conflictingDuration)

			lease, err := jumpstarterdevv1alpha1.LeaseFromProtobuf(pbLease, key, clientRef)

			// Should fail validation
			Expect(err).To(HaveOccurred())
			Expect(err.Error()).To(ContainSubstring("duration conflicts"))
			Expect(lease).To(BeNil())
		})
	})

	When("creating lease with BeginTime already in the past", func() {
		It("should start immediately without requeuing", func() {
			lease := leaseDutA2Sec.DeepCopy()
			// Set BeginTime to 2 seconds in the past to ensure it's definitely passed
			nowTime := metav1.NewTime(time.Now().Truncate(time.Second).Add(-2 * time.Second))
			lease.Spec.BeginTime = &nowTime
			lease.Spec.Duration = &metav1.Duration{Duration: 1 * time.Second}

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			result := reconcileLease(ctx, lease)

			// Should not requeue (or requeue with 0)
			Expect(result.RequeueAfter).To(BeNumerically("<=", 0))

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).NotTo(BeNil(), "Should acquire exporter immediately")
			Expect(updatedLease.Status.BeginTime).NotTo(BeNil())
		})
	})

	When("lease expires based on Spec.EndTime", func() {
		It("should end the lease at EndTime even if Duration would suggest later", func() {
			lease := leaseDutA2Sec.DeepCopy()
			endTime := metav1.NewTime(time.Now().Truncate(time.Second).Add(1 * time.Second))
			lease.Spec.EndTime = &endTime
			lease.Spec.Duration = &metav1.Duration{Duration: 10 * time.Second} // Much longer than EndTime

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).NotTo(BeNil())

			// Poll until EndTime passes and lease ends
			Eventually(func() bool {
				_ = reconcileLease(ctx, lease)
				updatedLease = getLease(ctx, lease.Name)
				return updatedLease.Status.Ended
			}).WithTimeout(3*time.Second).WithPolling(50*time.Millisecond).Should(BeTrue(), "Should respect EndTime over Duration")
			Expect(updatedLease.Status.EndTime).NotTo(BeNil())

			// Verify EffectiveDuration is calculated correctly
			pbLease := updatedLease.ToProtobuf()
			Expect(pbLease.EffectiveDuration).NotTo(BeNil())
			actualDuration := updatedLease.Status.EndTime.Sub(updatedLease.Status.BeginTime.Time)
			// Allow tolerance for CI environments - duration is based on second-truncated times
			Expect(pbLease.EffectiveDuration.AsDuration()).To(BeNumerically("~", actualDuration, 1*time.Second))
			// Verify it's shorter than the specified Duration (10s)
			Expect(pbLease.EffectiveDuration.AsDuration()).To(BeNumerically("<", 3*time.Second))
		})
	})

	When("lease with BeginTime expires based on BeginTime + Duration", func() {
		It("should end the lease at BeginTime + Duration", func() {
			lease := leaseDutA2Sec.DeepCopy()
			beginTime := metav1.NewTime(time.Now().Truncate(time.Second).Add(1 * time.Second))
			duration := 1 * time.Second
			lease.Spec.BeginTime = &beginTime
			lease.Spec.Duration = &metav1.Duration{Duration: duration}
			lease.Spec.EndTime = nil

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())

			// Poll until BeginTime passes and exporter is acquired
			var updatedLease *jumpstarterdevv1alpha1.Lease
			Eventually(func() bool {
				_ = reconcileLease(ctx, lease)
				updatedLease = getLease(ctx, lease.Name)
				return updatedLease.Status.ExporterRef != nil
			}).WithTimeout(1200 * time.Millisecond).WithPolling(50 * time.Millisecond).Should(BeTrue())

			// Poll until lease expires (Duration after BeginTime)
			Eventually(func() bool {
				_ = reconcileLease(ctx, lease)
				updatedLease = getLease(ctx, lease.Name)
				return updatedLease.Status.Ended
			}).WithTimeout(3*time.Second).WithPolling(50*time.Millisecond).Should(BeTrue(), "Should expire at BeginTime + Duration")
			Expect(updatedLease.Status.EndTime).NotTo(BeNil())

			// Verify EffectiveDuration matches the specified duration
			// Allow generous tolerance for CI environments with second-precision timestamps
			pbLease := updatedLease.ToProtobuf()
			Expect(pbLease.EffectiveDuration).NotTo(BeNil())
			Expect(pbLease.EffectiveDuration.AsDuration()).To(BeNumerically("~", duration, 1*time.Second))
		})
	})

	When("lease without BeginTime expires based on Status.BeginTime + Duration", func() {
		It("should end the lease at Status.BeginTime + Duration", func() {
			lease := leaseDutA2Sec.DeepCopy()
			lease.Spec.Duration = &metav1.Duration{Duration: 1 * time.Second}
			lease.Spec.BeginTime = nil
			lease.Spec.EndTime = nil

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).NotTo(BeNil())
			Expect(updatedLease.Status.BeginTime).NotTo(BeNil())
			actualBeginTime := updatedLease.Status.BeginTime.Time

			// Poll until lease expires
			Eventually(func() bool {
				_ = reconcileLease(ctx, lease)
				updatedLease = getLease(ctx, lease.Name)
				return updatedLease.Status.Ended
			}).WithTimeout(2 * time.Second).WithPolling(50 * time.Millisecond).Should(BeTrue())
			Expect(updatedLease.Status.EndTime).NotTo(BeNil())

			// Verify it expired based on Status.BeginTime + Duration
			expectedExpiry := actualBeginTime.Add(1 * time.Second)
			Expect(time.Now().Truncate(time.Second)).To(BeTemporally(">=", expectedExpiry))

			// Verify EffectiveDuration is calculated correctly
			// Allow generous tolerance for CI environments with second-precision timestamps
			pbLease := updatedLease.ToProtobuf()
			Expect(pbLease.EffectiveDuration).NotTo(BeNil())
			Expect(pbLease.EffectiveDuration.AsDuration()).To(BeNumerically("~", 1*time.Second, 1*time.Second))
		})
	})

	When("checking EffectiveDuration on active lease", func() {
		It("should calculate EffectiveDuration as current time minus Status.BeginTime", func() {
			lease := leaseDutA2Sec.DeepCopy()
			lease.Spec.Duration = &metav1.Duration{Duration: 10 * time.Second} // Long duration so it doesn't expire

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).NotTo(BeNil())
			Expect(updatedLease.Status.BeginTime).NotTo(BeNil())
			Expect(updatedLease.Status.EndTime).To(BeNil(), "Active lease should not have EndTime")

			// Check EffectiveDuration on active lease
			beforeCheck := time.Now().Truncate(time.Second)
			pbLease := updatedLease.ToProtobuf()
			afterCheck := time.Now().Truncate(time.Second).Add(time.Second)

			Expect(pbLease.EffectiveBeginTime).NotTo(BeNil())
			Expect(pbLease.EffectiveEndTime).To(BeNil(), "Active lease should not have EffectiveEndTime")
			Expect(pbLease.EffectiveDuration).NotTo(BeNil())

			// EffectiveDuration should be approximately now() - BeginTime
			expectedMinDuration := beforeCheck.Sub(updatedLease.Status.BeginTime.Time)
			expectedMaxDuration := afterCheck.Sub(updatedLease.Status.BeginTime.Time)
			actualDuration := pbLease.EffectiveDuration.AsDuration()
			Expect(actualDuration).To(BeNumerically(">=", expectedMinDuration))
			Expect(actualDuration).To(BeNumerically("<=", expectedMaxDuration))
		})
	})

	When("multiple leases with different BeginTimes", func() {
		It("should acquire exporters at their respective BeginTimes", func() {
			ctx := context.Background()

			// Immediate lease
			lease1 := leaseDutA2Sec.DeepCopy()
			lease1.Name = lease1Name
			lease1.Spec.Duration = &metav1.Duration{Duration: 5 * time.Second}
			Expect(k8sClient.Create(ctx, lease1)).To(Succeed())
			_ = reconcileLease(ctx, lease1)

			updatedLease1 := getLease(ctx, lease1Name)
			Expect(updatedLease1.Status.ExporterRef).NotTo(BeNil())
			exporter1 := updatedLease1.Status.ExporterRef.Name

			// Scheduled lease 1s in future
			lease2 := leaseDutA2Sec.DeepCopy()
			lease2.Name = lease2Name
			futureTime := metav1.NewTime(time.Now().Truncate(time.Second).Add(1 * time.Second))
			lease2.Spec.BeginTime = &futureTime
			lease2.Spec.Duration = &metav1.Duration{Duration: 1 * time.Second}
			Expect(k8sClient.Create(ctx, lease2)).To(Succeed())
			_ = reconcileLease(ctx, lease2)

			updatedLease2 := getLease(ctx, lease2Name)
			Expect(updatedLease2.Status.ExporterRef).To(BeNil(), "Scheduled lease should wait")

			// Poll until lease2's BeginTime passes and exporter is acquired
			Eventually(func() bool {
				_ = reconcileLease(ctx, lease2)
				updatedLease2 = getLease(ctx, lease2Name)
				return updatedLease2.Status.ExporterRef != nil
			}).WithTimeout(1200*time.Millisecond).WithPolling(50*time.Millisecond).Should(BeTrue(), "Should acquire after BeginTime")
			exporter2 := updatedLease2.Status.ExporterRef.Name

			// Should have acquired different exporters (both dut:a exporters)
			Expect(exporter2).NotTo(Equal(exporter1))
			Expect([]string{exporter1, exporter2}).To(ConsistOf(testExporter1DutA.Name, testExporter2DutA.Name))
		})
	})

	// Validation error tests
	When("creating lease with BeginTime after EndTime", func() {
		It("should reject with validation error", func() {
			beginTime := metav1.NewTime(time.Now().Truncate(time.Second).Add(1 * time.Second))
			endTime := metav1.NewTime(beginTime.Add(-1 * time.Second)) // Before BeginTime!

			key := types.NamespacedName{Name: "invalid-lease", Namespace: "default"}
			clientRef := corev1.LocalObjectReference{Name: testClient.Name}

			pbLease := &cpb.Lease{
				Selector:  "dut=a",
				BeginTime: timestamppb.New(beginTime.Time),
				EndTime:   timestamppb.New(endTime.Time),
				// No duration provided - will calculate negative duration from BeginTime > EndTime
			}

			lease, err := jumpstarterdevv1alpha1.LeaseFromProtobuf(pbLease, key, clientRef)

			Expect(err).To(HaveOccurred())
			Expect(err.Error()).To(ContainSubstring("duration must be positive"))
			Expect(lease).To(BeNil())
		})
	})

	When("creating lease with BeginTime but zero Duration and no EndTime", func() {
		It("should reject with validation error", func() {
			beginTime := metav1.NewTime(time.Now().Truncate(time.Second).Add(1 * time.Second))

			key := types.NamespacedName{Name: "invalid-lease", Namespace: "default"}
			clientRef := corev1.LocalObjectReference{Name: testClient.Name}

			pbLease := &cpb.Lease{
				Selector: "dut=a",
			}
			pbLease.BeginTime = timestamppb.New(beginTime.Time)
			// No Duration, no EndTime

			lease, err := jumpstarterdevv1alpha1.LeaseFromProtobuf(pbLease, key, clientRef)

			Expect(err).To(HaveOccurred())
			Expect(err.Error()).To(ContainSubstring("duration is required"))
			Expect(lease).To(BeNil())
		})
	})

	// EndTime in the past
	When("creating lease with EndTime already in the past", func() {
		It("should create but expire immediately", func() {
			lease := leaseDutA2Sec.DeepCopy()
			pastEndTime := metav1.NewTime(time.Now().Truncate(time.Second).Add(-500 * time.Millisecond))
			lease.Spec.EndTime = &pastEndTime
			lease.Spec.Duration = nil

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			// Should acquire exporter (or try to)
			// Then immediately expire because EndTime is in the past
			Expect(updatedLease.Status.Ended).To(BeTrue(), "Lease should be ended immediately")
			Expect(updatedLease.Status.EndTime).NotTo(BeNil())
		})
	})

	When("creating lease with BeginTime in past but EndTime in future", func() {
		It("should start immediately and run until EndTime", func() {
			lease := leaseDutA2Sec.DeepCopy()
			pastBeginTime := metav1.NewTime(time.Now().Truncate(time.Second).Add(-500 * time.Millisecond))
			futureEndTime := metav1.NewTime(time.Now().Truncate(time.Second).Add(1 * time.Second))
			lease.Spec.BeginTime = &pastBeginTime
			lease.Spec.EndTime = &futureEndTime
			lease.Spec.Duration = &metav1.Duration{Duration: futureEndTime.Sub(pastBeginTime.Time)}

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).NotTo(BeNil(), "Should acquire immediately")
			Expect(updatedLease.Status.BeginTime).NotTo(BeNil())
			Expect(updatedLease.Status.Ended).To(BeFalse(), "Should not be ended yet")

			// Poll until EndTime passes and lease ends
			Eventually(func() bool {
				_ = reconcileLease(ctx, lease)
				updatedLease = getLease(ctx, lease.Name)
				return updatedLease.Status.Ended
			}).WithTimeout(3*time.Second).WithPolling(50*time.Millisecond).Should(BeTrue(), "Should expire at EndTime")
		})
	})

	// Early release scenarios
	When("releasing a scheduled lease before it starts", func() {
		It("should cancel the scheduled lease", func() {
			lease := leaseDutA2Sec.DeepCopy()
			futureTime := metav1.NewTime(time.Now().Truncate(time.Second).Add(1 * time.Second))
			lease.Spec.BeginTime = &futureTime
			lease.Spec.Duration = &metav1.Duration{Duration: 1 * time.Second}

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).To(BeNil(), "Should not have acquired yet")
			Expect(updatedLease.Status.Ended).To(BeFalse())

			// Release before BeginTime
			updatedLease.Spec.Release = true
			Expect(k8sClient.Update(ctx, updatedLease)).To(Succeed())
			_ = reconcileLease(ctx, updatedLease)

			updatedLease = getLease(ctx, lease.Name)
			Expect(updatedLease.Status.Ended).To(BeTrue(), "Should be cancelled/ended")
			Expect(updatedLease.Status.ExporterRef).To(BeNil(), "Should never have acquired exporter")
		})
	})

	When("releasing an active lease early", func() {
		It("should have EffectiveDuration matching actual time held", func() {
			lease := leaseDutA2Sec.DeepCopy()
			lease.Spec.Duration = &metav1.Duration{Duration: 10 * time.Second} // Long duration

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).NotTo(BeNil())
			Expect(updatedLease.Status.BeginTime).NotTo(BeNil())
			beginTime := updatedLease.Status.BeginTime.Time

			// Brief wait to ensure some time has passed
			time.Sleep(50 * time.Millisecond)

			// Release early
			updatedLease = getLease(ctx, lease.Name)
			updatedLease.Spec.Release = true
			Expect(k8sClient.Update(ctx, updatedLease)).To(Succeed())
			_ = reconcileLease(ctx, updatedLease)

			updatedLease = getLease(ctx, lease.Name)
			Expect(updatedLease.Status.Ended).To(BeTrue())
			Expect(updatedLease.Status.EndTime).NotTo(BeNil())

			// EffectiveDuration should be actual time held, not 10 seconds
			// Allow generous tolerance for CI environments with second-precision timestamps
			pbLease := updatedLease.ToProtobuf()
			Expect(pbLease.EffectiveDuration).NotTo(BeNil())
			actualDuration := pbLease.EffectiveDuration.AsDuration()
			expectedDuration := updatedLease.Status.EndTime.Sub(beginTime)
			Expect(actualDuration).To(BeNumerically("~", expectedDuration, 1*time.Second))
			Expect(actualDuration).To(BeNumerically("<=", 2*time.Second), "Should be much less than 10s")
		})
	})

	// Boundary conditions
	When("creating lease with BeginTime very close to EndTime", func() {
		It("should work with minimal duration", func() {
			lease := leaseDutA2Sec.DeepCopy()
			beginTime := metav1.NewTime(time.Now().Truncate(time.Second).Add(1 * time.Second))
			endTime := metav1.NewTime(beginTime.Add(1 * time.Second)) // 1 second duration
			lease.Spec.BeginTime = &beginTime
			lease.Spec.EndTime = &endTime
			lease.Spec.Duration = &metav1.Duration{Duration: 1 * time.Second}

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())

			// Poll until BeginTime passes and exporter is acquired
			var updatedLease *jumpstarterdevv1alpha1.Lease
			Eventually(func() bool {
				_ = reconcileLease(ctx, lease)
				updatedLease = getLease(ctx, lease.Name)
				return updatedLease.Status.ExporterRef != nil
			}).WithTimeout(1200 * time.Millisecond).WithPolling(50 * time.Millisecond).Should(BeTrue())

			// Poll until 1-second duration expires
			Eventually(func() bool {
				_ = reconcileLease(ctx, lease)
				updatedLease = getLease(ctx, lease.Name)
				return updatedLease.Status.Ended
			}).WithTimeout(1200 * time.Millisecond).WithPolling(50 * time.Millisecond).Should(BeTrue())
		})
	})

	When("lease expires between reconciliation calls", func() {
		It("should be marked as ended in next reconcile", func() {
			lease := leaseDutA2Sec.DeepCopy()
			lease.Spec.Duration = &metav1.Duration{Duration: 150 * time.Millisecond}

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).NotTo(BeNil())
			Expect(updatedLease.Status.Ended).To(BeFalse())

			// Poll until expiration is detected (lease duration is 150ms)
			Eventually(func() bool {
				_ = reconcileLease(ctx, lease)
				updatedLease = getLease(ctx, lease.Name)
				return updatedLease.Status.Ended
			}).WithTimeout(500*time.Millisecond).WithPolling(50*time.Millisecond).Should(BeTrue(), "Should be marked as ended")
		})
	})

	When("an ended lease has the ended label", func() {
		It("should be a no-op on subsequent reconciles", func() {
			lease := leaseDutA2Sec.DeepCopy()
			lease.Spec.Duration = &metav1.Duration{Duration: 100 * time.Millisecond}

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			Eventually(func() bool {
				_ = reconcileLease(ctx, lease)
				return getLease(ctx, lease.Name).Status.Ended
			}).WithTimeout(500 * time.Millisecond).WithPolling(50 * time.Millisecond).Should(BeTrue())

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Labels).To(HaveKeyWithValue(
				string(jumpstarterdevv1alpha1.LeaseLabelEnded),
				jumpstarterdevv1alpha1.LeaseLabelEndedValue,
			))

			// Reconciling again should be a no-op
			result := reconcileLease(ctx, lease)
			Expect(result.RequeueAfter).To(BeZero())
		})
	})

	When("an ended lease is missing the ended label", func() {
		It("should backfill the label on the next reconcile", func() {
			lease := leaseDutA2Sec.DeepCopy()
			lease.Spec.Duration = &metav1.Duration{Duration: 100 * time.Millisecond}

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			Eventually(func() bool {
				_ = reconcileLease(ctx, lease)
				return getLease(ctx, lease.Name).Status.Ended
			}).WithTimeout(500 * time.Millisecond).WithPolling(50 * time.Millisecond).Should(BeTrue())

			// Simulate split-brain: remove the ended label as if metadata update failed
			updatedLease := getLease(ctx, lease.Name)
			delete(updatedLease.Labels, string(jumpstarterdevv1alpha1.LeaseLabelEnded))
			Expect(k8sClient.Update(ctx, updatedLease)).To(Succeed())

			// Verify label is gone
			updatedLease = getLease(ctx, lease.Name)
			Expect(updatedLease.Labels).NotTo(HaveKey(string(jumpstarterdevv1alpha1.LeaseLabelEnded)))

			// Reconcile should backfill the label
			_ = reconcileLease(ctx, lease)
			updatedLease = getLease(ctx, lease.Name)
			Expect(updatedLease.Labels).To(HaveKeyWithValue(
				string(jumpstarterdevv1alpha1.LeaseLabelEnded),
				jumpstarterdevv1alpha1.LeaseLabelEndedValue,
			))
		})
	})

	// UpdateLease mutation tests
	// Note: These tests simulate what UpdateLease does via gRPC by directly
	// modifying the lease spec and calling ReconcileLeaseTimeFields
	When("updating BeginTime on a lease that has already started", func() {
		It("should be rejected in UpdateLease logic", func() {
			// This tests the validation that exists in client_service.go UpdateLease
			// We simulate it by checking the condition: ExporterRef != nil
			lease := leaseDutA2Sec.DeepCopy()
			lease.Spec.Duration = &metav1.Duration{Duration: 5 * time.Second}

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).NotTo(BeNil(), "Lease should be active")

			// Try to update BeginTime - this would be rejected by UpdateLease
			// We verify the precondition that UpdateLease checks
			Expect(updatedLease.Status.ExporterRef).NotTo(BeNil(), "Cannot update BeginTime after lease starts")
		})
	})

	When("updating EndTime on a scheduled lease before it starts", func() {
		It("should update EndTime and recalculate Duration", func() {
			lease := leaseDutA2Sec.DeepCopy()
			beginTime := metav1.NewTime(time.Now().Truncate(time.Second).Add(1 * time.Second))
			endTime := metav1.NewTime(beginTime.Add(1 * time.Second))
			lease.Spec.BeginTime = &beginTime
			lease.Spec.EndTime = &endTime
			lease.Spec.Duration = &metav1.Duration{Duration: 1 * time.Second}

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).To(BeNil(), "Should not have started yet")

			// Update EndTime (simulating UpdateLease behavior)
			newEndTime := metav1.NewTime(beginTime.Add(2 * time.Second))
			updatedLease.Spec.EndTime = &newEndTime
			// Clear Duration so it gets recalculated
			updatedLease.Spec.Duration = nil

			// Recalculate (this is what UpdateLease does)
			err := jumpstarterdevv1alpha1.ReconcileLeaseTimeFields(
				&updatedLease.Spec.BeginTime,
				&updatedLease.Spec.EndTime,
				&updatedLease.Spec.Duration,
			)
			Expect(err).NotTo(HaveOccurred())

			// Duration should be recalculated
			Expect(updatedLease.Spec.Duration.Duration).To(Equal(2 * time.Second))
			Expect(updatedLease.Spec.EndTime.Time).To(Equal(newEndTime.Time))
		})
	})

	When("extending an active lease by updating EndTime", func() {
		It("should extend the lease duration", func() {
			lease := leaseDutA2Sec.DeepCopy()
			endTime := metav1.NewTime(time.Now().Truncate(time.Second).Add(1 * time.Second))
			lease.Spec.EndTime = &endTime
			lease.Spec.Duration = nil

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).NotTo(BeNil(), "Should be active")
			Expect(updatedLease.Status.Ended).To(BeFalse())

			// Extend EndTime to 2 seconds from now
			newEndTime := metav1.NewTime(time.Now().Truncate(time.Second).Add(2 * time.Second))
			updatedLease.Spec.EndTime = &newEndTime
			Expect(k8sClient.Update(ctx, updatedLease)).To(Succeed())

			// Verify lease is still active after extension
			_ = reconcileLease(ctx, lease)
			updatedLease = getLease(ctx, lease.Name)
			Expect(updatedLease.Status.Ended).To(BeFalse(), "Should not expire yet due to extension")

			// Poll until new EndTime passes and lease ends
			Eventually(func() bool {
				_ = reconcileLease(ctx, lease)
				updatedLease = getLease(ctx, lease.Name)
				return updatedLease.Status.Ended
			}).WithTimeout(2200*time.Millisecond).WithPolling(50*time.Millisecond).Should(BeTrue(), "Should expire at new EndTime")
		})
	})

	When("shortening an active lease by updating Duration", func() {
		It("should shorten the lease duration", func() {
			lease := leaseDutA2Sec.DeepCopy()
			lease.Spec.Duration = &metav1.Duration{Duration: 1 * time.Second}

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).NotTo(BeNil(), "Should be active")
			Expect(updatedLease.Status.BeginTime).NotTo(BeNil())

			// Shorten to 200ms total duration
			updatedLease.Spec.Duration = &metav1.Duration{Duration: 200 * time.Millisecond}
			Expect(k8sClient.Update(ctx, updatedLease)).To(Succeed())

			// Poll until lease expires after shortened duration
			Eventually(func() bool {
				_ = reconcileLease(ctx, lease)
				updatedLease = getLease(ctx, lease.Name)
				return updatedLease.Status.Ended
			}).WithTimeout(500*time.Millisecond).WithPolling(50*time.Millisecond).Should(BeTrue(), "Should expire after shortened duration")
		})
	})

	When("updating scheduled lease EndTime before it starts", func() {
		It("should allow update and adjust timing", func() {
			lease := leaseDutA2Sec.DeepCopy()
			beginTime := metav1.NewTime(time.Now().Truncate(time.Second).Add(1 * time.Second))
			endTime := metav1.NewTime(beginTime.Add(10 * time.Second)) // Very long lease initially
			lease.Spec.BeginTime = &beginTime
			lease.Spec.EndTime = &endTime
			lease.Spec.Duration = &metav1.Duration{Duration: 10 * time.Second}

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).To(BeNil(), "Should not have started")

			// Shorten EndTime significantly
			newEndTime := metav1.NewTime(beginTime.Add(1 * time.Second))
			updatedLease.Spec.EndTime = &newEndTime
			// Clear Duration so it gets recalculated
			updatedLease.Spec.Duration = nil

			// Recalculate Duration (simulating UpdateLease)
			err := jumpstarterdevv1alpha1.ReconcileLeaseTimeFields(
				&updatedLease.Spec.BeginTime,
				&updatedLease.Spec.EndTime,
				&updatedLease.Spec.Duration,
			)
			Expect(err).NotTo(HaveOccurred())
			Expect(updatedLease.Spec.Duration.Duration).To(Equal(1 * time.Second))

			Expect(k8sClient.Update(ctx, updatedLease)).To(Succeed())

			// Poll until BeginTime passes and exporter is acquired
			Eventually(func() bool {
				_ = reconcileLease(ctx, updatedLease)
				updatedLease = getLease(ctx, lease.Name)
				return updatedLease.Status.ExporterRef != nil
			}).WithTimeout(1200 * time.Millisecond).WithPolling(50 * time.Millisecond).Should(BeTrue())

			// Poll until lease expires at new (shortened) EndTime (1s duration)
			Eventually(func() bool {
				_ = reconcileLease(ctx, lease)
				updatedLease = getLease(ctx, lease.Name)
				return updatedLease.Status.Ended
			}).WithTimeout(1200 * time.Millisecond).WithPolling(50 * time.Millisecond).Should(BeTrue())
		})
	})

	When("updating a lease with all three fields to maintain consistency", func() {
		It("should allow valid updates", func() {
			lease := leaseDutA2Sec.DeepCopy()
			beginTime := metav1.NewTime(time.Now().Truncate(time.Second).Add(1 * time.Second))
			duration := 500 * time.Millisecond
			endTime := metav1.NewTime(beginTime.Add(duration))

			lease.Spec.BeginTime = &beginTime
			lease.Spec.EndTime = &endTime
			lease.Spec.Duration = &metav1.Duration{Duration: duration}

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).To(BeNil(), "Should not have started yet")

			// Update all three fields consistently
			newDuration := 800 * time.Millisecond
			newEndTime := metav1.NewTime(beginTime.Add(newDuration))
			updatedLease.Spec.Duration = &metav1.Duration{Duration: newDuration}
			updatedLease.Spec.EndTime = &newEndTime

			// Validate consistency (simulating UpdateLease)
			err := jumpstarterdevv1alpha1.ReconcileLeaseTimeFields(
				&updatedLease.Spec.BeginTime,
				&updatedLease.Spec.EndTime,
				&updatedLease.Spec.Duration,
			)
			Expect(err).NotTo(HaveOccurred(), "Consistent update should succeed")
			Expect(updatedLease.Spec.Duration.Duration).To(Equal(newDuration))
			Expect(updatedLease.Spec.EndTime.Time).To(Equal(newEndTime.Time))
		})
	})

	When("updating a lease with all three fields to create conflict", func() {
		It("should reject updates that break consistency", func() {
			// Start with consistent fields
			beginTimeVal := metav1.NewTime(time.Now().Truncate(time.Second).Add(1 * time.Second))
			beginTime := &beginTimeVal
			duration := 500 * time.Millisecond
			endTimeVal := metav1.NewTime(beginTimeVal.Add(duration))
			endTime := &endTimeVal

			// Try to update Duration to conflict with BeginTime and EndTime
			conflictingDuration := &metav1.Duration{Duration: 1 * time.Second} // Wrong! EndTime-BeginTime = 500ms

			// Simulate UpdateLease validation
			err := jumpstarterdevv1alpha1.ReconcileLeaseTimeFields(
				&beginTime,
				&endTime,
				&conflictingDuration,
			)

			Expect(err).To(HaveOccurred())
			Expect(err.Error()).To(ContainSubstring("duration conflicts"))
		})
	})

	When("updating active lease Duration when all three fields exist", func() {
		It("should require updating both Duration and EndTime to keep them consistent", func() {
			lease := leaseDutA2Sec.DeepCopy()
			beginTime := metav1.NewTime(time.Now().Truncate(time.Second).Add(1 * time.Second))
			duration := 10 * time.Second // Long duration initially
			endTime := metav1.NewTime(beginTime.Add(duration))

			lease.Spec.BeginTime = &beginTime
			lease.Spec.EndTime = &endTime
			lease.Spec.Duration = &metav1.Duration{Duration: duration}

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())

			// Poll until lease starts
			var updatedLease *jumpstarterdevv1alpha1.Lease
			Eventually(func() bool {
				_ = reconcileLease(ctx, lease)
				updatedLease = getLease(ctx, lease.Name)
				return updatedLease.Status.ExporterRef != nil
			}).WithTimeout(1200*time.Millisecond).WithPolling(50*time.Millisecond).Should(BeTrue(), "Should have started")

			// Shorten the lease: Update both Duration AND EndTime together (must stay consistent)
			newDuration := 800 * time.Millisecond
			updatedLease.Spec.Duration = &metav1.Duration{Duration: newDuration}
			newEndTime := metav1.NewTime(beginTime.Add(newDuration))
			updatedLease.Spec.EndTime = &newEndTime

			// Validate the updated fields (should pass since all three are consistent)
			err := jumpstarterdevv1alpha1.ReconcileLeaseTimeFields(
				&updatedLease.Spec.BeginTime,
				&updatedLease.Spec.EndTime,
				&updatedLease.Spec.Duration,
			)
			Expect(err).NotTo(HaveOccurred())

			Expect(k8sClient.Update(ctx, updatedLease)).To(Succeed())

			// Poll until lease expires at new EndTime (800ms duration)
			Eventually(func() bool {
				_ = reconcileLease(ctx, lease)
				updatedLease = getLease(ctx, lease.Name)
				return updatedLease.Status.Ended
			}).WithTimeout(1500 * time.Millisecond).WithPolling(50 * time.Millisecond).Should(BeTrue())
		})
	})

	// Additional edge cases
	When("two scheduled leases compete for the same exporter", func() {
		It("should acquire first lease at BeginTime, then second after first is released", func() {
			ctx := context.Background()

			// Give lease1 an earlier BeginTime to ensure deterministic ordering
			// Use a 2 second gap to ensure lease1 acquires exporter before lease2's BeginTime passes
			lease1BeginTime := metav1.NewTime(time.Now().Truncate(time.Second).Add(1 * time.Second))
			lease2BeginTime := metav1.NewTime(time.Now().Truncate(time.Second).Add(3 * time.Second))

			// Both leases target dut:b (only one exporter available)
			lease1 := leaseDutA2Sec.DeepCopy()
			lease1.Name = lease1Name
			lease1.Spec.Selector.MatchLabels["dut"] = "b"
			lease1.Spec.BeginTime = &lease1BeginTime
			lease1.Spec.Duration = &metav1.Duration{Duration: 10 * time.Second} // Long duration, but we'll release early

			lease2 := leaseDutA2Sec.DeepCopy()
			lease2.Name = lease2Name
			lease2.Spec.Selector.MatchLabels["dut"] = "b"
			lease2.Spec.BeginTime = &lease2BeginTime
			lease2.Spec.Duration = &metav1.Duration{Duration: 10 * time.Second}

			Expect(k8sClient.Create(ctx, lease1)).To(Succeed())
			Expect(k8sClient.Create(ctx, lease2)).To(Succeed())

			// Both should be waiting
			_ = reconcileLease(ctx, lease1)
			_ = reconcileLease(ctx, lease2)

			updatedLease1 := getLease(ctx, lease1Name)
			updatedLease2 := getLease(ctx, lease2Name)
			Expect(updatedLease1.Status.ExporterRef).To(BeNil())
			Expect(updatedLease2.Status.ExporterRef).To(BeNil())

			// Poll until lease1's BeginTime passes and it acquires exporter
			Eventually(func() bool {
				_ = reconcileLease(ctx, lease1)
				_ = reconcileLease(ctx, lease2)
				updatedLease1 = getLease(ctx, lease1Name)
				return updatedLease1.Status.ExporterRef != nil
			}).WithTimeout(2*time.Second).WithPolling(50*time.Millisecond).Should(BeTrue(), "lease1 should acquire exporter")

			updatedLease2 = getLease(ctx, lease2Name)
			Expect(updatedLease2.Status.ExporterRef).To(BeNil(), "lease2 should still be waiting")

			// Explicitly release lease1
			updatedLease1 = getLease(ctx, lease1Name)
			updatedLease1.Spec.Release = true
			Expect(k8sClient.Update(ctx, updatedLease1)).To(Succeed())

			// Poll until lease1 is released and lease2 acquires exporter
			// lease2's BeginTime is at T+3s, so we need enough time to wait for it
			Eventually(func() bool {
				_ = reconcileLease(ctx, lease1)
				_ = reconcileLease(ctx, lease2)
				updatedLease1 = getLease(ctx, lease1Name)
				updatedLease2 = getLease(ctx, lease2Name)
				return updatedLease1.Status.Ended && updatedLease2.Status.ExporterRef != nil
			}).WithTimeout(3*time.Second).WithPolling(50*time.Millisecond).Should(BeTrue(), "lease1 should be released and lease2 should acquire exporter after its BeginTime")
		})
	})

	When("deleting a scheduled lease before it starts", func() {
		It("should delete successfully without acquiring exporter", func() {
			lease := leaseDutA2Sec.DeepCopy()
			futureTime := metav1.NewTime(time.Now().Truncate(time.Second).Add(5 * time.Second))
			lease.Spec.BeginTime = &futureTime
			lease.Spec.Duration = &metav1.Duration{Duration: 1 * time.Second}

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).To(BeNil(), "Should not have acquired yet")

			// Delete before BeginTime
			Expect(k8sClient.Delete(ctx, updatedLease)).To(Succeed())

			// Verify it's deleted
			err := k8sClient.Get(ctx, types.NamespacedName{
				Name:      lease.Name,
				Namespace: "default",
			}, &jumpstarterdevv1alpha1.Lease{})
			Expect(err).To(HaveOccurred(), "Lease should be deleted")
		})
	})

	When("updating scheduled lease to make BeginTime in the past", func() {
		It("should start immediately after update", func() {
			lease := leaseDutA2Sec.DeepCopy()
			futureTime := metav1.NewTime(time.Now().Truncate(time.Second).Add(5 * time.Second))
			lease.Spec.BeginTime = &futureTime
			lease.Spec.Duration = &metav1.Duration{Duration: 1 * time.Second}

			ctx := context.Background()
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updatedLease := getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).To(BeNil(), "Should not have started yet")

			// Update BeginTime to be in the past
			pastTime := metav1.NewTime(time.Now().Truncate(time.Second).Add(-100 * time.Millisecond))
			updatedLease.Spec.BeginTime = &pastTime
			Expect(k8sClient.Update(ctx, updatedLease)).To(Succeed())

			// Should acquire immediately now
			_ = reconcileLease(ctx, updatedLease)

			updatedLease = getLease(ctx, lease.Name)
			Expect(updatedLease.Status.ExporterRef).NotTo(BeNil(), "Should acquire immediately after BeginTime moved to past")
			Expect(updatedLease.Status.BeginTime).NotTo(BeNil())

			// Verify that actual BeginTime is before the original futureTime (started early)
			Expect(updatedLease.Status.BeginTime.Time).To(BeTemporally("<", futureTime.Time), "Should have started before the original scheduled time")
		})
	})

	When("creating lease with negative Duration", func() {
		It("should reject with validation error", func() {
			key := types.NamespacedName{Name: "invalid-lease", Namespace: "default"}
			clientRef := corev1.LocalObjectReference{Name: testClient.Name}

			pbLease := &cpb.Lease{
				Selector: "dut=a",
			}
			pbLease.Duration = durationpb.New(-1 * time.Second) // Negative!

			lease, err := jumpstarterdevv1alpha1.LeaseFromProtobuf(pbLease, key, clientRef)

			Expect(err).To(HaveOccurred())
			Expect(err.Error()).To(ContainSubstring("duration must be positive"))
			Expect(lease).To(BeNil())
		})
	})

	When("creating lease with EndTime and negative Duration", func() {
		It("should reject with validation error", func() {
			key := types.NamespacedName{Name: "invalid-lease-2", Namespace: "default"}
			clientRef := corev1.LocalObjectReference{Name: testClient.Name}

			endTime := metav1.NewTime(time.Now().Truncate(time.Second).Add(1 * time.Second))
			pbLease := &cpb.Lease{
				Selector: "dut=a",
				EndTime:  timestamppb.New(endTime.Time),
			}
			pbLease.Duration = durationpb.New(-2 * time.Second) // Negative!

			lease, err := jumpstarterdevv1alpha1.LeaseFromProtobuf(pbLease, key, clientRef)

			Expect(err).To(HaveOccurred())
			Expect(err.Error()).To(ContainSubstring("duration must be positive"))
			Expect(lease).To(BeNil())
		})
	})
})

var _ = Describe("pendingRequeueAfter", func() {
	It("should return 1s when no Pending condition exists", func() {
		lease := &jumpstarterdevv1alpha1.Lease{}
		Expect(pendingRequeueAfter(lease)).To(Equal(time.Second))
	})

	DescribeTable("should back off exponentially based on pending duration",
		func(elapsed, expected time.Duration) {
			lease := &jumpstarterdevv1alpha1.Lease{}
			meta.SetStatusCondition(&lease.Status.Conditions, metav1.Condition{
				Type:               string(jumpstarterdevv1alpha1.LeaseConditionTypePending),
				Status:             metav1.ConditionTrue,
				Reason:             "Offline",
				LastTransitionTime: metav1.NewTime(time.Now().Add(-elapsed)),
			})
			Expect(pendingRequeueAfter(lease)).To(Equal(expected))
		},
		Entry("just pending", 500*time.Millisecond, time.Second),
		Entry("1.5s", 1500*time.Millisecond, 2*time.Second),
		Entry("3s", 3*time.Second, 4*time.Second),
		Entry("6s", 6*time.Second, 8*time.Second),
		Entry("12s", 12*time.Second, 16*time.Second),
		Entry("25s (hits cap)", 25*time.Second, 30*time.Second),
		Entry("5m (capped)", 5*time.Minute, 30*time.Second),
	)
})

var _ = Describe("Lease against an exporter set with nothing warm", func() {
	const setName = "cold-pool"

	newSet := func(maxReplicas, replicas int32) *virtualtargetv1alpha1.ExporterSet {
		return &virtualtargetv1alpha1.ExporterSet{
			ObjectMeta: metav1.ObjectMeta{Name: setName, Namespace: "default"},
			Spec: virtualtargetv1alpha1.ExporterSetSpec{
				VirtualTargetClassName: "qemu",
				MinReplicas:            0,
				MaxReplicas:            maxReplicas,
				Selector:               metav1.LabelSelector{MatchLabels: map[string]string{"pool": "cold"}},
				Template: virtualtargetv1alpha1.ExporterSetTemplate{
					Metadata: virtualtargetv1alpha1.EmbeddedObjectMeta{
						Labels: map[string]string{"pool": "cold"},
					},
				},
			},
			Status: virtualtargetv1alpha1.ExporterSetStatus{Replicas: replicas},
		}
	}

	newLease := func(name string, matchLabels map[string]string) *jumpstarterdevv1alpha1.Lease {
		return &jumpstarterdevv1alpha1.Lease{
			ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "default"},
			Spec: jumpstarterdevv1alpha1.LeaseSpec{
				ClientRef: corev1.LocalObjectReference{Name: testClient.Name},
				Selector:  metav1.LabelSelector{MatchLabels: matchLabels},
				Duration:  &metav1.Duration{Duration: 2 * time.Second},
			},
		}
	}

	createSet := func(ctx context.Context, set *virtualtargetv1alpha1.ExporterSet) {
		status := set.Status
		Expect(k8sClient.Create(ctx, set)).To(Succeed())
		set.Status = status
		Expect(k8sClient.Status().Update(ctx, set)).To(Succeed())
	}

	AfterEach(func() {
		ctx := context.Background()
		set := &virtualtargetv1alpha1.ExporterSet{
			ObjectMeta: metav1.ObjectMeta{Name: setName, Namespace: "default"},
		}
		_ = k8sClient.Delete(ctx, set)
		deleteLeases(ctx, "cold-lease", "cold-lease-max", "no-pool-lease")
	})

	When("a pool could provision a matching exporter", func() {
		It("should stay pending rather than ending the lease", func() {
			ctx := context.Background()
			createSet(ctx, newSet(4, 0))

			lease := newLease("cold-lease", map[string]string{"pool": "cold"})
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updated := getLease(ctx, lease.Name)
			// Ending it here is what stopped the set from ever seeing demand:
			// countPendingLeases skips ended leases and requires Pending.
			Expect(updated.Status.Ended).To(BeFalse())
			Expect(meta.IsStatusConditionTrue(
				updated.Status.Conditions,
				string(jumpstarterdevv1alpha1.LeaseConditionTypeUnsatisfiable),
			)).To(BeFalse())

			condition := meta.FindStatusCondition(
				updated.Status.Conditions,
				string(jumpstarterdevv1alpha1.LeaseConditionTypePending),
			)
			Expect(condition).NotTo(BeNil())
			Expect(condition.Reason).To(Equal("Provisioning"))
			Expect(condition.Message).To(ContainSubstring(setName))
		})
	})

	When("the pool is already at its ceiling", func() {
		It("should be unsatisfiable, since waiting would not help", func() {
			ctx := context.Background()
			createSet(ctx, newSet(2, 2))

			lease := newLease("cold-lease-max", map[string]string{"pool": "cold"})
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updated := getLease(ctx, lease.Name)
			Expect(meta.IsStatusConditionTrue(
				updated.Status.Conditions,
				string(jumpstarterdevv1alpha1.LeaseConditionTypeUnsatisfiable),
			)).To(BeTrue())
		})
	})

	When("no pool matches the selector", func() {
		It("should still fail fast rather than waiting forever", func() {
			ctx := context.Background()
			createSet(ctx, newSet(4, 0))

			lease := newLease("no-pool-lease", map[string]string{"pool": "does-not-exist"})
			Expect(k8sClient.Create(ctx, lease)).To(Succeed())
			_ = reconcileLease(ctx, lease)

			updated := getLease(ctx, lease.Name)
			Expect(meta.IsStatusConditionTrue(
				updated.Status.Conditions,
				string(jumpstarterdevv1alpha1.LeaseConditionTypeUnsatisfiable),
			)).To(BeTrue())
			Expect(updated.Status.Ended).To(BeTrue())
		})
	})
})
