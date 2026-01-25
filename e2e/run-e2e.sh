#!/usr/bin/env bash
# Jumpstarter End-to-End Test Runner
# This script runs the e2e test suite (assumes setup-e2e.sh was run first)

set -euo pipefail

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Get the monorepo root (parent of e2e directory)
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

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

# Check if setup was completed
check_setup() {
    if [ ! -f "$REPO_ROOT/.e2e-setup-complete" ]; then
        log_error "Setup not complete! Please run setup-e2e.sh first:"
        log_error "  bash e2e/setup-e2e.sh"
        log_error ""
        log_error "Or in CI mode, run the full setup automatically"
        return 1
    fi
    
    # Load setup configuration
    source "$REPO_ROOT/.e2e-setup-complete"
    
    # Export SSL certificate paths for Python
    export SSL_CERT_FILE
    export REQUESTS_CA_BUNDLE
    
    # Verify critical components are still running
    if ! kubectl get namespace "$JS_NAMESPACE" &> /dev/null; then
        log_error "Namespace $JS_NAMESPACE not found. Please run setup-e2e.sh again."
        return 1
    fi
    
    log_info "✓ Setup verified"
    return 0
}

# Setup environment for bats
setup_bats_env() {
    # Always set BATS_LIB_PATH to include local libraries
    local LOCAL_BATS_LIB="$REPO_ROOT/.bats/lib"
    
    if [ -d "$LOCAL_BATS_LIB" ]; then
        export BATS_LIB_PATH="$LOCAL_BATS_LIB:${BATS_LIB_PATH:-}"
        log_info "Set BATS_LIB_PATH to local libraries: $BATS_LIB_PATH"
    else
        log_warn "Local bats libraries not found at $LOCAL_BATS_LIB"
        log_warn "You may need to run setup-e2e.sh first"
    fi
}

# Run the tests
run_tests() {
    log_info "Running jumpstarter e2e tests..."
    
    cd "$REPO_ROOT"
    
    # Activate virtual environment
    if [ -f .venv/bin/activate ]; then
        source .venv/bin/activate
    else
        log_error "Virtual environment not found. Please run setup-e2e.sh first."
        exit 1
    fi
    
    # Use insecure GRPC for testing
    export JUMPSTARTER_GRPC_INSECURE=1
    
    # Export variables for bats
    export JS_NAMESPACE="${JS_NAMESPACE}"
    export ENDPOINT="${ENDPOINT}"
    
    # Setup bats environment
    setup_bats_env
    
    # Run bats tests (all .bats files in e2e directory)
    log_info "Running bats tests..."
    bats --show-output-of-passing-tests --verbose-run "$SCRIPT_DIR"/*.bats
}

# Full setup and run (for CI or first-time use)
full_run() {
    log_info "Running full setup + test cycle..."
    
    if [ -f "$SCRIPT_DIR/setup-e2e.sh" ]; then
        bash "$SCRIPT_DIR/setup-e2e.sh"
    else
        log_error "setup-e2e.sh not found!"
        exit 1
    fi
    
    # After setup, load the configuration
    if [ -f "$REPO_ROOT/.e2e-setup-complete" ]; then
        source "$REPO_ROOT/.e2e-setup-complete"
        # Export SSL certificate paths for Python
        export SSL_CERT_FILE
        export REQUESTS_CA_BUNDLE
    fi
    
    run_tests
}

# Main execution
main() {
    # Default namespace
    export JS_NAMESPACE="${JS_NAMESPACE:-jumpstarter-lab}"
    
    log_info "=== Jumpstarter E2E Test Runner ==="
    log_info "Namespace: $JS_NAMESPACE"
    log_info "Repository Root: $REPO_ROOT"
    echo ""
    
    # If --full flag is passed, always run full setup
    if [[ "${1:-}" == "--full" ]]; then
        full_run
    # In CI mode, check if setup was already done
    elif is_ci; then
        if check_setup 2>/dev/null; then
            log_info "Setup already complete, skipping setup and running tests..."
            run_tests
        else
            log_info "Setup not found in CI, running full setup..."
            full_run
        fi
    else
        # Local development: require setup to be done first
        if check_setup; then
            run_tests
        else
            log_error ""
            log_error "Setup is required before running tests."
            log_error ""
            log_error "Options:"
            log_error "  1. Run setup first: bash e2e/setup-e2e.sh"
            log_error "  2. Run full cycle:  bash e2e/run-e2e.sh --full"
            exit 1
        fi
    fi
    
    echo ""
    log_info "✓✓✓ All e2e tests completed successfully! ✓✓✓"
}

# Run main function
main "$@"
