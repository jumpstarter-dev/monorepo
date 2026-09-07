# Cuttlefish Driver

`jumpstarter-driver-cuttlefish` manages
[Android Cuttlefish](https://source.android.com/docs/devices/cuttlefish)
virtual devices through the
[Host Orchestrator](https://github.com/google/android-cuttlefish) REST API.
It provides full CVD (Cuttlefish Virtual Device) lifecycle management through
standard Jumpstarter interfaces: `VirtualPowerInterface` for on/off/cycle,
plus cuttlefish-specific operations
(snapshot, powerwash, restart).

## Installation

```{code-block} console
:substitutions:
$ pip3 install --extra-index-url {{index_url}} jumpstarter-driver-cuttlefish
```

### Prerequisites

- A running Cuttlefish Host Orchestrator (port 2080 by default)

## Host Setup

A `cvd-images` named volume mounted at `/home/vsoc-01/fetch` persists
fetched AOSP images across container restarts. Instance state (`/var/tmp/cvd`)
is deliberately kept ephemeral — restarting the container gives you a clean
slate with no orphaned instance directories.

```bash
# 1. Pull the orchestration image
podman pull us-docker.pkg.dev/android-cuttlefish-artifacts/cuttlefish-orchestration/cuttlefish-orchestration:stable

# 2. Create a named volume for AOSP images
podman volume create cvd-images

# 3. Start the container
#    --network=host: netsim and rootcanal bind to 127.0.0.1 inside the
#    container, so without host networking they'd be unreachable from
#    outside. Host networking shares the VM's network namespace directly.
#
#    Security note: --privileged + --network=host gives the container full
#    access to the VM's network stack. HO, netsim, and rootcanal have no
#    auth — only deploy on dedicated, non-public hosts.
podman run -d \
  --name cuttlefish-orchestrator \
  --restart=always \
  --privileged \
  --network=host \
  -v cvd-images:/home/vsoc-01/fetch:Z \
  -v /opt/cuttlefish:/opt/cuttlefish:Z \
  us-docker.pkg.dev/android-cuttlefish-artifacts/cuttlefish-orchestration/cuttlefish-orchestration:stable

# 4. Fix permissions
podman exec cuttlefish-orchestrator chown -R httpcvd:httpcvd /home/vsoc-01/fetch

# 5. Fetch AOSP images (one-time, ~2 minutes)
podman exec cuttlefish-orchestrator cvd fetch \
  --default_build=aosp-android-latest-release/aosp_cf_x86_64_auto-userdebug \
  --target_directory=/home/vsoc-01/fetch

# 6. Verify
curl -s http://localhost:2080/_debug/statusz  # should return 200
curl -s http://localhost:2080/cvds             # should return {"cvds":[]}
```

### Ports

After a CVD boots, the following ports are available on the host.
All per-instance ports use the same offset: `base + instance_num - 1`.

| Service | Base port | Instance 1 | Instance 2 |
|---------|-----------|------------|------------|
| Host Orchestrator | 2080 | 2080 (fixed) | 2080 (fixed) |
| ADB | 6520 | 6520 | 6521 |
| Netsim REST | 7681 | 7681 | 7682 |
| Rootcanal HCI | 7300 | 7300 | 7301 |

When using `instance_num > 1`, update the netsim `port` and bt_peer
`hci_port` in the exporter config to match.

### SSH tunnel (local development only)

For local development, when running the exporter on your workstation
instead of as a pod, tunnel ports from the VM. This works because
`--network=host` places netsim and rootcanal on the VM's loopback -
the tunnel's `localhost` target reaches them directly.

```bash
ssh -L 2080:localhost:2080 \
    -L 6520:localhost:6520 \
    -L 7681:localhost:7681 \
    -L 7300:localhost:7300 \
    fedora@<vm-ip> -p 22000 -N
```

In production, the exporter runs as a pod and the `host` config
points to the cuttlefish VM's address directly - no tunnel needed.

### Resetting stale state

If CVDs get stuck or orphaned, clear stale state inside the container:

```bash
podman exec cuttlefish-orchestrator bash -c '
    rm -rf /var/tmp/cvd/[0-9]* /var/tmp/cvd/lock/* /tmp/cf_avd_* /tmp/vsock_*
    chown -R httpcvd:httpcvd /var/tmp/cvd/
'
```

Or restart the container - ephemeral `/var/tmp/cvd` means a restart is
equivalent to a full reset. Fetched images in the `cvd-images` volume
are preserved.

### Teardown

Delete CVDs and snapshots when done to avoid accumulation:

```bash
j power off --destroy          # deletes the CVD
j cuttlefish snapshot delete <id>  # remove specific snapshots
```

## Configuration

Example exporter configuration:

```yaml
export:
  cuttlefish:
    type: jumpstarter_driver_cuttlefish.driver.Cuttlefish
    config:
      host: localhost
      port: 2080
      instance_num: 1
      env_config:
        instances:
          - disk:
              default_build: /home/vsoc-01/fetch
            vm:
              enable_virtiofs: false   # required for snapshot support
        common:
          host_package: /home/vsoc-01/fetch
          gpu_mode: guest_swiftshader  # required for snapshot support
  netsim:
    type: jumpstarter_driver_netsim.driver.Netsim
    config:
      host: localhost
      port: 7681       # 7681 + instance_num - 1
  bt_peer:
    type: jumpstarter_driver_bt_peer.driver.BtPeer
    config:
      hci_host: 127.0.0.1
      hci_port: 7300    # 7300 + instance_num - 1
  power:
    ref: cuttlefish.power
  adb:
    ref: cuttlefish.adb
```

### Configuration Parameters

| Parameter       | Description                         | Type | Required | Default     |
| --------------- | ----------------------------------- | ---- | -------- | ----------- |
| host            | Host Orchestrator hostname          | str  | no       | "localhost" |
| port            | Host Orchestrator HTTP port         | int  | no       | 2080        |
| group           | CVD group name passed to `cvd load`. HO auto-assigns a different group name (e.g. `cvd_1`); the driver tracks the assigned name internally. | str  | no       | "cvd"       |
| name            | CVD instance name within the group  | str  | no       | "1"         |
| instance_num    | CVD instance number (determines ADB/netsim/HCI ports). Must match HO's assigned slot. Pinning avoids drift (see `env_config` example). | int  | no       | 1           |
| adb_server_port | ADB server port on the exporter     | int  | no       | 15037       |
| boot_timeout    | Seconds to wait for boot on power on| int  | no       | 300         |
| env_config      | Default env_config for CVD creation | dict | no       | {}          |

This is a **composite driver** with three children:
- **power** — `VirtualPowerInterface`: `j power on`, `j power off [--destroy]`, `j power cycle`
- **storage** — `FlasherInterface`: flashes Cuttlefish image archives through the HO user-artifact API
- **adb** — ADB server for device communication

The exporter config also typically includes sibling drivers:
- **netsim** (`jumpstarter-driver-netsim`) — virtual radio control (BLE, WiFi, UWB) via netsim REST API
- **bt_peer** (`jumpstarter-driver-bt-peer`) — Bluetooth peer device via bumble + rootcanal HCI

Use `ref:` entries in the exporter config to expose children at the top level.

### Storage flash

Storage flash uploads archives to Host Orchestrator, extracts them, and merges
their contents into one image directory. The documented target names are:

- `images` — the device image ZIP (`*-img-*.zip`)
- `host_package` — the Cuttlefish host package (`cvd-host_package.tar.gz`)

For a split AAOS build, flash both artifacts in one call:

```console
j storage flash \
  --target images:/path/to/aosp_cf_x86_64_auto-img-<build>.zip \
  --target host_package:/path/to/cvd-host_package.tar.gz
```

The same targets accept `http://` or `https://` URLs. The client passes those
URLs as presigned GET resources; the exporter downloads and streams them into
the same hashing, deduplication, extraction, and staging path, so the artifact
does not pass through the client.

An untargeted single archive is treated as a complete bundle and is injected
into both `common.host_package` and every
`instances[].disk.default_build`. A targeted call may provide only one role;
when an active generation exists, the missing role is carried forward by
checksum into a new immutable image directory. This means
`flash(host_package)` after a complete flash keeps the existing device images.
For a fresh CVD, provide both targeted artifacts (or a complete untargeted
bundle). Targeted roles must match the archive format; an unknown target is
rejected.

Flashing only stages the files. Recreate the CVD to select them:

```console
j power off --destroy
j power on
```

The staged and active image-directory generations are exporter in-memory state.
An exporter restart loses that state; the user artifacts remain in Host
Orchestrator, but the configured `env_config` is used until the artifacts are
flashed again. `power off --destroy` releases the active image directory after
the CVD is deleted. At exporter teardown, pending staged directories are
deleted best-effort; an active directory is retained because its CVD may still
be running. Cleanup failures are logged at WARNING and may require manual
cleanup on the Host Orchestrator.

## Usage

### CLI

```bash
# Power on (creates CVD if none exists, starts if stopped)
j power on

# Power off (stops CVD, keeps state)
j power off

# Power off and delete CVD entirely
j power off --destroy

# Power cycle
j power cycle

# Health check
j cuttlefish status

# List all CVDs
j cuttlefish list

# Get this CVD's details
j cuttlefish get

# Restart the CVD
j cuttlefish restart

# Factory reset
j cuttlefish powerwash

# Simulate power button press
j cuttlefish powerbtn

# List running operations
j cuttlefish ops

# Snapshot management
# Requires: x86_64 host, enable_virtiofs: false, gpu_mode: guest_swiftshader
j cuttlefish snapshot create --id my-snapshot
j cuttlefish snapshot delete <snapshot_id>
```

### Python API

```python
from jumpstarter.common.utils import serve
from jumpstarter_driver_cuttlefish.driver import Cuttlefish

driver = Cuttlefish(
    host="localhost",
    port=2080,
    env_config={
        "instances": [{"disk": {"default_build": "/home/vsoc-01/fetch"}}],
        "common": {"host_package": "/home/vsoc-01/fetch"},
    },
)
with serve(driver) as client:
    # Check Host Orchestrator is reachable
    print(client.status())  # "OK"

    # Power on (creates CVD from env_config)
    client.power.on()

    # List CVDs
    cvds = client.list_cvds()
    print(cvds)

    # Snapshots
    client.create_snapshot(snapshot_id="baseline")

    # Cleanup
    client.power.off(destroy=True)
```

## Architecture

```text
┌────────────┐     gRPC      ┌────────────────┐    HTTP     ┌──────────────────┐
│ jmp shell  │──────────────►│ Exporter       │────────────►│ Host             │
│ (client)   │               │  ├─ cuttlefish │  :2080      │ Orchestrator     │
│            │               │  │  ├─ power   │             │                  │
│            │               │  │  ├─ storage │             │  cvd create/     │
│            │               │  │  └─ adb     │             │  start/stop      │
│            │               │  ├─ netsim ────│── :7681 ──►│  netsim REST     │
│            │               │  └─ bt_peer ───│── :7300 ──►│  rootcanal HCI   │
└────────────┘               └────────────────┘             └────────┬─────────┘
                                                                     │
                                                                     ▼
                                                            ┌──────────────────┐
                                                            │ Cuttlefish VM    │
                                                            │ (Android guest)  │
                                                            │  ADB :6520       │
                                                            └──────────────────┘
```

The driver is a thin REST client that translates Jumpstarter driver calls into
Host Orchestrator API requests. Long-running operations (create, start, stop,
delete) are handled asynchronously - the driver polls the `/operations/:wait`
endpoint until completion or timeout.

`power.on()` waits for full boot by default (`boot_timeout=300`). It polls
`adb connect` + `adb devices` until the device is online, then waits for
`sys.boot_completed=1`. Set `boot_timeout: 0` to skip the wait.

### CVD Build Sources

The `env_config` supports two build source formats in `disk.default_build`:

- **Android CI**: `@ab/<branch>/<target>` - fetches images from Android Build servers.
  Example: `@ab/aosp-android-latest-release/aosp_cf_x86_64_auto-userdebug` (AAOS)
- **Local path**: `/path/to/android/build` - uses pre-fetched images on the host.

## API Reference

### Driver

```{eval-rst}
.. autoclass:: jumpstarter_driver_cuttlefish.driver.Cuttlefish()
   :members:
```

### Client

```{eval-rst}
.. autoclass:: jumpstarter_driver_cuttlefish.client.CuttlefishClient()
   :members:
```
