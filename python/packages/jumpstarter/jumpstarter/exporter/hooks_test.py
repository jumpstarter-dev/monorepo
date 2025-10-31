import asyncio
from unittest.mock import AsyncMock, Mock, call, patch

import pytest

from jumpstarter.config.env import JMP_DRIVERS_ALLOW, JUMPSTARTER_HOST
from jumpstarter.config.exporter import HookConfigV1Alpha1, HookInstanceConfigV1Alpha1
from jumpstarter.driver import Driver
from jumpstarter.exporter.hooks import HookContext, HookExecutionError, HookExecutor

pytestmark = pytest.mark.anyio


class MockDriver(Driver):
    @classmethod
    def client(cls) -> str:
        return "test.MockClient"

    def close(self):
        pass

    def reset(self):
        pass


@pytest.fixture
def mock_device_factory():
    def factory():
        return MockDriver()

    return factory


@pytest.fixture
def hook_config():
    return HookConfigV1Alpha1(
        before_lease=HookInstanceConfigV1Alpha1(script="echo 'Pre-lease hook executed'", timeout=10),
        after_lease=HookInstanceConfigV1Alpha1(script="echo 'Post-lease hook executed'", timeout=10),
    )


@pytest.fixture
def hook_context():
    return HookContext(
        lease_name="test-lease-123",
        client_name="test-client",
        lease_duration="30m",
        exporter_name="test-exporter",
        exporter_namespace="default",
    )


class TestHookExecutor:
    async def test_hook_executor_creation(self, hook_config, mock_device_factory):
        executor = HookExecutor(
            config=hook_config,
            device_factory=mock_device_factory,
        )

        assert executor.config == hook_config
        assert executor.device_factory == mock_device_factory

    async def test_empty_hook_execution(self, mock_device_factory, hook_context):
        empty_config = HookConfigV1Alpha1()
        executor = HookExecutor(
            config=empty_config,
            device_factory=mock_device_factory,
        )

        # Both hooks should return True for empty/None commands
        assert await executor.execute_before_lease_hook(hook_context) is True
        assert await executor.execute_after_lease_hook(hook_context) is True

    async def test_successful_hook_execution(self, mock_device_factory, hook_context):
        hook_config = HookConfigV1Alpha1(
            before_lease=HookInstanceConfigV1Alpha1(script="echo 'Pre-lease hook executed'", timeout=10),
        )
        # Mock the Session and serve_unix_async
        with patch("jumpstarter.exporter.hooks.Session") as mock_session_class:
            mock_session = Mock()
            mock_session_class.return_value.__enter__.return_value = mock_session

            # Mock the async context manager for serve_unix_async
            mock_session.serve_unix_async.return_value.__aenter__ = AsyncMock(return_value="/tmp/test_socket")
            mock_session.serve_unix_async.return_value.__aexit__ = AsyncMock(return_value=None)

            # Mock asyncio.create_subprocess_shell to simulate successful execution
            mock_process = AsyncMock()
            mock_process.returncode = 0
            # Mock stdout.readline to simulate line-by-line output
            mock_process.stdout.readline.side_effect = [
                b"Pre-lease hook executed\n",
                b"",  # EOF
            ]
            mock_process.wait = AsyncMock(return_value=None)

            with patch("asyncio.create_subprocess_shell", return_value=mock_process) as mock_subprocess:
                executor = HookExecutor(
                    config=hook_config,
                    device_factory=mock_device_factory,
                )

                result = await executor.execute_before_lease_hook(hook_context)

                assert result is True

                # Verify subprocess was called with correct environment
                mock_subprocess.assert_called_once()
                call_args = mock_subprocess.call_args
                command = call_args[0][0]
                env = call_args[1]["env"]

                assert command == "echo 'Pre-lease hook executed'"
                assert JUMPSTARTER_HOST in env
                assert env[JUMPSTARTER_HOST] == "/tmp/test_socket"
                assert env[JMP_DRIVERS_ALLOW] == "UNSAFE"
                assert env["LEASE_NAME"] == "test-lease-123"
                assert env["CLIENT_NAME"] == "test-client"

    async def test_failed_hook_execution(self, mock_device_factory, hook_context):
        failed_config = HookConfigV1Alpha1(
            before_lease=HookInstanceConfigV1Alpha1(
                script="exit 1", timeout=10, on_failure="block"
            ),  # Command that will fail with on_failure="block"
        )

        with patch("jumpstarter.exporter.hooks.Session") as mock_session_class:
            mock_session = Mock()
            mock_session_class.return_value.__enter__.return_value = mock_session

            mock_session.serve_unix_async.return_value.__aenter__ = AsyncMock(return_value="/tmp/test_socket")
            mock_session.serve_unix_async.return_value.__aexit__ = AsyncMock(return_value=None)

            # Mock failed process
            mock_process = AsyncMock()
            mock_process.returncode = 1
            # Mock stdout.readline for failed process
            mock_process.stdout.readline.side_effect = [
                b"Command failed\n",
                b"",  # EOF
            ]
            mock_process.wait = AsyncMock(return_value=None)

            with patch("asyncio.create_subprocess_shell", return_value=mock_process):
                executor = HookExecutor(
                    config=failed_config,
                    device_factory=mock_device_factory,
                )

                # Should raise HookExecutionError since on_failure="block"
                with pytest.raises(HookExecutionError, match="expected exit code 0, got 1"):
                    await executor.execute_before_lease_hook(hook_context)

    async def test_hook_timeout(self, mock_device_factory, hook_context):
        timeout_config = HookConfigV1Alpha1(
            before_lease=HookInstanceConfigV1Alpha1(
                script="sleep 60", timeout=1, on_failure="block"
            ),  # Command that will timeout with on_failure="block"
        )

        with patch("jumpstarter.exporter.hooks.Session") as mock_session_class:
            mock_session = Mock()
            mock_session_class.return_value.__enter__.return_value = mock_session

            mock_session.serve_unix_async.return_value.__aenter__ = AsyncMock(return_value="/tmp/test_socket")
            mock_session.serve_unix_async.return_value.__aexit__ = AsyncMock(return_value=None)

            # Mock process that times out
            mock_process = AsyncMock()
            mock_process.terminate.return_value = None
            mock_process.wait.return_value = None

            with (
                patch("asyncio.create_subprocess_shell", return_value=mock_process),
                patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()),
            ):
                executor = HookExecutor(
                    config=timeout_config,
                    device_factory=mock_device_factory,
                )

                # Should raise HookExecutionError since on_failure="block"
                with pytest.raises(HookExecutionError, match="timed out after 1 seconds"):
                    await executor.execute_before_lease_hook(hook_context)

                mock_process.terminate.assert_called_once()

    async def test_hook_environment_variables(self, mock_device_factory, hook_context):
        hook_config = HookConfigV1Alpha1(
            before_lease=HookInstanceConfigV1Alpha1(script="echo 'Pre-lease hook executed'", timeout=10),
        )
        with patch("jumpstarter.exporter.hooks.Session") as mock_session_class:
            mock_session = Mock()
            mock_session_class.return_value.__enter__.return_value = mock_session

            mock_session.serve_unix_async.return_value.__aenter__ = AsyncMock(return_value="/tmp/test_socket")
            mock_session.serve_unix_async.return_value.__aexit__ = AsyncMock(return_value=None)

            mock_process = AsyncMock()
            mock_process.returncode = 0
            # Mock stdout.readline for environment test
            mock_process.stdout.readline.side_effect = [
                b"",  # EOF (no output)
            ]
            mock_process.wait = AsyncMock(return_value=None)

            with patch("asyncio.create_subprocess_shell", return_value=mock_process) as mock_subprocess:
                executor = HookExecutor(
                    config=hook_config,
                    device_factory=mock_device_factory,
                )

                await executor.execute_before_lease_hook(hook_context)

                # Check that all expected environment variables are set
                call_args = mock_subprocess.call_args
                env = call_args[1]["env"]

                assert env["LEASE_NAME"] == "test-lease-123"
                assert env["CLIENT_NAME"] == "test-client"
                assert env["LEASE_DURATION"] == "30m"
                assert env["EXPORTER_NAME"] == "test-exporter"
                assert env["EXPORTER_NAMESPACE"] == "default"
                assert env[JUMPSTARTER_HOST] == "/tmp/test_socket"
                assert env[JMP_DRIVERS_ALLOW] == "UNSAFE"

    async def test_real_time_output_logging(self, mock_device_factory, hook_context):
        """Test that hook output is logged in real-time at INFO level."""
        hook_config = HookConfigV1Alpha1(
            before_lease=HookInstanceConfigV1Alpha1(
                script="echo 'Line 1'; echo 'Line 2'; echo 'Line 3'", timeout=10
            ),
        )

        with patch("jumpstarter.exporter.hooks.Session") as mock_session_class:
            mock_session = Mock()
            mock_session_class.return_value.__enter__.return_value = mock_session

            mock_session.serve_unix_async.return_value.__aenter__ = AsyncMock(return_value="/tmp/test_socket")
            mock_session.serve_unix_async.return_value.__aexit__ = AsyncMock(return_value=None)

            mock_process = AsyncMock()
            mock_process.returncode = 0
            # Mock multiple lines of output to verify streaming
            mock_process.stdout.readline.side_effect = [
                b"Line 1\n",
                b"Line 2\n",
                b"Line 3\n",
                b"",  # EOF
            ]
            mock_process.wait = AsyncMock(return_value=None)

            # Mock the logger to capture log calls
            with (
                patch("jumpstarter.exporter.hooks.logger") as mock_logger,
                patch("asyncio.create_subprocess_shell", return_value=mock_process),
            ):
                executor = HookExecutor(
                    config=hook_config,
                    device_factory=mock_device_factory,
                )

                result = await executor.execute_before_lease_hook(hook_context)

                assert result is True

                # Verify that output lines were logged in real-time at INFO level
                expected_calls = [
                    call("Executing before-lease hook for lease %s", "test-lease-123"),
                    call("Executing hook: %s", "echo 'Line 1'; echo 'Line 2'; echo 'Line 3'"),
                    call("Hook executed successfully with exit code %d", 0),
                ]
                mock_logger.info.assert_has_calls(expected_calls, any_order=False)

    async def test_post_lease_hook_execution_on_completion(self, mock_device_factory, hook_context):
        """Test that post-lease hook executes when called directly."""
        hook_config = HookConfigV1Alpha1(
            after_lease=HookInstanceConfigV1Alpha1(script="echo 'Post-lease cleanup completed'", timeout=10),
        )

        with patch("jumpstarter.exporter.hooks.Session") as mock_session_class:
            mock_session = Mock()
            mock_session_class.return_value.__enter__.return_value = mock_session

            mock_session.serve_unix_async.return_value.__aenter__ = AsyncMock(return_value="/tmp/test_socket")
            mock_session.serve_unix_async.return_value.__aexit__ = AsyncMock(return_value=None)

            mock_process = AsyncMock()
            mock_process.returncode = 0
            # Mock post-lease hook output
            mock_process.stdout.readline.side_effect = [
                b"Post-lease cleanup completed\n",
                b"",  # EOF
            ]
            mock_process.wait = AsyncMock(return_value=None)

            # Mock the logger to capture log calls
            with (
                patch("jumpstarter.exporter.hooks.logger") as mock_logger,
                patch("asyncio.create_subprocess_shell", return_value=mock_process),
            ):
                executor = HookExecutor(
                    config=hook_config,
                    device_factory=mock_device_factory,
                )

                result = await executor.execute_after_lease_hook(hook_context)

                assert result is True

                # Verify that post-lease hook output was logged
                expected_calls = [
                    call("Executing after-lease hook for lease %s", "test-lease-123"),
                    call("Executing hook: %s", "echo 'Post-lease cleanup completed'"),
                    call("Hook executed successfully with exit code %d", 0),
                ]
                mock_logger.info.assert_has_calls(expected_calls, any_order=False)

    async def test_hook_exit_code_matching_success(self, mock_device_factory, hook_context):
        """Test that hook succeeds when exit code matches expected value."""
        hook_config = HookConfigV1Alpha1(
            before_lease=HookInstanceConfigV1Alpha1(script="exit 0", timeout=10, exit_code=0),
        )

        with patch("jumpstarter.exporter.hooks.Session") as mock_session_class:
            mock_session = Mock()
            mock_session_class.return_value.__enter__.return_value = mock_session
            mock_session.serve_unix_async.return_value.__aenter__ = AsyncMock(return_value="/tmp/test_socket")
            mock_session.serve_unix_async.return_value.__aexit__ = AsyncMock(return_value=None)

            mock_process = AsyncMock()
            mock_process.returncode = 0
            mock_process.stdout.readline.side_effect = [b""]
            mock_process.wait = AsyncMock(return_value=None)

            with patch("asyncio.create_subprocess_shell", return_value=mock_process):
                executor = HookExecutor(config=hook_config, device_factory=mock_device_factory)
                result = await executor.execute_before_lease_hook(hook_context)
                assert result is True

    async def test_hook_exit_code_matching_custom(self, mock_device_factory, hook_context):
        """Test that hook succeeds when exit code matches custom expected value."""
        hook_config = HookConfigV1Alpha1(
            before_lease=HookInstanceConfigV1Alpha1(script="exit 42", timeout=10, exit_code=42),
        )

        with patch("jumpstarter.exporter.hooks.Session") as mock_session_class:
            mock_session = Mock()
            mock_session_class.return_value.__enter__.return_value = mock_session
            mock_session.serve_unix_async.return_value.__aenter__ = AsyncMock(return_value="/tmp/test_socket")
            mock_session.serve_unix_async.return_value.__aexit__ = AsyncMock(return_value=None)

            mock_process = AsyncMock()
            mock_process.returncode = 42
            mock_process.stdout.readline.side_effect = [b""]
            mock_process.wait = AsyncMock(return_value=None)

            with patch("asyncio.create_subprocess_shell", return_value=mock_process):
                executor = HookExecutor(config=hook_config, device_factory=mock_device_factory)
                result = await executor.execute_before_lease_hook(hook_context)
                assert result is True

    async def test_hook_exit_code_mismatch_pass(self, mock_device_factory, hook_context):
        """Test that hook succeeds when exit code mismatches but on_failure='pass'."""
        hook_config = HookConfigV1Alpha1(
            before_lease=HookInstanceConfigV1Alpha1(script="exit 1", timeout=10, exit_code=0, on_failure="pass"),
        )

        with patch("jumpstarter.exporter.hooks.Session") as mock_session_class:
            mock_session = Mock()
            mock_session_class.return_value.__enter__.return_value = mock_session
            mock_session.serve_unix_async.return_value.__aenter__ = AsyncMock(return_value="/tmp/test_socket")
            mock_session.serve_unix_async.return_value.__aexit__ = AsyncMock(return_value=None)

            mock_process = AsyncMock()
            mock_process.returncode = 1
            mock_process.stdout.readline.side_effect = [b""]
            mock_process.wait = AsyncMock(return_value=None)

            with (
                patch("asyncio.create_subprocess_shell", return_value=mock_process),
                patch("jumpstarter.exporter.hooks.logger") as mock_logger,
            ):
                executor = HookExecutor(config=hook_config, device_factory=mock_device_factory)
                result = await executor.execute_before_lease_hook(hook_context)
                assert result is True
                # Verify INFO log was created
                mock_logger.info.assert_any_call(
                    "Hook failed: expected exit code 0, got 1 (on_failure=pass, continuing)"
                )

    async def test_hook_exit_code_mismatch_warn(self, mock_device_factory, hook_context):
        """Test that hook succeeds when exit code mismatches but on_failure='warn'."""
        hook_config = HookConfigV1Alpha1(
            before_lease=HookInstanceConfigV1Alpha1(script="exit 1", timeout=10, exit_code=0, on_failure="warn"),
        )

        with patch("jumpstarter.exporter.hooks.Session") as mock_session_class:
            mock_session = Mock()
            mock_session_class.return_value.__enter__.return_value = mock_session
            mock_session.serve_unix_async.return_value.__aenter__ = AsyncMock(return_value="/tmp/test_socket")
            mock_session.serve_unix_async.return_value.__aexit__ = AsyncMock(return_value=None)

            mock_process = AsyncMock()
            mock_process.returncode = 1
            mock_process.stdout.readline.side_effect = [b""]
            mock_process.wait = AsyncMock(return_value=None)

            with (
                patch("asyncio.create_subprocess_shell", return_value=mock_process),
                patch("jumpstarter.exporter.hooks.logger") as mock_logger,
            ):
                executor = HookExecutor(config=hook_config, device_factory=mock_device_factory)
                result = await executor.execute_before_lease_hook(hook_context)
                assert result is True
                # Verify WARNING log was created
                mock_logger.warning.assert_any_call(
                    "Hook failed: expected exit code 0, got 1 (on_failure=warn, continuing)"
                )

    async def test_hook_exit_code_mismatch_block(self, mock_device_factory, hook_context):
        """Test that hook raises exception when exit code mismatches and on_failure='block'."""
        hook_config = HookConfigV1Alpha1(
            before_lease=HookInstanceConfigV1Alpha1(script="exit 1", timeout=10, exit_code=0, on_failure="block"),
        )

        with patch("jumpstarter.exporter.hooks.Session") as mock_session_class:
            mock_session = Mock()
            mock_session_class.return_value.__enter__.return_value = mock_session
            mock_session.serve_unix_async.return_value.__aenter__ = AsyncMock(return_value="/tmp/test_socket")
            mock_session.serve_unix_async.return_value.__aexit__ = AsyncMock(return_value=None)

            mock_process = AsyncMock()
            mock_process.returncode = 1
            mock_process.stdout.readline.side_effect = [b""]
            mock_process.wait = AsyncMock(return_value=None)

            with patch("asyncio.create_subprocess_shell", return_value=mock_process):
                executor = HookExecutor(config=hook_config, device_factory=mock_device_factory)
                with pytest.raises(HookExecutionError, match="expected exit code 0, got 1"):
                    await executor.execute_before_lease_hook(hook_context)

    async def test_hook_timeout_with_pass(self, mock_device_factory, hook_context):
        """Test that hook succeeds when timeout occurs but on_failure='pass'."""
        hook_config = HookConfigV1Alpha1(
            before_lease=HookInstanceConfigV1Alpha1(script="sleep 60", timeout=1, on_failure="pass"),
        )

        with patch("jumpstarter.exporter.hooks.Session") as mock_session_class:
            mock_session = Mock()
            mock_session_class.return_value.__enter__.return_value = mock_session
            mock_session.serve_unix_async.return_value.__aenter__ = AsyncMock(return_value="/tmp/test_socket")
            mock_session.serve_unix_async.return_value.__aexit__ = AsyncMock(return_value=None)

            mock_process = AsyncMock()
            mock_process.terminate = AsyncMock(return_value=None)
            mock_process.wait = AsyncMock(return_value=None)

            with (
                patch("asyncio.create_subprocess_shell", return_value=mock_process),
                patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()),
                patch("jumpstarter.exporter.hooks.logger") as mock_logger,
            ):
                executor = HookExecutor(config=hook_config, device_factory=mock_device_factory)
                result = await executor.execute_before_lease_hook(hook_context)
                assert result is True
                # Verify INFO log was created
                assert any("on_failure=pass, continuing" in str(call) for call in mock_logger.info.call_args_list)

    async def test_hook_timeout_with_warn(self, mock_device_factory, hook_context):
        """Test that hook succeeds when timeout occurs but on_failure='warn'."""
        hook_config = HookConfigV1Alpha1(
            before_lease=HookInstanceConfigV1Alpha1(script="sleep 60", timeout=1, on_failure="warn"),
        )

        with patch("jumpstarter.exporter.hooks.Session") as mock_session_class:
            mock_session = Mock()
            mock_session_class.return_value.__enter__.return_value = mock_session
            mock_session.serve_unix_async.return_value.__aenter__ = AsyncMock(return_value="/tmp/test_socket")
            mock_session.serve_unix_async.return_value.__aexit__ = AsyncMock(return_value=None)

            mock_process = AsyncMock()
            mock_process.terminate = AsyncMock(return_value=None)
            mock_process.wait = AsyncMock(return_value=None)

            with (
                patch("asyncio.create_subprocess_shell", return_value=mock_process),
                patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()),
                patch("jumpstarter.exporter.hooks.logger") as mock_logger,
            ):
                executor = HookExecutor(config=hook_config, device_factory=mock_device_factory)
                result = await executor.execute_before_lease_hook(hook_context)
                assert result is True
                # Verify WARNING log was created
                assert any("on_failure=warn, continuing" in str(call) for call in mock_logger.warning.call_args_list)
