"""MCP tools for command execution, environment export, and introspection."""

from __future__ import annotations

import asyncio
import asyncio.subprocess
import logging
import shutil

import anyio
import anyio.to_thread
import click

from jumpstarter_mcp.connections import ConnectionManager

from jumpstarter.client.introspect import get_driver_methods, list_drivers, walk_click_tree

logger = logging.getLogger(__name__)


async def run_command(
    manager: ConnectionManager,
    connection_id: str,
    command: list[str],
    timeout_seconds: int = 120,
) -> dict:
    """Run a `j` subcommand against the connection.

    Executes the command as a subprocess with JUMPSTARTER_HOST set to the
    connection's socket path. Captures stdout, stderr, and exit code.
    """
    conn = manager.get_connection(connection_id)
    j_path = shutil.which("j")
    if j_path is None:
        return {"error": "j CLI binary not found in PATH"}

    env = {
        "JUMPSTARTER_HOST": conn.socket_path,
        "JMP_DRIVERS_ALLOW": "UNSAFE" if conn.unsafe else ",".join(conn.allow),
        "_JMP_SUPPRESS_DRIVER_WARNINGS": "1",
    }

    import os

    full_env = {**os.environ, **env}

    logger.info("Running command: j %s (timeout=%ds)", " ".join(command), timeout_seconds)
    try:
        proc = await asyncio.create_subprocess_exec(
            j_path,
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=full_env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        logger.info("Command finished: j %s -> exit_code=%s", " ".join(command), proc.returncode)
        return {
            "exit_code": proc.returncode,
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
            "command": [j_path, *command],
        }
    except TimeoutError:
        proc.kill()
        stdout, stderr = await proc.communicate()
        logger.warning("Command timed out after %ds: j %s", timeout_seconds, " ".join(command))
        return {
            "exit_code": proc.returncode,
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
            "timed_out": True,
            "timeout_seconds": timeout_seconds,
            "command": [j_path, *command],
        }
    except Exception:
        logger.exception("Command failed unexpectedly: j %s", " ".join(command))
        raise


async def get_env(
    manager: ConnectionManager,
    connection_id: str,
) -> dict:
    """Return environment variables and paths for direct shell/Python interaction."""
    return manager.get_env(connection_id)


async def explore(
    manager: ConnectionManager,
    connection_id: str,
    command_path: list[str] | None = None,
) -> dict:
    """Walk the Click command tree for a connected device.

    If command_path is provided, drills into that subtree.
    """
    conn = manager.get_connection(connection_id)
    client = conn.client

    if not hasattr(client, "cli"):
        return {"error": "Client does not have a CLI interface"}

    cli_cmd = await anyio.to_thread.run_sync(client.cli)  # ty: ignore[invalid-argument-type]

    if command_path:
        for name in command_path:
            if isinstance(cli_cmd, click.Group) and name in cli_cmd.commands:
                cli_cmd = cli_cmd.commands[name]
            else:
                return {"error": f"Command '{name}' not found at this level"}

    return walk_click_tree(cli_cmd)


async def drivers(
    manager: ConnectionManager,
    connection_id: str,
) -> list[dict]:
    """List all drivers in the client tree."""
    conn = manager.get_connection(connection_id)
    # Off the loop thread, as explore() does. These walk a sync client facade
    # over a blocking portal; they read attributes statically today and so do
    # not dispatch, but a driver property that ever did would deadlock the loop.
    return await anyio.to_thread.run_sync(list_drivers, conn.client)


async def driver_methods(
    manager: ConnectionManager,
    connection_id: str,
    driver_path: list[str],
) -> dict:
    """Inspect methods on a specific driver."""
    conn = manager.get_connection(connection_id)
    return await anyio.to_thread.run_sync(get_driver_methods, conn.client, driver_path)
