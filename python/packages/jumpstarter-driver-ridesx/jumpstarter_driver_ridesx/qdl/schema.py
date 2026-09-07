"""Schema definitions for Qualcomm firmware manifest YAML files."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

EMBEDDED_MANIFEST_NAMES = ("jumpstarter_manifest.yaml",)


class StepBase(BaseModel):
    name: str | None = Field(None, description="Optional human-readable name for this step")
    retry_mode: Literal["edl", "fastboot"] | None = Field(
        None, description="Mode to retry in if the step fails"
    )


class SetModeStep(StepBase):
    set_mode: Literal["edl", "fastboot"]
    check_dmesg: str | None = Field(None, description="Expected dmesg output to verify the mode change")


class SleepStep(StepBase):
    sleep: int = Field(..., description="Sleep duration in seconds")


class QdlConfig(BaseModel):
    storage: Literal["ufs", "spinor"]
    programmer: str = Field(..., description="Firehose programmer ELF relative to the storage workdir")
    files: list[str] = Field(..., description="XML or glob patterns relative to the storage workdir")
    workdir: str | None = Field(
        None,
        description="Directory relative to firmware root. Defaults to the storage name (ufs/spinor).",
    )


class QdlStep(StepBase):
    qdl: QdlConfig


class FastbootFlashOp(BaseModel):
    partition: str
    file: str
    revision: str | None = Field(
        None,
        description="Only flash when board revision matches (e.g. v3). "
        "Requires --board-revision or board_revision in the driver config.",
    )


class FastbootConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    flash: list[FastbootFlashOp] | None = None
    erase: list[str] | None = None
    continue_: bool | None = Field(None, alias="continue")


class FastbootStep(StepBase):
    fastboot: FastbootConfig


Step = Annotated[
    Union[SetModeStep, SleepStep, QdlStep, FastbootStep],
    Field(discriminator=None),
]


class FirmwareData(BaseModel):
    folder: str = Field(..., description="Folder containing the firmware files after extraction")


class FirmwareManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    data: FirmwareData
    steps: list[Step]


def _parse_step(data: dict) -> Step:
    if "set_mode" in data:
        return SetModeStep.model_validate(data)
    if "sleep" in data:
        return SleepStep.model_validate(data)
    if "qdl" in data:
        return QdlStep.model_validate(data)
    if "fastboot" in data:
        return FastbootStep.model_validate(data)
    raise ValidationError.from_exception_data(
        "Step",
        [{"type": "value_error", "loc": (), "msg": f"Unknown step type: {sorted(data)}"}],
    )


def load_firmware_manifest(yaml_path: Path) -> FirmwareManifest:
    if not yaml_path.exists():
        raise FileNotFoundError(f"Firmware manifest not found: {yaml_path}")

    with yaml_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)

    if not isinstance(raw, dict):
        raise ValidationError.from_exception_data(
            "FirmwareManifest",
            [{"type": "value_error", "loc": (), "msg": "Manifest root must be a mapping"}],
        )

    steps_raw = raw.get("steps", [])
    parsed_steps = [_parse_step(step) for step in steps_raw]
    payload = {**raw, "steps": parsed_steps}
    return FirmwareManifest.model_validate(payload)


def load_firmware_manifest_from_mapping(data: dict) -> FirmwareManifest:
    steps_raw = data.get("steps", [])
    parsed_steps = [_parse_step(step) for step in steps_raw]
    payload = {**data, "steps": parsed_steps}
    return FirmwareManifest.model_validate(payload)


def find_embedded_manifest(work_dir: Path) -> Path | None:
    """Search ``work_dir`` recursively for a manifest file.

    Returns the first match found (sorted alphabetically) or ``None``.
    When multiple manifests exist in the tree, the alphabetically first
    path wins.  Firmware archives are expected to contain at most one
    manifest, so this is typically unambiguous.
    """
    for filename in EMBEDDED_MANIFEST_NAMES:
        matches = sorted(work_dir.rglob(filename))
        if matches:
            return matches[0]
    return None


def normalize_revision(board_revision: str) -> str:
    """Normalize a board revision string for comparison (lowercase, 'v' prefix)."""
    revision = board_revision.strip().lower()
    return revision if revision.startswith("v") else f"v{revision}"
