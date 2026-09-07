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

// Package disk provides shared helpers for guest-disk volume provisioning
// used by ExporterSet provisioners.
//
// Guest disk config lives under parameters.storage (JEP-0014):
//
//	parameters:
//	  storage:
//	    size: 20Gi
//	    fsOverhead: "10%"           # default; "0%" disables inflation
//	    storageClassName: gp3       # omit or "" → sized emptyDir
//	    accessModes: ["ReadWriteOnce"]
//
// When storageClassName is set, the volume is a generic ephemeral PVC
// (volumeClaimTemplate) so its lifetime follows the Pod (ExitAndReplace).
package disk

import (
	"fmt"
	"strconv"
	"strings"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
)

const (
	// VolumeName is the Pod volume name for guest disk storage.
	VolumeName = "disk"

	// MountPath is where guest disk storage is mounted in exporter and runtime.
	MountPath = "/disk"

	// DefaultSize is used when parameters.storage.size is unset.
	DefaultSize = "10Gi"

	// DefaultFSOverhead is applied to volume requests when fsOverhead is omitted.
	DefaultFSOverhead = "10%"
)

// Spec is the resolved guest-disk volume configuration.
type Spec struct {
	// Size is the logical guest disk size (parameters.storage.size).
	Size resource.Quantity
	// VolumeSize is the physical allocation (Size plus fsOverhead).
	VolumeSize       resource.Quantity
	StorageClassName string
	AccessModes      []corev1.PersistentVolumeAccessMode
}

// UsePVC reports whether the disk is backed by an ephemeral PVC.
func (s Spec) UsePVC() bool {
	return s.StorageClassName != ""
}

// Mount returns the VolumeMount for /disk.
func Mount() corev1.VolumeMount {
	return corev1.VolumeMount{
		Name:      VolumeName,
		MountPath: MountPath,
	}
}

// FromParameters reads disk size and optional storage backend from merged
// ExporterSet/VirtualTargetClass parameters.
func FromParameters(params map[string]interface{}) (Spec, error) {
	spec := Spec{
		AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
	}
	if params == nil {
		size, err := parseSize("")
		if err != nil {
			return Spec{}, err
		}
		spec.Size = size
		spec.VolumeSize = applyOverhead(size, defaultOverheadPercent())
		return spec, nil
	}
	storage, ok := params["storage"].(map[string]interface{})
	if !ok {
		if params["storage"] != nil {
			return Spec{}, fmt.Errorf("parameters.storage must be an object, got %T", params["storage"])
		}
		size, err := parseSize("")
		if err != nil {
			return Spec{}, err
		}
		spec.Size = size
		spec.VolumeSize = applyOverhead(size, defaultOverheadPercent())
		return spec, nil
	}

	sizeRaw := ""
	if raw, exists := storage["size"]; exists && raw != nil {
		switch v := raw.(type) {
		case string:
			sizeRaw = v
		case float64:
			return Spec{}, fmt.Errorf("parameters.storage.size must be a string quantity (e.g. \"10Gi\"), got number %v", v)
		default:
			return Spec{}, fmt.Errorf("parameters.storage.size must be a string quantity (e.g. \"10Gi\"), got %T", v)
		}
	}
	size, err := parseSize(sizeRaw)
	if err != nil {
		return Spec{}, err
	}
	spec.Size = size

	overheadPercent, err := parseFSOverhead(storage["fsOverhead"])
	if err != nil {
		return Spec{}, err
	}
	spec.VolumeSize = applyOverhead(size, overheadPercent)

	switch v := storage["storageClassName"].(type) {
	case string:
		spec.StorageClassName = v
	case nil:
		// omit → emptyDir
	default:
		return Spec{}, fmt.Errorf("parameters.storage.storageClassName must be a string, got %T", v)
	}

	if raw, exists := storage["accessModes"]; exists && raw != nil {
		modes, err := parseAccessModes(raw)
		if err != nil {
			return Spec{}, err
		}
		spec.AccessModes = modes
	}
	return spec, nil
}

// SizeFromParameters reads parameters.storage.size, defaulting to DefaultSize.
func SizeFromParameters(params map[string]interface{}) (resource.Quantity, error) {
	spec, err := FromParameters(params)
	if err != nil {
		return resource.Quantity{}, err
	}
	return spec.Size, nil
}

// Volume builds the guest-disk Pod volume from spec.
func Volume(spec Spec) corev1.Volume {
	vol := corev1.Volume{Name: VolumeName}
	if spec.UsePVC() {
		sc := spec.StorageClassName
		vol.VolumeSource = corev1.VolumeSource{
			Ephemeral: &corev1.EphemeralVolumeSource{
				VolumeClaimTemplate: &corev1.PersistentVolumeClaimTemplate{
					Spec: corev1.PersistentVolumeClaimSpec{
						AccessModes:      spec.AccessModes,
						StorageClassName: &sc,
						Resources: corev1.VolumeResourceRequirements{
							Requests: corev1.ResourceList{
								corev1.ResourceStorage: spec.VolumeSize,
							},
						},
					},
				},
			},
		}
		return vol
	}
	size := spec.VolumeSize.DeepCopy()
	vol.VolumeSource = corev1.VolumeSource{
		EmptyDir: &corev1.EmptyDirVolumeSource{
			SizeLimit: &size,
		},
	}
	return vol
}

// SetEphemeralStorage ensures requests and limits include ephemeral-storage
// equal to size (used when guest disk is backed by emptyDir).
func SetEphemeralStorage(resources *corev1.ResourceRequirements, size resource.Quantity) {
	if resources.Requests == nil {
		resources.Requests = corev1.ResourceList{}
	}
	if resources.Limits == nil {
		resources.Limits = corev1.ResourceList{}
	}
	// Only set when unset so explicit scheduling.resources win.
	if _, ok := resources.Requests[corev1.ResourceEphemeralStorage]; !ok {
		resources.Requests[corev1.ResourceEphemeralStorage] = size.DeepCopy()
	}
	if _, ok := resources.Limits[corev1.ResourceEphemeralStorage]; !ok {
		resources.Limits[corev1.ResourceEphemeralStorage] = size.DeepCopy()
	}
}

func parseSize(raw string) (resource.Quantity, error) {
	if raw == "" {
		raw = DefaultSize
	}
	qty, err := resource.ParseQuantity(raw)
	if err != nil {
		return resource.Quantity{}, fmt.Errorf("parse parameters.storage.size %q: %w", raw, err)
	}
	return qty, nil
}

func defaultOverheadPercent() int {
	percent, err := parseFSOverhead(nil)
	if err != nil {
		return 10
	}
	return percent
}

func parseFSOverhead(v interface{}) (int, error) {
	raw := DefaultFSOverhead
	if v != nil {
		s, ok := v.(string)
		if !ok {
			return 0, fmt.Errorf("parameters.storage.fsOverhead must be a percentage string (e.g. \"10%%\"), got %T", v)
		}
		raw = s
	}
	raw = strings.TrimSpace(raw)
	if raw == "" {
		raw = DefaultFSOverhead
	}
	if !strings.HasSuffix(raw, "%") {
		return 0, fmt.Errorf("parameters.storage.fsOverhead must be a percentage string (e.g. \"10%%\"), got %q", raw)
	}
	percentStr := strings.TrimSpace(strings.TrimSuffix(raw, "%"))
	if percentStr == "" {
		return 0, fmt.Errorf("parameters.storage.fsOverhead must be a percentage string (e.g. \"10%%\"), got %q", raw)
	}
	percent, err := strconv.Atoi(percentStr)
	if err != nil {
		return 0, fmt.Errorf("parse parameters.storage.fsOverhead %q: %w", raw, err)
	}
	if percent < 0 {
		return 0, fmt.Errorf("parameters.storage.fsOverhead must be non-negative, got %q", raw)
	}
	return percent, nil
}

func applyOverhead(size resource.Quantity, overheadPercent int) resource.Quantity {
	if overheadPercent == 0 {
		return size.DeepCopy()
	}
	inflated := size.Value() * int64(100+overheadPercent) / 100
	return *resource.NewQuantity(inflated, size.Format)
}

func parseAccessModes(v interface{}) ([]corev1.PersistentVolumeAccessMode, error) {
	items, ok := v.([]interface{})
	if !ok {
		return nil, fmt.Errorf("parameters.storage.accessModes must be a list of strings, got %T", v)
	}
	if len(items) == 0 {
		return nil, fmt.Errorf("parameters.storage.accessModes must not be empty")
	}
	out := make([]corev1.PersistentVolumeAccessMode, 0, len(items))
	for _, item := range items {
		s, ok := item.(string)
		if !ok || s == "" {
			return nil, fmt.Errorf("parameters.storage.accessModes must be a list of strings, got %T", item)
		}
		// Kubernetes validates access modes at PVC creation time.
		out = append(out, corev1.PersistentVolumeAccessMode(s))
	}
	return out, nil
}
