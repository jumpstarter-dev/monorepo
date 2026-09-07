"""Introspection must drive synchronous driver facades outside the event loop."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import click
import pytest
from anyio import fail_after
from anyio.from_thread import BlockingPortal

from jumpstarter_mcp.tools.commands import driver_methods, drivers, explore


class LeafClient:
    children: dict = {}

    def on(self):
        """Turn power on."""


class PortalClient:
    """Model driver attributes/CLI builders that dispatch through a live portal.

    Calling these on the event-loop thread raises immediately, just like a
    synchronous driver facade. A static fake would miss that regression.
    """

    def __init__(self, portal):
        self.portal = portal

    @property
    def description(self):
        return self.portal.call(lambda: "A remotely described driver")

    @property
    def children(self):
        return self.portal.call(lambda: {"power": LeafClient()})

    def cli(self):
        self.portal.call(lambda: None)
        return click.Group("j", commands={"on": click.Command("on")})


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["drivers", "driver_methods", "explore"])
async def test_introspection_dispatches_off_the_loop(tool):
    with fail_after(5):
        async with BlockingPortal() as portal:
            manager = MagicMock()
            manager.get_connection.return_value = SimpleNamespace(client=PortalClient(portal))
            if tool == "drivers":
                result = await drivers(manager, "connection-1")
                assert result[0]["description"] == "A remotely described driver"
                assert result[1]["driver_path"] == ["power"]
            elif tool == "driver_methods":
                result = await driver_methods(manager, "connection-1", ["power"])
                assert [method["name"] for method in result["methods"]] == ["on"]
            else:
                result = await explore(manager, "connection-1")
                assert "on" in result["subcommands"]
            manager.get_connection.assert_called_once_with("connection-1")
