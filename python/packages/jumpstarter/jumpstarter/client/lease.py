import logging
import os
import sys
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import (
    ExitStack,
    asynccontextmanager,
    contextmanager,
)
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Self

from anyio import (
    AsyncContextManagerMixin,
    CancelScope,
    ContextManagerMixin,
    create_task_group,
    fail_after,
    sleep,
)
from anyio.from_thread import BlockingPortal
from grpc.aio import AioRpcError, Channel
from jumpstarter_protocol import jumpstarter_pb2, jumpstarter_pb2_grpc
from rich.console import Console
from tenacity import retry, retry_if_exception_type, wait_exponential_jitter

from .exceptions import LeaseError
from jumpstarter.client import client_from_path
from jumpstarter.client.grpc import ClientService
from jumpstarter.common import TemporaryUnixListener
from jumpstarter.common.condition import condition_false, condition_message, condition_present_and_equal, condition_true
from jumpstarter.common.exceptions import ConnectionError
from jumpstarter.common.grpc import translate_grpc_exceptions
from jumpstarter.common.streams import connect_router_stream
from jumpstarter.config.tls import TLSConfigV1Alpha1

logger = logging.getLogger(__name__)


@dataclass(kw_only=True)
class Lease(ContextManagerMixin, AsyncContextManagerMixin):
    channel: Channel
    duration: timedelta
    selector: str
    portal: BlockingPortal
    namespace: str
    name: str | None = field(default=None)
    allow: list[str]
    unsafe: bool
    release: bool = True  # release on contexts exit
    controller: jumpstarter_pb2_grpc.ControllerServiceStub = field(init=False)
    tls_config: TLSConfigV1Alpha1 = field(default_factory=TLSConfigV1Alpha1)
    grpc_options: dict[str, Any] = field(default_factory=dict)
    acquisition_timeout: int = field(default=7200)  # Timeout in seconds for lease acquisition, polled in 5s intervals
    exporter_name: str = field(default="remote", init=False)  # Populated during acquisition
    lease_ending_callback: Callable[[Self, timedelta], None] | None = field(
        default=None, init=False
    )  # Called when lease is ending

    def __post_init__(self):
        if hasattr(super(), "__post_init__"):
            super().__post_init__()

        self.controller = jumpstarter_pb2_grpc.ControllerServiceStub(self.channel)
        self.svc = ClientService(channel=self.channel, namespace=self.namespace)

    async def _create(self):
        logger.debug("Creating lease request for selector %s for duration %s", self.selector, self.duration)
        with translate_grpc_exceptions():
            self.name = (
                await self.svc.CreateLease(
                    selector=self.selector,
                    duration=self.duration,
                    lease_id=self.name,
                )
            ).name
        logger.info("Acquiring lease %s for selector %s for duration %s", self.name, self.selector, self.duration)

    async def get(self):
        with translate_grpc_exceptions():
            svc = ClientService(channel=self.channel, namespace=self.namespace)
            return await svc.GetLease(name=self.name)

    @retry(
        wait=wait_exponential_jitter(initial=1, max=120, jitter=1),
        retry=retry_if_exception_type(ConnectionError),
        reraise=True,
    )
    async def _get_with_retry(self):
        """Get lease with exponential backoff retry on ConnectionError.

        Retries with exponential backoff and jitter indefinitely when ConnectionError occurs.
        The wait time between retries is capped at 2 minutes (120 seconds).
        Jitter helps prevent thundering herd problems when multiple clients retry simultaneously.
        """
        try:
            return await self.get()
        except ConnectionError as e:
            logger.error("Error while getting lease %s: %s", self.name, e)
            raise

    def request(self):
        """Request a lease, or verifies a lease which was already created.

        :return: lease
        :rtype: Lease
        :raises LeaseError: if lease is unsatisfiable
        :raises LeaseError: if lease is not pending
        :raises TimeoutError: if lease is not ready after timeout
        """
        return self.portal.call(self.request_async)

    async def request_async(self):
        """Request a lease, or verifies a lease which was already created.

        :return: lease
        :rtype: Lease
        :raises LeaseError: if lease is unsatisfiable
        :raises LeaseError: if lease is not pending
        :raises TimeoutError: if lease is not ready after timeout
        """
        if self.name:
            logger.debug("using existing lease via env or flag %s", self.name)
            existing_lease = await self.get()
            if self.selector is not None and existing_lease.selector != self.selector:
                logger.warning(
                    "Existing lease from env or flag %s has selector '%s' but requested selector is '%s'. "
                    "Creating a new lease instead",
                    self.name,
                    existing_lease.selector,
                    self.selector,
                )
                self.name = None
                await self._create()
        else:
            await self._create()

        return await self._acquire()

    def _update_spinner_status(self, spinner, result):
        """Update spinner with appropriate status message based on lease conditions."""
        if condition_true(result.conditions, "Pending"):
            pending_message = condition_message(result.conditions, "Pending")
            if pending_message:
                spinner.update_status(f"Waiting for lease: {pending_message}")
            else:
                spinner.update_status("Waiting for lease to be ready...")
        else:
            spinner.update_status("Waiting for server to provide status updates...")

    async def _acquire(self):
        """Acquire a lease.

        Makes sure the lease is ready, and returns the lease object.
        """
        try:
            with fail_after(self.acquisition_timeout):
                with LeaseAcquisitionSpinner(self.name) as spinner:
                    while True:
                        logger.debug("Polling Lease %s", self.name)
                        result = await self._get_with_retry()
                        # lease ready
                        if condition_true(result.conditions, "Ready"):
                            logger.debug("Lease %s acquired", self.name)
                            spinner.update_status(f"Lease {self.name} acquired successfully!", force=True)
                            self.exporter_name = result.exporter
                            break

                        # lease unsatisfiable
                        if condition_true(result.conditions, "Unsatisfiable"):
                            message = condition_message(result.conditions, "Unsatisfiable")
                            logger.debug("Lease %s cannot be satisfied: %s", self.name, message)
                            raise LeaseError(f"the lease cannot be satisfied: {message}")

                        # lease invalid
                        if condition_true(result.conditions, "Invalid"):
                            message = condition_message(result.conditions, "Invalid")
                            logger.debug("Lease %s is invalid: %s", self.name, message)
                            raise LeaseError(f"the lease is invalid: {message}")

                        # lease not pending
                        if condition_false(result.conditions, "Pending"):
                            raise LeaseError(
                                f"Lease {self.name} is not in pending, but it isn't in Ready or "
                                f"Unsatisfiable state either"
                            )

                        # lease released
                        if condition_present_and_equal(result.conditions, "Ready", "False", "Released"):
                            raise LeaseError(f"lease {self.name} released")

                        # Update spinner with appropriate status message
                        self._update_spinner_status(spinner, result)

                        # Wait in 1-second increments with tick updates for better UX
                        for _ in range(5):
                            await sleep(1)
                            spinner.tick()
            return self

        except TimeoutError:
            logger.debug(f"Lease {self.name} acquisition timed out after {self.acquisition_timeout} seconds")
            raise LeaseError(
                f"lease {self.name} acquisition timed out after {self.acquisition_timeout} seconds"
            ) from None

    @asynccontextmanager
    async def __asynccontextmanager__(self) -> AsyncGenerator[Self]:
        try:
            value = await self.request_async()
            yield value
        finally:
            if self.release and self.name:
                # Shield cleanup from cancellation to ensure it completes
                with CancelScope(shield=True):
                    try:
                        with fail_after(30):
                            # skip the message if the lease is already expired
                            lease = await self.get()
                            if not lease.effective_end_time:
                                logger.info("Releasing Lease %s", self.name)
                            await self.svc.DeleteLease(
                                name=self.name,
                            )
                    except TimeoutError:
                        logger.warning("Timeout while deleting lease %s during cleanup", self.name)

    @contextmanager
    def __contextmanager__(self) -> Generator[Self]:
        with self.portal.wrap_async_context_manager(self) as value:
            yield value

    async def handle_async(self, stream):
        logger.debug("Connecting to Lease with name %s", self.name)
        response = await self.controller.Dial(jumpstarter_pb2.DialRequest(lease_name=self.name))
        async with connect_router_stream(
            response.router_endpoint, response.router_token, stream, self.tls_config, self.grpc_options
        ):
            pass

    @asynccontextmanager
    async def serve_unix_async(self):
        async with TemporaryUnixListener(self.handle_async) as path:
            logger.debug("Serving Unix socket at %s", path)
            await self._wait_for_ready_connection(path)
            yield path

    async def _wait_for_ready_connection(self, path: str):
        """Wait for the basic gRPC connection to be established.

        This only waits for the connection to be available. It does NOT wait
        for beforeLease hooks to complete - that should be done after log
        streaming is started so hook output can be displayed in real-time.
        """
        retries_left = 5
        logger.info("Waiting for ready connection at %s", path)
        while True:
            try:
                with ExitStack() as stack:
                    async with client_from_path(path, self.portal, stack, allow=self.allow, unsafe=self.unsafe) as _:
                        # Connection established
                        break
            except AioRpcError as e:
                if retries_left > 1:
                    retries_left -= 1
                else:
                    logger.error("Max retries reached while waiting for ready connection at %s", path)
                    raise ConnectionError("Max retries reached while waiting for ready connection at %s" % path) from e
                if e.code().name == "UNAVAILABLE":
                    logger.warning("Still waiting for connection to be ready at %s", path)
                else:
                    logger.warning("Waiting for ready connection to %s: %s", path, e)
                await sleep(5)
            except ConnectionError:
                raise
            except Exception as e:
                logger.error("Unexpected error while waiting for ready connection to %s: %s", path, e)
                raise ConnectionError("Unexpected error while waiting for ready connection to %s" % path) from e

    @asynccontextmanager
    async def monitor_async(self, threshold: timedelta = timedelta(minutes=5)):
        async def _monitor():
            check_interval = 30  # seconds - check periodically for external lease changes
            while True:
                lease = await self.get()
                if lease.effective_begin_time and lease.effective_duration:
                    if lease.effective_end_time:  # already ended
                        end_time = lease.effective_end_time
                    else:
                        end_time = lease.effective_begin_time + lease.duration
                    remain = end_time - datetime.now().astimezone()
                    if remain < timedelta(0):
                        # lease already expired, stopping monitor
                        logger.info("Lease {} ended at {}".format(self.name, end_time))
                        if self.lease_ending_callback is not None:
                            self.lease_ending_callback(self, timedelta(0))
                        break
                    # Log once when entering the threshold window
                    if threshold - timedelta(seconds=check_interval) <= remain < threshold:
                        logger.info(
                            "Lease {} ending in {} minutes at {}".format(
                                self.name, int((remain.total_seconds() + 30) // 60), end_time
                            )
                        )
                        # Notify callback about approaching expiration
                        if self.lease_ending_callback is not None:
                            self.lease_ending_callback(self, remain)
                    await sleep(min(remain.total_seconds(), check_interval))
                else:
                    await sleep(1)

        async with create_task_group() as tg:
            tg.start_soon(_monitor)
            try:
                yield
            finally:
                tg.cancel_scope.cancel()

    @asynccontextmanager
    async def connect_async(self, stack):
        async with self.serve_unix_async() as path:
            async with client_from_path(path, self.portal, stack, allow=self.allow, unsafe=self.unsafe) as client:
                yield client

    @contextmanager
    def connect(self):
        with ExitStack() as stack:
            with self.portal.wrap_async_context_manager(self.connect_async(stack)) as client:
                yield client

    @contextmanager
    def serve_unix(self):
        with self.portal.wrap_async_context_manager(self.serve_unix_async()) as path:
            yield path

    @contextmanager
    def monitor(self, threshold: timedelta = timedelta(minutes=5)):
        with self.portal.wrap_async_context_manager(self.monitor_async(threshold)):
            yield


class LeaseAcquisitionSpinner:
    """Context manager for displaying a spinner during lease acquisition."""

    def __init__(self, lease_name: str | None = None):
        self.lease_name = lease_name
        self.console = Console()
        self.spinner = None
        self.start_time = None
        self._should_show_spinner = self._is_terminal_available() and not self._is_non_interactive()
        self._current_message = None
        self._last_log_time = None
        self._log_throttle_interval = timedelta(minutes=5)

    def _is_non_interactive(self) -> bool:
        """Check if the user desires a NONINTERACTIVE environment."""
        return os.environ.get("NONINTERACTIVE", "false").lower() in ["true", "1"]

    def _is_terminal_available(self) -> bool:
        """Check if we're running in a terminal/TTY."""
        return (
            hasattr(sys.stdout, "isatty")
            and sys.stdout.isatty()
            and hasattr(sys.stderr, "isatty")
            and sys.stderr.isatty()
        )

    def __enter__(self):
        self.start_time = datetime.now()
        if self._should_show_spinner:
            self.spinner = self.console.status(
                f"Acquiring lease {self.lease_name or '...'}...", spinner="dots", spinner_style="blue"
            )
            self.spinner.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.spinner:
            self.spinner.stop()

    def update_status(self, message: str, force: bool = False):
        """Update the spinner status message.

        :param message: The status message to display
        :param force: If True, always log the message even when throttling (default: False)
        """
        if self.spinner and self._should_show_spinner:
            self._current_message = f"[blue]{message}[/blue]"
            elapsed = datetime.now() - self.start_time
            elapsed_str = str(elapsed).split(".")[0]  # Remove microseconds
            self.spinner.update(f"{self._current_message} [dim]({elapsed_str})[/dim]")
        else:
            # Log info message when no console is available
            # Throttle updates to at most every 5 minutes unless forced
            now = datetime.now()
            should_log = (
                force or self._last_log_time is None or (now - self._last_log_time) >= self._log_throttle_interval
            )

            if should_log:
                elapsed = now - self.start_time
                elapsed_str = str(elapsed).split(".")[0]  # Remove microseconds
                logger.info(f"{message} ({elapsed_str})")
                self._last_log_time = now

    def tick(self):
        """Update the spinner with current elapsed time without changing the message."""
        if self.spinner and self._should_show_spinner and self._current_message:
            elapsed = datetime.now() - self.start_time
            elapsed_str = str(elapsed).split(".")[0]  # Remove microseconds
            # Use the stored current message and update with new elapsed time
            self.spinner.update(f"{self._current_message} [dim]({elapsed_str})[/dim]")
