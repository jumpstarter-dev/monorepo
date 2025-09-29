"""
Base classes for drivers and driver clients
"""

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from anyio import create_task_group
from google.protobuf import empty_pb2
from grpc import StatusCode
from grpc.aio import AioRpcError
from jumpstarter_protocol import jumpstarter_pb2, jumpstarter_pb2_grpc, router_pb2_grpc
from rich.logging import RichHandler

from jumpstarter.common import ExporterStatus, Metadata
from jumpstarter.common.exceptions import JumpstarterException
from jumpstarter.common.resources import ResourceMetadata
from jumpstarter.common.serde import decode_value, encode_value
from jumpstarter.common.streams import (
    DriverStreamRequest,
    ResourceStreamRequest,
    StreamRequestMetadata,
)
from jumpstarter.streams.common import forward_stream
from jumpstarter.streams.encoding import compress_stream
from jumpstarter.streams.metadata import MetadataStream, MetadataStreamAttributes
from jumpstarter.streams.progress import ProgressStream
from jumpstarter.streams.router import RouterStream


class DriverError(JumpstarterException):
    """
    Raised when a driver call returns an error
    """


class DriverMethodNotImplemented(DriverError, NotImplementedError):
    """
    Raised when a driver method is not implemented
    """


class DriverInvalidArgument(DriverError, ValueError):
    """
    Raised when a driver method is called with invalid arguments
    """


class ExporterNotReady(DriverError):
    """
    Raised when the exporter is not ready to accept driver calls
    """


@dataclass(kw_only=True)
class AsyncDriverClient(
    Metadata,
    jumpstarter_pb2_grpc.ExporterServiceStub,
    router_pb2_grpc.RouterServiceStub,
):
    """
    Async driver client base class

    Backing implementation of blocking driver client.
    """

    stub: Any

    log_level: str = "INFO"
    logger: logging.Logger = field(init=False)

    def __post_init__(self):
        if hasattr(super(), "__post_init__"):
            super().__post_init__()
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.setLevel(self.log_level)

        # add default handler
        if not self.logger.handlers:
            handler = RichHandler()
            self.logger.addHandler(handler)

    async def check_exporter_status(self):
        """Check if the exporter is ready to accept driver calls"""
        try:
            response = await self.stub.GetStatus(jumpstarter_pb2.GetStatusRequest())
            status = ExporterStatus.from_proto(response.status)

            if status != ExporterStatus.LEASE_READY:
                raise ExporterNotReady(f"Exporter status is {status}: {response.status_message}")

        except AioRpcError as e:
            # If GetStatus is not implemented, assume ready for backward compatibility
            if e.code() == StatusCode.UNIMPLEMENTED:
                self.logger.debug("GetStatus not implemented, assuming exporter is ready")
                return
            raise DriverError(f"Failed to check exporter status: {e.details()}") from e

    async def call_async(self, method, *args):
        """Make DriverCall by method name and arguments"""

        # Check exporter status before making the call
        await self.check_exporter_status()

        request = jumpstarter_pb2.DriverCallRequest(
            uuid=str(self.uuid),
            method=method,
            args=[encode_value(arg) for arg in args],
        )

        try:
            response = await self.stub.DriverCall(request)
        except AioRpcError as e:
            match e.code():
                case StatusCode.NOT_FOUND:
                    raise DriverMethodNotImplemented(e.details()) from None
                case StatusCode.UNIMPLEMENTED:
                    raise DriverMethodNotImplemented(e.details()) from None
                case StatusCode.INVALID_ARGUMENT:
                    raise DriverInvalidArgument(e.details()) from None
                case StatusCode.UNKNOWN:
                    raise DriverError(e.details()) from None
                case _:
                    raise DriverError(e.details()) from e

        return decode_value(response.result)

    async def streamingcall_async(self, method, *args):
        """Make StreamingDriverCall by method name and arguments"""

        # Check exporter status before making the call
        await self.check_exporter_status()

        request = jumpstarter_pb2.StreamingDriverCallRequest(
            uuid=str(self.uuid),
            method=method,
            args=[encode_value(arg) for arg in args],
        )

        try:
            async for response in self.stub.StreamingDriverCall(request):
                yield decode_value(response.result)
        except AioRpcError as e:
            match e.code():
                case StatusCode.UNIMPLEMENTED:
                    raise DriverMethodNotImplemented(e.details()) from None
                case StatusCode.INVALID_ARGUMENT:
                    raise DriverInvalidArgument(e.details()) from None
                case StatusCode.UNKNOWN:
                    raise DriverError(e.details()) from None
                case _:
                    raise DriverError(e.details()) from e

    @asynccontextmanager
    async def stream_async(self, method):
        context = self.stub.Stream(
            metadata=StreamRequestMetadata.model_construct(request=DriverStreamRequest(uuid=self.uuid, method=method))
            .model_dump(mode="json", round_trip=True)
            .items(),
        )
        metadata = dict(list(await context.initial_metadata()))
        async with MetadataStream(stream=RouterStream(context=context), metadata=metadata) as stream:
            yield stream

    @asynccontextmanager
    async def resource_async(
        self,
        stream,
        content_encoding: str | None = None,
    ):
        context = self.stub.Stream(
            metadata=StreamRequestMetadata.model_construct(
                request=ResourceStreamRequest(uuid=self.uuid, x_jmp_content_encoding=content_encoding)
            )
            .model_dump(mode="json", round_trip=True)
            .items(),
        )
        metadata = dict(list(await context.initial_metadata()))
        async with MetadataStream(stream=RouterStream(context=context), metadata=metadata) as rstream:
            metadata = ResourceMetadata(**rstream.extra(MetadataStreamAttributes.metadata))
            if metadata.x_jmp_accept_encoding is None:
                stream = compress_stream(stream, content_encoding)

            async with forward_stream(ProgressStream(stream=stream), rstream):
                yield metadata.resource.model_dump(mode="json")

    def __log(self, level: int, msg: str):
        self.logger.log(level, msg)

    @asynccontextmanager
    async def log_stream_async(self):
        async def log_stream():
            async for response in self.stub.LogStream(empty_pb2.Empty()):
                self.__log(logging.getLevelName(response.severity), response.message)

        async with create_task_group() as tg:
            tg.start_soon(log_stream)
            try:
                yield
            finally:
                tg.cancel_scope.cancel()
