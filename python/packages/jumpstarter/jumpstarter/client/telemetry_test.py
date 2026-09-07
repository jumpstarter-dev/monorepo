"""Tests for jmp client telemetry: GetServiceEndpoints + PushLogs (JEP-0013 Phase 3)."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import grpc
import pytest

from jumpstarter.client.telemetry import attach_client_telemetry

pytestmark = pytest.mark.anyio


def make_endpoint(endpoint="telemetry:19093", certificate="", min_severity="info"):
    ep = MagicMock()
    ep.endpoint = endpoint
    ep.certificate = certificate
    ep.min_severity = min_severity
    return ep


def make_config(*, token="tok", namespace="jumpstarter", insecure=False):
    tls = SimpleNamespace(insecure=insecure)
    metadata = SimpleNamespace(namespace=namespace)

    config = SimpleNamespace(
        token=token,
        tls=tls,
        metadata=metadata,
        grpcOptions={},
    )

    async def channel():
        return MagicMock(name="controller-channel")

    config.channel = channel
    return config


async def test_attach_skips_when_no_endpoints():
    resp = MagicMock()
    resp.telemetry_endpoints = []
    stub = AsyncMock()
    stub.GetServiceEndpoints = AsyncMock(return_value=resp)

    with (
        patch("jumpstarter.client.telemetry.jumpstarter_pb2_grpc.ControllerServiceStub", return_value=stub),
        patch("jumpstarter.client.telemetry.logging.getLogger") as get_logger,
    ):
        session = await attach_client_telemetry(make_config())

    assert session is None
    get_logger.return_value.addHandler.assert_not_called()


async def test_attach_swallows_rpc_errors():
    stub = AsyncMock()
    stub.GetServiceEndpoints = AsyncMock(
        side_effect=grpc.aio.AioRpcError(
            code=grpc.StatusCode.UNIMPLEMENTED,
            initial_metadata=None,
            trailing_metadata=None,
        )
    )

    with patch("jumpstarter.client.telemetry.jumpstarter_pb2_grpc.ControllerServiceStub", return_value=stub):
        session = await attach_client_telemetry(make_config())

    assert session is None


async def test_attach_adds_cli_handler_without_replacing_stdout():
    resp = MagicMock()
    resp.telemetry_endpoints = [make_endpoint(min_severity="warning")]
    stub = AsyncMock()
    stub.GetServiceEndpoints = AsyncMock(return_value=resp)

    root = logging.getLogger()
    before = list(root.handlers)

    with (
        patch("jumpstarter.client.telemetry.jumpstarter_pb2_grpc.ControllerServiceStub", return_value=stub),
        patch("jumpstarter.client.telemetry.grpc.aio.insecure_channel") as insecure,
        patch("jumpstarter.client.telemetry.telemetry_pb2_grpc.TelemetryServiceStub") as tel_stub,
    ):
        insecure.return_value = MagicMock()
        tel_stub.return_value = MagicMock()
        session = await attach_client_telemetry(make_config(insecure=True))

    try:
        assert session is not None
        assert session.handler.component == "cli"
        assert session.handler.level == logging.WARNING
        assert session.handler in root.handlers
        for h in before:
            assert h in root.handlers
    finally:
        if session is not None:
            await session.aclose()
