# ADB Driver

`jumpstarter-driver-adb` tunnels Android Debug Bridge (ADB) connections over Jumpstarter, enabling remote Android device access via standard ADB tools such as Android Studio.

## How it works

Devices are plugged into the **exporter** over USB. Jumpstarter moves the ADB
protocol to your machine; ADB and Android Studio do everything else.

```text
DUT ──USB──▶ EXPORTER ──Jumpstarter tunnel──▶ YOU
             (owns the USB                    (your own adb,
              connection)                      Studio, tradefed…)
```

Two commands, and the difference is **whose ADB server your tools talk to**:

```bash
j adb attach     # remote devices are ADDED to your ADB server (5037)
                 #   -> they appear in Android Studio, beside your emulators
                 #   -> many devices, many exporters, all at once

j adb tunnel     # your tools are POINTED AT the exporter's ADB server
                 #   -> you see the exporter's devices instead of your own
                 #   -> nothing needed on the device; right for CI
```

Everything else is ordinary adb, passed straight through:

```bash
j adb devices
j adb shell getprop ro.product.model
j adb logcat
```

Choose `attach` to work on a remote device in your IDE alongside local ones.
Choose `tunnel` when you own your ADB server, or when the device cannot expose
adbd over TCP — see [Two ways to reach the exporter's devices](#two-ways-to-reach-the-exporters-devices).

## Installation

```shell
pip3 install --extra-index-url https://pkg.jumpstarter.dev/simple/ jumpstarter-driver-adb
```

For the optional Python ADB API:

```shell
pip3 install --extra-index-url https://pkg.jumpstarter.dev/simple/ "jumpstarter-driver-adb[python-api]"
```

## Configuration

Example exporter configuration:

```yaml
export:
  adb:
    type: jumpstarter_driver_adb.driver.AdbServer
    config:
      host: "127.0.0.1"
      port: 15037
```

### Configuration Parameters

| Parameter | Description                                    | Type | Required | Default                    |
| --------- | ---------------------------------------------- | ---- | -------- | -------------------------- |
| adb_path        | Path to the ADB executable on the exporter          | str   | no       | "adb" (resolved from PATH) |
| host            | Host address of the ADB server on the exporter      | str   | no       | "127.0.0.1"                |
| port            | Port of the ADB server on the exporter              | int   | no       | 15037                      |
| connect_timeout | Timeout (seconds) for `connect`/`disconnect` commands | float | no       | 30.0                       |
| attach_slots     | Number of devices that can be attached at once (see `attach`) | int   | no       | 8                          |
| attach_base_port | First exporter-side port used for attach slots         | int   | no       | 16000                      |
| adopt_existing_server | Use an ADB server already listening on `port` instead of starting another (see below) | bool  | no       | true                       |

### An ADB server already running on the exporter

An ADB server **claims** the USB devices it finds, and only one server can hold a
given device. So if a server is already listening on the driver's `port` — started
by hand, by udev, by a previous run, or by a developer working on the exporter
directly — a second one does not give a second view of those devices. It gives an
*empty* one, and `adb start-server` reports success either way, so the driver would
come up seeing no devices at all while looking healthy.

By default the driver therefore **adopts** a server already on its port, and leaves
it running at teardown rather than killing a server other processes are using. You
will see:

```text
adopting the ADB server already listening on 127.0.0.1:15037; it owns the
connected devices, and this driver will leave it running
```

Set `adopt_existing_server: false` to always insist on starting (and later killing)
its own server. Note this only helps when nothing else is holding the devices.

If something that is *not* an ADB server holds the port, the driver declines to
adopt it and logs a warning. This matters because `adb start-server` and
`adb devices` both block forever against such a listener rather than failing, so all
of the driver's adb calls are bounded by `connect_timeout`.

### Port Assignment

The exporter runs its own ADB server on a non-standard port (default: 15037)
to avoid conflicting with the standard ADB server on port 5037
(if Jumpstarter is running in local mode). This is important because tools like
Android Studio automatically start and maintain an ADB server on port 5037 and
will restart it if killed.

On the client side, the `tunnel` command binds to an auto-assigned port by
default. Use `-P` to specify a port (such as 5037) if needed.

## Usage

### Run ADB commands

All standard adb commands are passed through to the remote ADB server:

```bash
# List devices
j adb devices

# Interactive shell
j adb shell

# Run a command on the device
j adb shell getprop ro.product.model

# Install an app
j adb install app.apk

# View device logs
j adb logcat

# Push/pull files
j adb push local_file.txt /sdcard/
j adb pull /sdcard/remote_file.txt .
```

### Two ways to reach the exporter's devices

The driver offers two models. They differ in **who owns the ADB server**, and that
single question decides which one you want.

|  | `attach` | `tunnel` |
|---|---|---|
| Your tooling talks to | **your own** ADB server (5037) | the exporter's ADB server |
| Server ownership | you don't need to own it | you must own it |
| If you have no local ADB server | fine — `adb connect` starts one on 5037 | fine — you own it by definition |
| Devices visible at once | many, from many exporters | those of one exporter |
| Coexists with Android Studio | yes | only if you win port 5037 |
| Configuration needed | none | `ANDROID_ADB_SERVER_PORT` |

**`attach` — add a remote device to the ADB server you already run.**

```bash
j adb attach                    # every usable device on the exporter
j adb attach emulator-5554      # or pick by serial
```

The exporter publishes the device's `adbd` on a forward slot, Jumpstarter tunnels
that slot, and plain `adb connect` adds it locally. Because `adb connect` is
**additive**, the device joins whatever your ADB server already holds — your own
emulator, another bench, a phone — and every Android tool sees it without being
told anything: `adb`, `logcat`, Android Studio, the Android CLI, tradefed, gradle.

This is the right default. Jumpstarter moves the ADB protocol between the two
machines; ADB does the rest.

**`tunnel` — point your tooling at the exporter's ADB server.**

Right when you *do* own your ADB server and want the exporter's view of the world
— CI, a headless runner, a container. It replaces your server rather than adding
to it, which is exactly wrong when an IDE is running.

#### How `attach` works

```text
EXPORTER   adb server (dynamic — it already knows what is plugged in)
             │  adb forward tcp:<slot> tcp:5555      ← per device, on demand
             ↓
TUNNEL     Jumpstarter streams the slot               ← all Jumpstarter does
             ↓
CLIENT     adb connect 127.0.0.1:<port>               ← plain adb
             ↓
           your existing ADB server (5037), untouched
```

Devices need **no declaration**: any serial `adb devices` reports on the exporter
can be attached, including one that appeared *after* the lease began — a
hotplugged phone, an emulator started mid-session.

Slots are a small fixed pool (`attach_slots`, default 8) of TCP children with a
dynamic device→slot mapping. The pool is fixed because Jumpstarter children are
resolved when the lease is established and stream methods take no arguments, so a
per-device child would freeze the device list at lease start and could never
express hotplug. The mapping is dynamic, which is what keeps ADB's behaviour.

Requirements and limits:

- The device's `adbd` must listen on TCP (`persist.adb.tcp.port`, commonly 5555).
  A stock phone needs `adb tcpip 5555` first — note this restarts `adbd` and may
  drop the USB connection.
- The local address (`127.0.0.1:<port>`) is assigned per session, not stable
  across sessions. Anything that remembers a device by address (an IDE run
  target) should re-select it after re-attaching.
- `attach` blocks while holding the tunnel, and detaches on Ctrl+C. If the client
  is killed rather than interrupted, two things are left behind, and they need
  different remedies:
  - the local `adb connect` entry — clear it with `adb disconnect <address>`;
  - the **exporter's slot**, which `adb disconnect` does *not* touch, because
    releasing it means calling `detach_device` on the exporter. Re-run
    `j adb attach <serial>` and exit with Ctrl+C to release it (attaching is
    idempotent and reuses the same slot), or restart the exporter. Otherwise the
    slot stays occupied and, after `attach_slots` of these, attaching fails with
    "no free attach slot".
- Direct mode has no lease arbitration, so two clients attaching the same device
  will interfere. Use distributed mode for a shared fleet.

#### Devices that come and go

By default `attach` takes the device list once, at startup: most exporters have a
fixed set of devices bolted to a bench, and polling a list that never changes only
adds noise.

Pass `--hotplug` when the hardware really does change while you work — a device
being re-flashed, rebooted into a different mode, or physically re-plugged:

```bash
j adb attach --hotplug                       # follow devices as they appear/vanish
j adb attach --hotplug --poll-interval 5     # check every 5s instead of 2s
```

Then the exporter's device list is re-read on each tick: a device that appears is
attached and announced, one that disappears is detached and its slot released. A
device that cannot be attached (no `adbd` on TCP) is reported once and not retried
until it disappears and comes back, so a broken device does not spam every tick.

Note this only makes *attachment* follow the hardware. It does not make the local
address stable — a re-plugged device generally comes back on a new
`127.0.0.1:<port>`, so an IDE run target pinned to the old one needs re-selecting.

### Persistent tunnel

`attach` and `tunnel` are the only Jumpstarter-specific commands. All others
(including `start-server`, `kill-server`, `connect`, `disconnect`, `reconnect`,
`pair`) are passed through to the remote ADB server.

```bash
# Create a persistent ADB tunnel (auto-assigned port)
j adb tunnel

# Create a tunnel on a specific port
j adb tunnel -P 5038

# Background the tunnel for continued shell use
j adb tunnel &
```

When a persistent tunnel is running, subsequent `j adb` commands will
automatically reuse it instead of creating a new ephemeral tunnel. This
makes commands faster and ensures a consistent connection.

For native `adb` or external tools, export the env vars printed by the
`tunnel` command in another terminal.

### Unsupported commands

The `nodaemon` command is not supported as it would start a local ADB server
process, ignoring the tunnel entirely.

### Connecting to a remote device

When the Android device is **not** attached to the exporter over USB but is
reachable over the network (for example a virtual device such as
[Cuttlefish](https://source.android.com/docs/devices/cuttlefish), or a device
exposing `adb` over TCP/IP), the exporter's ADB server must `connect` to it
before any `adb` command will see it.

The `connect_device` / `disconnect_device` driver methods run
`adb connect <host:port>` / `adb disconnect <host:port>` on the exporter. The
address is supplied by the caller — this driver does **not** discover or scan
for devices. Timeouts and command failures raise, so callers can react instead
of receiving a silent error string.

#### From the CLI

`connect` and `disconnect` are also plain adb commands, so they pass through the
tunnel like any other:

```bash
# Connect the exporter's ADB server to a networked device, then use it
j adb connect 10.0.0.5:6520
j adb devices
j adb shell getprop ro.product.model
j adb disconnect 10.0.0.5:6520
```

#### From a parent (composite) driver

The intended use case is a higher-level driver that owns the device lifecycle
and knows the address deterministically — no IP discovery needed. For example,
the Cuttlefish driver embeds an `AdbServer` child and connects to a pinned
address derived from its own config (`host` + an ADB port computed from the
instance number) after the virtual device is created:

```python
class CuttlefishServer(CompositeInterface, Driver):
    def __post_init__(self):
        super().__post_init__()
        # AdbServer runs on the exporter; the parent drives connect/disconnect
        self.children["adb"] = AdbServer(host="127.0.0.1", port=self.adb_server_port)

    def _adb_device(self) -> str:
        # Address is known from config, never scanned
        return f"{self.host}:{6520 + self.instance_num - 1}"

    def _connect(self):
        adb = self.children["adb"]
        device = self._adb_device()
        try:
            adb.connect_device(device)
        except (subprocess.CalledProcessError, TimeoutError) as e:
            # Device may not be up yet; the boot-wait loop below reconnects.
            self.logger.warning("ADB connect to %s failed (%s); retrying while waiting for boot", device, e)
        # unexpected exceptions (config/programming errors) propagate

    def _wait_for_boot(self):
        adb = self.children["adb"]
        device = self._adb_device()
        deadline = time.monotonic() + self.boot_timeout
        while time.monotonic() < deadline:
            try:
                adb.connect_device(device)
                if self._is_booted(device):
                    return
            except (subprocess.CalledProcessError, TimeoutError):
                pass
            time.sleep(3)
        raise TimeoutError(f"{device} did not come online within {self.boot_timeout}s")
```

Because `connect_device` raises on failure or timeout, the parent catches only
the *expected* connection failures (letting configuration or programming errors
propagate) and drives its own retry loop rather than parsing return strings.

### Integration with Android Ecosystem Tools

#### Forward ADB for external tools

The `tunnel` command creates a persistent tunnel that other `j adb` commands
reuse automatically. For external tools, export the env vars printed by the
command:

```bash
# In the jmp shell:
j adb tunnel
```

```bash
# In another terminal, using the port printed by the tunnel command:
export ANDROID_ADB_SERVER_ADDRESS=127.0.0.1
export ANDROID_ADB_SERVER_PORT=<port>
adb devices
```

#### Android Studio

Use `j adb attach`. The device appears in Studio's device chooser with **no
configuration**: no `adb.server.port`, no environment variables, no restart.

```bash
j adb attach
# HVA1234567 -> 127.0.0.1:51141
# Attached to your local ADB server; Android Studio will list them.
# Press Ctrl+C to detach.
```

Leave it running for as long as you want the device available.

Why not `tunnel -P 5037`: Studio starts its own ADB server on 5037 and
**respawns it within ~3 seconds** of `adb kill-server`, so the port cannot
reliably be taken over while Studio is open. `attach` sidesteps the contest
entirely by adding the device *to* Studio's server rather than replacing it.

#### Trade Federation (tradefed)

tradefed discovers devices through the ADB server via the
`ANDROID_ADB_SERVER_PORT` environment variable:

```bash
# Terminal 1: Start the tunnel
j adb tunnel
# Note the port, e.g. 54321

# Terminal 2: Run tradefed with the tunnel port
export ANDROID_ADB_SERVER_PORT=54321
tradefed.sh
# > list devices   <-- shows remote devices
```

#### Python API

You can also perform interactions via ADB using the
[`adbutils`](https://github.com/openatx/adbutils) Python package.

```python
# Requires: pip install jumpstarter-driver-adb[python-api]
import adbutils

with client.adb.forward_adb(port=0) as (host, port):
    adb = adbutils.AdbClient(host=host, port=port)
    for device in adb.device_list():
        print(device.serial, device.prop.model)
```

### CLI

#### Standard ADB commands (passed through)

| Usage                         | Description                                       |
| ----------------------------- | ------------------------------------------------- |
| `j adb <command> [args...]`   | Run any adb command against the remote ADB server |
| `j adb devices`               | List connected devices                            |
| `j adb shell [command]`       | Open a shell or run a command on the device       |
| `j adb install <apk>`         | Install an APK                                    |
| `j adb push <local> <remote>` | Push a file to the device                         |
| `j adb pull <remote> <local>` | Pull a file from the device                       |
| `j adb logcat`                | View device logs                                  |

#### Jumpstarter-specific commands

| Usage                     | Description                                                             |
| ------------------------- | ----------------------------------------------------------------------- |
| `j adb attach [SERIAL...]` | Add the exporter's devices to your own ADB server (works with Android Studio, and starts a local server if you have none). Defaults to every usable device. Blocks; Ctrl+C detaches. Add `--hotplug` to follow device changes. |
| `j adb tunnel [-P PORT]`  | Create a persistent ADB tunnel (auto-assigned port, or specify with -P) |

#### Options

| Option       | Description                          | Default   |
| ------------ | ------------------------------------ | --------- |
| `-H HOST`    | Local address to tunnel ADB to       | 127.0.0.1 |
| `-P PORT`    | Local port to tunnel ADB to (0=auto) | 0         |
| `--adb PATH` | Path to local adb executable         | adb       |
| `--hotplug`  | `attach`: keep following devices that appear or vanish while running | off |
| `--poll-interval SECS` | `attach`: seconds between device checks, with `--hotplug` | 2.0 |

## API Reference

### Driver

```{eval-rst}
.. autoclass:: jumpstarter_driver_adb.driver.AdbServer()
    :members: attach_device, detach_device, list_attached, list_devices, start_server, kill_server, connect_device, disconnect_device
```

### Client

```{eval-rst}
.. autoclass:: jumpstarter_driver_adb.client.AdbClient()
    :members: attach, forward_adb, devices
```
