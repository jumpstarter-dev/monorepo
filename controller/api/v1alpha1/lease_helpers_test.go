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
	"testing"
	"time"

	cpb "github.com/jumpstarter-dev/jumpstarter-controller/internal/protocol/jumpstarter/client/v1"
	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
	"google.golang.org/protobuf/types/known/durationpb"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/types"
)

func TestLeaseHelpers(t *testing.T) {
	RegisterFailHandler(Fail)
	RunSpecs(t, "Lease Helpers Suite")
}

var _ = Describe("ParseLabelSelector", func() {
	Context("when parsing simple selectors", func() {
		It("should parse a single key=value selector", func() {
			selector, err := ParseLabelSelector("app=myapp")
			Expect(err).NotTo(HaveOccurred())
			Expect(selector).NotTo(BeNil())
			Expect(selector.MatchLabels).To(HaveKeyWithValue("app", "myapp"))
			Expect(selector.MatchExpressions).To(BeEmpty())
		})

		It("should parse multiple key=value selectors", func() {
			selector, err := ParseLabelSelector("app=myapp,env=prod")
			Expect(err).NotTo(HaveOccurred())
			Expect(selector).NotTo(BeNil())
			Expect(selector.MatchLabels).To(HaveKeyWithValue("app", "myapp"))
			Expect(selector.MatchLabels).To(HaveKeyWithValue("env", "prod"))
			Expect(selector.MatchExpressions).To(BeEmpty())
		})

		It("should handle selectors with spaces", func() {
			selector, err := ParseLabelSelector("app = myapp , env = prod")
			Expect(err).NotTo(HaveOccurred())
			Expect(selector).NotTo(BeNil())
			Expect(selector.MatchLabels).To(HaveKeyWithValue("app", "myapp"))
			Expect(selector.MatchLabels).To(HaveKeyWithValue("env", "prod"))
		})
	})

	Context("when parsing != operator (the bug fix)", func() {
		It("should parse != operator correctly", func() {
			selector, err := ParseLabelSelector("revision!=v3")
			Expect(err).NotTo(HaveOccurred())
			Expect(selector).NotTo(BeNil())
			Expect(selector.MatchExpressions).To(HaveLen(1))
			Expect(selector.MatchExpressions[0].Key).To(Equal("revision"))
			Expect(selector.MatchExpressions[0].Operator).To(Equal(metav1.LabelSelectorOpNotIn))
			Expect(selector.MatchExpressions[0].Values).To(Equal([]string{"v3"}))
		})

		It("should parse != operator with other selectors", func() {
			selector, err := ParseLabelSelector("board-type=qc8775,revision!=v3")
			Expect(err).NotTo(HaveOccurred())
			Expect(selector).NotTo(BeNil())
			Expect(selector.MatchLabels).To(HaveKeyWithValue("board-type", "qc8775"))
			Expect(selector.MatchExpressions).To(HaveLen(1))
			Expect(selector.MatchExpressions[0].Key).To(Equal("revision"))
			Expect(selector.MatchExpressions[0].Operator).To(Equal(metav1.LabelSelectorOpNotIn))
			Expect(selector.MatchExpressions[0].Values).To(Equal([]string{"v3"}))
		})

		It("should parse multiple != operators", func() {
			selector, err := ParseLabelSelector("revision!=v3,board-type!=qc8774")
			Expect(err).NotTo(HaveOccurred())
			Expect(selector).NotTo(BeNil())
			Expect(selector.MatchExpressions).To(HaveLen(2))

			// Find expressions by key
			var revExpr, boardExpr *metav1.LabelSelectorRequirement
			for i := range selector.MatchExpressions {
				if selector.MatchExpressions[i].Key == "revision" {
					revExpr = &selector.MatchExpressions[i]
				}
				if selector.MatchExpressions[i].Key == "board-type" {
					boardExpr = &selector.MatchExpressions[i]
				}
			}

			Expect(revExpr).NotTo(BeNil())
			Expect(revExpr.Operator).To(Equal(metav1.LabelSelectorOpNotIn))
			Expect(revExpr.Values).To(Equal([]string{"v3"}))

			Expect(boardExpr).NotTo(BeNil())
			Expect(boardExpr.Operator).To(Equal(metav1.LabelSelectorOpNotIn))
			Expect(boardExpr.Values).To(Equal([]string{"qc8774"}))
		})
	})

	Context("when parsing In and NotIn operators", func() {
		It("should parse In operator", func() {
			selector, err := ParseLabelSelector("env in (prod,staging)")
			Expect(err).NotTo(HaveOccurred())
			Expect(selector).NotTo(BeNil())
			Expect(selector.MatchExpressions).To(HaveLen(1))
			Expect(selector.MatchExpressions[0].Key).To(Equal("env"))
			Expect(selector.MatchExpressions[0].Operator).To(Equal(metav1.LabelSelectorOpIn))
			Expect(selector.MatchExpressions[0].Values).To(ContainElements("prod", "staging"))
		})

		It("should parse NotIn operator", func() {
			selector, err := ParseLabelSelector("env notin (dev,test)")
			Expect(err).NotTo(HaveOccurred())
			Expect(selector).NotTo(BeNil())
			Expect(selector.MatchExpressions).To(HaveLen(1))
			Expect(selector.MatchExpressions[0].Key).To(Equal("env"))
			Expect(selector.MatchExpressions[0].Operator).To(Equal(metav1.LabelSelectorOpNotIn))
			Expect(selector.MatchExpressions[0].Values).To(ContainElements("dev", "test"))
		})
	})

	Context("when parsing Exists and DoesNotExist operators", func() {
		It("should parse Exists operator", func() {
			selector, err := ParseLabelSelector("app")
			Expect(err).NotTo(HaveOccurred())
			Expect(selector).NotTo(BeNil())
			Expect(selector.MatchExpressions).To(HaveLen(1))
			Expect(selector.MatchExpressions[0].Key).To(Equal("app"))
			Expect(selector.MatchExpressions[0].Operator).To(Equal(metav1.LabelSelectorOpExists))
			Expect(selector.MatchExpressions[0].Values).To(BeEmpty())
		})

		It("should parse DoesNotExist operator", func() {
			selector, err := ParseLabelSelector("!app")
			Expect(err).NotTo(HaveOccurred())
			Expect(selector).NotTo(BeNil())
			Expect(selector.MatchExpressions).To(HaveLen(1))
			Expect(selector.MatchExpressions[0].Key).To(Equal("app"))
			Expect(selector.MatchExpressions[0].Operator).To(Equal(metav1.LabelSelectorOpDoesNotExist))
			Expect(selector.MatchExpressions[0].Values).To(BeEmpty())
		})
	})

	Context("when parsing complex selectors", func() {
		It("should parse a mix of matchLabels and matchExpressions", func() {
			selector, err := ParseLabelSelector("app=myapp,env!=prod")
			Expect(err).NotTo(HaveOccurred())
			Expect(selector).NotTo(BeNil())
			Expect(selector.MatchLabels).To(HaveKeyWithValue("app", "myapp"))
			Expect(selector.MatchExpressions).To(HaveLen(1))
			Expect(selector.MatchExpressions[0].Key).To(Equal("env"))
			Expect(selector.MatchExpressions[0].Operator).To(Equal(metav1.LabelSelectorOpNotIn))
		})

		It("should parse selector with all operator types", func() {
			selector, err := ParseLabelSelector("app=myapp,revision!=v3,env in (prod,staging),!debug")
			Expect(err).NotTo(HaveOccurred())
			Expect(selector).NotTo(BeNil())
			Expect(selector.MatchLabels).To(HaveKeyWithValue("app", "myapp"))
			Expect(selector.MatchExpressions).To(HaveLen(3))
		})
	})

	Context("when parsing edge cases", func() {
		It("should handle empty selector", func() {
			selector, err := ParseLabelSelector("")
			Expect(err).NotTo(HaveOccurred())
			Expect(selector).NotTo(BeNil())
			Expect(selector.MatchLabels).To(BeEmpty())
			Expect(selector.MatchExpressions).To(BeEmpty())
		})

		It("should handle selector with special characters in values", func() {
			selector, err := ParseLabelSelector("version=v1.2.3,label=my-label")
			Expect(err).NotTo(HaveOccurred())
			Expect(selector).NotTo(BeNil())
			Expect(selector.MatchLabels).To(HaveKeyWithValue("version", "v1.2.3"))
			Expect(selector.MatchLabels).To(HaveKeyWithValue("label", "my-label"))
		})

		It("should handle selector with underscores in keys", func() {
			selector, err := ParseLabelSelector("board_type=qc8775,device_id=123")
			Expect(err).NotTo(HaveOccurred())
			Expect(selector).NotTo(BeNil())
			Expect(selector.MatchLabels).To(HaveKeyWithValue("board_type", "qc8775"))
			Expect(selector.MatchLabels).To(HaveKeyWithValue("device_id", "123"))
		})
	})

	Context("when parsing invalid selectors", func() {
		It("should return error for invalid syntax", func() {
			selector, err := ParseLabelSelector("invalid===syntax")
			Expect(err).To(HaveOccurred())
			Expect(selector).To(BeNil())
		})

		It("should reject repeated equality requirements on the same key with different values", func() {
			selector, err := ParseLabelSelector("a=1,a=2")
			Expect(err).To(HaveOccurred())
			Expect(err.Error()).To(ContainSubstring("cannot have multiple equality requirements"))
			Expect(err.Error()).To(ContainSubstring("a"))
			Expect(selector).To(BeNil())
		})

		It("should accept repeated equality requirements on the same key with the same value", func() {
			selector, err := ParseLabelSelector("a=1,a=1")
			Expect(err).NotTo(HaveOccurred())
			Expect(selector).NotTo(BeNil())
			Expect(selector.MatchLabels).To(HaveKeyWithValue("a", "1"))
		})

		It("should combine multiple != operators for the same key into NotIn", func() {
			selector, err := ParseLabelSelector("key!=value1,key!=value2")
			Expect(err).NotTo(HaveOccurred())
			Expect(selector).NotTo(BeNil())
			Expect(selector.MatchExpressions).To(HaveLen(1))
			Expect(selector.MatchExpressions[0].Key).To(Equal("key"))
			Expect(selector.MatchExpressions[0].Operator).To(Equal(metav1.LabelSelectorOpNotIn))
			Expect(selector.MatchExpressions[0].Values).To(ConsistOf("value1", "value2"))
		})
	})

	Context("round-trip compatibility", func() {
		It("should produce a selector that can be converted back to labels.Selector", func() {
			originalStr := "board-type=qc8775,revision!=v3"
			selector, err := ParseLabelSelector(originalStr)
			Expect(err).NotTo(HaveOccurred())
			Expect(selector).NotTo(BeNil())

			// Convert back to labels.Selector using the standard Kubernetes function
			parsedSelector, err := metav1.LabelSelectorAsSelector(selector)
			Expect(err).NotTo(HaveOccurred())
			Expect(parsedSelector).NotTo(BeNil())

			// Verify it matches the expected labels
			testLabels := labels.Set{
				"board-type": "qc8775",
				"revision":   "v3",
			}
			// Should NOT match because revision!=v3
			Expect(parsedSelector.Matches(testLabels)).To(BeFalse())

			testLabels2 := labels.Set{
				"board-type": "qc8775",
				"revision":   "v2",
			}
			// Should match because revision is v2, not v3
			Expect(parsedSelector.Matches(testLabels2)).To(BeTrue())
		})

		It("should match labels correctly for != operator", func() {
			selector, err := ParseLabelSelector("revision!=v3")
			Expect(err).NotTo(HaveOccurred())

			parsedSelector, err := metav1.LabelSelectorAsSelector(selector)
			Expect(err).NotTo(HaveOccurred())

			// Should match labels without revision=v3
			Expect(parsedSelector.Matches(labels.Set{"revision": "v2"})).To(BeTrue())
			Expect(parsedSelector.Matches(labels.Set{"revision": "v4"})).To(BeTrue())

			// Should not match labels with revision=v3
			Expect(parsedSelector.Matches(labels.Set{"revision": "v3"})).To(BeFalse())
			Expect(parsedSelector.Matches(labels.Set{"revision": "v3", "other": "value"})).To(BeFalse())
		})
	})
})

var _ = Describe("LeaseFromProtobuf", func() {
	Context("when creating a lease with selector labels", func() {
		It("should copy selector matchLabels to lease metadata labels", func() {
			pbLease := &cpb.Lease{
				Selector: "board-type=virtual,env=test",
				Duration: durationpb.New(time.Hour),
			}
			key := types.NamespacedName{Name: "test-lease", Namespace: "default"}
			clientRef := corev1.LocalObjectReference{Name: "test-client"}

			lease, err := LeaseFromProtobuf(pbLease, key, clientRef)

			Expect(err).NotTo(HaveOccurred())
			Expect(lease).NotTo(BeNil())
			Expect(lease.Labels).To(HaveKeyWithValue("board-type", "virtual"))
			Expect(lease.Labels).To(HaveKeyWithValue("env", "test"))
		})

		It("should handle selector with only matchExpressions (no matchLabels)", func() {
			pbLease := &cpb.Lease{
				Selector: "env!=prod", // Only != operator, results in no matchLabels
				Duration: durationpb.New(time.Hour),
			}
			key := types.NamespacedName{Name: "test-lease", Namespace: "default"}
			clientRef := corev1.LocalObjectReference{Name: "test-client"}

			lease, err := LeaseFromProtobuf(pbLease, key, clientRef)

			Expect(err).NotTo(HaveOccurred())
			Expect(lease).NotTo(BeNil())
			Expect(lease.Labels).To(BeEmpty()) // nil or empty map is fine
		})
	})
})
