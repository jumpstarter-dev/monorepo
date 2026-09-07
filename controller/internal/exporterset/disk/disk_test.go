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

package disk

import (
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
)

func TestFromParameters_defaults(t *testing.T) {
	spec, err := FromParameters(nil)
	if err != nil {
		t.Fatalf("nil params: %v", err)
	}
	if !spec.Size.Equal(resource.MustParse(DefaultSize)) {
		t.Errorf("size = %v, want %s", spec.Size, DefaultSize)
	}
	wantVolume := applyOverhead(resource.MustParse(DefaultSize), 10)
	if !spec.VolumeSize.Equal(wantVolume) {
		t.Errorf("VolumeSize = %v, want %v (10%% overhead)", spec.VolumeSize, wantVolume)
	}
	if spec.UsePVC() {
		t.Error("expected emptyDir when storageClassName is unset")
	}
	if len(spec.AccessModes) != 1 || spec.AccessModes[0] != corev1.ReadWriteOnce {
		t.Errorf("accessModes = %v, want [ReadWriteOnce]", spec.AccessModes)
	}
}

func TestFromParameters_storageClassAndSize(t *testing.T) {
	spec, err := FromParameters(map[string]interface{}{
		"storage": map[string]interface{}{
			"size":             "15Gi",
			"storageClassName": "gp3",
			"accessModes":      []interface{}{"ReadWriteOnce", "ReadWriteMany"},
		},
	})
	if err != nil {
		t.Fatalf("FromParameters: %v", err)
	}
	if !spec.Size.Equal(resource.MustParse("15Gi")) {
		t.Errorf("size = %v, want 15Gi", spec.Size)
	}
	if spec.StorageClassName != "gp3" {
		t.Errorf("storageClassName = %q, want gp3", spec.StorageClassName)
	}
	if !spec.UsePVC() {
		t.Error("expected PVC when storageClassName is set")
	}
	if len(spec.AccessModes) != 2 {
		t.Fatalf("accessModes = %v", spec.AccessModes)
	}
}

func TestFromParameters_emptyStorageClassForcesEmptyDir(t *testing.T) {
	spec, err := FromParameters(map[string]interface{}{
		"storage": map[string]interface{}{
			"storageClassName": "",
		},
	})
	if err != nil {
		t.Fatalf("FromParameters: %v", err)
	}
	if spec.UsePVC() {
		t.Error("empty storageClassName should force emptyDir")
	}
}

func TestFromParameters_rejectsNumericStorage(t *testing.T) {
	_, err := FromParameters(map[string]interface{}{
		"storage": map[string]interface{}{"size": 10.0},
	})
	if err == nil {
		t.Fatal("expected error for numeric storage")
	}
}

func TestFromParameters_fsOverhead(t *testing.T) {
	spec, err := FromParameters(map[string]interface{}{
		"storage": map[string]interface{}{
			"size":       "10Gi",
			"fsOverhead": "0%",
		},
	})
	if err != nil {
		t.Fatalf("FromParameters: %v", err)
	}
	if !spec.Size.Equal(resource.MustParse("10Gi")) {
		t.Errorf("size = %v, want 10Gi", spec.Size)
	}
	if !spec.VolumeSize.Equal(resource.MustParse("10Gi")) {
		t.Errorf("VolumeSize = %v, want 10Gi with 0%% overhead", spec.VolumeSize)
	}

	spec, err = FromParameters(map[string]interface{}{
		"storage": map[string]interface{}{
			"size":       "10Gi",
			"fsOverhead": "10%",
		},
	})
	if err != nil {
		t.Fatalf("FromParameters: %v", err)
	}
	wantVolume := applyOverhead(resource.MustParse("10Gi"), 10)
	if !spec.VolumeSize.Equal(wantVolume) {
		t.Errorf("VolumeSize = %v, want %v", spec.VolumeSize, wantVolume)
	}
}

func TestFromParameters_rejectsInvalidFSOverhead(t *testing.T) {
	_, err := FromParameters(map[string]interface{}{
		"storage": map[string]interface{}{
			"fsOverhead": "ten",
		},
	})
	if err == nil {
		t.Fatal("expected error for invalid fsOverhead")
	}
}

func TestVolume_emptyDir(t *testing.T) {
	logical := resource.MustParse("7Gi")
	spec := Spec{
		Size:       logical,
		VolumeSize: applyOverhead(logical, 10),
	}
	vol := Volume(spec)
	if vol.Name != VolumeName {
		t.Errorf("name = %q, want %s", vol.Name, VolumeName)
	}
	if vol.EmptyDir == nil || vol.EmptyDir.SizeLimit == nil {
		t.Fatalf("expected sized emptyDir, got %#v", vol)
	}
	if !vol.EmptyDir.SizeLimit.Equal(spec.VolumeSize) {
		t.Errorf("SizeLimit = %v, want %v", vol.EmptyDir.SizeLimit, spec.VolumeSize)
	}
}

func TestVolume_ephemeralPVC(t *testing.T) {
	sc := "fast-ssd"
	logical := resource.MustParse("20Gi")
	spec := Spec{
		Size:             logical,
		VolumeSize:       applyOverhead(logical, 10),
		StorageClassName: sc,
		AccessModes:      []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
	}
	vol := Volume(spec)
	if vol.Ephemeral == nil || vol.Ephemeral.VolumeClaimTemplate == nil {
		t.Fatalf("expected ephemeral volumeClaimTemplate, got %#v", vol)
	}
	claim := vol.Ephemeral.VolumeClaimTemplate.Spec
	if claim.StorageClassName == nil || *claim.StorageClassName != sc {
		t.Errorf("StorageClassName = %v, want %s", claim.StorageClassName, sc)
	}
	got := claim.Resources.Requests[corev1.ResourceStorage]
	if !got.Equal(spec.VolumeSize) {
		t.Errorf("storage request = %v, want %v", got, spec.VolumeSize)
	}
}

func TestSetEphemeralStorage(t *testing.T) {
	size := resource.MustParse("10Gi")
	res := corev1.ResourceRequirements{
		Requests: corev1.ResourceList{
			corev1.ResourceCPU: resource.MustParse("1"),
		},
	}
	SetEphemeralStorage(&res, size)
	if !res.Requests[corev1.ResourceEphemeralStorage].Equal(size) {
		t.Errorf("request = %v, want %v", res.Requests[corev1.ResourceEphemeralStorage], size)
	}
	if !res.Limits[corev1.ResourceEphemeralStorage].Equal(size) {
		t.Errorf("limit = %v, want %v", res.Limits[corev1.ResourceEphemeralStorage], size)
	}
	if !res.Requests[corev1.ResourceCPU].Equal(resource.MustParse("1")) {
		t.Error("cpu request should be preserved")
	}

	custom := resource.MustParse("1Gi")
	res.Requests[corev1.ResourceEphemeralStorage] = custom
	SetEphemeralStorage(&res, size)
	if !res.Requests[corev1.ResourceEphemeralStorage].Equal(custom) {
		t.Errorf("should preserve explicit ephemeral-storage, got %v", res.Requests[corev1.ResourceEphemeralStorage])
	}
}

func TestFromParameters_mergedEmptyStorageClassForcesEmptyDir(t *testing.T) {
	merged := deepMerge(map[string]interface{}{
		"storage": map[string]interface{}{
			"storageClassName": "gp3",
			"size":             "20Gi",
		},
	}, map[string]interface{}{
		"storage": map[string]interface{}{
			"storageClassName": "",
		},
	})

	spec, err := FromParameters(merged)
	if err != nil {
		t.Fatalf("FromParameters: %v", err)
	}
	if spec.UsePVC() {
		t.Error("empty storageClassName override should force emptyDir")
	}
	if !spec.Size.Equal(resource.MustParse("20Gi")) {
		t.Errorf("size = %v, want 20Gi (inherited from class)", spec.Size)
	}
}

// deepMerge mirrors exporterset.deepMerge for merge-semantics tests.
func deepMerge(base, override map[string]interface{}) map[string]interface{} {
	result := make(map[string]interface{}, len(base)+len(override))
	for k, v := range base {
		result[k] = v
	}
	for k, v := range override {
		if baseMap, ok := result[k].(map[string]interface{}); ok {
			if overrideMap, ok := v.(map[string]interface{}); ok {
				result[k] = deepMerge(baseMap, overrideMap)
				continue
			}
		}
		result[k] = v
	}
	return result
}
