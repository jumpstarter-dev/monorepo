"""Shared TAC/EPM serial helpers for RideSX drivers."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

CommandDelay = tuple[str, float]
PROMPT = b"CMD >> "
DEFAULT_TAC_COMMAND_TIMEOUT = 10.0


async def _send_power_command_on_stream(
    stream, logger, command: str, *, timeout: float
) -> None:
    """Send a single power command on an already-open stream and wait for ok."""
    logger.info(f"Executing power command: {command}")
    await stream.send(f"{command}\r".encode())
    data = b""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while b"ok" not in data:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(f"Power command '{command}' timed out after {timeout}s")
        chunk = await asyncio.wait_for(stream.receive(), timeout=remaining)
        data += chunk
    logger.debug(f"Command {command} acknowledged with 'ok'")


async def send_power_command(
    serial, logger, command: str, *, timeout: float = DEFAULT_TAC_COMMAND_TIMEOUT
) -> None:
    """Send a power command and wait for ok."""
    async with serial.connect() as stream:
        await _send_power_command_on_stream(stream, logger, command, timeout=timeout)


async def send_power_commands_sequence(
    serial, logger, commands: Sequence[CommandDelay], *, timeout: float = DEFAULT_TAC_COMMAND_TIMEOUT
) -> None:
    """Send a sequence of power commands with delays, using a single connection."""
    async with serial.connect() as stream:
        for command, delay in commands:
            await _send_power_command_on_stream(stream, logger, command, timeout=timeout)
            if delay > 0:
                await asyncio.sleep(delay)


async def _send_tac_command_on_stream(stream, command: str, *, timeout: float) -> None:
    """Send a single TAC command on an already-open stream and wait for ok."""
    await stream.send(f"{command}\r".encode())
    data = b""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while b"ok" not in data:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(f"TAC command '{command}' timed out after {timeout}s")
        chunk = await asyncio.wait_for(stream.receive(), timeout=remaining)
        data += chunk


async def send_tac_command(tac, command: str, *, timeout: float = DEFAULT_TAC_COMMAND_TIMEOUT) -> None:
    """Send a TAC command and wait for ok."""
    async with tac.connect() as stream:
        await _send_tac_command_on_stream(stream, command, timeout=timeout)


async def send_tac_sequence(
    tac,
    commands: Sequence[CommandDelay],
    *,
    timeout: float = DEFAULT_TAC_COMMAND_TIMEOUT,
) -> None:
    """Send a sequence of TAC commands with delays, using a single connection."""
    async with tac.connect() as stream:
        for command, delay in commands:
            await _send_tac_command_on_stream(stream, command, timeout=timeout)
            if delay > 0:
                await asyncio.sleep(delay)
