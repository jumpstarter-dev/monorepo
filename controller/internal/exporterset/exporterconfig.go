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
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"

	virtualtargetv1alpha1 "github.com/jumpstarter-dev/jumpstarter/controller/api/virtualtarget/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	sigsyaml "sigs.k8s.io/yaml"

	jumpstarterdevv1alpha1 "github.com/jumpstarter-dev/jumpstarter/controller/api/v1alpha1"
)

const (
	// configSecretPrefix is used to name the ExporterConfig Secret.
	configSecretPrefix = "exporter-config-"

	// caConfigMapName is the well-known ConfigMap containing the service CA bundle.
	caConfigMapName = "jumpstarter-service-ca-cert"
	caConfigMapKey  = "ca.crt"

	// exporterConfigKey is the key in the Secret data map.
	exporterConfigKey = "config.yaml"

	// configVolumeName is the volume name for the ExporterConfig Secret.
	configVolumeName = "exporter-config"

	// ExporterConfigMountPath is where the ExporterConfig Secret is mounted
	// inside the exporter container (QEMU provisioner and injectConfigVolume).
	ExporterConfigMountPath = "/etc/jumpstarter/exporters"

	// configMountPath is the unexported alias used within this package.
	configMountPath = ExporterConfigMountPath

	// exporterContainerName is the main container that runs jmp run.
	exporterContainerName = "exporter"
)

// exporterConfig represents the YAML structure of a Jumpstarter ExporterConfig.
// Fields use json tags because sigs.k8s.io/yaml marshals through JSON.
type exporterConfig struct {
	APIVersion     string                          `json:"apiVersion"`
	Kind           string                          `json:"kind"`
	Metadata       exporterConfigMetadata          `json:"metadata"`
	Endpoint       string                          `json:"endpoint"`
	TLS            *exporterConfigTLS              `json:"tls,omitempty"`
	Token          string                          `json:"token"`
	Export         map[string]exporterConfigDriver `json:"export,omitempty"`
	ExitOnLeaseEnd bool                            `json:"exitOnLeaseEnd"`
}

type exporterConfigMetadata struct {
	Name      string `json:"name"`
	Namespace string `json:"namespace"`
}

type exporterConfigTLS struct {
	CA string `json:"ca"`
}

type exporterConfigDriver struct {
	Type     string                          `json:"type,omitempty"`
	Ref      string                          `json:"ref,omitempty"`
	Config   any                             `json:"config,omitempty"`
	Children map[string]exporterConfigDriver `json:"children,omitempty"`
}

// buildExporterConfigSecret builds a Secret containing the ExporterConfig YAML
// for the given exporter instance. The caBundle is read once per reconcile
// and passed in to avoid repeated ConfigMap lookups.
func (r *ExporterSetReconciler) buildExporterConfigSecret(
	ctx context.Context,
	es *virtualtargetv1alpha1.ExporterSet,
	exporter *jumpstarterdevv1alpha1.Exporter,
	caBundle string,
	mergedParameters map[string]any,
) (*corev1.Secret, error) {
	token, err := r.readCredentialToken(ctx, exporter)
	if err != nil {
		return nil, err
	}

	caBase64 := base64.StdEncoding.EncodeToString([]byte(caBundle))

	drivers := es.Spec.Template.Spec.Drivers
	drivers, err = r.Provisioner.EnrichExporterExport(drivers, mergedParameters)
	if err != nil {
		return nil, fmt.Errorf("enrich drivers for %s: %w", exporter.Name, err)
	}

	exportMap, err := buildExportMap(drivers)
	if err != nil {
		return nil, fmt.Errorf("build export map for %s: %w", exporter.Name, err)
	}

	cfg := exporterConfig{
		APIVersion: "jumpstarter.dev/v1alpha1",
		Kind:       "ExporterConfig",
		Metadata: exporterConfigMetadata{
			Name:      exporter.Name,
			Namespace: exporter.Namespace,
		},
		Endpoint: exporter.Status.Endpoint,
		TLS: &exporterConfigTLS{
			CA: caBase64,
		},
		Token:          token,
		Export:         exportMap,
		ExitOnLeaseEnd: es.Spec.RecycleStrategy != virtualtargetv1alpha1.RecycleStrategyInPlaceReuse,
	}

	cfgYAML, err := sigsyaml.Marshal(cfg)
	if err != nil {
		return nil, fmt.Errorf("marshal ExporterConfig for %s/%s: %w",
			exporter.Namespace, exporter.Name, err)
	}

	secret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name:      configSecretPrefix + exporter.Name,
			Namespace: exporter.Namespace,
			Labels: map[string]string{
				labelExporterSetName: es.Name,
			},
		},
		Data: map[string][]byte{
			exporterConfigKey: cfgYAML,
		},
	}

	return secret, nil
}

// buildExportMap converts a slice of DriverConfigs into the export map
// used by the ExporterConfig YAML. Returns an error on duplicate keys.
func buildExportMap(drivers []virtualtargetv1alpha1.DriverConfig) (map[string]exporterConfigDriver, error) {
	exportMap := make(map[string]exporterConfigDriver, len(drivers))

	for _, d := range drivers {
		name := d.Name

		if _, exists := exportMap[name]; exists {
			return nil, fmt.Errorf("duplicate driver key %q in ExporterSet drivers", name)
		}

		if d.Ref != "" {
			exportMap[name] = exporterConfigDriver{
				Ref: d.Ref,
			}
			continue
		}

		var config any
		if d.Config != nil && d.Config.Raw != nil {
			if err := json.Unmarshal(d.Config.Raw, &config); err != nil {
				return nil, fmt.Errorf("unmarshal config for driver %q: %w", name, err)
			}
		}

		exportMap[name] = exporterConfigDriver{
			Type:   d.Type,
			Config: config,
		}
	}

	return exportMap, nil
}
