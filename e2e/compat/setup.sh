#!/usr/bin/env bash
# Jumpstarter Compatibility E2E Testing Setup Script
# This script sets up the environment for cross-version compatibility tests.
#
# No OIDC/dex is needed -- tests use legacy auth (--unsafe --save).
# Uses operator-based deployment.
#
# Environment variables:
#   COMPAT_SCENARIO      - "old-controller" or "old-client" (required)
#   COMPAT_CONTROLLER_TAG - Controller release tag for old-controller scenario (default: v0.8.1)
#   COMPAT_CLIENT_VERSION - PyPI version for old-client scenario (default: 0.7.4)

set -euo pipefail

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The parent e2e directory
E2E_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Get the monorepo root
REPO_ROOT="$(cd "$E2E_DIR/.." && pwd)"

# Default namespace for tests
export JS_NAMESPACE="${JS_NAMESPACE:-jumpstarter-lab}"

# Scenario configuration
COMPAT_SCENARIO="${COMPAT_SCENARIO:-old-controller}"
# renovate: datasource=github-releases depName=jumpstarter-dev/jumpstarter
COMPAT_CONTROLLER_TAG="${COMPAT_CONTROLLER_TAG:-v0.9.0}"
# renovate: datasource=pypi depName=jumpstarter
COMPAT_CLIENT_VERSION="${COMPAT_CLIENT_VERSION:-0.8.1}"

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $*"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $*"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $*"
}

# Check if running in CI
is_ci() {
    [ -n "${CI:-}" ] || [ -n "${GITHUB_ACTIONS:-}" ]
}

# Install dependencies
install_dependencies() {
    log_info "Installing dependencies..."

    if ! command -v uv &> /dev/null; then
        log_info "Installing uv..."
        grep -qxE '[0-9]+\.[0-9]+\.[0-9]+' "$REPO_ROOT/.uv-version" || { log_error "Invalid .uv-version"; exit 1; }
        curl -LsSf "https://astral.sh/uv/$(cat "$REPO_ROOT/.uv-version")/install.sh" | sh
        export PATH="$HOME/.cargo/bin:$PATH"
    fi

    grep -qxE '[0-9]+\.[0-9]+\.[0-9]+' "$REPO_ROOT/.py-version" || { log_error "Invalid .py-version"; exit 1; }
    PY_VERSION="$(cat "$REPO_ROOT/.py-version")"
    log_info "Installing Python ${PY_VERSION}..."
    uv python install "$PY_VERSION"

    log_info "Dependencies installed"
}

# Get the external IP for baseDomain
get_external_ip() {
    if which ip 2>/dev/null 1>/dev/null; then
        ip route get 1.1.1.1 | grep -oP 'src \K\S+'
    else
        local INTERFACE
        INTERFACE=$(route get 1.1.1.1 | grep interface | awk '{print $2}')
        ifconfig | grep "$INTERFACE" -A 10 | grep "inet " | grep -Fv 127.0.0.1 | awk '{print $2}' | head -n 1
    fi
}

# Create kind cluster and install grpcurl
create_cluster() {
    log_info "Creating kind cluster..."

    cd "$REPO_ROOT"
    make -C controller cluster grpcurl

    log_info "Kind cluster created"
}

deploy_old_controller() {
    log_info "Deploying old controller (version: $COMPAT_CONTROLLER_TAG)..."

    cd "$REPO_ROOT"

    # Compute networking variables
    local IP
    IP=$(get_external_ip)
    local BASEDOMAIN="jumpstarter.${IP}.nip.io"
    local GRPC_ENDPOINT="grpc.${BASEDOMAIN}:8082"
    local GRPC_ROUTER_ENDPOINT="router.${BASEDOMAIN}:8083"

    kubectl config use-context kind-jumpstarter

    # Install old controller using operator installer from the release assets
    local INSTALLER_URL="https://github.com/jumpstarter-dev/jumpstarter/releases/download/${COMPAT_CONTROLLER_TAG}/operator-installer.yaml"
    log_info "Installing old controller via operator (version: ${COMPAT_CONTROLLER_TAG})..."
    kubectl apply -f "${INSTALLER_URL}"

    log_info "Waiting for operator to be ready..."
    kubectl wait --namespace jumpstarter-operator-system \
        --for=condition=available deployment/jumpstarter-operator-controller-manager \
        --timeout=120s

    kubectl create namespace "${JS_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

    log_info "Creating Jumpstarter CR..."
    kubectl apply -f - <<EOF
apiVersion: operator.jumpstarter.dev/v1alpha1
kind: Jumpstarter
metadata:
  name: jumpstarter
  namespace: ${JS_NAMESPACE}
spec:
  baseDomain: ${BASEDOMAIN}
  certManager:
    enabled: false
  authentication:
    internal:
      prefix: "internal:"
      enabled: true
    autoProvisioning:
      enabled: true
  controller:
    replicas: 1
    grpc:
      endpoints:
        - address: ${GRPC_ENDPOINT}
          nodeport:
            enabled: true
            port: 30010
  routers:
    replicas: 1
    grpc:
      endpoints:
        - address: ${GRPC_ROUTER_ENDPOINT}
          nodeport:
            enabled: true
            port: 30011
EOF

    kubectl config set-context --current --namespace="${JS_NAMESPACE}"

    log_info "Waiting for controller deployment..."
    local retries=90
    while ! kubectl get deployment jumpstarter-controller -n "${JS_NAMESPACE}" > /dev/null 2>&1; do
        sleep 2
        retries=$((retries - 1))
        if [ ${retries} -eq 0 ]; then
            log_error "Controller deployment not created after 180s"
            exit 1
        fi
    done
    kubectl wait --namespace "${JS_NAMESPACE}" \
        --for=condition=available deployment/jumpstarter-controller \
        --timeout=180s

    # Wait for gRPC endpoints
    local GRPCURL="${REPO_ROOT}/controller/bin/grpcurl"
    log_info "Waiting for gRPC endpoints..."
    for ep in ${GRPC_ENDPOINT} ${GRPC_ROUTER_ENDPOINT}; do
        retries=60
        log_info "  Checking ${ep}..."
        while ! ${GRPCURL} -insecure "${ep}" list > /dev/null 2>&1; do
            sleep 2
            retries=$((retries - 1))
            if [ ${retries} -eq 0 ]; then
                log_error "${ep} not ready after 120s"
                exit 1
            fi
        done
    done

    log_info "Old controller deployed"
}

deploy_new_controller() {
    log_info "Deploying new controller from HEAD..."

    cd "$REPO_ROOT"

    make -C controller deploy

    log_info "New controller deployed"
}

# Install jumpstarter Python packages from HEAD (shared helper)
# shellcheck source=../lib/install.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib/install.sh"

# Install old client from PyPI into separate venv
install_old_client() {
    log_info "Installing old client v${COMPAT_CLIENT_VERSION} from PyPI..."

    local OLD_JMP_DIR="/tmp/jumpstarter-compat"

    # Clean up any previous installation
    rm -rf "$OLD_JMP_DIR"

    # Create a separate venv for old client
    uv venv "$OLD_JMP_DIR/.venv" --python "$(cat "$REPO_ROOT/.py-version")"

    # Install old packages from PyPI
    uv pip install --python "$OLD_JMP_DIR/.venv/bin/python" \
        "jumpstarter-cli==${COMPAT_CLIENT_VERSION}" \
        "jumpstarter==${COMPAT_CLIENT_VERSION}" \
        "jumpstarter-driver-composite==${COMPAT_CLIENT_VERSION}" \
        "jumpstarter-driver-power==${COMPAT_CLIENT_VERSION}" \
        "jumpstarter-driver-opendal==${COMPAT_CLIENT_VERSION}"

    # Verify the old jmp works
    "$OLD_JMP_DIR/.venv/bin/jmp" version || log_warn "Old jmp version command failed"

    export OLD_JMP="$OLD_JMP_DIR/.venv/bin/jmp"
    log_info "Old jmp CLI installed at: $OLD_JMP"
}

# Setup test environment
setup_test_environment() {
    log_info "Setting up test environment..."

    cd "$REPO_ROOT"

    # Get the controller endpoint from Jumpstarter CR
    export ENDPOINT
    local BASEDOMAIN
    BASEDOMAIN=$(kubectl get jumpstarter -n "${JS_NAMESPACE}" jumpstarter -o jsonpath='{.spec.baseDomain}')
    if [ -z "${BASEDOMAIN}" ]; then
        log_error "Failed to get baseDomain from Jumpstarter CR in namespace ${JS_NAMESPACE}. Is the controller deployed with a Jumpstarter CR?"
        exit 1
    fi
    ENDPOINT="grpc.${BASEDOMAIN}:8082"

    log_info "Controller endpoint: $ENDPOINT"

    # Setup exporters directory
    if [ ! -d /etc/jumpstarter/exporters ] || [ ! -w /etc/jumpstarter/exporters ]; then
        log_info "Setting up exporters directory (requires sudo)..."
        sudo mkdir -p /etc/jumpstarter/exporters
        sudo chown "$USER" /etc/jumpstarter/exporters
    else
        log_info "Exporters directory already exists and is writable"
    fi

    # Write setup configuration
    cat > "$REPO_ROOT/.e2e-setup-complete" <<EOF
ENDPOINT=$ENDPOINT
E2E_TEST_NS=$JS_NAMESPACE
REPO_ROOT=$REPO_ROOT
SCRIPT_DIR=$SCRIPT_DIR
EOF

    log_info "Test environment ready"
}

# Main execution
main() {
    log_info "=== Jumpstarter Compatibility E2E Setup ==="
    log_info "Scenario: $COMPAT_SCENARIO"
    log_info "Namespace: $JS_NAMESPACE"
    log_info "Repository Root: $REPO_ROOT"
    echo ""

    install_dependencies
    echo ""

    create_cluster
    echo ""

    case "$COMPAT_SCENARIO" in
        old-controller)
            log_info "Scenario: Old Controller ($COMPAT_CONTROLLER_TAG) + New Client/Exporter"
            deploy_old_controller
            echo ""
            install_jumpstarter
            ;;
        old-client)
            log_info "Scenario: New Controller + Old Client/Exporter ($COMPAT_CLIENT_VERSION)"
            deploy_new_controller
            echo ""
            install_jumpstarter
            echo ""
            install_old_client
            ;;
        *)
            log_error "Unknown COMPAT_SCENARIO: $COMPAT_SCENARIO (expected 'old-controller' or 'old-client')"
            exit 1
            ;;
    esac
    echo ""

    setup_test_environment
    echo ""

    log_info "=== Compat setup complete! ==="
    log_info "Scenario: $COMPAT_SCENARIO"
    log_info "To run tests: make e2e-compat-run COMPAT_TEST=<old-controller|old-client>"
}

main "$@"
