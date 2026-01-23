import logging
from collections import deque
from collections.abc import Generator
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass, field
from logging.handlers import QueueHandler
from typing import TYPE_CHECKING, Self
from uuid import UUID

import grpc
from anyio import ContextManagerMixin, Event, TypedAttributeLookupError, sleep
from anyio.from_thread import start_blocking_portal
from jumpstarter_protocol import (
    jumpstarter_pb2,
    jumpstarter_pb2_grpc,
    router_pb2_grpc,
)

from .logging import LogHandler
from jumpstarter.common import ExporterStatus, LogSource, Metadata, TemporarySocket
from jumpstarter.common.streams import StreamRequestMetadata
from jumpstarter.streams.common import forward_stream
from jumpstarter.streams.metadata import MetadataStreamAttributes
from jumpstarter.streams.router import RouterStream

if TYPE_CHECKING:
    from jumpstarter.driver import Driver
    from jumpstarter.exporter.lease_context import LeaseContext

logger = logging.getLogger(__name__)


@dataclass(kw_only=True)
class Session(
    jumpstarter_pb2_grpc.ExporterServiceServicer,
    router_pb2_grpc.RouterServiceServicer,
    Metadata,
    ContextManagerMixin,
):
    root_device: "Driver"
    mapping: dict[UUID, "Driver"]
    lease_context: "LeaseContext | None" = field(init=False, default=None)

    _logging_queue: deque = field(init=False)
    _logging_handler: QueueHandler = field(init=False)
    _current_status: ExporterStatus = field(init=False, default=ExporterStatus.AVAILABLE)
    _status_message: str = field(init=False, default="")
    _status_update_event: Event = field(init=False)
    _status_version: int = field(init=False, default=0)
    _previous_status: ExporterStatus | None = field(init=False, default=None)

    @contextmanager
    def __contextmanager__(self) -> Generator[Self]:
        logging.getLogger().addHandler(self._logging_handler)
        self.root_device.reset()
        try:
            yield self
        finally:
            try:
                self.root_device.close()
            except Exception as e:
                # Get driver name from report for more descriptive logging
                try:
                    report = self.root_device.report()
                    driver_name = report.labels.get("jumpstarter.dev/name", self.root_device.__class__.__name__)
                except Exception:
                    driver_name = self.root_device.__class__.__name__
                logger.error("Error closing driver %s: %s", driver_name, e, exc_info=True)
            finally:
                logging.getLogger().removeHandler(self._logging_handler)

    def __init__(self, *args, root_device, **kwargs):
        super().__init__(*args, **kwargs)

        self.root_device = root_device
        self.mapping = {u: i for (u, _, _, i) in self.root_device.enumerate()}

        self._logging_queue = deque(maxlen=32)
        self._logging_handler = LogHandler(self._logging_queue, LogSource.SYSTEM)
        self._status_update_event = Event()

        # Map all driver logs to DRIVER source
        self._logging_handler.add_child_handler("driver.", LogSource.DRIVER)

    @asynccontextmanager
    async def serve_port_async(self, port):
        server = grpc.aio.server()
        server.add_insecure_port(port)

        jumpstarter_pb2_grpc.add_ExporterServiceServicer_to_server(self, server)
        router_pb2_grpc.add_RouterServiceServicer_to_server(self, server)

        await server.start()
        try:
            yield
        finally:
            await server.stop(grace=None)

    @asynccontextmanager
    async def serve_multi_port_async(self, *ports):
        """Serve session on multiple ports simultaneously.

        This is used to create separate sockets for client connections and hook
        j commands, preventing SSL frame corruption when both are active.

        Args:
            *ports: One or more port specifications (e.g., "unix:///path/to/socket")

        Yields:
            None - caller manages socket paths externally
        """
        server = grpc.aio.server()
        for port in ports:
            server.add_insecure_port(port)
            logger.debug("Session server listening on %s", port)

        jumpstarter_pb2_grpc.add_ExporterServiceServicer_to_server(self, server)
        router_pb2_grpc.add_RouterServiceServicer_to_server(self, server)

        await server.start()
        logger.info("Session server started on %d ports", len(ports))
        try:
            yield
        finally:
            logger.info("Stopping session server...")
            # Use a short grace period to allow pending RPCs to complete
            # This helps prevent SSL corruption from abrupt connection termination
            await server.stop(grace=1.0)
            # Brief delay to ensure all connections are fully closed
            await sleep(0.1)
            logger.info("Session server stopped")

    @asynccontextmanager
    async def serve_unix_async(self):
        with TemporarySocket() as path:
            async with self.serve_port_async(f"unix://{path}"):
                yield path

    @asynccontextmanager
    async def serve_unix_with_hook_socket_async(self):
        """Serve session on two Unix sockets: one for clients, one for hooks.

        This creates separate sockets to prevent SSL frame corruption when
        hook subprocess j commands access the session concurrently with
        client LogStream connections.

        Yields:
            tuple[str, str]: (main_socket_path, hook_socket_path)
        """
        with TemporarySocket() as main_path:
            with TemporarySocket() as hook_path:
                async with self.serve_multi_port_async(f"unix://{main_path}", f"unix://{hook_path}"):
                    yield main_path, hook_path

    @contextmanager
    def serve_unix(self):
        with start_blocking_portal() as portal:
            with portal.wrap_async_context_manager(self.serve_unix_async()) as path:
                yield path

    def __getitem__(self, key: UUID):
        return self.mapping[key]

    def _check_status_for_driver_call(self, context):
        """Check if the current status allows driver calls.

        Driver calls are allowed during:
        - LEASE_READY: Normal operation
        - BEFORE_LEASE_HOOK: Allows hooks to use `j` commands
        - AFTER_LEASE_HOOK: Allows hooks to use `j` commands

        Args:
            context: gRPC context for aborting with error if status invalid

        Raises:
            Aborts the RPC with FAILED_PRECONDITION if status doesn't allow driver calls
        """
        ALLOWED_STATUSES = {
            ExporterStatus.LEASE_READY,
            ExporterStatus.BEFORE_LEASE_HOOK,
            ExporterStatus.AFTER_LEASE_HOOK,
        }

        if self._current_status not in ALLOWED_STATUSES:
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"Exporter not ready for driver calls (status: {self._current_status})"
            )

    async def GetReport(self, request, context):
        logger.debug("GetReport()")
        return jumpstarter_pb2.GetReportResponse(
            uuid=str(self.uuid),
            labels=self.labels,
            reports=[
                instance.report(parent=parent, name=name)
                for (_, parent, name, instance) in self.root_device.enumerate()
            ],
        )

    async def DriverCall(self, request, context):
        logger.debug("DriverCall(uuid=%s, method=%s)", request.uuid, request.method)
        self._check_status_for_driver_call(context)
        return await self[UUID(request.uuid)].DriverCall(request, context)

    async def StreamingDriverCall(self, request, context):
        logger.debug("StreamingDriverCall(uuid=%s, method=%s)", request.uuid, request.method)
        self._check_status_for_driver_call(context)
        async for v in self[UUID(request.uuid)].StreamingDriverCall(request, context):
            yield v

    async def Stream(self, _request_iterator, context):
        request = StreamRequestMetadata(**dict(list(context.invocation_metadata()))).request
        logger.debug("Streaming(%s)", request)
        async with self[request.uuid].Stream(request, context) as stream:
            metadata = []
            with suppress(TypedAttributeLookupError):
                metadata.extend(stream.extra(MetadataStreamAttributes.metadata).items())
            await context.send_initial_metadata(metadata)

            async with RouterStream(context=context) as remote:
                async with forward_stream(remote, stream):
                    event = Event()
                    context.add_done_callback(lambda _: event.set())
                    await event.wait()

    async def LogStream(self, request, context):
        while not context.done():
            try:
                yield self._logging_queue.popleft()
            except IndexError:
                # Short polling interval for real-time log streaming
                await sleep(0.05)

    def update_status(self, status: int | ExporterStatus, message: str = ""):
        """Update the current exporter status for the session and signal status change.

        Tracks previous status and increments version counter for transition detection.
        """
        if isinstance(status, int):
            new_status = ExporterStatus.from_proto(status)
        else:
            new_status = status

        # Track previous status for transition detection
        self._previous_status = self._current_status
        self._current_status = new_status
        self._status_message = message

        # Increment version to help clients detect missed transitions
        self._status_version += 1

        # Signal status change for any waiters
        self._status_update_event.set()
        # Create a new event for the next status change
        self._status_update_event = Event()

    def add_logger_source(self, logger_name: str, source: LogSource):
        """Add a log source mapping for a specific logger."""
        self._logging_handler.add_child_handler(logger_name, source)

    def remove_logger_source(self, logger_name: str):
        """Remove a log source mapping for a specific logger."""
        self._logging_handler.remove_child_handler(logger_name)

    def context_log_source(self, logger_name: str, source: LogSource):
        """Context manager to temporarily set a log source for a specific logger."""
        return self._logging_handler.context_log_source(logger_name, source)

    async def GetStatus(self, request, context):
        """Get the current exporter status with transition tracking."""
        logger.info("GetStatus() -> %s (version=%d)", self._current_status, self._status_version)
        response = jumpstarter_pb2.GetStatusResponse(
            status=self._current_status.to_proto(),
            message=self._status_message,
            status_version=self._status_version,
        )
        if self._previous_status is not None:
            response.previous_status = self._previous_status.to_proto()
        return response

    async def EndSession(self, request, context):
        """End the current session and trigger the afterLease hook.

        This is called by the client when it's done with the session. The method
        signals the end_session_requested event and returns immediately, allowing
        the client to keep receiving logs via LogStream while the afterLease hook
        runs asynchronously.

        The client should:
        1. Keep LogStream running after calling EndSession
        2. Poll GetStatus (using StatusMonitor) to detect when AVAILABLE status is reached
        3. Then disconnect

        This enables the session socket to stay open for controller monitoring and
        supports exporter autonomy - the afterLease hook runs regardless of client state.

        Returns:
            EndSessionResponse with success status and optional message.
        """
        logger.debug("EndSession RPC called by client")

        if self.lease_context is None:
            logger.debug("EndSession called but no lease context available")
            return jumpstarter_pb2.EndSessionResponse(
                success=False,
                message="No active lease context",
            )

        # Signal that the client wants to end the session
        # The afterLease hook will run asynchronously via _handle_end_session
        logger.debug("Setting end_session_requested event...")
        self.lease_context.end_session_requested.set()
        logger.debug("end_session_requested event SET, returning response")

        # Return immediately - don't wait for afterLease hook
        # The client should continue receiving logs and monitor status for AVAILABLE
        return jumpstarter_pb2.EndSessionResponse(
            success=True,
            message="Session end triggered, afterLease hook running asynchronously",
        )
