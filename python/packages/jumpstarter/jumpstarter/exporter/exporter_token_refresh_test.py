import base64
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import grpc
import pytest
from jumpstarter_protocol import jumpstarter_pb2

from jumpstarter.config.common import ObjectMeta
from jumpstarter.config.exporter import ExporterConfigV1Alpha1
from jumpstarter.config.tls import TLSConfigV1Alpha1
from jumpstarter.exporter import Exporter


def _make_jwt(payload: dict) -> str:
    header = {"alg": "ES256", "typ": "JWT"}
    h_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b"=").decode()
    p_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{h_b64}.{p_b64}.dummy_signature"


class FakeRpcError(grpc.aio.AioRpcError):
    def __init__(self, code, details="error"):
        self._code = code
        self._details = details

    def code(self):
        return self._code

    def details(self):
        return self._details


@pytest.mark.anyio
async def test_exporter_rotate_token_success():
    mock_channel = MagicMock(spec=grpc.aio.Channel)
    mock_channel.close = AsyncMock()

    mock_ctrl = AsyncMock()
    mock_ctrl.RotateToken.return_value = jumpstarter_pb2.RotateTokenResponse(token="rotated-token-value")

    async def channel_factory():
        return mock_channel

    on_rotated = AsyncMock()
    mock_telemetry_handler = MagicMock()
    mock_telemetry_handler.token = "old-token"

    exporter = Exporter(
        exporter_name="test-exporter",
        token="old-token",
        channel_factory=channel_factory,
        device_factory=lambda: MagicMock(),
        on_token_rotated=on_rotated,
    )
    exporter._telemetry_handler = mock_telemetry_handler

    with patch("jumpstarter.exporter.exporter.jumpstarter_pb2_grpc.ControllerServiceStub", return_value=mock_ctrl):
        token = await exporter.rotate_token()

    assert token == "rotated-token-value"
    assert exporter.token == "rotated-token-value"
    assert mock_telemetry_handler.token == "rotated-token-value"
    on_rotated.assert_awaited_once_with("rotated-token-value")
    mock_ctrl.RotateToken.assert_awaited_once()


@pytest.mark.anyio
async def test_exporter_rotate_token_sync_callback():
    mock_channel = MagicMock(spec=grpc.aio.Channel)
    mock_channel.close = AsyncMock()

    mock_ctrl = AsyncMock()
    mock_ctrl.RotateToken.return_value = jumpstarter_pb2.RotateTokenResponse(token="rotated-token-value")

    async def channel_factory():
        return mock_channel

    recorded_tokens = []

    def sync_on_rotated(token: str) -> None:
        recorded_tokens.append(token)

    exporter = Exporter(
        exporter_name="test-exporter",
        token="old-token",
        channel_factory=channel_factory,
        device_factory=lambda: MagicMock(),
        on_token_rotated=sync_on_rotated,
    )

    with patch("jumpstarter.exporter.exporter.jumpstarter_pb2_grpc.ControllerServiceStub", return_value=mock_ctrl):
        token = await exporter.rotate_token()

    assert token == "rotated-token-value"
    assert exporter.token == "rotated-token-value"
    assert recorded_tokens == ["rotated-token-value"]
    mock_ctrl.RotateToken.assert_awaited_once()


@pytest.mark.anyio
async def test_exporter_rotate_token_callback_failure():
    mock_channel = MagicMock(spec=grpc.aio.Channel)
    mock_channel.close = AsyncMock()

    mock_ctrl = AsyncMock()
    mock_ctrl.RotateToken.return_value = jumpstarter_pb2.RotateTokenResponse(token="rotated-token-value")

    async def channel_factory():
        return mock_channel

    on_rotated = AsyncMock(side_effect=RuntimeError("disk full or save failed"))

    exporter = Exporter(
        exporter_name="test-exporter",
        token="old-token",
        channel_factory=channel_factory,
        device_factory=lambda: MagicMock(),
        on_token_rotated=on_rotated,
    )

    with patch("jumpstarter.exporter.exporter.jumpstarter_pb2_grpc.ControllerServiceStub", return_value=mock_ctrl):
        token = await exporter.rotate_token()

    # Normal rotation result is preserved and token is updated
    assert token == "rotated-token-value"
    assert exporter.token == "rotated-token-value"
    on_rotated.assert_awaited_once_with("rotated-token-value")
    mock_ctrl.RotateToken.assert_awaited_once()


@pytest.mark.anyio
async def test_exporter_token_refresh_loop_near_expiry():
    # Token expiring in 1 second, with lead time 10s -> refresh sleep is 0s (immediate)
    now = time.time()
    token = _make_jwt({"iat": now - 100, "exp": now + 1})

    mock_channel = MagicMock(spec=grpc.aio.Channel)
    mock_channel.close = AsyncMock()

    mock_ctrl = AsyncMock()
    # Next token valid for 1000s so it doesn't loop infinitely
    next_token = _make_jwt({"iat": now, "exp": now + 100000})
    mock_ctrl.RotateToken.return_value = jumpstarter_pb2.RotateTokenResponse(token=next_token)

    async def channel_factory():
        return mock_channel

    exporter = Exporter(
        exporter_name="test-exporter",
        token=token,
        channel_factory=channel_factory,
        device_factory=lambda: MagicMock(),
        token_refresh_lead_time=10.0,
    )

    with patch("jumpstarter.exporter.exporter.jumpstarter_pb2_grpc.ControllerServiceStub", return_value=mock_ctrl):
        with anyio.move_on_after(0.5) as cancel_scope:
            await exporter._token_refresh_loop()

    assert cancel_scope.cancel_called
    assert exporter.token == next_token
    mock_ctrl.RotateToken.assert_awaited_once()


@pytest.mark.anyio
async def test_exporter_token_refresh_loop_unimplemented():
    now = time.time()
    token = _make_jwt({"iat": now - 100, "exp": now + 1})

    mock_channel = MagicMock(spec=grpc.aio.Channel)
    mock_channel.close = AsyncMock()

    mock_ctrl = AsyncMock()
    mock_ctrl.RotateToken.side_effect = FakeRpcError(grpc.StatusCode.UNIMPLEMENTED)

    async def channel_factory():
        return mock_channel

    exporter = Exporter(
        exporter_name="test-exporter",
        token=token,
        channel_factory=channel_factory,
        device_factory=lambda: MagicMock(),
        token_refresh_lead_time=10.0,
    )

    with patch("jumpstarter.exporter.exporter.jumpstarter_pb2_grpc.ControllerServiceStub", return_value=mock_ctrl):
        # Should gracefully return without hanging or raising
        with anyio.fail_after(2.0):
            await exporter._token_refresh_loop()

    # Token was not updated
    assert exporter.token == token
    mock_ctrl.RotateToken.assert_awaited_once()


@pytest.mark.anyio
async def test_exporter_token_refresh_loop_standalone_and_no_token():
    exporter_standalone = Exporter(
        exporter_name="test-exporter",
        token="dummy-token",
        channel_factory=AsyncMock(),
        device_factory=lambda: MagicMock(),
    )
    exporter_standalone._standalone = True
    # Should return immediately
    with anyio.fail_after(1.0):
        await exporter_standalone._token_refresh_loop()

    exporter_no_token = Exporter(
        exporter_name="test-exporter",
        token="",
        channel_factory=AsyncMock(),
        device_factory=lambda: MagicMock(),
    )
    # Should return immediately
    with anyio.fail_after(1.0):
        await exporter_no_token._token_refresh_loop()


@pytest.mark.anyio
async def test_config_on_token_rotated_saves_to_path(tmp_path):
    config_file = tmp_path / "exporter.yaml"
    cfg = ExporterConfigV1Alpha1(
        metadata=ObjectMeta(name="my-exporter", namespace="default"),
        endpoint="localhost:8082",
        token="initial-token",
        tls=TLSConfigV1Alpha1(insecure=True),
    )
    ExporterConfigV1Alpha1.save(cfg, path=str(config_file))
    loaded = ExporterConfigV1Alpha1.load_path(config_file)
    assert loaded.token == "initial-token"

    # Simulate create_exporter wiring
    async with loaded.create_exporter() as exporter:
        assert exporter.on_token_rotated is not None
        await exporter.on_token_rotated("new-saved-token")

    assert loaded.token == "new-saved-token"
    # Verify persisted to disk
    reloaded = ExporterConfigV1Alpha1.load_path(config_file)
    assert reloaded.token == "new-saved-token"
