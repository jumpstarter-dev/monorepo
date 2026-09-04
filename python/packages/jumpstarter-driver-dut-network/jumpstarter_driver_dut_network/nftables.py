from __future__ import annotations

import ipaddress
import logging
import re
import subprocess
import textwrap
from typing import TYPE_CHECKING

from ._privilege import sudo_cmd

if TYPE_CHECKING:
    from .driver import FilterConfig, FilterRule

logger = logging.getLogger(__name__)

_IFACE_RE = re.compile(r"^[a-zA-Z0-9._-]{1,15}$")


def _validate_iface(name: str) -> None:
    """Raise if *name* is not a valid Linux interface name."""
    if not _IFACE_RE.match(name):
        raise ValueError(f"Invalid interface name: {name!r}")


def _validate_subnet(subnet: str) -> None:
    """Raise if *subnet* is not a valid CIDR network."""
    ipaddress.ip_network(subnet, strict=False)


def _validate_ip(ip: str) -> None:
    """Raise if *ip* is not a valid IP address."""
    ipaddress.ip_address(ip)


def _run_nft(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Execute an ``nft`` command, using sudo when not root."""
    cmd = sudo_cmd(["nft"] + args)
    logger.debug("Running: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _load_ruleset(ruleset: str) -> None:
    """Feed an nftables ruleset via ``nft -f -`` and raise on failure."""
    logger.debug("Loading nftables ruleset:\n%s", ruleset)
    result = subprocess.run(
        sudo_cmd(["nft", "-f", "-"]),
        input=ruleset,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to load nftables ruleset: {result.stderr}")


def _table_name_for(interface: str) -> str:
    """Derive the nftables table name from the interface name."""
    return f"jumpstarter_{interface}".replace("-", "_")


def _render_filter_rule(
    rule: FilterRule,
    iif: str,
    oif: str,
    addr_field: str,
) -> str:
    """Render a single :class:`FilterRule` as an nftables rule line."""
    parts = [f'        iifname "{iif}" oifname "{oif}"']
    addr_value = getattr(rule, addr_field, None)
    if addr_value is not None:
        prefix = "ip daddr" if addr_field == "destination" else "ip saddr"
        parts.append(f"{prefix} {addr_value}")
    if rule.protocol is not None:
        parts.append(rule.protocol)
    if rule.port is not None:
        parts.append(f"dport {rule.port}")
    parts.append(rule.action)
    return " ".join(parts)


def _normalize_upstreams(upstream: str | list[str]) -> list[str]:
    """Return a de-duplicated list of upstream/NAT interface names."""
    if isinstance(upstream, str):
        return [upstream]
    seen: list[str] = []
    for name in upstream:
        if name not in seen:
            seen.append(name)
    return seen


def _build_forward_chain(
    interface: str,
    upstream: str | list[str],
    filter_config: FilterConfig | None = None,
    extra_forward_rules: list[str] | None = None,
) -> str:
    """Build nftables forward chain rules with optional traffic filtering.

    When *filter_config* is ``None`` the generated chain is identical
    to the legacy (pre-filter) behaviour so that existing setups are not
    affected.

    *upstream* may be a single interface name or a list of NAT interfaces
    (untagged upstream plus any VLAN sub-interfaces).  A single string
    produces the same rules as before.

    *extra_forward_rules* are additional pre-formatted nftables rule lines
    (e.g. per-mapping DNAT accept rules for 1:1 NAT).  In legacy mode they
    are appended after the conntrack line; in filtered mode they are inserted
    just before the ingress catch-all.

    When a filter configuration is provided the chain layout is::

        ct state related,established accept   # conntrack first
        <egress rules>                        # DUT -> upstream
        <egress catch-all>                    # egress policy
        <extra_forward_rules>                 # e.g. 1:1 NAT per-mapping accepts
        <ingress rules>                       # upstream -> DUT (new conns)
        <ingress catch-all>                   # ingress policy

    The chain *policy* is always ``accept`` so that forwarded traffic
    unrelated to this interface pair is not disturbed.
    """
    extras = extra_forward_rules or []
    upstreams = _normalize_upstreams(upstream)
    body = (
        _legacy_forward_body(interface, upstreams, extras)
        if filter_config is None
        else _filtered_forward_body(interface, upstreams, filter_config, extras)
    )
    lines = [
        "    chain forward {",
        "        type filter hook forward priority filter; policy accept;",
        *body,
        "    }",
    ]
    return "\n".join(lines)


def _legacy_forward_body(
    interface: str,
    upstreams: list[str],
    extras: list[str],
) -> list[str]:
    """Build forward-chain rules in legacy (no filter) mode."""
    lines: list[str] = []
    for up in upstreams:
        lines.append(f'        iifname "{interface}" oifname "{up}" accept')
        lines.append(
            f'        iifname "{up}" oifname "{interface}" ct state related,established accept'
        )
    lines.extend(extras)
    return lines


def _filtered_forward_body(
    interface: str,
    upstreams: list[str],
    filter_config: FilterConfig,
    extras: list[str],
) -> list[str]:
    """Build forward-chain rules when a traffic filter is configured."""
    lines: list[str] = ["        ct state related,established accept"]
    egress = filter_config.egress
    ingress = filter_config.ingress

    if egress:
        for up in upstreams:
            for rule in egress.rules:
                lines.append(_render_filter_rule(rule, interface, up, "destination"))

    egress_policy = egress.policy if egress else "accept"
    for up in upstreams:
        lines.append(f'        iifname "{interface}" oifname "{up}" {egress_policy}')

    lines.extend(extras)

    if ingress:
        for up in upstreams:
            for rule in ingress.rules:
                lines.append(_render_filter_rule(rule, up, interface, "source"))

    ingress_policy = ingress.policy if ingress else "accept"
    for up in upstreams:
        lines.append(f'        iifname "{up}" oifname "{interface}" {ingress_policy}')
    return lines


def apply_masquerade_rules(
    interface: str,
    upstream: str,
    subnet: str,
    table_name: str | None = None,
    filter_config: FilterConfig | None = None,
    nat_interfaces: list[str] | None = None,
) -> None:
    """Apply nftables masquerade NAT and forwarding rules.

    *nat_interfaces* overrides *upstream* as the list of outbound
    interfaces for masquerade (e.g. VLAN sub-interfaces).
    """
    _validate_iface(interface)
    _validate_iface(upstream)
    _validate_subnet(subnet)
    outbound = _normalize_upstreams(nat_interfaces if nat_interfaces else upstream)
    for oif in outbound:
        _validate_iface(oif)
    table = table_name or _table_name_for(interface)
    logger.info(
        "Applying masquerade rules: interface=%s upstream=%s outbound=%s subnet=%s table=%s",
        interface,
        upstream,
        outbound,
        subnet,
        table,
    )
    forward_chain = _build_forward_chain(interface, outbound, filter_config)
    masq_lines = "\n".join(
        f'        oifname "{oif}" ip saddr {subnet} masquerade' for oif in outbound
    )
    ruleset = (
        f"table ip {table} {{\n"
        f"    chain postrouting {{\n"
        f"        type nat hook postrouting priority srcnat; policy accept;\n"
        f"{masq_lines}\n"
        f"    }}\n"
        f"{forward_chain}\n"
        f"}}\n"
    )
    flush_rules(table)
    _load_ruleset(ruleset)


def apply_1to1_rules(
    interface: str,
    upstream: str,
    mappings: list[dict[str, str]],
    subnet: str,
    table_name: str | None = None,
    filter_config: FilterConfig | None = None,
) -> None:
    """Apply nftables 1:1 NAT (DNAT/SNAT) and forwarding rules."""
    _validate_iface(interface)
    _validate_iface(upstream)
    _validate_subnet(subnet)
    for m in mappings:
        _validate_ip(m["private_ip"])
        _validate_ip(m["public_ip"])
    table = table_name or _table_name_for(interface)
    logger.info(
        "Applying 1:1 NAT rules: interface=%s upstream=%s mappings=%d subnet=%s table=%s",
        interface,
        upstream,
        len(mappings),
        subnet,
        table,
    )

    prerouting_rules = []
    postrouting_rules = []
    extra_forward_rules = []
    output_rules = []
    outbound: list[str] = []

    for m in mappings:
        private_ip = m["private_ip"]
        public_ip = m["public_ip"]
        nat_if = m.get("nat_interface") or upstream
        _validate_iface(nat_if)
        if nat_if not in outbound:
            outbound.append(nat_if)
        prerouting_rules.append(f'        iifname "{nat_if}" ip daddr {public_ip} dnat to {private_ip}')
        postrouting_rules.append(f'        ip saddr {private_ip} oifname "{nat_if}" snat to {public_ip}')
        extra_forward_rules.append(f'        iifname "{nat_if}" oifname "{interface}" ip daddr {private_ip} accept')
        output_rules.append(f"        ip daddr {public_ip} dnat to {private_ip}")

    # Keep the original upstream in the forward/masquerade set so unmapped
    # DUTs (no public_ip) still egress via the untagged interface.
    if upstream not in outbound:
        outbound.append(upstream)

    prerouting_block = "\n".join(prerouting_rules)
    postrouting_block = "\n".join(postrouting_rules)
    output_block = "\n".join(output_rules)
    masq_lines = "\n".join(
        f'        oifname "{oif}" ip saddr {subnet} masquerade' for oif in outbound
    )

    forward_chain = _build_forward_chain(interface, outbound, filter_config, extra_forward_rules)

    ruleset = (
        f"table ip {table} {{\n"
        f"    chain prerouting {{\n"
        f"        type nat hook prerouting priority dstnat; policy accept;\n"
        f"{prerouting_block}\n"
        f"    }}\n"
        f"    chain output {{\n"
        f"        type nat hook output priority dstnat; policy accept;\n"
        f"{output_block}\n"
        f"    }}\n"
        f"    chain postrouting {{\n"
        f"        type nat hook postrouting priority srcnat; policy accept;\n"
        f"{postrouting_block}\n"
        f"{masq_lines}\n"
        f"    }}\n"
        f"{forward_chain}\n"
        f"}}\n"
    )

    flush_rules(table)
    _load_ruleset(ruleset)


def is_filter_forward_drop() -> bool:
    """Check if the nftables ``ip filter`` table has a FORWARD chain with policy drop.

    Docker (via iptables-nft) creates this to isolate container networks.
    Returns False when the table or chain does not exist.
    """
    result = _run_nft(["list", "chain", "ip", "filter", "FORWARD"], check=False)
    if result.returncode != 0:
        return False
    return "policy drop" in result.stdout


def ensure_filter_forward(
    interface: str,
    upstream: str,
    extra_interfaces: list[str] | None = None,
) -> list[int]:
    """Insert nft ACCEPT rules into ``ip filter FORWARD`` if its policy is drop.

    Returns a list of rule handles so they can be removed on cleanup.
    *extra_interfaces* are additional names (e.g. VLAN sub-interfaces)
    that also need accept rules when Docker has set FORWARD policy drop.
    """
    if not is_filter_forward_drop():
        return []

    ifaces: list[str] = []
    for name in (interface, upstream, *(extra_interfaces or [])):
        if name and name not in ifaces:
            ifaces.append(name)

    handles: list[int] = []
    for iface in ifaces:
        for direction in ("iifname", "oifname"):
            result = _run_nft(
                ["-e", "-a", "insert", "rule", "ip", "filter", "FORWARD",
                 direction, iface, "accept"],
                check=False,
            )
            if result.returncode == 0:
                match = re.search(r"# handle (\d+)", result.stdout)
                if match:
                    handles.append(int(match.group(1)))

    if handles:
        logger.info(
            "Inserted %d nft rules into ip filter FORWARD for %s and %s "
            "(policy was drop, likely set by Docker)",
            len(handles), interface, upstream,
        )
    return handles


def remove_filter_forward(handles: list[int]) -> None:
    """Remove nft rules previously inserted by ensure_filter_forward."""
    for handle in handles:
        _run_nft(
            ["delete", "rule", "ip", "filter", "FORWARD", "handle", str(handle)],
            check=False,
        )
    if handles:
        logger.info("Removed %d nft rules from ip filter FORWARD", len(handles))


def apply_ntp_redirect(interface: str, gateway_ip: str, table_name: str) -> None:
    """Redirect all NTP traffic (UDP 123) on *interface* to *gateway_ip*.

    Adds a DNAT rule in a dedicated prerouting chain so that any NTP
    client request arriving on the DUT-facing interface is redirected
    to the local NTP server listening on the gateway address.
    """
    _validate_iface(interface)
    _validate_ip(gateway_ip)
    ntp_table = f"{table_name}_ntp"
    logger.info(
        "Applying NTP redirect: interface=%s gateway=%s table=%s",
        interface, gateway_ip, ntp_table,
    )
    ruleset = textwrap.dedent(f"""\
        table ip {ntp_table} {{
            chain prerouting {{
                type nat hook prerouting priority dstnat; policy accept;
                iifname "{interface}" udp dport 123 dnat to {gateway_ip}:123
            }}
        }}
    """)
    flush_rules(ntp_table)
    _load_ruleset(ruleset)


def remove_ntp_redirect(table_name: str) -> None:
    """Remove the NTP redirect rules created by :func:`apply_ntp_redirect`."""
    ntp_table = f"{table_name}_ntp"
    logger.info("Removing NTP redirect table %s", ntp_table)
    flush_rules(ntp_table)


def flush_rules(table_name: str = "jumpstarter") -> None:
    """Delete the named nftables table (silently ignored if absent)."""
    logger.info("Flushing nftables table %s", table_name)
    _run_nft(["delete", "table", "ip", table_name], check=False)


def list_rules(table_name: str = "jumpstarter") -> str:
    """Return the nftables rules for *table_name* as text (empty if absent)."""
    result = _run_nft(["list", "table", "ip", table_name], check=False)
    if result.returncode != 0:
        return ""
    return result.stdout
