import asyncio
import json
import os
import socket
import subprocess
import tempfile
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from .client import (
    AdbClient,
    _adb_connect,
    _AttachSet,
    _read_tunnel_state,
    _remove_tunnel_state,
    _sleep_through_portal,
    _wait_for_interrupt,
    _write_tunnel_state,
)


def _listener():
    """A real bound listener, so 'is the tunnel up?' is answered by connecting."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock, sock.getsockname()[1]


def _state_file(tmp_path):
    return patch("jumpstarter_driver_adb.client._TUNNEL_STATE_FILE", str(tmp_path / "tunnel.json"))


def test_live_tunnel_is_reused(tmp_path):
    sock, port = _listener()
    try:
        with _state_file(tmp_path):
            _write_tunnel_state("127.0.0.1", port)
            state = _read_tunnel_state()
        assert state is not None
        assert int(state["port"]) == port
    finally:
        sock.close()


def test_orphaned_tunnel_is_not_reused(tmp_path):
    """The bug this guards: a live PID does NOT mean a live tunnel.

    `j adb tunnel` orphaned by its parent shell keeps running, reparented to init,
    so os.kill(pid, 0) succeeds indefinitely. But the lease carrying the tunnel is
    gone and nothing listens on the port, so reusing it made every later `j adb`
    command fail with "cannot connect to daemon at tcp:127.0.0.1:<port>".
    Reproduced on macOS with a tunnel orphaned hours earlier.
    """
    sock, port = _listener()
    sock.close()  # port recorded, nothing listening -- exactly the orphan case

    with _state_file(tmp_path) as _:
        # os.getpid() is alive by construction, so the process check cannot help.
        _write_tunnel_state("127.0.0.1", port)
        assert _read_tunnel_state() is None


def test_stale_state_file_is_removed(tmp_path):
    """Otherwise the dead entry is re-examined on every single command."""
    sock, port = _listener()
    sock.close()

    path = tmp_path / "tunnel.json"
    with _state_file(tmp_path):
        _write_tunnel_state("127.0.0.1", port)
        assert path.exists()
        assert _read_tunnel_state() is None
        assert not path.exists()


def test_dead_process_is_not_reused(tmp_path):
    path = tmp_path / "tunnel.json"
    with _state_file(tmp_path):
        # PID 2**22 is above any Linux/macOS pid_max, so it cannot exist.
        path.write_text(json.dumps({"host": "127.0.0.1", "port": "5037", "pid": 2**22}))
        assert _read_tunnel_state() is None
        assert not path.exists()


def test_malformed_state_file_is_discarded(tmp_path):
    path = tmp_path / "tunnel.json"
    with _state_file(tmp_path):
        path.write_text("{not json")
        assert _read_tunnel_state() is None

        path.write_text(json.dumps({"host": "127.0.0.1"}))  # no port/pid
        assert _read_tunnel_state() is None

        path.write_text(json.dumps({"host": "127.0.0.1", "port": "nope", "pid": os.getpid()}))
        assert _read_tunnel_state() is None


def test_missing_state_file_is_not_an_error(tmp_path):
    with _state_file(tmp_path):
        assert _read_tunnel_state() is None
        _remove_tunnel_state()  # must not raise


def test_hostile_state_records_are_discarded(tmp_path):
    """The file names an endpoint we then connect to, so it is not trusted input.

    A list root used to raise TypeError at state["pid"], and a port outside
    0-65535 raised OverflowError inside socket.create_connection -- both aborting
    an ordinary `j adb` command instead of falling back to a fresh tunnel.
    """
    path = tmp_path / "tunnel.json"
    with _state_file(tmp_path):
        for hostile in (
            [],  # list root: used to raise TypeError
            "just a string",
            {"host": "127.0.0.1", "port": 99999, "pid": os.getpid()},  # used to OverflowError
            {"host": "127.0.0.1", "port": -1, "pid": os.getpid()},
            {"host": "127.0.0.1", "port": 0, "pid": os.getpid()},
            {"host": "", "port": 5037, "pid": os.getpid()},
            {"host": "127.0.0.1", "port": 5037, "pid": True},  # bool is an int subclass
            {"host": "127.0.0.1", "port": 5037, "pid": "1234"},
            {"host": ["127.0.0.1"], "port": 5037, "pid": os.getpid()},
        ):
            path.write_text(json.dumps(hostile))
            assert _read_tunnel_state() is None, f"accepted {hostile!r}"


def test_state_file_is_private_to_this_user(tmp_path):
    """It records an endpoint a later command connects to, so it is 0600 in a 0700 dir."""
    state_dir = tmp_path / "state"  # deliberately absent, so the mode is ours to set
    with patch("jumpstarter_driver_adb.client._TUNNEL_STATE_FILE", str(state_dir / "tunnel.json")):
        _write_tunnel_state("127.0.0.1", 5037)
        assert state_dir.stat().st_mode & 0o777 == 0o700
        assert (state_dir / "tunnel.json").stat().st_mode & 0o777 == 0o600


def test_a_symlinked_state_file_is_not_followed(tmp_path):
    """Following it would let someone else pick the endpoint we connect to."""
    elsewhere = tmp_path / "attacker.json"
    elsewhere.write_text(json.dumps({"host": "127.0.0.1", "port": "5037", "pid": os.getpid()}))
    link = tmp_path / "tunnel.json"
    link.symlink_to(elsewhere)

    with patch("jumpstarter_driver_adb.client._TUNNEL_STATE_FILE", str(link)):
        assert _read_tunnel_state() is None


def test_default_state_path_is_not_world_writable():
    """Regression: this used to live in the shared temp directory."""
    from .client import _TUNNEL_STATE_FILE

    assert "/tmp/" not in _TUNNEL_STATE_FILE
    assert not _TUNNEL_STATE_FILE.startswith(tempfile.gettempdir() + os.sep)


# ------------------------------------------------------------- `adb connect`
#
# `adb connect` exits 0 even when it fails, printing the reason to STDOUT. Verified
# against adb 1.0.41: a refused port, an unresolvable host and an out-of-range port
# all return 0. So the exit status cannot be used to tell whether a device attached.


def _completed(stdout, returncode=0):
    return subprocess.CompletedProcess(args=["adb", "connect", "x"], returncode=returncode, stdout=stdout, stderr="")


@pytest.mark.parametrize(
    "output",
    [
        "failed to connect to '127.0.0.1:59999': Connection refused",
        "failed to connect to 127.0.0.1:15055",
        "failed to resolve host: 'nope.invalid': nodename nor servname provided",
        "bad port number '99999' in '127.0.0.1:99999'",
        "cannot connect to daemon at tcp:127.0.0.1:5037: Connection refused",
        "",
    ],
)
def test_a_failed_connect_is_detected_despite_exit_zero(output):
    """This is the whole point: rc=0 with a failure message on stdout."""
    with patch("subprocess.run", return_value=_completed(output, returncode=0)):
        with pytest.raises(RuntimeError, match="did not connect"):
            _adb_connect("adb", "127.0.0.1:59999")


@pytest.mark.parametrize(
    "output",
    ["connected to 127.0.0.1:16000", "already connected to 127.0.0.1:16000"],
)
def test_a_successful_connect_is_accepted(output):
    """adb's only two success strings: `connected to %s`, `already connected to %s`."""
    with patch("subprocess.run", return_value=_completed(output)):
        assert _adb_connect("adb", "127.0.0.1:16000") == output


def test_a_hung_connect_raises_rather_than_blocking():
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("adb connect", 60)):
        with pytest.raises(RuntimeError, match="failed"):
            _adb_connect("adb", "127.0.0.1:16000")


# ------------------------------------------------------------------- hotplug
#
# `attach` used to resolve the device list once and then block, so a device plugged
# in mid-session was never attached and an unplugged one left a dead entry behind.
# `_AttachSet.reconcile` matches the held set against what the exporter reports now.


class _FakeClient:
    """An AdbClient stand-in whose device list and attach outcomes are scriptable."""

    def __init__(self, devices, failing=(), on_attach=None):
        self._devices = list(devices)
        self._failing = set(failing)
        # Raised instead of the default behaviour, to script a specific failure.
        self._on_attach = on_attach
        # Set to an exception to make the next device listing fail.
        self.fail_listing: Exception | None = None
        self.logger = MagicMock()
        self.attached = []  # every device attach() was called for
        self.detached = []  # every device whose context was exited
        self.ports = []  # the local_port asked for on each attach

    def set_devices(self, devices):
        """Change what the exporter reports, as a plug or unplug would."""
        self._devices = list(devices)

    def devices(self):
        """Serials the exporter reports right now."""
        if self.fail_listing is not None:
            raise self.fail_listing
        return list(self._devices)

    @contextmanager
    def attach(self, device, *, adb="adb", local_port=0):
        """Attach *device*, honouring any scripted failure for it."""
        self.ports.append(local_port)
        if self._on_attach is not None:
            self._on_attach(device)
        if device in self._failing:
            raise RuntimeError(f"no adbd on tcp for {device}")
        self.attached.append(device)
        try:
            yield f"127.0.0.1:{16000 + len(self.attached)}"
        finally:
            self.detached.append(device)


def test_a_device_plugged_in_later_is_attached():
    """The gap: attach resolved devices once, so hotplug never worked."""
    client = _FakeClient(["tablet"])
    with _AttachSet(client, [], adb="adb", local_port=0) as attachments:
        attachments.reconcile(first_pass=True)
        assert sorted(attachments.attached) == ["tablet"]

        client.set_devices(["tablet", "headunit"])  # someone plugs in a second device
        attachments.reconcile(first_pass=False)
        assert sorted(attachments.attached) == ["headunit", "tablet"]


def test_an_unplugged_device_is_released():
    """Otherwise its slot and its local `adb connect` entry linger."""
    client = _FakeClient(["tablet", "headunit"])
    with _AttachSet(client, [], adb="adb", local_port=0) as attachments:
        attachments.reconcile(first_pass=True)

        client.set_devices(["tablet"])  # headunit unplugged
        attachments.reconcile(first_pass=False)
        assert sorted(attachments.attached) == ["tablet"]
        assert client.detached == ["headunit"]


def test_named_serials_ignore_other_devices():
    """`j adb attach tablet` must not grab a colleague's device that appears later."""
    client = _FakeClient(["tablet"])
    with _AttachSet(client, ["tablet"], adb="adb", local_port=0) as attachments:
        attachments.reconcile(first_pass=True)
        client.set_devices(["tablet", "someone-elses-phone"])
        attachments.reconcile(first_pass=False)
        assert sorted(attachments.attached) == ["tablet"]


def test_an_already_attached_device_is_not_reattached():
    """Reconciling repeatedly must be a no-op, not a stream of duplicate attaches."""
    client = _FakeClient(["tablet"])
    with _AttachSet(client, [], adb="adb", local_port=0) as attachments:
        for _ in range(5):
            attachments.reconcile(first_pass=False)
    assert client.attached == ["tablet"]


def test_a_device_that_cannot_attach_is_not_retried_every_poll():
    """A device with no adbd on TCP would otherwise spam errors on every tick."""
    client = _FakeClient(["tablet", "broken"], failing=["broken"])
    with _AttachSet(client, [], adb="adb", local_port=0) as attachments:
        for _ in range(4):
            attachments.reconcile(first_pass=False)
        assert sorted(attachments.attached) == ["tablet"]
    assert client.attached == ["tablet"]


def test_replugging_retries_a_previously_failed_device():
    """Forgetting the failure on disappearance is what makes a re-plug a real retry."""
    client = _FakeClient(["broken"], failing=["broken"])
    with _AttachSet(client, [], adb="adb", local_port=0) as attachments:
        attachments.reconcile(first_pass=True)
        assert attachments.attached == {}

        client.set_devices([])  # unplugged
        attachments.reconcile(first_pass=False)
        client._failing.clear()  # replugged, now with adbd on TCP
        client.set_devices(["broken"])
        attachments.reconcile(first_pass=False)
        assert sorted(attachments.attached) == ["broken"]


def test_a_failed_poll_keeps_the_session_alive():
    """A dropped `adb devices` must not detach working devices or kill the command."""
    client = _FakeClient(["tablet"])
    with _AttachSet(client, [], adb="adb", local_port=0) as attachments:
        attachments.reconcile(first_pass=True)
        client.fail_listing = RuntimeError("exporter busy")
        attachments.reconcile(first_pass=False)  # must not raise
        assert sorted(attachments.attached) == ["tablet"]
        assert client.detached == []


def test_everything_is_detached_on_exit():
    client = _FakeClient(["tablet", "headunit"])
    with _AttachSet(client, [], adb="adb", local_port=0) as attachments:
        attachments.reconcile(first_pass=True)
    assert sorted(client.detached) == ["headunit", "tablet"]


def test_an_explicit_local_port_is_used_once():
    """-P binds a single listener, so only the first device can honour it."""
    client = _FakeClient(["a", "b", "c"])
    with _AttachSet(client, [], adb="adb", local_port=5555) as attachments:
        attachments.reconcile(first_pass=True)
    assert client.ports == [5555, 0, 0]


def test_an_unresponsive_adb_costs_only_that_device():
    """A local `adb` that hangs raises TimeoutExpired, not CalledProcessError.

    Catching only CalledProcessError let it escape `_attach_one` and tear down the
    whole session, taking every working device with it.
    """

    def wedge(device):
        if device == "wedged":
            raise subprocess.TimeoutExpired("adb connect", 60)

    client = _FakeClient(["tablet", "wedged"], on_attach=wedge)
    with _AttachSet(client, [], adb="adb", local_port=0) as attachments:
        attachments.reconcile(first_pass=True)  # must not raise
        assert sorted(attachments.attached) == ["tablet"]


# ------------------------------------------------------- parsing `adb devices`
#
# The exporter's ADB server is the device inventory; this driver keeps no list of
# its own. So the parse has to be right, including the states that cannot be
# forwarded.


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("List of devices attached\nHVA1234567\tdevice\n", ["HVA1234567"]),
        # offline/unauthorized devices have no working adbd to forward.
        ("List of devices attached\nHVA1\tdevice\nHVA2\toffline\nHVA3\tunauthorized\n", ["HVA1"]),
        ("List of devices attached\n", []),
        ("", []),
        # `* daemon started successfully` and friends must not be read as serials.
        ("* daemon not running; starting now at tcp:15037\n* daemon started successfully\n", []),
        ("List of devices attached\nemulator-5554\tdevice\n", ["emulator-5554"]),
        # `devices -l` appends properties; only the serial and state matter.
        ("List of devices attached\nHVA1\tdevice product:x model:y device:z\n", ["HVA1"]),
        ("List of devices attached\n10.0.0.2:5555\tdevice\n", ["10.0.0.2:5555"]),
    ],
)
def test_only_forwardable_devices_are_listed(output, expected):
    client = AdbClient.__new__(AdbClient)
    with patch.object(AdbClient, "list_devices", return_value=output):
        assert client.devices() == expected


# --------------------------------------------------------------- `attach` body
#
# `_cli_attach` is what `j adb attach` runs. Driven here with a scripted client so
# the exit statuses and the wait/poll choice are covered without an exporter.


class _CliClient(_FakeClient):
    """A fake that also stands in for the client passed to the portal helpers."""

    def __init__(self, devices, failing=(), interrupts_after=0):
        super().__init__(devices, failing=failing)
        # How many poll ticks to allow before reporting an interrupt.
        self.interrupts_after = interrupts_after
        self.polls = 0
        self.waited = False


def _run_cli_attach(client, targets=(), **kwargs):
    """Invoke `_cli_attach` with the portal waits stubbed out."""

    def sleep(_client, _seconds):
        client.polls += 1
        return client.polls <= client.interrupts_after

    def wait(_client):
        client.waited = True

    with (
        patch("jumpstarter_driver_adb.client._sleep_through_portal", side_effect=sleep),
        patch("jumpstarter_driver_adb.client._wait_for_interrupt", side_effect=wait),
    ):
        return AdbClient._cli_attach(client, list(targets), adb="adb", local_port=0, **kwargs)


def test_attach_reports_failure_when_nothing_is_attachable():
    """Exit 1, so a script does not carry on believing it has a device."""
    client = _CliClient([])
    assert _run_cli_attach(client) == 1
    assert client.attached == []


def test_attach_returns_zero_after_a_clean_detach():
    client = _CliClient(["tablet"])
    assert _run_cli_attach(client) == 0
    assert client.attached == ["tablet"]
    assert client.detached == ["tablet"]  # released, not left connected
    assert client.waited is True  # blocked for Ctrl+C rather than polling


def test_attach_does_not_poll_unless_hotplug_is_asked_for():
    """Default is a static bench; polling a fixed list is only noise."""
    client = _CliClient(["tablet"])
    assert _run_cli_attach(client) == 0
    assert client.polls == 0


def test_hotplug_polls_and_picks_up_a_new_device():
    client = _CliClient(["tablet"], interrupts_after=3)
    with patch.object(_CliClient, "devices", autospec=True) as devices:
        # Third poll is when the head unit appears.
        devices.side_effect = [["tablet"], ["tablet"], ["tablet", "headunit"], ["tablet", "headunit"]]
        assert _run_cli_attach(client, hotplug=True, poll_interval=0.01) == 0
    assert sorted(client.attached) == ["headunit", "tablet"]


def test_attach_only_the_named_serial():
    client = _CliClient(["tablet", "headunit"])
    assert _run_cli_attach(client, targets=["tablet"]) == 0
    assert client.attached == ["tablet"]


def test_attach_fails_when_the_named_serial_is_absent():
    client = _CliClient(["headunit"])
    assert _run_cli_attach(client, targets=["not-plugged-in"]) == 1


# ------------------------------------------------- waiting inside the event loop
#
# Both helpers must return rather than propagate, or Ctrl+C leaves a stale
# `adb connect` entry behind and a second Ctrl+C hangs in threading._shutdown.


class _Portal:
    def __init__(self, raises):
        self._raises = raises

    def call(self, *args, **kwargs):
        raise self._raises


@pytest.mark.parametrize(
    "exc",
    [KeyboardInterrupt(), SystemExit(), GeneratorExit(), RuntimeError("portal is closed")],
)
def test_an_interrupt_ends_the_wait_without_propagating(exc):
    client = MagicMock(portal=_Portal(exc))
    _wait_for_interrupt(client)  # must return, so teardown can run
    assert _sleep_through_portal(client, 1) is False


def test_anyio_cancellation_ends_the_wait():
    """Cancellation is a BaseException, not an Exception, so it needs its own arm.

    Deliberately synchronous and with no event loop: these waits run in a worker
    thread, and `get_cancelled_exc_class()` in the except arm used to raise
    NoEventLoopError there, masking the cancellation it was meant to detect.
    """
    client = MagicMock(portal=_Portal(asyncio.CancelledError()))
    _wait_for_interrupt(client)
    assert _sleep_through_portal(client, 1) is False


def test_an_unexpected_error_is_not_swallowed():
    """A real bug must surface, not look like a clean Ctrl+C."""
    client = MagicMock(portal=_Portal(ValueError("something else")))
    with pytest.raises(ValueError):
        _wait_for_interrupt(client)
    with pytest.raises(ValueError):
        _sleep_through_portal(client, 1)


def test_a_completed_sleep_keeps_polling():
    client = MagicMock()
    client.portal.call.return_value = None
    assert _sleep_through_portal(client, 0.01) is True


def test_poll_interval_must_be_positive():
    """Zero or negative turns the hotplug loop into an unthrottled poll.

    anyio.sleep(0) returns at once, so the loop would hammer the exporter's ADB
    server and the gRPC link for the whole session.
    """
    from click.testing import CliRunner

    client = _CliClient(["tablet"])
    runner = CliRunner()

    for bad in ("0", "-1"):
        result = runner.invoke(AdbClient.cli(client), ["--hotplug", "--poll-interval", bad, "attach"])
        assert result.exit_code != 0
        assert "poll-interval" in result.output
