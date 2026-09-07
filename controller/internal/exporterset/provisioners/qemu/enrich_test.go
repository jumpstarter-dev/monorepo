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

package qemu

import (
	"encoding/json"
	"testing"

	virtualtargetv1alpha1 "github.com/jumpstarter-dev/jumpstarter/controller/api/virtualtarget/v1alpha1"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
)

func TestEnrichExporterExport_injectsLauncherSocket(t *testing.T) {
	drivers := []virtualtargetv1alpha1.DriverConfig{
		{
			Name: "qemu",
			Type: qemuDriverType,
			Config: mustJSON(map[string]any{
				"arch": "x86_64",
				"smp":  2,
				"mem":  "2G",
			}),
		},
	}

	result, err := New("dev").EnrichExporterExport(drivers, nil)
	if err != nil {
		t.Fatal(err)
	}

	qemuDriver := findDriver(result, "qemu")
	if qemuDriver == nil {
		t.Fatal("qemu driver not found in result")
	}

	config := unmarshalConfig(t, qemuDriver.Config)
	if got := config["launcher_socket"]; got != launcherSocketPath {
		t.Errorf("launcher_socket = %v, want %v", got, launcherSocketPath)
	}
}

func TestEnrichExporterExport_injectsDefaultPartitionsX86(t *testing.T) {
	drivers := []virtualtargetv1alpha1.DriverConfig{
		{
			Name: "qemu",
			Type: qemuDriverType,
			Config: mustJSON(map[string]any{
				"arch": "x86_64",
			}),
		},
	}

	result, err := New("dev").EnrichExporterExport(drivers, nil)
	if err != nil {
		t.Fatal(err)
	}

	config := unmarshalConfig(t, findDriver(result, "qemu").Config)
	partitions, ok := config["default_partitions"].(map[string]any)
	if !ok {
		t.Fatalf("default_partitions not a map: %T", config["default_partitions"])
	}
	if got := partitions["OVMF_CODE.fd"]; got != "/usr/share/edk2/ovmf/OVMF_CODE.fd" {
		t.Errorf("OVMF_CODE.fd = %v", got)
	}
	if got := partitions["OVMF_VARS.fd"]; got != "/usr/share/edk2/ovmf/OVMF_VARS.fd" {
		t.Errorf("OVMF_VARS.fd = %v", got)
	}
}

func TestEnrichExporterExport_injectsDefaultPartitionsAarch64(t *testing.T) {
	drivers := []virtualtargetv1alpha1.DriverConfig{
		{
			Name: "qemu",
			Type: qemuDriverType,
			Config: mustJSON(map[string]any{
				"arch": "aarch64",
			}),
		},
	}

	result, err := New("dev").EnrichExporterExport(drivers, nil)
	if err != nil {
		t.Fatal(err)
	}

	config := unmarshalConfig(t, findDriver(result, "qemu").Config)
	partitions, ok := config["default_partitions"].(map[string]any)
	if !ok {
		t.Fatalf("default_partitions not a map: %T", config["default_partitions"])
	}
	if got := partitions["OVMF_CODE.fd"]; got != "/usr/share/AAVMF/AAVMF_CODE.fd" {
		t.Errorf("OVMF_CODE.fd = %v", got)
	}
	if got := partitions["OVMF_VARS.fd"]; got != "/usr/share/AAVMF/AAVMF_VARS.fd" {
		t.Errorf("OVMF_VARS.fd = %v", got)
	}
}

func TestEnrichExporterExport_respectsUserDefaultPartitions(t *testing.T) {
	userPartitions := map[string]any{
		"OVMF_CODE.fd": "/custom/path/code.fd",
		"OVMF_VARS.fd": "/custom/path/vars.fd",
	}
	drivers := []virtualtargetv1alpha1.DriverConfig{
		{
			Name: "qemu",
			Type: qemuDriverType,
			Config: mustJSON(map[string]any{
				"arch":               "x86_64",
				"default_partitions": userPartitions,
			}),
		},
	}

	result, err := New("dev").EnrichExporterExport(drivers, nil)
	if err != nil {
		t.Fatal(err)
	}

	config := unmarshalConfig(t, findDriver(result, "qemu").Config)
	partitions, ok := config["default_partitions"].(map[string]any)
	if !ok {
		t.Fatalf("default_partitions not a map: %T", config["default_partitions"])
	}
	if got := partitions["OVMF_CODE.fd"]; got != "/custom/path/code.fd" {
		t.Errorf("OVMF_CODE.fd = %v, user override not preserved", got)
	}
}

func TestEnrichExporterExport_injectsHostfwdSSH(t *testing.T) {
	drivers := []virtualtargetv1alpha1.DriverConfig{
		{
			Name: "qemu",
			Type: qemuDriverType,
			Config: mustJSON(map[string]any{
				"arch": "x86_64",
			}),
		},
	}

	result, err := New("dev").EnrichExporterExport(drivers, nil)
	if err != nil {
		t.Fatal(err)
	}

	config := unmarshalConfig(t, findDriver(result, "qemu").Config)
	hostfwd, ok := config["hostfwd"].(map[string]any)
	if !ok {
		t.Fatalf("hostfwd not a map: %T", config["hostfwd"])
	}
	ssh, ok := hostfwd["ssh"].(map[string]any)
	if !ok {
		t.Fatalf("hostfwd.ssh not a map: %T", hostfwd["ssh"])
	}
	if got := ssh["hostaddr"]; got != "127.0.0.1" {
		t.Errorf("hostfwd.ssh.hostaddr = %v", got)
	}
	if got := ssh["hostport"].(float64); got != 2222 {
		t.Errorf("hostfwd.ssh.hostport = %v", got)
	}
	if got := ssh["guestport"].(float64); got != 22 {
		t.Errorf("hostfwd.ssh.guestport = %v", got)
	}
}

func TestEnrichExporterExport_autoInjectsTCPDriver(t *testing.T) {
	drivers := []virtualtargetv1alpha1.DriverConfig{
		{
			Name: "qemu",
			Type: qemuDriverType,
			Config: mustJSON(map[string]any{
				"arch": "x86_64",
			}),
		},
	}

	result, err := New("dev").EnrichExporterExport(drivers, nil)
	if err != nil {
		t.Fatal(err)
	}

	tcp := findDriver(result, "tcp")
	if tcp == nil {
		t.Fatal("tcp driver not auto-injected")
	}
	if tcp.Type != tcpDriverType {
		t.Errorf("tcp driver type = %q", tcp.Type)
	}
}

func TestEnrichExporterExport_doesNotDuplicateExistingTCP(t *testing.T) {
	drivers := []virtualtargetv1alpha1.DriverConfig{
		{
			Name: "qemu",
			Type: qemuDriverType,
			Config: mustJSON(map[string]any{
				"arch": "x86_64",
			}),
		},
		{
			Name:   "tcp",
			Type:   tcpDriverType,
			Config: mustJSON(map[string]any{"host": "10.0.0.1", "port": 3333}),
		},
	}

	result, err := New("dev").EnrichExporterExport(drivers, nil)
	if err != nil {
		t.Fatal(err)
	}

	tcpCount := 0
	for _, d := range result {
		if d.Type == tcpDriverType {
			tcpCount++
		}
	}
	if tcpCount != 1 {
		t.Errorf("tcp driver count = %d, want 1", tcpCount)
	}
}

func TestEnrichExporterExport_defaultsFromMergedParameters(t *testing.T) {
	drivers := []virtualtargetv1alpha1.DriverConfig{
		{
			Name:   "qemu",
			Type:   qemuDriverType,
			Config: mustJSON(map[string]any{}),
		},
	}

	params := map[string]any{
		"arch": "aarch64",
		"resources": map[string]any{
			"cpu":     4,
			"memory":  "4Gi",
			"storage": "40Gi",
		},
	}

	result, err := New("dev").EnrichExporterExport(drivers, params)
	if err != nil {
		t.Fatal(err)
	}

	config := unmarshalConfig(t, findDriver(result, "qemu").Config)
	if got := config["arch"]; got != "aarch64" {
		t.Errorf("arch = %v, want aarch64", got)
	}
	if got := config["smp"]; got != float64(4) {
		t.Errorf("smp = %v, want 4", got)
	}
	if got := config["mem"]; got != "4G" {
		t.Errorf("mem = %v, want 4G", got)
	}
	if got := config["disk_size"]; got != "40G" {
		t.Errorf("disk_size = %v, want 40G", got)
	}
}

func TestNormalizeQemuSize(t *testing.T) {
	cases := []struct {
		in, want string
	}{
		{"10Gi", "10G"},
		{"512Mi", "512M"},
		{"1Ti", "1T"},
		{"2G", "2G"},
		{"128M", "128M"},
	}
	for _, tc := range cases {
		got := normalizeQemuSize(tc.in)
		if got != tc.want {
			t.Errorf("normalizeQemuSize(%q) = %v, want %q", tc.in, got, tc.want)
		}
	}
}

// --- helpers ---

func findDriver(drivers []virtualtargetv1alpha1.DriverConfig, name string) *virtualtargetv1alpha1.DriverConfig {
	for i := range drivers {
		if drivers[i].Name == name {
			return &drivers[i]
		}
	}
	return nil
}

func unmarshalConfig(t *testing.T, raw *apiextensionsv1.JSON) map[string]any {
	t.Helper()
	if raw == nil || raw.Raw == nil {
		t.Fatal("config is nil")
	}
	var config map[string]any
	if err := json.Unmarshal(raw.Raw, &config); err != nil {
		t.Fatalf("unmarshal config: %v", err)
	}
	return config
}
