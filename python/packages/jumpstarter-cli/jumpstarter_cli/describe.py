from datetime import datetime, timezone

import click
from jumpstarter_cli_common.alias import AliasedGroup
from jumpstarter_cli_common.config import opt_config
from jumpstarter_cli_common.exceptions import handle_exceptions, handle_exceptions_with_reauthentication
from jumpstarter_cli_common.oidc import decode_jwt, format_duration, get_token_remaining_seconds
from jumpstarter_cli_common.opt import OutputMode, OutputType
from jumpstarter_cli_common.print import model_print
from pydantic import BaseModel, ConfigDict, Field

from .login import relogin_client
from jumpstarter.client.grpc import Lease
from jumpstarter.client.introspect import describe_drivers
from jumpstarter.config.client import ClientConfigV1Alpha1
from jumpstarter.config.user import UserConfigV1Alpha1

opt_output = click.option(
    "-o",
    "--output",
    type=click.Choice([OutputMode.JSON, OutputMode.YAML]),
    default=None,
    help="Output mode. Defaults to a human-readable description.",
)


def _format_value(value) -> str:
    if value is None or value == "":
        return "<none>"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S %Z").strip()
    return str(value)


def _print_fields(fields: list[tuple[str, object]], indent: int = 0) -> None:
    width = max(len(label) for label, _ in fields) + 1
    prefix = " " * indent
    for label, value in fields:
        click.echo(f"{prefix}{label + ':':<{width}}  {_format_value(value)}")


def _print_mapping(label: str, mapping: dict[str, str], indent: int = 0) -> None:
    prefix = " " * indent
    if not mapping:
        click.echo(f"{prefix}{label}:  <none>")
        return
    click.echo(f"{prefix}{label}:")
    for key, value in sorted(mapping.items()):
        click.echo(f"{prefix}  {key}={value}")


def _condition_time(condition) -> datetime | None:
    if condition.HasField("lastTransitionTime"):
        time = condition.lastTransitionTime
        return datetime.fromtimestamp(time.seconds + time.nanos / 1e9, tz=timezone.utc)
    return None


def _print_conditions(conditions) -> None:
    _print_table(
        "Conditions",
        ["Type", "Status", "Reason", "Message", "Last Transition Time"],
        [
            [
                _format_value(condition.type),
                _format_value(condition.status),
                _format_value(condition.reason),
                _format_value(condition.message),
                _format_value(_condition_time(condition)),
            ]
            for condition in conditions
        ],
    )


@click.group(cls=AliasedGroup)
def describe():
    """
    Show detailed information about a specific resource
    """


def _holding_lease(config, exporter: str):
    """The lease currently holding an exporter, if any.

    GetExporter answers with the exporter on its own, so the lease has to be
    looked up separately — without it, describing a busy exporter could not say
    who has it, which is the main reason to ask.
    """
    for lease in config.list_leases(only_active=True).leases:
        if lease.exporter == exporter:
            return lease
    return None


@describe.command(name="exporter")
@opt_config(exporter=False)
@click.argument("name")
@opt_output
@handle_exceptions_with_reauthentication(relogin_client)
def describe_exporter(config, name: str, output: OutputType):
    """
    Show details of a specific exporter
    """

    exporter = config.get_exporter(name)
    exporter = exporter.model_copy(update={"lease": _holding_lease(config, name)})

    if output:
        model_print(exporter, output)
        return

    _print_fields(
        [
            ("Name", exporter.name),
            ("Namespace", exporter.namespace),
        ]
    )
    _print_mapping("Labels", exporter.labels)
    if exporter.deprecated_labels:
        _print_mapping("Deprecated Labels", exporter.deprecated_labels)
    _print_fields(
        [
            ("Online", exporter.online),
            ("Status", str(exporter.status) if exporter.status else "UNKNOWN"),
            ("Enabled", exporter.enabled),
        ]
    )
    if exporter.lease:
        lease = exporter.lease
        click.echo("Lease:")
        _print_fields(
            [
                ("Name", lease.name),
                ("Client", lease.client),
                ("Status", lease.get_status()),
                ("Begin Time", lease.effective_begin_time or lease.begin_time),
                ("End Time", lease.effective_end_time),
                ("Duration", lease.duration),
            ],
            indent=2,
        )
    else:
        click.echo("Lease:  <none>")


class LeaseDescription(BaseModel):
    lease: Lease
    driver_tree: dict


def _walk_commands(tree: dict, path: list[str]) -> list[tuple[str, str]]:
    commands = []
    for name, subtree in sorted((tree.get("subcommands") or {}).items()):
        subpath = [*path, name]
        if subtree.get("subcommands"):
            commands.extend(_walk_commands(subtree, subpath))
        else:
            help_text = (subtree.get("help") or "").strip().splitlines()
            commands.append((" ".join(subpath), help_text[0] if help_text else ""))
    return commands


def _print_table(label: str, headers: list[str], rows: list[list[str]]) -> None:
    if not rows:
        click.echo(f"{label}:  <none>")
        return
    click.echo(f"{label}:")
    widths = [max([len(header)] + [len(row[i]) for row in rows]) for i, header in enumerate(headers)]
    dashes = ["-" * len(header) for header in headers]
    for cells in [headers, dashes, *rows]:
        click.echo("  " + "  ".join(cell.ljust(width) for cell, width in zip(cells, widths, strict=True)).rstrip())


def _print_drivers(driver_tree: dict) -> None:
    _print_table(
        "Drivers",
        ["Path", "Class", "Methods"],
        [
            [
                ".".join(driver["driver_path"]) or "(root)",
                driver["class"],
                ", ".join(driver["methods"]),
            ]
            for driver in driver_tree["drivers"]
        ],
    )
    commands = _walk_commands(driver_tree["cli_tree"], ["j"]) if driver_tree.get("cli_tree") else []
    _print_table("Commands", ["Command", "Description"], [[command, help] for command, help in commands])


@describe.command(name="lease")
@opt_config(exporter=False)
@click.argument("name")
@click.option(
    "--drivers",
    "show_drivers",
    is_flag=True,
    default=False,
    help="Connect to the leased exporter and include its driver tree and driver commands.",
)
@opt_output
@handle_exceptions_with_reauthentication(relogin_client)
def describe_lease(config, name: str, show_drivers: bool, output: OutputType):
    """
    Show details of a specific lease
    """

    lease = config.get_lease(name=name)
    driver_tree = describe_drivers(config, name) if show_drivers else None

    if output:
        if driver_tree is not None:
            model_print(LeaseDescription(lease=lease, driver_tree=driver_tree), output)
        else:
            model_print(lease, output)
        return

    _print_fields(
        [
            ("Name", lease.name),
            ("Namespace", lease.namespace),
            ("Selector", lease.selector),
            ("Exporter", lease.exporter),
            ("Client", lease.client),
            ("Status", lease.get_status()),
            ("Duration", lease.duration),
            ("Effective Begin Time", lease.effective_begin_time),
            ("Effective End Time", lease.effective_end_time),
        ]
    )
    _print_mapping("Tags", lease.tags)
    _print_mapping("Context", lease.context)
    _print_conditions(lease.conditions)
    if driver_tree is not None:
        _print_drivers(driver_tree)


class ClientDescription(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    alias: str
    path: str | None
    current: bool
    name: str | None
    namespace: str | None
    endpoint: str | None
    tls_ca_configured: bool = Field(alias="tlsCaConfigured")
    tls_insecure: bool = Field(alias="tlsInsecure")
    drivers_allow: list[str] = Field(alias="driversAllow")
    drivers_unsafe: bool = Field(alias="driversUnsafe")
    token_expiry: datetime | None = Field(alias="tokenExpiry")
    token_status: str = Field(alias="tokenStatus")
    refresh_token_stored: bool = Field(alias="refreshTokenStored")


def _token_details(token: str | None) -> tuple[datetime | None, str]:
    if not token:
        return None, "no token"
    try:
        payload = decode_jwt(token)
    except Exception:
        return None, "malformed"
    exp = payload.get("exp")
    remaining = get_token_remaining_seconds(token)
    if exp is None or remaining is None:
        return None, "no expiry claim"
    expiry = datetime.fromtimestamp(exp, tz=timezone.utc)
    if remaining < 0:
        return expiry, f"expired ({format_duration(remaining)} ago)"
    return expiry, f"valid ({format_duration(remaining)} remaining)"


@describe.command(name="client")
@click.argument("alias", required=False, default=None)
@opt_output
@handle_exceptions
def describe_client(alias: str | None, output: OutputType):
    """
    Show details of a client config
    """

    if alias is not None:
        config = ClientConfigV1Alpha1.load(alias)
    else:
        config = UserConfigV1Alpha1.load_or_create().config.current_client
        if config is None:
            raise click.ClickException("no client alias specified and no default client is set")

    current_alias = ClientConfigV1Alpha1.list().current_config

    token_expiry, token_status = _token_details(config.token)

    description = ClientDescription(
        alias=config.alias,
        path=str(config.path) if config.path else None,
        current=current_alias is not None and config.alias == current_alias,
        name=config.metadata.name,
        namespace=config.metadata.namespace,
        endpoint=config.endpoint,
        tls_ca_configured=bool(config.tls.ca),
        tls_insecure=config.tls.insecure,
        drivers_allow=config.drivers.allow,
        drivers_unsafe=config.drivers.unsafe,
        token_expiry=token_expiry,
        token_status=token_status,
        refresh_token_stored=bool(config.refresh_token),
    )

    if output:
        model_print(description, output)
        return

    _print_fields(
        [
            ("Alias", description.alias),
            ("Path", description.path),
            ("Current", description.current),
            ("Name", description.name),
            ("Namespace", description.namespace),
            ("Endpoint", description.endpoint),
        ]
    )
    click.echo("TLS:")
    _print_fields(
        [
            ("CA", "configured" if description.tls_ca_configured else None),
            ("Insecure", description.tls_insecure),
        ],
        indent=2,
    )
    click.echo("Drivers:")
    _print_fields(
        [
            ("Allow", ", ".join(description.drivers_allow) if description.drivers_allow else None),
            ("Unsafe", description.drivers_unsafe),
        ],
        indent=2,
    )
    click.echo("Token:")
    _print_fields(
        [
            ("Expiry", description.token_expiry),
            ("Status", description.token_status),
        ],
        indent=2,
    )
    _print_fields([("Refresh Token Stored", description.refresh_token_stored)])
