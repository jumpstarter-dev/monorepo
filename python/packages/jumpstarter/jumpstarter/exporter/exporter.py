import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Self

import grpc
from anyio import (
    AsyncContextManagerMixin,
    CancelScope,
    Event,
    connect_unix,
    create_memory_object_stream,
    create_task_group,
    move_on_after,
    sleep,
)
from anyio.abc import TaskGroup
from google.protobuf import empty_pb2
from jumpstarter_protocol import (
    jumpstarter_pb2,
    jumpstarter_pb2_grpc,
)

from jumpstarter.common import ExporterStatus, Metadata
from jumpstarter.common.streams import connect_router_stream
from jumpstarter.config.tls import TLSConfigV1Alpha1
from jumpstarter.driver import Driver
from jumpstarter.exporter.hooks import HookContext, HookExecutor
from jumpstarter.exporter.session import Session

logger = logging.getLogger(__name__)


@dataclass(kw_only=True)
class Exporter(AsyncContextManagerMixin, Metadata):
    channel_factory: Callable[[], Awaitable[grpc.aio.Channel]]
    device_factory: Callable[[], Driver]
    lease_name: str = field(init=False, default="")
    tls: TLSConfigV1Alpha1 = field(default_factory=TLSConfigV1Alpha1)
    grpc_options: dict[str, str] = field(default_factory=dict)
    hook_executor: HookExecutor | None = field(default=None)
    registered: bool = field(init=False, default=False)
    _unregister: bool = field(init=False, default=False)
    _stop_requested: bool = field(init=False, default=False)
    _started: bool = field(init=False, default=False)
    _tg: TaskGroup | None = field(init=False, default=None)
    _current_client_name: str = field(init=False, default="")
    _pre_lease_ready: Event | None = field(init=False, default=None)
    _current_status: ExporterStatus = field(init=False, default=ExporterStatus.OFFLINE)
    _current_session: Session | None = field(init=False, default=None)

    def stop(self, wait_for_lease_exit=False, should_unregister=False):
        """Signal the exporter to stop.

        Args:
            wait_for_lease_exit (bool): If True, wait for the current lease to exit before stopping.
            should_unregister (bool): If True, unregister from controller. Otherwise rely on heartbeat.
        """

        # Stop immediately if not started yet or if immediate stop is requested
        if (not self._started or not wait_for_lease_exit) and self._tg is not None:
            logger.info("Stopping exporter immediately, unregister from controller=%s", should_unregister)
            self._unregister = should_unregister
            self._tg.cancel_scope.cancel()
        elif not self._stop_requested:
            self._stop_requested = True
            logger.info("Exporter marked for stop upon lease exit")

    async def _update_status(self, status: ExporterStatus, message: str = ""):
        """Update exporter status with the controller and session."""
        self._current_status = status

        # Update session status if available
        if self._current_session:
            self._current_session.update_status(status, message)

        try:
            controller = jumpstarter_pb2_grpc.ControllerServiceStub(await self.channel_factory())
            await controller.UpdateStatus(
                jumpstarter_pb2.UpdateStatusRequest(
                    status=status.to_proto(),
                    status_message=message,
                )
            )
            logger.info(f"Updated status to {status}: {message}")
        except Exception as e:
            logger.error(f"Failed to update status: {e}")

    @asynccontextmanager
    async def __asynccontextmanager__(self) -> AsyncGenerator[Self]:
        try:
            yield self
        finally:
            try:
                if self.registered and self._unregister:
                    logger.info("Unregistering exporter with controller")
                    try:
                        with move_on_after(10):  # 10 second timeout
                            channel = await self.channel_factory()
                            try:
                                controller = jumpstarter_pb2_grpc.ControllerServiceStub(channel)
                                await self._update_status(ExporterStatus.OFFLINE, "Exporter shutting down")
                                await controller.Unregister(
                                    jumpstarter_pb2.UnregisterRequest(
                                        reason="Exporter shutdown",
                                    )
                                )
                                logger.info("Controller unregistration completed successfully")
                            finally:
                                with CancelScope(shield=True):
                                    await channel.close()
                    except Exception as e:
                        logger.error("Error during controller unregistration: %s", e, exc_info=True)

            except Exception as e:
                logger.error("Error during exporter cleanup: %s", e, exc_info=True)
                # Don't re-raise to avoid masking the original exception

    async def __handle(self, path, endpoint, token, tls_config, grpc_options):
        try:
            async with await connect_unix(path) as stream:
                async with connect_router_stream(endpoint, token, stream, tls_config, grpc_options):
                    pass
        except Exception as e:
            logger.info("failed to handle connection: {}".format(e))

    @asynccontextmanager
    async def session(self):
        controller = jumpstarter_pb2_grpc.ControllerServiceStub(await self.channel_factory())
        with Session(
            uuid=self.uuid,
            labels=self.labels,
            root_device=self.device_factory(),
        ) as session:
            # Store session reference for status updates
            self._current_session = session
            try:
                async with session.serve_unix_async() as path:
                    async with grpc.aio.secure_channel(
                        f"unix://{path}", grpc.local_channel_credentials(grpc.LocalConnectionType.UDS)
                    ) as channel:
                        response = await jumpstarter_pb2_grpc.ExporterServiceStub(channel).GetReport(empty_pb2.Empty())
                        logger.info("Registering exporter with controller")
                        await controller.Register(
                            jumpstarter_pb2.RegisterRequest(
                                labels=self.labels,
                                reports=response.reports,
                            )
                        )
                        self.registered = True
                        await self._update_status(ExporterStatus.AVAILABLE, "Exporter registered and available")
                    yield path
            finally:
                # Clear session reference
                self._current_session = None

    async def handle(self, lease_name, tg):
        logger.info("Listening for incoming connection requests on lease %s", lease_name)

        listen_tx, listen_rx = create_memory_object_stream()

        async def listen(retries=5, backoff=3):
            retries_left = retries
            while True:
                try:
                    controller = jumpstarter_pb2_grpc.ControllerServiceStub(await self.channel_factory())
                    async for request in controller.Listen(jumpstarter_pb2.ListenRequest(lease_name=lease_name)):
                        await listen_tx.send(request)
                except Exception as e:
                    if retries_left > 0:
                        retries_left -= 1
                        logger.info(
                            "Listen stream interrupted, restarting in {}s, {} retries left: {}".format(
                                backoff, retries_left, e
                            )
                        )
                        await sleep(backoff)
                    else:
                        raise
                else:
                    retries_left = retries

        tg.start_soon(listen)

        # Wait for pre-lease hook to complete before processing connections
        if self._pre_lease_ready is not None:
            logger.info("Waiting for pre-lease hook to complete before accepting connections")
            await self._pre_lease_ready.wait()
            logger.info("Pre-lease hook completed, now accepting connections")

        async with self.session() as path:
            async for request in listen_rx:
                logger.info("Handling new connection request on lease %s", lease_name)
                tg.start_soon(
                    self.__handle, path, request.router_endpoint, request.router_token, self.tls, self.grpc_options
                )

    async def serve(self):  # noqa: C901
        """
        Serve the exporter.
        """
        # initial registration
        async with self.session():
            pass
        status_tx, status_rx = create_memory_object_stream()

        async def status(retries=5, backoff=3):
            retries_left = retries
            while True:
                try:
                    controller = jumpstarter_pb2_grpc.ControllerServiceStub(await self.channel_factory())
                    async for status in controller.Status(jumpstarter_pb2.StatusRequest()):
                        await status_tx.send(status)
                except Exception as e:
                    if retries_left > 0:
                        retries_left -= 1
                        logger.info(
                            "Status stream interrupted, restarting in {}s, {} retries left: {}".format(
                                backoff, retries_left, e
                            )
                        )
                        await sleep(backoff)
                    else:
                        raise
                else:
                    retries_left = retries

        async with create_task_group() as tg:
            self._tg = tg
            tg.start_soon(status)
            async for status in status_rx:
                if self.lease_name != "" and self.lease_name != status.lease_name:
                    # Post-lease hook for the previous lease
                    if self.hook_executor and self._current_client_name:
                        hook_context = HookContext(
                            lease_name=self.lease_name,
                            client_name=self._current_client_name,
                        )
                        # Shield the post-lease hook from cancellation and await it
                        with CancelScope(shield=True):
                            await self._update_status(ExporterStatus.AFTER_LEASE_HOOK, "Running afterLease hooks")
                            # Pass the current session to hook executor for logging
                            self.hook_executor.main_session = self._current_session
                            await self.hook_executor.execute_post_lease_hook(hook_context)
                            await self._update_status(ExporterStatus.AVAILABLE, "Available for new lease")

                    self.lease_name = status.lease_name
                    logger.info("Lease status changed, killing existing connections")
                    # Reset event for next lease
                    self._pre_lease_ready = None
                    self.stop()
                    break

                # Check for lease state transitions
                previous_leased = hasattr(self, "_previous_leased") and self._previous_leased
                current_leased = status.leased

                self.lease_name = status.lease_name
                if not self._started and self.lease_name != "":
                    self._started = True
                    # Create event for pre-lease synchronization
                    self._pre_lease_ready = Event()
                    tg.start_soon(self.handle, self.lease_name, tg)

                if current_leased:
                    logger.info("Currently leased by %s under %s", status.client_name, status.lease_name)
                    self._current_client_name = status.client_name

                    # Pre-lease hook when transitioning from unleased to leased
                    if not previous_leased:
                        if self.hook_executor:
                            hook_context = HookContext(
                                lease_name=status.lease_name,
                                client_name=status.client_name,
                            )

                            # Start pre-lease hook asynchronously
                            async def run_before_lease_hook(hook_ctx):
                                try:
                                    await self._update_status(
                                        ExporterStatus.BEFORE_LEASE_HOOK, "Running beforeLease hooks"
                                    )
                                    # Pass the current session to hook executor for logging
                                    self.hook_executor.main_session = self._current_session
                                    await self.hook_executor.execute_pre_lease_hook(hook_ctx)
                                    await self._update_status(ExporterStatus.LEASE_READY, "Ready for commands")
                                    logger.info("beforeLease hook completed successfully")
                                except Exception as e:
                                    logger.error("beforeLease hook failed: %s", e)
                                    # Still transition to ready even if hook fails
                                    await self._update_status(
                                        ExporterStatus.LEASE_READY, f"Ready (beforeLease hook failed: {e})"
                                    )
                                finally:
                                    # Always set the event to unblock connections
                                    if self._pre_lease_ready:
                                        self._pre_lease_ready.set()

                            tg.start_soon(run_before_lease_hook, hook_context)
                        else:
                            # No hook configured, set event immediately
                            await self._update_status(ExporterStatus.LEASE_READY, "Ready for commands")
                            if self._pre_lease_ready:
                                self._pre_lease_ready.set()
                else:
                    logger.info("Currently not leased")

                    # Post-lease hook when transitioning from leased to unleased
                    if previous_leased and self.hook_executor and self._current_client_name:
                        hook_context = HookContext(
                            lease_name=self.lease_name,
                            client_name=self._current_client_name,
                        )
                        # Shield the post-lease hook from cancellation and await it
                        with CancelScope(shield=True):
                            await self._update_status(ExporterStatus.AFTER_LEASE_HOOK, "Running afterLease hooks")
                            # Pass the current session to hook executor for logging
                            self.hook_executor.main_session = self._current_session
                            await self.hook_executor.execute_post_lease_hook(hook_context)
                            await self._update_status(ExporterStatus.AVAILABLE, "Available for new lease")

                    self._current_client_name = ""
                    # Reset event for next lease
                    self._pre_lease_ready = None

                    if self._stop_requested:
                        self.stop(should_unregister=True)
                        break

                self._previous_leased = current_leased
        self._tg = None
