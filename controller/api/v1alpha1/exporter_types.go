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

package v1alpha1

import (
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// EDIT THIS FILE!  THIS IS SCAFFOLDING FOR YOU TO OWN!
// NOTE: json tags are required.  Any new fields you add must have json tags for the fields to be serialized.

// ExporterSpec defines the desired state of Exporter
type ExporterSpec struct {
	Username *string `json:"username,omitempty"`
}

// ExporterStatus defines the observed state of Exporter
type ExporterStatus struct {
	// Exporter status fields
	Conditions []metav1.Condition           `json:"conditions,omitempty" patchStrategy:"merge" patchMergeKey:"type"`
	Credential *corev1.LocalObjectReference `json:"credential,omitempty"`
	Devices    []Device                     `json:"devices,omitempty"`
	LeaseRef   *corev1.LocalObjectReference `json:"leaseRef,omitempty"`
	LastSeen   metav1.Time                  `json:"lastSeen,omitempty"`
	Endpoint   string                       `json:"endpoint,omitempty"`
	// ExporterStatusValue is the current operational status reported by the exporter
	// +kubebuilder:validation:Enum=Unspecified;Offline;Available;BeforeLeaseHook;LeaseReady;AfterLeaseHook;BeforeLeaseHookFailed;AfterLeaseHookFailed
	ExporterStatusValue string `json:"exporterStatus,omitempty"`
	// StatusMessage is an optional human-readable message describing the current state
	StatusMessage string `json:"statusMessage,omitempty"`
}

type ExporterConditionType string

const (
	ExporterConditionTypeRegistered ExporterConditionType = "Registered"
	ExporterConditionTypeOnline     ExporterConditionType = "Online"
)

// ExporterStatus values - PascalCase for Kubernetes, converted from proto ALL_CAPS
const (
	ExporterStatusUnspecified           = "Unspecified"
	ExporterStatusOffline               = "Offline"
	ExporterStatusAvailable             = "Available"
	ExporterStatusBeforeLeaseHook       = "BeforeLeaseHook"
	ExporterStatusLeaseReady            = "LeaseReady"
	ExporterStatusAfterLeaseHook        = "AfterLeaseHook"
	ExporterStatusBeforeLeaseHookFailed = "BeforeLeaseHookFailed"
	ExporterStatusAfterLeaseHookFailed  = "AfterLeaseHookFailed"
)

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="Status",type="string",JSONPath=".status.exporterStatus"
// +kubebuilder:printcolumn:name="Message",type="string",JSONPath=".status.statusMessage",priority=1

// Exporter is the Schema for the exporters API
type Exporter struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   ExporterSpec   `json:"spec,omitempty"`
	Status ExporterStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// ExporterList contains a list of Exporter
type ExporterList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []Exporter `json:"items"`
}

func init() {
	SchemeBuilder.Register(&Exporter{}, &ExporterList{})
}
