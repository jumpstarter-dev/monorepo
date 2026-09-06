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

package main

import (
	"flag"
	"fmt"
	"os"
	"time"

	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	_ "k8s.io/client-go/plugin/pkg/client/auth"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	jumpstarterdevv1alpha1 "github.com/jumpstarter-dev/jumpstarter/controller/api/v1alpha1"
	virtualtargetv1alpha1 "github.com/jumpstarter-dev/jumpstarter/controller/api/virtualtarget/v1alpha1"
	"github.com/jumpstarter-dev/jumpstarter/controller/internal/exporterset"
	"github.com/jumpstarter-dev/jumpstarter/controller/internal/exporterset/provisioners/cuttlefish"
	"github.com/jumpstarter-dev/jumpstarter/controller/internal/exporterset/provisioners/qemu"
)

var (
	scheme   = runtime.NewScheme()
	setupLog = ctrl.Log.WithName("setup")

	// Version information - set via ldflags at build time
	version   = "dev"
	gitCommit = "unknown"
	buildDate = "unknown"
)

func init() {
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))
	utilruntime.Must(jumpstarterdevv1alpha1.AddToScheme(scheme))
	utilruntime.Must(virtualtargetv1alpha1.AddToScheme(scheme))
}

func main() {
	var (
		metricsAddr          string
		probeAddr            string
		provisioner          string
		enableLeaderElection bool
	)

	flag.StringVar(&metricsAddr, "metrics-bind-address", "0",
		"The address the metrics endpoint binds to. "+
			"Use :8443 for HTTPS or :8080 for HTTP, or 0 to disable.")
	flag.StringVar(&probeAddr, "health-probe-bind-address", ":8081",
		"The address the probe endpoint binds to.")
	flag.StringVar(&provisioner, "provisioner", "",
		"The provisioner this controller handles "+
			"(e.g. qemu.jumpstarter.dev). Required.")
	flag.BoolVar(&enableLeaderElection, "leader-elect", false, "Enable leader election for controller manager.")

	opts := zap.Options{}
	opts.BindFlags(flag.CommandLine)
	flag.Parse()
	ctrl.SetLogger(zap.New(zap.UseFlagOptions(&opts)))

	setupLog.Info("Jumpstarter Exporter-Set Controller starting",
		"version", version,
		"gitCommit", gitCommit,
		"buildDate", buildDate,
		"provisioner", provisioner,
	)

	if provisioner == "" {
		setupLog.Error(fmt.Errorf("missing required flag"), "--provisioner flag is required")
		os.Exit(1)
	}

	// This controller is only granted a namespaced Role (not a ClusterRole, see
	// exporterSetPolicyRules in the operator), so its cache MUST be restricted to
	// its own namespace. Without this, the informers issue cluster-scoped
	// List/Watch calls that are rejected by the API server with "forbidden ...
	// at the cluster scope", even though the namespaced Role grants the verb.
	namespace := os.Getenv("NAMESPACE")
	if namespace == "" {
		setupLog.Error(fmt.Errorf("missing required environment variable"), "NAMESPACE environment variable is required")
		os.Exit(1)
	}

	// Select the provisioner implementation based on the flag
	prov, err := selectProvisioner(provisioner)
	if err != nil {
		setupLog.Error(err, "unsupported provisioner", "provisioner", provisioner)
		os.Exit(1)
	}

	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		Scheme: scheme,
		Cache: cache.Options{
			DefaultNamespaces: map[string]cache.Config{
				namespace: {},
			},
		},
		Metrics: metricsserver.Options{
			BindAddress: metricsAddr,
		},
		HealthProbeBindAddress:  probeAddr,
		LeaderElection:          enableLeaderElection,
		LeaderElectionID:        "exporter-set-controller-" + provisioner,
		LeaderElectionNamespace: namespace,
	})
	if err != nil {
		setupLog.Error(err, "unable to start manager")
		os.Exit(1)
	}

	if err = (&exporterset.ExporterSetReconciler{
		Client:              mgr.GetClient(),
		Scheme:              mgr.GetScheme(),
		Recorder:            mgr.GetEventRecorderFor("exporter-set-controller"),
		Provisioner:         prov,
		LastScaleDownAction: make(map[types.NamespacedName]time.Time),
	}).SetupWithManager(mgr); err != nil {
		setupLog.Error(err, "unable to create controller", "controller", "ExporterSet")
		os.Exit(1)
	}

	if err := mgr.AddHealthzCheck("healthz", healthz.Ping); err != nil {
		setupLog.Error(err, "unable to set up health check")
		os.Exit(1)
	}
	if err := mgr.AddReadyzCheck("readyz", healthz.Ping); err != nil {
		setupLog.Error(err, "unable to set up ready check")
		os.Exit(1)
	}

	setupLog.Info("starting manager", "provisioner", provisioner)
	if err := mgr.Start(ctrl.SetupSignalHandler()); err != nil {
		setupLog.Error(err, "problem running manager")
		os.Exit(1)
	}
}

// selectProvisioner returns the Provisioner implementation for the given name.
// Add new provisioners here as they are implemented.
func selectProvisioner(name string) (exporterset.Provisioner, error) {
	switch name {
	case cuttlefish.ProvisionerName:
		return cuttlefish.New(version), nil
	case qemu.ProvisionerName:
		return qemu.New(version), nil
	default:
		return nil, fmt.Errorf(
			"unknown provisioner %q; supported: %s, %s",
			name, qemu.ProvisionerName, cuttlefish.ProvisionerName,
		)
	}
}
