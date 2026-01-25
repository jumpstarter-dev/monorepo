JS_NAMESPACE="${JS_NAMESPACE:-jumpstarter-lab}"

# File to track bash wrapper process PIDs across tests
EXPORTER_PIDS_FILE="${BATS_RUN_TMPDIR:-/tmp}/exporter_pids.txt"

setup_file() {
  # Initialize the PIDs file at the start of all tests
  echo "" > "$EXPORTER_PIDS_FILE"
}

setup() {
  bats_load_library bats-support
  bats_load_library bats-assert

  bats_require_minimum_version 1.5.0
}

# teardown_file runs once after all tests complete (requires bats-core 1.5.0+)
teardown_file() {
  echo "" >&2
  echo "========================================" >&2
  echo "TEARDOWN_FILE RUNNING" >&2
  echo "========================================" >&2
  echo "=== Cleaning up exporter bash processes ===" >&2
  
  # Read PIDs from file
  if [ -f "$EXPORTER_PIDS_FILE" ]; then
    local pids=$(cat "$EXPORTER_PIDS_FILE" | tr '\n' ' ')
    echo "Tracked PIDs from file: $pids" >&2
    
    while IFS= read -r pid; do
      if [ -n "$pid" ]; then
        echo "Checking PID $pid..." >&2
        if ps -p "$pid" > /dev/null 2>&1; then
          echo "  Killing PID $pid" >&2
          kill -9 "$pid" 2>/dev/null || true
        else
          echo "  PID $pid already terminated" >&2
        fi
      fi
    done < "$EXPORTER_PIDS_FILE"
  else
    echo "No PIDs file found at $EXPORTER_PIDS_FILE" >&2
  fi
  
  echo "Checking for orphaned jmp processes..." >&2
  local orphans=$(pgrep -f "jmp run --exporter" 2>/dev/null | wc -l)
  echo "Found $orphans orphaned jmp processes" >&2
  
  # remove orphaned processes
  pkill -9 -f "jmp run --exporter" 2>/dev/null || true
  
  # Clean up the PIDs file
  rm -f "$EXPORTER_PIDS_FILE"
  
  echo "=== Cleanup complete ===" >&2
}

wait_for_exporter() {
  # After a lease operation the exporter is disconnecting from controller and reconnecting.
  # The disconnect can take a short while so let's avoid catching the pre-disconnect state and early return
  sleep 2
  kubectl -n "${JS_NAMESPACE}" wait --timeout 20m --for=condition=Online --for=condition=Registered \
    exporters.jumpstarter.dev/test-exporter-oidc
  kubectl -n "${JS_NAMESPACE}" wait --timeout 20m --for=condition=Online --for=condition=Registered \
    exporters.jumpstarter.dev/test-exporter-sa
  kubectl -n "${JS_NAMESPACE}" wait --timeout 20m --for=condition=Online --for=condition=Registered \
    exporters.jumpstarter.dev/test-exporter-legacy
}

@test "can create clients with admin cli" {
  jmp admin create client -n "${JS_NAMESPACE}" test-client-oidc     --unsafe --out /dev/null \
    --oidc-username dex:test-client-oidc
  jmp admin create client -n "${JS_NAMESPACE}" test-client-sa       --unsafe --out /dev/null \
    --oidc-username dex:system:serviceaccount:"${JS_NAMESPACE}":test-client-sa
  jmp admin create client -n "${JS_NAMESPACE}" test-client-legacy   --unsafe --save
}

@test "can create exporters with admin cli" {
  jmp admin create exporter -n "${JS_NAMESPACE}" test-exporter-oidc   --out /dev/null \
    --oidc-username dex:test-exporter-oidc \
    --label example.com/board=oidc
  jmp admin create exporter -n "${JS_NAMESPACE}" test-exporter-sa     --out /dev/null \
    --oidc-username dex:system:serviceaccount:"${JS_NAMESPACE}":test-exporter-sa \
    --label example.com/board=sa
  jmp admin create exporter -n "${JS_NAMESPACE}" test-exporter-legacy --save \
    --label example.com/board=legacy
}

@test "can login with oidc" {
  jmp config client   list
  jmp config exporter list

  jmp login --client test-client-oidc \
    --endpoint "$ENDPOINT" --namespace "${JS_NAMESPACE}" --name test-client-oidc \
    --issuer https://dex.dex.svc.cluster.local:5556 \
    --username test-client-oidc@example.com --password password --unsafe

  jmp login --client test-client-oidc-provisioning \
    --endpoint "$ENDPOINT" --namespace "${JS_NAMESPACE}" --name "" \
    --issuer https://dex.dex.svc.cluster.local:5556 \
    --username test-client-oidc-provisioning@example.com --password password --unsafe

  jmp login --client test-client-sa \
    --endpoint "$ENDPOINT" --namespace "${JS_NAMESPACE}" --name test-client-sa \
    --issuer https://dex.dex.svc.cluster.local:5556 \
    --connector-id kubernetes \
    --token $(kubectl create -n "${JS_NAMESPACE}" token test-client-sa) --unsafe

  jmp login --exporter test-exporter-oidc \
    --endpoint "$ENDPOINT" --namespace "${JS_NAMESPACE}" --name test-exporter-oidc \
    --issuer https://dex.dex.svc.cluster.local:5556 \
    --username test-exporter-oidc@example.com --password password

  jmp login --exporter test-exporter-sa \
    --endpoint "$ENDPOINT" --namespace "${JS_NAMESPACE}" --name test-exporter-sa \
    --issuer https://dex.dex.svc.cluster.local:5556 \
    --connector-id kubernetes \
    --token $(kubectl create -n "${JS_NAMESPACE}" token test-exporter-sa)

  go run github.com/mikefarah/yq/v4@latest -i ". * load(\"e2e/exporters/exporter.yaml\")" \
    /etc/jumpstarter/exporters/test-exporter-oidc.yaml
  go run github.com/mikefarah/yq/v4@latest -i ". * load(\"e2e/exporters/exporter.yaml\")" \
    /etc/jumpstarter/exporters/test-exporter-sa.yaml
  go run github.com/mikefarah/yq/v4@latest -i ". * load(\"e2e/exporters/exporter.yaml\")" \
    /etc/jumpstarter/exporters/test-exporter-legacy.yaml
 
  jmp config client   list
  jmp config exporter list
}

@test "can run exporters" {
  cat <<EOF | bash 3>&- &
while true; do
  jmp run --exporter test-exporter-oidc
done
EOF
  echo "$!" >> "$EXPORTER_PIDS_FILE"

  cat <<EOF | bash 3>&- &
while true; do
  jmp run --exporter test-exporter-sa
done
EOF
  echo "$!" >> "$EXPORTER_PIDS_FILE"

  cat <<EOF | bash 3>&- &
while true; do
  jmp run --exporter test-exporter-legacy
done
EOF
  echo "$!" >> "$EXPORTER_PIDS_FILE"

  wait_for_exporter
}

@test "can specify client config only using environment variables" {
  wait_for_exporter

  # we feed the namespace into JMP_NAMESPACE along with all the other client details
  # to verify that the client can operate without a config file
  JMP_NAMESPACE="${JS_NAMESPACE}" \
  JMP_DRIVERS_ALLOW="*" \
  JMP_NAME=test-client-legacy \
  JMP_ENDPOINT=$(kubectl get clients.jumpstarter.dev -n "${JS_NAMESPACE}" test-client-legacy -o 'jsonpath={.status.endpoint}') \
  JMP_TOKEN=$(kubectl get secrets -n "${JS_NAMESPACE}" test-client-legacy-client -o 'jsonpath={.data.token}' | base64 -d) \
  jmp shell --selector example.com/board=oidc j power on
}

@test "can operate on leases" {
  wait_for_exporter

  jmp config client use test-client-oidc

  jmp create lease     --selector example.com/board=oidc --duration 1d
  jmp get    leases
  jmp get    exporters
  jmp delete leases    --all
}

@test "can lease and connect to exporters" {
  wait_for_exporter

  jmp shell --client test-client-oidc   --selector example.com/board=oidc   j power on
  jmp shell --client test-client-sa     --selector example.com/board=sa     j power on
  jmp shell --client test-client-legacy --selector example.com/board=legacy j power on

  wait_for_exporter
  jmp shell --client test-client-oidc-provisioning --selector example.com/board=oidc j power on
}

@test "can get crds with admin cli" {
  jmp admin get client --namespace "${JS_NAMESPACE}"
  jmp admin get exporter --namespace "${JS_NAMESPACE}"
  jmp admin get lease --namespace "${JS_NAMESPACE}"
}

@test "can delete clients with admin cli" {
  kubectl -n "${JS_NAMESPACE}" get secret test-client-oidc-client
  kubectl -n "${JS_NAMESPACE}" get clients.jumpstarter.dev/test-client-oidc
  kubectl -n "${JS_NAMESPACE}" get clients.jumpstarter.dev/test-client-sa
  kubectl -n "${JS_NAMESPACE}" get clients.jumpstarter.dev/test-client-legacy

  jmp admin delete client --namespace "${JS_NAMESPACE}" test-client-oidc   --delete
  jmp admin delete client --namespace "${JS_NAMESPACE}" test-client-sa     --delete
  jmp admin delete client --namespace "${JS_NAMESPACE}" test-client-legacy --delete

  run ! kubectl -n "${JS_NAMESPACE}" get secret test-client-oidc-client
  run ! kubectl -n "${JS_NAMESPACE}" get clients.jumpstarter.dev/test-client-oidc
  run ! kubectl -n "${JS_NAMESPACE}" get clients.jumpstarter.dev/test-client-sa
  run ! kubectl -n "${JS_NAMESPACE}" get clients.jumpstarter.dev/test-client-legacy
}

@test "can delete exporters with admin cli" {
  kubectl -n "${JS_NAMESPACE}" get secret test-exporter-oidc-exporter
  kubectl -n "${JS_NAMESPACE}" get exporters.jumpstarter.dev/test-exporter-oidc
  kubectl -n "${JS_NAMESPACE}" get exporters.jumpstarter.dev/test-exporter-sa
  kubectl -n "${JS_NAMESPACE}" get exporters.jumpstarter.dev/test-exporter-legacy

  jmp admin delete exporter --namespace "${JS_NAMESPACE}" test-exporter-oidc   --delete
  jmp admin delete exporter --namespace "${JS_NAMESPACE}" test-exporter-sa     --delete
  jmp admin delete exporter --namespace "${JS_NAMESPACE}" test-exporter-legacy --delete

  run ! kubectl -n "${JS_NAMESPACE}" get secret test-exporter-oidc-exporter
  run ! kubectl -n "${JS_NAMESPACE}" get exporters.jumpstarter.dev/test-exporter-oidc
  run ! kubectl -n "${JS_NAMESPACE}" get exporters.jumpstarter.dev/test-exporter-sa
  run ! kubectl -n "${JS_NAMESPACE}" get exporters.jumpstarter.dev/test-exporter-legacy
}
