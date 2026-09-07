import inspect
import logging
import math
import os
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Self

import anyio
import grpc
from anyio import (
    AsyncContextManagerMixin,
    CancelScope,
    Event,
    connect_unix,
    create_memory_object_stream,
    create_task_group,
    move_on_after,
    sleep,
)
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from google.protobuf import empty_pb2
from jumpstarter_protocol import (
    jumpstarter_pb2,
    jumpstarter_pb2_grpc,
    telemetry_pb2_grpc,
)

from jumpstarter.common import ExporterStatus, Metadata, TemporarySocket
from jumpstarter.common.streams import connect_router_stream
from jumpstarter.config.env import JMP_GRPC_INSECURE, JUMPSTARTER_GRPC_INSECURE
from jumpstarter.config.tls import TLSConfigV1Alpha1
from jumpstarter.exporter.hooks import HookExecutor
from jumpstarter.exporter.lease_context import LeaseContext
from jumpstarter.exporter.session import Session
from jumpstarter.exporter.telemetry import TelemetryLogHandler
from jumpstarter.exporter.token_refresh import (
    DEFAULT_TOKEN_REFRESH_FRACTION,
    DEFAULT_TOKEN_REFRESH_LEAD_TIME,
    calculate_token_refresh_sleep,
)
from jumpstarter.logging import clear_log_context, set_log_context

if TYPE_CHECKING:
    from jumpstarter.driver import Driver

logger = logging.getLogger(__name__)

# gRPC retry configuration
# Worst case ~18 min (21 attempts × 30s timeout + backoff sum ~8 min).
# The exporter has nothing useful to do while the controller is unreachable —
# even a restart just fails at _register_with_controller (no retry).
_TRANSIENT_GRPC_CODES = frozenset({grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED})
_RPC_MAX_RETRIES = 20
_RPC_BACKOFF_BASE = 1.0
_RPC_BACKOFF_CAP = 30.0
_RPC_TIMEOUT = 30

# How long after a lease ends to wait for handle_lease's LeaseFinished before
# warning. The loop waits for LeaseFinished indefinitely (it is a real
# happens-before edge, not a timer), so this only logs — it never releases the
# slot — keeping a wedged teardown diagnosable without reintroducing a timeout.
_LEASE_FINISHED_WATCHDOG = 30.0

# Status codes indicating old controller without exporter auth on ReleaseLease
_RELEASE_LEASE_UNSUPPORTED_CODES = frozenset(
    {
        grpc.StatusCode.PERMISSION_DENIED,
        grpc.StatusCode.INVALID_ARGUMENT,
        grpc.StatusCode.UNAUTHENTICATED,
        grpc.StatusCode.UNIMPLEMENTED,
    }
)

_SEVERITY_MAP = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


def _severity_to_level(severity: str) -> int:
    """Map a JEP-0013 severity string to a Python logging level."""
    key = severity.lower() if severity else ""
    level = _SEVERITY_MAP.get(key)
    if level is None:
        if key:
            import structlog

            structlog.get_logger(__name__).warning(
                "Unrecognized min_severity value, defaulting to info",
                value=severity,
                accepted=list(_SEVERITY_MAP),
            )
        return logging.INFO
    return level


# Sidecar launcher socket injected by the ExporterSet QEMU provisioner.
_LAUNCHER_SOCKET_ENV = "JUMPSTARTER_LAUNCHER_SOCKET"
_DEFAULT_JMP_EXEC = "/shared/jumpstarter-exec"


def shutdown_runtime_sidecar(
    *,
    socket_path: str | None = None,
    binary: str | None = None,
    timeout: float = 10.0,
) -> bool:
    """Ask jumpstarter-exec serve (runtime container PID 1) to exit cleanly.

    With native sidecars (KEP-753), kubelet already terminates ``target-runtime``
    after the exporter (main) container exits, so Pod completion does not depend
    on this call. Invoking ``jumpstarter-exec shutdown`` is best-effort: it
    SIGTERMs in-flight Exec children (e.g. QEMU) for a faster, cleaner teardown
    than waiting for the Pod termination grace period.

    Returns True if a shutdown was attempted successfully, False if no launcher
    socket is configured (non-sidecar / InPlaceReuse hosts) or shutdown failed.

    Callers on the async event loop must offload this via
    ``await anyio.to_thread.run_sync(shutdown_runtime_sidecar)``.
    """
    import os
    import subprocess
    from pathlib import Path

    sock = socket_path or os.environ.get(_LAUNCHER_SOCKET_ENV)
    if not sock:
        return False

    exec_bin = Path(binary) if binary else Path(_DEFAULT_JMP_EXEC)
    if not exec_bin.is_file():
        # Fall back to the binary next to the socket (same shared volume).
        candidate = Path(sock).parent / "jumpstarter-exec"
        if candidate.is_file():
            exec_bin = candidate
        else:
            logger.warning(
                "jumpstarter-exec binary not found at %s or %s; cannot shut down runtime",
                _DEFAULT_JMP_EXEC,
                candidate,
            )
            return False

    cmd = [str(exec_bin), "shutdown", "--socket", sock]
    logger.info("Shutting down runtime sidecar via %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=timeout)
    except OSError as e:
        # FileNotFoundError, PermissionError, and other pre-exec failures.
        logger.warning("jumpstarter-exec not executable at %s: %s", exec_bin, e)
        return False
    except subprocess.TimeoutExpired:
        logger.warning("jumpstarter-exec shutdown timed out after %ss", timeout)
        return False

    if result.returncode != 0:
        logger.warning(
            "jumpstarter-exec shutdown exited %s: stdout=%r stderr=%r",
            result.returncode,
            result.stdout,
            result.stderr,
        )
        return False

    logger.info("Runtime sidecar shutdown acknowledged")
    return True


class LeaseState(Enum):
    IDLE = "idle"
    LEASED = "leased"


@dataclass
class LeaseFinished:
    """Control-plane message: a handle_lease task has fully torn down its lease.

    Sent by handle_lease's finally after session_for_lease has exited (gRPC
    graceful stop included), so receiving it means the transport is gone. The
    control-plane loop is the sole writer of _lease_context; handle_lease hands
    the slot back with this message instead of clearing it from its own task.
    """

    lease_ctx: LeaseContext


async def _standalone_shutdown_waiter():
    """Wait forever; used so serve_standalone_tcp can be cancelled by stop()."""
    await anyio.sleep_forever()


@dataclass(kw_only=True)
class Exporter(AsyncContextManagerMixin, Metadata):
    """Represents a Jumpstarter Exporter runtime instance.

    Inherits from Metadata, which provides:
        uuid: Unique identifier for the exporter instance (UUID4)
        labels: Key-value labels for exporter identification and selector matching
    """

    # Public Configuration Fields

    exporter_name: str = "unknown"

    channel_factory: Callable[[], Awaitable[grpc.aio.Channel]]
    """Factory function for creating gRPC channels to communicate with the controller.

    Called multiple times throughout the exporter lifecycle to establish connections.
    The factory should handle authentication, credentials, and channel configuration.
    Used when creating controller stubs, unregistering, and establishing streams.
    """

    device_factory: Callable[[], "Driver"]
    """Factory function for creating Driver instances representing the hardware/devices.

    Called when creating Sessions to provide access to the underlying device.
    The Driver can contain child drivers in a composite pattern, representing
    the full device tree being exported. Typically created from ExporterConfigV1Alpha1.
    """

    tls: TLSConfigV1Alpha1 = field(default_factory=TLSConfigV1Alpha1)
    """TLS/SSL configuration for secure communication with router and controller.

    Contains certificate authority (ca) and insecure flag for certificate verification.
    Passed to connect_router_stream() when handling client connections.
    Default creates empty config with ca="" and insecure=False.
    """

    grpc_options: dict[str, str] = field(default_factory=dict)
    """Custom gRPC channel options that override or supplement default settings.

    Merged with defaults (round_robin load balancing, keepalive settings, etc.).
    Configured via YAML as grpcOptions in exporter config.
    Passed to connect_router_stream() for client connections.
    """

    motd: str | None = field(default=None)
    """Message of the day shown to clients when they enter a shell on this exporter.

    Configured via YAML as motd in exporter config and returned to clients in GetReport.
    """

    hook_executor: HookExecutor | None = field(default=None)
    """Optional executor for lifecycle hooks (before-lease and after-lease).

    When configured, runs custom scripts at key points in the lease lifecycle:
    - before-lease: Runs when transitioning to leased state (setup, validation)
    - after-lease: Runs when transitioning from leased state (cleanup, reset)
    Created when hooks.before_lease or hooks.after_lease are defined in config.
    """

    exit_on_lease_end: bool = field(default=False)
    """When True, the exporter exits after serving one lease.

    Triggers the existing _stop_requested mechanism after lease cleanup.
    """

    token: str = field(default="")
    """Bearer token used to authenticate with the telemetry service.

    Set from ExporterConfigV1Alpha1.token so PushLogs calls can include
    the exporter's JWT as an Authorization header.
    """

    on_token_rotated: Callable[[str], Awaitable[None] | None] | None = field(default=None)
    """Optional callback invoked when the authentication token is rotated.

    Passed the new token string. Can be async or sync.
    """

    token_refresh_lead_time: float = DEFAULT_TOKEN_REFRESH_LEAD_TIME
    """Maximum lead time before expiry to attempt token refresh (default: 7 days)."""

    token_refresh_fraction: float = DEFAULT_TOKEN_REFRESH_FRACTION
    """Fraction of total token lifetime at which to trigger refresh (default: 0.2)."""

    # Internal State Fields

    _registered: bool = field(init=False, default=False)
    """Tracks whether exporter has successfully registered with the controller.

    Set to True after successful registration. Used to determine if unregistration
    is needed during cleanup.
    """

    _unregister: bool = field(init=False, default=False)
    """Internal flag indicating whether to actively unregister during shutdown.

    Set when stop(should_unregister=True) is called. When False, relies on
    heartbeat timeout for implicit unregistration.
    """

    _stop_requested: bool = field(init=False, default=False)
    """Internal flag indicating a graceful stop has been requested.

    Set to True when stop(wait_for_lease_exit=True) is called. The exporter
    waits for the current lease to exit before stopping.
    """

    _deferred_unregister: bool = field(init=False, default=True)
    """Preserved should_unregister value for deferred stop.

    When stop(wait_for_lease_exit=True) is called, the should_unregister
    preference is stored here and applied when the deferred stop executes.
    """

    _started: bool = field(init=False, default=False)
    """Internal flag tracking whether the exporter has started serving.

    Set to True when the first lease is assigned. Used to determine immediate
    vs graceful stop behavior.
    """

    _tg: TaskGroup | None = field(init=False, default=None)
    """Reference to the anyio TaskGroup managing concurrent tasks.

    Manages streams and connection handling tasks. Used to cancel all tasks
    when stopping. Set during serve() and cleared when done.
    """

    _exporter_status: ExporterStatus = field(init=False, default=ExporterStatus.OFFLINE)
    """Current status of the exporter.

    Updated via _update_status() and reported to controller and session.
    Possible values: OFFLINE, AVAILABLE, BEFORE_LEASE_HOOK, LEASE_READY,
    AFTER_LEASE_HOOK, BEFORE_LEASE_HOOK_FAILED, AFTER_LEASE_HOOK_FAILED.
    """

    _exit_code: int | None = field(init=False, default=None)
    """Exit code to use when the exporter shuts down.

    When set to a non-zero value, the exporter should terminate permanently
    (not restart). This is used by hooks with on_failure='exit' to signal
    that the exporter should shut down and not be restarted by the CLI.
    """

    _standalone: bool = field(init=False, default=False)
    """When True, exporter runs without a controller (TCP listener only).

    _report_status and __aexit__ skip controller calls when _standalone is True.
    """

    _last_completed_lease: str | None = field(init=False, default=None)
    """Name of the most recently completed lease. Set by the control-plane loop
    when it processes LeaseFinished; suppresses trailing leased=true ticks for a
    lease that has already torn down."""

    _pending_lease_status: jumpstarter_pb2.StatusResponse | None = field(init=False, default=None)
    """Stashed status from a lease reassignment, replayed by the control-plane
    loop when it processes LeaseFinished (after the old slot is released) so the
    new lease can be acquired."""

    _control_tx: "MemoryObjectSendStream[jumpstarter_pb2.StatusResponse | LeaseFinished] | None" = field(
        init=False, default=None
    )
    """Send side of the control-plane channel. handle_lease posts LeaseFinished
    here to hand its slot back to the loop, and the loop replays
    _pending_lease_status through it after a lease transition."""
    _lease_context: LeaseContext | None = field(init=False, default=None)
    """Encapsulates all resources associated with the current lease.

    Contains the session, socket path, and synchronization event needed
    throughout the lease lifecycle. This replaces the previous individual
    _current_session, _session_socket_path, and _before_lease_hook fields.

    Lifecycle:
    1. Created in serve() when a lease is assigned (session/socket initially None)
    2. Populated in handle_lease() when the session is created
    3. Accessed by hook execution methods and status reporting
    4. Cleared when lease ends or changes

    The session and socket are managed by the context manager in handle_lease(),
    ensuring proper cleanup when the lease ends. The LeaseScope itself is just
    a reference holder and doesn't manage resource lifecycles directly.
    """

    _release_lease_unsupported: bool = field(init=False, default=False)
    """Caches whether the controller doesn't support exporter auth on ReleaseLease.

    When True, _request_lease_release skips the ReleaseLease RPC and goes straight
    to the deprecated ReportStatus(release_lease=true) fallback. Avoids rediscovering
    the auth rejection on every lease cycle.
    TODO: Remove this field when all controllers support ReleaseLease for exporters.
    """

    _telemetry_handler: "TelemetryLogHandler | None" = field(init=False, default=None)
    """Optional telemetry log handler that pushes log entries to jumpstarter-telemetry.

    Created after successful registration when GetServiceEndpoints returns a
    telemetry endpoint. None when telemetry is not configured or not available.
    """

    _telemetry_channel: grpc.aio.Channel | None = field(init=False, default=None)
    """gRPC channel to the telemetry service. Closed on exporter shutdown."""

    _status_drain_active: bool = field(init=False, default=False)
    """True only while serve()'s task group is running and the drain task is active.

    When True, _report_status enqueues status updates for background processing.
    When False (registration, unregistration), _report_status awaits directly.
    """

    _pending_status_request: jumpstarter_pb2.ReportStatusRequest | None = field(init=False, default=None)
    """Latest status update pending delivery by the background drain task.

    "Latest wins" semantics: if multiple _report_status calls happen while the
    drain task is busy retrying, only the most recent one is sent.
    """

    _status_rpc_event: Event = field(init=False, default_factory=Event)
    """Signals the drain task that a new status update is pending."""

    @property
    def _lease_state(self) -> LeaseState:
        return LeaseState.LEASED if self._lease_context is not None else LeaseState.IDLE

    def stop(self, wait_for_lease_exit=False, should_unregister=False, exit_code: int | None = None):
        """Signal the exporter to stop.

        Args:
            wait_for_lease_exit (bool): If True, wait for the current lease to exit before stopping.
            should_unregister (bool): If True, unregister from controller. Otherwise rely on heartbeat.
            exit_code (int | None): If set, the exporter will exit with this code (non-zero means no restart).
        """
        # Set exit code if provided
        if exit_code is not None:
            self._exit_code = exit_code

        # Stop immediately if not started yet or if immediate stop is requested
        if (not self._started or not wait_for_lease_exit) and self._tg is not None:
            if should_unregister:
                logger.info("Stopping exporter immediately, unregistering from controller")
            else:
                logger.info("Stopping exporter immediately, will not unregister from controller")
            self._unregister = should_unregister
            # Cancel any ongoing tasks
            self._tg.cancel_scope.cancel()
        elif not self._stop_requested:
            self._stop_requested = True
            self._deferred_unregister = should_unregister
            logger.info("Exporter marked for stop upon lease exit")

    @property
    def exit_code(self) -> int | None:
        """Get the exit code for the exporter.

        Returns:
            The exit code if set, or None if the exporter should restart.
        """
        return self._exit_code

    @asynccontextmanager
    async def _controller_stub(self) -> AsyncGenerator[jumpstarter_pb2_grpc.ControllerServiceStub, None]:
        """Create a controller service stub as a context manager.

        Yields:
            ControllerServiceStub connected to the controller

        The underlying channel is automatically closed when the context exits.
        """
        channel = await self.channel_factory()
        try:
            yield jumpstarter_pb2_grpc.ControllerServiceStub(channel)
        finally:
            await channel.close()

    async def _retry_stream(
        self,
        stream_name: str,
        stream_factory: Callable[[jumpstarter_pb2_grpc.ControllerServiceStub], AsyncGenerator],
        send_tx,
        retries: int = 5,
        backoff: float = 1.0,  # Reduced from 3.0 for faster recovery from transient errors
    ):
        """Generic retry wrapper for gRPC streaming calls.

        Args:
            stream_name: Name of the stream for logging purposes
            stream_factory: Function that takes a controller stub and returns an async generator
            send_tx: Transmission channel to send stream items to
            retries: Maximum number of retry attempts
            backoff: Seconds to wait between retries
        """
        retries_left = retries
        while True:
            received_data = False
            try:
                async with self._controller_stub() as controller:
                    logger.debug("%s stream connected to controller", stream_name)
                    async for item in stream_factory(controller):
                        received_data = True
                        logger.debug("%s stream received item", stream_name)
                        await send_tx.send(item)
            except Exception as e:
                if received_data:
                    logger.debug("%s stream retry counter reset after receiving data", stream_name)
                    retries_left = retries
                if retries_left > 0:
                    retries_left -= 1
                    # Check for common transient errors that warrant faster retry
                    error_str = str(e)
                    is_transient = "Stream removed" in error_str or "UNAVAILABLE" in error_str
                    retry_delay = 0.5 if is_transient else backoff
                    logger.info(
                        "%s stream interrupted, restarting in %ss, %s retries left: %s",
                        stream_name,
                        retry_delay,
                        retries_left,
                        e,
                    )
                    await sleep(retry_delay)
                else:
                    raise
            else:
                retries_left = retries

    def _listen_stream_factory(
        self, lease_name: str
    ) -> Callable[[jumpstarter_pb2_grpc.ControllerServiceStub], AsyncGenerator[jumpstarter_pb2.ListenResponse, None]]:
        """Create a stream factory for listening to connection requests."""

        def factory(
            ctrl: jumpstarter_pb2_grpc.ControllerServiceStub,
        ) -> AsyncGenerator[jumpstarter_pb2.ListenResponse, None]:
            return ctrl.Listen(jumpstarter_pb2.ListenRequest(lease_name=lease_name))

        return factory

    def _status_stream_factory(
        self,
    ) -> Callable[[jumpstarter_pb2_grpc.ControllerServiceStub], AsyncGenerator[jumpstarter_pb2.StatusResponse, None]]:
        """Create a stream factory for status updates."""

        def factory(
            ctrl: jumpstarter_pb2_grpc.ControllerServiceStub,
        ) -> AsyncGenerator[jumpstarter_pb2.StatusResponse, None]:
            return ctrl.Status(jumpstarter_pb2.StatusRequest())

        return factory

    async def _register_with_controller(self, local_channel: grpc.aio.Channel):
        """Register the exporter with the controller.

        Args:
            local_channel: The local Unix socket channel to get device reports from
        """
        # Get device reports from the local session
        exporter_stub = jumpstarter_pb2_grpc.ExporterServiceStub(local_channel)
        response: jumpstarter_pb2.GetReportResponse = await exporter_stub.GetReport(empty_pb2.Empty())

        # Register with the REMOTE controller (not the local session)
        logger.info("Registering exporter with controller")
        async with self._controller_stub() as controller:
            await controller.Register(
                jumpstarter_pb2.RegisterRequest(
                    labels=self.labels,
                    reports=response.reports,
                )
            )
        # Mark exporter as registered internally
        self._registered = True
        # Only report AVAILABLE status during initial registration (no lease context)
        # During per-lease registration, status is managed by serve() to avoid
        # overwriting LEASE_READY with AVAILABLE
        if self._lease_context is None:
            await self._report_status(ExporterStatus.AVAILABLE, "Exporter registered and available")

        # Discover optional telemetry service endpoint.
        await self._setup_telemetry()

    async def _setup_telemetry(self) -> None:
        """Discover and connect to the optional telemetry service.

        Calls GetServiceEndpoints on the controller. When a telemetry endpoint is
        returned, a gRPC channel is created and TelemetryLogHandler is attached
        to the root Python logger. Safe to call multiple times; subsequent calls
        are no-ops if a handler is already configured.
        """
        if self._telemetry_handler is not None:
            return

        try:
            async with self._controller_stub() as controller:
                resp = await controller.GetServiceEndpoints(
                    jumpstarter_pb2.GetServiceEndpointsRequest(),
                    timeout=_RPC_TIMEOUT,
                )
        except grpc.aio.AioRpcError as e:
            # Older controllers that don't support this RPC return UNIMPLEMENTED.
            # Any other error is also non-fatal — telemetry is best-effort.
            logger.debug("GetServiceEndpoints unavailable: %s", e.code())
            return

        if not resp.telemetry_endpoints:
            logger.debug("No telemetry endpoint configured, skipping telemetry setup")
            return

        ep = resp.telemetry_endpoints[0]
        logger.info("Connecting to telemetry service at %s (min_severity=%s)", ep.endpoint, ep.min_severity)

        grpc_insecure = (
            self.tls.insecure or os.getenv(JMP_GRPC_INSECURE) == "1" or os.getenv(JUMPSTARTER_GRPC_INSECURE) == "1"
        )

        if ep.certificate:
            # Use CA certificate provided by the controller for the telemetry endpoint.
            self._telemetry_channel = grpc.aio.secure_channel(
                ep.endpoint,
                grpc.ssl_channel_credentials(root_certificates=ep.certificate.encode()),
            )
        elif grpc_insecure:
            # Development/testing mode: plaintext gRPC, no TLS at all.
            self._telemetry_channel = grpc.aio.insecure_channel(ep.endpoint)
        else:
            # Production: TLS with system CA pool.
            self._telemetry_channel = grpc.aio.secure_channel(ep.endpoint, grpc.ssl_channel_credentials())
        stub = telemetry_pb2_grpc.TelemetryServiceStub(self._telemetry_channel)
        handler = TelemetryLogHandler(stub, namespace=getattr(self, "namespace", "") or "", token=self.token)
        handler.setLevel(_severity_to_level(ep.min_severity))
        logging.getLogger().addHandler(handler)
        self._telemetry_handler = handler
        logger.info("Telemetry log handler attached")

    async def rotate_token(self) -> str:
        """Request a newly signed authentication token from the controller.

        Updates self.token, updates the telemetry handler if attached, and invokes
        the on_token_rotated callback if configured.

        Returns:
            The newly rotated token string.
        """
        async with self._controller_stub() as controller:
            response: jumpstarter_pb2.RotateTokenResponse = await controller.RotateToken(
                jumpstarter_pb2.RotateTokenRequest(),
                timeout=_RPC_TIMEOUT,
            )

        new_token = response.token
        self.token = new_token

        if self._telemetry_handler is not None:
            self._telemetry_handler.token = new_token

        if self.on_token_rotated is not None:
            try:
                res = self.on_token_rotated(new_token)
                if inspect.isawaitable(res):
                    await res
            except Exception as e:
                logger.error("Error in on_token_rotated callback: %s", e, exc_info=True)

        return new_token

    async def _token_refresh_loop(self) -> None:
        """Monitor token expiration and request a new token when it is about to expire."""
        if self._standalone or not self.token:
            return

        while True:
            sleep_duration = calculate_token_refresh_sleep(
                self.token,
                lead_time=self.token_refresh_lead_time,
                fraction=self.token_refresh_fraction,
            )
            if sleep_duration is None:
                logger.debug("Token does not have an expiry claim; automatic refresh disabled")
                return

            if sleep_duration > 0:
                logger.debug("Token refresh scheduled in %.1f seconds", sleep_duration)
                await sleep(sleep_duration)

            # Token is about to expire (or already expired) - request a new token with retries
            attempt = 0
            while True:
                try:
                    logger.info("Token is about to expire; requesting new token from controller")
                    await self.rotate_token()
                    logger.info("Successfully refreshed exporter authentication token")
                    break
                except grpc.aio.AioRpcError as e:
                    if e.code() == grpc.StatusCode.UNIMPLEMENTED:
                        logger.warning(
                            "Controller does not support token rotation (UNIMPLEMENTED); disabling automatic refresh"
                        )
                        return
                    elif e.code() in (grpc.StatusCode.UNAUTHENTICATED, grpc.StatusCode.PERMISSION_DENIED):
                        logger.error("Failed to rotate token (%s): %s", e.code(), e.details())
                        return
                    else:
                        attempt += 1
                        backoff = min(_RPC_BACKOFF_BASE * (2 ** min(attempt - 1, 5)), _RPC_BACKOFF_CAP)
                        logger.warning(
                            "Transient failure rotating token (%s: %s); retrying in %.1fs (attempt %d)",
                            e.code(),
                            e.details(),
                            backoff,
                            attempt,
                        )
                        await sleep(backoff)
                except Exception as e:
                    attempt += 1
                    backoff = min(_RPC_BACKOFF_BASE * (2 ** min(attempt - 1, 5)), _RPC_BACKOFF_CAP)
                    logger.warning(
                        "Unexpected failure rotating token (%s); retrying in %.1fs (attempt %d)",
                        e,
                        backoff,
                        attempt,
                    )
                    await sleep(backoff)

    async def _retry_rpc(
        self,
        rpc_call: Callable,
        description: str,
        *,
        non_retryable_codes: frozenset[grpc.StatusCode] = frozenset(),
    ) -> tuple[bool, grpc.StatusCode | None]:
        """Retry a unary gRPC call with exponential backoff.

        Args:
            rpc_call: Async callable that takes a controller stub and performs the RPC
            description: Human-readable description for log messages
            non_retryable_codes: gRPC status codes that should cause immediate return
                without retry (e.g. UNIMPLEMENTED for unsupported RPCs)

        Returns:
            (True, None) on success.
            (False, status_code) on non-retryable error or retry exhaustion.
            (False, None) on non-gRPC exception.
        """
        for attempt in range(_RPC_MAX_RETRIES + 1):
            try:
                async with self._controller_stub() as controller:
                    await rpc_call(controller)
                return True, None
            except grpc.aio.AioRpcError as e:
                if e.code() in non_retryable_codes:
                    return False, e.code()
                if e.code() in _TRANSIENT_GRPC_CODES and attempt < _RPC_MAX_RETRIES:
                    backoff = min(_RPC_BACKOFF_BASE * (2**attempt), _RPC_BACKOFF_CAP)
                    logger.warning(
                        "Transient error %s (attempt %d/%d), retrying in %.1fs: %s",
                        description,
                        attempt + 1,
                        _RPC_MAX_RETRIES + 1,
                        backoff,
                        e,
                    )
                    await anyio.sleep(backoff)
                    continue
                logger.error("Failed to %s: %s", description, e)
                return False, e.code()
            except Exception as e:
                logger.error("Failed to %s: %s", description, e)
                return False, None

    async def _send_report_status_rpc(self, request: jumpstarter_pb2.ReportStatusRequest) -> bool:
        """Send ReportStatus RPC to the controller with retry on transient errors.

        Returns:
            True if the RPC succeeded, False if unsupported or retries exhausted
        """
        ok, code = await self._retry_rpc(
            lambda ctrl: ctrl.ReportStatus(request, timeout=_RPC_TIMEOUT),
            "report status",
            non_retryable_codes=frozenset({grpc.StatusCode.UNIMPLEMENTED}),
        )
        if not ok and code == grpc.StatusCode.UNIMPLEMENTED:
            # Legacy support: ReportStatus added Nov 2025 (commit b76f6c87).
            # All production controllers support it; safe to remove in future versions.
            logger.warning("ReportStatus not supported by controller, status updates will be skipped")
        return ok

    async def _drain_status_reports(self):
        """Background task that sends status updates to the controller.

        Runs during serve() to make _report_status non-blocking. Uses "latest wins"
        semantics: if multiple status updates arrive while retrying, only the most
        recent one is sent. This prevents blocking the client connection path when
        the controller is temporarily unreachable.
        """
        while True:
            await self._status_rpc_event.wait()
            self._status_rpc_event = Event()  # Reset for next update
            request = self._pending_status_request
            if request and await self._send_report_status_rpc(request):
                logger.info(
                    "Updated status to %s: %s",
                    ExporterStatus.from_proto(request.status),
                    request.message,
                )

    async def _report_status(self, status: ExporterStatus, message: str = ""):
        """Report the exporter status with the controller and session."""
        self._exporter_status = status

        # Update status in lease context (handles session update internally)
        # This ensures status is stored even before session is created
        if self._lease_context:
            self._lease_context.update_status(status, message)

        if self._standalone:
            logger.debug("Updated status to %s: %s (standalone, no controller)", status, message)
            return

        request = jumpstarter_pb2.ReportStatusRequest(
            status=status.to_proto(),
            message=message,
        )

        if self._status_drain_active:
            # During serve(): enqueue for background drain (non-blocking)
            self._pending_status_request = request
            self._status_rpc_event.set()
        else:
            # Outside serve() (registration, unregistration): send directly
            if await self._send_report_status_rpc(request):
                logger.info("Updated status to %s: %s", status, message)

    async def _send_compat_release(self, lease_name: str):
        """Release lease via ReportStatus with release_lease=true (DEPRECATED).

        Backward-compat fallback for controllers that don't support exporter auth
        on ReleaseLease. Uses non-AVAILABLE status to block new lease assignment
        (filterOutNotReadyExporters) until the final AVAILABLE status, preventing
        retry from releasing a newly-assigned lease.

        TODO: Remove when all controllers support ReleaseLease for exporters.
        """
        release_status = self._exporter_status
        if release_status == ExporterStatus.AVAILABLE:
            release_status = ExporterStatus.AFTER_LEASE_HOOK

        if await self._send_report_status_rpc(
            jumpstarter_pb2.ReportStatusRequest(
                status=release_status.to_proto(),
                message="Lease released (compat: ReportStatus with release_lease)",
                release_lease=True,
            )
        ):
            logger.info("Requested controller to release lease %s (compat path)", lease_name)

    async def _request_lease_release(self):
        """Request the controller to release the current lease.

        Called after the afterLease hook completes to ensure the lease is
        released even if the client disconnects unexpectedly. This moves
        the lease release responsibility from the client to the exporter.

        Tries the ReleaseLease RPC first (semantically correct, retry-safe).
        Falls back to ReportStatus(release_lease=true) for old controllers that
        don't support exporter auth on ReleaseLease (deprecated path).
        """
        if not self._lease_context or not self._lease_context.lease_name:
            logger.debug("No active lease to release")
            return

        # If the lease has already ended (controller sent leased=false, or a previous
        # call already released it), skip the release RPC.
        if self._lease_context.lease_ended.is_set():
            logger.debug("Lease already ended, skipping release request")
            return

        if self._standalone:
            self._lease_context.lease_ended.set()
            return

        lease_name = self._lease_context.lease_name

        if self._release_lease_unsupported:
            await self._send_compat_release(lease_name)
        else:
            ok, code = await self._retry_rpc(
                lambda ctrl: ctrl.ReleaseLease(
                    jumpstarter_pb2.ReleaseLeaseRequest(name=lease_name), timeout=_RPC_TIMEOUT
                ),
                "release lease",
                non_retryable_codes=_RELEASE_LEASE_UNSUPPORTED_CODES,
            )

            if ok:
                logger.info("Released lease %s via ReleaseLease RPC", lease_name)
            elif code in _RELEASE_LEASE_UNSUPPORTED_CODES:
                self._release_lease_unsupported = True
                logger.info(
                    "Controller doesn't support ReleaseLease for exporters (%s), "
                    "falling back to ReportStatus with release_lease",
                    code.name,
                )
                await self._send_compat_release(lease_name)
            else:
                logger.warning("Failed to release lease %s after retries, proceeding to AVAILABLE", lease_name)

        await self._report_status(ExporterStatus.AVAILABLE, "Exporter available after lease release")

        # Directly signal lease ended so handle_lease can exit.
        # The controller may not send another leased=False after our release request,
        # so we signal it ourselves as a fallback.
        if self._lease_context and not self._lease_context.lease_ended.is_set():
            self._lease_context.lease_ended.set()

    async def _unregister_with_controller(self):
        """Safely unregister from controller with timeout and error handling."""
        if not (self._registered and self._unregister):
            return

        logger.info("Unregistering exporter with controller")
        try:
            with move_on_after(10):  # 10 second timeout
                channel = await self.channel_factory()
                try:
                    controller = jumpstarter_pb2_grpc.ControllerServiceStub(channel)
                    await self._report_status(ExporterStatus.OFFLINE, "Exporter shutting down")
                    await controller.Unregister(
                        jumpstarter_pb2.UnregisterRequest(
                            reason="Exporter shutdown",
                        )
                    )
                    logger.info("Controller unregistration completed successfully")
                finally:
                    with CancelScope(shield=True):
                        await channel.close()
        except Exception as e:
            logger.error("Error during controller unregistration: %s", e, exc_info=True)

    @asynccontextmanager
    async def __asynccontextmanager__(self) -> AsyncGenerator[Self]:
        try:
            yield self
        finally:
            try:
                await self._unregister_with_controller()
            except Exception as e:
                logger.error("Error during exporter cleanup: %s", e, exc_info=True)
                # Don't re-raise to avoid masking the original exception

    async def _handle_client_conn(
        self, path: str, endpoint: str, token: str, tls_config: TLSConfigV1Alpha1, grpc_options: dict[str, Any] | None
    ) -> None:
        """Handle a single client connection by proxying between session and router.

        This method establishes a connection from the local session Unix socket to the
        router endpoint, creating a bidirectional proxy that allows the client to
        communicate with the device through the router infrastructure.

        Args:
            path: Unix socket path where the session is serving
            endpoint: Router endpoint URL to connect to
            token: Authentication token for the router connection
            tls_config: TLS configuration for secure router communication
            grpc_options: Optional gRPC channel options for the router connection

        Note:
            This is a private method spawned as a concurrent task by handle_lease_conn()
            for each incoming connection request. It runs until the client disconnects
            or an error occurs.
        """
        try:
            logger.debug("Connecting to session socket at %s", path)
            async with await connect_unix(path) as stream:
                logger.debug("Connected to session, bridging to router at %s", endpoint)
                async with connect_router_stream(endpoint, token, stream, tls_config, grpc_options):
                    logger.debug("Router stream established, forwarding traffic")
        except Exception as e:
            logger.warning("Failed to handle client connection: %s", e)

    async def _handle_end_session(self, lease_context: LeaseContext) -> None:
        """Handle EndSession requests from client.

        Waits for the end_session_requested event, runs the afterLease hook,
        and signals after_lease_hook_done when complete. This allows clients
        to receive afterLease hook logs before the connection is closed.

        Args:
            lease_context: The LeaseContext for the current lease.
        """
        logger.debug("_handle_end_session task started, waiting for end_session_requested or lease_ended event")
        # Wait for EndSession or lease end, whichever happens first
        async with create_task_group() as wait_tg:

            async def _wait_end_session():
                await lease_context.end_session_requested.wait()
                wait_tg.cancel_scope.cancel()

            async def _wait_lease_end():
                await lease_context.lease_ended.wait()
                wait_tg.cancel_scope.cancel()

            wait_tg.start_soon(_wait_end_session)
            wait_tg.start_soon(_wait_lease_end)

        # If lease ended without EndSession, exit cleanly (handle_lease finally block handles cleanup)
        if lease_context.lease_ended.is_set() and not lease_context.end_session_requested.is_set():
            logger.debug("Lease ended without EndSession; exiting EndSession handler")
            return

        logger.debug("end_session_requested event received")
        logger.info("EndSession requested, running afterLease hook")

        try:
            # Check if hook already started (via lease state transition)
            if lease_context.after_lease_hook_started.is_set():
                logger.debug("afterLease hook already started, waiting for completion")
                await lease_context.after_lease_hook_done.wait()
                return

            # Mark hook as started to prevent duplicate execution
            logger.debug("Marking afterLease hook as started")
            lease_context.after_lease_hook_started.set()

            if self.hook_executor and lease_context.has_client() and not lease_context.skip_after_lease_hook:
                logger.debug("Calling run_after_lease_hook")
                with CancelScope(shield=True):
                    await self.hook_executor.run_after_lease_hook(
                        lease_context,
                        self._report_status,
                        self.stop,
                        self._request_lease_release,
                    )
                logger.info("afterLease hook completed via EndSession")
            else:
                if lease_context.skip_after_lease_hook:
                    logger.info("Skipping afterLease hook: beforeLease hook failed")
                else:
                    logger.debug("No afterLease hook configured or no client, transitioning to AVAILABLE")
                await self._report_status(ExporterStatus.AVAILABLE, "Available for new lease")
        except Exception as e:
            logger.error("Error running afterLease hook via EndSession: %s", e)
        finally:
            # Signal that the hook is done (whether it ran or not)
            lease_context.after_lease_hook_done.set()

    @asynccontextmanager
    async def session(self):
        """Create and manage an exporter Session context for initial registration.

        Yields:
            tuple[Session, str]: A tuple of (session, socket_path) for use in lease handling.
        """
        with Session(
            uuid=self.uuid,
            labels=self.labels,
            exporter_name=self.exporter_name,
            root_device=self.device_factory(),
            motd=self.motd,
        ) as session:
            # Create a Unix socket
            async with session.serve_unix_async() as path:
                # Create a gRPC channel to the controller via the socket
                async with grpc.aio.secure_channel(
                    f"unix://{path}", grpc.local_channel_credentials(grpc.LocalConnectionType.UDS)
                ) as channel:
                    # Register the exporter with the controller
                    await self._register_with_controller(channel)
                # Yield both session and path for creating LeaseScope
                yield session, path

    @asynccontextmanager
    async def session_for_lease(self):
        """Create and manage an exporter Session context with separate hook socket.

        This creates two Unix sockets:
        - Main socket: For client gRPC connections (LogStream, driver calls, etc.)
        - Hook socket: For hook subprocess j commands (isolated to prevent SSL corruption)

        The separation prevents SSL frame corruption that occurs when multiple gRPC
        connections share the same socket simultaneously.

        Note: Registration with the controller is handled once during serve() via
        self.session(). Per-lease sessions do not re-register to avoid spurious
        status updates that can tear down the session prematurely.

        Yields:
            tuple[Session, str, str]: A tuple of (session, main_socket_path, hook_socket_path)
        """
        logger.info("Creating new session for lease")
        with Session(
            uuid=self.uuid,
            labels=self.labels,
            exporter_name=self.exporter_name,
            root_device=self.device_factory(),
            motd=self.motd,
        ) as session:
            # Create dual Unix sockets - one for clients, one for hooks
            async with session.serve_unix_with_hook_socket_async() as (main_path, hook_path):
                logger.info("Session serving on main=%s, hook=%s", main_path, hook_path)
                yield session, main_path, hook_path
        logger.info("Session closed")

    async def _cleanup_after_lease(self, lease_scope: LeaseContext) -> None:
        """Run afterLease hook cleanup when handle_lease exits.

        This handles the finally-block logic: shielding from cancellation,
        running the afterLease hook if appropriate, and transitioning to AVAILABLE.
        """
        with CancelScope(shield=True):
            # Wait for beforeLease hook to complete before running afterLease.
            # When a lease ends during hook execution, the hook must finish
            # (subject to its configured timeout) before cleanup proceeds.
            # Safety timeout: prevent permanent deadlock if before_lease_hook
            # was never set due to a race (e.g. conn_tg cancelled early).
            # Use the configured hook timeout (+ margin) when available so we
            # never interrupt a legitimately-running beforeLease hook.
            safety_timeout = 15  # generous default for no-hook / unknown cases
            if self.hook_executor and self.hook_executor.config.before_lease:
                safety_timeout = self.hook_executor.config.before_lease.timeout + 30
            with move_on_after(safety_timeout) as timeout_scope:
                await lease_scope.before_lease_hook.wait()
            if timeout_scope.cancelled_caught:
                logger.warning("Timed out waiting for before_lease_hook; forcing it set to avoid deadlock")
                lease_scope.before_lease_hook.set()

            if not lease_scope.after_lease_hook_started.is_set():
                lease_scope.after_lease_hook_started.set()
                if (
                    self.hook_executor
                    and (lease_scope.has_client() or self._standalone)
                    and not lease_scope.skip_after_lease_hook
                ):
                    logger.info("Running afterLease hook on session close")
                    await self.hook_executor.run_after_lease_hook(
                        lease_scope,
                        self._report_status,
                        self.stop,
                        self._request_lease_release,
                    )
                else:
                    if lease_scope.skip_after_lease_hook:
                        if lease_scope.lease_ended.is_set():
                            logger.info("Skipping afterLease hook: lease ended before beforeLease hook ran")
                        else:
                            logger.info("Skipping afterLease hook: beforeLease hook failed")
                    if not self._stop_requested:
                        logger.debug("No afterLease hook or no client on session close, transitioning to AVAILABLE")
                        await self._report_status(ExporterStatus.AVAILABLE, "Available for new lease")
                    else:
                        logger.debug("Exporter is shutting down, skipping AVAILABLE status report")
                if not lease_scope.after_lease_hook_done.is_set():
                    lease_scope.after_lease_hook_done.set()
            else:
                logger.debug("Waiting for afterLease hook to complete before closing session")
                await lease_scope.after_lease_hook_done.wait()
                logger.debug("afterLease hook completed, closing session")

    async def _skip_stale_lease(self, lease_name: str, lease_scope: LeaseContext, context: str) -> bool:
        """Handle early bail for a stale lease whose lease_ended is already set.

        Sets the events that serve() is waiting on and reports AVAILABLE.
        Returns True if the lease was stale and the caller should return.
        """
        if not lease_scope.lease_ended.is_set():
            return False
        logger.info("Lease %s already ended (%s), skipping", lease_name, context)
        lease_scope.skip_after_lease_hook = True
        lease_scope.before_lease_hook.set()
        if not self._stop_requested:
            await self._report_status(ExporterStatus.AVAILABLE, "Available for new lease")
        lease_scope.after_lease_hook_done.set()
        return True

    async def handle_lease(self, lease_name: str, tg: TaskGroup, lease_scope: LeaseContext) -> None:  # noqa: C901
        """Handle all incoming client connections for a lease.

        This method orchestrates the complete lifecycle of managing connections during
        a lease period. It listens for connection requests and spawns individual
        tasks to handle each client connection.

        The method performs the following steps:
        1. Creates a session for the lease duration
        2. Populates the lease_scope with session and socket path
        3. Sets up a stream to listen for incoming connection requests
        4. Waits for the before-lease hook to complete (if configured)
        5. Spawns a new task for each incoming connection request

        Args:
            lease_name: Name of the lease to handle connections for
            tg: TaskGroup for spawning concurrent connection handler tasks
            lease_scope: LeaseScope with before_lease_hook event (session/socket set here)

        Note:
            This method runs for the entire duration of the lease and is spawned by
            the serve() method when a lease is assigned. It terminates when the lease
            ends or the exporter stops.
        """
        try:
            # Yield to let serve() process any immediately-following leased=False
            # status that's already in the buffer. Without this, handle_lease runs
            # before serve() gets a chance to set lease_ended (anyio's receive()
            # always checkpoints, even when data is buffered). Inside the try so
            # cancellation here still runs fallback cleanup.
            await anyio.sleep(0)

            # Fast path: if the lease is already ended (stale lease from backlog
            # when the exporter couldn't keep up with lease churn), skip session
            # creation and all connection handling entirely.
            if await self._skip_stale_lease(lease_name, lease_scope, "before session creation"):
                return

            logger.info("Listening for incoming connection requests on lease %s", lease_name)

            # Buffer Listen responses to avoid blocking when responses arrive before
            # process_connections starts iterating. This prevents a race condition where
            # the client dials immediately after lease acquisition but before the session is ready.
            listen_tx, listen_rx = create_memory_object_stream[jumpstarter_pb2.ListenResponse](max_buffer_size=10)
            try:
                # Create session for the lease duration and populate lease_scope
                # Uses dual sockets: main socket for clients, hook socket for j commands
                async with self.session_for_lease() as (session, main_path, hook_path):
                    # Populate the lease scope with session and socket paths
                    lease_scope.session = session
                    lease_scope.socket_path = main_path
                    lease_scope.hook_socket_path = hook_path  # Isolated socket for hook j commands
                    # Link session to lease context for EndSession RPC
                    session.lease_context = lease_scope
                    # Sync status from LeaseContext to Session (status may have been updated
                    # before session was created, e.g., BEFORE_LEASE_HOOK when hooks are configured)
                    session.update_status(lease_scope.current_status, lease_scope.status_message)
                    logger.debug("Session sockets: main=%s, hook=%s", main_path, hook_path)

                    # Check if lease ended during session creation - serve() often
                    # processes the buffered leased=False while session_for_lease is
                    # setting up sockets and gRPC servers.  Bailing here avoids the
                    # Listen stream, conn_tg, and _cleanup_after_lease overhead.
                    # The session context manager handles teardown on return.
                    if await self._skip_stale_lease(lease_name, lease_scope, "during session setup"):
                        return

                    # Accept connections immediately - driver calls will be gated internally
                    # until the beforeLease hook completes. This allows LogStream to work
                    # during hook execution for real-time log streaming.
                    logger.info("Accepting connections (driver calls gated until beforeLease hook completes)")

                    # Note: Status is managed by _report_status() which updates both LeaseContext
                    # and Session. The sync above handles the case where status was updated before
                    # session creation (e.g., BEFORE_LEASE_HOOK when hooks are configured).

                    # Start task to handle EndSession requests (runs afterLease hook when client signals done)
                    tg.start_soon(self._handle_end_session, lease_scope)

                    # Process client connections until lease ends
                    # The lease can end via:
                    # 1. listen_rx stream closing (controller stops sending)
                    # 2. lease_ended event being set (serve() detected lease status change)
                    # Type: request is jumpstarter_pb2.ListenResponse with router_endpoint and router_token fields
                    try:
                        async with create_task_group() as conn_tg:
                            # Start listening for connection requests with retry logic
                            # This is inside conn_tg so it gets cancelled when the lease ends
                            conn_tg.start_soon(
                                self._retry_stream,
                                "Listen",
                                self._listen_stream_factory(lease_name),
                                listen_tx,
                            )

                            async def wait_for_lease_end():
                                """Wait for lease_ended event and cancel the connection loop."""
                                await lease_scope.lease_ended.wait()
                                logger.info("Lease ended event received, stopping connection handling")
                                conn_tg.cancel_scope.cancel()

                            async def process_connections():
                                """Process incoming connection requests."""
                                # Wait for beforeLease hook to complete before routing connections.
                                # The Listen buffer holds early Dials; we process them after ready.
                                await lease_scope.before_lease_hook.wait()
                                logger.debug("Starting to process connection requests from Listen stream")
                                async for request in listen_rx:
                                    logger.info(
                                        "Handling new connection request on lease %s (router=%s)",
                                        lease_name,
                                        request.router_endpoint,
                                    )
                                    tg.start_soon(
                                        self._handle_client_conn,
                                        lease_scope.socket_path,
                                        request.router_endpoint,
                                        request.router_token,
                                        self.tls,
                                        self.grpc_options,
                                    )

                            conn_tg.start_soon(wait_for_lease_end)
                            conn_tg.start_soon(process_connections)

                            # Report LEASE_READY if no beforeLease hook is configured.
                            # This MUST happen after Listen stream is started so the
                            # controller can forward client Dial requests.
                            if not self.hook_executor:
                                await self._report_status(ExporterStatus.LEASE_READY, "Ready for commands")
                                lease_scope.before_lease_hook.set()
                    finally:
                        # Ensure before_lease_hook is set so _cleanup_after_lease never
                        # blocks forever.  When conn_tg is cancelled before the no-hook
                        # path reaches lease_scope.before_lease_hook.set(), this flag
                        # remains unset and _cleanup_after_lease (shielded) deadlocks.
                        # Only apply this fallback when NO hooks are configured - when
                        # hooks ARE configured, run_before_lease_hook's finally block
                        # sets the event after updating skip_after_lease_hook. Setting
                        # it here prematurely would race with that flag update.
                        if not self.hook_executor and not lease_scope.before_lease_hook.is_set():
                            lease_scope.before_lease_hook.set()
                        # Run afterLease hook before closing the session
                        # This ensures the socket is still available for driver calls within the hook
                        # Shield from cancellation so the hook can complete even during shutdown
                        await self._cleanup_after_lease(lease_scope)
            finally:
                with CancelScope(shield=True):
                    await listen_tx.aclose()
                    await listen_rx.aclose()
        finally:
            # Hand the slot back to the control-plane loop, the sole writer of
            # _lease_context. This task only sets events on its own LeaseContext
            # and posts one message; it never clears the slot or replays status
            # itself, so there is no second writer to race.
            with CancelScope(shield=True):
                if not lease_scope.before_lease_hook.is_set():
                    lease_scope.before_lease_hook.set()
                if not lease_scope.after_lease_hook_done.is_set():
                    lease_scope.after_lease_hook_done.set()
                # session_for_lease has fully exited by now (gRPC graceful stop
                # included), so LeaseFinished is the "transport is gone" signal
                # the loop waits for before releasing the slot. Sent on every
                # exit path: normal leased=false, reassignment, stale lease, and
                # cancellation — so the loop always gets its finalize trigger.
                # send_nowait, not send: this runs shielded during shutdown, and
                # a blocking send with the loop already gone would hang the task
                # group forever. The channel is unbounded, so this never blocks.
                if self._control_tx is not None:
                    try:
                        self._control_tx.send_nowait(LeaseFinished(lease_scope))
                    except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                        logger.debug(
                            "Control channel closed, slot release for lease %s skipped",
                            lease_scope.lease_name,
                        )

    async def serve(self):
        """Serve the exporter, handling leases until stopped."""
        # Set exporter identity before anything else so every log line (including
        # registration and telemetry setup) carries the correct exporter name.
        set_log_context(exporter=self.exporter_name)
        async with self.session():
            pass
        # Unbounded on purpose. The control-plane loop is the sole receiver AND,
        # when it replays a stashed reassignment, a producer. With a bounded
        # buffer, anyio hands a freed slot straight to a blocked sender on
        # receive(), so a full buffer plus the loop's own send would deadlock the
        # consumer, and handle_lease's shielded LeaseFinished send could hang
        # shutdown. Producers are bounded (live leases + one RPC stream), so an
        # unbounded buffer costs nothing and removes both hangs. Sends use
        # send_nowait, which never blocks against math.inf.
        status_tx, status_rx = create_memory_object_stream[jumpstarter_pb2.StatusResponse | LeaseFinished](
            max_buffer_size=math.inf
        )
        try:
            await self._run_control_plane(status_tx, status_rx)
        finally:
            if self.exit_on_lease_end:
                # Ensure the runtime container exits whenever this exporter is
                # configured for ExitAndReplace (covers hook on_failure=exit and
                # other stop paths that skip the lease-end branch above).
                await anyio.to_thread.run_sync(shutdown_runtime_sidecar)
            self._tg = None
            self._status_drain_active = False
            clear_log_context()

        # Flush any remaining telemetry entries before the process exits.
        if self._telemetry_handler is not None:
            logging.getLogger().removeHandler(self._telemetry_handler)
            await self._telemetry_handler.close_async()
            self._telemetry_handler = None
        if self._telemetry_channel is not None:
            await self._telemetry_channel.close()
            self._telemetry_channel = None

    async def _run_control_plane(
        self,
        status_tx: MemoryObjectSendStream[jumpstarter_pb2.StatusResponse | LeaseFinished],
        status_rx: MemoryObjectReceiveStream[jumpstarter_pb2.StatusResponse | LeaseFinished],
    ) -> None:
        """Start control-plane streams and process status updates."""
        async with create_task_group() as tg:
            self._tg = tg
            self._control_tx = status_tx
            self._status_rpc_event = Event()
            self._pending_status_request = None
            self._status_drain_active = True
            tg.start_soon(self._drain_status_reports)
            if self._telemetry_handler is not None:
                tg.start_soon(self._telemetry_handler.flush_loop)
            tg.start_soon(
                self._retry_stream,
                "Status",
                self._status_stream_factory(),
                status_tx,
            )
            tg.start_soon(self._token_refresh_loop)
            # One loop, one writer of _lease_context. Status ticks come from the
            # controller RPC; LeaseFinished comes from handle_lease when it has
            # torn a lease down. Both are handled here, sequentially.
            async for message in status_rx:
                if isinstance(message, LeaseFinished):
                    if await self._on_lease_finished(message.lease_ctx):
                        break
                    continue
                if await self._apply_status(message, tg):
                    break

    async def _apply_status(
        self,
        status: jumpstarter_pb2.StatusResponse,
        tg: TaskGroup,
    ) -> bool:
        """Process a single status update. Returns True to stop the status loop."""
        previous_state = self._lease_state
        current_leased = status.leased

        if not current_leased:
            self._last_completed_lease = None

        if current_leased:
            if previous_state == LeaseState.IDLE and status.lease_name != "":
                if status.lease_name == self._last_completed_lease:
                    logger.debug("Ignoring trailing status for completed lease %s", status.lease_name)
                    return False
                self._on_lease_acquired(status, tg)
            elif (
                previous_state == LeaseState.LEASED
                and self._lease_context
                and self._lease_context.lease_name != status.lease_name
            ):
                # Controller reassigned the exporter to a different lease.
                # Stash the new status and signal the old lease to tear down.
                # The loop replays the stashed status from _on_lease_finished,
                # once LeaseFinished for the old lease has released the slot. The
                # controller won't re-send it because proto.Equal suppresses
                # duplicates.
                self._pending_lease_status = status
                if not self._lease_context.lease_ended.is_set():
                    logger.warning(
                        "Controller reassigned exporter from lease %s to %s; tearing down current lease",
                        self._lease_context.lease_name,
                        status.lease_name,
                    )
                    self._lease_context.lease_ended.set()
                    tg.start_soon(self._lease_finished_watchdog, self._lease_context)
                return False

            self._on_lease_update(status)
        else:
            await self._on_lease_released(previous_state, tg)
            if self._lease_context is not None:
                # A lease is tearing down. The slot is released, and the stop
                # decision made, when LeaseFinished arrives (_on_lease_finished),
                # so exit_on_lease_end keeps the runtime up until the hook is done.
                return False

        return self._check_stop_requested() if not current_leased else False

    def _lease_log_context(self, status: jumpstarter_pb2.StatusResponse) -> dict[str, str]:
        """Build the log context dict for a newly-assigned lease.

        Extracted so tests can verify context propagation without duplicating
        this logic separately from _on_lease_acquired.
        """
        log_ctx: dict[str, str] = {"lease_id": status.lease_name, "exporter": self.exporter_name}
        if status.context:
            log_ctx.update(status.context)
        return log_ctx

    def _on_lease_acquired(
        self,
        status: jumpstarter_pb2.StatusResponse,
        tg: TaskGroup,
    ) -> None:
        """Handle new lease assignment: create context and spawn lease handler."""
        self._started = True
        logger.info("Starting new lease: %s", status.lease_name)
        lease_scope = LeaseContext(
            lease_name=status.lease_name,
            before_lease_hook=Event(),
        )
        self._lease_context = lease_scope
        set_log_context(**self._lease_log_context(status))
        if self.hook_executor:
            tg.start_soon(
                self.hook_executor.run_before_lease_hook,
                lease_scope,
                self._report_status,
                self.stop,
                self._request_lease_release,
            )
        tg.start_soon(self.handle_lease, status.lease_name, tg, lease_scope)

    def _on_lease_update(self, status: jumpstarter_pb2.StatusResponse) -> None:
        """Update client info on every leased status tick."""
        if self._lease_context:
            self._lease_context.update_client(status.client_name)
            if status.client_name:
                set_log_context(client=status.client_name)
        logger.info("Currently leased by %s under %s", status.client_name, status.lease_name)

    async def _on_lease_finished(self, lease_ctx: LeaseContext) -> bool:
        """Release a finished lease's slot. Runs only in the control-plane loop.

        handle_lease posts LeaseFinished from its finally, after session_for_lease
        has exited (gRPC graceful stop included). Receiving it *is* the
        "transport is gone" happens-before edge, so there is no timing-based
        settle to guess at — the old 0.2s sleep it replaced never actually
        covered that teardown.

        No ownership re-check is needed: the loop is the sole writer of
        _lease_context, and it only acquires a replacement by replaying a stashed
        reassignment at the end of this handler. So when LeaseFinished arrives,
        the slot is still this lease.

        Returns True if the loop should stop (exit_on_lease_end / requested stop).
        """
        if self._lease_context is not lease_ctx:
            # Defensive: with the loop as sole writer, only reacquiring a
            # replacement after releasing the slot, this should not happen —
            # LeaseFinished for a lease that still owns the slot is the only
            # reachable case. Kept as a guard so a future second writer can't
            # silently wipe an unrelated lease.
            logger.debug("Lease %s not the current slot owner, ignoring LeaseFinished", lease_ctx.lease_name)
            return self._check_stop_requested()

        self._last_completed_lease = lease_ctx.lease_name
        self._lease_context = None
        if self.exit_on_lease_end:
            # _on_lease_released sets this on the leased=false tick; setting it
            # here too covers exit paths that never saw that tick (cancellation,
            # stale lease, handle_lease finishing first).
            self._stop_requested = True
        # structlog contextvars live in this (loop) task, so clearing here is
        # what actually drops the lease's log fields — a clear in handle_lease's
        # task would not reach the loop.
        clear_log_context()
        set_log_context(exporter=self.exporter_name)
        logger.debug("Ready for next lease")

        # Now that the slot is free, replay a stashed reassignment so the loop
        # acquires the new lease on the next iteration.
        pending = self._pending_lease_status
        if pending is not None:
            self._pending_lease_status = None
            if self._control_tx is not None:
                try:
                    # send_nowait, not send: this is the loop sending into the
                    # channel it is the sole receiver of. A blocking send here
                    # would deadlock — nothing else drains it. The channel is
                    # unbounded, so this always succeeds unless it is closed.
                    self._control_tx.send_nowait(pending)
                except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                    logger.debug("Control channel closed, skipping replay for %s", pending.lease_name)

        return self._check_stop_requested()

    async def _on_lease_released(self, previous_state: LeaseState, tg: TaskGroup) -> None:
        """Handle a not-leased status tick: signal the lease to tear down.

        This does not block or clear _lease_context. It only sets lease_ended,
        which handle_lease is waiting on to run the afterLease hook and tear the
        session down. handle_lease then posts LeaseFinished, and the loop
        releases the slot in _on_lease_finished. Keeping the slot set until then
        is what lets _report_status still reach the client session while the
        afterLease hook runs (it gates its session update on _lease_context).

        Not blocking here is the point: the loop stays responsive during Ending,
        and slot release happens on the LeaseFinished message, not inline.
        """
        logger.info("Currently not leased")

        if previous_state == LeaseState.LEASED and self._lease_context:
            lease_ctx = self._lease_context
            if self.exit_on_lease_end:
                # Refuse new leases immediately, but keep the runtime up until
                # afterLease finishes — shutdown SIGTERMs Exec children (QEMU)
                # that hooks may still be talking to. The loop breaks only once
                # LeaseFinished arrives (see _on_lease_finished), so the hook has
                # completed by the time serve()'s finally runs the shutdown.
                self._stop_requested = True

            if not lease_ctx.lease_ended.is_set():
                logger.info("Lease ended, signaling handle_lease to run afterLease hook")
                lease_ctx.lease_ended.set()
                # Diagnostic only: the loop waits for LeaseFinished with no
                # timeout, so a wedged handle_lease would park the exporter in
                # Ending forever (still reporting leased). Warn if that happens;
                # never release the slot on a timer.
                tg.start_soon(self._lease_finished_watchdog, lease_ctx)

    async def _lease_finished_watchdog(self, lease_ctx: LeaseContext) -> None:
        """Warn if a lease stays in Ending too long waiting for LeaseFinished.

        Spawned when lease_ended is set. The loop releases the slot only on
        LeaseFinished, with no timeout — the right behavior, since it is a real
        happens-before edge rather than a guess. But a handle_lease that never
        posts (e.g. wedged teardown) would leave the exporter reporting leased
        while serving nothing, invisible from outside. This logs once so the
        stall is diagnosable; it never touches _lease_context.
        """
        await anyio.sleep(_LEASE_FINISHED_WATCHDOG)
        if self._lease_context is lease_ctx:
            logger.warning(
                "Lease %s ended %ss ago but handle_lease has not posted LeaseFinished; "
                "exporter is stuck in Ending and still reporting leased",
                lease_ctx.lease_name,
                _LEASE_FINISHED_WATCHDOG,
            )

    def _check_stop_requested(self) -> bool:
        """Check if stop was requested and initiate shutdown. Returns True to break the status loop."""
        if self._stop_requested:
            self.stop(should_unregister=self._deferred_unregister)
            return True
        return False

    async def serve_standalone_tcp(
        self,
        host: str,
        port: int,
        *,
        tls_credentials: grpc.ServerCredentials | None = None,
        interceptors: list | None = None,
    ) -> None:
        """Serve the exporter on a TCP address without a controller (standalone mode).

        One session is created and served on host:port (and a temporary Unix socket
        for hook j commands). beforeLease hook runs once if configured; then status
        is set to LEASE_READY. Runs until stop() cancels the task group.
        """
        self._standalone = True
        lease_scope = LeaseContext(lease_name="standalone", before_lease_hook=Event())
        self._lease_context = lease_scope
        set_log_context(exporter=self.exporter_name, lease_id="standalone")

        with TemporarySocket() as hook_path:
            hook_path_str = str(hook_path)
            with Session(
                uuid=self.uuid,
                labels=self.labels,
                exporter_name=self.exporter_name,
                root_device=self.device_factory(),
                motd=self.motd,
            ) as session:
                session.lease_context = lease_scope
                lease_scope.session = session
                lease_scope.socket_path = hook_path_str
                lease_scope.hook_socket_path = hook_path_str

                async with session.serve_tcp_and_unix_async(
                    host,
                    port,
                    hook_path_str,
                    tls_credentials=tls_credentials,
                    interceptors=interceptors,
                ):
                    try:
                        async with create_task_group() as tg:
                            self._tg = tg
                            tg.start_soon(self._handle_end_session, lease_scope)

                            if self.hook_executor:
                                await self.hook_executor.run_before_lease_hook(
                                    lease_scope,
                                    self._report_status,
                                    self.stop,
                                    self._request_lease_release,
                                )
                            else:
                                await self._report_status(ExporterStatus.LEASE_READY, "Ready for commands")
                                lease_scope.before_lease_hook.set()

                            await _standalone_shutdown_waiter()
                    finally:
                        await self._cleanup_after_lease(lease_scope)

        self._lease_context = None
        self._tg = None
