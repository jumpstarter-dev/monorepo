from contextlib import nullcontext
from typing import Callable
from unittest.mock import MagicMock, patch

import pytest

from jumpstarter.config.exporter import HookConfigV1Alpha1, HookInstanceConfigV1Alpha1
from jumpstarter.driver import Driver
from jumpstarter.exporter.hooks import HookExecutionError, HookExecutor

pytestmark = pytest.mark.anyio


class MockDriver(Driver):
    @classmethod
    def client(cls) -> str:
        return "test.MockClient"

    def close(self) -> None:
        pass

    def reset(self) -> None:
        pass


@pytest.fixture
def mock_device_factory() -> Callable[[], MockDriver]:
    def factory() -> MockDriver:
        return MockDriver()

    return factory


@pytest.fixture
def hook_config() -> HookConfigV1Alpha1:
    return HookConfigV1Alpha1(
        before_lease=HookInstanceConfigV1Alpha1(script="echo 'Pre-lease hook executed'", timeout=10),
        after_lease=HookInstanceConfigV1Alpha1(script="echo 'Post-lease hook executed'", timeout=10),
    )


@pytest.fixture
def lease_scope():
    from anyio import Event

    from jumpstarter.exporter.lease_context import LeaseContext

    lease_scope = LeaseContext(
        lease_name="test-lease-123",
        before_lease_hook=Event(),
        client_name="test-client",
    )
    # Add mock session to lease_scope
    mock_session = MagicMock()
    # Return a no-op context manager for context_log_source
    mock_session.context_log_source.return_value = nullcontext()
    lease_scope.session = mock_session
    lease_scope.socket_path = "/tmp/test_socket"
    return lease_scope


class TestHookExecutor:
    async def test_hook_executor_creation(self, hook_config, mock_device_factory) -> None:
        executor = HookExecutor(
            config=hook_config,
            device_factory=mock_device_factory,
        )

        assert executor.config == hook_config
        assert executor.device_factory == mock_device_factory

    async def test_empty_hook_execution(self, mock_device_factory, lease_scope) -> None:
        empty_config = HookConfigV1Alpha1()
        executor = HookExecutor(
            config=empty_config,
            device_factory=mock_device_factory,
        )

        # Both hooks should return None for empty/None commands
        assert await executor.execute_before_lease_hook(lease_scope) is None
        assert await executor.execute_after_lease_hook(lease_scope) is None

    async def test_successful_hook_execution(self, mock_device_factory, lease_scope) -> None:
        hook_config = HookConfigV1Alpha1(
            before_lease=HookInstanceConfigV1Alpha1(script="echo 'Pre-lease hook executed'", timeout=10),
        )
        executor = HookExecutor(config=hook_config, device_factory=mock_device_factory)
        result = await executor.execute_before_lease_hook(lease_scope)
        assert result is None

    async def test_failed_hook_execution(self, mock_device_factory, lease_scope) -> None:
        failed_config = HookConfigV1Alpha1(
            before_lease=HookInstanceConfigV1Alpha1(
                script="exit 1", timeout=10, on_failure="endLease"
            ),
        )
        executor = HookExecutor(config=failed_config, device_factory=mock_device_factory)

        with pytest.raises(HookExecutionError) as exc_info:
            await executor.execute_before_lease_hook(lease_scope)

        assert "exit code 1" in str(exc_info.value)
        assert exc_info.value.on_failure == "endLease"
        assert exc_info.value.hook_type == "before_lease"

    async def test_hook_timeout(self, mock_device_factory, lease_scope) -> None:
        timeout_config = HookConfigV1Alpha1(
            before_lease=HookInstanceConfigV1Alpha1(
                script="sleep 60", timeout=1, on_failure="exit"
            ),
        )
        executor = HookExecutor(config=timeout_config, device_factory=mock_device_factory)

        with pytest.raises(HookExecutionError) as exc_info:
            await executor.execute_before_lease_hook(lease_scope)

        assert "timed out after 1 seconds" in str(exc_info.value)
        assert exc_info.value.on_failure == "exit"

    async def test_hook_environment_variables(self, mock_device_factory, lease_scope) -> None:
        hook_config = HookConfigV1Alpha1(
            before_lease=HookInstanceConfigV1Alpha1(
                script="echo LEASE_NAME=$LEASE_NAME; echo CLIENT_NAME=$CLIENT_NAME",
                timeout=10
            ),
        )
        executor = HookExecutor(config=hook_config, device_factory=mock_device_factory)

        with patch("jumpstarter.exporter.hooks.logger") as mock_logger:
            await executor.execute_before_lease_hook(lease_scope)
            info_calls = [str(call) for call in mock_logger.info.call_args_list]
            assert any("LEASE_NAME=test-lease-123" in call for call in info_calls)
            assert any("CLIENT_NAME=test-client" in call for call in info_calls)

    async def test_real_time_output_logging(self, mock_device_factory, lease_scope) -> None:
        """Test that hook output is logged in real-time at INFO level."""
        hook_config = HookConfigV1Alpha1(
            before_lease=HookInstanceConfigV1Alpha1(
                script="echo 'Line 1'; echo 'Line 2'; echo 'Line 3'",
                timeout=10
            ),
        )
        executor = HookExecutor(config=hook_config, device_factory=mock_device_factory)

        with patch("jumpstarter.exporter.hooks.logger") as mock_logger:
            result = await executor.execute_before_lease_hook(lease_scope)

            assert result is None

            info_calls = [str(call) for call in mock_logger.info.call_args_list]
            assert any("Line 1" in call for call in info_calls)
            assert any("Line 2" in call for call in info_calls)
            assert any("Line 3" in call for call in info_calls)

    async def test_post_lease_hook_execution_on_completion(self, mock_device_factory, lease_scope) -> None:
        """Test that post-lease hook executes when called directly."""
        hook_config = HookConfigV1Alpha1(
            after_lease=HookInstanceConfigV1Alpha1(
                script="echo 'Post-lease cleanup completed'",
                timeout=10
            ),
        )
        executor = HookExecutor(config=hook_config, device_factory=mock_device_factory)

        with patch("jumpstarter.exporter.hooks.logger") as mock_logger:
            result = await executor.execute_after_lease_hook(lease_scope)

            assert result is None

            info_calls = [str(call) for call in mock_logger.info.call_args_list]
            assert any("Post-lease cleanup completed" in call for call in info_calls)

    async def test_hook_timeout_with_warn(self, mock_device_factory, lease_scope) -> None:
        """Test that hook succeeds when timeout occurs but on_failure='warn'."""
        hook_config = HookConfigV1Alpha1(
            before_lease=HookInstanceConfigV1Alpha1(
                script="sleep 60",
                timeout=1,
                on_failure="warn"
            ),
        )
        executor = HookExecutor(config=hook_config, device_factory=mock_device_factory)

        with patch("jumpstarter.exporter.hooks.logger") as mock_logger:
            result = await executor.execute_before_lease_hook(lease_scope)
            assert result is None
            # Verify WARNING log was created
            warning_calls = [str(call) for call in mock_logger.warning.call_args_list]
            assert any("on_failure=warn, continuing" in call for call in warning_calls)
