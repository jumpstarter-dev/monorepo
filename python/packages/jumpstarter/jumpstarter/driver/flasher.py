from __future__ import annotations

from abc import ABCMeta, abstractmethod
from collections.abc import AsyncGenerator
from typing import Any


class FlasherInterface(metaclass=ABCMeta):
    driver_type = "storage"

    @classmethod
    def client(cls) -> str:
        return "jumpstarter.client.flasher.FlasherClient"

    @abstractmethod
    def flash(self, source: Any, target: str | None = None) -> None: ...

    @abstractmethod
    def dump(self, target: Any, partition: str | None = None) -> None: ...


class StreamingFlasherInterface(metaclass=ABCMeta):
    """Interface for flash drivers that report progress via an async stream.

    Drivers extending this interface yield ``FlashStatus`` updates during
    the flash process, enabling real-time progress reporting to the client.
    """

    driver_type = "storage"

    @classmethod
    def client(cls) -> str:
        return "jumpstarter.client.flasher.StreamingFlasherClient"

    @abstractmethod
    async def flash(
        self, source: Any, manifest: Any | None = None
    ) -> AsyncGenerator[Any, None]:
        """Flash firmware to the device, yielding progress updates.

        Args:
            source: Firmware source (file handle, presigned URL, etc.).
            manifest: Optional manifest data for the firmware.

        Yields:
            ``FlashStatus`` instances describing progress.
        """
        ...
        # Make this a valid async generator for type checkers.
        yield  # pragma: no cover
