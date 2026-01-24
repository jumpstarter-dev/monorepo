import logging
import sys
from functools import partial
from typing import Literal, Optional

import click
from rich import traceback
from rich.logging import RichHandler


def _opt_log_level_callback(ctx, param, value):
    traceback.install()
    # there is no way to determine if the command is invoked for jmp run or something else at this
    # point based on ctx and params, so we just look at sys.argv
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        # on a exporter run we don't want to use RichHandler for logs, just plain logs for the system journal
        basicConfig = partial(logging.basicConfig)
    else:
        basicConfig = partial(
            logging.basicConfig, format="%(message)s [%(name)s]", handlers=[RichHandler(show_path=False)]
        )

    if value:
        basicConfig(level=value.upper())
    else:
        basicConfig(level=logging.INFO)


opt_log_level = click.option(
    "--log-level",
    "log_level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]),
    help="Set the log level",
    expose_value=False,
    callback=_opt_log_level_callback,
)


opt_kubeconfig = click.option(
    "--kubeconfig", "kubeconfig", type=click.File(), default=None, help="path to the kubeconfig file"
)

opt_context = click.option("--context", "context", type=str, default=None, help="Kubernetes context to use")

opt_namespace = click.option("-n", "--namespace", type=str, help="Kubernetes namespace to use", default="default")


def _opt_labels_callback(ctx, param, value):
    labels = {}

    for label in value:
        k, sep, v = label.partition("=")
        if sep == "":
            raise click.BadParameter("Invalid label '{}', should be formatted as 'key=value'".format(k))
        labels[k] = v

    return labels


opt_labels = partial(
    click.option,
    "-l",
    "--label",
    "labels",
    type=str,
    multiple=True,
    help="Labels to set on resource, can be set multiple times",
    callback=_opt_labels_callback,
)

opt_insecure_tls_config = click.option(
    "--insecure-tls-config",
    "insecure_tls_config",
    is_flag=True,
    default=False,
    help="Disable endpoint TLS verification. This is insecure and should only be used for testing purposes",
)


def confirm_insecure_tls(insecure_tls_config: bool, nointeractive: bool):
    """Confirm if insecure TLS config is enabled and user wants to continue.

    Args:
        insecure_tls_config (bool): Insecure TLS config flag requested by the user.
        nointeractive (bool): This flag is set to True if the command is run in non-interactive mode.

    Raises:
        click.Abort: Abort the command if user does not want to continue.
    """
    if nointeractive is False and insecure_tls_config:
        if not click.confirm("Insecure TLS config is enabled. Are you sure you want to continue?"):
            click.echo("Aborting.")
            raise click.Abort()


class OutputMode(str):
    JSON = "json"
    YAML = "yaml"
    NAME = "name"
    PATH = "path"


OutputType = Optional[OutputMode]

opt_output_all = click.option(
    "-o",
    "--output",
    type=click.Choice([OutputMode.JSON, OutputMode.YAML, OutputMode.NAME]),
    default=None,
    help='Output mode. Use "-o name" for shorter output (resource/name).',
)

NameOutputType = Optional[Literal["name"]]

opt_output_name_only = click.option(
    "-o",
    "--output",
    type=click.Choice([OutputMode.NAME]),
    default=None,
    help='Output mode. Use "-o name" for shorter output (resource/name).',
)

PathOutputType = Optional[Literal["path"]]

opt_output_path_only = click.option(
    "-o",
    "--output",
    type=click.Choice([OutputMode.PATH]),
    default=None,
    help='Output mode. Use "-o path" for shorter output (file/path).',
)

opt_nointeractive = click.option(
    "--nointeractive", is_flag=True, default=False, help="Disable interactive prompts (for use in scripts)."
)


def _normalize_tokens(items: list[str], normalize_case: bool) -> list[str]:
    """Extract and normalize tokens from comma-separated values."""
    tokens = (
        token.strip().lower() if normalize_case else token.strip()
        for item in items
        for token in item.split(',')
    )
    return [token for token in tokens if token]


def _deduplicate_tokens(tokens: list[str]) -> list[str]:
    """Remove duplicates while preserving order."""
    return list(dict.fromkeys(tokens))


def _validate_tokens(tokens: list[str], allowed_values: set[str], ctx, param) -> None:
    """Validate tokens against allowed values."""
    invalid = [t for t in tokens if t not in allowed_values]
    if invalid:
        allowed_list = ", ".join(sorted(allowed_values))
        raise click.BadParameter(
            f"Invalid value(s) {invalid}. Allowed values are: {allowed_list}",
            ctx=ctx,
            param=param
        )


def parse_comma_separated(
    ctx: click.Context,
    param: click.Parameter,
    value: str | tuple[str, ...] | None,
    allowed_values: set[str] | None = None,
    normalize_case: bool = True
) -> list[str]:
    """Generic comma-separated value parser with validation and normalization.

    Supports both CSV format ("a,b") and repeated flags ("a" "b" from --flag a --flag b).
    Normalizes by stripping whitespace, optionally lowercasing, deduplicating while preserving order.
    Optionally validates against allowed values and raises click.BadParameter on invalid tokens.

    Args:
        ctx: Click context
        param: Click parameter
        value: Input value(s) - string for CSV or tuple for repeated flags
        allowed_values: Set of allowed values for validation (None = no validation)
        normalize_case: Whether to convert values to lowercase

    Returns:
        List of normalized, deduplicated values

    Raises:
        click.BadParameter: If validation fails with invalid tokens
    """
    if not value:
        return []

    # Handle both single string and tuple (from multiple flag usage)
    items = [value] if isinstance(value, str) else list(value)

    # Process tokens through the pipeline
    all_tokens = _normalize_tokens(items, normalize_case)
    unique_tokens = _deduplicate_tokens(all_tokens)

    # Validate if allowed values are specified
    if allowed_values is not None:
        _validate_tokens(unique_tokens, allowed_values, ctx, param)

    return unique_tokens


def opt_comma_separated(
    name: str,
    allowed_values: set[str] | None = None,
    normalize_case: bool = True,
    help_text: str | None = None
):
    """Create a click option for comma-separated values with optional validation.

    Args:
        name: Option name (e.g. "with" creates --with option)
        allowed_values: Set of allowed values for validation (None = no validation)
        normalize_case: Whether to convert values to lowercase
        help_text: Custom help text (auto-generated if None)

    Returns:
        Click option decorator
    """

    def callback(ctx, param, value):
        return parse_comma_separated(ctx, param, value, allowed_values, normalize_case)

    # Auto-generate help text if not provided
    if help_text is None:
        if allowed_values:
            allowed_list = ", ".join(sorted(allowed_values))
            help_text = f"Comma-separated values. Allowed: {allowed_list} (comma-separated or repeated)"
        else:
            help_text = "Comma-separated values (comma-separated or repeated)"

    return click.option(
        f"--{name}",
        f"{name}_options",
        callback=callback,
        multiple=True,
        help=help_text
    )
