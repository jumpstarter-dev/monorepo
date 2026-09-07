"""MCP tools for connection management."""

from __future__ import annotations

import logging
from datetime import timedelta

import anyio.to_thread  # noqa: F401

from jumpstarter_mcp.connections import ConnectionManager

from jumpstarter.client.introspect import list_drivers, walk_click_tree
from jumpstarter.config.client import ClientConfigV1Alpha1

logger = logging.getLogger(__name__)


async def connect(
    config: ClientConfigV1Alpha1,
    manager: ConnectionManager,
    lease_id: str | None = None,
    selector: str | None = None,
    exporter_name: str | None = None,
    duration_seconds: int = 1800,
) -> dict:
    """Connect to a device, returning connection info and a CLI tree summary."""
    try:
        conn = await manager.connect(
            config=config,
            lease_name=lease_id,
            selector=selector,
            exporter_name=exporter_name,
            duration=timedelta(seconds=duration_seconds),
        )
    except ConnectionError as exc:
        logger.warning("Connection failed: %s", exc)
        return {"error": str(exc)}
    except Exception as exc:
        logger.exception("Unexpected connection failure")
        return {"error": f"Failed to connect: {exc}"}

    # Auto-explore: get CLI tree and driver list
    cli_tree = None
    drivers = None
    try:
        client = conn.client
        if hasattr(client, "cli"):
            import anyio
            cli_cmd = await anyio.to_thread.run_sync(client.cli)
            cli_tree = walk_click_tree(cli_cmd)
    except Exception:
        pass

    try:
        drivers = list_drivers(conn.client)
    except Exception:
        pass

    return {
        "connection_id": conn.id,
        "lease_name": conn.lease_name,
        "exporter_name": conn.exporter_name,
        "socket_path": conn.socket_path,
        "cli_tree": cli_tree,
        "drivers": drivers,
    }


async def disconnect(
    manager: ConnectionManager,
    connection_id: str,
) -> dict:
    """Disconnect from a device."""
    await manager.disconnect(connection_id)
    return {"connection_id": connection_id, "status": "disconnected"}


def list_connections(manager: ConnectionManager) -> list[dict]:
    """List active connections."""
    return manager.list_connections()
