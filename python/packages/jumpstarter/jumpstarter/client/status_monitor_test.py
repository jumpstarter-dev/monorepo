"""Tests for StatusMonitor background polling and status tracking."""

from unittest.mock import MagicMock

import anyio
import pytest
from grpc import StatusCode
from grpc.aio import AioRpcError

from jumpstarter.client.status_monitor import StatusMonitor
from jumpstarter.common import ExporterStatus

pytestmark = pytest.mark.anyio


class MockAioRpcError(AioRpcError):
    """Mock gRPC error for testing that properly inherits from AioRpcError."""

    def __init__(self, status_code: StatusCode, message: str = ""):
        self._status_code = status_code
        self._message = message

    def code(self) -> StatusCode:
        return self._status_code

    def details(self) -> str:
        return self._message


def create_mock_rpc_error(code: StatusCode, details: str = "") -> MockAioRpcError:
    """Create a mock AioRpcError with the specified status code."""
    return MockAioRpcError(code, details)


def create_status_response(
    status: ExporterStatus,
    version: int = 1,
    message: str = "",
    previous_status: ExporterStatus | None = None,
):
    """Create a mock GetStatusResponse."""
    response = MagicMock()
    response.status = status.to_proto()
    response.status_version = version
    response.message = message
    if previous_status is not None:
        response.HasField.return_value = True
        response.previous_status = previous_status.to_proto()
    else:
        response.HasField.return_value = False
    return response


class MockExporterStub:
    """Mock stub that returns predefined status responses."""

    def __init__(self, responses: list | None = None, repeat_last: bool = True):
        self._responses = list(responses or [])
        self._index = 0
        self._call_count = 0
        self._repeat_last = repeat_last

    async def GetStatus(self, request):
        self._call_count += 1
        if self._index < len(self._responses):
            response = self._responses[self._index]
            self._index += 1
            if isinstance(response, Exception):
                raise response
            return response
        elif self._repeat_last and self._responses:
            # Repeat the last response
            last = self._responses[-1]
            if isinstance(last, Exception):
                raise last
            return last
        else:
            # Default to AVAILABLE if no responses
            return create_status_response(ExporterStatus.AVAILABLE, version=self._call_count)


class TestStatusMonitorProperties:
    async def test_current_status_property(self) -> None:
        """Test that current_status property returns cached status."""
        stub = MockExporterStub()
        monitor = StatusMonitor(stub, poll_interval=0.1)

        # Initially None
        assert monitor.current_status is None

        # After manual update (simulating poll)
        monitor._current_status = ExporterStatus.AVAILABLE
        assert monitor.current_status == ExporterStatus.AVAILABLE

    async def test_status_version_property(self) -> None:
        """Test that status_version property returns cached version."""
        stub = MockExporterStub()
        monitor = StatusMonitor(stub, poll_interval=0.1)

        assert monitor.status_version == 0

        monitor._status_version = 5
        assert monitor.status_version == 5

    async def test_connection_lost_property(self) -> None:
        """Test that connection_lost property returns connection state."""
        stub = MockExporterStub()
        monitor = StatusMonitor(stub, poll_interval=0.1)

        assert not monitor.connection_lost

        monitor._connection_lost = True
        assert monitor.connection_lost

    async def test_missed_transitions_property(self) -> None:
        """Test that missed_transitions property tracks missed updates."""
        stub = MockExporterStub()
        monitor = StatusMonitor(stub, poll_interval=0.1)

        assert monitor.missed_transitions == 0

        monitor._missed_transitions = 3
        assert monitor.missed_transitions == 3


class TestStatusMonitorPolling:
    async def test_poll_loop_updates_status(self) -> None:
        """Test that poll loop updates current status from GetStatus."""
        responses = [
            create_status_response(ExporterStatus.AVAILABLE, version=1),
            create_status_response(ExporterStatus.BEFORE_LEASE_HOOK, version=2),
            create_status_response(ExporterStatus.LEASE_READY, version=3),
        ]
        stub = MockExporterStub(responses)
        monitor = StatusMonitor(stub, poll_interval=0.05)

        async with anyio.create_task_group() as tg:
            await monitor.start(tg)
            # Wait for a few poll cycles
            await anyio.sleep(0.2)
            await monitor.stop()

        assert monitor.current_status is not None
        assert stub._call_count >= 3

    async def test_poll_loop_tracks_version(self) -> None:
        """Test that poll loop tracks status version."""
        responses = [
            create_status_response(ExporterStatus.AVAILABLE, version=5),
        ]
        stub = MockExporterStub(responses)
        monitor = StatusMonitor(stub, poll_interval=0.05)

        async with anyio.create_task_group() as tg:
            await monitor.start(tg)
            await anyio.sleep(0.1)
            await monitor.stop()

        assert monitor.status_version == 5

    async def test_poll_loop_detects_missed_transitions(self) -> None:
        """Test that poll loop detects and counts missed transitions."""
        responses = [
            create_status_response(ExporterStatus.AVAILABLE, version=1),
            create_status_response(ExporterStatus.LEASE_READY, version=5),  # Skipped 2,3,4
        ]
        stub = MockExporterStub(responses)
        monitor = StatusMonitor(stub, poll_interval=0.05)

        async with anyio.create_task_group() as tg:
            await monitor.start(tg)
            await anyio.sleep(0.15)
            await monitor.stop()

        assert monitor.missed_transitions == 3  # Missed versions 2, 3, 4

    async def test_poll_loop_slow_polling_when_idle(self) -> None:
        """Test that poll loop uses slow interval when idle in LEASE_READY."""
        responses = [
            create_status_response(ExporterStatus.LEASE_READY, version=1),
        ]
        stub = MockExporterStub(responses)
        monitor = StatusMonitor(stub, poll_interval=0.05)
        monitor._slow_poll_interval = 0.5  # Set long slow poll

        async with anyio.create_task_group() as tg:
            await monitor.start(tg)
            await anyio.sleep(0.2)  # Less than slow poll interval
            call_count = stub._call_count
            await monitor.stop()

        # Should only have polled 1-2 times due to slow polling
        assert call_count <= 2

    async def test_poll_loop_fast_polling_with_waiters(self) -> None:
        """Test that poll loop uses fast interval when waiters are active."""
        responses = [
            create_status_response(ExporterStatus.LEASE_READY, version=1),
        ]
        stub = MockExporterStub(responses)
        monitor = StatusMonitor(stub, poll_interval=0.05)
        monitor._slow_poll_interval = 1.0

        # Simulate active waiter
        monitor._active_waiters = 1

        async with anyio.create_task_group() as tg:
            await monitor.start(tg)
            await anyio.sleep(0.2)
            call_count = stub._call_count
            await monitor.stop()

        # Should have polled multiple times due to fast polling
        assert call_count >= 3

    async def test_poll_loop_handles_unimplemented(self) -> None:
        """Test that poll loop exits gracefully on UNIMPLEMENTED."""
        error = create_mock_rpc_error(StatusCode.UNIMPLEMENTED)
        responses = [error]
        stub = MockExporterStub(responses)
        monitor = StatusMonitor(stub, poll_interval=0.05)

        async with anyio.create_task_group() as tg:
            await monitor.start(tg)
            await anyio.sleep(0.1)
            # Poll loop should have exited
            await monitor.stop()

        assert stub._call_count == 1  # Only tried once before giving up

    async def test_poll_loop_handles_unavailable(self) -> None:
        """Test that poll loop sets connection_lost on UNAVAILABLE."""
        responses = [
            create_status_response(ExporterStatus.AVAILABLE, version=1),
            create_mock_rpc_error(StatusCode.UNAVAILABLE),
        ]
        stub = MockExporterStub(responses)
        monitor = StatusMonitor(stub, poll_interval=0.05)

        async with anyio.create_task_group() as tg:
            await monitor.start(tg)
            await anyio.sleep(0.15)
            await monitor.stop()

        assert monitor.connection_lost


class TestStatusMonitorWaitForStatus:
    async def test_wait_for_status_already_at_target(self) -> None:
        """Test wait_for_status returns immediately when already at target."""
        stub = MockExporterStub()
        monitor = StatusMonitor(stub, poll_interval=0.1)
        monitor._current_status = ExporterStatus.LEASE_READY
        monitor._running = True

        result = await monitor.wait_for_status(ExporterStatus.LEASE_READY, timeout=1.0)

        assert result is True

    async def test_wait_for_status_transitions_to_target(self) -> None:
        """Test wait_for_status waits until target status is reached."""
        responses = [
            create_status_response(ExporterStatus.AVAILABLE, version=1),
            create_status_response(ExporterStatus.BEFORE_LEASE_HOOK, version=2),
            create_status_response(ExporterStatus.LEASE_READY, version=3),
        ]
        stub = MockExporterStub(responses)
        monitor = StatusMonitor(stub, poll_interval=0.05)

        async with anyio.create_task_group() as tg:
            await monitor.start(tg)

            result = await monitor.wait_for_status(ExporterStatus.LEASE_READY, timeout=2.0)

            await monitor.stop()

        assert result is True
        assert monitor.current_status == ExporterStatus.LEASE_READY

    async def test_wait_for_status_timeout(self) -> None:
        """Test wait_for_status returns False on timeout."""
        responses = [
            create_status_response(ExporterStatus.AVAILABLE, version=1),
        ]
        stub = MockExporterStub(responses)
        monitor = StatusMonitor(stub, poll_interval=0.05)

        async with anyio.create_task_group() as tg:
            await monitor.start(tg)

            result = await monitor.wait_for_status(ExporterStatus.LEASE_READY, timeout=0.15)

            await monitor.stop()

        assert result is False

    async def test_wait_for_status_connection_lost(self) -> None:
        """Test wait_for_status returns False when connection is lost."""
        # Return UNAVAILABLE to simulate connection loss
        responses = [
            create_status_response(ExporterStatus.AVAILABLE, version=1),
            create_mock_rpc_error(StatusCode.UNAVAILABLE),
        ]
        stub = MockExporterStub(responses)
        monitor = StatusMonitor(stub, poll_interval=0.05)

        async with anyio.create_task_group() as tg:
            await monitor.start(tg)

            result = await monitor.wait_for_status(ExporterStatus.LEASE_READY, timeout=0.5)

            await monitor.stop()

        assert result is False
        assert monitor.connection_lost

    async def test_wait_for_status_unimplemented_returns_promptly(self) -> None:
        """Test wait_for_status returns promptly when UNIMPLEMENTED is received.

        When GetStatus returns UNIMPLEMENTED (old exporter without status support),
        the monitor should signal waiters so they don't hang indefinitely.
        """
        # First return AVAILABLE, then UNIMPLEMENTED to simulate an old exporter
        responses = [
            create_status_response(ExporterStatus.AVAILABLE, version=1),
            create_mock_rpc_error(StatusCode.UNIMPLEMENTED),
        ]
        stub = MockExporterStub(responses)
        monitor = StatusMonitor(stub, poll_interval=0.05)

        async with anyio.create_task_group() as tg:
            await monitor.start(tg)

            # Wait for a status that will never be reached - should return promptly
            # when UNIMPLEMENTED is received, not hang until timeout
            result = await monitor.wait_for_status(ExporterStatus.LEASE_READY, timeout=2.0)

            await monitor.stop()

        # Should return False promptly (well before the 2s timeout)
        assert result is False
        # Monitor should have stopped running
        assert not monitor._running


class TestStatusMonitorWaitForAnyOf:
    async def test_wait_for_any_of_already_at_target(self) -> None:
        """Test wait_for_any_of returns immediately when already at one of targets."""
        stub = MockExporterStub()
        monitor = StatusMonitor(stub, poll_interval=0.1)
        monitor._current_status = ExporterStatus.LEASE_READY
        monitor._running = True

        targets = [ExporterStatus.LEASE_READY, ExporterStatus.AVAILABLE]
        result = await monitor.wait_for_any_of(targets, timeout=1.0)

        assert result == ExporterStatus.LEASE_READY

    async def test_wait_for_any_of_first_match_wins(self) -> None:
        """Test wait_for_any_of returns first matching status."""
        responses = [
            create_status_response(ExporterStatus.AVAILABLE, version=1),
            create_status_response(ExporterStatus.BEFORE_LEASE_HOOK, version=2),
        ]
        stub = MockExporterStub(responses)
        monitor = StatusMonitor(stub, poll_interval=0.05)

        async with anyio.create_task_group() as tg:
            await monitor.start(tg)

            targets = [ExporterStatus.BEFORE_LEASE_HOOK, ExporterStatus.LEASE_READY]
            result = await monitor.wait_for_any_of(targets, timeout=2.0)

            await monitor.stop()

        assert result == ExporterStatus.BEFORE_LEASE_HOOK

    async def test_wait_for_any_of_timeout_returns_none(self) -> None:
        """Test wait_for_any_of returns None on timeout."""
        responses = [
            create_status_response(ExporterStatus.AVAILABLE, version=1),
        ]
        stub = MockExporterStub(responses)
        monitor = StatusMonitor(stub, poll_interval=0.05)

        async with anyio.create_task_group() as tg:
            await monitor.start(tg)

            targets = [ExporterStatus.LEASE_READY, ExporterStatus.AFTER_LEASE_HOOK]
            result = await monitor.wait_for_any_of(targets, timeout=0.15)

            await monitor.stop()

        assert result is None

    async def test_wait_for_any_of_connection_lost(self) -> None:
        """Test wait_for_any_of returns None when connection is lost."""
        responses = [
            create_status_response(ExporterStatus.AVAILABLE, version=1),
            create_mock_rpc_error(StatusCode.UNAVAILABLE),
        ]
        stub = MockExporterStub(responses)
        monitor = StatusMonitor(stub, poll_interval=0.05)

        async with anyio.create_task_group() as tg:
            await monitor.start(tg)

            targets = [ExporterStatus.LEASE_READY]
            result = await monitor.wait_for_any_of(targets, timeout=0.5)

            await monitor.stop()

        assert result is None

    async def test_wait_for_any_of_unimplemented_returns_promptly(self) -> None:
        """Test wait_for_any_of returns promptly when UNIMPLEMENTED is received.

        When GetStatus returns UNIMPLEMENTED (old exporter without status support),
        the monitor should signal waiters so they don't hang indefinitely.
        """
        # First return AVAILABLE, then UNIMPLEMENTED to simulate an old exporter
        responses = [
            create_status_response(ExporterStatus.AVAILABLE, version=1),
            create_mock_rpc_error(StatusCode.UNIMPLEMENTED),
        ]
        stub = MockExporterStub(responses)
        monitor = StatusMonitor(stub, poll_interval=0.05)

        async with anyio.create_task_group() as tg:
            await monitor.start(tg)

            # Wait for statuses that will never be reached - should return promptly
            # when UNIMPLEMENTED is received, not hang until timeout
            targets = [ExporterStatus.LEASE_READY, ExporterStatus.AFTER_LEASE_HOOK]
            result = await monitor.wait_for_any_of(targets, timeout=2.0)

            await monitor.stop()

        # Should return None promptly (well before the 2s timeout)
        assert result is None
        # Monitor should have stopped running
        assert not monitor._running


class TestStatusMonitorLifecycle:
    async def test_start_sets_running_flag(self) -> None:
        """Test that start() sets the running flag."""
        stub = MockExporterStub([
            create_status_response(ExporterStatus.AVAILABLE, version=1),
        ])
        monitor = StatusMonitor(stub, poll_interval=0.1)

        assert not monitor._running

        async with anyio.create_task_group() as tg:
            await monitor.start(tg)
            assert monitor._running
            await monitor.stop()

    async def test_stop_clears_running_flag(self) -> None:
        """Test that stop() clears the running flag."""
        stub = MockExporterStub([
            create_status_response(ExporterStatus.AVAILABLE, version=1),
        ])
        monitor = StatusMonitor(stub, poll_interval=0.1)

        async with anyio.create_task_group() as tg:
            await monitor.start(tg)
            await monitor.stop()

        assert not monitor._running

    async def test_context_manager_stops_on_exit(self) -> None:
        """Test that context manager stops monitor on exit."""
        stub = MockExporterStub([
            create_status_response(ExporterStatus.AVAILABLE, version=1),
        ])
        monitor = StatusMonitor(stub, poll_interval=0.1)
        monitor._running = True

        async with monitor:
            pass

        assert not monitor._running


class TestStatusMonitorCallbacks:
    async def test_on_status_change_callback(self) -> None:
        """Test that status change callbacks are invoked."""
        responses = [
            create_status_response(ExporterStatus.AVAILABLE, version=1),
            create_status_response(ExporterStatus.LEASE_READY, version=2),
        ]
        stub = MockExporterStub(responses)
        monitor = StatusMonitor(stub, poll_interval=0.05)

        callback_invocations = []

        async def callback(new_status, old_status):
            callback_invocations.append((new_status, old_status))

        monitor.on_status_change(callback)

        async with anyio.create_task_group() as tg:
            await monitor.start(tg)
            await anyio.sleep(0.15)
            await monitor.stop()

        # Should have at least one callback for the transition
        assert len(callback_invocations) >= 1

    async def test_callback_exception_does_not_stop_monitor(self) -> None:
        """Test that callback exceptions don't stop the monitor."""
        responses = [
            create_status_response(ExporterStatus.AVAILABLE, version=1),
            create_status_response(ExporterStatus.LEASE_READY, version=2),
            create_status_response(ExporterStatus.AFTER_LEASE_HOOK, version=3),
        ]
        stub = MockExporterStub(responses)
        monitor = StatusMonitor(stub, poll_interval=0.05)

        async def failing_callback(new_status, old_status):
            raise ValueError("Callback error")

        monitor.on_status_change(failing_callback)

        async with anyio.create_task_group() as tg:
            await monitor.start(tg)
            await anyio.sleep(0.2)
            await monitor.stop()

        # Monitor should still have polled multiple times despite callback errors
        assert stub._call_count >= 2
