/*
Copyright 2026. The Jumpstarter Authors.

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

package image_test

import (
	"os"
	"strings"
	"testing"
)

func mustRead(t *testing.T, path string) string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	return string(b)
}

func TestContainerfileBuildsTelemetryBinary(t *testing.T) {
	content := mustRead(t, "Containerfile")
	if !strings.Contains(content, "./cmd/telemetry") {
		t.Error("Containerfile must compile ./cmd/telemetry")
	}
	if !strings.Contains(content, "-o telemetry") {
		t.Error("Containerfile must emit a binary named telemetry")
	}
	if !strings.Contains(content, "COPY --from=builder /build/telemetry") {
		t.Error("Containerfile must copy /build/telemetry into the runtime image as /telemetry")
	}
}

func TestMakefileBuildProducesTelemetry(t *testing.T) {
	content := mustRead(t, "Makefile")
	if !strings.Contains(content, "-o bin/telemetry") {
		t.Error("make build must produce bin/telemetry")
	}
	if !strings.Contains(content, "cmd/telemetry") {
		t.Error("make build must compile cmd/telemetry")
	}
}

func TestMakefileDockerBuildCIStagesTelemetry(t *testing.T) {
	content := mustRead(t, "Makefile")
	if !strings.Contains(content, "-o bin/ci-stage/controller/telemetry") {
		t.Error("make docker-build-ci must stage telemetry next to manager and router so Containerfile.prebuilt COPY . . places /telemetry")
	}
}
