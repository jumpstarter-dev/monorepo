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

package main

import (
	"context"
	"crypto/tls"
	"encoding/pem"
	"flag"
	"net"
	"os"

	// Import all Kubernetes client auth plugins (e.g. Azure, GCP, OIDC, etc.)
	// to ensure that exec-entrypoint and run can make use of them.
	apiserverinstall "k8s.io/apiserver/pkg/apis/apiserver/install"
	apiserverv1beta1 "k8s.io/apiserver/pkg/apis/apiserver/v1beta1"
	_ "k8s.io/client-go/plugin/pkg/client/auth"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	"k8s.io/apimachinery/pkg/util/yaml"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
	"sigs.k8s.io/controller-runtime/pkg/webhook"

	jumpstarterdevv1alpha1 "github.com/jumpstarter-dev/jumpstarter/controller/api/v1alpha1"
	virtualtargetv1alpha1 "github.com/jumpstarter-dev/jumpstarter/controller/api/virtualtarget/v1alpha1"
	"github.com/jumpstarter-dev/jumpstarter/controller/internal/authentication"
	"github.com/jumpstarter-dev/jumpstarter/controller/internal/authorization"
	"github.com/jumpstarter-dev/jumpstarter/controller/internal/config"
	"github.com/jumpstarter-dev/jumpstarter/controller/internal/controller"
	jmpmetrics "github.com/jumpstarter-dev/jumpstarter/controller/internal/metrics"
	"github.com/jumpstarter-dev/jumpstarter/controller/internal/oidc"
	"github.com/jumpstarter-dev/jumpstarter/controller/internal/service"
	"github.com/jumpstarter-dev/jumpstarter/controller/internal/service/login"

	// +kubebuilder:scaffold:imports

	_ "google.golang.org/grpc/encoding"
)

var (
	scheme   = runtime.NewScheme()
	setupLog = ctrl.Log.WithName("setup")

	// Version information - set via ldflags at build time
	version   = "dev"
	gitCommit = "unknown"
	buildDate = "unknown"
)

const (
	// namespaceFile is the path to the namespace file mounted by Kubernetes
	namespaceFile = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
)

// getWatchNamespace returns the namespace the controller should watch.
// It tries multiple sources in order:
// 1. NAMESPACE environment variable (explicit configuration takes precedence)
// 2. Namespace file (automatically mounted by Kubernetes in every pod)
// 3. Empty string (will fail, not supported since 0.8.0)
func getWatchNamespace() string {
	// First check NAMESPACE environment variable (explicit configuration)
	if ns := os.Getenv("NAMESPACE"); ns != "" {
		setupLog.Info("Using namespace from NAMESPACE environment variable", "namespace", ns)
		return ns
	}

	// Fall back to reading from the namespace file mounted by Kubernetes
	if ns, err := os.ReadFile(namespaceFile); err == nil {
		namespace := string(ns)
		if namespace != "" {
			setupLog.Info("Auto-detected namespace from service account", "namespace", namespace)
			return namespace
		}
	}

	return ""
}

func init() {
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))

	utilruntime.Must(jumpstarterdevv1alpha1.AddToScheme(scheme))

	// Read-only, for the lease controller: whether a selector that matches no
	// exporter today could be satisfied by a pool provisioning one.
	utilruntime.Must(virtualtargetv1alpha1.AddToScheme(scheme))

	// +kubebuilder:scaffold:scheme
	apiserverinstall.Install(scheme)
}

func main() {
	var metricsAddr string
	var enableLeaderElection bool
	var probeAddr string
	var secureMetrics bool
	var enableHTTP2 bool
	flag.StringVar(&metricsAddr, "metrics-bind-address", "0", "The address the metric endpoint binds to. "+
		"Use the port :8080. If not set, it will be 0 in order to disable the metrics server")
	flag.StringVar(&probeAddr, "health-probe-bind-address", ":8081", "The address the probe endpoint binds to.")
	flag.BoolVar(&enableLeaderElection, "leader-elect", false,
		"Enable leader election for controller manager. "+
			"Enabling this will ensure there is only one active controller manager.")
	flag.BoolVar(&secureMetrics, "metrics-secure", false,
		"If set the metrics endpoint is served securely")
	flag.BoolVar(&enableHTTP2, "enable-http2", false,
		"If set, HTTP/2 will be enabled for the metrics and webhook servers")
	opts := zap.Options{}
	opts.BindFlags(flag.CommandLine)
	flag.Parse()

	ctrl.SetLogger(zap.New(zap.UseFlagOptions(&opts)).WithValues("component", "controller"))

	// Print version information
	setupLog.Info("Jumpstarter Controller starting",
		"version", version,
		"gitCommit", gitCommit,
		"buildDate", buildDate,
	)

	// if the enable-http2 flag is false (the default), http/2 should be disabled
	// due to its vulnerabilities. More specifically, disabling http/2 will
	// prevent from being vulnerable to the HTTP/2 Stream Cancellation and
	// Rapid Reset CVEs. For more information see:
	// - https://github.com/advisories/GHSA-qppj-fm5r-hxr3
	// - https://github.com/advisories/GHSA-4374-p667-p6c8
	disableHTTP2 := func(c *tls.Config) {
		setupLog.Info("disabling http/2")
		c.NextProtos = []string{"http/1.1"}
	}

	tlsOpts := []func(*tls.Config){}
	if !enableHTTP2 {
		tlsOpts = append(tlsOpts, disableHTTP2)
	}

	webhookServer := webhook.NewServer(webhook.Options{
		TLSOpts: tlsOpts,
	})

	// Get the namespace to watch. Try to auto-detect from the pod's service account,
	// fall back to NAMESPACE environment variable, or watch all namespaces if neither is available
	watchNamespace := getWatchNamespace()

	mgrOptions := ctrl.Options{
		Scheme: scheme,
		Metrics: metricsserver.Options{
			BindAddress:   metricsAddr,
			SecureServing: secureMetrics,
			TLSOpts:       tlsOpts,
		},
		WebhookServer:          webhookServer,
		HealthProbeBindAddress: probeAddr,
		LeaderElection:         enableLeaderElection,
		LeaderElectionID:       "a38b78e7.jumpstarter.dev",
		// LeaderElectionReleaseOnCancel defines if the leader should step down voluntarily
		// when the Manager ends. This requires the binary to immediately end when the
		// Manager is stopped, otherwise, this setting is unsafe. Setting this significantly
		// speeds up voluntary leader transitions as the new leader don't have to wait
		// LeaseDuration time first.
		//
		// In the default scaffold provided, the program ends immediately after
		// the manager stops, so would be fine to enable this option. However,
		// if you are doing or is intended to do any operation such as perform cleanups
		// after the manager stops then its usage might be unsafe.
		// LeaderElectionReleaseOnCancel: true,
	}

	// If a specific namespace is set, configure the cache to only watch that namespace
	if watchNamespace != "" {
		mgrOptions.LeaderElectionNamespace = watchNamespace
		mgrOptions.Cache = cache.Options{
			DefaultNamespaces: map[string]cache.Config{
				watchNamespace: {},
			},
		}
	} else {
		setupLog.Error(nil, "Jumpstarter controller can only be configured to work on a single namespace since 0.8.0")
		os.Exit(1)
	}

	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), mgrOptions)
	if err != nil {
		setupLog.Error(err, "unable to start manager")
		os.Exit(1)
	}

	jmpmetrics.RegisterDefaults()

	oidcCert, err := service.NewSelfSignedCertificate("jumpstarter oidc", []string{"localhost"}, []net.IP{})
	if err != nil {
		setupLog.Error(err, "unable to generate certificate for internal oidc provider")
		os.Exit(1)
	}

	oidcSigner, err := oidc.NewSignerFromSeed(
		[]byte(os.Getenv("CONTROLLER_KEY")),
		"https://localhost:8085",
		"jumpstarter",
	)
	if err != nil {
		setupLog.Error(err, "unable to create internal oidc signer")
		os.Exit(1)
	}

	cfg, err := config.LoadConfiguration(
		context.Background(),
		mgr.GetAPIReader(),
		mgr.GetScheme(),
		client.ObjectKey{
			Namespace: os.Getenv("NAMESPACE"),
			Name:      "jumpstarter-controller",
		},
		oidcSigner,
		string(pem.EncodeToMemory(&pem.Block{
			Type:  "CERTIFICATE",
			Bytes: oidcCert.Certificate[0],
		})),
	)
	if err != nil {
		setupLog.Error(err, "unable to load configuration")
		os.Exit(1)
	}

	if err = (&controller.ExporterReconciler{
		Client:   mgr.GetClient(),
		Scheme:   mgr.GetScheme(),
		Signer:   oidcSigner,
		Recorder: mgr.GetEventRecorderFor("exporter-controller"),
	}).SetupWithManager(mgr); err != nil {
		setupLog.Error(err, "unable to create controller", "controller", "Exporter")
		os.Exit(1)
	}
	if err = (&controller.ClientReconciler{
		Client:   mgr.GetClient(),
		Scheme:   mgr.GetScheme(),
		Signer:   oidcSigner,
		Recorder: mgr.GetEventRecorderFor("client-controller"),
	}).SetupWithManager(mgr); err != nil {
		setupLog.Error(err, "unable to create controller", "controller", "Identity")
		os.Exit(1)
	}
	if err = (&controller.LeaseReconciler{
		Client: mgr.GetClient(),
		Scheme: mgr.GetScheme(),
	}).SetupWithManager(mgr); err != nil {
		setupLog.Error(err, "unable to create controller", "controller", "Lease")
		os.Exit(1)
	}
	// +kubebuilder:scaffold:builder

	watchClient, err := client.NewWithWatch(mgr.GetConfig(), client.Options{Scheme: mgr.GetScheme()})
	if err != nil {
		setupLog.Error(err, "unable to create client with watch", "service", "Controller")
		os.Exit(1)
	}

	if err = (&service.ControllerService{
		Client: watchClient,
		Scheme: mgr.GetScheme(),
		Authn:  authentication.NewBearerTokenAuthenticator(cfg.Authenticator),
		Authz:  authorization.NewBasicAuthorizer(watchClient, cfg.Prefix, cfg.Provisioning.Enabled),
		Attr: authorization.NewMetadataAttributesGetter(authorization.MetadataAttributesGetterConfig{
			NamespaceKey: "jumpstarter-namespace",
			ResourceKey:  "jumpstarter-kind",
			NameKey:      "jumpstarter-name",
		}),
		Router:           cfg.Router,
		ServerOptions:    cfg.ServerOptions,
		LeasePolicy:      cfg.LeasePolicy,
		HiddenLabels:     cfg.HiddenLabels,
		DeprecatedLabels: cfg.DeprecatedLabels,
		Signer:           oidcSigner,
		TelemetryConfig:  cfg.Telemetry,
	}).SetupWithManager(mgr); err != nil {
		setupLog.Error(err, "unable to create service", "service", "Controller")
		os.Exit(1)
	}

	if err = (&service.OIDCService{
		Signer: oidcSigner,
		Cert:   oidcCert,
	}).SetupWithManager(mgr); err != nil {
		setupLog.Error(err, "unable to create service", "service", "OIDC")
		os.Exit(1)
	}

	if err = (&service.DashboardService{
		Client: mgr.GetClient(),
		Scheme: mgr.GetScheme(),
	}).SetupWithManager(mgr); err != nil {
		setupLog.Error(err, "unable to create service", "service", "Dashboard")
		os.Exit(1)
	}

	// Setup Login Service for simplified CLI login
	loginService := login.NewServiceFromEnv()
	// Extract OIDC configuration from the loaded config for the login service
	oidcConfigs := extractOIDCConfigs(mgr.GetAPIReader(), watchNamespace)
	loginService.SetOIDCConfig(oidcConfigs)
	if err = loginService.SetupWithManager(mgr); err != nil {
		setupLog.Error(err, "unable to create service", "service", "Login")
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

	setupLog.Info("starting manager")
	if err := mgr.Start(ctrl.SetupSignalHandler()); err != nil {
		setupLog.Error(err, "problem running manager")
		os.Exit(1)
	}
}

// extractOIDCConfigs reads the OIDC configuration from the ConfigMap
func extractOIDCConfigs(reader client.Reader, namespace string) []login.OIDCConfig {
	var configmap corev1.ConfigMap
	if err := reader.Get(context.Background(), client.ObjectKey{
		Namespace: namespace,
		Name:      "jumpstarter-controller",
	}, &configmap); err != nil {
		setupLog.Error(err, "unable to read configmap for OIDC config")
		return nil
	}

	// Try new config format first
	rawConfig, ok := configmap.Data["config"]
	if ok {
		var cfg config.Config
		if err := yaml.UnmarshalStrict([]byte(rawConfig), &cfg); err == nil {
			return jwtAuthenticatorsToOIDCConfigs(cfg.Authentication.JWT)
		}
	}

	// Fall back to legacy authentication format
	rawAuth, ok := configmap.Data["authentication"]
	if ok {
		var auth config.Authentication
		if err := yaml.Unmarshal([]byte(rawAuth), &auth); err == nil {
			return jwtAuthenticatorsToOIDCConfigs(auth.JWT)
		}
	}

	return nil
}

// jwtAuthenticatorsToOIDCConfigs converts JWT authenticators to login OIDC configs
func jwtAuthenticatorsToOIDCConfigs(authenticators []apiserverv1beta1.JWTAuthenticator) []login.OIDCConfig {
	var configs []login.OIDCConfig
	for _, auth := range authenticators {
		// Skip internal OIDC provider (localhost)
		if auth.Issuer.URL == "https://localhost:8085" {
			continue
		}
		configs = append(configs, login.OIDCConfig{
			Issuer:    auth.Issuer.URL,
			ClientID:  "jumpstarter-cli", // Default client ID for CLI
			Audiences: auth.Issuer.Audiences,
		})
	}
	return configs
}
