from pathlib import Path

import pytest

from jumpstarter_driver_ridesx.qdl.schema import (
    FastbootFlashOp,
    FastbootStep,
    QdlStep,
    SetModeStep,
    SleepStep,
    load_firmware_manifest,
    normalize_revision,
)
from jumpstarter_driver_ridesx.qdl.soc_profiles import SA8775P, get_soc_profile

MANIFEST_DIR = Path(__file__).parent / "examples" / "manifests"


@pytest.mark.parametrize(
    "manifest_name",
    ["cs4.yaml", "cs5.yaml", "es13.yaml", "es21.yaml", "es22.yaml"],
)
def test_example_manifests_validate(manifest_name):
    manifest = load_firmware_manifest(MANIFEST_DIR / manifest_name)
    assert manifest.name
    assert manifest.steps
    assert any(isinstance(step, (SetModeStep, QdlStep, FastbootStep, SleepStep)) for step in manifest.steps)


def test_es22_manifest_contains_qdl_steps():
    manifest = load_firmware_manifest(MANIFEST_DIR / "es22.yaml")
    qdl_steps = [step for step in manifest.steps if isinstance(step, QdlStep)]
    assert len(qdl_steps) == 3
    assert qdl_steps[0].qdl.storage == "ufs"
    assert qdl_steps[2].qdl.storage == "spinor"


def test_es13_manifest_contains_fastboot_steps():
    manifest = load_firmware_manifest(MANIFEST_DIR / "es13.yaml")
    fastboot_steps = [step for step in manifest.steps if isinstance(step, FastbootStep)]
    # hypervisor step + CDT step
    assert len(fastboot_steps) == 2
    # Last fastboot step has continue: true
    assert fastboot_steps[-1].fastboot.continue_ is True


def test_cs4_manifest_has_abl_and_cdt_fastboot_steps():
    manifest = load_firmware_manifest(MANIFEST_DIR / "cs4.yaml")
    fastboot_steps = [step for step in manifest.steps if isinstance(step, FastbootStep)]
    assert len(fastboot_steps) == 2
    # ABL step flashes to abl_a and abl_b
    abl_step = fastboot_steps[0]
    assert abl_step.fastboot.flash is not None
    partitions = [op.partition for op in abl_step.fastboot.flash]
    assert "abl_a" in partitions
    assert "abl_b" in partitions
    # CDT step has revision-filtered ops
    cdt_step = fastboot_steps[1]
    assert cdt_step.fastboot.flash is not None
    revisions = [op.revision for op in cdt_step.fastboot.flash if op.revision]
    assert "v1" in revisions
    assert "v4" in revisions
    assert cdt_step.fastboot.continue_ is True


def test_soc_profiles_match_reference_sequences():
    profile = get_soc_profile("sa8775p")
    assert profile.power_on_commands[0][0] == "devicePower 1"
    assert profile.edl_commands[3][0] == "pin 1 31"
    assert profile is SA8775P


def test_fastboot_flash_op_with_revision():
    op = FastbootFlashOp(partition="cdt", file="cdt.bin", revision="v3")
    assert op.revision == "v3"


def test_fastboot_flash_op_without_revision():
    op = FastbootFlashOp(partition="abl_a", file="abl.elf")
    assert op.revision is None


def test_normalize_revision_adds_v_prefix():
    assert normalize_revision("3") == "v3"
    assert normalize_revision("V3") == "v3"
    assert normalize_revision("v3") == "v3"
    assert normalize_revision(" v3 ") == "v3"
