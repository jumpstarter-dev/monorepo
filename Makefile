# Jumpstarter Monorepo Makefile
#
# This Makefile provides common targets that delegate to subdirectory Makefiles.
#

# Subdirectories containing projects
SUBDIRS := python protocol controller rust e2e

# UV invocation for docs (must run from python/ where pyproject.toml lives)
UV_DOCS = cd python && uv run --isolated --all-packages --group docs

# Default target
.PHONY: all
all: build

# Help target - shows available commands
.PHONY: help
help:
	@echo "Jumpstarter Monorepo"
	@echo ""
	@echo "Available targets:"
	@echo "  make all        - Build all projects (default)"
	@echo "  make build      - Build all projects"
	@echo "  make test       - Run tests in all projects"
	@echo "  make clean      - Clean build artifacts in all projects"
	@echo "  make lint       - Run linters in all projects"
	@echo "  make fmt        - Format code in all projects"
	@echo ""
	@echo "Documentation targets:"
	@echo "  make docs            - Build HTML documentation with warnings as errors"
	@echo "  make docs-singlehtml - Build single HTML page documentation"
	@echo "  make docs-all        - Build multiversion documentation"
	@echo "  make docs-serve      - Build and serve documentation locally"
	@echo "  make docs-serve-all  - Build and serve multiversion documentation locally"
	@echo "  make docs-linkcheck  - Check documentation links"
	@echo "  make docs-test       - Run documentation tests"
	@echo ""
	@echo "End-to-end testing:"
	@echo "  make e2e-setup             - Setup e2e test environment (one-time)"
	@echo "  make e2e-run               - Run e2e suite (excludes lease-churn; includes ExporterSet QEMU)"
	@echo "  make e2e                   - Same as e2e-run"
	@echo "  make e2e-exporterset-qemu  - Run ExporterSet QEMU e2e only"
	@echo "  make e2e-lease-churn       - Run core e2e including lease-churn cycles"
	@echo "  make e2e-full              - Full setup + run (for CI or first time)"
	@echo "  make e2e-clean             - Clean up e2e test environment (delete cluster, certs, etc.)"
	@echo ""
	@echo "Per-project targets:"
	@echo "  make build-<project>  - Build specific project"
	@echo "  make test-<project>   - Test specific project"
	@echo "  make clean-<project>  - Clean specific project"
	@echo ""
	@echo ""
	@echo "Rust targets:"
	@echo "  make build-rust   - Build Rust workspace"
	@echo "  make test-rust    - Run Rust tests"
	@echo "  make clean-rust   - Clean Rust build artifacts"
	@echo ""
	@echo "Projects: $(SUBDIRS)"

# ---- Documentation targets ----

.PHONY: docs docs-singlehtml docs-all docs-serve docs-serve-all docs-linkcheck \
	docs-test docs-test-grpc docs-check-grpc docs-generate-crds docs-generate-grpc clean-docs

docs-generate-crds:
	$(UV_DOCS) python3 ../docs/source/reference/generate-crd-docs.py

docs-generate-grpc:
	$(UV_DOCS) python3 ../docs/source/reference/generate_grpc_docs.py

docs: docs-generate-crds docs-generate-grpc
	$(UV_DOCS) $(MAKE) -C ../docs html SPHINXOPTS="-W --keep-going -n"

docs-singlehtml: docs-generate-crds docs-generate-grpc
	$(UV_DOCS) $(MAKE) -C ../docs singlehtml

docs-all: docs-generate-crds docs-generate-grpc
	$(UV_DOCS) $(MAKE) -C ../docs multiversion

docs-serve: clean-docs docs-generate-crds docs-generate-grpc
	$(UV_DOCS) $(MAKE) -C ../docs serve

docs-serve-all: clean-docs docs-all
	$(UV_DOCS) $(MAKE) -C ../docs serve-multiversion

docs-check-grpc:
	@TMPDIR1=$$(mktemp -d) && TMPDIR2=$$(mktemp -d) && \
	trap 'rm -rf "$$TMPDIR1" "$$TMPDIR2"' EXIT && \
	cd python && \
	uv run --isolated --all-packages --group docs python3 -c \
		"import sys; sys.path.insert(0, '../docs/source/reference'); from generate_grpc_docs import main; main(output_dir='$$TMPDIR1')" && \
	uv run --isolated --all-packages --group docs python3 -c \
		"import sys; sys.path.insert(0, '../docs/source/reference'); from generate_grpc_docs import main; main(output_dir='$$TMPDIR2')" && \
	diff -r "$$TMPDIR1/" "$$TMPDIR2/"

docs-test-grpc:
	$(UV_DOCS) pytest --cov=../docs/source/reference --cov-report=xml:../docs/source/reference/coverage.xml ../docs/source/reference/generate_grpc_docs_test.py

docs-test: docs-generate-crds docs-generate-grpc
	$(UV_DOCS) $(MAKE) -C ../docs doctest

docs-linkcheck: docs-generate-crds docs-generate-grpc
	$(UV_DOCS) $(MAKE) -C ../docs linkcheck

clean-docs:
	rm -rf docs/build

# ---- Build targets ----

# Build all projects
.PHONY: build
build:
	@for dir in $(SUBDIRS); do \
		if [ -f $$dir/Makefile ]; then \
			echo "Building $$dir..."; \
			$(MAKE) -C $$dir build || true; \
		fi \
	done

# Test all projects
.PHONY: test
test:
	@for dir in $(SUBDIRS); do \
		if [ -f $$dir/Makefile ]; then \
			echo "Testing $$dir..."; \
			$(MAKE) -C $$dir test ; \
		fi \
	done

# Clean all projects
.PHONY: clean
clean:
	@for dir in $(SUBDIRS); do \
		if [ -f $$dir/Makefile ]; then \
			echo "Cleaning $$dir..."; \
			$(MAKE) -C $$dir clean || true; \
		fi \
	done

# Lint all projects
.PHONY: lint
lint:
	@for dir in $(SUBDIRS); do \
		if [ -f $$dir/Makefile ]; then \
			echo "Linting $$dir..."; \
			$(MAKE) -C $$dir lint; \
		fi \
	done

# Format all projects
.PHONY: fmt
fmt:
	@for dir in $(SUBDIRS); do \
		if [ -f $$dir/Makefile ]; then \
			echo "Formatting $$dir..."; \
			$(MAKE) -C $$dir fmt || true; \
		fi \
	done

# Per-project build targets
.PHONY: build-python build-protocol build-controller build-rust build-e2e
build-python:
	@if [ -f python/Makefile ]; then $(MAKE) -C python build; fi

build-protocol:
	@if [ -f protocol/Makefile ]; then $(MAKE) -C protocol build; fi

build-controller:
	@if [ -f controller/Makefile ]; then $(MAKE) -C controller build; fi

build-rust:
	@if [ -f rust/Makefile ]; then $(MAKE) -C rust build; fi

build-e2e:
	@if [ -f e2e/Makefile ]; then $(MAKE) -C e2e build; fi

# Per-project test targets
.PHONY: test-python test-protocol test-controller test-rust test-e2e
test-python:
	@if [ -f python/Makefile ]; then $(MAKE) -C python test; fi

test-protocol:
	@if [ -f protocol/Makefile ]; then $(MAKE) -C protocol test; fi

test-controller:
	@if [ -f controller/Makefile ]; then $(MAKE) -C controller test; fi

test-rust:
	@if [ -f rust/Makefile ]; then $(MAKE) -C rust test; fi

# Setup e2e testing environment (one-time)
.PHONY: e2e-setup
e2e-setup:
	@echo "Setting up e2e test environment..."
	@bash e2e/setup-e2e.sh

# Run e2e tests (excludes lease-churn; override with GINKGO_LABEL_FILTER)
.PHONY: e2e-run
e2e-run:
	@echo "Running e2e tests..."
	@GINKGO_LABEL_FILTER="$(or $(GINKGO_LABEL_FILTER),!lease-churn)" bash e2e/run-e2e.sh

# Focused local run of ExporterSet QEMU e2e (also covered by make e2e-run / CI).
.PHONY: e2e-exporterset-qemu
e2e-exporterset-qemu:
	@echo "Running ExporterSet QEMU e2e tests..."
	@GINKGO_LABEL_FILTER=exporterset-qemu bash e2e/run-e2e.sh

# Core lane including the lease-churn cycle spec (create/release ×20).
# Needs the Ordered core setup specs, so this is `core` not `lease-churn` alone.
.PHONY: e2e-lease-churn
e2e-lease-churn:
	@echo "Running core e2e tests including lease-churn..."
	@GINKGO_LABEL_FILTER=core bash e2e/run-e2e.sh

# Prints a lane-grouped index of all e2e specs (group + name + labels + source),
# generated via `ginkgo --dry-run`. Use it to check e2e/README.md's lane/test/
# source accuracy; requires no live cluster.
.PHONY: e2e-spec-index
e2e-spec-index:
	@./e2e/scripts/gen-spec-index.sh

# Convenience alias for running e2e tests
.PHONY: e2e
e2e: e2e-run

# Full e2e setup + run
.PHONY: e2e-full
e2e-full:
	@bash e2e/run-e2e.sh --full

# Clean up e2e test environment
.PHONY: e2e-clean
e2e-clean:
	@echo "Cleaning up e2e test environment..."
	@if command -v kind >/dev/null 2>&1; then \
		echo "Deleting jumpstarter kind cluster..."; \
		kind delete cluster --name jumpstarter 2>/dev/null || true; \
	fi
	@echo "Removing certificates and setup files..."
	@rm -f ca.pem ca-key.pem ca.csr server.pem server-key.pem server.csr
	@rm -f .e2e-setup-complete
	@echo "Removing local e2e configuration directory..."
	@rm -rf .e2e
	@echo "Removing virtual environment..."
	@rm -rf .venv
	@echo "Removing local bats libraries..."
	@rm -rf .bats
	@if [ -d /etc/jumpstarter/exporters ] && [ -w /etc/jumpstarter/exporters ]; then \
		echo "Removing exporter configs..."; \
		rm -rf /etc/jumpstarter/exporters/* 2>/dev/null || true; \
	fi
	@echo "✓ E2E test environment cleaned"
	@echo ""
	@echo "Note: You may need to manually remove the dex entry from /etc/hosts:"
	@echo "  sudo sed -i.bak '/dex.dex.svc.cluster.local/d' /etc/hosts"

# Backward compatibility alias
.PHONY: test-e2e
test-e2e: e2e-run

# Compatibility E2E testing (cross-version tests, separate from main e2e)
COMPAT_SCENARIO ?= old-controller
COMPAT_TEST ?= old-controller
# renovate: datasource=github-releases depName=jumpstarter-dev/jumpstarter
COMPAT_CONTROLLER_TAG ?= v0.9.0
# renovate: datasource=pypi depName=jumpstarter
COMPAT_CLIENT_VERSION ?= 0.8.1

.PHONY: e2e-compat-setup
e2e-compat-setup:
	@echo "Setting up compat e2e (scenario: $(COMPAT_SCENARIO))..."
	@COMPAT_SCENARIO=$(COMPAT_SCENARIO) COMPAT_CONTROLLER_TAG=$(COMPAT_CONTROLLER_TAG) \
	 COMPAT_CLIENT_VERSION=$(COMPAT_CLIENT_VERSION) bash e2e/compat/setup.sh

.PHONY: e2e-compat-run
e2e-compat-run:
	@echo "Running compat e2e (test: $(COMPAT_TEST))..."
	@COMPAT_TEST=$(COMPAT_TEST) bash e2e/compat/run.sh

# Per-project clean targets
.PHONY: clean-python clean-protocol clean-controller clean-rust clean-e2e
clean-python:
	@if [ -f python/Makefile ]; then $(MAKE) -C python clean; fi

clean-protocol:
	@if [ -f protocol/Makefile ]; then $(MAKE) -C protocol clean; fi

clean-controller:
	@if [ -f controller/Makefile ]; then $(MAKE) -C controller clean; fi

clean-rust:
	@if [ -f rust/Makefile ]; then $(MAKE) -C rust clean; fi

clean-e2e:
	@if [ -f e2e/Makefile ]; then $(MAKE) -C e2e clean; fi
