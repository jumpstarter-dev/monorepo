package config

import (
	"fmt"
	"time"

	"google.golang.org/grpc"
	apiserverv1beta1 "k8s.io/apiserver/pkg/apis/apiserver/v1beta1"
	"k8s.io/apiserver/pkg/authentication/authenticator"
)

// Config represents the main controller configuration structure.
// This matches the YAML structure in the ConfigMap's "config" key.
type Config struct {
	Authentication   Authentication   `json:"authentication" yaml:"authentication"`
	Provisioning     Provisioning     `json:"provisioning" yaml:"provisioning"`
	Grpc             Grpc             `json:"grpc" yaml:"grpc"`
	LeasePolicy      LeasePolicy      `json:"leasePolicy" yaml:"leasePolicy,omitempty"`
	HiddenLabels     HiddenLabels     `json:"hiddenLabels" yaml:"hiddenLabels,omitempty"`
	DeprecatedLabels DeprecatedLabels `json:"deprecatedLabels" yaml:"deprecatedLabels,omitempty"`
	Telemetry        *Telemetry       `json:"telemetry,omitempty" yaml:"telemetry,omitempty"`
}

// Telemetry configures the optional jumpstarter-telemetry service.
// When Enabled is true the controller advertises the telemetry endpoint to
// exporters and clients via GetServiceEndpoints so they can push logs without
// holding cluster credentials.
type Telemetry struct {
	// Enabled controls whether the telemetry service is active.
	// When true the controller advertises the endpoint returned by GetServiceEndpoints.
	Enabled bool `json:"enabled,omitempty" yaml:"enabled,omitempty"`

	// Endpoint is an optional override for the telemetry gRPC address advertised
	// to exporters. When empty the controller reads GRPC_TELEMETRY_ENDPOINT from its
	// own environment (set by the operator on the controller Deployment).
	Endpoint string `json:"endpoint,omitempty" yaml:"endpoint,omitempty"`

	// Certificate is the PEM-encoded CA certificate that exporters use to verify
	// the telemetry server's TLS certificate.
	//
	// When the operator provisions the telemetry service with a cert-manager-issued
	// certificate, set this to the issuer's CA certificate.
	//
	// When the telemetry service runs in self-signed mode (no EXTERNAL_CERT_PEM /
	// EXTERNAL_KEY_PEM set), it logs the generated certificate PEM at startup under
	// the key "certPEM". Copy that value here so exporters can pin and verify it.
	// A self-signed certificate is not trusted by the system CA pool, so leaving
	// this field empty means exporters cannot establish a verified TLS connection.
	Certificate string `json:"certificate,omitempty" yaml:"certificate,omitempty"`

	// Logging configures the log ingestion path to the telemetry service.
	Logging TelemetryLogging `json:"logging" yaml:"logging,omitempty"`
}

// TelemetryLogging configures the log push path to the telemetry service.
type TelemetryLogging struct {
	Filter TelemetryLoggingFilter `json:"filter" yaml:"filter,omitempty"`
}

// TelemetryLoggingFilter controls which log entries are forwarded to the telemetry service.
type TelemetryLoggingFilter struct {
	// MinSeverity is the minimum log severity to forward.
	// Accepted values: debug, info, warning, error, critical. Defaults to "info" when empty.
	MinSeverity string `json:"minSeverity,omitempty" yaml:"minSeverity,omitempty"`
}

var validSeverities = map[string]struct{}{
	"debug": {}, "info": {}, "warning": {}, "error": {}, "critical": {},
}

// Validate returns an error if any field contains an invalid value.
func (t *Telemetry) Validate() error {
	if t.Logging.Filter.MinSeverity != "" {
		if _, ok := validSeverities[t.Logging.Filter.MinSeverity]; !ok {
			return fmt.Errorf(
				"telemetry.logging.filter.minSeverity %q is not valid, accepted values: debug, info, warning, error, critical",
				t.Logging.Filter.MinSeverity,
			)
		}
	}
	return nil
}

// LeasePolicy defines policy constraints for leases.
type LeasePolicy struct {
	MaxTags int32 `json:"maxTags,omitempty" yaml:"maxTags,omitempty"`
}

// HiddenLabels defines label keys to hide from exporter listings by default.
type HiddenLabels struct {
	Keys []string `json:"keys,omitempty" yaml:"keys,omitempty"`
}

// DeprecatedLabels defines label keys that trigger deprecation warnings.
type DeprecatedLabels struct {
	Keys map[string]string `json:"keys,omitempty" yaml:"keys,omitempty"`
}

// Authentication defines the authentication configuration for the controller.
// Supports multiple authentication methods: internal tokens, Kubernetes tokens, and JWT.
type Authentication struct {
	Internal Internal                            `json:"internal" yaml:"internal"`
	K8s      K8s                                 `json:"k8s" yaml:"k8s,omitempty"`
	JWT      []apiserverv1beta1.JWTAuthenticator `json:"jwt" yaml:"jwt"`
}

// Internal defines the internal token authentication configuration.
type Internal struct {
	// Prefix to add to the subject claim of issued tokens (e.g., "internal:")
	Prefix string `json:"prefix" yaml:"prefix"`

	// TokenLifetime defines how long issued tokens are valid.
	// Parsed as a Go duration string (e.g., "43800h", "30d").
	TokenLifetime string `json:"tokenLifetime,omitempty" yaml:"tokenLifetime,omitempty"`
}

// K8s defines the Kubernetes service account token authentication configuration.
type K8s struct {
	// Enabled indicates whether Kubernetes authentication is enabled.
	Enabled bool `json:"enabled,omitempty" yaml:"enabled,omitempty"`
}

// Provisioning defines the provisioning configuration.
type Provisioning struct {
	Enabled bool `json:"enabled" yaml:"enabled"`
}

// Grpc defines the gRPC server configuration.
type Grpc struct {
	Keepalive Keepalive `json:"keepalive" yaml:"keepalive"`
}

// Keepalive defines the gRPC keepalive configuration.
// All duration fields are parsed as Go duration strings (e.g., "1s", "10s", "180s").
type Keepalive struct {
	// MinTime is the minimum time between keepalives that the server will accept.
	// Default: "1s"
	MinTime string `json:"minTime,omitempty" yaml:"minTime,omitempty"`

	// PermitWithoutStream allows keepalive pings even when there are no active streams.
	// Default: true
	PermitWithoutStream bool `json:"permitWithoutStream,omitempty" yaml:"permitWithoutStream,omitempty"`

	// Timeout is the duration to wait for a keepalive ping acknowledgment.
	// Default: "180s"
	Timeout string `json:"timeout,omitempty" yaml:"timeout,omitempty"`

	// IntervalTime is the duration between keepalive pings.
	// Default: "10s"
	IntervalTime string `json:"intervalTime,omitempty" yaml:"intervalTime,omitempty"`

	// MaxConnectionIdle is the maximum duration a connection can be idle before being closed.
	// Default: infinity (not set)
	MaxConnectionIdle string `json:"maxConnectionIdle,omitempty" yaml:"maxConnectionIdle,omitempty"`

	// MaxConnectionAge is the maximum age of a connection before it is closed.
	// Default: infinity (not set)
	MaxConnectionAge string `json:"maxConnectionAge,omitempty" yaml:"maxConnectionAge,omitempty"`

	// MaxConnectionAgeGrace is the grace period for closing connections that exceed MaxConnectionAge.
	// Default: infinity (not set)
	MaxConnectionAgeGrace string `json:"maxConnectionAgeGrace,omitempty" yaml:"maxConnectionAgeGrace,omitempty"`
}

// LoadedConfig holds all values returned by LoadConfiguration.
// Using a single struct avoids a long list of return values and makes
// it easy to add new fields without touching every call site.
type LoadedConfig struct {
	Authenticator    authenticator.Token
	Prefix           string
	Router           Router
	ServerOptions    []grpc.ServerOption
	Provisioning     *Provisioning
	LeasePolicy      *LeasePolicy
	HiddenLabels     *HiddenLabels
	DeprecatedLabels *DeprecatedLabels
	Telemetry        *Telemetry
}

// Router represents the router configuration mapping.
// This is a map where keys are router names (e.g., "default", "router-1", "router-2")
// and values are RouterEntry structs containing endpoint and label information.
// This matches the YAML structure in the ConfigMap's "router" key.
type Router map[string]RouterEntry

// RouterEntry defines a single router endpoint configuration.
type RouterEntry struct {
	// Endpoint is the router's gRPC endpoint address (e.g., "router-0.example.com:443")
	Endpoint string `json:"endpoint" yaml:"endpoint"`

	// Labels are optional labels to associate with this router entry.
	// Used to distinguish between different router instances.
	Labels map[string]string `json:"labels,omitempty" yaml:"labels,omitempty"`
}

// ParseDuration is a helper to parse duration strings with better error messages.
func ParseDuration(s string) (time.Duration, error) {
	if s == "" {
		return 0, nil
	}
	return time.ParseDuration(s)
}
