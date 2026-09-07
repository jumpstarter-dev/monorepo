"""Firmware identification helpers based on Qualcomm boot logs."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pexpect

logger = logging.getLogger(__name__)


@dataclass
class VersionInfo:
    sail_versions: dict[str, str] = field(default_factory=dict)
    main_versions: dict[str, str] = field(default_factory=dict)
    firmware_variant: str | None = None
    raw_output: str = ""
    sail_raw: str = ""
    main_raw: str = ""


class _SerialCapture:
    def __init__(self):
        self._chunks: list[str] = []

    def write(self, data):
        if isinstance(data, bytes):
            self._chunks.append(data.decode("utf-8", errors="replace"))
        else:
            self._chunks.append(str(data))

    def flush(self):
        pass

    def getvalue(self) -> str:
        return "".join(self._chunks)


PatternSpec = tuple[str, str, int]
"""(regex, key, group) tuple for serial pattern matching."""

ValidateCallback = Callable[[str, str], bool] | None
"""Optional ``(key, value) -> bool`` callback; return False to reject a match."""


def _scan_serial_patterns(
    serial,
    patterns: list[PatternSpec],
    *,
    timeout: int = 30,
    min_found: int = 0,
    log_buffer=None,
    label: str = "version",
    validate: ValidateCallback = None,
) -> dict[str, str]:
    """Drive a pexpect session looking for *patterns*, returning matched values.

    Args:
        serial: A pexpect spawn-like object.
        patterns: List of ``(regex, key, capture_group)`` tuples.
        timeout: Overall wall-clock timeout in seconds.
        min_found: Stop early on timeout/EOF once at least this many
            distinct keys have been captured.
        log_buffer: Optional writable object attached to
            ``serial.logfile_read``.
        label: Human-readable label used in warning messages.
        validate: Optional callback ``(key, value) -> bool``.  When it
            returns ``False`` the captured value is silently discarded.
    """
    serial.logfile_read = log_buffer
    version_info: dict[str, str] = {}
    found_patterns: set[str] = set()
    start_time = time.time()

    while time.time() - start_time < timeout and len(found_patterns) < len(patterns):
        try:
            expect_patterns = [p for p, k, _ in patterns if k not in found_patterns]
            if not expect_patterns:
                break
            index = serial.expect(expect_patterns, timeout=5)
            _process_match(serial, patterns, found_patterns, version_info, index, validate)
        except (pexpect.TIMEOUT, pexpect.EOF):
            if len(found_patterns) >= min_found:
                break
        except Exception:
            logger.warning("Unexpected error during %s extraction", label, exc_info=True)
            if len(found_patterns) >= min_found:
                break
    return version_info


def _process_match(
    serial,
    patterns: list[PatternSpec],
    found: set[str],
    results: dict[str, str],
    matched_index: int,
    validate: ValidateCallback,
) -> None:
    """Record the captured value for the pattern that matched at *matched_index*."""
    pattern_idx = 0
    for _regex, key, group in patterns:
        if key not in found:
            if pattern_idx == matched_index and serial.match:
                value = serial.match.group(group)
                if isinstance(value, bytes):
                    value = value.decode("utf-8", errors="ignore")
                value = value.strip()
                if validate and not validate(key, value):
                    return
                results[key] = value
                found.add(key)
                return
            pattern_idx += 1


def _validate_main_version(key: str, value: str) -> bool:
    """Reject spurious UEFI version matches that are too short or lack BOOT."""
    if key == "uefi_version" and (len(value) < 10 or ("BOOT" not in value and len(value) < 20)):
        return False
    return True


def extract_sail_version(sail, timeout=60, log_buffer=None) -> dict[str, str]:
    return _scan_serial_patterns(
        sail,
        patterns=[
            (r"FW Version:\s*([\d.]+)", "sail_fw_version", 1),
            (r"(?:Info:\s+)?Platform type:\s*(\S+)", "platform_type", 1),
            (r"(?:Info:\s+)?SOC1 Board ID corresponds to SOC Type:\s*([^\r\n]+)[\r\n]", "soc_type", 1),
            (r"(?:Info:\s+)?SIP1 Board ID corresponds to SIP Type:\s*([^\r\n]+)[\r\n]", "sip_type", 1),
            (r"Hypervisor\s+cold\s+boot[^:]*:\s*gunyah-[^\s]+\s+(perf|debug|prod)", "hypervisor", 1),
            (r"Hypervisor.*?(perf|debug)", "hypervisor", 1),
        ],
        timeout=timeout,
        min_found=2,
        log_buffer=log_buffer,
        label="SAIL version",
    )


def extract_main_version(serial, timeout=60, log_buffer=None) -> dict[str, str]:
    return _scan_serial_patterns(
        serial,
        patterns=[
            (r"QC_IMAGE_VERSION_STRING=([^\r\n]+)[\r\n]", "qc_image_version", 1),
            (r"IMAGE_VARIANT_STRING=([^\r\n]+)[\r\n]", "image_variant", 1),
            (r"OEM_IMAGE_VERSION_STRING=([^\r\n]+)[\r\n]", "oem_image_version", 1),
            (r"UEFI Ver\s+:\s*([\d.]+\.BOOT\.[^\r\n]+)[\r\n]", "uefi_version", 1),
            (r"UEFI Ver\s*:\s*([\d.]+\.BOOT\.[^\r\n]+)[\r\n]", "uefi_version", 1),
            (r"UEFI Ver\s+:\s*([^\r\n]+)[\r\n]", "uefi_version", 1),
            (r"UEFI Ver\s*:\s*([^\r\n]+)[\r\n]", "uefi_version", 1),
            (r"SBL1 BUILD @ ([^\r\n]+)[\r\n]", "sbl1_build", 1),
            (r"Chip Name\s*:\s*([^\r\n]+)[\r\n]", "chip_name", 1),
            (r"Chip Ver\s*:\s*([^\r\n]+)[\r\n]", "chip_version", 1),
            (r"Loader Build Info:\s*([^\r\n]+)[\r\n]", "abl_build", 1),
            (r"CDT Version:\d+,Platform ID:(\d+),Major ID:(\d+),Minor ID:(\d+),Subtype:(\d+)", "cdt_platform_id", 1),
            (r"\[RM\]Resource Manager version:\s*(\S+)", "rm_version", 1),
            (r"Hypervisor\s+cold\s+boot[^:]*:\s*gunyah-[^\s]+\s+(perf|debug|prod)", "hypervisor", 1),
            (r"Hypervisor.*?(perf|debug|prod)", "hypervisor", 1),
        ],
        timeout=timeout,
        min_found=8,
        log_buffer=log_buffer,
        label="main version",
        validate=_validate_main_version,
    )


def identify_firmware_variant(qc_image_version: str | None, rm_version: str | None = None) -> str | None:
    if not qc_image_version:
        return None

    known_variants = {
        "BOOT.MXF.1.2-00527-LEMANS-1": "ES21",
        "BOOT.MXF.1.2-00541-LEMANS-1": "ES22",
        "BOOT.MXF.1.2-00369-LEMANS-3": "ES14",
        "BOOT.MXF.1.2-00347-LEMANS-2": "ES13",
        "BOOT.MXF.1.2-00333-LEMANS-1": "ES12",
    }
    if qc_image_version in known_variants:
        return known_variants[qc_image_version]

    if qc_image_version == "BOOT.MXF.1.2-00568-LEMANS-2":
        rm_version_map = {
            "55198a39": "CS4",
            "ba2475df": "CS5",
        }
        if rm_version and rm_version in rm_version_map:
            return rm_version_map[rm_version]
        return "CS4/CS5"

    return "unknown"


def collect_version_info(serial, sail, power_cycle_callable, *, verbose=False, capture_dir=None) -> VersionInfo:
    result = VersionInfo()
    sail_capture = _SerialCapture()
    main_capture = _SerialCapture()

    with sail:
        with serial:
            if verbose:
                logger.info("Power cycling device...")
            power_cycle_callable()
            if verbose:
                logger.info("Collecting version information...")
            # Run both scans concurrently: SAIL output appears early in
            # boot while main serial output spans SBL1 through UEFI/ABL.
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                sail_future = pool.submit(extract_sail_version, sail, timeout=60, log_buffer=sail_capture)
                main_future = pool.submit(extract_main_version, serial, timeout=60, log_buffer=main_capture)
                result.sail_versions = sail_future.result()
                result.main_versions = main_future.result()
            if "hypervisor" in result.sail_versions and "hypervisor" not in result.main_versions:
                result.main_versions["hypervisor"] = result.sail_versions["hypervisor"]
            qc_image_version = result.main_versions.get("qc_image_version")
            rm_version = result.main_versions.get("rm_version")
            if qc_image_version:
                result.firmware_variant = identify_firmware_variant(qc_image_version, rm_version=rm_version)

    result.sail_raw = sail_capture.getvalue()
    result.main_raw = main_capture.getvalue()
    result.raw_output = result.sail_raw + result.main_raw

    if capture_dir:
        capture_path = Path(capture_dir)
        capture_path.mkdir(parents=True, exist_ok=True)
        (capture_path / "sail.log").write_text(result.sail_raw, encoding="utf-8")
        (capture_path / "main.log").write_text(result.main_raw, encoding="utf-8")

    return result


def format_version_report(versions: VersionInfo) -> str:
    lines = ["=" * 60, "VERSION INFORMATION SUMMARY", "=" * 60]
    if versions.sail_versions:
        lines.append("\nSAIL Port:")
        for key, value in versions.sail_versions.items():
            lines.append(f"  {key}: {value}")
    if versions.main_versions:
        lines.append("\nMAIN/SERIAL Port:")
        for key, value in versions.main_versions.items():
            lines.append(f"  {key}: {value}")
    lines.extend(["", "=" * 60, "FIRMWARE VARIANT IDENTIFICATION", "=" * 60])
    qc_image_version = versions.main_versions.get("qc_image_version")
    if versions.firmware_variant:
        lines.append(f"\n  Firmware Variant: {versions.firmware_variant}")
        lines.append(f"  QC Image Version: {qc_image_version}")
    else:
        lines.append(f"\n  QC Image Version: {qc_image_version or 'Not found'}")
        lines.append("  Firmware Variant: Could not be identified")
    uefi_version = versions.main_versions.get("uefi_version")
    hypervisor = versions.main_versions.get("hypervisor")
    lines.append(f"  UEFI Version: {uefi_version or 'Not found'}")
    lines.append(f"  Hypervisor: {hypervisor or 'Not found'}")
    lines.append("\n" + "=" * 60)
    return "\n".join(lines)
