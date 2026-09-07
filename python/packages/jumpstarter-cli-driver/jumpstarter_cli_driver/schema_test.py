"""Driver-schema discovery is independent of optional installed drivers."""

import json
from dataclasses import field
from enum import Enum
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import yaml
from click.testing import CliRunner
from pydantic import BaseModel
from pydantic.dataclasses import dataclass

from . import driver
from jumpstarter.driver import Driver


class Mode(str, Enum):
    TCP = "tcp"
    UDP = "udp"


class Endpoint(BaseModel):
    mode: Mode = Mode.TCP


@dataclass(kw_only=True)
class ConfigDriver(Driver):
    """A driver with typed configuration, without hardware access."""

    host: str
    port: int
    endpoint: Endpoint = field(default_factory=Endpoint)
    runtime_state: int = field(default=0, init=False)

    @classmethod
    def client(cls):
        return "example.client.ConfigClient"

    def __post_init__(self):
        raise AssertionError("Schema discovery must not instantiate a driver")


@dataclass(kw_only=True)
class RecursiveDriver(ConfigDriver):
    peer: "RecursiveDriver | None" = None


def entry_point(name="Config", cls=ConfigDriver):
    """An entry point whose load can be observed or made to fail."""
    return SimpleNamespace(
        name=name,
        value=f"{cls.__module__}:{cls.__qualname__}",
        dist=SimpleNamespace(name="example-driver", version="1.2.3"),
        load=Mock(return_value=cls),
    )


@pytest.fixture
def installed():
    entry = entry_point()
    with patch("jumpstarter_cli_driver.driver.entry_points", return_value=[entry]) as discover:
        yield entry, discover


def schema_json(*args):
    """Parse stdout directly: discovery chatter must never prefix the JSON."""
    result = CliRunner().invoke(driver, ["schema", *args, "-o", "json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout), result


def test_typed_schema_and_metadata(installed):
    entry, discover = installed
    data, _ = schema_json()
    schema = data["drivers"][0]
    assert schema["name"] == "Config"
    assert schema["type"] == entry.value.replace(":", ".")
    assert schema["client"] == "example.client.ConfigClient"
    assert schema["package"] == "example-driver"
    assert schema["version"] == "1.2.3"
    assert schema["description"] == ConfigDriver.__doc__
    assert schema["properties"]["host"]["type"] == "string"
    assert schema["properties"]["port"]["type"] == "integer"
    assert set(schema["required"]) == {"host", "port"}
    assert schema["error"] is None
    assert set(schema["properties"]) == {"host", "port", "endpoint"}
    discover.assert_called_once_with(group="jumpstarter.drivers")
    entry.load.assert_called_once_with()
    # Nested models and enums keep resolvable references.
    assert schema["properties"]["endpoint"]["$ref"] == "#/$defs/Endpoint"
    assert schema["defs"]["Endpoint"]["properties"]["mode"]["$ref"] == "#/$defs/Mode"
    assert schema["defs"]["Mode"]["enum"] == ["tcp", "udp"]


def test_recursive_root_schema(installed):
    entry, _ = installed
    entry.load.return_value = RecursiveDriver
    schema = schema_json()[0]["drivers"][0]
    assert schema["properties"]["host"]["type"] == "string"
    assert set(schema["required"]) == {"host", "port"}
    assert schema["properties"]["peer"]["anyOf"][0]["$ref"] == "#/$defs/RecursiveDriver"
    assert "RecursiveDriver" in schema["defs"]


def test_fallback_recovers_only_constructor_keys(installed):
    with patch("jumpstarter_cli_driver.driver.TypeAdapter", side_effect=ValueError("unsupported field")):
        schema = schema_json()[0]["drivers"][0]
    assert schema["error"] == "ValueError: unsupported field"
    assert set(schema["properties"]) == {"host", "port", "endpoint"}
    assert set(schema["required"]) == {"host", "port"}
    assert schema["defs"] == {}


def test_broken_import_does_not_hide_other_drivers(installed):
    good, discover = installed
    broken = entry_point("Broken")
    broken.load.side_effect = ImportError("missing optional dependency")
    broken.dist = None
    discover.return_value = [good, broken]
    schemas = schema_json()[0]["drivers"]
    assert [s["name"] for s in schemas] == ["Broken", "Config"]
    assert schemas[0]["error"] == "ImportError: missing optional dependency"
    assert schemas[0]["properties"] == {}
    assert schemas[0]["package"] is None
    assert schemas[0]["version"] is None
    assert schemas[1]["error"] is None


def test_discovery_output_goes_to_stderr(installed):
    entry, _ = installed

    def noisy_import():
        print("driver import chatter {not JSON}")
        return ConfigDriver

    entry.load.side_effect = noisy_import
    _, result = schema_json()
    assert "driver import chatter" in result.stderr
    assert "driver import chatter" not in result.stdout


@pytest.mark.parametrize("selector", ["name", "type"])
def test_filter_loads_only_selected_drivers(installed, selector):
    entry, discover = installed
    other = entry_point("Other")
    other.value = "other.driver:Other"
    discover.return_value = [other, entry]
    name = entry.name if selector == "name" else entry.value.replace(":", ".")
    assert [s["name"] for s in schema_json(name)[0]["drivers"]] == ["Config"]
    other.load.assert_not_called()


@pytest.mark.parametrize("names", [["Missing"], ["Config", "Missing"]])
def test_unknown_filters_fail_before_loading(installed, names):
    entry, _ = installed
    result = CliRunner().invoke(driver, ["schema", *names, "-o", "json"])
    assert result.exit_code != 0
    assert "No installed driver matches: Missing" in result.stderr
    assert result.stdout == ""
    entry.load.assert_not_called()


@pytest.mark.parametrize("output", ["json", "yaml", "name", None])
def test_empty_discovery(installed, output):
    _, discover = installed
    discover.return_value = []
    result = CliRunner().invoke(driver, ["schema", *(["-o", output] if output else [])])
    assert result.exit_code == 0
    if output == "json":
        assert json.loads(result.stdout) == {"drivers": []}
    elif output == "yaml":
        assert yaml.safe_load(result.stdout) == {"drivers": []}
    elif output == "name":
        assert result.stdout == ""
    else:
        assert "No drivers found." in result.stdout


@pytest.mark.parametrize("output", ["yaml", "name", None])
def test_output_formats(installed, output):
    result = CliRunner().invoke(driver, ["schema", *(["-o", output] if output else [])])
    assert result.exit_code == 0
    if output == "yaml":
        assert yaml.safe_load(result.stdout)["drivers"][0]["properties"]["host"]["type"] == "string"
    elif output == "name":
        assert result.stdout.strip() == "Config"
    else:
        assert "CONFIG KEYS" in result.stdout
        assert "Config" in result.stdout
        assert "host" in result.stdout
