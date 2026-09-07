"""Backward-compatible re-exports of the driver introspection utilities.

The implementation lives in jumpstarter.client.introspect so it can be shared
between the CLI and the MCP server.
"""

from jumpstarter.client.introspect import (
    _BASE_METHODS,
    _INTERNAL_METHODS,
    _get_public_method_names,
    _inspect_method,
    get_driver_methods,
    list_drivers,
    walk_click_tree,
)

__all__ = [
    "_BASE_METHODS",
    "_INTERNAL_METHODS",
    "_get_public_method_names",
    "_inspect_method",
    "get_driver_methods",
    "list_drivers",
    "walk_click_tree",
]
