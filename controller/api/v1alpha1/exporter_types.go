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

// ExporterSpec defines the desired state of Exporter.
type ExporterSpec struct {
	// Username is the identity of the exporter, used for authentication and authorization.
	Username *string `json:"username,omitempty"`
	// Enabled controls whether this exporter is eligible for lease assignment.
	// When set to false, the controller will not assign new leases to this exporter.
	// Useful for temporarily taking an exporter offline for maintenance without deleting it,
	// or for graceful scale-down of virtual exporter pools.
	// +kubebuilder:default=true
	Enabled *bool `json:"enabled,omitempty"`
}

// ExporterStatus defines the observed state of Exporter.
type ExporterStatus struct {
	// Conditions represent the latest available observations of the exporter state.
	Conditions []metav1.Condition `json:"conditions,omitempty" patchStrategy:"merge" patchMergeKey:"type"`
	// Credential is a reference to the secret containing the exporter credentials.
	Credential *corev1.LocalObjectReference `json:"credential,omitempty"`
	// Devices is the list of driver instances currently reported by the exporter.
	Devices []Device `json:"devices,omitempty"`
	// LeaseRef is a reference to the lease currently assigned to this exporter.
	LeaseRef *corev1.LocalObjectReference `json:"leaseRef,omitempty"`
	// LastSeen is the timestamp of the last communication from the exporter.
	LastSeen metav1.Time `json:"lastSeen,omitempty"`
	// Endpoint is the gRPC endpoint URL where the exporter is reachable.
	Endpoint string `json:"endpoint,omitempty"`
	// ExporterStatusValue is the current operational status reported by the exporter
	// +kubebuilder:validation:Enum=Unspecified;Offline;Available;BeforeLeaseHook;LeaseReady;AfterLeaseHook;BeforeLeaseHookFailed;AfterLeaseHookFailed
	ExporterStatusValue string `json:"exporterStatus,omitempty"`
	// StatusMessage is an optional human-readable message describing the current state
	StatusMessage string `json:"statusMessage,omitempty"`
	// TokenExpiresAt is the expiry time of the internal credential token.
	TokenExpiresAt *metav1.Time `json:"tokenExpiresAt,omitempty"`
}

type ExporterConditionType string

const (
	ExporterConditionTypeRegistered    ExporterConditionType = "Registered"
	ExporterConditionTypeOnline        ExporterConditionType = "Online"
	ExporterConditionTypeTokenExpiring ExporterConditionType = "TokenExpiring"
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
// +kubebuilder:printcolumn:name="Enabled",type="boolean",JSONPath=".spec.enabled"
// +kubebuilder:printcolumn:name="Status",type="string",JSONPath=".status.exporterStatus"
// +kubebuilder:printcolumn:name="Message",type="string",JSONPath=".status.statusMessage",priority=1
// +kubebuilder:printcolumn:name="Token Expires",type="string",JSONPath=".status.tokenExpiresAt"

// Exporter is the Schema for the exporters API
type Exporter struct {
	// Exporters represent the services that connect to the physical or virtual
	// devices. They are responsible for providing the access to the devices and
	// for the communication with the devices. A jumpstarter exporter service
	// should be run on a linux machine, or a pod, with the exporter credentials
	// and the right configuration for this resource to become online. For
	// more information see the Jumpstarter documentation:
	// https://jumpstarter.dev/main/introduction/exporters.html#exporters
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
