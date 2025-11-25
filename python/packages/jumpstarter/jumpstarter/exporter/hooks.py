"""Lifecycle hooks for Jumpstarter exporters."""

import asyncio
import logging
import os
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Literal

from jumpstarter.common import ExporterStatus, LogSource
from jumpstarter.config.env import JMP_DRIVERS_ALLOW, JUMPSTARTER_HOST
from jumpstarter.config.exporter import HookConfigV1Alpha1, HookInstanceConfigV1Alpha1
from jumpstarter.exporter.logging import get_logger
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

        logger.info("Executing hook: %s", command.strip().split("\n")[0][:100])

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
        """Execute the hook process with the given environment and logging session."""
        command = hook_config.script
        timeout = hook_config.timeout
        on_failure = hook_config.on_failure

        # Exception handling
        error_msg: str | None = None
        cause: Exception | None = None

        try:
            # Execute the hook command using shell
            process = await asyncio.create_subprocess_shell(
                command,
                env=hook_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            try:
                # Create a logger with automatic source registration
                hook_logger = get_logger(f"hook.{lease_scope.lease_name}", log_source, logging_session)

                # Stream output line-by-line for real-time logging
                output_lines = []

                async def read_output():
                    while True:
                        line = await process.stdout.readline()
                        if not line:
                            break
                        line_decoded = line.decode().rstrip()
                        output_lines.append(line_decoded)
                        # Route hook output through the logging system
                        hook_logger.info(line_decoded)

                # Run output reading and process waiting concurrently with timeout
                await asyncio.wait_for(asyncio.gather(read_output(), process.wait()), timeout=timeout)

                # Check if hook succeeded (exit code 0)
                if process.returncode == 0:
                    logger.info("Hook executed successfully")
                    return

                # Non-zero exit code is a failure
                error_msg = f"Hook failed with exit code {process.returncode}"

            except asyncio.TimeoutError as e:
                error_msg = f"Hook timed out after {timeout} seconds"
                cause = e
                logger.error(error_msg)
                try:
                    # Attempt to gracefully terminate the process
                    process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    # Force kill if it didn't terminate in time
                    process.kill()
                    await process.wait()

        except Exception as e:
            error_msg = f"Error executing hook: {e}"
            cause = e
            logger.error(error_msg, exc_info=True)

        # Handle failure if one occurred
        if error_msg is not None:
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
        shutdown: Callable[[], None],
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
            shutdown: Callback to trigger exporter shutdown on critical failures
        """
        try:
            # Wait for lease scope to be fully populated by handle_lease
            assert lease_scope.is_ready(), "LeaseScope must be fully initialized before running before-lease hooks"

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
                shutdown()
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
        shutdown: Callable[[], None],
    ) -> None:
        """Execute after-lease hook with full orchestration.

        This method handles the complete lifecycle of running an after-lease hook:
        - Validates that the lease scope is ready
        - Reports status changes via the provided callback
        - Sets up the hook executor with the session for logging
        - Executes the hook and handles errors
        - Triggers shutdown on critical failures (HookExecutionError)

        Args:
            lease_scope: LeaseScope containing session, socket_path, and client info
            report_status: Async callback to report status changes to controller
            shutdown: Callback to trigger exporter shutdown on critical failures
        """
        try:
            # Verify lease scope is ready
            assert lease_scope.is_ready(), "LeaseScope must be fully initialized before running after-lease hooks"

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
                shutdown()
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
