import asyncio
import json
import os
import socket
import subprocess
import sys
from contextlib import AbstractContextManager, ExitStack, contextmanager
from typing import Any, Generator, Protocol

import anyio
import click
from jumpstarter_driver_network.adapters import TcpPortforwardAdapter
from xdg_base_dirs import xdg_state_home

from jumpstarter.client import DriverClient

_UNSUPPORTED_ADB_COMMANDS = frozenset({"nodaemon"})

# Where the persistent tunnel records itself, in a private per-user directory.
#
# Deliberately **not** in the shared temp directory, where this used to live. The
# file records an endpoint that `_read_tunnel_state` then connects to, so whoever
# can write it chooses where a later `j adb` connects — and a world-writable path
# lets any local user pre-create it. Liveness checking cannot save us: a planted
# record can name a pid that really is alive.
#
# The directory is created 0700, and ownership is re-checked on read, so a file
# belonging to someone else is discarded rather than trusted.
_TUNNEL_STATE_FILE = str(xdg_state_home() / "jumpstarter" / "adb-tunnel.json")


def _validate_adb_args(args: tuple[str, ...]) -> None:
    """Validate adb command arguments, raising UsageError for unsupported commands."""
    for arg in args:
        if arg in _UNSUPPORTED_ADB_COMMANDS:
            raise click.UsageError(f"'{arg}' is not supported through the Jumpstarter ADB tunnel")


def _is_cancelled(exc: BaseException) -> bool:
    """Whether *exc* is a task cancellation.

    Checked without ``anyio.get_cancelled_exc_class()``, which resolves the *running*
    backend and raises ``NoEventLoopError`` when there is none. These waits run in a
    worker thread, off the loop, so asking there would raise from the except arm and
    mask the very cancellation being handled -- reproduced as a test failure.

    Both backends' cancellations are matched directly: asyncio's ``CancelledError``
    (which trio's also subclasses on recent versions) and trio's ``Cancelled`` by
    name, so trio need not be installed.
    """
    if isinstance(exc, asyncio.CancelledError):
        return True
    return type(exc).__name__ == "Cancelled" and type(exc).__module__.startswith("trio")


def _wait_for_interrupt(client: DriverClient) -> None:
    """Block until the CLI is interrupted, then return so teardown can run.

    The wait must happen **in the event loop**, not in this thread.

    Driver CLIs run in a worker thread driven by a ``BlockingPortal``, while
    ``jmp shell`` handles Ctrl+C with ``anyio.open_signal_receiver`` and cancels
    the enclosing task group. A thread-side wait — ``Event().wait()``,
    ``time.sleep()``, or ``signal.signal()`` — cannot observe either: Python
    delivers signals only to the main thread, and anyio cancellation only unwinds
    tasks. So the CLI printed "SIGINT pressed, terminating" while the worker
    thread kept waiting, the caller's ``finally`` never ran (leaving a stale
    ``adb connect`` entry behind), and a second Ctrl+C hung in
    ``threading._shutdown``.

    Sleeping through the portal puts the wait in a real task, so the cancel scope
    unwinds it and ``portal.call`` re-raises here, letting teardown proceed.
    """
    try:
        client.portal.call(anyio.sleep_forever)
    except (KeyboardInterrupt, SystemExit, GeneratorExit, RuntimeError):
        # RuntimeError covers the portal already being shut down when we ask.
        return
    except BaseException as e:
        # Cancellation derives from BaseException, not Exception, so it needs its
        # own arm.
        if _is_cancelled(e):
            return
        raise


def _adb_connect(adb: str, target: str) -> str:
    """Run ``adb connect target``, raising if it did not actually connect.

    The exit status cannot be used: ``adb connect`` returns 0 even when it fails,
    reporting the failure on **stdout** instead ("failed to connect to ...", "failed
    to resolve host: ...", "bad port number ..."). Verified against adb 1.0.41, for
    a refused port, an unresolvable host and an out-of-range port — all rc=0. So a
    `check=True` here would silently accept a device that never attached, leaving the
    caller to believe it had one.

    A local ADB server is *not* required: if none is running, ``adb connect`` starts
    one on 5037 first. That is the whole point of attach — the developer does not
    have to own, configure, or even have an ADB server.

    Returns:
        adb's own message, for logging.

    Raises:
        RuntimeError: adb reported a failure, or timed out.
    """
    try:
        result = subprocess.run([adb, "connect", target], check=False, capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise RuntimeError(f"`adb connect {target}` failed: {e}") from e

    message = (result.stdout or "").strip() or (result.stderr or "").strip()
    # Matched against adb's own format strings, which are the only two successes:
    # "connected to %s" and "already connected to %s". The failures are
    # "failed to connect to ...", "bad port number ...", "cannot connect to daemon ...".
    if result.returncode != 0 or not message.startswith(("connected to", "already connected to")):
        raise RuntimeError(f"`adb connect {target}` did not connect: {message or 'no output'}")
    return message


def _sleep_through_portal(client: DriverClient, seconds: float) -> bool:
    """Sleep *seconds* in the event loop; return False once interrupted.

    The polling counterpart to :func:`_wait_for_interrupt`, and for the same reason:
    a `time.sleep()` here would run in the worker thread, where neither the signal
    nor anyio's cancellation can reach it, so Ctrl+C would not be noticed until the
    sleep happened to end — and teardown would not run at all if the task group was
    already unwinding.

    Returns:
        True to keep polling, False if the session is being torn down.
    """
    try:
        client.portal.call(anyio.sleep, seconds)
        return True
    except (KeyboardInterrupt, SystemExit, GeneratorExit, RuntimeError):
        return False
    except BaseException as e:
        if _is_cancelled(e):
            return False
        raise


def _read_tunnel_state() -> dict | None:
    """Return the recorded tunnel, or None if it is not actually usable.

    Liveness is decided by **connecting to the port**, not by checking the
    process. A `j adb tunnel` orphaned by its parent shell keeps running and stays
    reparented to init, so ``os.kill(pid, 0)`` succeeds long after the lease that
    carried the tunnel is gone — and then every later ``j adb`` command reuses a
    port with nothing behind it and fails with "cannot connect to daemon at
    tcp:127.0.0.1:<port>". Observed on macOS with a tunnel orphaned hours earlier.

    The process check is kept as a cheap first filter, and a stale file is removed
    so the next invocation falls straight through to an ephemeral tunnel.

    Every field is validated before use. A malformed record must be *discarded*, not
    raised through: this runs at the start of ordinary `j adb` commands, so a
    ``[]``, a non-integer pid or a port outside 0-65535 would otherwise abort the
    command with a TypeError or OverflowError instead of falling back.
    """
    try:
        # Refuse a file we do not own, and never follow a symlink out of the
        # directory: both mean someone else chose the endpoint we are about to
        # connect to. O_NOFOLLOW fails on a symlinked path rather than opening it.
        fd = os.open(_TUNNEL_STATE_FILE, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None

    try:
        with os.fdopen(fd, "r") as f:
            if os.fstat(f.fileno()).st_uid != os.getuid():
                # Not ours to delete, either.
                return None
            state = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        _remove_tunnel_state()
        return None

    if not isinstance(state, dict):
        _remove_tunnel_state()
        return None

    host, pid, port = state.get("host"), state.get("pid"), state.get("port")
    # bool is an int subclass; a `true` pid is not a pid.
    if not isinstance(host, str) or not host or isinstance(pid, bool) or not isinstance(pid, int):
        _remove_tunnel_state()
        return None
    try:
        port = int(port)  # historically written as a string
    except (TypeError, ValueError):
        _remove_tunnel_state()
        return None
    if not 0 < port < 65536:
        _remove_tunnel_state()
        return None

    try:
        os.kill(pid, 0)
    except OSError:
        _remove_tunnel_state()
        return None

    # The authoritative check: is anything accepting connections there?
    try:
        with socket.create_connection((host, port), timeout=2):
            return state
    except OSError:
        _remove_tunnel_state()
        return None


def _write_tunnel_state(host: str, port: int) -> None:
    """Record this tunnel for other `j adb` invocations to reuse.

    Written 0600 inside a 0700 directory, since the endpoint here is one a later
    command will connect to — see `_TUNNEL_STATE_FILE`.
    """
    parent = os.path.dirname(_TUNNEL_STATE_FILE)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    fd = os.open(_TUNNEL_STATE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"host": host, "port": str(port), "pid": os.getpid()}, f)


def _remove_tunnel_state() -> None:
    """Remove the tunnel state file."""
    try:
        os.unlink(_TUNNEL_STATE_FILE)
    except FileNotFoundError:
        pass


class _AttachTarget(Protocol):
    """What `_AttachSet` needs of a client: list devices, and attach one.

    A protocol rather than `AdbClient` itself, because that is the whole dependency —
    which also lets the reconciliation logic be tested against a scripted stand-in
    instead of a live exporter.
    """

    logger: Any

    def devices(self) -> list[str]:
        """Serials of usable devices on the exporter, read live."""
        ...

    def attach(self, device: str, *, adb: str = ..., local_port: int = ...) -> AbstractContextManager[str]:
        """Attach *device*, yielding the ``host:port`` it landed on."""
        ...


class _AttachSet:
    """The set of devices `j adb attach` currently holds, reconcilable against ADB.

    Exists so attach can be re-run against a changing device list: `reconcile` brings
    the held set in line with what the exporter reports now, attaching what appeared
    and releasing what went away. Called once for a static bench, or on a timer for
    `--hotplug`.

    Each device gets its own ``ExitStack`` so it can be released independently;
    everything still held is closed when the set exits.
    """

    def __init__(self, client: _AttachTarget, targets: list[str], *, adb: str, local_port: int) -> None:
        """Track *targets*, or every usable device when *targets* is empty."""
        self._client = client
        # Empty => track whatever the exporter reports, rather than a fixed list.
        self._wanted = set(targets)
        self._adb = adb
        self._local_port = local_port
        self.attached: dict[str, ExitStack] = {}
        # Devices whose attach failed, so one that cannot work (adbd not on TCP, say)
        # is not retried every poll. Cleared when it disappears, so a re-plug retries.
        self._failed: set[str] = set()

    def __enter__(self) -> "_AttachSet":
        """Enter the set; nothing is attached until `reconcile` runs."""
        return self

    def __exit__(self, *exc_info) -> None:
        """Detach everything still held, so no device is left connected."""
        while self.attached:
            _, device_stack = self.attached.popitem()
            device_stack.close()

    def _present(self) -> list[str]:
        """Devices the exporter reports now; the current set if it cannot be asked."""
        try:
            return self._client.devices()
        except Exception as e:  # noqa: BLE001 - a failed poll must not end the session
            self._client.logger.debug("listing devices failed: %s", e)
            return list(self.attached)

    def reconcile(self, *, first_pass: bool) -> None:
        """Match the attached set to the exporter's current devices."""
        here = self._present()

        for device in list(self.attached):
            if device not in here:
                click.echo(f"{device} disconnected")
                self.attached.pop(device).close()

        # Forget failures for devices that are no longer here, so re-plugging one is a
        # genuine retry. Done for every remembered failure, not just attached devices:
        # a device that *failed* and then vanished never entered `attached`, so
        # clearing only those left it permanently blacklisted.
        self._failed -= {device for device in self._failed if device not in here}

        for device in here:
            if self._wanted and device not in self._wanted:
                continue
            if device in self.attached or device in self._failed:
                continue
            self._attach_one(device, first_pass=first_pass)

    def _attach_one(self, device: str, *, first_pass: bool) -> None:
        """Attach one device, recording a failure rather than raising.

        A device that cannot attach must not end the session or block the others, so
        the error is reported and the device remembered as failed.
        """
        # -P/local_port binds a single listener, so it applies to the first
        # attachment; the rest take an OS-assigned port.
        port = self._local_port if (self._local_port and not self.attached) else 0
        device_stack = ExitStack()
        try:
            target = device_stack.enter_context(self._client.attach(device, adb=self._adb, local_port=port))
        # SubprocessError, not CalledProcessError: it also covers TimeoutExpired, which
        # a local `adb` that stops responding raises. One unresponsive device must cost
        # only that device, not the whole session -- everything else stays attached.
        except (RuntimeError, subprocess.SubprocessError, OSError) as e:
            device_stack.close()
            self._failed.add(device)
            click.echo(f"error: could not attach {device}: {e}", err=True)
            return
        self.attached[device] = device_stack
        click.echo(f"{device} -> {target}" if first_pass else f"{device} attached -> {target}")


class AdbClient(DriverClient):
    """Client for tunneling ADB connections through Jumpstarter."""

    @contextmanager
    def forward_adb(self, host: str = "127.0.0.1", port: int = 0) -> Generator[tuple[str, int], None, None]:
        """Forward remote ADB server to a local TCP port.

        Args:
            host: Local bind address (default: 127.0.0.1)
            port: Local port (default: 0 = auto-assign, use 5037 to replace local ADB)

        Yields:
            Tuple of (host, port) of the local listener.
        """
        with TcpPortforwardAdapter(
            client=self,
            local_host=host,
            local_port=port,
        ) as addr:
            yield addr

    def start_server(self) -> int:
        """Start ADB server on the exporter."""
        return self.call("start_server")

    def kill_server(self) -> int:
        """Kill ADB server on the exporter."""
        return self.call("kill_server")

    def connect_device(self, device: str) -> str:
        """Connect to an ADB device by address (host:port)."""
        return self.call("connect_device", device)

    def disconnect_device(self, device: str) -> str:
        """Disconnect an ADB device by address (host:port)."""
        return self.call("disconnect_device", device)

    def list_devices(self) -> str:
        """List devices visible to the exporter's ADB server."""
        return self.call("list_devices")

    def devices(self) -> list[str]:
        """Return the serials of usable devices on the exporter.

        Read live on every call, so a device plugged in — or an emulator started —
        after the lease began is included. The exporter's ADB server is the
        inventory; this driver keeps no device list of its own.
        """
        serials = []
        for line in self.list_devices().splitlines():
            line = line.strip()
            if not line or line.startswith("*") or line.startswith("List of devices"):
                continue
            fields = line.split()
            # Only `device`; offline/unauthorized cannot be forwarded.
            if len(fields) >= 2 and fields[1] == "device":
                serials.append(fields[0])
        return serials

    # Exporter-side slot plumbing. Private: `attach()` is the interface, and
    # calling these directly means managing forwards and tunnels by hand.

    def _attach_device(self, device: str, adbd_port: int = 5555) -> str:
        """Publish a device's adbd on an exporter forward slot; return the slot name."""
        return self.call("attach_device", device, adbd_port)

    def _detach_device(self, device: str) -> None:
        """Release a device's forward slot on the exporter."""
        self.call("detach_device", device)

    def _list_attached(self) -> dict:
        """Return ``{slot_port: device}`` for devices attached on the exporter."""
        return self.call("list_attached")

    @contextmanager
    def attach(
        self,
        device: str,
        *,
        adbd_port: int = 5555,
        adb: str = "adb",
        local_port: int = 0,
    ) -> Generator[str, None, None]:
        """Add a remote device to the ADB server this machine already uses.

        Three steps, none of them clever: the exporter forwards the device's adbd
        onto a slot, Jumpstarter tunnels that slot here, and plain ``adb connect``
        adds it to the local server. Because ``adb connect`` is additive, the
        device lands in the *default* server — the one Android Studio, tradefed,
        gradle, and a bare ``adb`` all talk to — with no environment variables, no
        ``adb.server.port``, and no IDE restart.

        Args:
            device: ADB serial from :meth:`devices`.
            adbd_port: adbd's TCP port on the device.
            adb: path to the local adb binary.
            local_port: local port to bind; 0 lets the OS choose. The device's
            address is whatever this resolves to — deliberately not something this
            driver invents, since ADB owns device addressing.

        Yields:
            The ``host:port`` the device was attached as.
        """
        slot = self._attach_device(device, adbd_port)
        # From here the exporter holds a slot for us, so every failure path has to
        # release it. Without this, a tunnel that cannot bind or an `adb connect`
        # that fails leaks the slot until the exporter restarts, and a handful of
        # failed attaches exhausts the pool.
        try:
            with TcpPortforwardAdapter(client=self.children[slot], local_port=local_port) as addr:
                target = f"{addr[0]}:{addr[1]}"
                _adb_connect(adb, target)
                try:
                    yield target
                finally:
                    # Leave no stale `offline` entry in the developer's ADB server.
                    # Swallowing TimeoutExpired matters: raising here would skip the
                    # `_detach_device` below and leak the exporter's slot.
                    try:
                        subprocess.run(
                            [adb, "disconnect", target], check=False, capture_output=True, text=True, timeout=30
                        )
                    except (subprocess.SubprocessError, OSError) as e:
                        self.logger.debug("disconnect %s failed: %s", target, e)
        finally:
            try:
                self._detach_device(device)
            except Exception as e:  # noqa: BLE001 - teardown is best-effort
                self.logger.debug("detach %s failed: %s", device, e)

    def _cli_attach(
        self,
        targets: list[str],
        *,
        adb: str,
        local_port: int,
        hotplug: bool = False,
        poll_interval: float = 2.0,
    ) -> int:
        """Body of `j adb attach`. Attaches devices and holds them until Ctrl+C.

        With *hotplug* the exporter's device list is re-read every *poll_interval*
        seconds and the attachment set is reconciled against it, so a device connected
        mid-session is attached and one unplugged is dropped. Useful for a test run
        that reboots, re-flashes, or physically re-plugs a device.

        Off by default: most exporters have a fixed set of devices bolted to a bench,
        where polling only adds `adb devices` traffic and log noise for a list that
        never changes. Opt in with ``--hotplug`` when the hardware really does come
        and go.

        Args:
            targets: ADB serials to attach, or empty to track every usable device.
            adb: path to the local adb binary.
            local_port: local port for the first attachment; 0 lets the OS choose.
            hotplug: keep reconciling with the exporter's device list.
            poll_interval: seconds between polls when *hotplug* is set.

        Returns:
            A process exit status: 0 on a clean detach, 1 if nothing could be attached.
        """
        with _AttachSet(self, targets, adb=adb, local_port=local_port) as attachments:
            attachments.reconcile(first_pass=True)
            if not attachments.attached:
                click.echo("No usable devices on the exporter.", err=True)
                return 1

            click.echo("\nAttached to your local ADB server; Android Studio will list them.")
            if hotplug:
                click.echo(f"Watching for device changes every {poll_interval:g}s. Press Ctrl+C to detach.")
                while _sleep_through_portal(self, poll_interval):
                    attachments.reconcile(first_pass=False)
            else:
                click.echo("Press Ctrl+C to detach.")
                _wait_for_interrupt(self)

        click.echo("detached")
        return 0

    def cli(self):
        """Build the `j adb` command group."""

        @click.command(context_settings={"ignore_unknown_options": True})
        @click.option(
            "-H",
            "host",
            default="127.0.0.1",
            show_default=True,
            help="Local address to tunnel ADB to",
        )
        @click.option(
            "-P",
            "port",
            type=int,
            default=0,
            show_default=True,
            help="Local port to tunnel ADB to (0=auto)",
        )
        @click.option(
            "--adb",
            default="adb",
            show_default=True,
            help="Path to local adb executable",
        )
        @click.option(
            "--hotplug",
            is_flag=True,
            default=False,
            help="attach: keep tracking devices connected or removed while running "
            "(off by default; most exporters have a fixed set of devices)",
        )
        @click.option(
            "--poll-interval",
            # Zero or negative makes anyio.sleep return at once, turning the
            # hotplug loop into an unthrottled poll of the exporter's ADB server.
            type=click.FloatRange(min=0, min_open=True),
            default=2.0,
            show_default=True,
            help="attach: seconds between device checks, with --hotplug",
        )
        @click.argument("args", nargs=-1)
        def adb(host: str, port: int, adb: str, hotplug: bool, poll_interval: float, args: tuple[str, ...]):
            """ADB tunneling and device access.

            Wraps the local adb binary to work against a remote ADB server
            tunneled through Jumpstarter. The exporter's ADB server is
            automatically tunneled to a local port, and environment variables
            ANDROID_ADB_SERVER_ADDRESS and ANDROID_ADB_SERVER_PORT are set so
            the local adb binary communicates through the tunnel.

            All standard adb commands (shell, install, push, pull, logcat,
            start-server, kill-server, connect, disconnect, etc.) are passed
            through directly to the remote ADB server.

            If a persistent tunnel is already running (from a previous
            `j adb tunnel`), commands will reuse it instead of creating
            a new ephemeral tunnel.

            \b
            Jumpstarter-specific commands:
              attach    Add the exporter's devices to the ADB server this machine
                        already uses, via plain `adb connect`. Works when you do
                        NOT own that server -- Android Studio keeps 5037 and
                        respawns it in ~3s if killed, so it cannot be taken over.
                        No local ADB server is needed either: adb starts one if
                        there is none. Devices appear in Studio's chooser with no
                        configuration. Defaults to every usable device; name
                        serials to pick. Blocks until Ctrl+C, then detaches
                        cleanly. Pass --hotplug to keep following devices that
                        come and go while it runs.
              tunnel    Create a persistent ADB tunnel to a local port
                        (auto-assigned by default, use -P to pick a specific
                        port). Other j adb commands will automatically reuse
                        the tunnel. For native adb or external tools, export
                        the env vars printed by the command.

            \b
            Unsupported commands:
              nodaemon  Not supported (would start a local server, ignoring
                        the tunnel).
            """
            if not args or (len(args) == 1 and args[0] == "help"):
                click.echo(click.get_current_context().get_help())
                click.echo("\n" + "=" * 60)
                click.echo("ADB built-in help (from local adb binary):")
                click.echo("=" * 60 + "\n")
                subprocess.run([adb, "help"], stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
                return 0

            _validate_adb_args(args)

            if args[0] == "attach":
                serials = [a for a in args[1:] if not a.startswith("-")]
                return self._cli_attach(
                    serials,
                    adb=adb,
                    local_port=port,
                    hotplug=hotplug,
                    poll_interval=poll_interval,
                )

            if args[0] == "tunnel":
                state = _read_tunnel_state()
                if state:
                    # If a specific port was requested, check it matches the running tunnel
                    if port != 0 and (state["host"] != host or int(state["port"]) != port):
                        click.echo(
                            f"Error: tunnel already running (PID {state['pid']}) "
                            f"on {state['host']}:{state['port']}, "
                            f"cannot bind to {host}:{port}",
                            err=True,
                        )
                        return 1
                    click.echo(f"Tunnel already running (PID {state['pid']}) on {state['host']}:{state['port']}")
                    return 0

                with self.forward_adb(host, port) as addr:
                    _write_tunnel_state(addr[0], addr[1])
                    try:
                        click.echo(f"ADB server tunneled to {addr[0]}:{addr[1]}")
                        click.echo("")
                        click.echo("To use native adb or other tools, run:")
                        click.echo(f"  export ANDROID_ADB_SERVER_ADDRESS={addr[0]}")
                        click.echo(f"  export ANDROID_ADB_SERVER_PORT={addr[1]}")
                        click.echo("")
                        click.echo("Press Ctrl+C to stop")
                        _wait_for_interrupt(self)
                    finally:
                        _remove_tunnel_state()
                return 0

            # Check if a persistent tunnel is already running
            state = _read_tunnel_state()
            if state:
                env = os.environ | {
                    "ANDROID_ADB_SERVER_ADDRESS": state["host"],
                    "ANDROID_ADB_SERVER_PORT": state["port"],
                }
                process = subprocess.Popen(
                    [adb, *args],
                    stdin=sys.stdin,
                    stdout=sys.stdout,
                    stderr=sys.stderr,
                    env=env,
                )
                return process.wait()

            # No persistent tunnel - create an ephemeral one
            with self.forward_adb(host, port) as addr:
                env = os.environ | {
                    "ANDROID_ADB_SERVER_ADDRESS": addr[0],
                    "ANDROID_ADB_SERVER_PORT": str(addr[1]),
                }
                process = subprocess.Popen(
                    [adb, *args],
                    stdin=sys.stdin,
                    stdout=sys.stdout,
                    stderr=sys.stderr,
                    env=env,
                )
                return process.wait()

        return adb
