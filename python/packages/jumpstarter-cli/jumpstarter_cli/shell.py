import logging
import os
import sys
import time
from contextlib import ExitStack
from datetime import timedelta
from types import SimpleNamespace

import anyio
import anyio.from_thread
import anyio.to_thread
import click
import grpc
import grpc.aio
from anyio import create_task_group, get_cancelled_exc_class
from jumpstarter_cli_common.config import opt_config
from jumpstarter_cli_common.exceptions import find_exception_in_group, handle_exceptions_with_reauthentication
from jumpstarter_cli_common.oidc import (
    TOKEN_EXPIRY_WARNING_SECONDS,
    Config,
    decode_jwt_issuer,
    format_duration,
    get_token_remaining_seconds,
)
from jumpstarter_cli_common.signal import signal_handler

from .common import (
    opt_acquisition_timeout,
    opt_allow_disabled,
    opt_dial_timeout,
    opt_duration_partial,
    opt_exporter_name,
    opt_retry_timeout,
    opt_selector,
)
from .login import relogin_client
from jumpstarter.client import DirectLease
from jumpstarter.client.client import client_from_path, fetch_motd
from jumpstarter.client.exceptions import LeaseError
from jumpstarter.client.telemetry import attach_client_telemetry
from jumpstarter.common import HOOK_WARNING_PREFIX, ExporterStatus
from jumpstarter.common.exceptions import (
    ConnectionError,
    ExporterOfflineError,
    ExporterUnreachableError,
)
from jumpstarter.common.utils import launch_shell
from jumpstarter.config.client import ClientConfigV1Alpha1
from jumpstarter.config.env import JMP_LEASE
from jumpstarter.config.exporter import ExporterConfigV1Alpha1
from jumpstarter.config.tls import TLSConfigV1Alpha1

logger = logging.getLogger(__name__)

# Refresh token when less than this many seconds remain
_TOKEN_REFRESH_THRESHOLD_SECONDS = 120


def _run_shell_only(lease, config, command, path: str, motd: str | None = None) -> int:
    """Run just the shell command without log streaming."""
    allow = config.drivers.allow if config is not None else getattr(lease, "allow", [])
    unsafe = config.drivers.unsafe if config is not None else getattr(lease, "unsafe", False)
    use_profiles = config.shell.use_profiles if config is not None else False
    insecure = getattr(lease, "insecure", False)
    passphrase = getattr(lease, "passphrase", None)
    return launch_shell(
        path,
        lease.exporter_name,
        allow,
        unsafe,
        use_profiles,
        command=command,
        lease=lease,
        insecure=insecure,
        passphrase=passphrase,
        motd=motd,
    )


def _warn_about_expired_token(lease_name: str, selector: str) -> None:
    """Warn user that lease won't be cleaned up due to expired token."""
    click.echo(click.style("\nToken expired - lease cleanup will fail.", fg="yellow", bold=True))
    click.echo(click.style(f"Lease '{lease_name}' will remain active.", fg="yellow"))
    click.echo(click.style(f"To reconnect: JMP_LEASE={lease_name} jmp shell", fg="cyan"))


async def _update_lease_channel(config, lease) -> None:
    """Update the lease's gRPC channel with the current config credentials."""
    if lease is not None:
        new_channel = await config.channel()
        lease.refresh_channel(new_channel)


async def _try_refresh_token(config, lease) -> bool:
    """Attempt to refresh the token and update the lease channel.

    Returns True if refresh succeeded, False otherwise.
    """
    refresh_token = getattr(config, "refresh_token", None)
    if not refresh_token:
        return False

    old_token = config.token
    old_refresh_token = config.refresh_token
    try:
        issuer = decode_jwt_issuer(config.token)
        oidc = Config(
            issuer=issuer,
            client_id="jumpstarter-cli",
            offline_access=True,
        )

        tokens = await oidc.refresh_token_grant(refresh_token)
        config.token = tokens["access_token"]
        new_refresh_token = tokens.get("refresh_token")
        if new_refresh_token is not None:
            config.refresh_token = new_refresh_token

        # Update the lease channel first (critical for the running session)
        await _update_lease_channel(config, lease)

        # Persist to disk (best-effort, uses original config path)
        try:
            ClientConfigV1Alpha1.save(config, path=config.path)
        except Exception as e:
            logger.warning("Failed to save refreshed token to disk: %s", e)

        return True
    except Exception as e:
        # Restore old token so the monitor doesn't think we succeeded
        config.token = old_token
        config.refresh_token = old_refresh_token
        logger.debug("Token refresh failed: %s", e)
        return False


async def _try_reload_token_from_disk(config, lease) -> bool:
    """Check if the config on disk has a newer/valid token (e.g. from 'jmp login').

    If a valid token is found on disk, updates the in-memory config and lease channel.
    Returns True if a valid token was loaded, False otherwise.
    """
    config_path = getattr(config, "path", None)
    if not config_path:
        return False

    old_token = config.token
    old_refresh_token = config.refresh_token
    try:
        disk_config = ClientConfigV1Alpha1.from_file(config_path)
        disk_token = getattr(disk_config, "token", None)
        if not disk_token or disk_token == config.token:
            return False

        # Check if the token on disk is actually valid
        disk_remaining = get_token_remaining_seconds(disk_token)
        if disk_remaining is None or disk_remaining <= 0:
            return False

        # Token on disk is valid and different - use it
        config.token = disk_token
        config.refresh_token = getattr(disk_config, "refresh_token", None)

        # Update the lease channel (critical for the running session)
        await _update_lease_channel(config, lease)

        return True
    except Exception as e:
        config.token = old_token
        config.refresh_token = old_refresh_token
        logger.debug("Failed to reload token from disk: %s", e)
        return False


async def _attempt_token_recovery(config, lease) -> str | None:
    """Try all available methods to recover a valid token.

    Attempts OIDC refresh first, then falls back to reloading from disk
    (e.g. if user ran 'jmp login' from the shell).

    Returns a message describing the recovery method, or None if all failed.
    """
    if await _try_refresh_token(config, lease):
        return "Token refreshed automatically."
    if await _try_reload_token_from_disk(config, lease):
        return "Token reloaded from login."
    return None


def _warn_refresh_failed(remaining: float) -> None:
    """Warn the user that token refresh failed."""
    if remaining > 0:
        duration = format_duration(remaining)
        click.echo(
            click.style(
                f"\nToken expires in {duration} and auto-refresh failed. "
                "Run 'jmp login' from this shell to refresh manually.",
                fg="yellow",
                bold=True,
            )
        )
    else:
        click.echo(
            click.style(
                "\nToken expired and auto-refresh failed. "
                "New commands will fail until you run 'jmp login' from this shell.",
                fg="red",
                bold=True,
            )
        )


async def _handle_token_refresh(config, lease, remaining, warn_state, token_state=None) -> None:
    """Try to recover the token and update warning state accordingly."""
    recovery_msg = await _attempt_token_recovery(config, lease)
    if recovery_msg:
        click.echo(click.style(f"\n{recovery_msg}", fg="green"))
        warn_state["expiry"] = False
        warn_state["refresh_failed"] = False
        warn_state["token_expired"] = False
        if token_state is not None:
            token_state["expired_unrecovered"] = False
    elif remaining <= 0 and not warn_state["token_expired"]:
        _warn_refresh_failed(remaining)
        warn_state["token_expired"] = True
        if token_state is not None:
            token_state["expired_unrecovered"] = True
    elif remaining > 0 and not warn_state["refresh_failed"]:
        _warn_refresh_failed(remaining)
        warn_state["refresh_failed"] = True


async def _monitor_token_expiry(config, lease, cancel_scope, token_state=None) -> None:
    """Monitor token expiry, auto-refresh when possible, warn user otherwise.

    this monitor:
    1. Proactively refreshes the token before it expires using the refresh token
    2. Updates the lease's gRPC channel with new credentials
    3. If refresh fails, periodically checks the config on disk for a token
       refreshed externally (e.g. via 'jmp login' from within the shell)
    4. Never cancels the scope - the shell stays alive regardless
    """
    token = getattr(config, "token", None)
    if not token:
        return

    warn_state = {"expiry": False, "refresh_failed": False, "token_expired": False}
    while not cancel_scope.cancel_called:
        try:
            # Re-read config.token each iteration since it may have been refreshed
            remaining = get_token_remaining_seconds(config.token)
            if remaining is None:
                return

            if remaining <= _TOKEN_REFRESH_THRESHOLD_SECONDS:
                await _handle_token_refresh(config, lease, remaining, warn_state, token_state)
            elif remaining <= TOKEN_EXPIRY_WARNING_SECONDS and not warn_state["expiry"]:
                duration = format_duration(remaining)
                click.echo(
                    click.style(
                        f"\nToken expires in {duration}. Will attempt auto-refresh.",
                        fg="yellow",
                        bold=True,
                    )
                )
                warn_state["expiry"] = True

            # Check more frequently as we approach expiry
            if remaining <= _TOKEN_REFRESH_THRESHOLD_SECONDS:
                await anyio.sleep(5)
            else:
                await anyio.sleep(30)
        except Exception:
            return


async def _cancel_if_connection_lost(monitor, coro):
    """Run *coro* but cancel it as soon as monitor.connection_lost becomes True.

    Returns the coroutine's result, or None if cancelled due to connection loss.
    """
    result = None
    async with anyio.create_task_group() as tg:
        async def _watcher():
            while not monitor.connection_lost:
                await anyio.sleep(1)
            tg.cancel_scope.cancel()

        tg.start_soon(_watcher)
        result = await coro
        tg.cancel_scope.cancel()
    return result


async def _run_shell_with_lease_async(lease, exporter_logs, config, command, cancel_scope):  # noqa: C901
    """Run shell with lease context managers and wait for afterLease hook if logs enabled.

    When exporter_logs is enabled, this function will:
    1. Connect and start log streaming with background status monitor
    2. Wait for beforeLease hook to complete (logs stream in real-time)
    3. Run the shell command
    4. After shell exits, call EndSession to trigger and wait for afterLease hook
    5. Logs stream to client during hook execution
    6. Release the lease after hook completes

    Uses non-blocking polling via StatusMonitor for robust status tracking.
    If Ctrl+C is pressed during EndSession, the wait is skipped but the lease is still released.
    """
    async with lease.serve_unix_async() as path:
        async with lease.monitor_async():
            # Use ExitStack for the client (required by client_from_path)
            with ExitStack() as stack:
                async with client_from_path(
                    path,
                    lease.portal,
                    stack,
                    allow=lease.allow,
                    unsafe=lease.unsafe,
                    tls_config=getattr(lease, "tls_config", None),
                    grpc_options=getattr(lease, "grpc_options", None),
                    insecure=getattr(lease, "insecure", False),
                    passphrase=getattr(lease, "passphrase", None),
                ) as client:
                    try:
                        await client.get_status_async()
                    except grpc.aio.AioRpcError as e:
                        if e.code() in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED):
                            raise ExporterUnreachableError(
                                f"Exporter {lease.exporter_name} did not respond to initial status check"
                            ) from e
                        raise

                    # Start log streaming and status monitor together
                    # The status monitor polls in the background for reliable status tracking
                    async with client.log_stream_async(show_all_logs=exporter_logs):
                        async with client.status_monitor_async(poll_interval=0.3) as monitor:
                            # Wait for beforeLease hook to complete while logs are streaming
                            # This allows hook output to be displayed in real-time
                            # Uses non-blocking polling instead of streaming for robustness
                            logger.info("Waiting for beforeLease hook to complete...")

                            # Wait for LEASE_READY or hook failure using background monitor
                            result = await monitor.wait_for_any_of(
                                [ExporterStatus.LEASE_READY, ExporterStatus.BEFORE_LEASE_HOOK_FAILED], timeout=300.0
                            )

                            if result == ExporterStatus.BEFORE_LEASE_HOOK_FAILED:
                                reason = monitor.status_message or "beforeLease hook failed"
                                raise ExporterOfflineError(reason)
                            elif result is None:
                                if monitor.connection_lost:
                                    # Connection lost while waiting for hook - lease expired
                                    logger.info("Lease expired while waiting for beforeLease hook to complete")
                                    return 0
                                else:
                                    reason = monitor.status_message or "Timeout waiting for beforeLease hook"
                                    raise ExporterOfflineError(reason)

                            logger.debug("Exporter ready (status: %s), launching shell...", result)

                            if monitor.status_message and monitor.status_message.startswith(HOOK_WARNING_PREFIX):
                                warning_text = monitor.status_message[len(HOOK_WARNING_PREFIX) :]
                                click.echo(click.style(f"Warning: {warning_text}", fg="yellow", bold=True))

                            # Fetch motd now (after the beforeLease hook has run);
                            # skipped in command mode where it is never shown
                            motd = None
                            if not command:
                                try:
                                    with anyio.fail_after(5):
                                        motd = await fetch_motd(client)
                                except Exception:
                                    logger.debug("Failed to fetch motd, continuing without it")

                            # Run the shell command
                            exit_code = await anyio.to_thread.run_sync(
                                _run_shell_only, lease, config, command, path, motd
                            )

                            # Shell has exited. For auto-created leases (release=True), call
                            # EndSession to trigger afterLease hook while keeping log stream
                            # and status monitor open. For pre-created leases (release=False),
                            # skip EndSession so the exporter stays in LEASE_READY and the
                            # user can reconnect later.
                            if (
                                lease.release
                                and lease.name
                                and not lease.lease_ended
                                and not cancel_scope.cancel_called
                                and not monitor._get_status_unsupported
                            ):
                                # Quick probe to catch exporter restarts the slow-poll loop
                                # (5s interval in LEASE_READY) may not have detected yet.
                                if not monitor.connection_lost:
                                    try:
                                        probe_status = await _cancel_if_connection_lost(
                                            monitor, client.get_status_async()
                                        )
                                        if probe_status is None:
                                            logger.debug(
                                                "Connection probe timed out, marking connection as lost"
                                            )
                                            monitor._connection_lost = True
                                        elif lease.lease_ended:
                                            logger.debug(
                                                "Lease ended during probe (status=%s), skipping afterLease hook",
                                                probe_status,
                                            )
                                            return exit_code
                                        elif probe_status not in (
                                            ExporterStatus.LEASE_READY,
                                            ExporterStatus.AFTER_LEASE_HOOK,
                                        ):
                                            logger.debug(
                                                "Exporter in unexpected state (%s), skipping afterLease hook",
                                                probe_status,
                                            )
                                            monitor._connection_lost = True
                                    except Exception:
                                        if lease.lease_ended:
                                            logger.debug("Lease ended during probe, skipping afterLease hook")
                                            return exit_code
                                        logger.debug("Connection probe failed, marking connection as lost")
                                        monitor._connection_lost = True

                                if monitor.connection_lost:
                                    logger.debug("Connection already lost, skipping afterLease hook")
                                else:
                                    logger.info("Running afterLease hook (Ctrl+C to skip)...")
                                    try:
                                        # EndSession triggers the afterLease hook asynchronously
                                        # Wrap in anyio timeout as safety net in case gRPC deadline
                                        # doesn't fire on a broken channel (e.g. lease timeout)
                                        success = False
                                        with anyio.move_on_after(10):
                                            success = await client.end_session_async()
                                        if success:
                                            # Wait for hook to complete using background monitor
                                            # This allows afterLease logs to be displayed in real-time
                                            result = await monitor.wait_for_any_of(
                                                [ExporterStatus.AVAILABLE, ExporterStatus.AFTER_LEASE_HOOK_FAILED],
                                                timeout=300.0,
                                            )
                                            if result == ExporterStatus.AVAILABLE:
                                                if monitor.status_message and monitor.status_message.startswith(
                                                    HOOK_WARNING_PREFIX
                                                ):
                                                    warning_text = monitor.status_message[len(HOOK_WARNING_PREFIX) :]
                                                    click.echo(
                                                        click.style(f"Warning: {warning_text}", fg="yellow", bold=True)
                                                    )
                                                logger.info("afterLease hook completed")
                                            elif result == ExporterStatus.AFTER_LEASE_HOOK_FAILED:
                                                reason = monitor.status_message or "afterLease hook failed"
                                                raise ExporterOfflineError(reason)
                                            elif monitor.connection_lost:
                                                # If connection lost during afterLease hook lifecycle
                                                # (running or failed), the exporter shut down
                                                if monitor.current_status in (
                                                    ExporterStatus.AFTER_LEASE_HOOK,
                                                    ExporterStatus.AFTER_LEASE_HOOK_FAILED,
                                                ):
                                                    reason = (
                                                        monitor.status_message
                                                        or "afterLease hook failed (connection lost)"
                                                    )
                                                    raise ExporterOfflineError(reason)
                                                # Connection lost but hook wasn't running. This is expected when
                                                # the lease times out - exporter handles its own cleanup.
                                                logger.info("Connection lost, skipping afterLease hook wait")
                                            elif result is None:
                                                logger.warning("Timeout waiting for afterLease hook to complete")
                                        else:
                                            logger.debug("EndSession not implemented, skipping hook wait")
                                    except ExporterOfflineError:
                                        raise
                                    except Exception as e:
                                        logger.warning("Error during afterLease hook: %s", e)

                            return exit_code


async def _shell_with_signal_handling(  # noqa: C901
    config, selector, exporter_name, lease_name, duration, exporter_logs, command, acquisition_timeout,
    retry_timeout=None, dial_timeout=None, allow_disabled=False,
):
    """Handle lease acquisition and shell execution with signal handling."""
    exit_code = 0
    cancelled_exc_class = get_cancelled_exc_class()
    lease_used = None
    token_state = {"expired_unrecovered": False}

    # Check token before starting
    token = getattr(config, "token", None)
    if token:
        remaining = get_token_remaining_seconds(token)
        if remaining is not None and remaining <= 0:
            err = ConnectionError("token is expired")
            err.set_config(config)
            raise err

    telemetry = None
    async with create_task_group() as tg:
        tg.start_soon(signal_handler, tg.cancel_scope)

        telemetry = await attach_client_telemetry(config)
        if telemetry is not None:
            tg.start_soon(telemetry.handler.flush_loop)

        try:
            try:
                async with anyio.from_thread.BlockingPortal() as portal:
                    connect_deadline = None
                    while True:
                        async with config.lease_async(
                            selector, exporter_name, lease_name, duration, portal, acquisition_timeout,
                            retry_timeout=retry_timeout, dial_timeout=dial_timeout,
                            allow_disabled=allow_disabled,
                        ) as lease:
                            lease_used = lease

                            # Start token monitoring only once we're in the shell
                            tg.start_soon(_monitor_token_expiry, config, lease, tg.cancel_scope, token_state)

                            unreachable = None
                            try:
                                exit_code = await _run_shell_with_lease_async(
                                    lease, exporter_logs, config, command, tg.cancel_scope
                                )
                            except BaseExceptionGroup as eg:
                                unreachable = find_exception_in_group(eg, ExporterUnreachableError)
                                if unreachable is None:
                                    raise
                            except ExporterUnreachableError as exc:
                                unreachable = exc
                            if unreachable is not None:
                                if lease.lease_ended:
                                    break  # lease expired naturally — exit cleanly
                                if lease.lease_transferred:
                                    raise ExporterOfflineError(
                                        "Lease has been transferred to another client. "
                                        "Session is no longer valid."
                                    ) from unreachable
                                if connect_deadline is None:
                                    connect_deadline = time.monotonic() + lease.retry_timeout
                                if time.monotonic() >= connect_deadline:
                                    raise ExporterUnreachableError(
                                        f"Exporter {lease.exporter_name} unreachable after "
                                        f"{lease.retry_timeout:.0f}s of retrying"
                                    ) from unreachable
                                logger.warning(
                                    "Exporter %s is unreachable, releasing lease and retrying...",
                                    lease.exporter_name,
                                )
                                logger.debug("Unreachable cause: %s", unreachable)
                                continue  # lease released by __aexit__, loop re-acquires
                            if lease.release and lease.name and token_state["expired_unrecovered"]:
                                _warn_about_expired_token(lease.name, selector)
                            break
            except BaseExceptionGroup as eg:
                for exc in eg.exceptions:
                    if isinstance(exc, TimeoutError):
                        raise exc from None
                unreachable_exc = find_exception_in_group(eg, ExporterUnreachableError)
                if unreachable_exc:
                    raise unreachable_exc from None
                offline_exc = find_exception_in_group(eg, ExporterOfflineError)
                if offline_exc:
                    raise offline_exc from None
                lease_exc = find_exception_in_group(eg, LeaseError)
                if lease_exc:
                    raise lease_exc from None
                if lease_used is not None:
                    if lease_used.lease_ended:
                        # Lease expired naturally (e.g. during beforeLease hook)
                        # - exit gracefully instead of showing a scary error
                        pass
                    elif lease_used.lease_transferred:
                        raise ExporterOfflineError(
                            "Lease has been transferred to another client. Session is no longer valid."
                        ) from None
                    else:
                        raise ExporterOfflineError("Connection to exporter lost") from None
                else:
                    raise
            except cancelled_exc_class:
                # Check if cancellation was due to token expiry
                token = getattr(config, "token", None)
                if lease_used and token:
                    remaining = get_token_remaining_seconds(token)
                    if remaining is not None and remaining <= 0:
                        _warn_about_expired_token(lease_used.name, selector)
                exit_code = 2
        finally:
            if not tg.cancel_scope.cancel_called:
                tg.cancel_scope.cancel()

    if telemetry is not None:
        await telemetry.aclose()
    return exit_code


def _format_lease_display(lease) -> str:
    parts = []
    if lease.exporter:
        parts.append(f"exporter={lease.exporter}")
    if lease.selector:
        parts.append(f"selector={lease.selector}")
    if lease.effective_end_time:
        parts.append(f"expires {lease.effective_end_time.strftime('%Y-%m-%d %H:%M')}")
    elif lease.effective_begin_time and lease.duration:
        end = lease.effective_begin_time + lease.duration
        parts.append(f"expires {end.strftime('%Y-%m-%d %H:%M')}")
    return ", ".join(parts) if parts else ""


async def _resolve_lease_from_active_async(config) -> str:
    lease_list = await config.list_leases(only_active=True)
    client_name = config.metadata.name
    leases = [lease for lease in lease_list.leases if lease.client == client_name]

    if not leases:
        raise click.UsageError(
            "no active leases found. Use --selector/-l or --name/-n to create one, "
            "or create a lease with 'jmp create lease'."
        )

    if len(leases) == 1:
        return leases[0].name

    if sys.stdin.isatty():
        click.echo("Multiple active leases found:\n")
        for i, lease in enumerate(leases, 1):
            info = _format_lease_display(lease)
            click.echo(f"  {i}) {lease.name}")
            if info:
                click.echo(f"     {info}")
        click.echo()
        chosen = click.prompt(
            "Select a lease [1-{}]".format(len(leases)),
            type=click.IntRange(1, len(leases)),
        )
        return leases[chosen - 1].name

    lease_summaries = []
    for lease in leases:
        info = _format_lease_display(lease)
        summary = f"{lease.name} ({info})" if info else lease.name
        lease_summaries.append(summary)
    raise click.UsageError(
        "multiple active leases found:\n  "
        + "\n  ".join(lease_summaries)
        + "\nUse --lease to specify one, or run interactively to select."
    )


async def _shell_direct_async(
    tls_grpc_address: str,
    tls_grpc_insecure: bool,
    exporter_logs: bool,
    command: tuple,
    passphrase: str | None = None,
):
    """Run shell with direct connection to exporter (no controller)."""
    exit_code = 0
    cancelled_exc_class = get_cancelled_exc_class()

    async with anyio.from_thread.BlockingPortal() as portal:
        lease = DirectLease(
            address=tls_grpc_address,
            portal=portal,
            allow=[],
            unsafe=True,
            tls_config=TLSConfigV1Alpha1(),
            grpc_options={},
            insecure=tls_grpc_insecure,
            passphrase=passphrase,
        )
        # Minimal config for _run_shell_with_lease_async (allow/unsafe/use_profiles)
        config = SimpleNamespace(
            drivers=SimpleNamespace(allow=lease.allow, unsafe=lease.unsafe),
            shell=SimpleNamespace(use_profiles=False),
        )

        async with create_task_group() as tg:
            tg.start_soon(signal_handler, tg.cancel_scope)
            try:
                exit_code = await _run_shell_with_lease_async(lease, exporter_logs, config, command, tg.cancel_scope)
            except grpc.aio.AioRpcError as e:
                if e.code() == grpc.StatusCode.UNAUTHENTICATED:
                    raise click.ClickException("Authentication failed: invalid or missing passphrase") from None
                raise
            except cancelled_exc_class:
                exit_code = 2
            finally:
                if not tg.cancel_scope.cancel_called:
                    tg.cancel_scope.cancel()

    return exit_code


@click.command("shell")
@opt_config(allow_missing=True)
@click.argument("command", nargs=-1)
# client specific
# TODO: warn if these are specified with exporter config
@click.option("--lease", "lease_name")
@opt_selector
@opt_exporter_name
@opt_duration_partial(default=timedelta(minutes=30), show_default="00:30:00")
@click.option("--exporter-logs", is_flag=True, help="Enable exporter log streaming")
@opt_allow_disabled
@opt_acquisition_timeout()
@opt_retry_timeout()
@opt_dial_timeout()
# direct connection (no controller)
@click.option(
    "--tls-grpc",
    "tls_grpc_address",
    metavar="HOST:PORT",
    help="Connect directly to an exporter at this address (no controller). E.g. exporter.host.name:1234.",
)
@click.option(
    "--tls-grpc-insecure",
    "tls_grpc_insecure",
    is_flag=True,
    help="With --tls-grpc, connect without TLS (insecure, for development only).",
)
@click.option(
    "--passphrase",
    "passphrase",
    default=None,
    help="Passphrase for authenticating with a standalone exporter (--tls-grpc).",
)
# end client specific
@handle_exceptions_with_reauthentication(relogin_client)
def shell(
    config,
    command: tuple[str, ...],
    lease_name,
    selector,
    exporter_name,
    duration,
    exporter_logs,
    allow_disabled,
    acquisition_timeout,
    retry_timeout,
    dial_timeout,
    tls_grpc_address,
    tls_grpc_insecure,
    passphrase,
):
    """
    Spawns a shell (or custom command) connecting to a local or remote exporter

    COMMAND is the custom command to run instead of shell.

    Example:

    .. code-block:: bash

        $ jmp shell --exporter foo -- python bar.py
        $ jmp shell --tls-grpc exporter.host.name:1234
    """

    if tls_grpc_address is not None:
        exit_code = anyio.run(
            _shell_direct_async,
            tls_grpc_address,
            tls_grpc_insecure,
            exporter_logs,
            command,
            passphrase,
        )
        sys.exit(exit_code)

    if config is None or isinstance(config, tuple):
        raise click.UsageError(
            "Specify one of: --client / --client-config, --exporter / --exporter-config, or --tls-grpc HOST:PORT"
        )

    match config:
        case ClientConfigV1Alpha1():
            has_existing_lease = bool(lease_name or os.environ.get(JMP_LEASE))
            if not selector and not exporter_name and not has_existing_lease:
                lease_name = anyio.run(_resolve_lease_from_active_async, config)
            exit_code = anyio.run(
                _shell_with_signal_handling,
                config,
                selector,
                exporter_name,
                lease_name,
                duration,
                exporter_logs,
                command,
                acquisition_timeout,
                retry_timeout,
                dial_timeout,
                allow_disabled,
            )
            sys.exit(exit_code)

        case ExporterConfigV1Alpha1():
            with config.serve_unix() as path:
                # SAFETY: the exporter config is local thus considered trusted
                launch_shell(
                    path,
                    "local",
                    allow=[],
                    unsafe=True,
                    use_profiles=False,
                    command=command,
                    motd=config.motd,
                )
