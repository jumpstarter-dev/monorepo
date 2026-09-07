GO_FILES := $(shell find ./ -name ".go" -not -path "./bin" -not -path "./packaging/*")

# Image URL to use all building/pushing image targets
IMG ?= quay.io/jumpstarter-dev/jumpstarter-controller:latest
DOCKER_REPO = $(shell echo $(IMG) | cut -d: -f1)
DOCKER_TAG  = $(shell echo $(IMG) | cut -d: -f2)
EXPORTER_SET_CONTROLLER_IMG ?= quay.io/jumpstarter-dev/jumpstarter-exporterset-controller:latest
QEMU_RUNTIME_IMG ?= quay.io/jumpstarter-dev/virtual/qemu-runtime:latest
EXPORTER_IMG ?= quay.io/jumpstarter-dev/jumpstarter:latest
# ENVTEST_K8S_VERSION refers to the version of kubebuilder assets to be downloaded by envtest binary.
# renovate: datasource=github-releases depName=kubernetes-sigs/controller-tools extractVersion=^envtest-v(?<version>.+)$
ENVTEST_K8S_VERSION = 1.37.0

# Version information
GIT_VERSION := $(shell git describe --tags --always --dirty 2>/dev/null || echo "unknown")
GIT_COMMIT := $(shell git rev-parse HEAD 2>/dev/null || echo "unknown")
BUILD_DATE := $(shell date -u +'%Y-%m-%dT%H:%M:%SZ')

# LDFLAGS for version information
LDFLAGS := -X main.version=$(GIT_VERSION) \
           -X main.gitCommit=$(GIT_COMMIT) \
           -X main.buildDate=$(BUILD_DATE)

# Get the currently used golang install path (in GOPATH/bin, unless GOBIN is set)
ifeq (,$(shell go env GOBIN))
GOBIN=$(shell go env GOPATH)/bin
else
GOBIN=$(shell go env GOBIN)
endif

GOARCH ?= $(shell go env GOARCH)

# CONTAINER_TOOL defines the container tool to be used for building images.
# Be aware that the target commands are only tested with Docker which is
# scaffolded by default. However, you might want to replace it to use other
# tools. (i.e. podman)
CONTAINER_TOOL ?= podman

# Cluster type: kind (default) or k3s
CLUSTER_TYPE ?= kind
export CLUSTER_TYPE

# Setting SHELL to bash allows bash commands to be executed by recipes.
# Options are set to exit when a recipe line exits non-zero or a piped command fails.
SHELL = /usr/bin/env bash -o pipefail
.SHELLFLAGS = -ec

.PHONY: all
all: build

##@ General

# The help target prints out all targets with their descriptions organized
# beneath their categories. The categories are represented by '##@' and the
# target descriptions by '##'. The awk command is responsible for reading the
# entire set of makefiles included in this invocation, looking for lines of the
# file as xyz: ## something, and then pretty-format the target and help. Then,
# if there's a line with ##@ something, that gets pretty-printed as a category.
# More info on the usage of ANSI control characters for terminal formatting:
# https://en.wikipedia.org/wiki/ANSI_escape_code#SGR_parameters
# More info on the awk command:
# http://linuxcommand.org/lc3_adv_awk.php

.PHONY: help
help: ## Display this help.
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n"} /^[a-zA-Z_0-9-]+:.*?##/ { printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) } ' $(MAKEFILE_LIST)

##@ Development

.PHONY: manifests
manifests: controller-gen ## Generate WebhookConfiguration and CustomResourceDefinition objects.
	# CRDs only — do not emit RBAC into deploy/operator/config/rbac/.
	# The operator ClusterRole (manager-role) is owned by deploy/operator/Makefile.
	# Writing jumpstarter-manager-role there overwrites the operator role and
	# breaks role_binding.yaml (which references manager-role).
	$(CONTROLLER_GEN) 	crd webhook paths="./api/..." paths="./internal/..." \
						output:crd:artifacts:config=deploy/operator/config/crd/bases/

	# Regenerate operator install.yaml to include updated CRDs
	$(MAKE) -C deploy/operator build-installer

.PHONY: generate
generate: controller-gen ## Generate code containing DeepCopy, DeepCopyInto, and DeepCopyObject method implementations.
	$(CONTROLLER_GEN) object:headerFile="hack/boilerplate.go.txt" paths="./api/..." paths="./internal/..."

.PHONY: fmt
fmt: ## Run go fmt against code.
	go fmt ./...

.PHONY: vet
vet: ## Run go vet against code.
	go vet ./...

.PHONY: test
test: manifests generate fmt vet envtest ## Run tests.
	KUBEBUILDER_ASSETS="$(shell $(ENVTEST) use $(ENVTEST_K8S_VERSION) --bin-dir $(LOCALBIN) -p path)" go test $$(go list ./... | grep -v /e2e) -race -coverprofile cover.out

# Utilize Kind or modify the e2e tests to load the image locally, enabling compatibility with other vendors.
.PHONY: test-e2e  # Run the e2e tests against a Kind k8s instance that is spun up.
test-e2e:
	go test ./test/e2e/ -v -ginkgo.v

.PHONY: lint
lint: golangci-lint ## Run golangci-lint linter
	$(GOLANGCI_LINT) run

.PHONY: lint-fix
lint-fix: golangci-lint ## Run golangci-lint linter and perform fixes
	$(GOLANGCI_LINT) run --fix

##@ Build
.PHONY: build-operator
build-operator:
	$(MAKE) -C deploy/operator build-installer
	$(MAKE) -C deploy/operator docker-build

.PHONY: build-operator-ci
build-operator-ci:
	$(MAKE) -C deploy/operator build-installer
	$(MAKE) -C deploy/operator docker-build-ci

.PHONY: build
build: manifests generate fmt vet ## Build manager binary.
	go build -ldflags "$(LDFLAGS)" -o bin/manager cmd/main.go
	go build -ldflags "$(LDFLAGS)" -o bin/router ./cmd/router
	go build -ldflags "$(LDFLAGS)" -o bin/telemetry ./cmd/telemetry
	go build -ldflags "$(LDFLAGS)" -o bin/exporter-set-controller cmd/exporter-set-controller/main.go

.PHONY: run
run: manifests generate fmt vet ## Run a controller from your host.
	go run ./cmd/main.go

.PHONY: run-router
run-router: manifests generate fmt vet ## Run a router from your host.
	go run ./cmd/router

# If you wish to build the manager image targeting other platforms you can use the --platform flag.
# (i.e. docker build --platform linux/arm64). However, you must enable docker buildKit for it.
# More info: https://docs.docker.com/develop/develop-images/build_enhancements/
.PHONY: docker-build
docker-build: ## Build docker image with the manager.
	$(CONTAINER_TOOL) build \
		--build-arg GIT_VERSION=$(GIT_VERSION) \
		--build-arg GIT_COMMIT=$(GIT_COMMIT) \
		--build-arg BUILD_DATE=$(BUILD_DATE) \
		-t ${IMG} -f Containerfile .

.PHONY: docker-build-ci
docker-build-ci: ## Build docker images from pre-compiled host binaries (fast CI path).
	rm -rf bin/ci-stage && mkdir -p bin/ci-stage/controller bin/ci-stage/esc
	CGO_ENABLED=0 GOOS=linux GOARCH=$(GOARCH) go build -ldflags "$(LDFLAGS)" -o bin/ci-stage/controller/manager cmd/main.go
	CGO_ENABLED=0 GOOS=linux GOARCH=$(GOARCH) go build -ldflags "$(LDFLAGS)" -o bin/ci-stage/controller/router ./cmd/router
	CGO_ENABLED=0 GOOS=linux GOARCH=$(GOARCH) go build -ldflags "$(LDFLAGS)" -o bin/ci-stage/controller/telemetry ./cmd/telemetry
	CGO_ENABLED=0 GOOS=linux GOARCH=$(GOARCH) go build -ldflags "$(LDFLAGS)" -o bin/ci-stage/esc/exporter-set-controller cmd/exporter-set-controller/main.go
	$(CONTAINER_TOOL) build --build-arg BIN=manager -t $(IMG) -f Containerfile.prebuilt bin/ci-stage/controller
	$(CONTAINER_TOOL) build --build-arg BIN=exporter-set-controller -t $(EXPORTER_SET_CONTROLLER_IMG) -f Containerfile.prebuilt bin/ci-stage/esc

.PHONY: docker-build-exporter-set-controller-ci
docker-build-exporter-set-controller-ci: ## CI-optimized: host-compiled ESC binary, no multi-stage build.
	rm -rf bin/ci-stage/esc && mkdir -p bin/ci-stage/esc
	CGO_ENABLED=0 GOOS=linux GOARCH=$(GOARCH) go build -ldflags "$(LDFLAGS)" -o bin/ci-stage/esc/exporter-set-controller cmd/exporter-set-controller/main.go
	$(CONTAINER_TOOL) build --build-arg BIN=exporter-set-controller -t $(EXPORTER_SET_CONTROLLER_IMG) -f Containerfile.prebuilt bin/ci-stage/esc

.PHONY: docker-build-exporter-set-controller
docker-build-exporter-set-controller: ## Build docker image for the exporter-set-controller.
	$(CONTAINER_TOOL) build \
		--build-arg GIT_VERSION=$(GIT_VERSION) \
		--build-arg GIT_COMMIT=$(GIT_COMMIT) \
		--build-arg BUILD_DATE=$(BUILD_DATE) \
		-t ${EXPORTER_SET_CONTROLLER_IMG} -f Containerfile.exporter-set-controller .

.PHONY: docker-build-qemu-runtime
docker-build-qemu-runtime: ## Build docker image for the qemu-runtime container.
	$(CONTAINER_TOOL) build -t ${QEMU_RUNTIME_IMG} -f Containerfile.qemu-runtime .

.PHONY: docker-build-exporter
docker-build-exporter: ## Build the jumpstarter exporter image (includes jumpstarter-exec).
	$(CONTAINER_TOOL) build \
		--build-arg GIT_VERSION=$(GIT_VERSION) \
		-t ${EXPORTER_IMG} -f ../python/Containerfile ..

.PHONY: docker-push
docker-push: ## Push docker image with the manager.
	$(CONTAINER_TOOL) push ${IMG}

# PLATFORMS defines the target platforms for the manager image be built to provide support to multiple
# architectures. (i.e. make docker-buildx IMG=myregistry/mypoperator:0.0.1). To use this option you need to:
# - be able to use docker buildx. More info: https://docs.docker.com/build/buildx/
# - have enabled BuildKit. More info: https://docs.docker.com/develop/develop-images/build_enhancements/
# - be able to push the image to your registry (i.e. if you do not set a valid value via IMG=<myregistry/image:<tag>> then the export will fail)
# To adequately provide solutions that are compatible with multiple platforms, you should consider using this option.
PLATFORMS ?= linux/arm64,linux/amd64,linux/s390x,linux/ppc64le
.PHONY: docker-buildx
docker-buildx: ## Build and push docker image for the manager for cross-platform support
	- $(CONTAINER_TOOL) buildx create --name jumpstarter-controller-builder
	$(CONTAINER_TOOL) buildx use jumpstarter-controller-builder
	- $(CONTAINER_TOOL) buildx build --push --platform=$(PLATFORMS) \
			--build-arg GIT_VERSION=$(GIT_VERSION) \
			--build-arg GIT_COMMIT=$(GIT_COMMIT) \
			--build-arg BUILD_DATE=$(BUILD_DATE) \
			--tag ${DOCKER_REPO}:${DOCKER_TAG} \
			--tag ${DOCKER_REPO}:latest \
			-f Containerfile .
	- $(CONTAINER_TOOL) buildx rm jumpstarter-controller-builder

.PHONY: build-installer
build-installer: manifests generate kustomize ## Generate a consolidated YAML with CRDs and deployment.
	mkdir -p dist
	cd config/manager && $(KUSTOMIZE) edit set image controller=${IMG}
	$(KUSTOMIZE) build config/default > dist/install.yaml

##@ Deployment

ifndef ignore-not-found
  ignore-not-found = false
endif

.PHONY: install
install: manifests kustomize ## Install CRDs into the K8s cluster specified in ~/.kube/config.
	$(KUSTOMIZE) build config/crd | $(KUBECTL) apply -f -

.PHONY: uninstall
uninstall: manifests kustomize ## Uninstall CRDs from the K8s cluster specified in ~/.kube/config. Call with ignore-not-found=true to ignore resource not found errors during deletion.
	$(KUSTOMIZE) build config/crd | $(KUBECTL) delete --ignore-not-found=$(ignore-not-found) -f -

.PHONY: deploy
deploy: cluster grpcurl ## Deploy controller using the operator. Set SKIP_BUILD=1 to skip image builds. Set CLUSTER_TYPE=k3s for k3s.
ifeq ($(SKIP_BUILD),)
	$(MAKE) docker-build
	$(MAKE) docker-build-exporter-set-controller
	$(MAKE) build-operator
endif
	./hack/deploy_with_operator.sh

.PHONY: deploy-ci
deploy-ci: cluster grpcurl $(if $(SKIP_BUILD),,docker-build-ci build-operator-ci) ## CI-optimized deploy: host-compiled binaries, no multi-stage container builds.
	./hack/deploy_with_operator.sh


.PHONY: deploy-operator
deploy-operator: docker-build docker-build-exporter-set-controller build-operator cluster grpcurl ## Deploy only the operator (without Jumpstarter CR)
	NETWORKING_MODE=ingress DEPLOY_JUMPSTARTER=false ./hack/deploy_with_operator.sh

.PHONY: deploy-operator-ci
deploy-operator-ci: docker-build-ci build-operator-ci cluster grpcurl ## CI-optimized: host-compiled binaries, no multi-stage container builds.
	NETWORKING_MODE=ingress DEPLOY_JUMPSTARTER=false ./hack/deploy_with_operator.sh

.PHONY: test-operator-e2e
test-operator-e2e: grpcurl deploy-operator
	$(MAKE) -C deploy/operator test-e2e

.PHONY: test-operator-e2e-ci
test-operator-e2e-ci: grpcurl deploy-operator-ci ## CI-optimized e2e test (host-compiled Go, cached modules).
	$(MAKE) -C deploy/operator test-e2e

.PHONY: operator-logs
operator-logs:
	kubectl logs -n jumpstarter-operator-system -l app.kubernetes.io/name=jumpstarter-operator -f

.PHONY: deploy-with-operator-parallel
deploy-with-operator-parallel:
	make deploy -j5 --output-sync=target

.PHONY: deploy-exporters
deploy-exporters:
	./hack/demoenv/prepare_exporters.sh

.PHONY: deploy-exporterset-sample
deploy-exporterset-sample: ## Apply the x86_64 sample VirtualTargetClass and ExporterSet (requires operator-deployed exporter-set-controller).
	$(KUBECTL) apply -f hack/sample-x86_64.yaml

.PHONY: deploy-exporterset-sample-kind
deploy-exporterset-sample-kind: ## Apply the kind-friendly sample (no KVM requirement) for local testing.
	$(KUBECTL) apply -f hack/sample-x86_64-kind.yaml

.PHONY: undeploy-exporterset-sample
undeploy-exporterset-sample: ## Remove sample VirtualTargetClass and ExporterSet resources.
	$(KUBECTL) delete --ignore-not-found=true -f hack/sample-x86_64.yaml
	$(KUBECTL) delete --ignore-not-found=true -f hack/sample-x86_64-kind.yaml

.PHONY: undeploy
undeploy: kustomize ## Undeploy controller from the K8s cluster specified in ~/.kube/config. Call with ignore-not-found=true to ignore resource not found errors during deletion.
	$(KUSTOMIZE) build config/default | $(KUBECTL) delete --ignore-not-found=$(ignore-not-found) -f -

##@ Dependencies

## Location to install dependencies to
LOCALBIN ?= $(shell pwd)/bin
$(LOCALBIN):
	mkdir -p $(LOCALBIN)

## Tool Binaries
KUBECTL ?= kubectl
KUSTOMIZE ?= $(LOCALBIN)/kustomize
CONTROLLER_GEN ?= $(LOCALBIN)/controller-gen
ENVTEST ?= $(LOCALBIN)/setup-envtest
GOLANGCI_LINT = $(LOCALBIN)/golangci-lint
KIND = $(LOCALBIN)/kind
GRPCURL = $(LOCALBIN)/grpcurl

## Tool Versions
# renovate: datasource=github-releases depName=kubernetes-sigs/kustomize extractVersion=^kustomize/(?<version>v\d+\.\d+\.\d+)$
KUSTOMIZE_VERSION ?= v5.8.1
# renovate: datasource=github-releases depName=kubernetes-sigs/controller-tools extractVersion=^(?<version>v\d+\.\d+\.\d+)$
CONTROLLER_TOOLS_VERSION ?= v0.21.0
ENVTEST_VERSION ?= $(shell go list -m -f "{{ .Version }}" sigs.k8s.io/controller-runtime | awk -F'[v.]' '{printf "release-%d.%d", $$2, $$3}')
# renovate: datasource=github-releases depName=golangci/golangci-lint
GOLANGCI_LINT_VERSION ?= v2.13.2
# renovate: datasource=github-releases depName=kubernetes-sigs/kind
KIND_VERSION ?= v0.33.0
# renovate: datasource=github-releases depName=fullstorydev/grpcurl
GRPCURL_VERSION ?= v1.9.4

.PHONY: kustomize
kustomize: $(KUSTOMIZE) ## Download kustomize locally if necessary.
$(KUSTOMIZE): $(LOCALBIN)
	$(call go-install-tool,$(KUSTOMIZE),sigs.k8s.io/kustomize/kustomize/v5,$(KUSTOMIZE_VERSION))

.PHONY: controller-gen
controller-gen: $(CONTROLLER_GEN) ## Download controller-gen locally if necessary.
$(CONTROLLER_GEN): $(LOCALBIN)
	$(call go-install-tool,$(CONTROLLER_GEN),sigs.k8s.io/controller-tools/cmd/controller-gen,$(CONTROLLER_TOOLS_VERSION))

# renovate: datasource=docker depName=docker.io/bufbuild/buf
BUF_VERSION ?= 1.71.0

.PHONY: protobuf-gen
protobuf-gen:
	podman run --volume "$(shell cd .. && pwd):/workspace" --workdir /workspace/controller docker.io/bufbuild/buf:$(BUF_VERSION) generate

.PHONY: envtest
envtest: $(ENVTEST) ## Download setup-envtest locally if necessary.
$(ENVTEST): $(LOCALBIN)
	$(call go-install-tool,$(ENVTEST),sigs.k8s.io/controller-runtime/tools/setup-envtest,$(ENVTEST_VERSION))

.PHONY: golangci-lint
golangci-lint: $(GOLANGCI_LINT) ## Download golangci-lint locally if necessary.
$(GOLANGCI_LINT): $(LOCALBIN)
	$(call go-install-tool,$(GOLANGCI_LINT),github.com/golangci/golangci-lint/v2/cmd/golangci-lint,$(GOLANGCI_LINT_VERSION))

.PHONY: grpcurl
grpcurl: $(GRPCURL) ## Download grpcurl locally if necessary.
$(GRPCURL): $(LOCALBIN)
	$(call go-install-tool,$(GRPCURL),github.com/fullstorydev/grpcurl/cmd/grpcurl,$(GRPCURL_VERSION))

.PHONY: kind
kind: $(KIND)
$(KIND): $(LOCALBIN)
	$(call go-install-tool,$(KIND),sigs.k8s.io/kind,$(KIND_VERSION))

.PHONY: cluster
ifeq ($(CLUSTER_TYPE),k3s)
cluster:
	@source hack/utils && create_cluster
else ifeq ($(CLUSTER_TYPE),kind)
cluster: $(KIND)
	$(KIND) get clusters | grep jumpstarter || $(KIND) create cluster --name jumpstarter --config hack/kind_cluster.yaml
else
cluster:
	$(error Unknown CLUSTER_TYPE=$(CLUSTER_TYPE). Use 'kind' or 'k3s')
endif

ifeq ($(CLUSTER_TYPE),k3s)
clean:
	@source hack/utils && delete_cluster
else ifeq ($(CLUSTER_TYPE),kind)
clean: $(KIND)
	$(KIND) delete cluster --name jumpstarter
else
clean:
	$(error Unknown CLUSTER_TYPE=$(CLUSTER_TYPE). Use 'kind' or 'k3s')
endif



# go-install-tool will 'go install' any package with custom target and name of binary, if it doesn't exist
# $1 - target path with name of binary
# $2 - package url which can be installed
# $3 - specific version of package
define go-install-tool
@[ -f "$(1)-$(3)" ] || { \
set -e; \
package=$(2)@$(3) ;\
echo "Downloading $${package}" ;\
rm -f $(1) || true ;\
GOBIN=$(LOCALBIN) go install $${package} ;\
mv $(1) $(1)-$(3) ;\
} ;\
ln -sf $(1)-$(3) $(1)
endef
