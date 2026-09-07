/*
Copyright 2026.

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

package service

import (
	"context"
	"strings"

	"github.com/jumpstarter-dev/jumpstarter/controller/internal/authentication"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// exporterIdentity is the exporter CRD namespace/name claimed by a bearer token.
type exporterIdentity struct {
	namespace string
	name      string
}

func (id exporterIdentity) key() string {
	return id.namespace + "/" + id.name
}

func parseExporterSubject(subject string) (exporterIdentity, error) {
	parts := strings.SplitN(subject, ":", 4)
	if len(parts) != 4 || parts[0] != "exporter" {
		return exporterIdentity{}, status.Errorf(codes.PermissionDenied, "token is not an exporter token")
	}
	if parts[1] == "" || parts[2] == "" {
		return exporterIdentity{}, status.Errorf(codes.PermissionDenied, "token has incomplete exporter identity")
	}
	return exporterIdentity{namespace: parts[1], name: parts[2]}, nil
}

func (s *TelemetryService) authenticateExporter(ctx context.Context) (exporterIdentity, error) {
	token, err := authentication.BearerTokenFromContext(ctx)
	if err != nil {
		return exporterIdentity{}, err
	}
	if s.Signer == nil {
		return exporterIdentity{}, status.Error(codes.Internal, "telemetry signer is not configured")
	}
	subject, err := s.Signer.ParseSubject(token)
	if err != nil {
		return exporterIdentity{}, status.Errorf(codes.Unauthenticated, "invalid token: %v", err)
	}
	return parseExporterSubject(subject)
}
