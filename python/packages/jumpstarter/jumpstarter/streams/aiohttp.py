from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from aiohttp import ClientError, StreamReader
from anyio import BrokenResourceError, EndOfStream
from anyio.abc import ObjectStream

from jumpstarter.streams.progress import ProgressAttribute


@dataclass(frozen=True, kw_only=True, slots=True)
class AiohttpStreamReaderStream(ObjectStream[bytes]):
    reader: StreamReader
    content_length: int | None = field(default=None)

    @property
    def extra_attributes(self) -> Mapping[Any, Callable[[], Any]]:
        if self.content_length is not None and self.content_length > 0:
            return {ProgressAttribute.total: lambda: float(self.content_length)}
        return {}

    async def send(self, item: bytes):
        raise BrokenResourceError

    async def receive(self) -> bytes:
        try:
            item = await self.reader.readany()
        except ClientError as e:
            raise BrokenResourceError from e
        if len(item) == 0:
            raise EndOfStream
        return item

    async def send_eof(self):
        pass

    async def aclose(self):
        pass
