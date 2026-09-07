import io
import tarfile
from pathlib import Path

import yaml

from jumpstarter_driver_ridesx.qdl.driver import QualcommFlasher
from jumpstarter_driver_ridesx.qdl.schema import (
    FirmwareData,
    FirmwareManifest,
    find_embedded_manifest,
    load_firmware_manifest,
)


def test_cache_is_valid(tmp_path):
    driver = QualcommFlasher.__new__(QualcommFlasher)
    driver.work_dir = str(tmp_path)
    manifest = load_firmware_manifest(Path(__file__).parent / "examples" / "manifests" / "es22.yaml")
    firmware_root = driver._firmware_root(manifest)
    assert not driver._cache_is_valid(firmware_root)

    firmware_root.mkdir(parents=True)
    (firmware_root / "ufs").mkdir()
    assert driver._cache_is_valid(firmware_root)


def test_cache_is_valid_empty_directory(tmp_path):
    driver = QualcommFlasher.__new__(QualcommFlasher)
    driver.work_dir = str(tmp_path)
    manifest = FirmwareManifest(name="test", data=FirmwareData(folder="empty"), steps=[])
    firmware_root = driver._firmware_root(manifest)
    firmware_root.mkdir(parents=True)
    assert not driver._cache_is_valid(firmware_root)


def test_find_embedded_manifest(tmp_path):
    nested = tmp_path / "release"
    nested.mkdir()
    (nested / "jumpstarter_manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "embedded",
                "data": {"folder": "release"},
                "steps": [],
            }
        ),
        encoding="utf-8",
    )

    manifest_path = find_embedded_manifest(tmp_path)
    assert manifest_path is not None
    assert manifest_path.name == "jumpstarter_manifest.yaml"
    assert load_firmware_manifest(manifest_path).name == "embedded"


def test_find_embedded_manifest_ignores_manifest_yaml(tmp_path):
    nested = tmp_path / "release"
    nested.mkdir()
    (nested / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "legacy",
                "data": {"folder": "release"},
                "steps": [],
            }
        ),
        encoding="utf-8",
    )

    assert find_embedded_manifest(tmp_path) is None


def test_resolve_manifest_from_embedded_jumpstarter_manifest(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    manifest = {
        "name": "embedded",
        "data": {"folder": "r00002.2a_AWE"},
        "steps": [{"sleep": 1}],
    }
    (work_dir / "jumpstarter_manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")

    driver = QualcommFlasher.__new__(QualcommFlasher)
    resolved = driver._resolve_manifest(None, work_dir)
    assert resolved.name == "embedded"


def test_load_manifest_from_archive_reads_jumpstarter_manifest(tmp_path):
    manifest = {
        "name": "archive embedded",
        "data": {"folder": "r00002.2a_AWE"},
        "steps": [],
    }
    archive_path = tmp_path / "firmware.tar"
    with tarfile.open(archive_path, "w") as archive:
        payload = yaml.safe_dump(manifest).encode("utf-8")
        info = tarfile.TarInfo(name="jumpstarter_manifest.yaml")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))

    driver = QualcommFlasher.__new__(QualcommFlasher)
    loaded = driver._load_manifest_from_archive(archive_path)
    assert loaded is not None
    assert loaded.name == "archive embedded"
    assert loaded.data.folder == "r00002.2a_AWE"
