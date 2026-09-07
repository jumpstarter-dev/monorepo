"""Client-side telemetry: discover the hub and push jmp logs via PushLogs."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import grpc
from jumpstarter_protocol import jumpstarter_pb2, jumpstarter_pb2_grpc, telemetry_pb2_grpc

from jumpstarter.config.env import JMP_GRPC_INSECURE, JUMPSTARTER_GRPC_INSECURE
from jumpstarter.exporter.telemetry import TelemetryLogHandler

logger = logging.getLogger(__name__)

_RPC_TIMEOUT = 10.0


def _severity_to_level(severity: str) -> int:
    return {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
        "critical": logging.CRITICAL,
    }.get((severity or "").lower(), logging.INFO)


@dataclass
class ClientTelemetry:
    handler: TelemetryLogHandler
    channel: grpc.aio.Channel

    async def aclose(self) -> None:
        try:
            await self.handler.close_async()
        except Exception:
            logger.debug("telemetry handler close failed", exc_info=True)
        logging.getLogger().removeHandler(self.handler)
        try:
            await self.channel.close()
        except Exception:
            logger.debug("telemetry channel close failed", exc_info=True)


def _telemetry_channel(endpoint: str, certificate: str, insecure: bool) -> grpc.aio.Channel:
    grpc_insecure = insecure or os.getenv(JMP_GRPC_INSECURE) == "1" or os.getenv(JUMPSTARTER_GRPC_INSECURE) == "1"
    if certificate:
        return grpc.aio.secure_channel(
            endpoint,
            grpc.ssl_channel_credentials(root_certificates=certificate.encode()),
        )
    if grpc_insecure:
        return grpc.aio.insecure_channel(endpoint)
    return grpc.aio.secure_channel(endpoint, grpc.ssl_channel_credentials())


async def attach_client_telemetry(config) -> ClientTelemetry | None:
    """Best-effort GetServiceEndpoints + TelemetryLogHandler for jmp.

    Human-facing stdout handlers are left in place; this only adds a PushLogs
    handler when the controller advertises a telemetry endpoint.
    """
    try:
        channel = await config.channel()
        stub = jumpstarter_pb2_grpc.ControllerServiceStub(channel)
        resp = await stub.GetServiceEndpoints(
            jumpstarter_pb2.GetServiceEndpointsRequest(),
            timeout=_RPC_TIMEOUT,
        )
    except Exception as exc:
        logger.debug("GetServiceEndpoints unavailable: %s", exc)
        return None

    if not resp.telemetry_endpoints:
        logger.debug("No telemetry endpoint configured, skipping client telemetry")
        return None

    ep = resp.telemetry_endpoints[0]
    tls = getattr(config, "tls", None)
    insecure = bool(getattr(tls, "insecure", False))
    tel_channel = _telemetry_channel(ep.endpoint, ep.certificate or "", insecure)
    tel_stub = telemetry_pb2_grpc.TelemetryServiceStub(tel_channel)
    namespace = ""
    metadata = getattr(config, "metadata", None)
    if metadata is not None:
        namespace = getattr(metadata, "namespace", "") or ""
    handler = TelemetryLogHandler(
        tel_stub,
        namespace=namespace,
        token=getattr(config, "token", None) or "",
        component="cli",
    )
    handler.setLevel(_severity_to_level(ep.min_severity))
    logging.getLogger().addHandler(handler)
    logger.info("Telemetry log handler attached (component=cli)")
    return ClientTelemetry(handler=handler, channel=tel_channel)
