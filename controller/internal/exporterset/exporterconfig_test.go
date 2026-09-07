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
	"encoding/json"
	"testing"

	virtualtargetv1alpha1 "github.com/jumpstarter-dev/jumpstarter/controller/api/virtualtarget/v1alpha1"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
)

func TestBuildExportMap(t *testing.T) {
	config := map[string]any{
		"arch": "x86_64",
		"smp":  2,
	}
	configRaw, _ := json.Marshal(config)

	drivers := []virtualtargetv1alpha1.DriverConfig{
		{
			Name: "qemu",
			Type: "jumpstarter_driver_qemu.driver.Qemu",
			Config: &apiextensionsv1.JSON{
				Raw: configRaw,
			},
		},
		{
			Name: "tcp",
			Type: "jumpstarter_driver_network.driver.TcpNetwork",
		},
	}

	result, err := buildExportMap(drivers)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(result) != 2 {
		t.Fatalf("expected 2 entries, got %d", len(result))
	}

	qemu, ok := result["qemu"]
	if !ok {
		t.Fatal("'qemu' key not found")
	}
	if qemu.Type != "jumpstarter_driver_qemu.driver.Qemu" {
		t.Errorf("qemu type = %q", qemu.Type)
	}
	configMap, ok := qemu.Config.(map[string]any)
	if !ok {
		t.Fatal("qemu config is not a map")
	}
	if configMap["arch"] != "x86_64" {
		t.Errorf("qemu config arch = %v", configMap["arch"])
	}

	tcp, ok := result["tcp"]
	if !ok {
		t.Fatal("'tcp' key not found")
	}
	if tcp.Type != "jumpstarter_driver_network.driver.TcpNetwork" {
		t.Errorf("tcp type = %q", tcp.Type)
	}
}

func TestBuildExportMap_duplicateKey(t *testing.T) {
	drivers := []virtualtargetv1alpha1.DriverConfig{
		{Name: "power", Type: "jumpstarter_driver_power.driver.QemuPower"},
		{Name: "power", Type: "jumpstarter_driver_power.driver.QemuPower2"},
	}
	_, err := buildExportMap(drivers)
	if err == nil {
		t.Fatal("expected error for duplicate driver key, got nil")
	}
}

func TestBuildExportMap_refEntry(t *testing.T) {
	drivers := []virtualtargetv1alpha1.DriverConfig{
		{Name: "qemu", Type: "jumpstarter_driver_qemu.driver.Qemu"},
		{Name: "console", Ref: "qemu.console"},
	}

	result, err := buildExportMap(drivers)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(result) != 2 {
		t.Fatalf("expected 2 entries, got %d", len(result))
	}

	console, ok := result["console"]
	if !ok {
		t.Fatal("'console' key not found")
	}
	if console.Ref != "qemu.console" {
		t.Errorf("console ref = %q, want %q", console.Ref, "qemu.console")
	}
	if console.Type != "" {
		t.Errorf("console type = %q, want empty", console.Type)
	}
	if console.Config != nil {
		t.Errorf("console config = %v, want nil", console.Config)
	}
}

func TestBuildExportMap_refWithExplicitName(t *testing.T) {
	drivers := []virtualtargetv1alpha1.DriverConfig{
		{Name: "my-console", Ref: "qemu.console"},
	}

	result, err := buildExportMap(drivers)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	myConsole, ok := result["my-console"]
	if !ok {
		t.Fatalf("expected explicit key %q, got keys %v", "my-console", result)
	}
	if myConsole.Ref != "qemu.console" {
		t.Errorf("my-console ref = %q, want %q", myConsole.Ref, "qemu.console")
	}
}

func TestBuildExportMap_refDuplicateName(t *testing.T) {
	drivers := []virtualtargetv1alpha1.DriverConfig{
		{Name: "console", Type: "jumpstarter_driver_pyserial.driver.PySerial"},
		{Name: "console", Ref: "qemu.console"},
	}

	_, err := buildExportMap(drivers)
	if err == nil {
		t.Fatal("expected error for duplicate driver key, got nil")
	}
}
