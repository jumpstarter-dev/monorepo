"""Lifecycle hooks for Jumpstarter exporters."""

import logging
import os
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Literal

import anyio

from jumpstarter.common import ExporterStatus, LogSource
from jumpstarter.config.env import JMP_DRIVERS_ALLOW, JUMPSTARTER_HOST
from jumpstarter.config.exporter import HookConfigV1Alpha1, HookInstanceConfigV1Alpha1
from jumpstarter.exporter.session import Session

if TYPE_CHECKING:
    from jumpstarter.driver import Driver
    from jumpstarter.exporter.lease_context import LeaseContext

logger = logging.getLogger(__name__)


@dataclass
class HookExecutionError(Exception):
    """Raised when a hook fails and on_failure is set to 'endLease' or 'exit'.

    Attributes:
        message: Error message describing the failure
        on_failure: The on_failure mode that triggered this error ('endLease' or 'exit')
        hook_type: The type of hook that failed ('before_lease' or 'after_lease')
    """

    message: str
    on_failure: Literal["endLease", "exit"]
    hook_type: Literal["before_lease", "after_lease"]

    def __str__(self) -> str:
        return self.message

    def should_shutdown_exporter(self) -> bool:
        """Returns True if the exporter should be shut down entirely."""
        return self.on_failure == "exit"

    def should_end_lease(self) -> bool:
        """Returns True if the lease should be ended."""
        return self.on_failure in ("endLease", "exit")


@dataclass(kw_only=True)
class HookExecutor:
    """Executes lifecycle hooks with access to the j CLI."""

    config: HookConfigV1Alpha1
    device_factory: Callable[[], "Driver"]

    def _create_hook_env(self, lease_scope: "LeaseContext") -> dict[str, str]:
        """Create standardized hook environment variables.

        Args:
            lease_scope: LeaseScope containing lease metadata and socket path

        Returns:
            Dictionary of environment variables for hook execution
        """
        hook_env = os.environ.copy()
        hook_env.update(
            {
                JUMPSTARTER_HOST: str(lease_scope.socket_path),
                JMP_DRIVERS_ALLOW: "UNSAFE",  # Allow all drivers for local access
                "LEASE_NAME": lease_scope.lease_name,
                "CLIENT_NAME": lease_scope.client_name,
            }
        )
        return hook_env

    async def _execute_hook(
        self,
        hook_config: HookInstanceConfigV1Alpha1,
        lease_scope: "LeaseContext",
        log_source: LogSource,
    ) -> None:
        """Execute a single hook command.

        Args:
            hook_config: Hook configuration including script, timeout, and on_failure
            lease_scope: LeaseScope containing lease metadata and session
            log_source: Log source for hook output
        """
        command = hook_config.script
        if not command or not command.strip():
            logger.debug("Hook command is empty, skipping")
            return

        logger.debug("Executing hook: %s", command.strip().split("\n")[0][:100])

        # Determine hook type from log source
        hook_type = "before_lease" if log_source == LogSource.BEFORE_LEASE_HOOK else "after_lease"

        # Use existing session from lease_scope
        hook_env = self._create_hook_env(lease_scope)

        return await self._execute_hook_process(
            hook_config, lease_scope, log_source, hook_env, lease_scope.session, hook_type
        )

    def _handle_hook_failure(
        self,
        error_msg: str,
        on_failure: Literal["warn", "endLease", "exit"],
        hook_type: Literal["before_lease", "after_lease"],
        cause: Exception | None = None,
    ) -> None:
        """Handle hook failure according to on_failure setting.

        Args:
            error_msg: Error message describing the failure
            on_failure: The on_failure mode ('warn', 'endLease', or 'exit')
            hook_type: The type of hook that failed
            cause: Optional exception that caused the failure

        Raises:
            HookExecutionError: If on_failure is 'endLease' or 'exit'
        """
        if on_failure == "warn":
            logger.warning("%s (on_failure=warn, continuing)", error_msg)
            return

        logger.error("%s (on_failure=%s, raising exception)", error_msg, on_failure)

        error = HookExecutionError(
            message=error_msg,
            on_failure=on_failure,
            hook_type=hook_type,
        )

        # Properly handle exception chaining
        if cause is not None:
            raise error from cause
        else:
            raise error

    async def _execute_hook_process(
        self,
        hook_config: HookInstanceConfigV1Alpha1,
        lease_scope: "LeaseContext",
        log_source: LogSource,
        hook_env: dict[str, str],
        logging_session: Session,
        hook_type: Literal["before_lease", "after_lease"],
    ) -> None:
        """Execute the hook process with the given environment and logging session.

        Uses subprocess with a PTY to force line buffering in the subprocess,
        ensuring logs stream in real-time rather than being block-buffered.
        """
        import pty
        import subprocess

        command = hook_config.script
        timeout = hook_config.timeout
        on_failure = hook_config.on_failure

        # Exception handling
        error_msg: str | None = None
        cause: Exception | None = None
        timed_out = False

        # Route hook output logs to the client via the session's log stream
        with logging_session.context_log_source(__name__, log_source):
            # Create a PTY pair - this forces line buffering in the subprocess
            logger.info("Starting hook subprocess...")
            master_fd, slave_fd = pty.openpty()

            # Track which fds are still open (use list for mutability in nested scope)
            fds_open = {"master": True, "slave": True}

            process: subprocess.Popen | None = None
            try:
                # Use subprocess.Popen with the PTY slave as stdin/stdout/stderr
                # This avoids the issues with os.fork() in async contexts
                process = subprocess.Popen(
                    ["/bin/sh", "-c", command],
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    env=hook_env,
                    start_new_session=True,  # Equivalent to os.setsid()
                )
                # Close slave in parent - subprocess has it now
                os.close(slave_fd)
                fds_open["slave"] = False

                output_lines: list[str] = []

                # Set master fd to non-blocking mode
                import fcntl
                flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
                fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

                async def read_pty_output() -> None:
                    """Read from PTY master fd line by line using non-blocking I/O."""
                    buffer = b""
                    while fds_open["master"]:
                        try:
                            # Wait for fd to be readable with timeout
                            with anyio.move_on_after(0.1):
                                await anyio.wait_readable(master_fd)

                            # Read available data (non-blocking)
                            try:
                                chunk = os.read(master_fd, 4096)
                                if not chunk:
                                    # EOF
                                    break
                                buffer += chunk
                            except BlockingIOError:
                                # No data available right now, continue loop
                                continue
                            except OSError:
                                # PTY closed or error
                                break

                            # Process complete lines
                            while b"\n" in buffer:
                                line, buffer = buffer.split(b"\n", 1)
                                line_decoded = line.decode(errors="replace").rstrip()
                                if line_decoded:
                                    output_lines.append(line_decoded)
                                    logger.info("%s", line_decoded)

                        except OSError:
                            # PTY closed or read error
                            break

                    # Handle any remaining data without newline
                    if buffer:
                        line_decoded = buffer.decode(errors="replace").rstrip()
                        if line_decoded:
                            output_lines.append(line_decoded)
                            logger.info("%s", line_decoded)

                async def wait_for_process() -> int:
                    """Wait for the subprocess to complete."""
                    return await anyio.to_thread.run_sync(
                        process.wait,
                        abandon_on_cancel=True
                    )

                # Use move_on_after for timeout
                returncode: int | None = None
                with anyio.move_on_after(timeout) as cancel_scope:
                    # Run output reading and process waiting concurrently
                    async with anyio.create_task_group() as tg:
                        tg.start_soon(read_pty_output)
                        returncode = await wait_for_process()
                        # Give a brief moment for any final output to be read
                        await anyio.sleep(0.2)
                        # Signal the read task to stop by marking fd as closed
                        # The read task checks fds_open["master"] in its loop
                        fds_open["master"] = False
                        # The task group will wait for read_pty_output to complete
                        # after it sees fds_open["master"] is False on next iteration

                if cancel_scope.cancelled_caught:
                    timed_out = True
                    error_msg = f"Hook timed out after {timeout} seconds"
                    logger.error(error_msg)
                    # Terminate the process
                    if process and process.poll() is None:
                        process.terminate()
                        # Give it a moment to terminate gracefully
                        try:
                            with anyio.move_on_after(5):
                                await anyio.to_thread.run_sync(process.wait)
                        except Exception:
                            pass
                        # Force kill if still running
                        if process.poll() is None:
                            process.kill()
                            try:
                                process.wait()
                            except Exception:
                                pass

                elif returncode == 0:
                    logger.info("Hook executed successfully")
                    return
                else:
                    error_msg = f"Hook failed with exit code {returncode}"

            except Exception as e:
                error_msg = f"Error executing hook: {e}"
                cause = e
                logger.error(error_msg, exc_info=True)
            finally:
                # Clean up the file descriptors (always close, fds_open tracks logical state)
                try:
                    os.close(master_fd)
                except OSError:
                    pass
                try:
                    os.close(slave_fd)
                except OSError:
                    pass

        # Handle failure if one occurred
        if error_msg is not None:
            # For timeout, create a TimeoutError as the cause
            if timed_out and cause is None:
                cause = TimeoutError(error_msg)
            self._handle_hook_failure(error_msg, on_failure, hook_type, cause)

    async def execute_before_lease_hook(self, lease_scope: "LeaseContext") -> None:
        """Execute the before-lease hook.

        Args:
            lease_scope: LeaseScope with lease metadata and session

        Raises:
            HookExecutionError: If hook fails and on_failure is set to 'endLease' or 'exit'
        """
        if not self.config.before_lease:
            logger.debug("No before-lease hook configured")
            return

        logger.info("Executing before-lease hook for lease %s", lease_scope.lease_name)
        await self._execute_hook(
            self.config.before_lease,
            lease_scope,
            LogSource.BEFORE_LEASE_HOOK,
        )

    async def execute_after_lease_hook(self, lease_scope: "LeaseContext") -> None:
        """Execute the after-lease hook.

        Args:
            lease_scope: LeaseScope with lease metadata and session

        Raises:
            HookExecutionError: If hook fails and on_failure is set to 'endLease' or 'exit'
        """
        if not self.config.after_lease:
            logger.debug("No after-lease hook configured")
            return

        logger.info("Executing after-lease hook for lease %s", lease_scope.lease_name)
        await self._execute_hook(
            self.config.after_lease,
            lease_scope,
            LogSource.AFTER_LEASE_HOOK,
        )

    async def run_before_lease_hook(
        self,
        lease_scope: "LeaseContext",
        report_status: Callable[["ExporterStatus", str], Awaitable[None]],
        shutdown: Callable[..., None],
    ) -> None:
        """Execute before-lease hook with full orchestration.

        This method handles the complete lifecycle of running a before-lease hook:
        - Waits for the lease scope to be ready (session/socket populated)
        - Reports status changes via the provided callback
        - Sets up the hook executor with the session for logging
        - Executes the hook and handles errors
        - Always signals the before_lease_hook event to unblock connections

        Args:
            lease_scope: LeaseScope containing session, socket_path, and sync event
            report_status: Async callback to report status changes to controller
            shutdown: Callback to trigger exporter shutdown (accepts optional exit_code kwarg)
        """
        try:
            # Wait for lease scope to be fully populated by handle_lease
            # This is necessary because handle_lease and run_before_lease_hook run concurrently
            timeout = 30  # seconds
            interval = 0.1  # seconds
            elapsed = 0.0
            while not lease_scope.is_ready():
                if elapsed >= timeout:
                    error_msg = "Timeout waiting for lease scope to be ready"
                    logger.error(error_msg)
                    await report_status(ExporterStatus.BEFORE_LEASE_HOOK_FAILED, error_msg)
                    lease_scope.before_lease_hook.set()
                    return
                await anyio.sleep(interval)
                elapsed += interval

            # Check if hook is configured
            if not self.config.before_lease:
                logger.debug("No before-lease hook configured")
                await report_status(ExporterStatus.LEASE_READY, "Ready for commands")
                return

            await report_status(ExporterStatus.BEFORE_LEASE_HOOK, "Running beforeLease hook")

            # Execute hook with lease scope
            logger.info("Executing before-lease hook for lease %s", lease_scope.lease_name)
            await self._execute_hook(
                self.config.before_lease,
                lease_scope,
                LogSource.BEFORE_LEASE_HOOK,
            )

            await report_status(ExporterStatus.LEASE_READY, "Ready for commands")
            logger.info("beforeLease hook completed successfully")

        except HookExecutionError as e:
            if e.should_shutdown_exporter():
                # on_failure='exit' - shut down the entire exporter
                logger.error("beforeLease hook failed with on_failure='exit': %s", e)
                await report_status(
                    ExporterStatus.BEFORE_LEASE_HOOK_FAILED,
                    f"beforeLease hook failed (on_failure=exit, shutting down): {e}",
                )
                logger.error("Shutting down exporter due to beforeLease hook failure with on_failure='exit'")
                # Exit code 1 tells the CLI not to restart the exporter
                shutdown(exit_code=1)
            else:
                # on_failure='endLease' - just block this lease, exporter stays available
                logger.error("beforeLease hook failed with on_failure='endLease': %s", e)
                await report_status(
                    ExporterStatus.BEFORE_LEASE_HOOK_FAILED,
                    f"beforeLease hook failed (on_failure=endLease): {e}",
                )
                # TODO: We need to implement a controller-side mechanism to end the lease here

        except Exception as e:
            logger.error("beforeLease hook failed with unexpected error: %s", e, exc_info=True)
            await report_status(
                ExporterStatus.BEFORE_LEASE_HOOK_FAILED,
                f"beforeLease hook failed: {e}",
            )
            # Unexpected errors don't trigger shutdown - just block the lease

        finally:
            # Always set the event to unblock connections
            lease_scope.before_lease_hook.set()

    async def run_after_lease_hook(
        self,
        lease_scope: "LeaseContext",
        report_status: Callable[["ExporterStatus", str], Awaitable[None]],
        shutdown: Callable[..., None],
        request_lease_release: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Execute after-lease hook with full orchestration.

        This method handles the complete lifecycle of running an after-lease hook:
        - Validates that the lease scope is ready
        - Reports status changes via the provided callback
        - Sets up the hook executor with the session for logging
        - Executes the hook and handles errors
        - Triggers shutdown on critical failures (HookExecutionError)
        - Requests lease release from controller after hook completes

        Args:
            lease_scope: LeaseScope containing session, socket_path, and client info
            report_status: Async callback to report status changes to controller
            shutdown: Callback to trigger exporter shutdown (accepts optional exit_code kwarg)
            request_lease_release: Async callback to request lease release from controller
        """
        try:
            # Verify lease scope is ready - for after-lease this should always be true
            # since we've already processed the lease, but check defensively
            if not lease_scope.is_ready():
                logger.warning("LeaseScope not ready for after-lease hook, skipping")
                await report_status(ExporterStatus.AVAILABLE, "Available for new lease")
                return

            # Check if hook is configured
            if not self.config.after_lease:
                logger.debug("No after-lease hook configured")
                await report_status(ExporterStatus.AVAILABLE, "Available for new lease")
                return

            await report_status(ExporterStatus.AFTER_LEASE_HOOK, "Running afterLease hooks")

            # Execute hook with lease scope
            logger.info("Executing after-lease hook for lease %s", lease_scope.lease_name)
            await self._execute_hook(
                self.config.after_lease,
                lease_scope,
                LogSource.AFTER_LEASE_HOOK,
            )

            await report_status(ExporterStatus.AVAILABLE, "Available for new lease")
            logger.info("afterLease hook completed successfully")

        except HookExecutionError as e:
            if e.should_shutdown_exporter():
                # on_failure='exit' - shut down the entire exporter
                logger.error("afterLease hook failed with on_failure='exit': %s", e)
                await report_status(
                    ExporterStatus.AFTER_LEASE_HOOK_FAILED,
                    f"afterLease hook failed (on_failure=exit, shutting down): {e}",
                )
                logger.error("Shutting down exporter due to afterLease hook failure with on_failure='exit'")
                # Exit code 1 tells the CLI not to restart the exporter
                shutdown(exit_code=1)
            else:
                # on_failure='endLease' - lease already ended, just report the failure
                # The exporter remains available for new leases
                logger.error("afterLease hook failed with on_failure='endLease': %s", e)
                await report_status(
                    ExporterStatus.AFTER_LEASE_HOOK_FAILED,
                    f"afterLease hook failed (on_failure=endLease): {e}",
                )
                # Note: Lease has already ended - no shutdown needed, exporter remains available

        except Exception as e:
            logger.error("afterLease hook failed with unexpected error: %s", e, exc_info=True)
            await report_status(
                ExporterStatus.AFTER_LEASE_HOOK_FAILED,
                f"afterLease hook failed: {e}",
            )
            # Unexpected errors don't trigger shutdown - exporter remains available

        finally:
            # Request lease release from controller after hook completes (success or failure)
            # This ensures the lease is always released even if the client disconnects
            if request_lease_release:
                await request_lease_release()
