#!/usr/bin/env bats
# E2E tests for hooks feature (beforeLease/afterLease)

JS_NAMESPACE="${JS_NAMESPACE:-jumpstarter-lab}"

# File to track bash wrapper process PIDs across tests
HOOKS_EXPORTER_PIDS_FILE="${BATS_RUN_TMPDIR:-/tmp}/hooks_exporter_pids.txt"

# Track which config is currently active
CURRENT_HOOKS_CONFIG=""

setup_file() {
  # Initialize the PIDs file at the start of all tests
  echo "" > "$HOOKS_EXPORTER_PIDS_FILE"
}

setup() {
  bats_load_library bats-support
  bats_load_library bats-assert

  bats_require_minimum_version 1.5.0
}

teardown_file() {
  echo "" >&2
  echo "========================================" >&2
  echo "HOOKS TESTS TEARDOWN_FILE RUNNING" >&2
  echo "========================================" >&2

  stop_hooks_exporter

  # Clean up client and exporter CRDs
  jmp admin delete client --namespace "${JS_NAMESPACE}" test-client-hooks --delete 2>/dev/null || true
  jmp admin delete exporter --namespace "${JS_NAMESPACE}" test-exporter-hooks --delete 2>/dev/null || true

  # Clean up the PIDs file
  rm -f "$HOOKS_EXPORTER_PIDS_FILE"

  echo "=== Hooks cleanup complete ===" >&2
}

# Helper: Stop hooks exporter processes
stop_hooks_exporter() {
  echo "=== Stopping hooks exporter processes ===" >&2

  # Read PIDs from file
  if [ -f "$HOOKS_EXPORTER_PIDS_FILE" ]; then
    while IFS= read -r pid; do
      if [ -n "$pid" ]; then
        echo "Checking PID $pid..." >&2
        if ps -p "$pid" > /dev/null 2>&1; then
          echo "  Killing PID $pid" >&2
          kill -9 "$pid" 2>/dev/null || true
        fi
      fi
    done < "$HOOKS_EXPORTER_PIDS_FILE"
    # Clear the file
    echo "" > "$HOOKS_EXPORTER_PIDS_FILE"
  fi

  # Kill any orphaned jmp processes for hooks exporter
  pkill -9 -f "jmp run --exporter test-exporter-hooks" 2>/dev/null || true

  # Give time for cleanup
  sleep 1
}

# Helper: Start hooks exporter with restart loop (normal mode)
start_hooks_exporter() {
  local config_file="$1"

  stop_hooks_exporter

  # Merge config into exporter yaml
  go run github.com/mikefarah/yq/v4@latest -i ". * load(\"e2e/exporters/${config_file}\")" \
    /etc/jumpstarter/exporters/test-exporter-hooks.yaml

  cat <<EOF | bash 3>&- &
while true; do
  jmp run --exporter test-exporter-hooks
done
EOF
  echo "$!" >> "$HOOKS_EXPORTER_PIDS_FILE"

  wait_for_hooks_exporter
}

# Helper: Start hooks exporter without restart loop (for exit mode tests)
start_hooks_exporter_single() {
  local config_file="$1"

  stop_hooks_exporter

  # Merge config into exporter yaml
  go run github.com/mikefarah/yq/v4@latest -i ". * load(\"e2e/exporters/${config_file}\")" \
    /etc/jumpstarter/exporters/test-exporter-hooks.yaml

  jmp run --exporter test-exporter-hooks &
  echo "$!" >> "$HOOKS_EXPORTER_PIDS_FILE"

  wait_for_hooks_exporter
}

# Helper: Wait for hooks exporter to be online and registered
wait_for_hooks_exporter() {
  # Brief delay to avoid catching pre-connect state
  sleep 2
  kubectl -n "${JS_NAMESPACE}" wait --timeout 5m --for=condition=Online --for=condition=Registered \
    exporters.jumpstarter.dev/test-exporter-hooks
}

# Helper: Wait for hooks exporter to go offline
wait_for_hooks_exporter_offline() {
  local max_wait=30
  local count=0

  while [ $count -lt $max_wait ]; do
    local status=$(kubectl -n "${JS_NAMESPACE}" get exporters.jumpstarter.dev/test-exporter-hooks \
      -o jsonpath='{.status.conditions[?(@.type=="Online")].status}' 2>/dev/null || echo "Unknown")

    if [ "$status" = "False" ] || [ "$status" = "Unknown" ]; then
      echo "Exporter is offline" >&2
      return 0
    fi

    sleep 1
    count=$((count + 1))
  done

  echo "Timed out waiting for exporter to go offline" >&2
  return 1
}

# Helper: Check if exporter process is still running
exporter_process_running() {
  if [ -f "$HOOKS_EXPORTER_PIDS_FILE" ]; then
    while IFS= read -r pid; do
      if [ -n "$pid" ] && ps -p "$pid" > /dev/null 2>&1; then
        return 0
      fi
    done < "$HOOKS_EXPORTER_PIDS_FILE"
  fi
  return 1
}

# ============================================================================
# Setup: Create client and exporter for hooks tests
# ============================================================================

@test "hooks: create client and exporter" {
  # Create client
  jmp admin create client -n "${JS_NAMESPACE}" test-client-hooks --unsafe --out /dev/null \
    --oidc-username dex:test-client-hooks

  # Create exporter with hooks label
  jmp admin create exporter -n "${JS_NAMESPACE}" test-exporter-hooks --out /dev/null \
    --oidc-username dex:test-exporter-hooks \
    --label example.com/board=hooks

  # Login client
  jmp login --client test-client-hooks \
    --endpoint "$ENDPOINT" --namespace "${JS_NAMESPACE}" --name test-client-hooks \
    --issuer https://dex.dex.svc.cluster.local:5556 \
    --username test-client-hooks@example.com --password password --unsafe

  # Login exporter
  jmp login --exporter test-exporter-hooks \
    --endpoint "$ENDPOINT" --namespace "${JS_NAMESPACE}" --name test-exporter-hooks \
    --issuer https://dex.dex.svc.cluster.local:5556 \
    --username test-exporter-hooks@example.com --password password
}

# ============================================================================
# Group A: Basic Hook Execution
# ============================================================================

@test "hooks A1: beforeLease hook executes" {
  start_hooks_exporter "exporter-hooks-before-only.yaml"

  run jmp shell --client test-client-hooks --selector example.com/board=hooks j power status

  assert_success
  assert_output --partial "BEFORE_HOOK_MARKER: executed"
}

@test "hooks A2: afterLease hook executes" {
  start_hooks_exporter "exporter-hooks-after-only.yaml"

  run jmp shell --client test-client-hooks --selector example.com/board=hooks j power status

  assert_success
  assert_output --partial "AFTER_HOOK_MARKER: executed"
}

@test "hooks A3: both hooks execute in correct order" {
  start_hooks_exporter "exporter-hooks-both.yaml"

  run jmp shell --client test-client-hooks --selector example.com/board=hooks j power status

  assert_success
  assert_output --partial "BEFORE_HOOK:"
  assert_output --partial "AFTER_HOOK:"

  # Verify order: BEFORE should appear before AFTER in output
  local before_pos=$(echo "$output" | grep -n "BEFORE_HOOK:" | head -1 | cut -d: -f1)
  local after_pos=$(echo "$output" | grep -n "AFTER_HOOK:" | head -1 | cut -d: -f1)

  [ "$before_pos" -lt "$after_pos" ]
}

# ============================================================================
# Group B: beforeLease Failure Modes
# ============================================================================

@test "hooks B1: beforeLease onFailure=warn allows shell to proceed" {
  start_hooks_exporter "exporter-hooks-before-fail-warn.yaml"

  run jmp shell --client test-client-hooks --selector example.com/board=hooks j power status

  # Shell should succeed despite hook failure
  assert_success
  assert_output --partial "HOOK_FAIL_WARN: will fail but continue"

  # Exporter should still be available
  wait_for_hooks_exporter
}

@test "hooks B2: beforeLease onFailure=endLease fails shell" {
  start_hooks_exporter "exporter-hooks-before-fail-endLease.yaml"

  run jmp shell --client test-client-hooks --selector example.com/board=hooks j power status

  # Shell should fail because lease was ended
  assert_failure
  assert_output --partial "HOOK_FAIL_ENDLEASE: will fail and end lease"

  # Exporter should still be available after failure
  wait_for_hooks_exporter
}

@test "hooks B3: beforeLease onFailure=exit shuts down exporter" {
  start_hooks_exporter_single "exporter-hooks-before-fail-exit.yaml"

  run jmp shell --client test-client-hooks --selector example.com/board=hooks j power status

  # Shell should fail
  assert_failure
  assert_output --partial "HOOK_FAIL_EXIT: shutting down"

  # Exporter process should have exited
  sleep 2
  run exporter_process_running
  assert_failure

  # Exporter should go offline
  wait_for_hooks_exporter_offline
}

# ============================================================================
# Group C: afterLease Failure Modes
# ============================================================================

@test "hooks C1: afterLease onFailure=warn keeps exporter available" {
  start_hooks_exporter "exporter-hooks-after-fail-warn.yaml"

  run jmp shell --client test-client-hooks --selector example.com/board=hooks j power status

  # Shell should succeed (afterLease runs after shell completes)
  assert_success
  assert_output --partial "HOOK_FAIL_WARN: afterLease failed but continuing"

  # Exporter should still be available
  wait_for_hooks_exporter
}

@test "hooks C2: afterLease onFailure=exit shuts down exporter" {
  start_hooks_exporter_single "exporter-hooks-after-fail-exit.yaml"

  run jmp shell --client test-client-hooks --selector example.com/board=hooks j power status

  # Shell command itself should succeed
  assert_success
  assert_output --partial "HOOK_FAIL_EXIT: afterLease failed, shutting down"

  # Exporter process should have exited
  sleep 2
  run exporter_process_running
  assert_failure

  # Exporter should go offline
  wait_for_hooks_exporter_offline
}

# ============================================================================
# Group D: Timeout Tests
# ============================================================================

@test "hooks D1: beforeLease timeout is treated as failure" {
  start_hooks_exporter "exporter-hooks-timeout.yaml"

  run jmp shell --client test-client-hooks --selector example.com/board=hooks j power status

  # Hook should timeout but shell proceeds (onFailure=warn)
  assert_success
  assert_output --partial "HOOK_TIMEOUT: starting"

  # Exporter should still be available
  wait_for_hooks_exporter
}

# ============================================================================
# Group E: j Commands in Hooks
# ============================================================================

@test "hooks E1: beforeLease can use j power on" {
  start_hooks_exporter "exporter-hooks-both.yaml"

  run jmp shell --client test-client-hooks --selector example.com/board=hooks j power status

  assert_success
  assert_output --partial "BEFORE_HOOK: complete"
  # The j power on command in beforeLease should have executed
}

@test "hooks E2: afterLease can use j power off" {
  start_hooks_exporter "exporter-hooks-both.yaml"

  run jmp shell --client test-client-hooks --selector example.com/board=hooks j power status

  assert_success
  assert_output --partial "AFTER_HOOK: complete"
  # The j power off command in afterLease should have executed
}

@test "hooks E3: environment variables are available in hooks" {
  start_hooks_exporter "exporter-hooks-both.yaml"

  run jmp shell --client test-client-hooks --selector example.com/board=hooks j power status

  assert_success
  # LEASE_NAME and CLIENT_NAME should be set (not empty)
  assert_output --partial "BEFORE_HOOK: lease="
  # The lease name should contain something after "lease="
  [[ "$output" =~ BEFORE_HOOK:\ lease=[^\ ]+\ client= ]]
}
