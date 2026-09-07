from unittest.mock import MagicMock, patch

from jumpstarter_driver_ridesx.qdl.client import (
    QualcommFlasherClient,
    _check_firmware,
    _load_manifest_source,
)
from jumpstarter_driver_ridesx.qdl.firmware_id import VersionInfo

MANIFEST_YAML = """
name: "SA8775P ES22 AWE Firmware"
data:
  folder: "r00002.2a_AWE"
steps:
  - sleep: 1
"""


def test_load_manifest_source_from_https_url():
    response = MagicMock()
    response.read.return_value = MANIFEST_YAML.encode("utf-8")
    response.__enter__.return_value = response

    with patch("jumpstarter_driver_ridesx.qdl.client.urlopen", return_value=response) as urlopen:
        manifest = _load_manifest_source("https://example.com/manifests/es22.yaml")

    urlopen.assert_called_once_with("https://example.com/manifests/es22.yaml")
    assert manifest["name"] == "SA8775P ES22 AWE Firmware"
    assert manifest["data"]["folder"] == "r00002.2a_AWE"


def test_iter_flash_status_coerces_float_to_int():
    """Protobuf struct_pb2.Value stores all numbers as doubles, so integer
    fields like bytes_transferred arrive as floats after gRPC round-tripping.
    model_validate must accept them without strict mode."""
    client = QualcommFlasherClient.__new__(QualcommFlasherClient)
    client.streamingcall = MagicMock(
        return_value=iter(
            [
                {
                    "phase": "download",
                    "message": "Received 450019 bytes",
                    "bytes_transferred": 450019.0,
                    "bytes_total": 2000000.0,
                },
                {"phase": "complete", "message": "done"},
            ]
        )
    )
    results = list(client._iter_flash_status(handle=None, manifest=None, cached=False))
    assert len(results) == 2
    assert results[0].bytes_transferred == 450019
    assert isinstance(results[0].bytes_transferred, int)


def test_flash_stream_uses_http_adapter_for_firmware_url():
    client = MagicMock()
    client.flash_stream = QualcommFlasherClient.flash_stream.__get__(client, QualcommFlasherClient)
    client._iter_flash_status = MagicMock(return_value=iter([]))

    with patch("jumpstarter_driver_ridesx.qdl.client._http_url_adapter") as http_adapter:
        http_adapter.return_value.__enter__.return_value = {"url": "https://example.com/fw.tar.xz"}
        http_adapter.return_value.__exit__.return_value = None

        list(
            client.flash_stream(
                "https://example.com/firmware/sx4-r00021.1a.tar.xz",
                manifest={"name": "test", "data": {"folder": "fw"}, "steps": []},
            )
        )

    http_adapter.assert_called_once_with(
        client=client,
        url="https://example.com/firmware/sx4-r00021.1a.tar.xz",
        mode="rb",
    )
    client._iter_flash_status.assert_called_once()


def test_flash_stream_uses_local_adapter_for_file_path():
    client = MagicMock()
    client.flash_stream = QualcommFlasherClient.flash_stream.__get__(client, QualcommFlasherClient)
    client._iter_flash_status = MagicMock(return_value=iter([]))

    with (
        patch("jumpstarter_driver_ridesx.qdl.client._local_file_adapter") as local_adapter,
        patch("jumpstarter_driver_ridesx.qdl.client._http_url_adapter") as http_adapter,
    ):
        local_adapter.return_value.__enter__.return_value = "local-handle"
        local_adapter.return_value.__exit__.return_value = None

        list(client.flash_stream("./firmware.tar.xz"))

    local_adapter.assert_called_once()
    http_adapter.assert_not_called()


def test_flash_stream_loads_manifest_from_https_url():
    client = MagicMock()
    client.flash_stream = QualcommFlasherClient.flash_stream.__get__(client, QualcommFlasherClient)
    client._iter_flash_status = MagicMock(return_value=iter([]))

    response = MagicMock()
    response.read.return_value = MANIFEST_YAML.encode("utf-8")
    response.__enter__.return_value = response

    with (
        patch("jumpstarter_driver_ridesx.qdl.client.urlopen", return_value=response),
        patch("jumpstarter_driver_ridesx.qdl.client._http_url_adapter") as http_adapter,
    ):
        http_adapter.return_value.__enter__.return_value = {"url": "https://example.com/fw.tar.xz"}
        http_adapter.return_value.__exit__.return_value = None

        list(
            client.flash_stream(
                "https://example.com/fw.tar.xz",
                manifest="https://example.com/es22.yaml",
            )
        )

    client._iter_flash_status.assert_called_once()
    assert client._iter_flash_status.call_args.kwargs["manifest"]["name"] == "SA8775P ES22 AWE Firmware"


def _make_version_info(**overrides):
    defaults = {
        "firmware_variant": "ES22",
        "sail_versions": {"sail_fw_version": "1.3.0", "platform_type": "RIDE_SX"},
        "main_versions": {
            "qc_image_version": "BOOT.MXF.1.2-00541-LEMANS-1",
            "uefi_version": "6.0.250710.BOOT.MXF.1.2-00541-LEMANS-1",
            "hypervisor": "prod",
            "abl_build": "Mar  3 2026 13:24:15",
            "cdt_platform_id": "37",
        },
    }
    defaults.update(overrides)
    return VersionInfo(**defaults)


def test_check_firmware_all_match():
    versions = _make_version_info()
    mismatches = _check_firmware(versions, "ES22", hypervisor="prod", sail_fw_version="1.3.0")
    assert mismatches == []


def test_check_firmware_variant_mismatch():
    versions = _make_version_info()
    mismatches = _check_firmware(versions, "CS4")
    assert len(mismatches) == 1
    assert "variant" in mismatches[0]


def test_check_firmware_variant_case_insensitive():
    versions = _make_version_info()
    assert _check_firmware(versions, "es22") == []


def test_check_firmware_field_mismatch():
    versions = _make_version_info()
    mismatches = _check_firmware(versions, "ES22", hypervisor="debug", cdt_platform_id="99")
    assert len(mismatches) == 2
    assert any("hypervisor" in m for m in mismatches)
    assert any("cdt_platform_id" in m for m in mismatches)


def test_check_firmware_none_fields_ignored():
    versions = _make_version_info()
    mismatches = _check_firmware(
        versions, "ES22",
        sail_fw_version=None, qc_image_version=None, hypervisor=None,
    )
    assert mismatches == []


