"""Background status monitor for non-blocking status tracking.

This module provides a StatusMonitor class that polls GetStatus in a background
task, enabling non-blocking status checks and event-driven notifications when
specific statuses are reached.
"""

import logging
from collections.abc import Awaitable, Callable

import anyio
from anyio import Event
from grpc import StatusCode
from grpc.aio import AioRpcError
from jumpstarter_protocol import jumpstarter_pb2

from jumpstarter.common import ExporterStatus

logger = logging.getLogger(__name__)


class StatusMonitor:
    """Background status monitor that polls GetStatus without blocking.

    Instead of blocking in a wait loop, the monitor:
    1. Runs in a background task, polling GetStatus periodically
    2. Tracks status transitions and detects missed ones via status_version
    3. Fires events when specific statuses are reached
    4. Allows non-blocking status checks at any time

    Usage:
        async with client.start_status_monitor() as monitor:
            # Non-blocking status access
            current = monitor.current_status

            # Wait for specific status (non-blocking to other tasks)
            reached = await monitor.wait_for_status(ExporterStatus.LEASE_READY)
    """

    def __init__(self, stub, poll_interval: float = 0.3):
        """Initialize the status monitor.

        Args:
            stub: gRPC stub with GetStatus method
            poll_interval: Seconds between status polls (default: 0.3)
        """
        self._stub = stub
        self._poll_interval = poll_interval

        # Current cached status (updated by background task)
        self._current_status: ExporterStatus | None = None
        self._status_message: str = ""
        self._status_version: int = 0
        self._previous_status: ExporterStatus | None = None

        # Events for specific status transitions
        self._status_events: dict[ExporterStatus, Event] = {}

        # General status change event (fires on any change)
        self._any_change_event: Event = Event()

        # Track if we missed any transitions
        self._missed_transitions: int = 0

        # Callbacks for status changes
        self._on_status_change: list[Callable[[ExporterStatus, ExporterStatus | None], Awaitable[None]]] = []

        # Control
        self._running = False
        self._stop_event: Event = Event()
        self._poll_task_started: Event = Event()

        # Slow polling mode - used when idle (e.g., shell running)
        self._slow_poll_interval: float = 5.0

        # Track if we're actively waiting for a status (forces fast polling)
        self._active_waiters: int = 0

        # Track if connection was lost (UNAVAILABLE)
        self._connection_lost: bool = False

    @property
    def current_status(self) -> ExporterStatus | None:
        """Get the current cached status (non-blocking)."""
        return self._current_status

    @property
    def status_message(self) -> str:
        """Get the current status message."""
        return self._status_message

    @property
    def status_version(self) -> int:
        """Get the current status version."""
        return self._status_version

    @property
    def previous_status(self) -> ExporterStatus | None:
        """Get the previous status (for transition tracking)."""
        return self._previous_status

    @property
    def missed_transitions(self) -> int:
        """Number of status transitions missed due to polling interval."""
        return self._missed_transitions

    @property
    def connection_lost(self) -> bool:
        """True if the connection to the exporter was lost (UNAVAILABLE)."""
        return self._connection_lost

    def on_status_change(self, callback: Callable[[ExporterStatus, ExporterStatus | None], Awaitable[None]]):
        """Register a callback for status changes.

        Args:
            callback: Async function called with (new_status, old_status) on changes
        """
        self._on_status_change.append(callback)

    async def wait_for_status(self, target: ExporterStatus, timeout: float | None = None) -> bool:
        """Wait for a specific status (non-blocking to other tasks).

        Args:
            target: The status to wait for
            timeout: Maximum seconds to wait (None = no timeout)

        Returns:
            True if status was reached, False if timed out or connection lost
        """
        # If connection was marked as lost, verify with a quick poll
        # (connection may have recovered from transient error)
        if self._connection_lost:
            logger.debug("Connection was marked as lost, verifying...")
            try:
                response = await self._stub.GetStatus(jumpstarter_pb2.GetStatusRequest())
                # Connection recovered!
                logger.info("Connection recovered during verification poll")
                self._connection_lost = False
                new_status = ExporterStatus.from_proto(response.status)
                self._current_status = new_status
                self._status_version = response.status_version
                # Check if we're already at target
                if new_status == target:
                    return True
            except AioRpcError as e:
                if e.code() == StatusCode.UNAVAILABLE:
                    logger.debug("Connection still lost (UNAVAILABLE)")
                    return False
                # Other errors - connection might still work, continue waiting
                logger.debug("GetStatus error during verification: %s", e.code())
            except Exception as e:
                logger.debug("GetStatus error during verification: %s", e)
                return False

        # Check if already at target status
        if self._current_status == target:
            return True

        # Get or create event for this status
        if target not in self._status_events:
            self._status_events[target] = Event()

        async def wait_loop():
            # Increment active waiters to force fast polling
            self._active_waiters += 1
            try:
                while self._running and not self._connection_lost:
                    # Check current status
                    if self._current_status == target:
                        return True

                    # Capture event reference before waiting
                    current_event = self._any_change_event

                    # Double-check after capturing
                    if self._current_status == target:
                        return True

                    # Check for connection lost
                    if self._connection_lost:
                        logger.debug("Connection lost while waiting")
                        return False

                    # Wait for any status change
                    await current_event.wait()
                return False
            finally:
                self._active_waiters -= 1

        if timeout:
            with anyio.move_on_after(timeout):
                return await wait_loop()
            return False
        else:
            return await wait_loop()

    async def wait_for_any_of(
        self, targets: list[ExporterStatus], timeout: float | None = None
    ) -> ExporterStatus | None:
        """Wait for any of the specified statuses.

        Args:
            targets: List of statuses to wait for
            timeout: Maximum seconds to wait (None = no timeout)

        Returns:
            The status that was reached, or None if timed out or connection lost
        """
        # If connection was marked as lost, verify with a quick poll
        # (connection may have recovered from transient error)
        if self._connection_lost:
            logger.debug("Connection was marked as lost, verifying...")
            try:
                response = await self._stub.GetStatus(jumpstarter_pb2.GetStatusRequest())
                # Connection recovered!
                logger.info("Connection recovered during verification poll")
                self._connection_lost = False
                new_status = ExporterStatus.from_proto(response.status)
                self._current_status = new_status
                self._status_version = response.status_version
                # Check if we're already at target
                if new_status in targets:
                    return new_status
            except AioRpcError as e:
                if e.code() == StatusCode.UNAVAILABLE:
                    logger.debug("Connection still lost (UNAVAILABLE)")
                    return None
                # Other errors - connection might still work, continue waiting
                logger.debug("GetStatus error during verification: %s", e.code())
            except Exception as e:
                logger.debug("GetStatus error during verification: %s", e)
                return None

        # Check if already at one of the target statuses
        if self._current_status in targets:
            return self._current_status

        # Create events for all targets
        for target in targets:
            if target not in self._status_events:
                self._status_events[target] = Event()

        async def wait_for_first():
            # Increment active waiters to force fast polling
            self._active_waiters += 1
            try:
                while self._running and not self._connection_lost:
                    # Check current status first
                    for target in targets:
                        if self._current_status == target:
                            return target

                    # Capture event reference BEFORE waiting to avoid race condition
                    # The poll loop may replace _any_change_event after setting it
                    current_event = self._any_change_event

                    # Double-check status after capturing event (in case it changed)
                    for target in targets:
                        if self._current_status == target:
                            return target

                    # Check for connection lost
                    if self._connection_lost:
                        logger.debug("Connection lost while waiting")
                        return None

                    # Wait for status change notification
                    # Don't reset the event here - only the poll loop manages it
                    await current_event.wait()
                return None
            finally:
                self._active_waiters -= 1

        if timeout:
            with anyio.move_on_after(timeout):
                return await wait_for_first()
            return None
        else:
            return await wait_for_first()

    async def _poll_loop(self):
        """Background polling loop."""
        self._poll_task_started.set()
        logger.debug("Status monitor poll loop started")

        while self._running:
            try:
                response = await self._stub.GetStatus(jumpstarter_pb2.GetStatusRequest())
                new_status = ExporterStatus.from_proto(response.status)
                new_version = response.status_version
                previous = (
                    ExporterStatus.from_proto(response.previous_status)
                    if response.HasField("previous_status")
                    else None
                )

                # Reset connection_lost flag on successful poll
                # (connection may have recovered from transient error)
                if self._connection_lost:
                    logger.info("Connection recovered, resetting connection_lost flag")
                    self._connection_lost = False

                # Detect missed transitions
                if self._status_version > 0 and new_version > self._status_version + 1:
                    missed = new_version - self._status_version - 1
                    self._missed_transitions += missed
                    logger.warning(f"Missed {missed} status transition(s)")

                # Update cached state
                old_status = self._current_status
                self._current_status = new_status
                self._status_message = response.message or ""
                self._status_version = new_version
                self._previous_status = previous

                # Fire events if status changed
                if old_status != new_status:
                    logger.info(f"Status changed: {old_status} -> {new_status} (version={new_version})")

                    # Fire specific status event
                    if new_status in self._status_events:
                        self._status_events[new_status].set()
                        # Reset for next time
                        self._status_events[new_status] = Event()

                    # Fire general change event - set BEFORE replacing
                    self._any_change_event.set()
                    self._any_change_event = Event()

                    # Call callbacks
                    for callback in self._on_status_change:
                        try:
                            await callback(new_status, old_status)
                        except Exception as e:
                            logger.error(f"Status change callback error: {e}")

            except AioRpcError as e:
                if e.code() == StatusCode.UNIMPLEMENTED:
                    # GetStatus not implemented - old exporter, stop polling
                    logger.debug("GetStatus not implemented, stopping monitor")
                    break
                elif e.code() == StatusCode.UNAVAILABLE:
                    # Connection lost - exporter closed or restarted
                    logger.info("Connection lost (UNAVAILABLE), signaling waiters")
                    self._connection_lost = True
                    # Fire the change event to wake up any waiters
                    self._any_change_event.set()
                    self._any_change_event = Event()
                    break
                logger.debug(f"GetStatus poll error: {e.code()}")
            except Exception as e:
                logger.debug(f"GetStatus poll error: {e}")

            # Wait for next poll or stop signal
            # Use fast polling when there are active waiters or status is not LEASE_READY
            # Use slow polling only when idle in LEASE_READY state
            use_slow_polling = (
                self._active_waiters == 0
                and self._current_status == ExporterStatus.LEASE_READY
            )
            interval = self._slow_poll_interval if use_slow_polling else self._poll_interval
            with anyio.move_on_after(interval):
                await self._stop_event.wait()
                logger.debug("Stop event received, exiting poll loop")
                break

        logger.debug("Status monitor poll loop exited (running=%s)", self._running)

    async def start(self, task_group=None):
        """Start the background polling task.

        Args:
            task_group: Optional task group to start the poll loop in.
                       If None, creates an internal task group.
        """
        if self._running:
            return
        self._running = True
        self._stop_event = Event()
        self._poll_task_started = Event()

        if task_group:
            task_group.start_soon(self._poll_loop)
            # Wait for the poll task to actually start
            await self._poll_task_started.wait()
        else:
            # If no task group provided, run synchronously (for testing)
            await self._poll_loop()

    async def stop(self):
        """Stop the background polling task."""
        self._running = False
        self._stop_event.set()

    async def __aenter__(self):
        """Context manager entry - starts monitoring in a new task group."""
        return self

    async def __aexit__(self, *args):
        """Context manager exit - stops monitoring."""
        await self.stop()
