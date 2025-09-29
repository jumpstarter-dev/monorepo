"""Lifecycle hooks for Jumpstarter exporters."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Callable

from jumpstarter.common import LogSource
from jumpstarter.config.env import JMP_DRIVERS_ALLOW, JUMPSTARTER_HOST
from jumpstarter.config.exporter import HookConfigV1Alpha1
from jumpstarter.driver import Driver
from jumpstarter.exporter.logging import get_logger
from jumpstarter.exporter.session import Session

logger = logging.getLogger(__name__)


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
    timeout: int = field(init=False)

    def __post_init__(self):
        self.timeout = self.config.timeout

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

    async def _execute_hook(self, command: str, context: HookContext, log_source: LogSource) -> bool:
        """Execute a single hook command."""
        if not command or not command.strip():
            logger.debug("Hook command is empty, skipping")
            return True

        logger.info("Executing hook: %s", command.strip().split("\n")[0][:100])

        async with self._create_hook_environment(context) as (session, hook_env):
            try:
                # Execute the hook command using shell
                process = await asyncio.create_subprocess_shell(
                    command,
                    env=hook_env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )

                try:
                    # Determine which session to use for logging - prefer main session if available
                    logging_session = self.main_session if self.main_session is not None else session

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
                    await asyncio.wait_for(asyncio.gather(read_output(), process.wait()), timeout=self.timeout)

                    if process.returncode == 0:
                        logger.info("Hook executed successfully")
                        return True
                    else:
                        logger.error("Hook failed with return code %d", process.returncode)
                        return False

                except asyncio.TimeoutError:
                    logger.error("Hook timed out after %d seconds", self.timeout)
                    try:
                        process.terminate()
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()
                    return False

            except Exception as e:
                logger.error("Error executing hook: %s", e, exc_info=True)
                return False

    async def execute_pre_lease_hook(self, context: HookContext) -> bool:
        """Execute the pre-lease hook."""
        if not self.config.pre_lease:
            logger.debug("No pre-lease hook configured")
            return True

        logger.info("Executing pre-lease hook for lease %s", context.lease_name)
        return await self._execute_hook(self.config.pre_lease, context, LogSource.BEFORE_LEASE_HOOK)

    async def execute_post_lease_hook(self, context: HookContext) -> bool:
        """Execute the post-lease hook."""
        if not self.config.post_lease:
            logger.debug("No post-lease hook configured")
            return True

        logger.info("Executing post-lease hook for lease %s", context.lease_name)
        return await self._execute_hook(self.config.post_lease, context, LogSource.AFTER_LEASE_HOOK)
