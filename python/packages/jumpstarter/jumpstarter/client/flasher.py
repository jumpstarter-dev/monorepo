from __future__ import annotations

import warnings
from abc import ABCMeta, abstractmethod
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from os import PathLike
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, cast

import click
from anyio import BrokenResourceError, EndOfStream
from anyio.abc import ObjectStream
from pydantic import BaseModel

from jumpstarter.client import DriverClient
from jumpstarter.client.adapters import blocking
from jumpstarter.client.decorators import driver_click_group
from jumpstarter.common.resources import PresignedRequestResource
from jumpstarter.streams.encoding import Compression
from jumpstarter.streams.progress import ProgressAttribute

PathBuf = str | PathLike


class FlashPhase:
    """Standard phase constants for ``FlashStatus`` updates."""

    DOWNLOAD = "download"
    """Firmware archive is being received / downloaded."""

    EXTRACT = "extract"
    """Archive is being extracted to disk."""

    STEP = "step"
    """A manifest step (QDL, fastboot, mode switch, etc.) is running."""

    CACHE = "cache"
    """Cached firmware is being reused or has been stored."""

    COMPLETE = "complete"
    """The flash operation finished successfully."""

    ERROR = "error"
    """The flash operation failed."""


class FlashStatus(BaseModel):
    """Progress update emitted during a streaming flash operation."""

    phase: str
    message: str
    step_index: int | None = None
    total_steps: int | None = None
    step_name: str | None = None
    progress: float | None = None
    bytes_transferred: int | None = None
    bytes_total: int | None = None
    stdout: str | None = None
    stderr: str | None = None


@dataclass(kw_only=True)
class _AsyncIteratorStream(ObjectStream[bytes]):
    """Wraps an async iterator as an ObjectStream for resource_async."""

    iterator: AsyncGenerator[bytes, None]
    total: int | None = None

    async def receive(self) -> bytes:
        try:
            return await self.iterator.__anext__()
        except StopAsyncIteration:
            raise EndOfStream from None

    async def send(self, item: bytes):
        raise BrokenResourceError("read-only stream")

    async def send_eof(self):
        pass

    async def aclose(self):
        await self.iterator.aclose()

    @property
    def extra_attributes(self) -> Mapping[Any, Callable[[], Any]]:
        if self.total is not None and self.total > 0:
            return {ProgressAttribute.total: lambda: float(self.total)}
        return {}


@dataclass(kw_only=True)
class _FileWriteObjectStream(ObjectStream[bytes]):
    """Wraps a file path as a writable ObjectStream for resource_async."""

    path: Path
    _file: Any = field(default=None, init=False)

    async def receive(self) -> bytes:
        raise EndOfStream

    async def send(self, item: bytes):
        if self._file is None:
            import anyio

            self._file = await anyio.open_file(self.path, "wb")
        await self._file.write(item)

    async def send_eof(self):
        if self._file is not None:
            await self._file.aclose()
            self._file = None

    async def aclose(self):
        if self._file is not None:
            await self._file.aclose()
            self._file = None


def _parse_path(path: PathBuf) -> tuple[Path, None] | tuple[None, str]:
    """Parse a path into either a local Path or an HTTP URL.

    Returns (local_path, None) for local files, or (None, url) for HTTP URLs.
    """
    path_str = str(path)
    if path_str.startswith(("http://", "https://")):
        return None, path_str
    return Path(path).resolve(), None


@blocking
@asynccontextmanager
async def _local_file_adapter(
    *,
    client: DriverClient,
    path: Path,
    mode: Literal["rb", "wb"] = "rb",
    compression: Compression | None = None,
):
    """Stream a local file via resource_async, without opendal."""
    import anyio

    if mode == "rb":
        # Read mode: stream file content to exporter
        file_size = path.stat().st_size

        async def file_reader():
            async with await anyio.open_file(path, "rb") as f:
                while True:
                    chunk = await f.read(65536)
                    if not chunk:
                        break
                    yield chunk

        stream = _AsyncIteratorStream(
            iterator=file_reader(),
            total=file_size,
        )

        async with client.resource_async(stream, content_encoding=compression) as res:
            yield res
    else:
        # Write mode: receive content from exporter into file
        stream = _FileWriteObjectStream(path=path)
        async with client.resource_async(stream, content_encoding=compression) as res:
            yield res


@blocking
@asynccontextmanager
async def _http_url_adapter(
    *,
    client: DriverClient,
    url: str,
    mode: Literal["rb", "wb"] = "rb",
):
    """Create a PresignedRequestResource for an HTTP URL.

    The exporter already handles HTTP downloads via aiohttp,
    so we just pass the URL as a presigned GET request.
    """
    if mode == "rb":
        yield PresignedRequestResource(
            headers={},
            url=url,
            method="GET",
        ).model_dump(mode="json")
    else:
        yield PresignedRequestResource(
            headers={},
            url=url,
            method="PUT",
        ).model_dump(mode="json")


class FlasherClientInterface(metaclass=ABCMeta):
    @abstractmethod
    def flash(
        self,
        path: PathBuf | dict[str, PathBuf],
        *,
        target: str | None = None,
        compression: Compression | None = None,
    ) -> Any:
        """Flash image to DUT"""
        ...

    @abstractmethod
    def dump(
        self,
        path: PathBuf,
        *,
        target: str | None = None,
        compression: Compression | None = None,
    ) -> Any:
        """Dump image from DUT"""
        ...

    def cli(self) -> click.Group:
        @driver_click_group(self)
        def base():
            """Generic flasher interface"""
            pass

        @base.command()
        @click.argument("file", nargs=-1, required=False)
        @click.option(
            "--target",
            "-t",
            "target_specs",
            multiple=True,
            help="name:file",
        )
        @click.option("--compression", type=click.Choice(Compression, case_sensitive=False))
        def flash(file, target_specs, compression):
            if target_specs:
                mapping: dict[str, str] = {}
                for spec in target_specs:
                    if ":" not in spec:
                        raise click.ClickException(f"Invalid target spec '{spec}', expected name:file")
                    name, img = spec.split(":", 1)
                    mapping[name] = img
                self.flash(cast(dict[str, PathBuf], mapping), compression=compression)
                return

            if not file:
                raise click.ClickException("FILE argument is required unless --target/-t is used")

            self.flash(file[0], target=None, compression=compression)

        @base.command()
        @click.argument("file")
        @click.option("--target", type=str)
        @click.option("--compression", type=click.Choice(Compression, case_sensitive=False))
        def dump(file, target, compression):
            """Dump image from DUT to file"""
            self.dump(file, target=target, compression=compression)

        return base


class FlasherClient(FlasherClientInterface, DriverClient):
    def _flash_single(
        self,
        image: PathBuf,
        *,
        target: str | None,
        compression: Compression | None,
    ) -> Any:
        """Flash image to DUT"""
        local_path, url = _parse_path(image)

        if url is not None:
            if compression is not None:
                warnings.warn(
                    "compression parameter is ignored for HTTP URLs",
                    stacklevel=2,
                )
            # HTTP URL: pass as presigned request for exporter-side download
            with _http_url_adapter(client=self, url=url, mode="rb") as handle:
                return self.call("flash", handle, target)
        else:
            # Local file: stream via resource_async
            with _local_file_adapter(client=self, path=local_path, mode="rb", compression=compression) as handle:
                return self.call("flash", handle, target)

    def flash(
        self,
        path: PathBuf | dict[str, PathBuf],
        *,
        target: str | None = None,
        compression: Compression | None = None,
    ) -> Any:
        if isinstance(path, dict):
            if target is not None:
                from jumpstarter.common.exceptions import ArgumentError

                raise ArgumentError("'target' parameter is not valid when flashing multiple images")

            results: dict[str, object] = {}
            for part, img in path.items():
                results[part] = self._flash_single(img, target=part, compression=compression)
            return results

        return self._flash_single(path, target=target, compression=compression)

    def dump(
        self,
        path: PathBuf,
        *,
        target: str | None = None,
        compression: Compression | None = None,
    ) -> Any:
        """Dump image from DUT"""
        local_path, url = _parse_path(path)

        if url is not None:
            if compression is not None:
                warnings.warn(
                    "compression parameter is ignored for HTTP URLs",
                    stacklevel=2,
                )
            with _http_url_adapter(client=self, url=url, mode="wb") as handle:
                return self.call("dump", handle, target)
        else:
            with _local_file_adapter(client=self, path=local_path, mode="wb", compression=compression) as handle:
                return self.call("dump", handle, target)


class StreamingFlasherClientInterface(FlasherClientInterface):
    @abstractmethod
    def flash_stream(
        self,
        path: PathBuf,
        *,
        manifest: Any | None = None,
        compression: Compression | None = None,
    ) -> Generator[FlashStatus, None, None]:
        """Flash image to DUT, yielding progress updates."""
        ...


class StreamingFlasherClient(FlasherClient, StreamingFlasherClientInterface):
    def _iter_flash_status(
        self,
        *,
        handle: Any,
        manifest: Any | None,
    ) -> Generator[FlashStatus, None, None]:
        for value in self.streamingcall("flash", handle, manifest):
            status = FlashStatus.model_validate(value)
            yield status
            if status.phase == FlashPhase.ERROR:
                raise RuntimeError(status.message)

    def flash_stream(
        self,
        path: PathBuf,
        *,
        manifest: Any | None = None,
        compression: Compression | None = None,
    ) -> Generator[FlashStatus, None, None]:
        local_path, url = _parse_path(path)

        if url is not None:
            if compression is not None:
                warnings.warn(
                    "compression parameter is ignored for HTTP URLs",
                    stacklevel=2,
                )
            with _http_url_adapter(client=self, url=url, mode="rb") as handle:
                yield from self._iter_flash_status(handle=handle, manifest=manifest)
        else:
            with _local_file_adapter(client=self, path=local_path, mode="rb", compression=compression) as handle:
                yield from self._iter_flash_status(handle=handle, manifest=manifest)

    def flash(
        self,
        path: PathBuf | dict[str, PathBuf],
        *,
        target: str | None = None,
        compression: Compression | None = None,
    ) -> FlashStatus:
        if isinstance(path, dict):
            from jumpstarter.common.exceptions import ArgumentError

            raise ArgumentError("StreamingFlasherClient does not support multi-target flash mappings")

        if target is not None:
            from jumpstarter.common.exceptions import ArgumentError

            raise ArgumentError("'target' parameter is not supported by StreamingFlasherClient")

        last: FlashStatus | None = None
        for status in self.flash_stream(path, compression=compression):
            last = status
        if last is None:
            raise RuntimeError("flash completed without status updates")
        if last.phase != FlashPhase.COMPLETE:
            raise RuntimeError(last.message or "flash did not complete successfully")
        return last

    @staticmethod
    def render_flash_status(status: FlashStatus, *, verbose: bool = False) -> str:
        parts = [status.phase.upper(), status.message]
        if status.step_index is not None and status.total_steps is not None:
            parts.append(f"step {status.step_index}/{status.total_steps}")
        if status.step_name:
            parts.append(status.step_name)
        if status.progress is not None:
            parts.append(f"{status.progress * 100:.1f}%")
        if status.bytes_transferred is not None and status.bytes_total is not None:
            parts.append(f"{status.bytes_transferred}/{status.bytes_total} bytes")
        result = " | ".join(parts)
        if verbose:
            if status.stdout:
                result += f"\n  [stdout] {status.stdout.rstrip()}"
            if status.stderr:
                result += f"\n  [stderr] {status.stderr.rstrip()}"
        return result

    def cli(self) -> click.Group:
        @driver_click_group(self)
        def base():
            """Streaming flasher interface"""
            pass

        @base.command()
        @click.argument("file")
        @click.option("--compression", type=click.Choice(Compression, case_sensitive=False))
        def flash(file, compression):
            """Flash image to DUT with progress updates"""
            for status in self.flash_stream(file, compression=compression):
                click.echo(self.render_flash_status(status))

        @base.command()
        @click.argument("file")
        @click.option("--target", type=str)
        @click.option("--compression", type=click.Choice(Compression, case_sensitive=False))
        def dump(file, target, compression):
            """Dump image from DUT to file"""
            super().dump(file, target=target, compression=compression)

        return base
