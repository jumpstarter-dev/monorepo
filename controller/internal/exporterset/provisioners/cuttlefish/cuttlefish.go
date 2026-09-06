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

// Package cuttlefish implements the cuttlefish.jumpstarter.dev provisioner.
// Each exporter Pod owns one Cuttlefish runtime and one CVD.
package cuttlefish

import (
	"context"
	"encoding/json"
	"fmt"
	"maps"
	"slices"
	"strings"

	jumpstarterdevv1alpha1 "github.com/jumpstarter-dev/jumpstarter/controller/api/v1alpha1"
	virtualtargetv1alpha1 "github.com/jumpstarter-dev/jumpstarter/controller/api/virtualtarget/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const (
	ProvisionerName = "cuttlefish.jumpstarter.dev"

	DefaultExporterImage = "quay.io/jumpstarter-dev/jumpstarter:latest"
	DefaultRuntimeImage  = "us-docker.pkg.dev/android-cuttlefish-artifacts/cuttlefish-orchestration/cuttlefish-orchestration:stable"
	DefaultRelayImage    = "docker.io/alpine/socat:latest"

	exporterConfigPath       = "/etc/jumpstarter/exporters/config.yaml"
	exporterNonRootUID int64 = 65532

	cuttlefishDriverType = "jumpstarter_driver_cuttlefish.driver.Cuttlefish"
	netsimDriverType     = "jumpstarter_driver_netsim.driver.Netsim"
	btPeerDriverType     = "jumpstarter_driver_bt_peer.driver.BtPeer"

	// The orchestration image reserves 2080 for nginx when running in a Pod;
	// Host Orchestrator consequently listens on 2081. Keep this configurable.
	hostOrchestratorPort = 2081
	netsimRelayPort      = 17681
	hciRelayPort         = 17300
	defaultGPUMode       = "guest_swiftshader"
	defaultVMCPUs        = 4
	defaultVMMemoryMB    = 8192

	fetchPath      = "/home/vsoc-01/fetch"
	cvdStatePath   = "/var/tmp/cvd"
	androidTmpPath = "/tmp/android"
)

type Provisioner struct {
	Version string
}

func New(version string) *Provisioner {
	return &Provisioner{Version: version}
}

func (p *Provisioner) Name() string {
	return ProvisionerName
}

func (p *Provisioner) resolveImage(image string) string {
	if p.Version == "" || p.Version == "dev" || strings.Contains(p.Version, "-g") {
		return image
	}
	version := strings.TrimPrefix(p.Version, "v")
	if base, ok := strings.CutSuffix(image, ":latest"); ok {
		return base + ":" + version
	}
	return image
}

func (p *Provisioner) resolveImageSpec(spec *virtualtargetv1alpha1.ImageSpec, defaultImage string) (string, corev1.PullPolicy) {
	image := p.resolveImage(defaultImage)
	pullPolicy := corev1.PullIfNotPresent
	if spec != nil {
		if spec.Image != "" {
			image = spec.Image
		}
		if spec.ImagePullPolicy != "" {
			pullPolicy = spec.ImagePullPolicy
		}
	}
	return image, pullPolicy
}

func (p *Provisioner) RenderPod(
	ctx context.Context,
	exporterSet *virtualtargetv1alpha1.ExporterSet,
	vtc *virtualtargetv1alpha1.VirtualTargetClass,
	mergedParameters map[string]interface{},
	images *virtualtargetv1alpha1.ImageOverrides,
	exporter *jumpstarterdevv1alpha1.Exporter,
) (*corev1.Pod, error) {
	_ = ctx

	var exporterSpec, runtimeSpec *virtualtargetv1alpha1.ImageSpec
	if images != nil {
		exporterSpec = images.Exporter
		runtimeSpec = images.Runtime
	}
	exporterImage, exporterPullPolicy := p.resolveImageSpec(exporterSpec, DefaultExporterImage)
	runtimeImage, runtimePullPolicy := p.resolveImageSpec(runtimeSpec, DefaultRuntimeImage)
	relayImage := DefaultRelayImage
	if value, ok := mergedParameters["relay_image"].(string); ok && value != "" {
		relayImage = value
	}

	imageClaim, _ := mergedParameters["image_volume_claim"].(string)
	fetchImages, _ := mergedParameters["fetch_images"].(bool)
	if imageClaim != "" && fetchImages {
		return nil, fmt.Errorf("cuttlefish requires either image_volume_claim or fetch_images, not both")
	}
	if imageClaim == "" && !fetchImages {
		return nil, fmt.Errorf("cuttlefish requires image_volume_claim or fetch_images=true")
	}
	if readOnly, _ := mergedParameters["image_volume_read_only"].(bool); readOnly && imageClaim == "" {
		return nil, fmt.Errorf("image_volume_read_only requires image_volume_claim")
	}
	imageSize, stateSize, tmpSize, err := storageSizes(mergedParameters)
	if err != nil {
		return nil, err
	}
	storageBudget := imageSize.DeepCopy()
	storageBudget.Add(stateSize)
	storageBudget.Add(tmpSize)
	storageBudget.Add(resource.MustParse("1Gi")) // Container layers and logs need space beyond the volume budgets.
	build := ""
	if fetchImages {
		build = resolveDefaultBuild(mergedParameters)
	}
	netsimPort, hciPort := relayPorts(mergedParameters)
	runtimePrivileged, privilegedConfigured := parameterBool(mergedParameters, "runtime_privileged")
	if !privilegedConfigured {
		return nil, fmt.Errorf("cuttlefish requires explicit runtime_privileged configuration")
	}

	podMeta := metav1.ObjectMeta{
		Namespace:   exporterSet.Namespace,
		Labels:      maps.Clone(exporterSet.Spec.Template.Metadata.Labels),
		Annotations: maps.Clone(exporterSet.Spec.Template.Metadata.Annotations),
	}
	if exporter != nil {
		podMeta.Name = exporter.Name
	} else {
		podMeta.GenerateName = fmt.Sprintf("%s-", exporterSet.Name)
	}

	runtimeRestart := corev1.ContainerRestartPolicyAlways
	runAsRoot := int64(0)
	runAsExporter := exporterNonRootUID
	runAsNonRoot := true

	volumeMounts := []corev1.VolumeMount{
		{Name: "cvd-images", MountPath: fetchPath, ReadOnly: false},
		{Name: "cvd-state", MountPath: cvdStatePath},
		{Name: "android-tmp", MountPath: androidTmpPath},
	}
	deviceMounts := []corev1.VolumeMount{
		{Name: "kvm", MountPath: "/dev/kvm"},
		{Name: "vhost-vsock", MountPath: "/dev/vhost-vsock"},
		{Name: "vhost-net", MountPath: "/dev/vhost-net"},
		{Name: "tun", MountPath: "/dev/net/tun"},
	}

	exporterContainer := corev1.Container{
		Name:            "exporter",
		Image:           exporterImage,
		ImagePullPolicy: exporterPullPolicy,
		Command:         []string{"jmp", "run", "--exporter-config", exporterConfigPath},
		Env: []corev1.EnvVar{{
			Name:  "HOME",
			Value: "/tmp",
		}},
		SecurityContext: &corev1.SecurityContext{
			RunAsUser:    &runAsExporter,
			RunAsNonRoot: &runAsNonRoot,
		},
	}
	if exporter != nil {
		exporterContainer.Env = append(exporterContainer.Env, corev1.EnvVar{
			Name:  "JUMPSTARTER_EXEC_LOG_FIELDS",
			Value: fmt.Sprintf("component=exporter,exporter=%s,namespace=%s", exporter.Name, exporter.Namespace),
		})
	}

	imageVolume := corev1.Volume{Name: "cvd-images", VolumeSource: corev1.VolumeSource{
		EmptyDir: &corev1.EmptyDirVolumeSource{SizeLimit: &imageSize},
	}}
	permissionCommand := "mkdir -p /var/tmp/cvd /tmp/android && chown -R httpcvd:httpcvd /var/tmp/cvd /tmp/android /home/vsoc-01/fetch"

	initContainers := make([]corev1.Container, 0, 4)
	if fetchImages {
		initContainers = append(initContainers, corev1.Container{
			Name:            "fetch-images",
			Image:           runtimeImage,
			ImagePullPolicy: runtimePullPolicy,
			Command:         []string{"cvd", "fetch", "--default_build=" + build, "--target_directory=" + fetchPath},
			VolumeMounts:    []corev1.VolumeMount{{Name: "cvd-images", MountPath: fetchPath, ReadOnly: false}},
		})
	}
	initContainers = append(initContainers,
		corev1.Container{
			Name:            "fix-cuttlefish-permissions",
			Image:           runtimeImage,
			ImagePullPolicy: runtimePullPolicy,
			Command: []string{
				"bash", "-c", permissionCommand,
			},
			VolumeMounts: volumeMounts,
		},
		corev1.Container{
			Name:            "cuttlefish",
			Image:           runtimeImage,
			ImagePullPolicy: runtimePullPolicy,
			RestartPolicy:   &runtimeRestart,
			SecurityContext: &corev1.SecurityContext{Privileged: boolPtr(runtimePrivileged), RunAsUser: &runAsRoot},
			VolumeMounts:    slices.Concat(volumeMounts, deviceMounts),
		},
		corev1.Container{
			Name:            "cuttlefish-relay",
			Image:           relayImage,
			ImagePullPolicy: corev1.PullIfNotPresent,
			RestartPolicy:   &runtimeRestart,
			Command: []string{
				"sh", "-c",
				fmt.Sprintf(
					"socat TCP-LISTEN:%d,bind=127.0.0.1,fork,reuseaddr TCP:127.0.0.1:7681 & socat TCP-LISTEN:%d,bind=127.0.0.1,fork,reuseaddr TCP:127.0.0.1:7300 & wait",
					netsimPort, hciPort,
				),
			},
		},
	)

	pod := &corev1.Pod{
		ObjectMeta: podMeta,
		Spec: corev1.PodSpec{
			RestartPolicy:  corev1.RestartPolicyNever,
			InitContainers: initContainers,
			Containers:     []corev1.Container{exporterContainer},
			Volumes: []corev1.Volume{
				imageVolume,
				{Name: "cvd-state", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{SizeLimit: &stateSize}}},
				{Name: "android-tmp", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{SizeLimit: &tmpSize}}},
				deviceVolume("kvm", "/dev/kvm"),
				deviceVolume("vhost-vsock", "/dev/vhost-vsock"),
				deviceVolume("vhost-net", "/dev/vhost-net"),
				deviceVolume("tun", "/dev/net/tun"),
			},
		},
	}

	if vtc.Spec.Scheduling != nil {
		if vtc.Spec.Scheduling.NodeSelector != nil {
			pod.Spec.NodeSelector = maps.Clone(vtc.Spec.Scheduling.NodeSelector)
		}
		if vtc.Spec.Scheduling.Tolerations != nil {
			pod.Spec.Tolerations = append([]corev1.Toleration(nil), vtc.Spec.Scheduling.Tolerations...)
		}
		if vtc.Spec.Scheduling.Resources != nil {
			// Cuttlefish runs in the runtime sidecar; the exporter and relay remain unbounded.
			for i := range pod.Spec.InitContainers {
				if pod.Spec.InitContainers[i].Name == "cuttlefish" {
					pod.Spec.InitContainers[i].Resources = *vtc.Spec.Scheduling.Resources.DeepCopy()
					break
				}
			}
		}
	}

	if imageClaim != "" {
		pod.Spec.Volumes = append(pod.Spec.Volumes, corev1.Volume{Name: "image-source", VolumeSource: corev1.VolumeSource{
			PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: imageClaim, ReadOnly: true},
		}})
		copyImages := corev1.Container{
			Name: "copy-images", Image: runtimeImage, ImagePullPolicy: runtimePullPolicy,
			Command: []string{"bash", "-ec", "cp -a --reflink=auto /image-source/. /home/vsoc-01/fetch/"},
			VolumeMounts: []corev1.VolumeMount{
				{Name: "image-source", MountPath: "/image-source", ReadOnly: true},
				{Name: "cvd-images", MountPath: fetchPath},
			},
		}
		pod.Spec.InitContainers = append([]corev1.Container{copyImages}, pod.Spec.InitContainers...)
	}
	for i := range pod.Spec.InitContainers {
		container := &pod.Spec.InitContainers[i]
		if container.Name == "cuttlefish" || container.Name == "fetch-images" || container.Name == "copy-images" {
			if err := reserveStorage(&container.Resources, storageBudget); err != nil {
				return nil, err
			}
		}
	}
	// Run in the exporter image so the check uses the same network namespace and Python runtime as jmp.
	healthURL := fmt.Sprintf("http://127.0.0.1:%d/_debug/statusz", parameterInt(mergedParameters, "host_orchestrator_port", hostOrchestratorPort))
	healthCheck := "import urllib.request; urllib.request.urlopen(" + fmt.Sprintf("%q", healthURL) + ", timeout=3).close()"
	pod.Spec.InitContainers = append(pod.Spec.InitContainers, corev1.Container{
		Name: "wait-for-cuttlefish", Image: exporterImage, ImagePullPolicy: exporterPullPolicy,
		SecurityContext: exporterContainer.SecurityContext.DeepCopy(),
		Command:         []string{"python3", "-c", "import time, urllib.request\nfor attempt in range(60):\n try:\n  " + healthCheck + "\n  break\n except Exception:\n  time.sleep(5)\nelse:\n raise SystemExit('Host Orchestrator did not become ready')"},
	})
	// With restartPolicy Never, a failed liveness check ends the exporter and lets ExitAndReplace recycle the Pod.
	pod.Spec.Containers[0].LivenessProbe = &corev1.Probe{
		ProbeHandler:  corev1.ProbeHandler{Exec: &corev1.ExecAction{Command: []string{"python3", "-c", healthCheck}}},
		PeriodSeconds: 10, TimeoutSeconds: 5, FailureThreshold: 6,
	}

	return pod, nil
}

func (p *Provisioner) EnrichExporterExport(
	drivers []virtualtargetv1alpha1.DriverConfig,
	mergedParameters map[string]interface{},
) ([]virtualtargetv1alpha1.DriverConfig, error) {
	netsimPort, hciPort := relayPorts(mergedParameters)
	result := make([]virtualtargetv1alpha1.DriverConfig, 0, len(drivers))
	for _, driver := range drivers {
		var err error
		switch driver.Type {
		case cuttlefishDriverType:
			driver, err = enrichCuttlefishDriver(driver, mergedParameters)
		case netsimDriverType:
			driver, err = enrichDriverConfig(driver, map[string]interface{}{
				"host": "127.0.0.1",
				"port": netsimPort,
			}, "netsim")
		case btPeerDriverType:
			driver, err = enrichDriverConfig(driver, map[string]interface{}{
				"transport": fmt.Sprintf("tcp-client:127.0.0.1:%d", hciPort),
			}, "bt_peer")
		}
		if err != nil {
			return nil, err
		}
		result = append(result, driver)
	}
	return result, nil
}

func enrichCuttlefishDriver(driver virtualtargetv1alpha1.DriverConfig, parameters map[string]interface{}) (virtualtargetv1alpha1.DriverConfig, error) {
	config, err := decodeConfig(driver, "Cuttlefish")
	if err != nil {
		return driver, err
	}

	setDefault(config, "scheme", "http")
	setDefault(config, "host", "127.0.0.1")
	setDefault(config, "port", parameterInt(parameters, "host_orchestrator_port", hostOrchestratorPort))
	setDefault(config, "group", "cvd")
	setDefault(config, "name", "1")
	setDefault(config, "instance_num", 1)
	setDefault(config, "boot_timeout", 300)

	envConfig, _ := config["env_config"].(map[string]interface{})
	if envConfig == nil {
		envConfig = map[string]interface{}{}
	}
	common, _ := envConfig["common"].(map[string]interface{})
	if common == nil {
		common = map[string]interface{}{}
	}
	setDefault(common, "host_package", fetchPath)
	envConfig["common"] = common
	instances, _ := envConfig["instances"].([]interface{})
	if len(instances) == 0 {
		instances = []interface{}{map[string]interface{}{}}
	}
	instance, _ := instances[0].(map[string]interface{})
	if instance == nil {
		instance = map[string]interface{}{}
	}
	disk, _ := instance["disk"].(map[string]interface{})
	if disk == nil {
		disk = map[string]interface{}{}
	}
	setDefault(disk, "default_build", fetchPath)
	instance["disk"] = disk
	graphics, _ := instance["graphics"].(map[string]interface{})
	if graphics == nil {
		graphics = map[string]interface{}{}
	}
	gpuMode := defaultGPUMode
	if configuredGPU, ok := parameters["gpu_mode"].(string); ok && configuredGPU != "" {
		gpuMode = configuredGPU
	}
	setDefault(graphics, "gpu_mode", gpuMode)
	instance["graphics"] = graphics
	vm, _ := instance["vm"].(map[string]interface{})
	if vm == nil {
		vm = map[string]interface{}{}
	}
	setDefault(vm, "cpus", parameterInt(parameters, "vm_cpus", defaultVMCPUs))
	setDefault(vm, "memory_mb", parameterInt(parameters, "vm_memory_mb", defaultVMMemoryMB))
	instance["vm"] = vm
	instances[0] = instance
	envConfig["instances"] = instances
	config["env_config"] = envConfig

	return encodeConfig(driver, config)
}

func enrichDriverConfig(driver virtualtargetv1alpha1.DriverConfig, defaults map[string]interface{}, name string) (virtualtargetv1alpha1.DriverConfig, error) {
	config, err := decodeConfig(driver, name)
	if err != nil {
		return driver, err
	}
	for key, value := range defaults {
		setDefault(config, key, value)
	}
	return encodeConfig(driver, config)
}

func decodeConfig(driver virtualtargetv1alpha1.DriverConfig, name string) (map[string]interface{}, error) {
	config := map[string]interface{}{}
	if driver.Config != nil && driver.Config.Raw != nil {
		if err := json.Unmarshal(driver.Config.Raw, &config); err != nil {
			return nil, fmt.Errorf("unmarshal %s driver config: %w", name, err)
		}
	}
	return config, nil
}

func encodeConfig(driver virtualtargetv1alpha1.DriverConfig, config map[string]interface{}) (virtualtargetv1alpha1.DriverConfig, error) {
	raw, err := json.Marshal(config)
	if err != nil {
		return driver, fmt.Errorf("marshal driver config: %w", err)
	}
	driver.Config = &apiextensionsv1.JSON{Raw: raw}
	return driver, nil
}

func resolveDefaultBuild(parameters map[string]interface{}) string {
	if value, ok := parameters["default_build"].(string); ok && value != "" {
		return value
	}
	return "aosp-android-latest-release/aosp_cf_x86_64_auto-userdebug"
}

func relayPorts(parameters map[string]interface{}) (int, int) {
	return parameterInt(parameters, "netsim_relay_port", netsimRelayPort),
		parameterInt(parameters, "hci_relay_port", hciRelayPort)
}

func parameterInt(parameters map[string]interface{}, key string, fallback int) int {
	switch value := parameters[key].(type) {
	case int:
		return value
	case int32:
		return int(value)
	case int64:
		return int(value)
	case float64:
		return int(value)
	default:
		return fallback
	}
}

func parameterBool(parameters map[string]interface{}, key string) (bool, bool) {
	value, ok := parameters[key].(bool)
	return value, ok
}

func setDefault(config map[string]interface{}, key string, value interface{}) {
	if _, exists := config[key]; !exists {
		config[key] = value
	}
}

func deviceVolume(name, path string) corev1.Volume {
	typeCharDevice := corev1.HostPathCharDev
	return corev1.Volume{
		Name: name,
		VolumeSource: corev1.VolumeSource{HostPath: &corev1.HostPathVolumeSource{
			Path: path,
			Type: &typeCharDevice,
		}},
	}
}

func boolPtr(value bool) *bool {
	return &value
}

func (p *Provisioner) Cleanup(
	ctx context.Context,
	exporterSet *virtualtargetv1alpha1.ExporterSet,
	exporter *jumpstarterdevv1alpha1.Exporter,
) error {
	// Cuttlefish state and fetched images are Pod-scoped. Kubernetes removes
	// the Pod-owned emptyDir volumes, while a claimed image tree is external
	// and must outlive the exporter, so there is nothing for the provisioner
	// to clean up here.
	return nil
}

func storageSizes(parameters map[string]interface{}) (resource.Quantity, resource.Quantity, resource.Quantity, error) {
	sizes := []resource.Quantity{resource.MustParse("20Gi"), resource.MustParse("20Gi"), resource.MustParse("4Gi")}
	raw, exists := parameters["storage"]
	if !exists {
		return sizes[0], sizes[1], sizes[2], nil
	}
	storage, ok := raw.(map[string]interface{})
	if !ok {
		return sizes[0], sizes[1], sizes[2], fmt.Errorf("parameters.storage must be an object")
	}
	for i, key := range []string{"imageSize", "stateSize", "tmpSize"} {
		if value, exists := storage[key]; exists {
			valueString, ok := value.(string)
			quantity, err := resource.ParseQuantity(valueString)
			if !ok || err != nil || quantity.Sign() <= 0 {
				return sizes[0], sizes[1], sizes[2], fmt.Errorf("parameters.storage.%s must be a positive storage quantity", key)
			}
			sizes[i] = quantity
		}
	}
	return sizes[0], sizes[1], sizes[2], nil
}

func reserveStorage(resources *corev1.ResourceRequirements, budget resource.Quantity) error {
	if resources.Requests == nil {
		resources.Requests = corev1.ResourceList{}
	}
	if resources.Limits == nil {
		resources.Limits = corev1.ResourceList{}
	}
	for _, values := range []corev1.ResourceList{resources.Requests, resources.Limits} {
		if value, exists := values[corev1.ResourceEphemeralStorage]; exists && value.Cmp(budget) < 0 {
			return fmt.Errorf("ephemeral-storage must be at least %s for Cuttlefish volume budgets and overhead", budget.String())
		}
	}
	if _, exists := resources.Requests[corev1.ResourceEphemeralStorage]; !exists {
		resources.Requests[corev1.ResourceEphemeralStorage] = budget.DeepCopy()
	}
	if _, exists := resources.Limits[corev1.ResourceEphemeralStorage]; !exists {
		resources.Limits[corev1.ResourceEphemeralStorage] = resources.Requests[corev1.ResourceEphemeralStorage].DeepCopy()
	}
	request := resources.Requests[corev1.ResourceEphemeralStorage]
	if request.Cmp(resources.Limits[corev1.ResourceEphemeralStorage]) > 0 {
		return fmt.Errorf("ephemeral-storage request exceeds limit")
	}
	return nil
}
