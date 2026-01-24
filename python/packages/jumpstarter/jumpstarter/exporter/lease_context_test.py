"""Tests for LeaseContext dataclass."""

from unittest.mock import MagicMock

import pytest
from anyio import Event

from jumpstarter.common import ExporterStatus
from jumpstarter.exporter.lease_context import LeaseContext

pytestmark = pytest.mark.anyio


class TestLeaseContextInitialization:
    def test_init_with_required_fields(self) -> None:
        """Test that LeaseContext can be created with required fields."""
        before_hook = Event()
        ctx = LeaseContext(lease_name="test-lease", before_lease_hook=before_hook)

        assert ctx.lease_name == "test-lease"
        assert ctx.before_lease_hook is before_hook
        assert ctx.session is None
        assert ctx.socket_path == ""
        assert ctx.hook_socket_path == ""
        assert ctx.client_name == ""
        assert ctx.current_status == ExporterStatus.AVAILABLE
        assert ctx.status_message == ""

    def test_init_validates_lease_name_non_empty(self) -> None:
        """Test that LeaseContext requires a non-empty lease_name."""
        before_hook = Event()
        with pytest.raises(AssertionError, match="non-empty lease_name"):
            LeaseContext(lease_name="", before_lease_hook=before_hook)

    def test_init_validates_before_lease_hook_present(self) -> None:
        """Test that LeaseContext requires before_lease_hook to be non-None."""
        with pytest.raises(AssertionError, match="before_lease_hook"):
            LeaseContext(lease_name="test-lease", before_lease_hook=None)  # type: ignore

    def test_default_events_created(self) -> None:
        """Test that default events are created properly."""
        before_hook = Event()
        ctx = LeaseContext(lease_name="test-lease", before_lease_hook=before_hook)

        assert ctx.end_session_requested is not None
        assert ctx.after_lease_hook_started is not None
        assert ctx.after_lease_hook_done is not None
        assert ctx.lease_ended is not None
        # Events should not be set by default
        assert not ctx.end_session_requested.is_set()
        assert not ctx.after_lease_hook_started.is_set()
        assert not ctx.after_lease_hook_done.is_set()
        assert not ctx.lease_ended.is_set()


class TestLeaseContextStateQueries:
    def test_is_ready_false_without_session(self) -> None:
        """Test that is_ready() returns False when session is None."""
        before_hook = Event()
        ctx = LeaseContext(lease_name="test-lease", before_lease_hook=before_hook)
        ctx.socket_path = "/tmp/socket"

        assert not ctx.is_ready()

    def test_is_ready_false_without_socket_path(self) -> None:
        """Test that is_ready() returns False when socket_path is empty."""
        before_hook = Event()
        ctx = LeaseContext(lease_name="test-lease", before_lease_hook=before_hook)
        ctx.session = MagicMock()

        assert not ctx.is_ready()

    def test_is_ready_true_with_both(self) -> None:
        """Test that is_ready() returns True when both session and socket_path are set."""
        before_hook = Event()
        ctx = LeaseContext(lease_name="test-lease", before_lease_hook=before_hook)
        ctx.session = MagicMock()
        ctx.socket_path = "/tmp/socket"

        assert ctx.is_ready()

    def test_is_active_true_with_lease_name(self) -> None:
        """Test that is_active() returns True when lease_name is non-empty."""
        before_hook = Event()
        ctx = LeaseContext(lease_name="active-lease", before_lease_hook=before_hook)

        assert ctx.is_active()

    def test_has_client_true_with_client_name(self) -> None:
        """Test that has_client() returns True when client_name is set."""
        before_hook = Event()
        ctx = LeaseContext(
            lease_name="test-lease",
            before_lease_hook=before_hook,
            client_name="my-client",
        )

        assert ctx.has_client()

    def test_has_client_false_without_client_name(self) -> None:
        """Test that has_client() returns False when client_name is empty."""
        before_hook = Event()
        ctx = LeaseContext(lease_name="test-lease", before_lease_hook=before_hook)

        assert not ctx.has_client()


class TestLeaseContextClientManagement:
    def test_update_client_sets_name(self) -> None:
        """Test that update_client() sets the client name."""
        before_hook = Event()
        ctx = LeaseContext(lease_name="test-lease", before_lease_hook=before_hook)

        ctx.update_client("new-client")

        assert ctx.client_name == "new-client"
        assert ctx.has_client()

    def test_clear_client_removes_name(self) -> None:
        """Test that clear_client() removes the client name."""
        before_hook = Event()
        ctx = LeaseContext(
            lease_name="test-lease",
            before_lease_hook=before_hook,
            client_name="my-client",
        )

        ctx.clear_client()

        assert ctx.client_name == ""
        assert not ctx.has_client()


class TestLeaseContextStatusUpdates:
    def test_update_status_stores_status(self) -> None:
        """Test that update_status() stores the status in the context."""
        before_hook = Event()
        ctx = LeaseContext(lease_name="test-lease", before_lease_hook=before_hook)

        ctx.update_status(ExporterStatus.LEASE_READY, "ready to go")

        assert ctx.current_status == ExporterStatus.LEASE_READY
        assert ctx.status_message == "ready to go"

    def test_update_status_propagates_to_session(self) -> None:
        """Test that update_status() propagates status to session when present."""
        before_hook = Event()
        ctx = LeaseContext(lease_name="test-lease", before_lease_hook=before_hook)
        mock_session = MagicMock()
        ctx.session = mock_session

        ctx.update_status(ExporterStatus.BEFORE_LEASE_HOOK, "running hook")

        mock_session.update_status.assert_called_once_with(
            ExporterStatus.BEFORE_LEASE_HOOK, "running hook"
        )

    def test_update_status_without_session_no_error(self) -> None:
        """Test that update_status() works without session (no error)."""
        before_hook = Event()
        ctx = LeaseContext(lease_name="test-lease", before_lease_hook=before_hook)

        # Should not raise any exception
        ctx.update_status(ExporterStatus.AVAILABLE, "available")

        assert ctx.current_status == ExporterStatus.AVAILABLE


class TestLeaseContextDriversReady:
    def test_drivers_ready_false_when_hook_not_set(self) -> None:
        """Test that drivers_ready() returns False when hook event is not set."""
        before_hook = Event()
        ctx = LeaseContext(lease_name="test-lease", before_lease_hook=before_hook)

        assert not ctx.drivers_ready()

    def test_drivers_ready_true_when_hook_set(self) -> None:
        """Test that drivers_ready() returns True when hook event is set."""
        before_hook = Event()
        before_hook.set()
        ctx = LeaseContext(lease_name="test-lease", before_lease_hook=before_hook)

        assert ctx.drivers_ready()

    async def test_wait_for_drivers_blocks_until_set(self) -> None:
        """Test that wait_for_drivers() blocks until hook event is set."""
        import anyio

        before_hook = Event()
        ctx = LeaseContext(lease_name="test-lease", before_lease_hook=before_hook)

        # Set the event after a short delay
        async def set_after_delay():
            await anyio.sleep(0.05)
            before_hook.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(set_after_delay)
            await ctx.wait_for_drivers()

        assert ctx.drivers_ready()
