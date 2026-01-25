"""
Base classes for drivers and driver clients
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from anyio import create_task_group
from google.protobuf import empty_pb2
from grpc import StatusCode
from grpc.aio import AioRpcError
from jumpstarter_protocol import jumpstarter_pb2, jumpstarter_pb2_grpc, router_pb2_grpc
from rich.logging import RichHandler

from jumpstarter.common import ExporterStatus, Metadata
from jumpstarter.common.exceptions import JumpstarterException
from jumpstarter.common.resources import ResourceMetadata
from jumpstarter.common.serde import decode_value, encode_value
from jumpstarter.common.streams import (
    DriverStreamRequest,
    ResourceStreamRequest,
    StreamRequestMetadata,
)
from jumpstarter.streams.common import forward_stream
from jumpstarter.streams.encoding import compress_stream
from jumpstarter.streams.metadata import MetadataStream, MetadataStreamAttributes
from jumpstarter.streams.progress import ProgressStream
from jumpstarter.streams.router import RouterStream


class DriverError(JumpstarterException):
    """
    Raised when a driver call returns an error
    """


class DriverMethodNotImplemented(DriverError, NotImplementedError):
    """
    Raised when a driver method is not implemented
    """


class DriverInvalidArgument(DriverError, ValueError):
    """
    Raised when a driver method is called with invalid arguments
    """


class ExporterNotReady(DriverError):
    """
    Raised when the exporter is not ready to accept driver calls
    """


@dataclass(kw_only=True)
class AsyncDriverClient(
    Metadata,
    jumpstarter_pb2_grpc.ExporterServiceStub,
    router_pb2_grpc.RouterServiceStub,
):
    """
    Async driver client base class

    Backing implementation of blocking driver client.
    """

    stub: Any

    log_level: str = "INFO"
    logger: logging.Logger = field(init=False)

    def __post_init__(self):
        if hasattr(super(), "__post_init__"):
            super().__post_init__()
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.setLevel(self.log_level)
        # Initialize status monitor (not a dataclass field to avoid Pydantic type resolution issues)
        self._status_monitor = None

        # add default handler
        if not self.logger.handlers:
            handler = RichHandler(show_path=False)
            self.logger.addHandler(handler)

    async def get_status_async(self) -> ExporterStatus | None:
        """Get the current exporter status.

        Returns:
            The current ExporterStatus, or None if GetStatus is not implemented.
        """
        try:
            response = await self.stub.GetStatus(jumpstarter_pb2.GetStatusRequest())
            return ExporterStatus.from_proto(response.status)
        except AioRpcError as e:
            # If GetStatus is not implemented, return None for backward compatibility
            if e.code() == StatusCode.UNIMPLEMENTED:
                self.logger.debug("GetStatus not implemented")
                return None
            raise DriverError(f"Failed to get exporter status: {e.details()}") from e

    async def check_exporter_status(self):
        """Check if the exporter is ready to accept driver calls.

        Allows driver commands during hook execution (BEFORE_LEASE_HOOK, AFTER_LEASE_HOOK)
        in addition to the normal LEASE_READY status. This enables hooks to interact
        with drivers via the `j` CLI for automation use cases.
        """
        # Statuses that allow driver commands
        ALLOWED_STATUSES = {
            ExporterStatus.LEASE_READY,
            ExporterStatus.BEFORE_LEASE_HOOK,
            ExporterStatus.AFTER_LEASE_HOOK,
        }

        status = await self.get_status_async()
        if status is None:
            # GetStatus not implemented, assume ready for backward compatibility
            return

        if status not in ALLOWED_STATUSES:
            raise ExporterNotReady(f"Exporter status is {status}")

    async def wait_for_lease_ready(self, timeout: float = 300.0) -> None:
        """Wait for exporter to report LEASE_READY status.

        This polls GetStatus until the beforeLease hook completes.
        Should be called after log streaming is started so hook output
        can be displayed in real-time.

        Prefer using StatusMonitor.wait_for_any_of() for non-blocking status tracking.

        Args:
            timeout: Maximum time to wait in seconds (default: 5 minutes)
        """
        import anyio

        poll_interval = 0.5  # seconds
        elapsed = 0.0
        poll_count = 0

        self.logger.debug("Waiting for exporter to be ready...")
        while elapsed < timeout:
            poll_count += 1
            self.logger.debug("[POLL %d] Calling GetStatus (elapsed: %.1fs)...", poll_count, elapsed)
            try:
                status = await self.get_status_async()
                self.logger.debug("[POLL %d] GetStatus returned: %s", poll_count, status)
            except Exception as e:
                # Connection error - keep trying
                self.logger.debug("[POLL %d] Error getting status, will retry: %s", poll_count, e)
                await anyio.sleep(poll_interval)
                elapsed += poll_interval
                continue

            if status is None:
                # GetStatus not implemented - assume ready for backward compatibility
                self.logger.debug("[POLL %d] GetStatus not implemented, assuming ready", poll_count)
                return

            if status == ExporterStatus.LEASE_READY:
                self.logger.info("Exporter ready, starting shell...")
                return
            elif status == ExporterStatus.BEFORE_LEASE_HOOK:
                # Hook is running - this is expected, keep waiting
                self.logger.debug("[POLL %d] beforeLease hook is running...", poll_count)
            elif status == ExporterStatus.BEFORE_LEASE_HOOK_FAILED:
                # Hook failed - log but continue (exporter may still be usable)
                self.logger.warning("beforeLease hook failed")
                return
            elif status == ExporterStatus.AVAILABLE:
                # Exporter is available but not yet leased - keep waiting
                # This can happen if client connects before exporter receives lease assignment
                self.logger.debug("[POLL %d] Exporter status: AVAILABLE (waiting for lease assignment)", poll_count)
            else:
                # Other status - continue waiting
                self.logger.debug("[POLL %d] Exporter status: %s (waiting...)", poll_count, status)

            self.logger.debug("[POLL %d] Sleeping for %.1fs before next poll...", poll_count, poll_interval)
            await anyio.sleep(poll_interval)
            elapsed += poll_interval

        self.logger.warning("Timeout waiting for beforeLease hook to complete (after %d polls)", poll_count)

    async def end_session_async(self) -> bool:
        """End the current session and trigger the afterLease hook.

        This signals the exporter to run the afterLease hook. The exporter will
        release the lease after the hook completes, which may cause the connection
        to be disrupted. Connection errors after EndSession is called are treated
        as successful completion (the hook ran and the lease was released).

        Returns:
            True if the session end was triggered successfully or the connection
            was disrupted (indicating the lease was released), False if EndSession
            is not implemented.
        """
        try:
            response = await self.stub.EndSession(jumpstarter_pb2.EndSessionRequest())
            self.logger.debug("EndSession completed: success=%s, message=%s", response.success, response.message)
            return response.success
        except AioRpcError as e:
            # If EndSession is not implemented, return False for backward compatibility
            if e.code() == StatusCode.UNIMPLEMENTED:
                self.logger.debug("EndSession not implemented")
                return False
            # Connection errors (UNAVAILABLE, CANCELLED, UNKNOWN with "Stream removed")
            # indicate the exporter has released the lease and restarted
            if e.code() in (StatusCode.UNAVAILABLE, StatusCode.CANCELLED):
                self.logger.debug("Connection disrupted during EndSession (lease released): %s", e.code())
                return True
            if e.code() == StatusCode.UNKNOWN and "Stream removed" in str(e.details()):
                self.logger.debug("Stream removed during EndSession (lease released)")
                return True
            raise DriverError(f"Failed to end session: {e.details()}") from e

    async def wait_for_hook_status(self, target_status: "ExporterStatus", timeout: float = 60.0) -> bool:
        """Wait for exporter to reach a target status using polling.

        Used after end_session_async() to wait for afterLease hook completion
        while keeping the log stream open to receive hook logs.

        Prefer using StatusMonitor.wait_for_any_of() for non-blocking status tracking.

        Args:
            target_status: The status to wait for (typically AVAILABLE)
            timeout: Maximum time to wait in seconds (default: 60 seconds)

        Returns:
            True if target status was reached, False if timed out
        """
        import anyio

        poll_interval = 0.5  # seconds
        elapsed = 0.0

        while elapsed < timeout:
            try:
                status = await self.get_status_async()

                if status is None:
                    # GetStatus not implemented - assume ready for backward compatibility
                    self.logger.debug("GetStatus not implemented, assuming hook complete")
                    return True

                if status == target_status:
                    self.logger.debug("Exporter reached target status: %s", status)
                    return True

                # Hook failed states also indicate completion
                if status == ExporterStatus.AFTER_LEASE_HOOK_FAILED:
                    self.logger.warning("afterLease hook failed")
                    return True

                # Still running hook - keep waiting
                self.logger.debug("Waiting for hook completion, current status: %s", status)

            except AioRpcError as e:
                # Connection error - the hook may still be running but we can't confirm
                self.logger.debug("Connection error while waiting for hook: %s", e.code())
                return False

            await anyio.sleep(poll_interval)
            elapsed += poll_interval

        self.logger.warning("Timeout waiting for hook to complete (target: %s)", target_status)
        return False

    @asynccontextmanager
    async def status_monitor_async(self, poll_interval: float = 0.3):
        """Start background status monitoring as a context manager.

        Creates a StatusMonitor that polls GetStatus in a background task,
        enabling non-blocking status checks and event-driven notifications.

        Args:
            poll_interval: Seconds between status polls (default: 0.3)

        Yields:
            StatusMonitor instance for non-blocking status tracking

        Example:
            async with client.status_monitor_async() as monitor:
                # Wait for beforeLease hook (non-blocking to other tasks)
                await monitor.wait_for_status(ExporterStatus.LEASE_READY)

                # Check current status at any time
                current = monitor.current_status
        """
        from jumpstarter.client.status_monitor import StatusMonitor

        monitor = StatusMonitor(self.stub, poll_interval)
        self._status_monitor = monitor

        async with create_task_group() as tg:
            await monitor.start(tg)
            try:
                yield monitor
            finally:
                await monitor.stop()
                self._status_monitor = None

    @property
    def status(self) -> ExporterStatus | None:
        """Get current cached status (non-blocking).

        Returns None if status monitor not started.
        """
        return self._status_monitor.current_status if self._status_monitor else None

    async def wait_for_lease_ready_monitored(self, timeout: float = 300.0) -> None:
        """Wait for LEASE_READY status using background monitor.

        Non-blocking to other async tasks. Requires status_monitor_async context.

        Args:
            timeout: Maximum time to wait in seconds (default: 5 minutes)
        """
        if not self._status_monitor:
            raise RuntimeError("Status monitor not started. Use status_monitor_async() context.")

        reached = await self._status_monitor.wait_for_status(
            ExporterStatus.LEASE_READY,
            timeout=timeout
        )
        if not reached:
            raise DriverError("Timeout waiting for LEASE_READY status")

    async def wait_for_hook_complete_monitored(self, timeout: float = 60.0) -> bool:
        """Wait for afterLease hook to complete (AVAILABLE status) using monitor.

        Non-blocking to other async tasks. Requires status_monitor_async context.

        Args:
            timeout: Maximum time to wait in seconds (default: 60 seconds)

        Returns:
            True if completed, False if timed out
        """
        if not self._status_monitor:
            return True  # No monitor, assume complete

        # Wait for AVAILABLE or hook failure status
        result = await self._status_monitor.wait_for_any_of(
            [ExporterStatus.AVAILABLE, ExporterStatus.AFTER_LEASE_HOOK_FAILED],
            timeout=timeout
        )
        return result is not None

    async def call_async(self, method, *args):
        """Make DriverCall by method name and arguments"""

        request = jumpstarter_pb2.DriverCallRequest(
            uuid=str(self.uuid),
            method=method,
            args=[encode_value(arg) for arg in args],
        )

        try:
            response = await self.stub.DriverCall(request)
        except AioRpcError as e:
            match e.code():
                case StatusCode.FAILED_PRECONDITION:
                    raise ExporterNotReady(e.details()) from None
                case StatusCode.NOT_FOUND:
                    raise DriverMethodNotImplemented(e.details()) from None
                case StatusCode.UNIMPLEMENTED:
                    raise DriverMethodNotImplemented(e.details()) from None
                case StatusCode.INVALID_ARGUMENT:
                    raise DriverInvalidArgument(e.details()) from None
                case StatusCode.UNKNOWN:
                    raise DriverError(e.details()) from None
                case _:
                    raise DriverError(e.details()) from e

        return decode_value(response.result)

    async def streamingcall_async(self, method, *args):
        """Make StreamingDriverCall by method name and arguments"""

        request = jumpstarter_pb2.StreamingDriverCallRequest(
            uuid=str(self.uuid),
            method=method,
            args=[encode_value(arg) for arg in args],
        )

        try:
            async for response in self.stub.StreamingDriverCall(request):
                yield decode_value(response.result)
        except AioRpcError as e:
            match e.code():
                case StatusCode.FAILED_PRECONDITION:
                    raise ExporterNotReady(e.details()) from None
                case StatusCode.UNIMPLEMENTED:
                    raise DriverMethodNotImplemented(e.details()) from None
                case StatusCode.INVALID_ARGUMENT:
                    raise DriverInvalidArgument(e.details()) from None
                case StatusCode.UNKNOWN:
                    raise DriverError(e.details()) from None
                case _:
                    raise DriverError(e.details()) from e

    @asynccontextmanager
    async def stream_async(self, method):
        context = self.stub.Stream(
            metadata=StreamRequestMetadata.model_construct(request=DriverStreamRequest(uuid=self.uuid, method=method))
            .model_dump(mode="json", round_trip=True)
            .items(),
        )
        metadata = dict(list(await context.initial_metadata()))
        async with MetadataStream(stream=RouterStream(context=context), metadata=metadata) as stream:
            yield stream

    @asynccontextmanager
    async def resource_async(
        self,
        stream,
        content_encoding: str | None = None,
    ):
        context = self.stub.Stream(
            metadata=StreamRequestMetadata.model_construct(
                request=ResourceStreamRequest(uuid=self.uuid, x_jmp_content_encoding=content_encoding)
            )
            .model_dump(mode="json", round_trip=True)
            .items(),
        )
        metadata = dict(list(await context.initial_metadata()))
        async with MetadataStream(stream=RouterStream(context=context), metadata=metadata) as rstream:
            metadata = ResourceMetadata(**rstream.extra(MetadataStreamAttributes.metadata))
            if metadata.x_jmp_accept_encoding is None:
                stream = compress_stream(stream, content_encoding)

            async with forward_stream(ProgressStream(stream=stream), rstream):
                yield metadata.resource.model_dump(mode="json")

    @asynccontextmanager
    async def log_stream_async(self, show_all_logs: bool = True):  # noqa: C901
        import anyio

        async def log_stream():  # noqa: C901
            from jumpstarter.common import LogSource

            reconnect_delay = 0.1  # Start with 100ms delay
            max_reconnect_delay = 2.0  # Max 2 seconds between reconnects
            max_reconnects = 10  # Give up after this many reconnects

            reconnect_count = 0
            while reconnect_count < max_reconnects:
                try:
                    async for response in self.stub.LogStream(empty_pb2.Empty()):
                        # Reset reconnect count on successful message
                        reconnect_count = 0
                        reconnect_delay = 0.1

                        # Determine log source
                        if response.HasField("source"):
                            source = LogSource(response.source)
                            is_hook = source in (LogSource.BEFORE_LEASE_HOOK, LogSource.AFTER_LEASE_HOOK)
                        else:
                            source = LogSource.SYSTEM
                            is_hook = False

                        # Filter: always show hooks, only show system logs if enabled
                        if is_hook or show_all_logs:
                            # Get severity level
                            severity = response.severity if response.severity else "INFO"
                            log_level = getattr(logging, severity, logging.INFO)

                            # Route to appropriate logger based on source
                            if source == LogSource.BEFORE_LEASE_HOOK:
                                logger_name = "exporter:beforeLease"
                            elif source == LogSource.AFTER_LEASE_HOOK:
                                logger_name = "exporter:afterLease"
                            elif source == LogSource.DRIVER:
                                logger_name = "exporter:driver"
                            else:  # SYSTEM
                                logger_name = "exporter:system"

                            # Log through logger for RichHandler formatting
                            source_logger = logging.getLogger(logger_name)
                            source_logger.log(log_level, response.message)

                    # Stream ended normally (server closed it)
                    self.logger.debug("Log stream ended normally, attempting reconnect...")

                except AioRpcError as e:
                    # Connection disrupted - try to reconnect for afterLease logs
                    if e.code() == StatusCode.UNAVAILABLE:
                        self.logger.debug("Log stream unavailable, reconnecting in %.1fs...", reconnect_delay)
                    elif e.code() == StatusCode.CANCELLED:
                        # Stream was cancelled, likely shutting down
                        self.logger.debug("Log stream cancelled")
                        break
                    else:
                        self.logger.debug("Log stream error: %s", e.code())

                except Exception as e:
                    # Other errors - log and try to reconnect
                    self.logger.debug("Log stream error: %s", e)

                # Wait before reconnecting (with exponential backoff)
                reconnect_count += 1
                await anyio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

            if reconnect_count >= max_reconnects:
                self.logger.debug("Log stream: max reconnects reached, giving up")

        async with create_task_group() as tg:
            tg.start_soon(log_stream)
            try:
                yield
            finally:
                tg.cancel_scope.cancel()
