import tarfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from jumpstarter_driver_ridesx.qdl.driver import QualcommFlasher
from jumpstarter_driver_ridesx.qdl.executor import execute_manifest
from jumpstarter_driver_ridesx.qdl.schema import (
    SleepStep,
    load_firmware_manifest,
)
from jumpstarter_driver_ridesx.qdl.soc_profiles import SA8775P

from jumpstarter.client.flasher import FlashPhase
from jumpstarter.common.exceptions import ConfigurationError


@pytest.mark.asyncio
async def test_execute_manifest_sleep_step():
    manifest = load_firmware_manifest(Path(__file__).parent / "examples" / "manifests" / "es22.yaml")
    manifest = manifest.model_copy(update={"steps": [SleepStep(sleep=0)]})
    tac = MagicMock()
    statuses = [
        status
        async for status in execute_manifest(
            manifest,
            tac=tac,
            profile=SA8775P,
            firmware_root=Path("/tmp/firmware"),
            qdl_timeout=1,
            fastboot_timeout=1,
            tac_timeout=1,
        )
    ]
    assert statuses[0].phase == FlashPhase.STEP
    assert statuses[0].step_name == "sleep 0s"


def test_qualcomm_flasher_requires_tac_child():
    with pytest.raises(ConfigurationError, match="tac"):
        QualcommFlasher(children={})


@pytest.mark.asyncio
async def test_qualcomm_flasher_power_cycle(tmp_path):
    mock_tac = MagicMock()
    stream = AsyncMock()
    stream.__aenter__ = AsyncMock(return_value=stream)
    stream.__aexit__ = AsyncMock(return_value=None)
    stream.receive = AsyncMock(return_value=b"ok")
    mock_tac.connect.return_value = stream

    driver = QualcommFlasher(children={"tac": mock_tac}, work_dir=str(tmp_path))
    await driver.power_cycle()
    assert stream.send.await_count > 0


@pytest.mark.asyncio
async def test_qualcomm_flasher_boot_to_edl(tmp_path):
    mock_tac = MagicMock()
    stream = AsyncMock()
    stream.__aenter__ = AsyncMock(return_value=stream)
    stream.__aexit__ = AsyncMock(return_value=None)
    stream.receive = AsyncMock(return_value=b"ok")
    mock_tac.connect.return_value = stream

    driver = QualcommFlasher(children={"tac": mock_tac}, work_dir=str(tmp_path))
    await driver.boot_to_edl()
    assert stream.send.await_count > 0


@pytest.mark.parametrize(
    "header, expected",
    [
        (b"\xfd7zXZ\x00", "-J"),           # xz
        (b"\x1f\x8b\x08\x00\x00\x00", "-z"),  # gzip
        (b"BZh91AY&SY", "-j"),             # bzip2
        (b"\x28\xb5\x2f\xfd\x04\x00", "--zstd"),  # zstd
        (b"\x00\x00\x00\x00\x00\x00", None),  # unknown / plain tar
    ],
)
def test_detect_compression(header, expected):
    assert QualcommFlasher._detect_compression(header) == expected


def test_safe_extractall_rejects_path_traversal(tmp_path):
    archive_path = tmp_path / "bad.tar"
    extract_root = tmp_path / "extract"
    extract_root.mkdir()
    with tarfile.open(archive_path, "w") as archive:
        info = tarfile.TarInfo(name="../escape.txt")
        info.size = 4
        import io

        archive.addfile(info, io.BytesIO(b"evil"))

    with tarfile.open(archive_path, "r") as archive:
        # Python 3.12+ filter="data" raises OutsideDestinationError (a FilterError),
        # while our 3.11 fallback raises ExtractError.  Both are subclasses of TarError.
        with pytest.raises(tarfile.TarError):
            QualcommFlasher._safe_extractall(archive, extract_root)
