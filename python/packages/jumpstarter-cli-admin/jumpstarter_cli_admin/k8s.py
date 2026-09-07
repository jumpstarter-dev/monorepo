import json

import click
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.config.config_exception import ConfigException


def handle_k8s_api_exception(e: ApiException):
    """Handle a Kubernetes API exception"""
    # Try to parse the JSON response
    try:
        json_body = json.loads(e.body)
    except (json.decoder.JSONDecodeError, TypeError):
        raise click.ClickException(f"Server error: {e.body}") from e

    # Valid JSON is not necessarily a Status: a proxy in front of the API server
    # can answer with a bare string, a list, or null.
    if not isinstance(json_body, dict):
        raise click.ClickException(f"Server error: {e.body}") from e

    # Not every Status carries a reason: a 500 from an admission or schema
    # check often has only a message, and losing it leaves nothing to act on.
    message = json_body.get("message") or e.reason or "unknown error"
    reason = json_body.get("reason")
    prefix = f"Error from server ({reason})" if reason else "Error from server"
    raise click.ClickException(f"{prefix}: {message}") from e


def handle_k8s_config_exception(e: ConfigException):
    """Handle a Kubernetes config exception"""
    # Try to parse the JSON response
    raise click.ClickException(e.args[0]) from e
