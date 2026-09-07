import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jumpstarter_driver_ridesx.qdl.executor import (
    build_qdl_command,
    check_dmesg,
    fix_provision_default_xml,
    run_fastboot_step,
    set_device_mode,
)
from jumpstarter_driver_ridesx.qdl.firmware_id import identify_firmware_variant
from jumpstarter_driver_ridesx.qdl.schema import FastbootConfig, FastbootFlashOp, FastbootStep, QdlConfig, QdlStep
from jumpstarter_driver_ridesx.qdl.soc_profiles import SA8775P


def test_identify_firmware_variant_known_es22():
    assert identify_firmware_variant("BOOT.MXF.1.2-00541-LEMANS-1") == "ES22"


def test_identify_firmware_variant_cs4_cs5_disambiguation():
    assert identify_firmware_variant("BOOT.MXF.1.2-00568-LEMANS-2", rm_version="55198a39") == "CS4"
    assert identify_firmware_variant("BOOT.MXF.1.2-00568-LEMANS-2", rm_version="ba2475df") == "CS5"
    assert identify_firmware_variant("BOOT.MXF.1.2-00568-LEMANS-2") == "CS4/CS5"


def test_build_qdl_command_expands_globs(tmp_path):
    ufs_dir = tmp_path / "ufs"
    ufs_dir.mkdir()
    programmer = ufs_dir / "prog_firehose_ddr.elf"
    programmer.write_bytes(b"elf")
    (ufs_dir / "rawprogram0.xml").write_text("<xml/>", encoding="utf-8")
    (ufs_dir / "patch0.xml").write_text("<xml/>", encoding="utf-8")

    step = QdlStep(
        qdl=QdlConfig(
            storage="ufs",
            programmer="prog_firehose_ddr.elf",
            files=["rawprogram*.xml", "patch*.xml"],
        )
    )
    cmd, workdir = build_qdl_command(step, tmp_path)
    assert workdir == ufs_dir
    assert cmd[:4] == ["qdl", "-s", "ufs", str(programmer)]
    assert str(ufs_dir / "rawprogram0.xml") in cmd
    assert str(ufs_dir / "patch0.xml") in cmd


def test_fix_provision_default_xml_strips_invalid_header(tmp_path):
    ufs_dir = tmp_path / "ufs"
    ufs_dir.mkdir()
    provision = ufs_dir / "provision_default.xml"
    provision.write_text(
        "\n".join(
            [
                "<!-- bad -->",
                "<!-- still bad -->",
                "<!-- x -->",
                "<!-- y -->",
                "<!-- z -->",
                "<!-- a -->",
                "<!-- b -->",
                "<!-- c -->",
                "<!-- d -->",
                '<?xml version="1.0" ?><data></data>',
            ]
        ),
        encoding="utf-8",
    )
    fix_provision_default_xml(ufs_dir)
    content = provision.read_text(encoding="utf-8")
    assert content.startswith("<?xml")


def test_check_dmesg_does_not_clear_kernel_log(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="line1\nUSB QTI_HS seen\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    check_dmesg("USB QTI_HS", baseline="line1\n")
    assert calls == [["dmesg"]]
    assert "-c" not in calls[0]


def test_check_dmesg_baseline_rejects_stale_marker(monkeypatch):
    """When a baseline is provided, stale markers from before the baseline must not match."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="old\nProduct: Android\nnew unrelated line\n"
        ),
    )
    # "Product: Android" is in the baseline (old output) — should NOT match
    with pytest.raises(RuntimeError, match="not found in new kernel log"):
        check_dmesg("Product: Android", baseline="old\nProduct: Android\n")


def test_check_dmesg_finds_marker_in_tail(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="old\nProduct: Android\n"),
    )
    check_dmesg("Product: Android")


@pytest.mark.asyncio
async def test_set_device_mode_polls_dmesg_until_marker_found():
    """set_device_mode should poll dmesg until the expected marker appears."""
    tac = MagicMock()
    stream = AsyncMock()
    stream.__aenter__ = AsyncMock(return_value=stream)
    stream.__aexit__ = AsyncMock(return_value=None)
    stream.receive = AsyncMock(return_value=b"ok")
    tac.connect.return_value = stream

    call_count = 0

    def fake_check_dmesg(expected, *, baseline=None):
        nonlocal call_count  # ty: ignore[unresolved-reference]
        call_count += 1  # ty: ignore[unresolved-reference]
        if call_count < 3:  # ty: ignore[unresolved-reference]
            raise RuntimeError("not found")

    with (
        patch("jumpstarter_driver_ridesx.qdl.executor.read_dmesg", return_value="baseline"),
        patch("jumpstarter_driver_ridesx.qdl.executor.check_dmesg", side_effect=fake_check_dmesg),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        await set_device_mode(
            tac=tac, profile=SA8775P, mode="fastboot",
            check="Product: Android", tac_timeout=1.0,
        )

    assert call_count == 3


@pytest.mark.asyncio
async def test_set_device_mode_raises_after_timeout():
    """set_device_mode should raise after check_timeout if marker never appears."""
    tac = MagicMock()
    stream = AsyncMock()
    stream.__aenter__ = AsyncMock(return_value=stream)
    stream.__aexit__ = AsyncMock(return_value=None)
    stream.receive = AsyncMock(return_value=b"ok")
    tac.connect.return_value = stream

    with (
        patch("jumpstarter_driver_ridesx.qdl.executor.read_dmesg", return_value="baseline"),
        patch("jumpstarter_driver_ridesx.qdl.executor.check_dmesg", side_effect=RuntimeError("not found")),
        patch("asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(RuntimeError, match="not found"),
    ):
        await set_device_mode(
            tac=tac, profile=SA8775P, mode="fastboot",
            check="Product: Android", tac_timeout=1.0,
            check_timeout=5.0, check_interval=2.0,
        )


def test_fastboot_step_skips_revision_mismatch(tmp_path, monkeypatch):
    """Flash ops with non-matching revision should be skipped."""
    image = tmp_path / "cdt_v3.bin"
    image.write_bytes(b"\x00")
    step = FastbootStep(
        fastboot=FastbootConfig(
            flash=[
                FastbootFlashOp(partition="cdt", file="cdt_v3.bin", revision="v3"),
                FastbootFlashOp(partition="cdt", file="cdt_v4.bin", revision="v4"),
            ],
        ),
    )
    # board is v3, so v4 op should be skipped (no v4 file needed)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, stdout="", stderr=""))
    result = run_fastboot_step(step, tmp_path, timeout=5, board_revision="v3")
    assert result.stdout == ""  # only one flash happened


def test_fastboot_step_errors_without_board_revision(tmp_path):
    """Revision-filtered flash ops must error when board_revision is None."""
    step = FastbootStep(
        fastboot=FastbootConfig(
            flash=[FastbootFlashOp(partition="cdt", file="cdt.bin", revision="v3")],
        ),
    )
    with pytest.raises(ValueError, match="Board revision is required"):
        run_fastboot_step(step, tmp_path, timeout=5, board_revision=None)


def test_fastboot_step_flashes_without_revision(tmp_path, monkeypatch):
    """Flash ops without revision should always execute."""
    image = tmp_path / "abl.elf"
    image.write_bytes(b"\x00")
    step = FastbootStep(
        fastboot=FastbootConfig(
            flash=[
                FastbootFlashOp(partition="abl_a", file="abl.elf"),
                FastbootFlashOp(partition="abl_b", file="abl.elf"),
            ],
        ),
    )
    calls = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: (calls.append(a[0]), subprocess.CompletedProcess(a[0], 0, stdout="", stderr=""))[1],
    )
    run_fastboot_step(step, tmp_path, timeout=5, board_revision=None)
    assert len(calls) == 2
