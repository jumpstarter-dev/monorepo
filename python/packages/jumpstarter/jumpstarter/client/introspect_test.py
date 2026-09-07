import ast
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from unittest.mock import MagicMock, patch

import click
import pytest

from jumpstarter.client.introspect import (
    _get_public_method_names,
    describe_client,
    describe_drivers,
    describe_drivers_async,
    get_driver_methods,
    list_drivers,
    walk_click_tree,
)


def test_description_helpers_are_public_package_exports():
    import jumpstarter.client as client

    for name, helper in (
        ("describe_client", describe_client),
        ("describe_devices", describe_drivers),
        ("describe_devices_async", describe_drivers_async),
        ("describe_drivers", describe_drivers),
        ("describe_drivers_async", describe_drivers_async),
    ):
        assert name in client.__all__
        assert getattr(client, name) is helper


class FakePowerClient:
    children: dict = {}

    def on(self) -> None:
        """Power on the device."""

    def off(self) -> None:
        """Power off the device."""

    def cycle(self, wait: int = 2) -> None:
        """Power cycle the device."""

    def _private(self):
        pass

    def call(self):
        pass

    def check_exporter_status(self):
        pass


class FakeSerialClient:
    children: dict = {}

    def open(self):
        """Open serial port."""

    def pexpect(self):
        """Create pexpect adapter."""


class FakeCompositeClient:
    description = "Test composite device"

    def __init__(self):
        self.children = {
            "power": FakePowerClient(),
            "serial": FakeSerialClient(),
        }

    def __getattr__(self, name):
        try:
            return self.children[name]
        except KeyError:
            raise AttributeError(name) from None

    def cli(self):
        @click.group("j")
        def base():
            """Fake composite device"""

        @base.command("on")
        def on():
            """Power on."""

        return base


class TestWalkClickTree:
    def test_simple_command(self):
        @click.command("hello")
        @click.option("--name", help="Your name")
        def hello(name):
            """Say hello."""

        result = walk_click_tree(hello)
        assert result["name"] == "hello"
        assert result["help"] == "Say hello."
        assert len(result["params"]) == 1
        assert result["params"][0]["name"] == "name"
        assert result["params"][0]["help"] == "Your name"
        assert "subcommands" not in result

    def test_group_with_subcommands(self):
        @click.group("root")
        def root():
            """Root group."""

        @root.command("sub1")
        def sub1():
            """First sub."""

        @root.command("sub2")
        @click.option("--count", type=int, default=5)
        def sub2(count):
            """Second sub."""

        result = walk_click_tree(root)
        assert result["name"] == "root"
        assert "subcommands" in result
        assert "sub1" in result["subcommands"]
        assert "sub2" in result["subcommands"]
        assert result["subcommands"]["sub2"]["params"][0]["name"] == "count"
        assert result["subcommands"]["sub2"]["params"][0]["default"] == 5

    @pytest.mark.parametrize("parameter", [
        click.Argument(["help"], required=True),
        click.Option(["--help"], required=True, help="An explicitly declared input"),
    ])
    def test_declared_parameter_named_help_is_preserved(self, parameter):
        command = click.Command("cmd", params=[parameter], add_help_option=False)
        params = walk_click_tree(command)["params"]
        assert len(params) == 1
        assert params[0]["name"] == "help"
        assert params[0]["kind"] == parameter.param_type_name
        assert params[0]["required"] is True
        assert params[0]["opts"] == parameter.opts

    def test_automatic_help_is_not_a_declared_parameter(self):
        command = click.Command("cmd")
        with click.Context(command) as context:
            assert command.get_help_option(context) is not None
        assert walk_click_tree(command)["params"] == []

    def test_hidden_params_excluded(self):
        @click.command("cmd")
        @click.option("--visible", help="shown")
        @click.option("--secret", hidden=True)
        def cmd(visible, secret):
            pass

        result = walk_click_tree(cmd)
        names = [p["name"] for p in result["params"]]
        assert "visible" in names
        assert "secret" not in names


class TestGetPublicMethodNames:
    def test_filters_private_and_base_methods(self):
        names = _get_public_method_names(FakePowerClient())
        assert "on" in names
        assert "off" in names
        assert "cycle" in names
        assert "_private" not in names
        assert "call" not in names
        assert "check_exporter_status" not in names


class TestListDrivers:
    def test_flat_tree(self):
        client = FakeCompositeClient()
        result = list_drivers(client)
        paths = [d["path"] for d in result]
        assert "client" in paths
        assert "client.power" in paths
        assert "client.serial" in paths

    def test_driver_path_field(self):
        client = FakeCompositeClient()
        result = list_drivers(client)
        root = next(d for d in result if d["path"] == "client")
        assert root["driver_path"] == []
        power = next(d for d in result if d["path"] == "client.power")
        assert power["driver_path"] == ["power"]

    def test_methods_populated(self):
        client = FakeCompositeClient()
        result = list_drivers(client)
        power = next(d for d in result if d["path"] == "client.power")
        assert "on" in power["methods"]
        assert "off" in power["methods"]


class TestGetDriverMethods:
    def test_returns_method_details(self):
        client = FakeCompositeClient()
        result = get_driver_methods(client, ["power"])
        assert result["driver_path"] == ["power"]
        method_names = [m["name"] for m in result["methods"]]
        assert "on" in method_names
        assert "off" in method_names
        assert "cycle" in method_names

    def test_call_example_uses_dot_notation(self):
        client = FakeCompositeClient()
        result = get_driver_methods(client, ["power"])
        cycle = next(m for m in result["methods"] if m["name"] == "cycle")
        assert "client.power.cycle(" in cycle["call_example"]
        assert 'children["power"]' not in cycle["call_example"]
        ast.parse(cycle["call_example"])

    def test_root_driver_example_is_valid_python(self):
        result = get_driver_methods(FakePowerClient(), [])
        cycle = next(m for m in result["methods"] if m["name"] == "cycle")
        assert "client.cycle(wait=...)" in cycle["call_example"]
        assert "client.." not in cycle["call_example"]
        ast.parse(cycle["call_example"])

    def test_invalid_path_raises(self):
        client = FakeCompositeClient()
        with pytest.raises(KeyError, match="nonexistent"):
            get_driver_methods(client, ["nonexistent"])

    def test_docstrings_captured(self):
        client = FakeCompositeClient()
        result = get_driver_methods(client, ["power"])
        on_method = next(m for m in result["methods"] if m["name"] == "on")
        assert on_method["docstring"] == "Power on the device."

    def test_parameters_captured(self):
        client = FakeCompositeClient()
        result = get_driver_methods(client, ["power"])
        cycle = next(m for m in result["methods"] if m["name"] == "cycle")
        assert len(cycle["parameters"]) == 1
        assert cycle["parameters"][0]["name"] == "wait"
        assert cycle["parameters"][0]["default"] == "2"


class TestDescribeClient:
    def test_returns_drivers_and_cli_tree(self):
        result = describe_client(FakeCompositeClient())
        paths = [d["path"] for d in result["drivers"]]
        assert "client" in paths
        assert "client.power" in paths
        assert result["cli_tree"]["name"] == "j"
        assert "on" in result["cli_tree"]["subcommands"]

    def test_cli_tree_none_without_cli(self):
        result = describe_client(FakePowerClient())
        assert result["cli_tree"] is None
        assert result["drivers"][0]["path"] == "client"

    def test_an_inherited_cli_still_counts(self):
        """A driver client is free to inherit its CLI rather than define one.

        QemuFlasherClient is a real example: it has no cli of its own and takes
        one from FlasherClientInterface. Looking only at type(client).__dict__
        would drop the CLI tree for every such driver.
        """

        class InheritsCli(FakeCompositeClient):
            pass

        assert "cli" not in InheritsCli.__dict__
        result = describe_client(InheritsCli())
        assert result["cli_tree"]["name"] == "j"

    def test_a_broken_cli_costs_only_the_cli_tree(self):
        """The driver listing is worth having even when cli() raises."""

        class BrokenCli(FakeCompositeClient):
            def cli(self):
                raise RuntimeError("this driver's cli is broken")

        result = describe_client(BrokenCli())
        assert result["cli_tree"] is None
        assert [d["path"] for d in result["drivers"]] != []

    def test_composite_client_e2e(self):
        from jumpstarter_driver_composite.driver import Composite
        from jumpstarter_driver_power.driver import MockPower

        from jumpstarter.common.utils import serve

        with serve(
            Composite(
                children={
                    "power": MockPower(),
                    "nested": Composite(
                        children={
                            "power": MockPower(),
                        },
                    ),
                },
            )
        ) as client:
            result = describe_client(client)

        paths = [d["path"] for d in result["drivers"]]
        assert "client" in paths
        assert "client.power" in paths
        assert "client.nested.power" in paths
        power = next(d for d in result["drivers"] if d["path"] == "client.power")
        assert power["driver_path"] == ["power"]
        assert "on" in power["methods"]
        assert "power" in result["cli_tree"]["subcommands"]
        assert "nested" in result["cli_tree"]["subcommands"]
        json.dumps(result)

    @pytest.mark.anyio
    async def test_stub_client_represented(self):
        from contextlib import ExitStack

        from anyio.from_thread import BlockingPortal

        from jumpstarter.client.base import StubDriverClient

        async with BlockingPortal() as portal:
            stub = StubDriverClient(
                labels={"jumpstarter.dev/client": "missing_pkg.client.MissingClient"},
                stub=None,
                portal=portal,
                stack=ExitStack(),
            )
            result = describe_client(stub)

        assert result["cli_tree"] is None
        assert result["drivers"][0]["class"].endswith("StubDriverClient")


class TestDescribeDrivers:
    def test_legacy_names_remain_compatible(self):
        from jumpstarter.client.introspect import describe_devices, describe_devices_async

        assert describe_devices is describe_drivers
        assert describe_devices_async is describe_drivers_async

    @pytest.fixture()
    def mock_config(self):
        fake_lease = MagicMock()
        fake_lease.allow = []
        fake_lease.unsafe = True

        @asynccontextmanager
        async def fake_serve_unix_async():
            yield "/tmp/fake.sock"

        fake_lease.serve_unix_async = fake_serve_unix_async

        @asynccontextmanager
        async def fake_lease_async(*args, **kwargs):
            yield fake_lease

        config = MagicMock()
        config.lease_async = MagicMock(side_effect=fake_lease_async)
        return config

    @pytest.fixture()
    def mock_client_from_path(self):
        @asynccontextmanager
        async def fake_client_from_path(path, portal, stack, allow, unsafe):
            yield FakeCompositeClient()

        with patch("jumpstarter.client.introspect.client_from_path", fake_client_from_path):
            yield

    @pytest.mark.anyio
    async def test_attaches_to_named_lease(self, mock_config, mock_client_from_path):
        result = await describe_drivers_async(mock_config, "existing-lease")

        kwargs = mock_config.lease_async.call_args.kwargs
        assert kwargs["lease_name"] == "existing-lease"
        assert kwargs["selector"] is None
        assert kwargs["exporter_name"] is None
        # lease_async requires a duration argument, but attaching by name never
        # sends it to the controller. Keep the placeholder explicitly neutral.
        assert kwargs["duration"] == timedelta(0)
        paths = [d["path"] for d in result["drivers"]]
        assert "client.power" in paths
        assert result["cli_tree"]["name"] == "j"
        json.dumps(result)

    @pytest.mark.anyio
    async def test_empty_lease_name_raises(self, mock_config):
        with pytest.raises(ValueError, match="non-empty"):
            await describe_drivers_async(mock_config, "")
        mock_config.lease_async.assert_not_called()

    def test_blocking_wrapper(self, mock_config, mock_client_from_path):
        result = describe_drivers(mock_config, "existing-lease")

        kwargs = mock_config.lease_async.call_args.kwargs
        assert kwargs["lease_name"] == "existing-lease"
        assert result["cli_tree"]["name"] == "j"


class TestParamDescription:
    @pytest.mark.parametrize("mapping_type", [dict, MappingProxyType])
    def test_mapping_defaults_remain_structured(self, mapping_type):
        """Preserve nested mappings while coercing keys and non-JSON leaves."""
        default = mapping_type({
            "nested": mapping_type({1: (Path("image.bin"), None, True)}),
            "items": [mapping_type({"count": 2})],
        })
        command = click.Command("cmd", params=[click.Option(["--config"], default=default)])
        tree = walk_click_tree(command)
        assert tree["params"][0]["default"] == {
            "nested": {"1": ["image.bin", None, True]},
            "items": [{"count": 2}],
        }
        assert json.loads(json.dumps(tree)) == tree

    @pytest.mark.parametrize("default", [
        {1: "numeric", "1": "string"},
        {"1": "string", 1: "numeric"},
        {"nested": [MappingProxyType({1: "numeric", "1": "string"})]},
    ])
    def test_mapping_key_collisions_are_rejected(self, default):
        """Never report a default with values lost to stringified key collisions."""
        command = click.Command("cmd", params=[click.Option(["--config"], default=default)])
        with pytest.raises(ValueError, match="Mapping keys collide after stringification: '1'"):
            walk_click_tree(command)

    def test_describes_arguments_and_options(self):
        @click.group()
        def root():
            pass

        @root.command()
        @click.argument("port", type=int)
        @click.option("--address", help="Local address to bind")
        @click.option("--verbose", is_flag=True)
        @click.option("--mode", type=click.Choice(["fast", "slow"]), default="fast")
        @click.option("--tag", multiple=True)
        def forward(port, address, verbose, mode, tag):
            """Forward a port."""

        tree = walk_click_tree(root)
        params = {p["name"]: p for p in tree["subcommands"]["forward"]["params"]}

        # A positional argument is spelled by name and an option by its flag,
        # so a caller building a command line has to tell them apart.
        assert params["port"]["kind"] == "argument"
        assert params["port"]["required"] is True
        assert params["port"]["type"] == "integer"
        assert params["address"]["kind"] == "option"
        assert params["address"]["opts"] == ["--address"]
        assert params["address"]["help"] == "Local address to bind"
        assert params["verbose"]["is_flag"] is True
        assert params["mode"]["choices"] == ["fast", "slow"]
        assert params["tag"]["multiple"] is True

    def test_type_name_is_readable(self):
        @click.group()
        def root():
            pass

        @root.command()
        @click.argument("path", type=click.Path())
        def send(path):
            pass

        params = walk_click_tree(root)["subcommands"]["send"]["params"]
        # str(click.Path()) is an object repr, which is useless in a prompt.
        assert params[0]["type"] == "path"
