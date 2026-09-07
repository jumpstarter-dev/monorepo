"""
Base classes for drivers and driver clients
"""

from __future__ import annotations

import logging
import os
import time
from abc import ABCMeta, abstractmethod
from contextlib import asynccontextmanager
from dataclasses import field
from inspect import isasyncgenfunction, iscoroutinefunction
from itertools import chain
from typing import Any, ClassVar
from urllib.parse import urlparse, urlunparse
from uuid import UUID, uuid4

import aiohttp
import grpc.aio
import yarl
from anyio import BrokenResourceError, to_thread
from grpc import StatusCode
from jumpstarter_protocol import jumpstarter_pb2, jumpstarter_pb2_grpc, router_pb2_grpc
from pydantic import TypeAdapter
from pydantic.dataclasses import dataclass

from .decorators import (
    MARKER_DRIVERCALL,
    MARKER_MAGIC,
    MARKER_STREAMCALL,
    MARKER_STREAMING_DRIVERCALL,
)
from jumpstarter.common import LogSource, Metadata
from jumpstarter.common.resources import ClientStreamResource, PresignedRequestResource, Resource, ResourceMetadata
from jumpstarter.common.serde import decode_value, encode_value
from jumpstarter.common.streams import (
    DriverStreamRequest,
    ResourceStreamRequest,
)
from jumpstarter.config.env import JMP_DISABLE_COMPRESSION
from jumpstarter.exporter.logging import get_logger
from jumpstarter.metrics.registry import (
    ErrorType,
    OperationResult,
    exemplars_from_log_context,
    exporter_from_log_context,
    get_registry,
)
from jumpstarter.streams.aiohttp import AiohttpStreamReaderStream
from jumpstarter.streams.common import create_memory_stream
from jumpstarter.streams.encoding import Compression, compress_stream
from jumpstarter.streams.metadata import MetadataStream
from jumpstarter.streams.progress import ProgressStream

# Ordered most-specific first: ConnectionError is an OSError subclass.
_DRIVER_CALL_ERRORS: tuple[tuple[type[BaseException], ErrorType, StatusCode], ...] = (
    (NotImplementedError, "not_implemented", StatusCode.UNIMPLEMENTED),
    (ValueError, "validation_error", StatusCode.INVALID_ARGUMENT),
    (TimeoutError, "timeout", StatusCode.DEADLINE_EXCEEDED),
    (ConnectionError, "connection_error", StatusCode.UNAVAILABLE),
    (OSError, "device_error", StatusCode.INTERNAL),
)

SUPPORTED_CONTENT_ENCODINGS = (
    {}
    if os.environ.get(JMP_DISABLE_COMPRESSION) == "1"
    else {
        Compression.GZIP,
        Compression.XZ,
        Compression.BZ2,
        Compression.ZSTD,
    }
)


@dataclass(kw_only=True)
class Driver(
    Metadata,
    jumpstarter_pb2_grpc.ExporterServiceServicer,
    router_pb2_grpc.RouterServiceServicer,
    metaclass=ABCMeta,
):
    """Base class for drivers

    Drivers should at the minimum implement the `client` method.

    Regular or streaming driver calls can be marked with the `export` decorator.
    Raw stream constructors can be marked with the `exportstream` decorator.
    """

    driver_type: ClassVar[str] = "other"
    """Driver category for observability (e.g. power, storage, network, serial, console, video, composite)."""

    children: dict[str, Driver] = field(default_factory=dict)

    resources: dict[UUID, Any] = field(default_factory=dict, init=False)
    """Dict of client side resources"""

    description: str | None = None
    """Custom description for the driver (shown in CLI help)"""

    methods_description: dict[str, str] = field(default_factory=dict)
    """Map of method names to their help descriptions (configurable via server config)"""

    log_level: str = "INFO"
    logger: logging.Logger = field(init=False)

    def __post_init__(self):
        if hasattr(super(), "__post_init__"):
            super().__post_init__()

        self.logger = get_logger(f"driver.{self.__class__.__name__}", LogSource.DRIVER)
        self.logger.setLevel(self.log_level)

    def close(self):
        for child in self.children.values():
            child.close()

    def reset(self):
        for child in self.children.values():
            child.reset()

    @classmethod
    @abstractmethod
    def client(cls) -> str:
        """
        Return full import path of the corresponding driver client class
        """

    def extra_labels(self) -> dict[str, str]:
        return {}

    def _record_operation_metrics(
        self,
        *,
        operation: str,
        result: OperationResult,
        duration_seconds: float,
        error_type: ErrorType | None = None,
    ) -> None:
        # Metrics must never discard a computed gRPC response or change the
        # abort status: keep recording failures isolated from the RPC path.
        try:
            get_registry().record_operation(
                exporter=exporter_from_log_context(default=self.name if hasattr(self, "name") else "unknown"),
                operation=operation,
                result=result,
                driver_type=self.driver_type,
                duration_seconds=duration_seconds,
                exemplars=exemplars_from_log_context(),
                error_type=error_type,
            )
        except Exception:
            self.logger.warning(
                "Failed to record operation metrics",
                extra={
                    "operation": operation,
                    "driver_type": self.driver_type,
                    "result": result,
                    "error_type": error_type,
                },
                exc_info=True,
            )

    async def _handle_driver_exception(
        self,
        exc: BaseException,
        op: str,
        started: float,
        context: grpc.aio.ServicerContext,
    ) -> None:
        for exc_type, error_type, status in _DRIVER_CALL_ERRORS:
            if isinstance(exc, exc_type):
                self._record_operation_metrics(
                    operation=op,
                    result="failure",
                    duration_seconds=time.perf_counter() - started,
                    error_type=error_type,
                )
                self.logger.warning(
                    "Operation failed",
                    extra={
                        "operation": op,
                        "driver_type": self.driver_type,
                        "result": "failure",
                        "error_type": error_type,
                    },
                )
                await context.abort(status, str(exc))
                return
        self._record_operation_metrics(
            operation=op,
            result="failure",
            duration_seconds=time.perf_counter() - started,
            error_type="internal_error",
        )
        self.logger.warning(
            "Operation failed",
            extra={
                "operation": op,
                "driver_type": self.driver_type,
                "result": "failure",
                "error_type": "internal_error",
            },
        )
        await context.abort(StatusCode.UNKNOWN, str(exc))

    async def DriverCall(self, request, context):
        """
        :meta private:
        """
        op = request.method
        started = time.perf_counter()
        self.logger.info(
            "Operation started",
            extra={"operation": op, "driver_type": self.driver_type},
        )
        try:
            method = await self.__lookup_drivercall(request.method, context, MARKER_DRIVERCALL)

            args = [decode_value(arg) for arg in request.args]

            if iscoroutinefunction(method):
                result = await method(*args)
            else:
                result = await to_thread.run_sync(method, *args)

            # Encode before recording success so serialization failures are
            # counted only as failures (not success then failure).
            response = jumpstarter_pb2.DriverCallResponse(
                uuid=str(uuid4()),
                result=encode_value(result),
            )
            self._record_operation_metrics(
                operation=op,
                result="success",
                duration_seconds=time.perf_counter() - started,
            )
            self.logger.info(
                "Operation completed",
                extra={"operation": op, "driver_type": self.driver_type, "result": "success"},
            )
            return response
        except grpc.aio.AbortError:
            # Propagate context.abort() from lookup/handlers without recording
            # metrics (avoids client-controlled operation label cardinality).
            raise
        except Exception as e:
            await self._handle_driver_exception(e, op, started, context)

    async def StreamingDriverCall(self, request, context):
        """
        :meta private:
        """
        op = request.method
        started = time.perf_counter()
        self.logger.info(
            "Operation started",
            extra={"operation": op, "driver_type": self.driver_type},
        )
        try:
            method = await self.__lookup_drivercall(request.method, context, MARKER_STREAMING_DRIVERCALL)

            args = [decode_value(arg) for arg in request.args]

            if isasyncgenfunction(method):
                async for result in method(*args):
                    yield jumpstarter_pb2.StreamingDriverCallResponse(
                        uuid=str(uuid4()),
                        result=encode_value(result),
                    )
            else:
                for result in await to_thread.run_sync(method, *args):
                    yield jumpstarter_pb2.StreamingDriverCallResponse(
                        uuid=str(uuid4()),
                        result=encode_value(result),
                    )
            self._record_operation_metrics(
                operation=op,
                result="success",
                duration_seconds=time.perf_counter() - started,
            )
            self.logger.info(
                "Operation completed",
                extra={"operation": op, "driver_type": self.driver_type, "result": "success"},
            )
        except grpc.aio.AbortError:
            # Propagate context.abort() from lookup/handlers without recording
            # metrics (avoids client-controlled operation label cardinality).
            raise
        except Exception as e:
            await self._handle_driver_exception(e, op, started, context)

    @asynccontextmanager
    async def Stream(self, request, context):
        """
        :meta private:
        """
        match request:
            case DriverStreamRequest(method=driver_method):
                method = await self.__lookup_drivercall(driver_method, context, MARKER_STREAMCALL)

                async with method() as stream:
                    yield stream

            case ResourceStreamRequest():
                remote, resource = create_memory_stream()

                resource_uuid = uuid4()

                self.resources[resource_uuid] = resource

                async with MetadataStream(
                    stream=remote,
                    metadata=ResourceMetadata.model_construct(
                        resource=ClientStreamResource(
                            uuid=resource_uuid, x_jmp_content_encoding=request.x_jmp_content_encoding
                        ),
                        x_jmp_accept_encoding=request.x_jmp_content_encoding
                        if request.x_jmp_content_encoding in SUPPORTED_CONTENT_ENCODINGS
                        else None,
                    ).model_dump(mode="json", round_trip=True),
                ) as stream:
                    yield stream

    def report(self, *, parent=None, name=None):
        """
        Create DriverInstanceReport

        :meta private:
        """
        return jumpstarter_pb2.DriverInstanceReport(
            uuid=str(self.uuid),
            parent_uuid=str(parent.uuid) if parent else None,
            labels=self.labels
            | self.extra_labels()
            | ({"jumpstarter.dev/client": self.client()})
            | ({"jumpstarter.dev/name": name} if name else {}),
            description=self.description or None,
            methods_description=self.methods_description or {},
        )

    def enumerate(self, *, root=None, parent=None, name=None):
        """
        Get list of self and child devices

        :meta private:
        """
        if root is None:
            root = self

        return [(self.uuid, parent, name, self)] + list(
            chain(*[child.enumerate(root=root, parent=self, name=cname) for (cname, child) in self.children.items()])
        )

    @asynccontextmanager
    async def _resource_from_client_stream(self, resource_uuid: UUID, content_encoding):
        async with self.resources[resource_uuid] as stream:
            try:
                yield compress_stream(stream, content_encoding)
            finally:
                del self.resources[resource_uuid]

    @staticmethod
    def _make_url(url: str) -> yarl.URL:
        """Construct a yarl.URL preserving percent-encoding in the path.

        yarl.URL() normalizes %XX sequences (e.g. %40 → @), which breaks
        signed redirect URLs (CloudFront, S3) whose signatures cover the
        encoded form.  Using encoded=True keeps the raw string intact.
        """
        return yarl.URL(url, encoded=True)

    @staticmethod
    def _redact_url(url: str) -> str:
        """Redact query parameters from a URL to avoid leaking credentials in logs."""
        parsed = urlparse(url)
        if parsed.query:
            return urlunparse(parsed._replace(query="[REDACTED]"))
        return url

    _SENSITIVE_HEADER_PREFIXES = ("authorization", "cookie", "proxy-authorization", "x-amz-", "x-ms-", "x-goog-")

    @classmethod
    def _strip_sensitive_headers(
        cls, headers: dict[str, str], original_url: str, redirect_url: str
    ) -> dict[str, str]:
        """Strip auth headers when a redirect crosses origins."""
        orig = yarl.URL(original_url)
        dest = yarl.URL(redirect_url)
        if (orig.scheme, orig.host, orig.port) == (dest.scheme, dest.host, dest.port):
            return headers
        return {
            k: v for k, v in headers.items()
            if not k.lower().startswith(cls._SENSITIVE_HEADER_PREFIXES)
        }

    @asynccontextmanager
    async def _presigned_get(
        self, url: str, headers: dict[str, str], timeout: aiohttp.ClientTimeout, max_redirects: int = 10
    ):
        """GET with manual redirect following to preserve percent-encoding in URLs."""
        current_url = url
        current_headers = headers
        for _ in range(max_redirects + 1):
            async with aiohttp.request(
                "GET", self._make_url(current_url), headers=current_headers, raise_for_status=True,
                timeout=timeout, allow_redirects=False,
            ) as resp:
                if resp.status not in (301, 302, 303, 307, 308):
                    async with AiohttpStreamReaderStream(
                        reader=resp.content, content_length=resp.content_length,
                    ) as stream:
                        yield ProgressStream(stream=stream, logging=True)
                        return
                location = resp.headers.get("Location", "")
                if not location:
                    raise RuntimeError(
                        f"Presigned HTTP GET redirect missing Location header for {self._redact_url(current_url)}"
                    )
                current_headers = self._strip_sensitive_headers(current_headers, current_url, location)
                current_url = location
        raise RuntimeError(f"Too many redirects ({max_redirects}) for {self._redact_url(url)}")

    @asynccontextmanager
    async def _resource_from_presigned(self, headers, url: str, method: str, timeout: int):
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        try:
            match method:
                case "GET":
                    async with self._presigned_get(url, headers, client_timeout) as stream:
                        yield stream
                case "PUT":
                    remote, stream = create_memory_stream()
                    async with aiohttp.request(
                        method, self._make_url(url), headers=headers, raise_for_status=True,
                        data=remote, timeout=client_timeout,
                    ) as _resp:
                        async with stream:
                            yield ProgressStream(stream=stream, logging=True)
                case _:
                    # INVARIANT: method is always one of GET or PUT, see PresignedRequestResource
                    raise ValueError("unreachable")
        except aiohttp.ClientResponseError as e:
            safe_url = self._redact_url(url)
            raise RuntimeError(
                f"Presigned HTTP {method} request failed: status={e.status}, reason={e.message!r}, url={safe_url}"
            ) from e
        except BrokenResourceError as e:
            safe_url = self._redact_url(url)
            cause = e.__cause__
            if cause is not None:
                raise RuntimeError(
                    f"Presigned HTTP {method} stream interrupted for {safe_url}: {type(cause).__name__}: {cause!s}"
                ) from e
            raise RuntimeError(f"Presigned HTTP {method} stream interrupted for {safe_url}") from e
        except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, aiohttp.ServerTimeoutError) as e:
            safe_url = self._redact_url(url)
            raise RuntimeError(
                f"Presigned HTTP {method} stream failed (connection/read error) for {safe_url}: "
                f"{type(e).__name__}: {e!s}"
            ) from e
        except TimeoutError as e:
            safe_url = self._redact_url(url)
            raise TimeoutError(
                f"Presigned HTTP {method} request timed out after {timeout}s for {safe_url}"
            ) from e
        except OSError as e:
            safe_url = self._redact_url(url)
            raise RuntimeError(
                f"Presigned HTTP {method} stream failed with OS error for {safe_url}: {type(e).__name__}: {e!s}"
            ) from e

    @asynccontextmanager
    async def resource(self, handle: str, timeout: int = 7200):
        handle = TypeAdapter(Resource).validate_python(handle)
        match handle:
            case ClientStreamResource(uuid=uuid, x_jmp_content_encoding=content_encoding):
                async with self._resource_from_client_stream(uuid, content_encoding) as stream:
                    yield stream
            case PresignedRequestResource(headers=headers, url=url, method=method):
                async with self._resource_from_presigned(headers, url, method, timeout) as stream:
                    yield stream

    async def __lookup_drivercall(self, name, context, marker):
        """Lookup drivercall by method name

        Methods are checked against magic markers
        to avoid accidentally calling non-exported
        methods
        """
        method = getattr(self, name, None)

        if method is None:
            await context.abort(StatusCode.NOT_FOUND, f"method {name} not found on driver")

        if getattr(method, marker, None) != MARKER_MAGIC:
            await context.abort(StatusCode.NOT_FOUND, f"method {name} missing marker {marker}")

        return method
