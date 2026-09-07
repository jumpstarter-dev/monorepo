"""Introspection utilities for Click CLI trees and driver object trees."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Mapping
from contextlib import ExitStack, asynccontextmanager
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import click
import click.core
from anyio import to_thread
from anyio.from_thread import BlockingPortal, start_blocking_portal

from .base import StubDriverClient
from .client import client_from_path

if TYPE_CHECKING:
    from jumpstarter.config.client import ClientConfigV1Alpha1

logger = logging.getLogger(__name__)


def _json_safe(value: Any) -> Any:
    """Coerce a value to a JSON-serializable equivalent, stringifying as a last resort."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            json_key = str(key)
            if json_key in result:
                raise ValueError(f"Mapping keys collide after stringification: {json_key!r}")
            result[json_key] = _json_safe(item)
        return result
    return str(value)


def _describe_param(param: click.Parameter) -> dict[str, Any]:
    """Describe one Click parameter well enough to prompt for it.

    Beyond name and help, a caller building a command line needs to know
    whether the parameter is positional or an option (and which flag spells
    it), whether it is a boolean flag that takes no value, whether it repeats,
    and the values it accepts when it is a choice.
    """
    described: dict[str, Any] = {
        "name": param.name,
        "kind": param.param_type_name,
        # Click's own type name ("integer", "path", "choice"); str() on some
        # types renders an object repr, which is no use in a prompt.
        "type": getattr(param.type, "name", None) or str(param.type),
        "help": getattr(param, "help", None),
        "required": getattr(param, "required", False),
        "default": _json_safe(param.default) if param.default is not None else None,
        "opts": list(param.opts),
        "is_flag": bool(getattr(param, "is_flag", False)),
        "multiple": bool(getattr(param, "multiple", False)),
        "nargs": param.nargs,
    }
    if isinstance(param.type, click.Choice):
        described["choices"] = [str(choice) for choice in param.type.choices]
    return described


def walk_click_tree(cmd: click.core.BaseCommand, path: list[str] | None = None) -> dict[str, Any]:  # ty: ignore[unresolved-attribute]
    """Recursively walk a Click command tree and return structured JSON.

    Returns command names, help text, parameters (with types and defaults),
    and nested subcommands.
    """
    path = path or []
    result: dict[str, Any] = {
        "name": cmd.name,
        "help": cmd.help,
        "params": [
            _describe_param(p) for p in cmd.params if not getattr(p, "hidden", False)
        ],
    }
    if isinstance(cmd, click.Group):
        result["subcommands"] = {
            name: walk_click_tree(sub, path + [name])
            for name, sub in cmd.commands.items()
        }
    return result


# Methods on the base DriverClient that should be excluded from introspection
_BASE_METHODS = {
    "call",
    "streamingcall",
    "stream",
    "stream_async",
    "log_stream",
    "log_stream_async",
    "open_stream",
    "close",
    "reset",
    "cli",
    "call_async",
    "streamingcall_async",
    "report",
    "report_async",
    "get_status_async",
    "end_session_async",
    "status_monitor_async",
}

# Methods inherited from base classes that add noise to driver listings
_INTERNAL_METHODS = {
    "check_exporter_status",
    "resource_async",
    "wait_for_hook_complete_monitored",
    "wait_for_hook_status",
    "wait_for_lease_ready",
    "wait_for_lease_ready_monitored",
}


def _get_public_method_names(obj: Any) -> list[str]:
    """Return public method names for a driver instance, excluding base internals.

    Uses getmembers_static to avoid triggering property descriptors, which can
    make gRPC calls that fail when invoked from the event loop thread.
    """
    names = []
    try:
        members = inspect.getmembers_static(obj)
    except Exception:
        logger.debug("inspect.getmembers_static failed for %s", type(obj).__name__, exc_info=True)
        return names
    for name, value in members:
        if name.startswith("_") or name in _BASE_METHODS or name in _INTERNAL_METHODS:
            continue
        if isinstance(value, property):
            continue
        if not callable(value):
            continue
        try:
            inspect.signature(value)
        except (ValueError, TypeError, AttributeError):
            continue
        names.append(name)
    return names


def list_drivers(client: Any, _keys: list[str] | None = None) -> list[dict[str, Any]]:
    """Flatten the driver client tree into a list with dot-separated paths.

    Returns path (Python access path like ``client.power``), driver_path
    (children-key list like ``["power"]`` for use with get_driver_methods),
    class name, description, and method names for each driver.
    """
    keys = _keys or []
    cls = type(client)
    results = [
        {
            "path": f"client.{'.'.join(keys)}" if keys else "client",
            "driver_path": keys,
            "class": f"{cls.__module__}.{cls.__qualname__}",
            "description": getattr(client, "description", None),
            "methods": _get_public_method_names(client),
        }
    ]
    children = getattr(client, "children", {})
    for name, child in children.items():
        results.extend(list_drivers(child, keys + [name]))
    return results


def _inspect_method(name: str, method: Any, driver_path: list[str]) -> dict[str, Any] | None:
    """Build a method descriptor dict, or return None if the method can't be inspected."""
    try:
        sig = inspect.signature(method)
    except (ValueError, TypeError, AttributeError):
        return None

    is_streaming = False
    try:
        source = inspect.getsource(method)
        is_streaming = "streamingcall" in source
    except (OSError, TypeError):
        pass

    params = [
        {
            "name": pname,
            "annotation": str(p.annotation) if p.annotation != inspect.Parameter.empty else None,
            "default": str(p.default) if p.default != inspect.Parameter.empty else None,
        }
        for pname, p in sig.parameters.items()
        if pname != "self"
    ]

    attr_path = ".".join(driver_path)
    call_args = ", ".join(f"{p['name']}=..." for p in params)
    receiver = f"client.{attr_path}" if attr_path else "client"
    method_call = f"{receiver}.{name}({call_args})"

    return {
        "name": name,
        "signature": str(sig),
        "docstring": inspect.getdoc(method),
        "parameters": params,
        "return_type": str(sig.return_annotation) if sig.return_annotation != inspect.Signature.empty else None,
        "is_streaming": is_streaming,
        "call_example": (
            "from jumpstarter.utils.env import env\n\n"
            "with env() as client:\n"
            f"    {method_call}"
        ),
    }


def get_driver_methods(client: Any, driver_path: list[str]) -> dict[str, Any]:
    """Inspect a specific driver client at the given path in the children tree.

    Uses Python inspect to return detailed method information for all public
    methods defined on the concrete driver class.
    """
    target = client
    for key in driver_path:
        children = getattr(target, "children", {})
        if key not in children:
            raise KeyError(f"Driver path component '{key}' not found. Available: {list(children.keys())}")
        target = children[key]

    cls = type(target)
    try:
        members = inspect.getmembers_static(target)
    except Exception:
        logger.debug("inspect.getmembers_static failed for %s", cls.__name__, exc_info=True)
        members = []

    methods = []
    for name, value in members:
        if name.startswith("_") or name in _BASE_METHODS or name in _INTERNAL_METHODS:
            continue
        if isinstance(value, property) or not callable(value):
            continue
        info = _inspect_method(name, value, driver_path)
        if info is not None:
            methods.append(info)

    return {
        "class": f"{cls.__module__}.{cls.__qualname__}",
        "driver_path": driver_path,
        "methods": methods,
    }


def describe_client(client: Any) -> dict[str, Any]:
    """Build a plain-serializable description of a connected driver client tree.

    Returns the flattened driver listing and the Click CLI tree. Drivers whose
    client packages are not installed appear as StubDriverClient entries in the
    listing, and cli_tree is None when the root client does not expose a CLI
    (including when the root client itself is a stub).
    """
    cli_tree = None
    # Inherited counts: QemuFlasherClient, for one, defines no cli of its own and
    # gets a real one from FlasherClientInterface, so looking only at
    # type(client).__dict__ would drop the CLI of every such driver.
    if not isinstance(client, StubDriverClient) and getattr(type(client), "cli", None) is not None:
        try:
            cli_tree = walk_click_tree(client.cli())
        except Exception:
            # A driver whose cli() is broken should cost us its CLI tree, not the
            # whole description — the driver listing below is still worth having.
            logger.warning("could not build the CLI tree for %s", type(client).__name__, exc_info=True)
    return {
        "drivers": list_drivers(client),
        "cli_tree": cli_tree,
    }


@asynccontextmanager
async def _connect_lease(config: ClientConfigV1Alpha1, lease_name: str, portal: BlockingPortal):
    """Attach to an existing lease and yield its root driver client.

    Passing lease_name into lease_async attaches to that lease rather than
    creating one, and leaves it unreleased on exit.
    """
    async with config.lease_async(
        selector=None,
        exporter_name=None,
        lease_name=lease_name,
        # Attaching by name never reaches Lease._create, and with selector None
        # the "selector changed, make a new one" branch cannot fire either, so
        # no duration is ever sent to the controller. Naming 30 minutes here
        # only suggested this call could extend a lease that it cannot.
        duration=timedelta(0),
        portal=portal,
    ) as lease:
        async with lease.serve_unix_async() as path:
            with ExitStack() as stack:
                async with client_from_path(
                    path, portal, stack, allow=lease.allow, unsafe=lease.unsafe
                ) as client:
                    yield client


def describe_drivers(config: ClientConfigV1Alpha1, lease_name: str) -> dict[str, Any]:
    """Attach to an existing lease and describe its driver clients and CLI tree.

    Returns a plain-serializable dict with drivers and cli_tree keys.

    Driver clients are synchronous facades over a blocking portal, so the
    portal has to run its event loop in its own thread and be driven from
    this one — introspection therefore happens outside the loop.

    :raises ValueError: if lease_name is empty
    :raises jumpstarter.client.exceptions.LeaseError: if the lease has ended
        or cannot be acquired
    :raises jumpstarter.common.exceptions.ConnectionError: if the lease does
        not exist or the controller is unreachable
    """
    if not lease_name:
        raise ValueError("lease_name must be a non-empty existing lease name")

    with start_blocking_portal() as portal:
        with portal.wrap_async_context_manager(_connect_lease(config, lease_name, portal)) as client:
            return describe_client(client)


async def describe_drivers_async(config: ClientConfigV1Alpha1, lease_name: str) -> dict[str, Any]:
    """Async wrapper around describe_drivers, run in a worker thread.

    The driver clients cannot be driven from an event loop thread, so the
    whole attach-and-introspect flow runs off-loop.
    """
    if not lease_name:
        raise ValueError("lease_name must be a non-empty existing lease name")

    return await to_thread.run_sync(describe_drivers, config, lease_name)


# Compatibility for consumers of the initial introspection-library branch.
# These describe driver clients, not a physical device inventory.
describe_devices_async = describe_drivers_async
describe_devices = describe_drivers
