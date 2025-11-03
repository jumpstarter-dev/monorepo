"""Lifecycle hooks for Jumpstarter exporters."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Callable

from jumpstarter.common import LogSource
from jumpstarter.config.env import JMP_DRIVERS_ALLOW, JUMPSTARTER_HOST
from jumpstarter.config.exporter import HookConfigV1Alpha1, HookInstanceConfigV1Alpha1
from jumpstarter.driver import Driver
from jumpstarter.exporter.logging import get_logger
from jumpstarter.exporter.session import Session

logger = logging.getLogger(__name__)


class HookExecutionError(Exception):
    """Raised when a hook fails and on_failure is set to 'block'."""

    pass


@dataclass(kw_only=True)
class HookContext:
    """Context information passed to hooks."""

    lease_name: str
    client_name: str = ""
    lease_duration: str = ""
    exporter_name: str = ""
    exporter_namespace: str = ""


@dataclass(kw_only=True)
class HookExecutor:
    """Executes lifecycle hooks with access to the j CLI."""

    config: HookConfigV1Alpha1
    device_factory: Callable[[], Driver]
    main_session: Session | None = field(default=None)

    @asynccontextmanager
    async def _create_hook_environment(self, context: HookContext):
        """Create a local session and Unix socket for j CLI access."""
        with Session(
            root_device=self.device_factory(),
            # Use hook context for metadata
            labels={
                "jumpstarter.dev/hook-context": "true",
                "jumpstarter.dev/lease": context.lease_name,
            },
        ) as session:
            async with session.serve_unix_async() as unix_path:
                # Create environment variables for the hook
                hook_env = os.environ.copy()
                hook_env.update(
                    {
                        JUMPSTARTER_HOST: str(unix_path),
                        JMP_DRIVERS_ALLOW: "UNSAFE",  # Allow all drivers for local access
                        "LEASE_NAME": context.lease_name,
                        "CLIENT_NAME": context.client_name,
                        "LEASE_DURATION": context.lease_duration,
                        "EXPORTER_NAME": context.exporter_name,
                        "EXPORTER_NAMESPACE": context.exporter_namespace,
                    }
                )

                yield session, hook_env

    async def _execute_hook(
        self,
        hook_config: HookInstanceConfigV1Alpha1,
        context: HookContext,
        log_source: LogSource,
        socket_path: str | None = None,
    ):
        """Execute a single hook command.

        Args:
            hook_config: Hook configuration including script, timeout, exit_code, and on_failure
            context: Hook context information
            log_source: Log source for hook output
            socket_path: Optional Unix socket path to reuse existing session.
                        If provided, hooks will access the main session instead of creating their own.
        """
        command = hook_config.script
        if not command or not command.strip():
            logger.debug("Hook command is empty, skipping")
            return

        logger.info("Executing hook: %s", command.strip().split("\n")[0][:100])

        # If socket_path provided, use existing session; otherwise create new one
        if socket_path is not None:
            # Reuse existing session - create environment without session creation
            hook_env = os.environ.copy()
            hook_env.update(
                {
                    JUMPSTARTER_HOST: str(socket_path),
                    JMP_DRIVERS_ALLOW: "UNSAFE",
                    "LEASE_NAME": context.lease_name,
                    "CLIENT_NAME": context.client_name,
                    "LEASE_DURATION": context.lease_duration,
                    "EXPORTER_NAME": context.exporter_name,
                    "EXPORTER_NAMESPACE": context.exporter_namespace,
                }
            )

            # Use main session for logging (must be available when socket_path is provided)
            logging_session = self.main_session
            if logging_session is None:
                raise ValueError("main_session must be set when reusing socket_path")

            return await self._execute_hook_process(hook_config, context, log_source, hook_env, logging_session)
        else:
            # Create new session for hook execution (fallback/standalone mode)
            async with self._create_hook_environment(context) as (session, hook_env):
                # Determine which session to use for logging
                logging_session = self.main_session if self.main_session is not None else session
                return await self._execute_hook_process(hook_config, context, log_source, hook_env, logging_session)

    async def _execute_hook_process(
        self,
        hook_config: HookInstanceConfigV1Alpha1,
        context: HookContext,
        log_source: LogSource,
        hook_env: dict,
        logging_session: Session,
    ):
        """Execute the hook process with the given environment and logging session."""
        command = hook_config.script
        timeout = hook_config.timeout
        expected_exit_code = hook_config.exit_code
        on_failure = hook_config.on_failure

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
                hook_logger = get_logger(f"hook.{context.lease_name}", log_source, logging_session)

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

                # Check if exit code matches expected
                if process.returncode == expected_exit_code:
                    logger.info("Hook executed successfully with exit code %d", process.returncode)
                    return
                else:
                    # Exit code mismatch - handle according to on_failure setting
                    error_msg = f"Hook failed: expected exit code {expected_exit_code}, got {process.returncode}"

                    if on_failure == "pass":
                        logger.info("%s (on_failure=pass, continuing)", error_msg)
                        return
                    elif on_failure == "warn":
                        logger.warning("%s (on_failure=warn, continuing)", error_msg)
                        return
                    else:  # on_failure == "block"
                        logger.error("%s (on_failure=block, raising exception)", error_msg)
                        raise HookExecutionError(error_msg)

            except asyncio.TimeoutError as e:
                error_msg = f"Hook timed out after {timeout} seconds"
                logger.error(error_msg)
                try:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()

                # Handle timeout according to on_failure setting
                if on_failure == "pass":
                    logger.info("%s (on_failure=pass, continuing)", error_msg)
                    return
                elif on_failure == "warn":
                    logger.warning("%s (on_failure=warn, continuing)", error_msg)
                    return
                else:  # on_failure == "block"
                    raise HookExecutionError(error_msg) from e

        except HookExecutionError:
            # Re-raise HookExecutionError to propagate to exporter
            raise
        except Exception as e:
            error_msg = f"Error executing hook: {e}"
            logger.error(error_msg, exc_info=True)

            # Handle exception according to on_failure setting
            if on_failure == "pass":
                logger.info("%s (on_failure=pass, continuing)", error_msg)
                return
            elif on_failure == "warn":
                logger.warning("%s (on_failure=warn, continuing)", error_msg)
                return
            else:  # on_failure == "block"
                raise HookExecutionError(error_msg) from e

    async def execute_before_lease_hook(self, context: HookContext, socket_path: str | None = None):
        """Execute the before-lease hook.

        Args:
            context: Hook context information
            socket_path: Optional Unix socket path to reuse existing session

        Raises:
            HookExecutionError: If hook fails and on_failure is set to 'block'
        """
        if not self.config.before_lease:
            logger.debug("No before-lease hook configured")
            return

        logger.info("Executing before-lease hook for lease %s", context.lease_name)
        await self._execute_hook(
            self.config.before_lease,
            context,
            LogSource.BEFORE_LEASE_HOOK,
            socket_path,
        )

    async def execute_after_lease_hook(self, context: HookContext, socket_path: str | None = None):
        """Execute the after-lease hook.

        Args:
            context: Hook context information
            socket_path: Optional Unix socket path to reuse existing session

        Raises:
            HookExecutionError: If hook fails and on_failure is set to 'block'
        """
        if not self.config.after_lease:
            logger.debug("No after-lease hook configured")
            return

        logger.info("Executing after-lease hook for lease %s", context.lease_name)
        await self._execute_hook(
            self.config.after_lease,
            context,
            LogSource.AFTER_LEASE_HOOK,
            socket_path,
        )
