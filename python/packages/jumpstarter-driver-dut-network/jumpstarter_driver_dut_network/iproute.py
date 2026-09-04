"""Interface management via iproute2 commands with NetworkManager awareness."""

import logging
import shutil
import subprocess

from ._privilege import sudo_cmd

logger = logging.getLogger(__name__)

# Linux IFNAMSIZ is 16 including the terminating NUL.
_IFNAMSIZ = 15

# ip rule / ip route table IDs reserved by the kernel.
_RESERVED_ROUTE_TABLES = frozenset({0, 253, 254, 255})


def vlan_subinterface_name(parent: str, vlan_id: int) -> str:
    """Return the conventional ``<parent>.<vlan_id>`` sub-interface name."""
    return f"{parent}.{vlan_id}"


def _sysctl_conf_key(iface: str, setting: str) -> str:
    """Build a ``net.ipv4.conf.<iface>.<setting>`` sysctl key.

    Sysctl uses ``.`` as a path separator, so dots in the interface name
    (e.g. VLAN sub-interfaces like ``end0.905``) must be written as
    slashes: ``net.ipv4.conf.end0/905.forwarding``.
    """
    return f"net.ipv4.conf.{iface.replace('.', '/')}.{setting}"


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a read-only command (no sudo)."""
    logger.debug("Running: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _run_priv(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a privileged command, using sudo when not root."""
    cmd = sudo_cmd(cmd)
    logger.debug("Running: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def is_nm_running() -> bool:
    """Check if NetworkManager is active on this system."""
    if not shutil.which("nmcli"):
        return False
    result = _run(["nmcli", "general", "status"], check=False)
    return result.returncode == 0


def nm_set_unmanaged(interface: str) -> None:
    """Tell NetworkManager to stop managing an interface."""
    if is_nm_running():
        logger.info("Setting interface %s as unmanaged by NetworkManager", interface)
        _run_priv(["nmcli", "device", "set", interface, "managed", "no"], check=False)


def nm_set_managed(interface: str) -> None:
    """Restore NetworkManager management of an interface."""
    if is_nm_running():
        logger.info("Restoring NetworkManager management of interface %s", interface)
        _run_priv(["nmcli", "device", "set", interface, "managed", "yes"], check=False)


def configure_interface(interface: str, gateway_ip: str, prefix_len: int) -> None:
    """Flush existing addresses, assign the gateway IP, and bring the interface up."""
    logger.info("Configuring %s with IP %s/%d", interface, gateway_ip, prefix_len)
    _run_priv(["ip", "addr", "flush", "dev", interface])
    _run_priv(["ip", "addr", "add", f"{gateway_ip}/{prefix_len}", "dev", interface])
    _run_priv(["ip", "link", "set", interface, "up"])


def deconfigure_interface(interface: str) -> None:
    """Flush addresses and bring the interface down."""
    logger.info("Deconfiguring interface %s", interface)
    _run_priv(["ip", "addr", "flush", "dev", interface], check=False)
    _run_priv(["ip", "link", "set", interface, "down"], check=False)


def add_ip_alias(interface: str, ip: str, prefix_len: int) -> None:
    """Add a secondary IP address to an interface.

    This operation is idempotent: if the address is already present on the
    interface, the call is silently skipped.
    """
    addr = f"{ip}/{prefix_len}"
    if addr in get_interface_addresses(interface):
        logger.info("IP alias %s already present on %s, skipping", addr, interface)
        return
    logger.info("Adding IP alias %s to %s", addr, interface)
    _run_priv(["ip", "addr", "add", addr, "dev", interface])


def remove_ip_alias(interface: str, ip: str, prefix_len: int) -> None:
    """Remove a secondary IP address from an interface."""
    logger.info("Removing IP alias %s/%d from %s", ip, prefix_len, interface)
    _run_priv(["ip", "addr", "del", f"{ip}/{prefix_len}", "dev", interface], check=False)


def get_interface_forwarding(iface: str) -> str:
    """Return the current per-interface forwarding value ("0" or "1")."""
    result = _run(["sysctl", "-n", _sysctl_conf_key(iface, "forwarding")], check=False)
    return result.stdout.strip() or "0"


def set_interface_forwarding(iface: str, enabled: bool) -> None:
    """Enable or disable IPv4 forwarding on a specific interface.

    Uses the per-interface sysctl (net.ipv4.conf.<iface>.forwarding) rather
    than the global net.ipv4.ip_forward to avoid turning the host into a
    full router on all interfaces.  Dots in *iface* are translated to
    slashes so VLAN names such as ``end0.905`` map to
    ``net.ipv4.conf.end0/905.forwarding``.
    """
    value = "1" if enabled else "0"
    key = _sysctl_conf_key(iface, "forwarding")
    logger.info("Setting %s=%s", key, value)
    _run_priv(["sysctl", "-w", f"{key}={value}"])


def get_interface_rp_filter(iface: str) -> str:
    """Return the current per-interface rp_filter value."""
    result = _run(["sysctl", "-n", _sysctl_conf_key(iface, "rp_filter")], check=False)
    return result.stdout.strip() or "0"


def set_interface_rp_filter(iface: str, value: int) -> None:
    """Set reverse-path filtering mode on a specific interface.

    Common values: ``0`` (disabled), ``1`` (strict), ``2`` (loose).
    Dots in *iface* are translated to slashes for the sysctl key.
    """
    key = _sysctl_conf_key(iface, "rp_filter")
    logger.info("Setting %s=%s", key, value)
    _run_priv(["sysctl", "-w", f"{key}={value}"])


def create_vlan_interface(parent: str, vlan_id: int) -> str:
    """Create (if needed) and bring up a VLAN sub-interface on *parent*.

    Returns the sub-interface name (``<parent>.<vlan_id>``).  The
    operation is idempotent: an existing sub-interface is left in place
    and brought up.
    """
    name = vlan_subinterface_name(parent, vlan_id)
    if len(name) > _IFNAMSIZ:
        raise ValueError(
            f"VLAN interface name {name!r} exceeds the Linux {_IFNAMSIZ}-character limit"
        )
    if not interface_exists(name):
        logger.info("Creating VLAN interface %s on %s id %d", name, parent, vlan_id)
        _run_priv(
            ["ip", "link", "add", "link", parent, "name", name, "type", "vlan", "id", str(vlan_id)]
        )
    else:
        logger.info("VLAN interface %s already exists", name)
    nm_set_unmanaged(name)
    _run_priv(["ip", "link", "set", name, "up"])
    return name


def delete_vlan_interface(name: str) -> None:
    """Delete a VLAN sub-interface.  Missing interfaces are ignored."""
    logger.info("Deleting VLAN interface %s", name)
    _run_priv(["ip", "link", "del", name], check=False)


def add_policy_route(gateway: str, device: str, table: int) -> None:
    """Install a default route in a custom routing table for PBR.

    *table* is the numeric routing-table ID (VLAN ID, or the DUT
    private IPv4 address as an integer for untagged PBR).

    Raises :class:`RuntimeError` if the ``ip route add`` command fails
    (unless the route already exists, which is treated as idempotent).
    """
    if table in _RESERVED_ROUTE_TABLES:
        raise ValueError(
            f"Routing table {table} is reserved by the kernel (local/main/default)"
        )
    logger.info("Adding policy route default via %s dev %s table %d", gateway, device, table)
    # ``replace`` is idempotent: if an identical route exists it is a no-op,
    # and if a *different* default exists it is updated to the requested
    # gateway/device.  ``add`` would return "File exists" in both cases,
    # making it impossible to distinguish a harmless duplicate from a
    # conflicting stale route.
    result = _run_priv(
        ["ip", "route", "replace", "default", "via", gateway, "dev", device,
         "table", str(table), "onlink"],
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(
            f"Failed to add policy route (table {table}): {stderr}"
        )


def add_ip_rule(from_ip: str, table: int, priority: int = 100) -> None:
    """Add a policy routing rule sending traffic from *from_ip* to *table*.

    Raises :class:`RuntimeError` if the ``ip rule add`` command fails
    (unless an identical rule already exists, which is treated as idempotent).
    """
    logger.info("Adding ip rule from %s table %d priority %d", from_ip, table, priority)
    result = _run_priv(
        ["ip", "rule", "add", "from", from_ip, "table", str(table), "priority", str(priority)],
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "File exists" in stderr:
            logger.info("IP rule from %s table %d already exists, skipping", from_ip, table)
        else:
            raise RuntimeError(
                f"Failed to add ip rule (from {from_ip} table {table}): {stderr}"
            )


def delete_ip_rule(from_ip: str, table: int) -> None:
    """Delete a policy routing rule previously added by :func:`add_ip_rule`."""
    logger.info("Deleting ip rule from %s table %d", from_ip, table)
    _run_priv(["ip", "rule", "del", "from", from_ip, "table", str(table)], check=False)


def flush_routing_table(table: int) -> None:
    """Flush all routes from a custom routing table."""
    logger.info("Flushing routing table %d", table)
    _run_priv(["ip", "route", "flush", "table", str(table)], check=False)


def detect_upstream_interface() -> str | None:
    """Detect the default upstream interface by parsing the default route."""
    result = _run(["ip", "route", "show", "default"], check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    parts = result.stdout.strip().split()
    try:
        dev_idx = parts.index("dev")
        return parts[dev_idx + 1]
    except (ValueError, IndexError):
        return None


def interface_exists(name: str) -> bool:
    """Check whether a network interface exists."""
    result = _run(["ip", "link", "show", name], check=False)
    return result.returncode == 0


def get_interface_addresses(name: str) -> list[str]:
    """Get IP addresses assigned to an interface."""
    result = _run(["ip", "-o", "-4", "addr", "show", "dev", name], check=False)
    if result.returncode != 0:
        return []
    addrs = []
    for line in result.stdout.strip().splitlines():
        parts = line.split()
        for i, part in enumerate(parts):
            if part == "inet" and i + 1 < len(parts):
                addrs.append(parts[i + 1])
                break
    return addrs


def get_interface_prefix_len(name: str) -> int | None:
    """Return the prefix length of the first IPv4 address on an interface."""
    addrs = get_interface_addresses(name)
    if not addrs:
        return None
    try:
        return int(addrs[0].split("/")[1])
    except (IndexError, ValueError):
        return None
