import base64
import inspect
import json
import logging
import math
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import anyio
import click
import pytest
from jumpstarter_cli_common.exceptions import handle_exceptions_with_reauthentication

from jumpstarter_cli.shell import (
    _attempt_token_recovery,
    _cancel_if_connection_lost,
    _monitor_token_expiry,
    _resolve_lease_from_active_async,
    _run_shell_with_lease_async,
    _shell_with_signal_handling,
    _try_refresh_token,
    _try_reload_token_from_disk,
    _update_lease_channel,
    _warn_refresh_failed,
    shell,
)

from jumpstarter.client.grpc import Lease, LeaseList
from jumpstarter.common import ExporterStatus
from jumpstarter.common.exceptions import ExporterOfflineError, ExporterUnreachableError
from jumpstarter.config.client import ClientConfigV1Alpha1
from jumpstarter.config.env import JMP_LEASE

pytestmark = pytest.mark.anyio


def _make_lease(name: str, client: str = "test-client") -> Lease:
    return Lease(
        namespace="default",
        name=name,
        selector="",
        exporter_name=None,
        duration=timedelta(minutes=30),
        effective_duration=None,
        begin_time=datetime.now(),
        client=client,
        exporter="test-exporter",
        conditions=[],
        effective_begin_time=None,
        effective_end_time=None,
    )


def _make_lease_list(names: list[str]) -> LeaseList:
    return LeaseList(
        leases=[_make_lease(n) for n in names],
        next_page_token=None,
    )


class _DummyConfig:
    def __init__(self):
        self.captured = None
        self.token = None

    @asynccontextmanager
    async def lease_async(
        self, selector, exporter_name, lease_name, duration, portal,
        acquisition_timeout, retry_timeout=None, dial_timeout=None, **kwargs,
    ):
        self.captured = (selector, exporter_name, lease_name, duration, acquisition_timeout)
        m = Mock()
        m.retry_timeout = 0.0
        yield m


def test_shell_passes_exporter_name_to_lease_async():
    config = _DummyConfig()

    with patch(
        "jumpstarter_cli.shell._run_shell_with_lease_async",
        new=AsyncMock(return_value=0),
    ):
        exit_code = anyio.run(
            _shell_with_signal_handling,
            config,
            None,
            "laptop-test-exporter",
            None,
            timedelta(minutes=1),
            False,
            (),
            None,
        )

    assert exit_code == 0
    assert config.captured is not None
    assert config.captured[1] == "laptop-test-exporter"


async def test_shell_warns_when_expired_token_prevents_cleanup_on_normal_exit():
    lease = Mock()
    lease.release = True
    lease.name = "expired-lease"
    lease.lease_ended = False
    lease.lease_transferred = False
    lease.retry_timeout = 0.0

    config = _DummyConfig()

    @asynccontextmanager
    async def lease_async(
        selector, exporter_name, lease_name, duration, portal,
        acquisition_timeout, retry_timeout=None, dial_timeout=None, **kwargs,
    ):
        yield lease

    config.lease_async = lease_async

    async def fake_monitor(_config, _lease, _cancel_scope, token_state=None):
        if token_state is not None:
            token_state["expired_unrecovered"] = True

    async def fake_run_shell(*_args):
        await anyio.sleep(0)
        return 0

    with (
        patch("jumpstarter_cli.shell._monitor_token_expiry", side_effect=fake_monitor),
        patch("jumpstarter_cli.shell._run_shell_with_lease_async", side_effect=fake_run_shell),
        patch("jumpstarter_cli.shell._warn_about_expired_token") as mock_warn,
    ):
        exit_code = await _shell_with_signal_handling(
            config,
            None,
            None,
            None,
            timedelta(minutes=1),
            False,
            (),
            None,
        )

    assert exit_code == 0
    mock_warn.assert_called_once_with("expired-lease", None)


def test_shell_requires_selector_or_name_when_no_leases():
    config = Mock(spec=ClientConfigV1Alpha1)
    config.metadata = type("Metadata", (), {"name": "test-client"})()
    config.list_leases = AsyncMock(return_value=_make_lease_list([]))
    with pytest.raises(click.UsageError, match="no active leases found"):
        shell.callback.__wrapped__.__wrapped__(
            config=config,
            command=(),
            lease_name=None,
            selector=None,
            exporter_name=None,
            duration=timedelta(minutes=1),
            exporter_logs=False,
            allow_disabled=False,
            acquisition_timeout=None,
            retry_timeout=None,
            dial_timeout=None,
            tls_grpc_address=None,
            tls_grpc_insecure=False,
            passphrase=None,
        )


def test_shell_allows_existing_lease_name_without_selector_or_name():
    with (
        patch("jumpstarter_cli.shell.anyio.run", return_value=0),
        patch("jumpstarter_cli.shell.sys.exit") as mock_exit,
    ):
        inspect.unwrap(shell.callback)(
            config=Mock(spec=ClientConfigV1Alpha1),
            command=(),
            lease_name="existing-lease",
            selector=None,
            exporter_name=None,
            duration=timedelta(minutes=1),
            exporter_logs=False,
            allow_disabled=False,
            acquisition_timeout=None,
            retry_timeout=None,
            dial_timeout=None,
            tls_grpc_address=None,
            tls_grpc_insecure=False,
            passphrase=None,
        )

    mock_exit.assert_called_once_with(0)


def test_shell_auto_connects_single_lease():
    config = Mock(spec=ClientConfigV1Alpha1)
    config.metadata = type("Metadata", (), {"name": "test-client"})()
    with (
        patch("jumpstarter_cli.shell.anyio.run", side_effect=["my-only-lease", 0]) as mock_run,
        patch("jumpstarter_cli.shell.sys.exit") as mock_exit,
    ):
        shell.callback.__wrapped__.__wrapped__(
            config=config,
            command=(),
            lease_name=None,
            selector=None,
            exporter_name=None,
            duration=timedelta(minutes=1),
            exporter_logs=False,
            allow_disabled=False,
            acquisition_timeout=None,
            retry_timeout=None,
            dial_timeout=None,
            tls_grpc_address=None,
            tls_grpc_insecure=False,
            passphrase=None,
        )

    resolve_call_args = mock_run.call_args_list[0]
    assert resolve_call_args[0][0] is _resolve_lease_from_active_async
    assert resolve_call_args[0][1] is config
    shell_call_args = mock_run.call_args_list[1]
    assert shell_call_args[0][4] == "my-only-lease"
    mock_exit.assert_called_once_with(0)


def test_shell_no_leases_shows_guidance():
    config = Mock(spec=ClientConfigV1Alpha1)
    config.metadata = type("Metadata", (), {"name": "test-client"})()
    config.list_leases = AsyncMock(return_value=_make_lease_list([]))
    with pytest.raises(click.UsageError, match="no active leases found"):
        shell.callback.__wrapped__.__wrapped__(
            config=config,
            command=(),
            lease_name=None,
            selector=None,
            exporter_name=None,
            duration=timedelta(minutes=1),
            exporter_logs=False,
            allow_disabled=False,
            acquisition_timeout=None,
            retry_timeout=None,
            dial_timeout=None,
            tls_grpc_address=None,
            tls_grpc_insecure=False,
            passphrase=None,
        )
    config.list_leases.assert_called_once_with(only_active=True)


def test_shell_multi_lease_tty_picker():
    config = Mock(spec=ClientConfigV1Alpha1)
    config.metadata = type("Metadata", (), {"name": "test-client"})()
    config.list_leases = AsyncMock(return_value=_make_lease_list(["lease-a", "lease-b", "lease-c"]))
    with (
        patch("jumpstarter_cli.shell.sys.stdin") as mock_stdin,
        patch("jumpstarter_cli.shell.click.prompt", return_value=2),
    ):
        mock_stdin.isatty.return_value = True
        selected = anyio.run(_resolve_lease_from_active_async, config)

    assert selected == "lease-b"
    config.list_leases.assert_called_once_with(only_active=True)


def test_shell_multi_lease_no_tty_error():
    config = Mock(spec=ClientConfigV1Alpha1)
    config.metadata = type("Metadata", (), {"name": "test-client"})()
    config.list_leases = AsyncMock(return_value=_make_lease_list(["lease-a", "lease-b"]))
    with (
        patch("jumpstarter_cli.shell.sys.stdin") as mock_stdin,
        pytest.raises(click.UsageError, match="lease-a"),
    ):
        mock_stdin.isatty.return_value = False
        shell.callback.__wrapped__.__wrapped__(
            config=config,
            command=(),
            lease_name=None,
            selector=None,
            exporter_name=None,
            duration=timedelta(minutes=1),
            exporter_logs=False,
            allow_disabled=False,
            acquisition_timeout=None,
            retry_timeout=None,
            dial_timeout=None,
            tls_grpc_address=None,
            tls_grpc_insecure=False,
            passphrase=None,
        )


def test_shell_filters_leases_by_current_client():
    other_user_lease = _make_lease("other-user-lease", client="other-client")
    my_lease = _make_lease("my-lease", client="test-client")
    lease_list = LeaseList(leases=[other_user_lease, my_lease], next_page_token=None)
    config = Mock(spec=ClientConfigV1Alpha1)
    config.metadata = type("Metadata", (), {"name": "test-client"})()
    config.list_leases = AsyncMock(return_value=lease_list)

    selected = anyio.run(_resolve_lease_from_active_async, config)
    assert selected == "my-lease"
    config.list_leases.assert_called_once_with(only_active=True)


def test_shell_no_own_leases_among_others():
    other_lease = _make_lease("other-lease", client="other-client")
    lease_list = LeaseList(leases=[other_lease], next_page_token=None)
    config = Mock(spec=ClientConfigV1Alpha1)
    config.metadata = type("Metadata", (), {"name": "test-client"})()
    config.list_leases = AsyncMock(return_value=lease_list)
    with pytest.raises(click.UsageError, match="no active leases found"):
        shell.callback.__wrapped__.__wrapped__(
            config=config,
            command=(),
            lease_name=None,
            selector=None,
            exporter_name=None,
            duration=timedelta(minutes=1),
            exporter_logs=False,
            allow_disabled=False,
            acquisition_timeout=None,
            retry_timeout=None,
            dial_timeout=None,
            tls_grpc_address=None,
            tls_grpc_insecure=False,
            passphrase=None,
        )


def test_shell_allows_env_lease_without_selector_or_name():
    with (
        patch("jumpstarter_cli.shell.anyio.run", return_value=0),
        patch("jumpstarter_cli.shell.sys.exit") as mock_exit,
        patch.dict("os.environ", {JMP_LEASE: "existing-lease"}, clear=False),
    ):
        inspect.unwrap(shell.callback)(
            config=Mock(spec=ClientConfigV1Alpha1),
            command=(),
            lease_name=None,
            selector=None,
            exporter_name=None,
            duration=timedelta(minutes=1),
            exporter_logs=False,
            allow_disabled=False,
            acquisition_timeout=None,
            retry_timeout=None,
            dial_timeout=None,
            tls_grpc_address=None,
            tls_grpc_insecure=False,
            passphrase=None,
        )

    mock_exit.assert_called_once_with(0)


def test_resolve_lease_handles_async_list_leases():
    config = Mock(spec=ClientConfigV1Alpha1)
    config.metadata = type("Metadata", (), {"name": "test-client"})()
    config.list_leases = AsyncMock(return_value=_make_lease_list(["async-lease"]))

    selected = anyio.run(_resolve_lease_from_active_async, config)
    assert selected == "async-lease"
    config.list_leases.assert_called_once_with(only_active=True)


def _make_expired_jwt() -> str:
    """Create a JWT with an exp claim in the past (no signature verification needed)."""

    def b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64url(json.dumps({"exp": int(time.time()) - 3600, "iss": "https://example.com"}).encode())
    sig = b64url(b"fakesig")
    return f"{header}.{payload}.{sig}"


def _make_valid_jwt() -> str:
    """Create a JWT with an exp claim in the future (no signature verification needed)."""

    def b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64url(json.dumps({"exp": int(time.time()) + 3600, "iss": "https://example.com"}).encode())
    sig = b64url(b"fakesig")
    return f"{header}.{payload}.{sig}"


def test_expired_token_triggers_reauth():
    config = _DummyConfig()
    config.token = _make_expired_jwt()

    def _login_side_effect(cfg):
        # Simulate successful re-authentication by updating the token
        cfg.token = _make_valid_jwt()

    login_mock = Mock(side_effect=_login_side_effect)

    @handle_exceptions_with_reauthentication(login_mock)
    def run_shell():
        anyio.run(
            _shell_with_signal_handling,
            config,
            "board-type=virtual",
            None,
            None,
            timedelta(minutes=1),
            False,
            (),
            None,
        )

    with patch(
        "jumpstarter_cli.shell._run_shell_with_lease_async",
        new=AsyncMock(return_value=0),
    ):
        run_shell()  # Should succeed after retry — no exception raised

    login_mock.assert_called_once_with(config)


def _make_config(token="tok", refresh_token="rt", path="/tmp/config.yaml"):
    """Create a mock config with sensible defaults."""
    config = Mock()
    config.token = token
    config.refresh_token = refresh_token
    config.path = path
    config.channel = AsyncMock(return_value=Mock(name="new_channel"))
    return config


def _make_mock_lease():
    """Create a mock lease with a refresh_channel method."""
    lease = Mock()
    lease.refresh_channel = Mock()
    return lease


class TestUpdateLeaseChannel:
    async def test_updates_channel_on_lease(self):
        config = _make_config()
        lease = _make_mock_lease()

        await _update_lease_channel(config, lease)

        config.channel.assert_awaited_once()
        lease.refresh_channel.assert_called_once_with(config.channel.return_value)

    async def test_noop_when_lease_is_none(self):
        config = _make_config()

        await _update_lease_channel(config, None)

        config.channel.assert_not_awaited()


class TestTryRefreshToken:
    async def test_returns_false_when_no_refresh_token(self):
        config = _make_config(refresh_token=None)
        assert await _try_refresh_token(config, _make_mock_lease()) is False

    async def test_returns_false_when_refresh_token_is_empty(self):
        config = _make_config(refresh_token="")
        assert await _try_refresh_token(config, _make_mock_lease()) is False

    @patch("jumpstarter_cli.shell.ClientConfigV1Alpha1")
    @patch("jumpstarter_cli.shell.Config")
    @patch("jumpstarter_cli.shell.decode_jwt_issuer", return_value="https://issuer")
    async def test_successful_refresh(self, _mock_issuer, mock_oidc_cls, mock_save):
        config = _make_config()
        lease = _make_mock_lease()

        mock_oidc = AsyncMock()
        mock_oidc.refresh_token_grant.return_value = {
            "access_token": "new_tok",
            "refresh_token": "new_rt",
        }
        mock_oidc_cls.return_value = mock_oidc

        result = await _try_refresh_token(config, lease)

        assert result is True
        assert config.token == "new_tok"
        assert config.refresh_token == "new_rt"
        lease.refresh_channel.assert_called_once()
        mock_save.save.assert_called_once()

    @patch("jumpstarter_cli.shell.ClientConfigV1Alpha1")
    @patch("jumpstarter_cli.shell.Config")
    @patch("jumpstarter_cli.shell.decode_jwt_issuer", return_value="https://issuer")
    async def test_successful_refresh_without_new_refresh_token(self, _mock_issuer, mock_oidc_cls, _mock_save):
        config = _make_config()
        lease = _make_mock_lease()

        mock_oidc = AsyncMock()
        mock_oidc.refresh_token_grant.return_value = {
            "access_token": "new_tok",
            # No refresh_token in response
        }
        mock_oidc_cls.return_value = mock_oidc

        result = await _try_refresh_token(config, lease)

        assert result is True
        assert config.token == "new_tok"
        assert config.refresh_token == "rt"  # unchanged

    @patch("jumpstarter_cli.shell.decode_jwt_issuer", side_effect=ValueError("bad jwt"))
    async def test_rollback_on_failure(self, _mock_issuer):
        config = _make_config(token="original_tok", refresh_token="original_rt")
        lease = _make_mock_lease()

        result = await _try_refresh_token(config, lease)

        assert result is False
        assert config.token == "original_tok"
        assert config.refresh_token == "original_rt"
        lease.refresh_channel.assert_not_called()

    @patch("jumpstarter_cli.shell.ClientConfigV1Alpha1")
    @patch("jumpstarter_cli.shell.Config")
    @patch("jumpstarter_cli.shell.decode_jwt_issuer", return_value="https://issuer")
    async def test_save_failure_does_not_fail_refresh(self, _mock_issuer, mock_oidc_cls, mock_save, caplog):
        """Disk save is best-effort; refresh should still succeed."""
        config = _make_config()
        lease = _make_mock_lease()

        mock_oidc = AsyncMock()
        mock_oidc.refresh_token_grant.return_value = {
            "access_token": "new_tok",
        }
        mock_oidc_cls.return_value = mock_oidc
        mock_save.save.side_effect = OSError("disk full")

        with caplog.at_level(logging.WARNING):
            result = await _try_refresh_token(config, lease)

        assert result is True
        assert config.token == "new_tok"
        assert "Failed to save refreshed token to disk" in caplog.text


class TestTryReloadTokenFromDisk:
    async def test_returns_false_when_no_path(self):
        config = _make_config(path=None)
        assert await _try_reload_token_from_disk(config, _make_mock_lease()) is False

    @patch("jumpstarter_cli.shell.ClientConfigV1Alpha1")
    @patch("jumpstarter_cli.shell.get_token_remaining_seconds", return_value=3600)
    async def test_successful_reload(self, _mock_remaining, mock_client_cfg):
        config = _make_config(token="old_tok", refresh_token="old_rt")
        lease = _make_mock_lease()

        disk_config = Mock()
        disk_config.token = "disk_tok"
        disk_config.refresh_token = "disk_rt"
        mock_client_cfg.from_file.return_value = disk_config

        result = await _try_reload_token_from_disk(config, lease)

        assert result is True
        assert config.token == "disk_tok"
        assert config.refresh_token == "disk_rt"
        lease.refresh_channel.assert_called_once()

    @patch("jumpstarter_cli.shell.ClientConfigV1Alpha1")
    @patch("jumpstarter_cli.shell.get_token_remaining_seconds", return_value=3600)
    async def test_clears_refresh_token_when_disk_has_none(self, _mock_remaining, mock_client_cfg):
        """If disk config has no refresh token, in-memory refresh token must be cleared."""
        config = _make_config(token="old_tok", refresh_token="stale_rt")
        lease = _make_mock_lease()

        disk_config = Mock()
        disk_config.token = "disk_tok"
        disk_config.refresh_token = None
        mock_client_cfg.from_file.return_value = disk_config

        result = await _try_reload_token_from_disk(config, lease)

        assert result is True
        assert config.token == "disk_tok"
        assert config.refresh_token is None

    @patch("jumpstarter_cli.shell.ClientConfigV1Alpha1")
    async def test_returns_false_when_disk_token_is_same(self, mock_client_cfg):
        config = _make_config(token="same_tok")
        disk_config = Mock()
        disk_config.token = "same_tok"
        mock_client_cfg.from_file.return_value = disk_config

        result = await _try_reload_token_from_disk(config, _make_mock_lease())

        assert result is False

    @patch("jumpstarter_cli.shell.ClientConfigV1Alpha1")
    @patch("jumpstarter_cli.shell.get_token_remaining_seconds", return_value=-10)
    async def test_returns_false_when_disk_token_is_expired(self, _mock_remaining, mock_client_cfg):
        config = _make_config(token="old_tok")
        disk_config = Mock()
        disk_config.token = "disk_tok"
        mock_client_cfg.from_file.return_value = disk_config

        result = await _try_reload_token_from_disk(config, _make_mock_lease())

        assert result is False
        assert config.token == "old_tok"  # unchanged

    @patch("jumpstarter_cli.shell.ClientConfigV1Alpha1")
    async def test_rollback_on_file_error(self, mock_client_cfg):
        config = _make_config(token="orig_tok", refresh_token="orig_rt")
        mock_client_cfg.from_file.side_effect = FileNotFoundError("gone")

        result = await _try_reload_token_from_disk(config, _make_mock_lease())

        assert result is False
        assert config.token == "orig_tok"
        assert config.refresh_token == "orig_rt"


class TestAttemptTokenRecovery:
    @patch("jumpstarter_cli.shell._try_reload_token_from_disk", new_callable=AsyncMock)
    @patch("jumpstarter_cli.shell._try_refresh_token", new_callable=AsyncMock)
    async def test_returns_message_on_oidc_success(self, mock_refresh, mock_disk):
        mock_refresh.return_value = True

        result = await _attempt_token_recovery(Mock(), Mock())

        assert result == "Token refreshed automatically."
        mock_disk.assert_not_awaited()  # should not fall through

    @patch("jumpstarter_cli.shell._try_reload_token_from_disk", new_callable=AsyncMock)
    @patch("jumpstarter_cli.shell._try_refresh_token", new_callable=AsyncMock)
    async def test_falls_back_to_disk_reload(self, mock_refresh, mock_disk):
        mock_refresh.return_value = False
        mock_disk.return_value = True

        result = await _attempt_token_recovery(Mock(), Mock())

        assert result == "Token reloaded from login."

    @patch("jumpstarter_cli.shell._try_reload_token_from_disk", new_callable=AsyncMock)
    @patch("jumpstarter_cli.shell._try_refresh_token", new_callable=AsyncMock)
    async def test_returns_none_when_all_fail(self, mock_refresh, mock_disk):
        mock_refresh.return_value = False
        mock_disk.return_value = False

        result = await _attempt_token_recovery(Mock(), Mock())

        assert result is None


class TestWarnRefreshFailed:
    @patch("jumpstarter_cli.shell.click")
    def test_warns_yellow_when_time_remaining(self, mock_click):
        _warn_refresh_failed(300)
        mock_click.style.assert_called_once()
        _, kwargs = mock_click.style.call_args
        assert kwargs["fg"] == "yellow"

    @patch("jumpstarter_cli.shell.click")
    def test_warns_red_when_expired(self, mock_click):
        _warn_refresh_failed(-10)
        mock_click.style.assert_called_once()
        _, kwargs = mock_click.style.call_args
        assert kwargs["fg"] == "red"


class TestMonitorTokenExpiry:
    async def test_returns_immediately_when_no_token(self):
        config = Mock(spec=[])  # no token attribute
        cancel_scope = Mock(cancel_called=False)

        await _monitor_token_expiry(config, None, cancel_scope)
        # Should return without error

    @patch("jumpstarter_cli.shell.anyio.sleep", new_callable=AsyncMock)
    @patch("jumpstarter_cli.shell.get_token_remaining_seconds", return_value=None)
    async def test_returns_when_remaining_is_none(self, _mock_remaining, _mock_sleep):
        config = _make_config()
        cancel_scope = Mock(cancel_called=False)

        await _monitor_token_expiry(config, None, cancel_scope)

    @patch("jumpstarter_cli.shell.click")
    @patch("jumpstarter_cli.shell.anyio.sleep", new_callable=AsyncMock)
    @patch("jumpstarter_cli.shell._attempt_token_recovery", new_callable=AsyncMock)
    @patch("jumpstarter_cli.shell.get_token_remaining_seconds")
    async def test_refreshes_when_below_threshold(self, mock_remaining, mock_recovery, mock_sleep, mock_click):
        # First call: below threshold; second call: raise to exit
        mock_remaining.side_effect = [60, Exception("done")]
        mock_recovery.return_value = "Token refreshed automatically."
        config = _make_config()
        cancel_scope = Mock(cancel_called=False)

        await _monitor_token_expiry(config, _make_mock_lease(), cancel_scope)

        mock_recovery.assert_awaited_once()
        # Should print the green success message
        mock_click.echo.assert_called()

    @patch("jumpstarter_cli.shell.click")
    @patch("jumpstarter_cli.shell.anyio.sleep", new_callable=AsyncMock)
    @patch("jumpstarter_cli.shell._attempt_token_recovery", new_callable=AsyncMock)
    @patch("jumpstarter_cli.shell.get_token_remaining_seconds")
    async def test_warns_when_refresh_fails(self, mock_remaining, mock_recovery, mock_sleep, mock_click):
        mock_remaining.side_effect = [60, Exception("done")]
        mock_recovery.return_value = None  # all recovery failed
        config = _make_config()
        cancel_scope = Mock(cancel_called=False)

        await _monitor_token_expiry(config, _make_mock_lease(), cancel_scope)

        mock_recovery.assert_awaited_once()

    @patch("jumpstarter_cli.shell.click")
    @patch("jumpstarter_cli.shell.anyio.sleep", new_callable=AsyncMock)
    @patch("jumpstarter_cli.shell.get_token_remaining_seconds")
    async def test_warns_within_expiry_window(self, mock_remaining, mock_sleep, mock_click):
        from jumpstarter_cli_common.oidc import TOKEN_EXPIRY_WARNING_SECONDS

        # First iteration: within warning window but above refresh threshold
        # Second iteration: exit via exception
        mock_remaining.side_effect = [
            TOKEN_EXPIRY_WARNING_SECONDS - 10,
            Exception("done"),
        ]
        config = _make_config()
        cancel_scope = Mock(cancel_called=False)

        await _monitor_token_expiry(config, _make_mock_lease(), cancel_scope)

        # Verify warning was echoed
        mock_click.echo.assert_called()
        args = mock_click.style.call_args
        assert "auto-refresh" in args[0][0]

    @patch("jumpstarter_cli.shell.anyio.sleep", new_callable=AsyncMock)
    @patch("jumpstarter_cli.shell.get_token_remaining_seconds", return_value=500)
    async def test_sleeps_30s_when_above_threshold(self, _mock_remaining, mock_sleep):
        # Exit after one loop via cancel_called
        call_count = 0

        def check_cancelled():
            nonlocal call_count
            call_count += 1  # ty: ignore[unresolved-reference]
            return call_count > 1

        config = _make_config()

        class _CancelScope(Mock):
            cancel_called = property(lambda self: check_cancelled())

        cancel_scope = _CancelScope()

        await _monitor_token_expiry(config, _make_mock_lease(), cancel_scope)

        mock_sleep.assert_awaited_with(30)

    @patch("jumpstarter_cli.shell.click")
    @patch("jumpstarter_cli.shell.anyio.sleep", new_callable=AsyncMock)
    @patch("jumpstarter_cli.shell._attempt_token_recovery", new_callable=AsyncMock)
    @patch("jumpstarter_cli.shell.get_token_remaining_seconds")
    async def test_sleeps_5s_when_below_threshold(self, mock_remaining, mock_recovery, mock_sleep, _mock_click):
        mock_remaining.side_effect = [60, Exception("done")]
        mock_recovery.return_value = None
        config = _make_config()
        cancel_scope = Mock(cancel_called=False)

        await _monitor_token_expiry(config, _make_mock_lease(), cancel_scope)

        mock_sleep.assert_awaited_with(5)

    @patch("jumpstarter_cli.shell.click")
    @patch("jumpstarter_cli.shell.anyio.sleep", new_callable=AsyncMock)
    @patch("jumpstarter_cli.shell._attempt_token_recovery", new_callable=AsyncMock)
    @patch("jumpstarter_cli.shell.get_token_remaining_seconds")
    async def test_does_not_cancel_scope_on_expiry(self, mock_remaining, mock_recovery, mock_sleep, _mock_click):
        """The monitor must never cancel the scope - the shell stays alive."""
        mock_remaining.side_effect = [60, Exception("done")]
        mock_recovery.return_value = None
        config = _make_config()
        cancel_scope = Mock(cancel_called=False)

        await _monitor_token_expiry(config, _make_mock_lease(), cancel_scope)

        cancel_scope.cancel.assert_not_called()

    @patch("jumpstarter_cli.shell.click")
    @patch("jumpstarter_cli.shell.anyio.sleep", new_callable=AsyncMock)
    @patch("jumpstarter_cli.shell._attempt_token_recovery", new_callable=AsyncMock)
    @patch("jumpstarter_cli.shell.get_token_remaining_seconds")
    async def test_warns_red_when_token_transitions_to_expired(
        self, mock_remaining, mock_recovery, mock_sleep, mock_click
    ):
        """After a yellow 'approaching expiry' warning, a red 'expired' warning
        must still appear when the token actually crosses zero."""
        mock_remaining.side_effect = [60, -5, Exception("done")]
        mock_recovery.return_value = None  # all recovery fails
        config = _make_config()
        cancel_scope = Mock(cancel_called=False)
        token_state = {"expired_unrecovered": False}

        await _monitor_token_expiry(config, _make_mock_lease(), cancel_scope, token_state)

        warn_calls = mock_click.style.call_args_list
        # Find the yellow warning (remaining > 0)
        yellow_calls = [c for c in warn_calls if c[1].get("fg") == "yellow"]
        # Find the red warning (remaining <= 0)

        red_calls = [c for c in warn_calls if c[1].get("fg") == "red"]
        assert len(yellow_calls) >= 1, "Expected yellow warning for near-expiry"
        assert len(red_calls) >= 1, "Expected red warning for actual expiry"
        assert token_state["expired_unrecovered"] is True


class _FakeStatusMonitor:
    """Minimal stand-in for StatusMonitor in shell tests."""

    def __init__(self, statuses=None, connection_lost=False):
        self._statuses = list(statuses or [ExporterStatus.LEASE_READY])
        self._connection_lost = connection_lost
        self._get_status_unsupported = False
        self.end_session_called = False

    @property
    def current_status(self):
        return self._statuses[0] if self._statuses else None

    @property
    def status_message(self):
        return ""

    @property
    def connection_lost(self):
        return self._connection_lost

    async def wait_for_first_observation(self, timeout=None):
        return self.current_status is not None

    async def wait_for_any_of(self, targets, timeout=None):
        for s in self._statuses:
            if s in targets:
                return s
        return None


def _make_shell_lease(*, release=True, lease_ended=False, name="test-lease"):
    """Build a mock lease suitable for _run_shell_with_lease_async tests."""
    lease = Mock()
    lease.release = release
    lease.name = name
    lease.lease_ended = lease_ended
    lease.lease_transferred = False
    lease.portal = Mock()
    lease.allow = []
    lease.unsafe = False
    lease.exporter_name = "test-exporter"

    @asynccontextmanager
    async def serve_unix_async():
        yield "/tmp/fake.sock"

    @asynccontextmanager
    async def monitor_async():
        yield

    lease.serve_unix_async = serve_unix_async
    lease.monitor_async = monitor_async
    return lease


def _build_fake_client(monitor, get_status_return=None, end_session_return=True):
    """Build a mock client wired to *monitor*."""
    client = AsyncMock()
    client.get_status_async.return_value = get_status_return
    client.end_session_async.return_value = end_session_return

    @asynccontextmanager
    async def log_stream_async(show_all_logs=False):
        yield

    @asynccontextmanager
    async def status_monitor_async(poll_interval=0.3):
        yield monitor

    client.log_stream_async = log_stream_async
    client.status_monitor_async = status_monitor_async
    return client


@asynccontextmanager
async def _fake_client_from_path_ctx(client):
    """Wraps a pre-built client in an async context manager for patching client_from_path."""
    yield client


class TestRunShellWithLeaseAsync:
    async def test_skips_after_lease_hook_when_lease_ended(self):
        monitor = _FakeStatusMonitor()
        client = _build_fake_client(monitor, get_status_return=ExporterStatus.LEASE_READY)
        lease = _make_shell_lease(release=True, lease_ended=True)
        cancel_scope = Mock(cancel_called=False)

        @asynccontextmanager
        async def fake_client_from_path(*_a, **_kw):
            yield client

        with (
            patch("jumpstarter_cli.shell.client_from_path", side_effect=fake_client_from_path),
            patch("jumpstarter_cli.shell._run_shell_only", return_value=42),
        ):
            exit_code = await _run_shell_with_lease_async(lease, False, None, (), cancel_scope)

        assert exit_code == 42
        client.end_session_async.assert_not_called()

    async def test_calls_end_session_when_lease_not_ended(self):
        monitor = _FakeStatusMonitor(statuses=[ExporterStatus.LEASE_READY, ExporterStatus.AVAILABLE])
        client = _build_fake_client(
            monitor,
            get_status_return=ExporterStatus.LEASE_READY,
            end_session_return=True,
        )
        lease = _make_shell_lease(release=True, lease_ended=False)
        cancel_scope = Mock(cancel_called=False)

        @asynccontextmanager
        async def fake_client_from_path(*_a, **_kw):
            yield client

        with (
            patch("jumpstarter_cli.shell.client_from_path", side_effect=fake_client_from_path),
            patch("jumpstarter_cli.shell._run_shell_only", return_value=0),
        ):
            exit_code = await _run_shell_with_lease_async(lease, False, None, (), cancel_scope)

        assert exit_code == 0
        client.end_session_async.assert_called_once()

    async def test_available_status_probe_with_lease_ended_race(self):
        """When lease expires during the probe (race condition), AVAILABLE
        should not be treated as connection loss."""
        monitor = _FakeStatusMonitor()
        lease = _make_shell_lease(release=True, lease_ended=False)
        cancel_scope = Mock(cancel_called=False)

        call_count = 0

        async def get_status_race():
            nonlocal call_count
            call_count += 1  # ty: ignore[unresolved-reference]
            if call_count == 1:
                return ExporterStatus.LEASE_READY
            lease.lease_ended = True
            return ExporterStatus.AVAILABLE

        client = _build_fake_client(monitor, get_status_return=ExporterStatus.LEASE_READY)
        client.get_status_async = get_status_race

        @asynccontextmanager
        async def fake_client_from_path(*_a, **_kw):
            yield client

        with (
            patch("jumpstarter_cli.shell.client_from_path", side_effect=fake_client_from_path),
            patch("jumpstarter_cli.shell._run_shell_only", return_value=0),
        ):
            exit_code = await _run_shell_with_lease_async(lease, False, None, (), cancel_scope)

        assert exit_code == 0
        assert not monitor._connection_lost

    async def test_probe_cancelled_when_connection_lost(self):
        """Probe blocks while connection_lost is False, then unblocks
        once the monitor marks connection as lost."""
        monitor = _FakeStatusMonitor()
        lease = _make_shell_lease(release=True, lease_ended=False)
        cancel_scope = Mock(cancel_called=False)

        initial_done = anyio.Event()

        async def get_status_blocks_on_probe():
            if not initial_done.is_set():
                initial_done.set()
                return ExporterStatus.LEASE_READY
            await anyio.sleep(math.inf)

        client = _build_fake_client(monitor, get_status_return=ExporterStatus.LEASE_READY)
        client.get_status_async = get_status_blocks_on_probe

        @asynccontextmanager
        async def fake_client_from_path(*_a, **_kw):
            yield client

        function_returned = anyio.Event()

        async def run_shell():
            nonlocal exit_code
            with (
                patch("jumpstarter_cli.shell.client_from_path", side_effect=fake_client_from_path),
                patch("jumpstarter_cli.shell._run_shell_only", return_value=0),
            ):
                exit_code = await _run_shell_with_lease_async(lease, False, None, (), cancel_scope)
            function_returned.set()

        async def flip_connection_lost():
            await anyio.sleep(5)
            assert not function_returned.is_set(), "probe returned while connection_lost was False"
            monitor._connection_lost = True

        exit_code = -1
        with anyio.fail_after(10):
            async with anyio.create_task_group() as tg:
                tg.start_soon(run_shell)
                tg.start_soon(flip_connection_lost)

        assert exit_code == 0
        assert monitor._connection_lost
        client.end_session_async.assert_not_called()


class TestCancelIfConnectionLost:
    """Unit tests for _cancel_if_connection_lost helper."""

    async def test_returns_result_when_coro_completes(self):
        """When the coroutine completes normally, its result is returned."""
        monitor = _FakeStatusMonitor()

        async def quick_coro():
            return "ok"

        result = await _cancel_if_connection_lost(monitor, quick_coro())
        assert result == "ok"
        assert not monitor.connection_lost

    async def test_returns_none_when_connection_lost(self):
        """When connection_lost flips while the coroutine is blocked, returns None."""
        monitor = _FakeStatusMonitor()

        async def blocking_coro():
            await anyio.sleep(math.inf)

        async def flip_after_delay():
            await anyio.sleep(2)
            monitor._connection_lost = True

        with anyio.fail_after(10):
            async with anyio.create_task_group() as tg:
                tg.start_soon(flip_after_delay)
                result = await _cancel_if_connection_lost(monitor, blocking_coro())

        assert result is None


class TestShellWithSignalHandlingExceptionGroup:
    """Tests for _shell_with_signal_handling's outer BaseExceptionGroup handler.

    This handler catches exceptions from the lease context —
    i.e. unrecoverable task-group failures.  We patch _run_shell_with_lease_async
    directly to isolate this layer.
    """

    def _make_config_with_lease(self, lease):
        config = _DummyConfig()

        @asynccontextmanager
        async def lease_async(
            selector, exporter_name, lease_name, duration, portal,
            acquisition_timeout, retry_timeout=None, dial_timeout=None, **kwargs,
        ):
            yield lease

        config.lease_async = lease_async
        return config

    async def test_exits_gracefully_when_lease_ended(self):
        """When the lease expired naturally, a task-group failure exits with code 0."""
        lease = Mock()
        lease.release = True
        lease.name = "timeout-lease"
        lease.lease_ended = True
        lease.lease_transferred = False
        config = self._make_config_with_lease(lease)

        async def fake_run(*_):
            raise BaseExceptionGroup("test", [RuntimeError("simulated cancellation")])

        with (
            patch("jumpstarter_cli.shell._monitor_token_expiry", new_callable=AsyncMock),
            patch("jumpstarter_cli.shell._run_shell_with_lease_async", side_effect=fake_run),
        ):
            exit_code = await _shell_with_signal_handling(
                config, None, None, None, timedelta(minutes=1), False, (), None
            )

        assert exit_code == 0

    async def test_raises_offline_error_when_lease_active_and_task_group_fails(self):
        """An active lease that hits a task-group failure surfaces ExporterOfflineError.

        _shell_with_signal_handling's outer create_task_group always wraps the raised
        exception in a BaseExceptionGroup (anyio behaviour).  The caller
        (handle_exceptions_with_reauthentication) unwraps it for the user, so we check
        that ExporterOfflineError is present inside the group.
        """
        from jumpstarter_cli_common.exceptions import find_exception_in_group

        lease = Mock()
        lease.release = True
        lease.name = "active-lease"
        lease.lease_ended = False
        lease.lease_transferred = False
        config = self._make_config_with_lease(lease)

        async def fake_run(*_):
            raise BaseExceptionGroup("test", [RuntimeError("connection broken")])

        with (
            patch("jumpstarter_cli.shell._monitor_token_expiry", new_callable=AsyncMock),
            patch("jumpstarter_cli.shell._run_shell_with_lease_async", side_effect=fake_run),
        ):
            with pytest.raises(BaseExceptionGroup) as exc_info:
                await _shell_with_signal_handling(
                    config, None, None, None, timedelta(minutes=1), False, (), None
                )

        assert isinstance(exc_info.value, BaseExceptionGroup)
        offline_exc = find_exception_in_group(exc_info.value, ExporterOfflineError)
        assert offline_exc is not None
        assert "Connection to exporter lost" in str(offline_exc)


class TestRetryLoopTimeout:
    """Tests for the retry_timeout bounded retry in _shell_with_signal_handling."""

    async def test_retries_then_raises_on_timeout(self):
        """ExporterUnreachableError retries until retry_timeout expires, then raises."""
        lease = Mock()
        lease.release = True
        lease.name = "test-lease"
        lease.exporter_name = "test-exporter"
        lease.retry_timeout = 0.3
        lease.lease_ended = False
        lease.lease_transferred = False

        config = _DummyConfig()
        state = {"call_count": 0}

        @asynccontextmanager
        async def lease_async(
            selector, exporter_name, lease_name, duration, portal,
            acquisition_timeout, retry_timeout=None, dial_timeout=None, **kwargs,
        ):
            yield lease

        config.lease_async = lease_async

        async def fake_run(*_):
            state["call_count"] += 1
            raise ExporterUnreachableError("exporter dead")

        with (
            patch("jumpstarter_cli.shell._monitor_token_expiry", new_callable=AsyncMock),
            patch("jumpstarter_cli.shell._run_shell_with_lease_async", side_effect=fake_run),
        ):
            with pytest.raises((ExporterUnreachableError, BaseExceptionGroup)) as exc_info:
                await _shell_with_signal_handling(
                    config, None, None, None, timedelta(minutes=1), False, (), None
                )

        exc = exc_info.value
        if isinstance(exc, BaseExceptionGroup):
            from jumpstarter_cli_common.exceptions import find_exception_in_group

            exc = find_exception_in_group(exc, ExporterUnreachableError)
            assert exc is not None
        assert "after 0s of retrying" in str(exc)
        assert state["call_count"] >= 1

    async def test_retries_when_wrapped_in_exception_group(self):
        """ExporterUnreachableError wrapped in BaseExceptionGroup is also retried."""
        lease = Mock()
        lease.release = True
        lease.name = "test-lease"
        lease.exporter_name = "test-exporter"
        lease.retry_timeout = 10.0
        lease.lease_ended = False
        lease.lease_transferred = False

        config = _DummyConfig()
        state = {"call_count": 0}

        @asynccontextmanager
        async def lease_async(
            selector, exporter_name, lease_name, duration, portal,
            acquisition_timeout, retry_timeout=None, dial_timeout=None, **kwargs,
        ):
            yield lease

        config.lease_async = lease_async

        async def fake_run(*_):
            state["call_count"] += 1
            if state["call_count"] < 3:
                raise BaseExceptionGroup(
                    "task group", [ExporterUnreachableError("exporter dead")]
                )
            return 0

        with (
            patch("jumpstarter_cli.shell._monitor_token_expiry", new_callable=AsyncMock),
            patch("jumpstarter_cli.shell._run_shell_with_lease_async", side_effect=fake_run),
        ):
            exit_code = await _shell_with_signal_handling(
                config, None, None, None, timedelta(minutes=1), False, (), None
            )

        assert exit_code == 0
        assert state["call_count"] == 3

    async def test_retry_succeeds_before_timeout(self):
        """ExporterUnreachableError retries and succeeds when exporter comes back."""
        state = {"call_count": 0}

        lease = Mock()
        lease.release = True
        lease.name = "test-lease"
        lease.exporter_name = "test-exporter"
        lease.retry_timeout = 10.0
        lease.lease_ended = False
        lease.lease_transferred = False

        config = _DummyConfig()

        @asynccontextmanager
        async def lease_async(
            selector, exporter_name, lease_name, duration, portal,
            acquisition_timeout, retry_timeout=None, dial_timeout=None, **kwargs,
        ):
            yield lease

        config.lease_async = lease_async

        async def fake_run(*_):
            state["call_count"] += 1
            if state["call_count"] < 3:
                raise ExporterUnreachableError("exporter dead")
            return 0

        with (
            patch("jumpstarter_cli.shell._monitor_token_expiry", new_callable=AsyncMock),
            patch("jumpstarter_cli.shell._run_shell_with_lease_async", side_effect=fake_run),
        ):
            exit_code = await _shell_with_signal_handling(
                config, None, None, None, timedelta(minutes=1), False, (), None
            )

        assert exit_code == 0
        assert state["call_count"] == 3


class TestRetryLoopLeaseExpired:
    """Tests for lease expiry detection in the retry loop."""

    async def test_exits_cleanly_when_lease_ended_during_connection(self):
        """Retry loop should exit cleanly instead of retrying when lease has ended."""
        lease = Mock()
        lease.release = True
        lease.name = "test-lease"
        lease.exporter_name = "test-exporter"
        lease.retry_timeout = 10.0
        lease.lease_ended = True
        lease.lease_transferred = False

        config = _DummyConfig()
        state = {"call_count": 0}

        @asynccontextmanager
        async def lease_async(
            selector, exporter_name, lease_name, duration, portal,
            acquisition_timeout, retry_timeout=None, dial_timeout=None, **kwargs,
        ):
            yield lease

        config.lease_async = lease_async

        async def fake_run(*_):
            state["call_count"] += 1
            raise ExporterUnreachableError("exporter dead")

        with (
            patch("jumpstarter_cli.shell._monitor_token_expiry", new_callable=AsyncMock),
            patch("jumpstarter_cli.shell._run_shell_with_lease_async", side_effect=fake_run),
        ):
            exit_code = await _shell_with_signal_handling(
                config, None, None, None, timedelta(minutes=1), False, (), None
            )

        assert exit_code == 0
        assert state["call_count"] == 1  # did not retry

    async def test_retries_normally_when_lease_not_ended(self):
        """Retry loop should continue retrying when lease has not ended."""
        lease = Mock()
        lease.release = True
        lease.name = "test-lease"
        lease.exporter_name = "test-exporter"
        lease.retry_timeout = 10.0
        lease.lease_ended = False
        lease.lease_transferred = False

        config = _DummyConfig()
        state = {"call_count": 0}

        @asynccontextmanager
        async def lease_async(
            selector, exporter_name, lease_name, duration, portal,
            acquisition_timeout, retry_timeout=None, dial_timeout=None, **kwargs,
        ):
            yield lease

        config.lease_async = lease_async

        async def fake_run(*_):
            state["call_count"] += 1
            if state["call_count"] < 3:
                raise ExporterUnreachableError("exporter dead")
            return 0

        with (
            patch("jumpstarter_cli.shell._monitor_token_expiry", new_callable=AsyncMock),
            patch("jumpstarter_cli.shell._run_shell_with_lease_async", side_effect=fake_run),
        ):
            exit_code = await _shell_with_signal_handling(
                config, None, None, None, timedelta(minutes=1), False, (), None
            )

        assert exit_code == 0
        assert state["call_count"] == 3
