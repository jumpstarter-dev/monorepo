from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

_HEX_RE = re.compile(r"([0-9a-fA-F]{2})*")


def _validate_hex_string(v: str) -> str:
    if not _HEX_RE.fullmatch(v):
        raise ValueError(f"payload must be a hex string of even length, got {v!r}")
    return v


class SomeIpPayload(BaseModel):
    """Hex-encoded SOME/IP payload for safe gRPC transport."""

    data: str

    @field_validator("data")
    @classmethod
    def _validate_hex(cls, v: str) -> str:
        return _validate_hex_string(v)


class SomeIpMessageResponse(BaseModel):
    """A received SOME/IP message."""

    service_id: int = Field(ge=0, le=0xFFFF)
    method_id: int = Field(ge=0, le=0xFFFF)
    client_id: int = Field(ge=0, le=0xFFFF)
    session_id: int = Field(ge=0, le=0xFFFF)
    protocol_version: int = 1
    interface_version: int = 1
    message_type: int
    return_code: int
    payload: str

    @field_validator("payload")
    @classmethod
    def _validate_hex(cls, v: str) -> str:
        return _validate_hex_string(v)


class SomeIpServiceEntry(BaseModel):
    """A SOME/IP service instance (for SD results)."""

    service_id: int = Field(ge=0, le=0xFFFF)
    instance_id: int = Field(ge=0, le=0xFFFF)
    major_version: int = 1
    minor_version: int = 0


class SomeIpEventNotification(BaseModel):
    """A SOME/IP event notification."""

    service_id: int = Field(ge=0, le=0xFFFF)
    event_id: int = Field(ge=0, le=0xFFFF)
    payload: str

    @field_validator("payload")
    @classmethod
    def _validate_hex(cls, v: str) -> str:
        return _validate_hex_string(v)


class SomeIpOfferedService(BaseModel):
    """A service instance the server is currently offering (server-side introspection)."""

    service_id: int = Field(ge=0, le=0xFFFF)
    instance_id: int = Field(ge=0, le=0xFFFF)
    major_version: int = Field(default=1, ge=0, le=0xFF)
    minor_version: int = Field(default=0, ge=0, le=0xFFFFFFFF)
