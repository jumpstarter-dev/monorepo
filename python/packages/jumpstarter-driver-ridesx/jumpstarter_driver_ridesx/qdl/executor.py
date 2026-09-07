"""Manifest step execution helpers for Qualcomm QDL flashing."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import subprocess
from pathlib import Path

from ..tac import send_tac_sequence
from .schema import (
    FastbootFlashOp,
    FastbootStep,
    FirmwareManifest,
    QdlStep,
    SetModeStep,
    SleepStep,
    Step,
    normalize_revision,
)
from .soc_profiles import SoCProfile
from jumpstarter.client.flasher import FlashPhase, FlashStatus

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class StepResult:
    """Collected stdout/stderr from one or more subprocess calls in a step."""

    stdout: str = ""
    stderr: str = ""

RETRY_MODE_DMESG = {
    "edl": "qcserial",
    "fastboot": "Product: Android",
}


def read_dmesg() -> str:
    result = subprocess.run(["dmesg"], capture_output=True, text=True, check=False)
    return result.stdout


def check_dmesg(expected: str, *, baseline: str | None = None, tail_lines: int = 200) -> None:
    """Check kernel ring buffer for an expected marker.

    When *baseline* is provided, only lines **not** in the baseline are
    inspected (diff-based check) and the function raises if the marker
    is not found among the new lines.  This prevents false positives
    from stale markers left by previous operations.

    Without a baseline the last *tail_lines* lines are searched.  On
    systems with very high dmesg throughput (USB enumeration storms,
    etc.) the default 200 lines may scroll past the marker; increase
    *tail_lines* if this becomes an issue.
    """
    output = read_dmesg()
    if baseline is not None:
        baseline_lines = set(baseline.splitlines())
        for line in output.splitlines():
            if line not in baseline_lines and expected in line:
                return
        raise RuntimeError(f"Expected dmesg marker '{expected}' not found in new kernel log entries")
    tail = "\n".join(output.splitlines()[-tail_lines:])
    if expected in tail:
        return
    raise RuntimeError(f"Expected dmesg marker '{expected}' not found in recent kernel log")


def fix_provision_default_xml(workdir: Path) -> None:
    provision = workdir / "provision_default.xml"
    if not provision.exists():
        return
    content = provision.read_text(encoding="utf-8", errors="replace")
    if content.lstrip().startswith("<?xml"):
        return
    # Qualcomm PCAT/provisioning tools may prepend a text header before the
    # actual XML declaration.  Search for the first line starting with "<?xml"
    # instead of assuming a fixed number of header lines.
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith("<?xml"):
            fixed = "\n".join(lines[i:]) + "\n"
            provision.write_text(fixed, encoding="utf-8")
            return
    logger.warning("provision_default.xml has no <?xml declaration in %s; leaving unchanged", workdir)


def build_qdl_command(step: QdlStep, firmware_root: Path) -> tuple[list[str], Path]:
    config = step.qdl
    workdir_name = config.workdir or config.storage
    workdir = firmware_root / workdir_name
    programmer = workdir / config.programmer
    cmd = ["qdl", "-s", config.storage, str(programmer)]
    for pattern in config.files:
        if any(char in pattern for char in "*?[]"):
            matches = sorted(workdir.glob(pattern))
            if not matches:
                raise FileNotFoundError(f"No QDL files matched pattern '{pattern}' in {workdir}")
            cmd.extend(str(path) for path in matches)
        else:
            path = workdir / pattern
            if not path.exists():
                raise FileNotFoundError(f"QDL file not found: {path}")
            cmd.append(str(path))
    return cmd, workdir


def run_qdl_step(step: QdlStep, firmware_root: Path, *, timeout: int) -> subprocess.CompletedProcess[str]:
    if any("provision_default.xml" in pattern for pattern in step.qdl.files):
        fix_provision_default_xml(firmware_root / (step.qdl.workdir or step.qdl.storage))
    cmd, workdir = build_qdl_command(step, firmware_root)
    logger.info("Running QDL: %s (cwd=%s)", " ".join(cmd), workdir)
    result = subprocess.run(
        cmd,
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    logger.info("QDL finished: returncode=%d", result.returncode)
    return result


def _should_flash(op: FastbootFlashOp, board_revision: str | None) -> bool:
    """Check whether a flash operation should execute based on board revision."""
    if op.revision is None:
        return True
    if board_revision is None:
        raise ValueError(
            f"Board revision is required to flash {op.partition} "
            f"(revision={op.revision}). "
            f"Use --board-revision or set board_revision in the driver config."
        )
    return normalize_revision(op.revision) == normalize_revision(board_revision)


def _run_fastboot_flash(
    operations: list[FastbootFlashOp],
    firmware_root: Path,
    *,
    timeout: int,
    board_revision: str | None,
    collected: StepResult,
) -> None:
    for operation in operations:
        if not _should_flash(operation, board_revision):
            logger.info(
                "Skipping flash %s (revision %s, board is %s)",
                operation.partition, operation.revision, board_revision,
            )
            continue
        image_path = firmware_root / operation.file
        if not image_path.exists():
            raise FileNotFoundError(f"Fastboot image not found: {image_path}")
        logger.info("fastboot flash %s %s", operation.partition, image_path.name)
        result = subprocess.run(
            ["fastboot", "flash", operation.partition, str(image_path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        collected.stdout += result.stdout
        collected.stderr += result.stderr
        if result.returncode != 0:
            raise RuntimeError(
                f"fastboot flash {operation.partition} failed: {result.stderr or result.stdout}"
            )


def run_fastboot_step(
    step: FastbootStep,
    firmware_root: Path,
    *,
    timeout: int,
    board_revision: str | None = None,
) -> StepResult:
    config = step.fastboot
    collected = StepResult()

    if config.erase:
        for partition in config.erase:
            logger.info("fastboot erase %s", partition)
            result = subprocess.run(
                ["fastboot", "erase", partition],
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
            collected.stdout += result.stdout
            collected.stderr += result.stderr
            if result.returncode != 0:
                raise RuntimeError(f"fastboot erase {partition} failed: {result.stderr or result.stdout}")

    if config.flash:
        _run_fastboot_flash(
            config.flash, firmware_root,
            timeout=timeout, board_revision=board_revision, collected=collected,
        )

    if config.continue_:
        logger.info("fastboot continue")
        result = subprocess.run(
            ["fastboot", "continue"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        collected.stdout += result.stdout
        collected.stderr += result.stderr
        if result.returncode != 0:
            raise RuntimeError(f"fastboot continue failed: {result.stderr or result.stdout}")

    return collected


async def _poll_dmesg(
    marker: str,
    baseline: str,
    *,
    timeout: float,
    interval: float,
    mode: str,
) -> None:
    """Poll dmesg for *marker* among lines added after *baseline*."""
    logger.info("Waiting for device in %s mode (marker: %s)", mode, marker)
    elapsed = 0.0
    while True:
        await asyncio.sleep(interval)
        elapsed += interval
        try:
            check_dmesg(marker, baseline=baseline)
            logger.info("Device entered %s mode (%.0fs)", mode, elapsed)
            return
        except RuntimeError:
            if elapsed >= timeout:
                logger.warning("Device did not enter %s mode after %.0fs", mode, elapsed)
                raise


async def set_device_mode(
    *,
    tac,
    profile: SoCProfile,
    mode: str,
    check: str | None,
    tac_timeout: float,
    check_timeout: float = 30.0,
    check_interval: float = 2.0,
) -> None:
    logger.info("Entering %s mode", mode)
    baseline = read_dmesg() if check else None
    if mode == "edl":
        await send_tac_sequence(tac, profile.edl_commands, timeout=tac_timeout)
    elif mode == "fastboot":
        await send_tac_sequence(tac, profile.fastboot_commands, timeout=tac_timeout)
    else:
        raise ValueError(f"Unsupported mode: {mode}")
    if check and baseline is not None:
        await _poll_dmesg(
            check, baseline, timeout=check_timeout, interval=check_interval,
            mode=mode,
        )


def step_label(step: Step) -> str:
    if step.name:
        return step.name
    if isinstance(step, SetModeStep):
        return f"set_mode {step.set_mode}"
    if isinstance(step, SleepStep):
        return f"sleep {step.sleep}s"
    if isinstance(step, QdlStep):
        return f"qdl {step.qdl.storage}"
    if isinstance(step, FastbootStep):
        return "fastboot"
    return "step"


async def execute_step(
    step: Step,
    *,
    tac,
    profile: SoCProfile,
    firmware_root: Path,
    qdl_timeout: int,
    fastboot_timeout: int,
    tac_timeout: float,
    board_revision: str | None = None,
) -> StepResult | None:
    if isinstance(step, SetModeStep):
        await set_device_mode(
            tac=tac,
            profile=profile,
            mode=step.set_mode,
            check=step.check_dmesg,
            tac_timeout=tac_timeout,
        )
        return None
    if isinstance(step, SleepStep):
        await asyncio.sleep(step.sleep)
        return None
    if isinstance(step, QdlStep):
        result = await asyncio.to_thread(run_qdl_step, step, firmware_root, timeout=qdl_timeout)
        if result.returncode != 0:
            raise RuntimeError(f"QDL failed ({result.returncode}): {result.stderr or result.stdout}")
        return StepResult(stdout=result.stdout, stderr=result.stderr)
    if isinstance(step, FastbootStep):
        return await asyncio.to_thread(
            run_fastboot_step, step, firmware_root, timeout=fastboot_timeout,
            board_revision=board_revision,
        )
    raise TypeError(f"Unsupported step type: {type(step)!r}")


async def execute_manifest(
    manifest: FirmwareManifest,
    *,
    tac,
    profile: SoCProfile,
    firmware_root: Path,
    qdl_timeout: int,
    fastboot_timeout: int,
    tac_timeout: float,
    board_revision: str | None = None,
    max_attempts: int = 3,
):
    total_steps = len(manifest.steps)
    for index, step in enumerate(manifest.steps, start=1):
        label = step_label(step)
        yield FlashStatus(
            phase=FlashPhase.STEP,
            message=f"Running {label}",
            step_index=index,
            total_steps=total_steps,
            step_name=label,
        )
        step_result: StepResult | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                step_result = await execute_step(
                    step,
                    tac=tac,
                    profile=profile,
                    firmware_root=firmware_root,
                    qdl_timeout=qdl_timeout,
                    fastboot_timeout=fastboot_timeout,
                    tac_timeout=tac_timeout,
                    board_revision=board_revision,
                )
                break
            except Exception as exc:
                if attempt >= max_attempts:
                    raise RuntimeError(f"Step '{label}' failed after {max_attempts} attempts: {exc}") from exc
                if not step.retry_mode:
                    raise
                yield FlashStatus(
                    phase=FlashPhase.STEP,
                    message=f"Retrying {label} in {step.retry_mode} mode (attempt {attempt + 1}/{max_attempts})",
                    step_index=index,
                    total_steps=total_steps,
                    step_name=label,
                )
                retry_check = RETRY_MODE_DMESG.get(step.retry_mode)
                if step.retry_mode and retry_check is None:
                    logger.warning(
                        "No dmesg verification configured for retry mode '%s'; "
                        "skipping post-retry device check",
                        step.retry_mode,
                    )
                await set_device_mode(
                    tac=tac,
                    profile=profile,
                    mode=step.retry_mode,
                    check=retry_check,
                    tac_timeout=tac_timeout,
                )
        if step_result:
            yield FlashStatus(
                phase=FlashPhase.STEP,
                message=f"Completed {label}",
                step_index=index,
                total_steps=total_steps,
                step_name=label,
                stdout=step_result.stdout or None,
                stderr=step_result.stderr or None,
            )


def resolve_firmware_root(work_dir: Path, manifest: FirmwareManifest) -> Path:
    return work_dir / manifest.data.folder
