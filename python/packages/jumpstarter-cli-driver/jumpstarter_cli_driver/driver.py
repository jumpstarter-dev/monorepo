import dataclasses
import inspect
import sys
from contextlib import redirect_stdout
from importlib.metadata import entry_points
from typing import Any

import click
from jumpstarter_cli_common.opt import OutputType, opt_output_all
from jumpstarter_cli_common.print import model_print
from pydantic import BaseModel, TypeAdapter


class DriverEntry(BaseModel):
    name: str
    type: str
    package: str | None = None
    version: str | None = None

    def rich_add_rows(self, table):
        table.add_row(self.name, self.type)

    def rich_add_names(self, names):
        names.append(self.name)


class DriverEntryList(BaseModel):
    drivers: list[DriverEntry]

    @classmethod
    def rich_add_columns(cls, table):
        table.add_column("NAME", no_wrap=True)
        table.add_column("TYPE")

    def rich_add_rows(self, table):
        for entry in self.drivers:
            entry.rich_add_rows(table)

    def rich_add_names(self, names):
        for entry in self.drivers:
            entry.rich_add_names(names)


class DriverSchema(BaseModel):
    """A driver's user-settable config keys, as JSON Schema."""

    name: str
    type: str
    # Client class this driver is consumed through — what a client config's
    # drivers.allow patterns are matched against.
    client: str | None = None
    package: str | None = None
    version: str | None = None
    description: str | None = None
    # JSON Schema fragment for the exporter config's `config:` block.
    properties: dict[str, Any] = {}
    required: list[str] = []
    # Definitions the properties reference (enums, nested models). Kept so a
    # consumer can resolve the "#/$defs/..." refs inside properties.
    defs: dict[str, Any] = {}
    # Set instead of the schema when the driver class could not be loaded
    # (an optional dependency is missing, most often).
    error: str | None = None

    def rich_add_rows(self, table):
        keys = ", ".join(sorted(self.properties)) if self.properties else ""
        table.add_row(self.name, self.error or keys, ", ".join(self.required))

    def rich_add_names(self, names):
        names.append(self.name)


class DriverSchemaList(BaseModel):
    drivers: list[DriverSchema]

    @classmethod
    def rich_add_columns(cls, table):
        table.add_column("NAME", no_wrap=True)
        table.add_column("CONFIG KEYS")
        table.add_column("REQUIRED")

    def rich_add_rows(self, table):
        for entry in self.drivers:
            entry.rich_add_rows(table)

    def rich_add_names(self, names):
        for entry in self.drivers:
            entry.rich_add_names(names)


def _base_field_names() -> set[str]:
    """Fields every Driver has, which are not part of a driver's own config."""
    from jumpstarter.driver import Driver

    return {field.name for field in dataclasses.fields(Driver)}


def _first_docstring_line(cls: type) -> str | None:
    doc = inspect.getdoc(cls)
    if not doc:
        return None
    return doc.strip().splitlines()[0]


def _client_class_path(cls: type) -> str | None:
    """The driver client class path, which drivers.allow patterns are matched against."""
    client = getattr(cls, "client", None)
    if not callable(client):
        return None
    try:
        value = client()
    except Exception:
        return None
    return value if isinstance(value, str) else None


def _fields_without_schema(cls: type, base_fields: set[str]) -> tuple[dict[str, Any], list[str]]:
    """Config keys recovered from the dataclass when JSON Schema generation fails.

    Some drivers hold fields whose types pydantic cannot model. Their names and
    annotations are still worth reporting: without types a consumer can still
    complete the keys and tell required from optional.
    """
    properties: dict[str, Any] = {}
    required: list[str] = []
    if not dataclasses.is_dataclass(cls):
        return properties, required
    for field in dataclasses.fields(cls):
        if field.name in base_fields or not field.init:
            continue
        annotation = field.type if isinstance(field.type, str) else getattr(field.type, "__name__", None)
        properties[field.name] = {"title": field.name} | (
            {"description": f"type: {annotation}"} if annotation else {}
        )
        if field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING:
            required.append(field.name)
    return properties, required


def _schema_for_entry_point(entry_point, base_fields: set[str]) -> DriverSchema:
    dist = entry_point.dist
    entry = DriverSchema(
        name=entry_point.name,
        type=entry_point.value.replace(":", "."),
        package=dist.name if dist else None,
        version=dist.version if dist else None,
    )
    try:
        cls = entry_point.load()
    except Exception as e:
        # One uninstallable driver must not sink the whole listing.
        entry.error = f"{type(e).__name__}: {e}"
        return entry

    entry.description = _first_docstring_line(cls)
    entry.client = _client_class_path(cls)
    try:
        schema = TypeAdapter(cls).json_schema()
    except Exception as e:
        entry.error = f"{type(e).__name__}: {e}"
        if dataclasses.is_dataclass(cls):
            entry.properties, entry.required = _fields_without_schema(cls, base_fields)
        return entry

    entry.defs = schema.get("$defs", {})
    # Recursive dataclasses can put the root object in $defs as well.
    if schema.get("$ref", "").startswith("#/$defs/"):
        schema = entry.defs[schema["$ref"].removeprefix("#/$defs/")]
    excluded_fields = set(base_fields)
    if dataclasses.is_dataclass(cls):
        excluded_fields.update(field.name for field in dataclasses.fields(cls) if not field.init)
    entry.properties = {k: v for k, v in schema.get("properties", {}).items() if k not in excluded_fields}
    entry.required = [r for r in schema.get("required", []) if r not in excluded_fields]
    return entry


@click.command("schema")
@click.argument("names", nargs=-1)
@opt_output_all
def driver_schema(names: tuple[str, ...], output: OutputType):
    """Show the config keys accepted by installed drivers

    Reports each driver's user-settable config keys as JSON Schema, derived
    from the driver class itself, for the `config:` block of an exporter
    config. NAMES filters by driver name or by full type path.
    """
    wanted = set(names)
    installed = sorted(entry_points(group="jumpstarter.drivers"), key=lambda ep: (ep.name, ep.value))
    known = {name for ep in installed for name in (ep.name, ep.value.replace(":", "."))}
    missing = wanted - known
    if missing:
        raise click.ClickException(f"No installed driver matches: {', '.join(sorted(missing))}")

    # Imports (and driver schema hooks) can print. Keep Python-level output on
    # stderr so stdout remains a single machine-readable document. Loading a
    # driver executes local package code; this is not a sandbox.
    with redirect_stdout(sys.stderr):
        base_fields = _base_field_names()
        drivers = [
            _schema_for_entry_point(entry_point, base_fields)
            for entry_point in installed
            if not wanted or entry_point.name in wanted or entry_point.value.replace(":", ".") in wanted
        ]

    if not drivers and output is None:
        click.echo("No drivers found.")
        return

    model_print(DriverSchemaList(drivers=drivers), output)


@click.command("list")
@opt_output_all
def list_drivers(output: OutputType):
    """List drivers installed in the current environment"""
    drivers = []
    for entry_point in entry_points(group="jumpstarter.drivers"):
        dist = entry_point.dist
        drivers.append(
            DriverEntry(
                name=entry_point.name,
                type=entry_point.value.replace(":", "."),
                package=dist.name if dist else None,
                version=dist.version if dist else None,
            )
        )

    if not drivers and output is None:
        click.echo("No drivers found.")
        return

    model_print(DriverEntryList(drivers=drivers), output)
