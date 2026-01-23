import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Self

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
from google.protobuf import empty_pb2
from jumpstarter_protocol import (
    jumpstarter_pb2,
    jumpstarter_pb2_grpc,
)

from jumpstarter.common import ExporterStatus, Metadata
from jumpstarter.common.streams import connect_router_stream
from jumpstarter.config.tls import TLSConfigV1Alpha1
from jumpstarter.exporter.hooks import HookExecutor
from jumpstarter.exporter.lease_context import LeaseContext
from jumpstarter.exporter.session import Session

if TYPE_CHECKING:
    from jumpstarter.driver import Driver

logger = logging.getLogger(__name__)


@dataclass(kw_only=True)
class Exporter(AsyncContextManagerMixin, Metadata):
    """Represents a Jumpstarter Exporter runtime instance.

    Inherits from Metadata, which provides:
        uuid: Unique identifier for the exporter instance (UUID4)
        labels: Key-value labels for exporter identification and selector matching
    """

    # Public Configuration Fields

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

    hook_executor: HookExecutor | None = field(default=None)
    """Optional executor for lifecycle hooks (before-lease and after-lease).

    When configured, runs custom scripts at key points in the lease lifecycle:
    - before-lease: Runs when transitioning to leased state (setup, validation)
    - after-lease: Runs when transitioning from leased state (cleanup, reset)
    Created when hooks.before_lease or hooks.after_lease are defined in config.
    """

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

    _previous_leased: bool = field(init=False, default=False)
    """Previous lease state used to detect lease state transitions.

    Tracks whether the exporter was leased in the previous status check to
    determine when to trigger before-lease and after-lease hooks.
    """

    _exit_code: int | None = field(init=False, default=None)
    """Exit code to use when the exporter shuts down.

    When set to a non-zero value, the exporter should terminate permanently
    (not restart). This is used by hooks with on_failure='exit' to signal
    that the exporter should shut down and not be restarted by the CLI.
    """

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
            logger.info("Exporter marked for stop upon lease exit")

    @property
    def exit_code(self) -> int | None:
        """Get the exit code for the exporter.

        Returns:
            The exit code if set, or None if the exporter should restart.
        """
        return self._exit_code

    async def _get_controller_stub(self) -> jumpstarter_pb2_grpc.ControllerServiceStub:
        """Create and return a controller service stub."""
        return jumpstarter_pb2_grpc.ControllerServiceStub(await self.channel_factory())

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
            try:
                controller = await self._get_controller_stub()
                logger.debug("%s stream connected to controller", stream_name)
                async for item in stream_factory(controller):
                    logger.debug("%s stream received item", stream_name)
                    await send_tx.send(item)
            except Exception as e:
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
        controller = await self._get_controller_stub()
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

    async def _report_status(self, status: ExporterStatus, message: str = ""):
        """Report the exporter status with the controller and session."""
        self._exporter_status = status

        # Update status in lease context (handles session update internally)
        # This ensures status is stored even before session is created
        if self._lease_context:
            self._lease_context.update_status(status, message)

        try:
            controller = await self._get_controller_stub()
            await controller.ReportStatus(
                jumpstarter_pb2.ReportStatusRequest(
                    status=status.to_proto(),
                    message=message,
                )
            )
            logger.info(f"Updated status to {status}: {message}")
        except Exception as e:
            logger.error(f"Failed to update status: {e}")

    async def _request_lease_release(self):
        """Request the controller to release the current lease.

        Called after the afterLease hook completes to ensure the lease is
        released even if the client disconnects unexpectedly. This moves
        the lease release responsibility from the client to the exporter.
        """
        if not self._lease_context or not self._lease_context.lease_name:
            logger.debug("No active lease to release")
            return

        try:
            controller = await self._get_controller_stub()
            await controller.ReportStatus(
                jumpstarter_pb2.ReportStatusRequest(
                    status=ExporterStatus.AVAILABLE.to_proto(),
                    message="Lease released after afterLease hook",
                    release_lease=True,
                )
            )
            logger.info("Requested controller to release lease %s", self._lease_context.lease_name)
        except Exception as e:
            logger.error("Failed to request lease release: %s", e)
            # Fall through - the client can still release the lease as a fallback,
            # or the lease will eventually expire

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
        logger.debug("_handle_end_session task started, waiting for end_session_requested event")
        # Wait for client to signal end of session
        await lease_context.end_session_requested.wait()
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

            if self.hook_executor and lease_context.has_client():
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
                logger.debug("No afterLease hook configured or no client, skipping")
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
            root_device=self.device_factory(),
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

        Yields:
            tuple[Session, str, str]: A tuple of (session, main_socket_path, hook_socket_path)
        """
        logger.info("Creating new session for lease")
        with Session(
            uuid=self.uuid,
            labels=self.labels,
            root_device=self.device_factory(),
        ) as session:
            # Create dual Unix sockets - one for clients, one for hooks
            async with session.serve_unix_with_hook_socket_async() as (main_path, hook_path):
                logger.info("Session serving on main=%s, hook=%s", main_path, hook_path)
                # Create a gRPC channel to the controller via the main socket
                async with grpc.aio.secure_channel(
                    f"unix://{main_path}", grpc.local_channel_credentials(grpc.LocalConnectionType.UDS)
                ) as channel:
                    # Register the exporter with the controller
                    await self._register_with_controller(channel)
                # Yield session and both socket paths
                yield session, main_path, hook_path
        logger.info("Session closed")

    async def handle_lease(self, lease_name: str, tg: TaskGroup, lease_scope: LeaseContext) -> None:
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
        logger.info("Listening for incoming connection requests on lease %s", lease_name)

        # Buffer Listen responses to avoid blocking when responses arrive before
        # process_connections starts iterating. This prevents a race condition where
        # the client dials immediately after lease acquisition but before the session is ready.
        listen_tx, listen_rx = create_memory_object_stream[jumpstarter_pb2.ListenResponse](max_buffer_size=10)

        # Start listening for connection requests with retry logic
        tg.start_soon(
            self._retry_stream,
            "Listen",
            self._listen_stream_factory(lease_name),
            listen_tx,
        )

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
            # before session was created, e.g., LEASE_READY when no hooks configured)
            session.update_status(lease_scope.current_status, lease_scope.status_message)
            logger.debug("Session sockets: main=%s, hook=%s", main_path, hook_path)

            # Accept connections immediately - driver calls will be gated internally
            # until the beforeLease hook completes. This allows LogStream to work
            # during hook execution for real-time log streaming.
            logger.info("Accepting connections (driver calls gated until beforeLease hook completes)")

            # Note: Status is managed by _report_status() which updates both LeaseContext
            # and Session. The sync above handles the case where status was updated before
            # session creation (e.g., LEASE_READY when no hooks configured).

            # Start task to handle EndSession requests (runs afterLease hook when client signals done)
            tg.start_soon(self._handle_end_session, lease_scope)

            # Process client connections until lease ends
            # The lease can end via:
            # 1. listen_rx stream closing (controller stops sending)
            # 2. lease_ended event being set (serve() detected lease status change)
            # Type: request is jumpstarter_pb2.ListenResponse with router_endpoint and router_token fields
            try:
                async with create_task_group() as conn_tg:
                    async def wait_for_lease_end():
                        """Wait for lease_ended event and cancel the connection loop."""
                        await lease_scope.lease_ended.wait()
                        logger.info("Lease ended event received, stopping connection handling")
                        conn_tg.cancel_scope.cancel()

                    async def process_connections():
                        """Process incoming connection requests."""
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
            finally:
                # Run afterLease hook before closing the session
                # This ensures the socket is still available for driver calls within the hook
                # Shield from cancellation so the hook can complete even during shutdown
                with CancelScope(shield=True):
                    # Always run afterLease hook when handle_lease exits (session closing)
                    # Skip if already started via EndSession or lease state transition
                    if not lease_scope.after_lease_hook_started.is_set():
                        lease_scope.after_lease_hook_started.set()
                        if self.hook_executor and lease_scope.has_client():
                            logger.info("Running afterLease hook on session close")
                            await self.hook_executor.run_after_lease_hook(
                                lease_scope,
                                self._report_status,
                                self.stop,
                                self._request_lease_release,
                            )
                        # Mark hook as done if we didn't run it (no hook configured or no client)
                        if not lease_scope.after_lease_hook_done.is_set():
                            lease_scope.after_lease_hook_done.set()
                    else:
                        # Hook was already started elsewhere, wait for it to complete
                        logger.debug("Waiting for afterLease hook to complete before closing session")
                        await lease_scope.after_lease_hook_done.wait()
                        logger.debug("afterLease hook completed, closing session")

    async def serve(self):  # noqa: C901
        """
        Serve the exporter.
        """
        # initial registration
        async with self.session():
            pass
        # Buffer status updates to avoid blocking during short processing gaps
        status_tx, status_rx = create_memory_object_stream[jumpstarter_pb2.StatusResponse](max_buffer_size=5)

        async with create_task_group() as tg:
            self._tg = tg
            # Start status stream with retry logic
            tg.start_soon(
                self._retry_stream,
                "Status",
                self._status_stream_factory(),
                status_tx,
            )
            async for status in status_rx:
                # Check if lease name changed (and there was a previous active lease)
                lease_changed = (
                    self._lease_context
                    and self._lease_context.is_active()
                    and self._lease_context.lease_name != status.lease_name
                )
                if lease_changed:
                    # Lease name changed - need to restart with new lease
                    # Signal handle_lease() that the lease has ended so it can exit its loop
                    # and run the afterLease hook in its finally block (where session is still open)
                    logger.info("Lease status changed, signaling lease ended")
                    self._lease_context.lease_ended.set()

                    # Wait for the afterLease hook to complete (run by handle_lease finally block)
                    # This ensures the session stays open for the hook subprocess
                    with CancelScope(shield=True):
                        await self._lease_context.after_lease_hook_done.wait()
                        logger.info("afterLease hook completed, stopping exporter")

                        # Brief yield to let pending gRPC callbacks complete before cancellation
                        # This prevents InvalidStateError on Future cleanup during stop()
                        await sleep(0.1)

                    # Clear lease scope for next lease
                    self._lease_context = None
                    self.stop()
                    break

                # Check for lease state transitions
                previous_leased = self._previous_leased
                current_leased = status.leased

                # Check if this is a new lease assignment (no active lease context and we have a lease name)
                # This handles both first lease and subsequent leases after the previous one ended
                if self._lease_context is None and status.lease_name != "":
                    self._started = True
                    logger.info("Starting new lease: %s", status.lease_name)
                    # Create lease scope and start handling the lease
                    # The session will be created inside handle_lease and stay open for the lease duration
                    lease_scope = LeaseContext(
                        lease_name=status.lease_name,
                        before_lease_hook=Event(),
                    )
                    self._lease_context = lease_scope
                    tg.start_soon(self.handle_lease, status.lease_name, tg, lease_scope)

                if current_leased:
                    logger.info("Currently leased by %s under %s", status.client_name, status.lease_name)
                    if self._lease_context:
                        self._lease_context.update_client(status.client_name)

                    # Before-lease hook when transitioning from unleased to leased
                    if not previous_leased:
                        if self.hook_executor and self._lease_context:
                            tg.start_soon(
                                self.hook_executor.run_before_lease_hook,
                                self._lease_context,
                                self._report_status,
                                self.stop,  # Pass shutdown callback
                            )
                        else:
                            # No hook configured, set event immediately
                            await self._report_status(ExporterStatus.LEASE_READY, "Ready for commands")
                            if self._lease_context:
                                self._lease_context.before_lease_hook.set()
                else:
                    logger.info("Currently not leased")

                    # After-lease hook when transitioning from leased to unleased
                    # Signal handle_lease() that the lease has ended so it can exit its loop
                    # and run the afterLease hook in its finally block (where session is still open)
                    if (
                        previous_leased
                        and self._lease_context
                        and self._lease_context.has_client()
                    ):
                        logger.info("Lease ended, signaling handle_lease to run afterLease hook")
                        self._lease_context.lease_ended.set()

                        # Wait for the hook to complete
                        with CancelScope(shield=True):
                            await self._lease_context.after_lease_hook_done.wait()
                        logger.info("afterLease hook completed")

                    # Clear lease scope for next lease
                    self._lease_context = None
                    # Brief delay to ensure session is fully closed before next lease
                    # This prevents SSL corruption from overlapping connections
                    await sleep(0.2)
                    logger.debug("Ready for next lease")

                    if self._stop_requested:
                        self.stop(should_unregister=True)
                        break

                self._previous_leased = current_leased
        self._tg = None
