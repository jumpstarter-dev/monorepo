import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Self

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
from jumpstarter.driver import Driver
from jumpstarter.exporter.hooks import HookContext, HookExecutionError, HookExecutor
from jumpstarter.exporter.session import Session

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

    device_factory: Callable[[], Driver]
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

    _lease_name: str = field(init=False, default="")
    """Current lease name assigned by the controller.

    Empty string indicates no active lease. Updated when controller assigns/reassigns
    the exporter. Used to detect lease transitions and create hook contexts.
    """

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

    _current_client_name: str = field(init=False, default="")
    """Name of the client currently holding the lease.

    Used to create hook contexts with client information and determine if
    after-lease hooks should run. Reset when lease is released.
    """

    _before_lease_hook: Event | None = field(init=False, default=None)
    """Synchronization event that blocks connection handling until hook completes.

    Created when a new lease starts, waited on before accepting connections,
    and set when hook completes or is not configured.
    """

    _exporter_status: ExporterStatus = field(init=False, default=ExporterStatus.OFFLINE)
    """Current status of the exporter.

    Updated via _update_status() and reported to controller and session.
    Possible values: OFFLINE, AVAILABLE, BEFORE_LEASE_HOOK, LEASE_READY,
    AFTER_LEASE_HOOK, BEFORE_LEASE_HOOK_FAILED, AFTER_LEASE_HOOK_FAILED.
    """

    _current_session: Session | None = field(init=False, default=None)
    """Reference to the currently active Session object.

    A Session wraps the root device and provides gRPC service endpoints.
    Used to update session status and pass to HookExecutor for logging.
    Set in session() context manager and cleared when context exits.
    """

    _session_socket_path: str | None = field(init=False, default=None)
    """Unix socket path where the current session is serving.

    Passed to hooks so they can communicate with the device via the CLI.
    Enables session reuse instead of creating new ones for hooks.
    """

    def stop(self, wait_for_lease_exit=False, should_unregister=False):
        """Signal the exporter to stop.

        Args:
            wait_for_lease_exit (bool): If True, wait for the current lease to exit before stopping.
            should_unregister (bool): If True, unregister from controller. Otherwise rely on heartbeat.
        """

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

    async def _get_controller_stub(self) -> jumpstarter_pb2_grpc.ControllerServiceStub:
        """Create and return a controller service stub."""
        return jumpstarter_pb2_grpc.ControllerServiceStub(await self.channel_factory())

    async def _retry_stream(
        self,
        stream_name: str,
        stream_factory: Callable[[jumpstarter_pb2_grpc.ControllerServiceStub], AsyncGenerator],
        send_tx,
        retries: int = 5,
        backoff: float = 3.0,
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
                async for item in stream_factory(controller):
                    await send_tx.send(item)
            except Exception as e:
                if retries_left > 0:
                    retries_left -= 1
                    logger.info(
                        "%s stream interrupted, restarting in %ss, %s retries left: %s",
                        stream_name,
                        backoff,
                        retries_left,
                        e,
                    )
                    await sleep(backoff)
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

    async def _register_with_controller(self, channel: grpc.aio.Channel):
        """Register the exporter with the controller."""
        exporter_stub = jumpstarter_pb2_grpc.ExporterServiceStub(channel)
        response: jumpstarter_pb2.GetReportResponse = await exporter_stub.GetReport(empty_pb2.Empty())
        logger.info("Registering exporter with controller")
        controller = jumpstarter_pb2_grpc.ControllerServiceStub(channel)
        await controller.Register(
            jumpstarter_pb2.RegisterRequest(
                labels=self.labels,
                reports=response.reports,
            )
        )
        # Mark exporter as registered internally
        self._registered = True
        # Report that exporter is available to the controller
        # TODO: Determine if the controller should handle this logic internally
        await self._report_status(ExporterStatus.AVAILABLE, "Exporter registered and available")

    async def _report_status(self, status: ExporterStatus, message: str = ""):
        """Report the exporter status with the controller and session."""
        self._exporter_status = status

        # Update session status if available
        if self._current_session:
            self._current_session.update_status(status, message)

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
            async with await connect_unix(path) as stream:
                async with connect_router_stream(endpoint, token, stream, tls_config, grpc_options):
                    pass
        except Exception as e:
            logger.info("failed to handle connection: {}".format(e))

    @asynccontextmanager
    async def session(self):
        """Create and manage an exporter Session context."""
        with Session(
            uuid=self.uuid,
            labels=self.labels,
            root_device=self.device_factory(),
        ) as session:
            # Store session reference outside context for status updates
            self._current_session = session
            try:
                # Create a Unix socket
                async with session.serve_unix_async() as path:
                    # Create a gRPC channel to the controller via the socket
                    async with grpc.aio.secure_channel(
                        f"unix://{path}", grpc.local_channel_credentials(grpc.LocalConnectionType.UDS)
                    ) as channel:
                        # Register the exporter with the controller
                        await self._register_with_controller(channel)
                    yield path
            finally:
                # Clear the session reference
                self._current_session = None

    async def handle_lease(self, lease_name: str, tg: TaskGroup) -> None:
        """Handle all incoming client connections for a lease.

        This method orchestrates the complete lifecycle of managing connections during
        a lease period. It listens for connection requests and spawns individual
        tasks to handle each client connection.

        The method performs the following steps:
        1. Sets up a stream to listen for incoming connection requests
        2. Creates a session with a Unix socket for device access
        3. Waits for the before-lease hook to complete (if configured)
        4. Spawns a new task for each incoming connection request

        Args:
            lease_name: Name of the lease to handle connections for
            tg: TaskGroup for spawning concurrent connection handler tasks

        Note:
            This method runs for the entire duration of the lease and is spawned by
            the serve() method when a lease is assigned. It terminates when the lease
            ends or the exporter stops.
        """
        logger.info("Listening for incoming connection requests on lease %s", lease_name)

        listen_tx, listen_rx = create_memory_object_stream[jumpstarter_pb2.ListenResponse]()

        # Start listening for connection requests with retry logic
        tg.start_soon(
            self._retry_stream,
            "Listen",
            self._listen_stream_factory(lease_name),
            listen_tx,
        )

        # Create a lease session to execute hooks and handle connections
        async with self.session() as path:
            # Store socket path for hook execution
            self._session_socket_path = path

            # Wait for before-lease hook to complete before processing client connections
            if self._before_lease_hook is not None:
                logger.info("Waiting for before-lease hook to complete before accepting connections")
                await self._before_lease_hook.wait()
                logger.info("Before-lease hook completed, now accepting connections")

            # Process client connections
            # Type: request is jumpstarter_pb2.ListenResponse with router_endpoint and router_token fields
            async for request in listen_rx:
                logger.info("Handling new connection request on lease %s", lease_name)
                tg.start_soon(
                    self._handle_client_conn,
                    path,
                    request.router_endpoint,
                    request.router_token,
                    self.tls,
                    self.grpc_options,
                )

    async def serve(self):  # noqa: C901
        """
        Serve the exporter.
        """
        # initial registration
        async with self.session():
            pass
        status_tx, status_rx = create_memory_object_stream[jumpstarter_pb2.StatusResponse]()

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
                if self._lease_name != "" and self._lease_name != status.lease_name:
                    # After-lease hook for the previous lease (lease name changed)
                    if self.hook_executor and self._current_client_name:
                        hook_context = HookContext(self._lease_name, self._current_client_name)
                        # Shield the after-lease hook from cancellation and await it
                        with CancelScope(shield=True):
                            await self._report_status(ExporterStatus.AFTER_LEASE_HOOK, "Running afterLease hooks")
                            self.hook_executor.main_session = self._current_session
                            try:
                                await self.hook_executor.execute_after_lease_hook(
                                    hook_context, socket_path=self._session_socket_path
                                )
                                await self._report_status(ExporterStatus.AVAILABLE, "Available for new lease")
                            except HookExecutionError as e:
                                logger.error("afterLease hook failed (on_failure=endLease/exit): %s", e)
                                await self._report_status(
                                    ExporterStatus.AFTER_LEASE_HOOK_FAILED,
                                    f"afterLease hook failed: {e}",
                                )
                                logger.error("Shutting down exporter due to afterLease hook failure")
                                self.stop()
                            except Exception as e:
                                logger.error("afterLease hook failed with unexpected error: %s", e, exc_info=True)
                                await self._report_status(
                                    ExporterStatus.AFTER_LEASE_HOOK_FAILED,
                                    f"afterLease hook failed: {e}",
                                )

                    self._lease_name = status.lease_name
                    logger.info("Lease status changed, killing existing connections")
                    # Reset event for next lease
                    self._before_lease_hook = None
                    self.stop()
                    break

                # Check for lease state transitions
                previous_leased = hasattr(self, "_previous_leased") and self._previous_leased
                current_leased = status.leased

                self._lease_name = status.lease_name
                if not self._started and self._lease_name != "":
                    self._started = True
                    # Create event for pre-lease synchronization
                    self._before_lease_hook = Event()
                    tg.start_soon(self.handle_lease, self._lease_name, tg)

                if current_leased:
                    logger.info("Currently leased by %s under %s", status.client_name, status.lease_name)
                    self._current_client_name = status.client_name

                    # Before-lease hook when transitioning from unleased to leased
                    if not previous_leased:
                        if self.hook_executor:
                            hook_context = HookContext(status.lease_name, status.client_name)
                            tg.start_soon(self.run_before_lease_hook, hook_context)
                        else:
                            # No hook configured, set event immediately
                            await self._report_status(ExporterStatus.LEASE_READY, "Ready for commands")
                            if self._before_lease_hook:
                                self._before_lease_hook.set()
                else:
                    logger.info("Currently not leased")

                    # After-lease hook when transitioning from leased to unleased
                    if previous_leased and self.hook_executor and self._current_client_name:
                        hook_context = HookContext(self._lease_name, self._current_client_name)
                        # Shield the after-lease hook from cancellation and await it
                        with CancelScope(shield=True):
                            await self._report_status(ExporterStatus.AFTER_LEASE_HOOK, "Running afterLease hooks")
                            # Pass the current session to hook executor for logging
                            self.hook_executor.main_session = self._current_session
                            # Use session socket if available, otherwise create new session
                            await self.hook_executor.execute_after_lease_hook(
                                hook_context, socket_path=self._session_socket_path
                            )
                            await self._report_status(ExporterStatus.AVAILABLE, "Available for new lease")

                    self._current_client_name = ""
                    # Reset event for next lease
                    self._before_lease_hook = None

                    if self._stop_requested:
                        self.stop(should_unregister=True)
                        break

                self._previous_leased = current_leased
        self._tg = None

    async def run_before_lease_hook(self, hook_ctx: HookContext):
        """
        Execute the before-lease hook for the current exporter session.

        Args:
            hook_ctx (HookContext): The current hook execution context
        """
        try:
            await self._report_status(ExporterStatus.BEFORE_LEASE_HOOK, "Running beforeLease hooks")
            # Pass the current session to hook executor for logging
            self.hook_executor.main_session = self._current_session

            # Wait for socket path to be available
            while self._session_socket_path is None:
                await sleep(0.1)

            # Execute hook with main session socket
            await self.hook_executor.execute_before_lease_hook(hook_ctx, socket_path=self._session_socket_path)
            await self._report_status(ExporterStatus.LEASE_READY, "Ready for commands")
            logger.info("beforeLease hook completed successfully")
        except HookExecutionError as e:
            # Hook failed with on_failure='block' - end lease and set failed status
            logger.error("beforeLease hook failed (on_failure=block): %s", e)
            await self._report_status(
                ExporterStatus.BEFORE_LEASE_HOOK_FAILED, f"beforeLease hook failed (on_failure=block): {e}"
            )
            # Note: We don't take the exporter offline for before_lease hook failures
            # The lease is simply not ready, and the exporter remains available for future leases
        except Exception as e:
            # Unexpected error during hook execution
            logger.error("beforeLease hook failed with unexpected error: %s", e, exc_info=True)
            await self._report_status(ExporterStatus.BEFORE_LEASE_HOOK_FAILED, f"beforeLease hook failed: {e}")
        finally:
            # Always set the event to unblock connections
            if self._before_lease_hook:
                self._before_lease_hook.set()

    async def run_after_lease_hook(self, hook_ctx: HookContext):
        """
        Execute the after-lease hook for the current exporter session.

        Args:
            hook_ctx (HookContext): The current hook execution context
        """
        try:
            await self._report_status(ExporterStatus.AFTER_LEASE_HOOK, "Running afterLease hooks")
            # Pass the current session to hook executor for logging
            self.hook_executor.main_session = self._current_session
            # Use session socket if available, otherwise create new session
            await self.hook_executor.execute_after_lease_hook(hook_ctx, socket_path=self._session_socket_path)
            await self._report_status(ExporterStatus.AVAILABLE, "Available for new lease")
            logger.info("afterLease hook completed successfully")
        except HookExecutionError as e:
            # Hook failed with on_failure='block' - set failed status and shut down exporter
            logger.error("afterLease hook failed (on_failure=block): %s", e)
            await self._report_status(
                ExporterStatus.AFTER_LEASE_HOOK_FAILED, f"afterLease hook failed (on_failure=block): {e}"
            )
            # Shut down the exporter after after_lease hook failure with on_failure='block'
            logger.error("Shutting down exporter due to afterLease hook failure")
            self.stop()
        except Exception as e:
            # Unexpected error during hook execution
            logger.error("afterLease hook failed with unexpected error: %s", e, exc_info=True)
            await self._report_status(ExporterStatus.AFTER_LEASE_HOOK_FAILED, f"afterLease hook failed: {e}")
