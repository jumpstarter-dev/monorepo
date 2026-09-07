"""TAC GPIO command sequences for Qualcomm SoC profiles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SoCType = Literal["sa8775p", "sa8540p1", "sa8540p2"]

CommandDelay = tuple[str, float]


@dataclass(frozen=True)
class SoCProfile:
    name: SoCType
    edl_commands: list[CommandDelay]
    fastboot_commands: list[CommandDelay]
    power_on_commands: list[CommandDelay]
    power_off_commands: list[CommandDelay]


SA8775P = SoCProfile(
    name="sa8775p",
    power_off_commands=[
        ("gpio vbusdis1 0", 0),
        ("usbDevicePower 1", 0),
        ("devicePower 0", 0.5),
    ],
    power_on_commands=[
        ("devicePower 1", 0.9),
        ("usbDevicePower 1", 0),
        ("gpio vbusdis1 0", 0.03),
    ],
    edl_commands=[
        ("devicePower 0", 0),
        ("usbDevicePower 1", 0),
        ("gpio vbusdis1 0", 0),
        ("pin 1 31", 0.5),
        ("devicePower 1", 0.9),
        ("usbDevicePower 1", 0),
        ("gpio vbusdis1 0", 0.03),
        ("ttl outputBit 1 1", 0.8),
        ("ttl outputBit 1 0", 0.5),
        ("pin 0 31", 0.5),
    ],
    fastboot_commands=[
        ("devicePower 0", 0),
        ("usbDevicePower 1", 0),
        ("gpio vbusdis1 0", 0),
        ("ttl outputBit 1 0", 0),
        ("gpio volup 0", 0),
        ("ttl outputBit 2 1", 0),
        ("ttl outputBit 4 0", 0.5),
        ("devicePower 1", 0.9),
        ("usbDevicePower 1", 0),
        ("gpio vbusdis1 0", 0.03),
        ("ttl outputBit 1 1", 0.8),
        ("ttl outputBit 1 0", 8),
        ("ttl outputBit 2 0", 0.5),
    ],
)

SA8540P1 = SoCProfile(
    name="sa8540p1",
    power_on_commands=[
        ("gpio start 0", 1.0),
    ],
    power_off_commands=[
        ("gpio start 1", 0.5),
    ],
    edl_commands=[
        ("gpio start 0", 0),
        ("usbDevicePower 0", 0),
        ("gpio volup 0", 0),
        ("ttl outputBit 2 0", 0),
        ("gpio start 0", 0),
        ("ttl outputBit 4 1", 0.5),
        ("gpio start 1", 0),
        ("gpio start 0", 2.0),
        ("ttl outputBit 4 0", 3.0),
    ],
    fastboot_commands=[
        ("gpio start 0", 0),
        ("usbDevicePower 0", 0),
        ("ttl outputBit 1 0", 0),
        ("gpio volup 0", 0),
        ("ttl outputBit 2 0", 0),
        ("gpio resetsec 0", 0),
        ("ttl outputBit 4 0", 0),
        ("gpio pshold 0", 0),
        ("ttl outputBit 2 1", 0),
        ("gpio start 1", 0.8),
        ("gpio start 0", 0.3),
        ("ttl outputBit 2 0", 7.0),
    ],
)

SA8540P2 = SoCProfile(
    name="sa8540p2",
    power_on_commands=[
        ("gpio pshold 0", 1.0),
    ],
    power_off_commands=[
        ("gpio pshold 1", 0.5),
    ],
    edl_commands=[
        ("gpio start 0", 0),
        ("usbDevicePower 0", 0),
        ("gpio volup 0", 0),
        ("ttl outputBit 2 0", 0),
        ("gpio pshold 0", 0),
        ("gpio swdwnldsec 1", 0.5),
        ("gpio pshold 1", 0),
        ("gpio pshold 0", 2.0),
        ("gpio swdwnldsec 0", 3.0),
    ],
    fastboot_commands=[
        ("gpio start 0", 0),
        ("usbDevicePower 0", 0),
        ("ttl outputBit 1 0", 0),
        ("gpio volup 0", 0),
        ("ttl outputBit 2 0", 0),
        ("gpio resetsec 0", 0),
        ("ttl outputBit 4 0", 0),
        ("gpio resetsec 1", 0),
        ("gpio pshold 1", 0.8),
        ("gpio pshold 0", 0.3),
        ("gpio resetsec 0", 7.0),
    ],
)

SOC_PROFILES: dict[SoCType, SoCProfile] = {
    "sa8775p": SA8775P,
    "sa8540p1": SA8540P1,
    "sa8540p2": SA8540P2,
}


def get_soc_profile(soc_type: str) -> SoCProfile:
    normalized = soc_type.lower()
    if normalized not in SOC_PROFILES:
        supported = ", ".join(sorted(SOC_PROFILES))
        raise ValueError(f"Unsupported soc_type '{soc_type}'. Supported values: {supported}")
    return SOC_PROFILES[normalized]
