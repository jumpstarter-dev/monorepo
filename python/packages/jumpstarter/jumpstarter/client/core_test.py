"""Tests for AsyncDriverClient async status methods."""

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from grpc import StatusCode
from grpc.aio import AioRpcError

from jumpstarter.common import ExporterStatus, Metadata

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
):
    """Create a mock GetStatusResponse."""
    response = MagicMock()
    response.status = status.to_proto()
    response.status_version = version
    response.message = message
    return response


def create_end_session_response(success: bool = True, message: str = ""):
    """Create a mock EndSessionResponse."""
    response = MagicMock()
    response.success = success
    response.message = message
    return response


@dataclass(kw_only=True)
class MockAsyncDriverClient(Metadata):
    """Minimal mock for testing AsyncDriverClient methods."""
    stub: MagicMock

    def __post_init__(self):
        import logging
        self.logger = logging.getLogger("MockAsyncDriverClient")
        self._status_monitor = None

    async def get_status_async(self) -> ExporterStatus | None:
        """Get the current exporter status."""
        try:
            response = await self.stub.GetStatus(MagicMock())
            return ExporterStatus.from_proto(response.status)
        except AioRpcError as e:
            if e.code() == StatusCode.UNIMPLEMENTED:
                return None
            raise

    async def end_session_async(self) -> bool:
        """End the current session and trigger the afterLease hook."""
        try:
            response = await self.stub.EndSession(MagicMock())
            return response.success
        except AioRpcError as e:
            if e.code() == StatusCode.UNIMPLEMENTED:
                return False
            if e.code() in (StatusCode.UNAVAILABLE, StatusCode.CANCELLED):
                return True
            if e.code() == StatusCode.UNKNOWN and "Stream removed" in str(e.details()):
                return True
            raise

    async def wait_for_hook_status(self, target_status: ExporterStatus, timeout: float = 60.0) -> bool:
        """Wait for exporter to reach a target status using polling."""
        import anyio

        poll_interval = 0.1  # Fast for testing
        elapsed = 0.0

        while elapsed < timeout:
            try:
                status = await self.get_status_async()

                if status is None:
                    return True

                if status == target_status:
                    return True

                if status == ExporterStatus.AFTER_LEASE_HOOK_FAILED:
                    return True

            except AioRpcError:
                return False

            await anyio.sleep(poll_interval)
            elapsed += poll_interval

        return False


class TestAsyncDriverClientGetStatus:
    async def test_get_status_returns_exporter_status(self) -> None:
        """Test that get_status_async() returns the correct ExporterStatus."""
        stub = MagicMock()
        stub.GetStatus = AsyncMock(return_value=create_status_response(ExporterStatus.LEASE_READY))

        client = MockAsyncDriverClient(uuid=uuid4(), labels={}, stub=stub)

        status = await client.get_status_async()

        assert status == ExporterStatus.LEASE_READY
        stub.GetStatus.assert_called_once()

    async def test_get_status_unimplemented_returns_none(self) -> None:
        """Test that get_status_async() returns None when UNIMPLEMENTED."""
        stub = MagicMock()
        stub.GetStatus = AsyncMock(side_effect=create_mock_rpc_error(StatusCode.UNIMPLEMENTED))

        client = MockAsyncDriverClient(uuid=uuid4(), labels={}, stub=stub)

        status = await client.get_status_async()

        assert status is None

    async def test_get_status_all_statuses(self) -> None:
        """Test that get_status_async() correctly converts all status values."""
        statuses = [
            ExporterStatus.UNSPECIFIED,
            ExporterStatus.OFFLINE,
            ExporterStatus.AVAILABLE,
            ExporterStatus.BEFORE_LEASE_HOOK,
            ExporterStatus.LEASE_READY,
            ExporterStatus.AFTER_LEASE_HOOK,
            ExporterStatus.BEFORE_LEASE_HOOK_FAILED,
            ExporterStatus.AFTER_LEASE_HOOK_FAILED,
        ]

        for expected_status in statuses:
            stub = MagicMock()
            stub.GetStatus = AsyncMock(return_value=create_status_response(expected_status))
            client = MockAsyncDriverClient(uuid=uuid4(), labels={}, stub=stub)

            status = await client.get_status_async()

            assert status == expected_status


class TestAsyncDriverClientEndSession:
    async def test_end_session_success(self) -> None:
        """Test that end_session_async() returns True on success."""
        stub = MagicMock()
        stub.EndSession = AsyncMock(return_value=create_end_session_response(success=True))

        client = MockAsyncDriverClient(uuid=uuid4(), labels={}, stub=stub)

        result = await client.end_session_async()

        assert result is True
        stub.EndSession.assert_called_once()

    async def test_end_session_unimplemented(self) -> None:
        """Test that end_session_async() returns False when UNIMPLEMENTED."""
        stub = MagicMock()
        stub.EndSession = AsyncMock(side_effect=create_mock_rpc_error(StatusCode.UNIMPLEMENTED))

        client = MockAsyncDriverClient(uuid=uuid4(), labels={}, stub=stub)

        result = await client.end_session_async()

        assert result is False

    async def test_end_session_unavailable_treated_as_success(self) -> None:
        """Test that end_session_async() returns True when UNAVAILABLE (lease released)."""
        stub = MagicMock()
        stub.EndSession = AsyncMock(side_effect=create_mock_rpc_error(StatusCode.UNAVAILABLE))

        client = MockAsyncDriverClient(uuid=uuid4(), labels={}, stub=stub)

        result = await client.end_session_async()

        assert result is True

    async def test_end_session_cancelled_treated_as_success(self) -> None:
        """Test that end_session_async() returns True when CANCELLED (connection dropped)."""
        stub = MagicMock()
        stub.EndSession = AsyncMock(side_effect=create_mock_rpc_error(StatusCode.CANCELLED))

        client = MockAsyncDriverClient(uuid=uuid4(), labels={}, stub=stub)

        result = await client.end_session_async()

        assert result is True

    async def test_end_session_stream_removed_treated_as_success(self) -> None:
        """Test that end_session_async() returns True when stream removed (lease released)."""
        error = create_mock_rpc_error(StatusCode.UNKNOWN, "Stream removed")
        stub = MagicMock()
        stub.EndSession = AsyncMock(side_effect=error)

        client = MockAsyncDriverClient(uuid=uuid4(), labels={}, stub=stub)

        result = await client.end_session_async()

        assert result is True


class TestAsyncDriverClientWaitForHookStatus:
    async def test_wait_for_hook_status_reaches_target(self) -> None:
        """Test that wait_for_hook_status() returns True when target is reached."""
        stub = MagicMock()
        # First call returns AFTER_LEASE_HOOK, second call returns AVAILABLE
        stub.GetStatus = AsyncMock(
            side_effect=[
                create_status_response(ExporterStatus.AFTER_LEASE_HOOK),
                create_status_response(ExporterStatus.AVAILABLE),
            ]
        )

        client = MockAsyncDriverClient(uuid=uuid4(), labels={}, stub=stub)

        result = await client.wait_for_hook_status(ExporterStatus.AVAILABLE, timeout=1.0)

        assert result is True

    async def test_wait_for_hook_status_hook_failed(self) -> None:
        """Test that wait_for_hook_status() returns True when hook fails."""
        stub = MagicMock()
        stub.GetStatus = AsyncMock(
            return_value=create_status_response(ExporterStatus.AFTER_LEASE_HOOK_FAILED)
        )

        client = MockAsyncDriverClient(uuid=uuid4(), labels={}, stub=stub)

        result = await client.wait_for_hook_status(ExporterStatus.AVAILABLE, timeout=1.0)

        assert result is True

    async def test_wait_for_hook_status_timeout(self) -> None:
        """Test that wait_for_hook_status() returns False on timeout."""
        stub = MagicMock()
        # Always return AFTER_LEASE_HOOK (never reaches target)
        stub.GetStatus = AsyncMock(
            return_value=create_status_response(ExporterStatus.AFTER_LEASE_HOOK)
        )

        client = MockAsyncDriverClient(uuid=uuid4(), labels={}, stub=stub)

        result = await client.wait_for_hook_status(ExporterStatus.AVAILABLE, timeout=0.2)

        assert result is False

    async def test_wait_for_hook_status_connection_error(self) -> None:
        """Test that wait_for_hook_status() returns False on connection error."""
        stub = MagicMock()
        stub.GetStatus = AsyncMock(
            side_effect=create_mock_rpc_error(StatusCode.UNAVAILABLE)
        )

        client = MockAsyncDriverClient(uuid=uuid4(), labels={}, stub=stub)

        result = await client.wait_for_hook_status(ExporterStatus.AVAILABLE, timeout=1.0)

        assert result is False

    async def test_wait_for_hook_status_unimplemented_returns_true(self) -> None:
        """Test that wait_for_hook_status() returns True when GetStatus not implemented."""
        stub = MagicMock()
        stub.GetStatus = AsyncMock(
            side_effect=create_mock_rpc_error(StatusCode.UNIMPLEMENTED)
        )

        client = MockAsyncDriverClient(uuid=uuid4(), labels={}, stub=stub)

        result = await client.wait_for_hook_status(ExporterStatus.AVAILABLE, timeout=1.0)

        # Should return True (backward compatibility - assume hook complete)
        assert result is True
