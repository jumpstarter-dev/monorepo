import asyncio
import asyncio.subprocess
import ipaddress
import shutil
import socket
import subprocess
import sys
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypedDict

from . import dnsmasq, iproute, nftables
from .ntp_server import NtpServer
from jumpstarter.driver import Driver, export


class InterfaceStatus(TypedDict):
    name: str
    exists: bool
    addresses: list[str]


class NetworkStatus(TypedDict):
    interface_status: InterfaceStatus
    interface: str
    upstream: str | None
    subnet: str
    nat_mode: str
    dhcp_enabled: bool
    leases: list[dict]
    dns_entries: list[dict[str, str]]
    nat_rules: str


_VALID_ACTIONS = ("accept", "drop")
_VALID_POLICIES = ("accept", "drop")
_VALID_PROTOCOLS = ("tcp", "udp")


@dataclass
class FilterRule:
    """A single filter rule for traffic matching."""

    action: str
    destination: str | None = None
    source: str | None = None
    port: int | None = None
    protocol: str | None = None

    def __post_init__(self) -> None:
        if self.action not in _VALID_ACTIONS:
            raise ValueError(
                f"action must be one of {_VALID_ACTIONS}, got {self.action!r}"
            )
        if self.destination is not None:
            try:
                ipaddress.ip_network(self.destination, strict=False)
            except ValueError as exc:
                raise ValueError(
                    f"destination is not a valid network address: {self.destination!r}"
                ) from exc
        if self.source is not None:
            try:
                ipaddress.ip_network(self.source, strict=False)
            except ValueError as exc:
                raise ValueError(
                    f"source is not a valid network address: {self.source!r}"
                ) from exc
        if self.port is not None and self.protocol is None:
            raise ValueError("port requires protocol to be set")
        if self.protocol is not None and self.protocol not in _VALID_PROTOCOLS:
            raise ValueError(
                f"protocol must be one of {_VALID_PROTOCOLS}, got {self.protocol!r}"
            )


@dataclass
class FilterDirection:
    """Egress or ingress filter configuration."""

    policy: str = "accept"
    rules: list[FilterRule] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.policy not in _VALID_POLICIES:
            raise ValueError(
                f"policy must be one of {_VALID_POLICIES}, got {self.policy!r}"
            )
        # Convert raw dicts to FilterRule instances (for YAML deserialization).
        self.rules = [
            FilterRule(**r) if isinstance(r, dict) else r for r in self.rules  # ty: ignore[missing-argument]
        ]


@dataclass
class FilterConfig:
    """Top-level traffic filter configuration."""

    egress: FilterDirection | None = None
    ingress: FilterDirection | None = None

    def __post_init__(self) -> None:
        # Convert raw dicts to FilterDirection instances (for YAML deserialization).
        if isinstance(self.egress, dict):
            self.egress = FilterDirection(**self.egress)
        if isinstance(self.ingress, dict):
            self.ingress = FilterDirection(**self.ingress)

    @classmethod
    def from_dict(cls, data: dict) -> "FilterConfig":
        """Create a FilterConfig from a plain dictionary."""
        return cls(**{k: v for k, v in data.items() if k in ("egress", "ingress")})


_VLAN_ID_MIN = 1
_VLAN_ID_MAX = 4094
_RESERVED_ROUTE_TABLES = frozenset({0, 253, 254, 255})
_PBR_PRIORITY = 100
_ADDRESS_FIELDS = frozenset({"ip", "mac", "hostname", "public_ip", "vlan_id", "public_gateway"})


@dataclass
class AddressEntry:
    """A single DUT address mapping (DHCP lease and/or NAT/VLAN config)."""

    ip: str
    mac: str | None = None
    hostname: str = ""
    public_ip: str | None = None
    vlan_id: int | None = None
    public_gateway: str | None = None

    def __post_init__(self) -> None:
        if self.vlan_id is not None:
            try:
                self.vlan_id = int(self.vlan_id)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"vlan_id must be an integer, got {self.vlan_id!r}") from exc
            if not _VLAN_ID_MIN <= self.vlan_id <= _VLAN_ID_MAX:
                raise ValueError(
                    f"vlan_id must be between {_VLAN_ID_MIN} and {_VLAN_ID_MAX}, got {self.vlan_id}"
                )
        if self.public_gateway is not None:
            try:
                ipaddress.ip_address(self.public_gateway)
            except ValueError as exc:
                raise ValueError(
                    f"public_gateway is not a valid IP address: {self.public_gateway!r}"
                ) from exc
            if self.vlan_id is None:
                try:
                    table = int(ipaddress.IPv4Address(self.ip))
                except ValueError as exc:
                    raise ValueError(
                        f"public_gateway without vlan_id requires a valid IPv4 "
                        f"address in ip, got {self.ip!r}"
                    ) from exc
                if table in _RESERVED_ROUTE_TABLES:
                    raise ValueError(
                        f"DUT IP {self.ip!r} maps to routing table {table} which "
                        "is reserved by the kernel (tables 0, 253, 254, 255)"
                    )
            elif self.vlan_id in _RESERVED_ROUTE_TABLES:
                raise ValueError(
                    f"vlan_id {self.vlan_id} cannot be used as a routing table ID "
                    "(tables 0, 253, 254, 255 are reserved by the kernel)"
                )

    @classmethod
    def from_dict(cls, data: "AddressEntry | dict[str, Any]") -> "AddressEntry":
        """Create an AddressEntry from a YAML/config dictionary."""
        if isinstance(data, AddressEntry):
            return data
        return cls(**{k: v for k, v in data.items() if k in _ADDRESS_FIELDS})

    def to_dict(self) -> dict[str, Any]:
        """Return a dict suitable for dnsmasq config and serialization."""
        result: dict[str, Any] = {"ip": self.ip}
        if self.mac:
            result["mac"] = self.mac
        if self.hostname:
            result["hostname"] = self.hostname
        if self.public_ip:
            result["public_ip"] = self.public_ip
        if self.vlan_id is not None:
            result["vlan_id"] = self.vlan_id
        if self.public_gateway:
            result["public_gateway"] = self.public_gateway
        return result


@dataclass(kw_only=True)
class DutNetwork(Driver):
    """DUT network isolation with bridge, DHCP, DNS, and NAT."""

    driver_type = "network"

    interface: str
    subnet: str = "192.168.100.0/24"
    gateway_ip: str = "192.168.100.1"
    upstream_interface: str | None = None

    dhcp_enabled: bool = True
    dhcp_range_start: str = "192.168.100.100"
    dhcp_range_end: str = "192.168.100.200"
    addresses: list[AddressEntry] = field(default_factory=list)
    dns_servers: list[str] = field(default_factory=lambda: ["8.8.8.8", "8.8.4.4"])

    dns_entries: list[dict[str, str]] = field(default_factory=list)

    local_ntp: bool = False

    enable_tcpdump: bool = False

    filter: FilterConfig | None = None

    state_dir: str | None = None
    nat_mode: Literal["masquerade", "1to1", "disabled", "none"] = "masquerade"
    public_interface: str | None = None

    _dnsmasq_process: subprocess.Popen | None = field(init=False, default=None)
    _state_path: Path | None = field(init=False, default=None)
    _upstream: str | None = field(init=False, default=None)
    _prefix_len: int = field(init=False, default=0)
    _table_name: str = field(init=False, default="jumpstarter")
    _prev_fwd_iface: str = field(init=False, default="0")
    _prev_fwd_upstream: str = field(init=False, default="0")
    _upstream_prefix_len: int = field(init=False, default=24)
    _added_aliases: set[str] = field(init=False, default_factory=set)
    _alias_ifaces: dict[str, str] = field(init=False, default_factory=dict)
    _created_vlans: set[str] = field(init=False, default_factory=set)
    _pbr_rules: list[tuple[str, int]] = field(init=False, default_factory=list)
    _pbr_tables: set[int] = field(init=False, default_factory=set)
    _fwd_rule_handles: list[int] = field(init=False, default_factory=list)
    _ntp_server: NtpServer | None = field(init=False, default=None)
    _tcpdump_process: asyncio.subprocess.Process | None = field(init=False, default=None)

    @classmethod
    def client(cls) -> str:
        return "jumpstarter_driver_dut_network.client.DutNetworkClient"

    def __post_init__(self):
        """Initialise the driver: validate config, set up the network stack."""
        if hasattr(super(), "__post_init__"):
            super().__post_init__()
        self._table_name = nftables._table_name_for(self.interface)
        self._check_system_requirements()
        self._validate_config()
        try:
            self._setup_network()
        except Exception:
            self.cleanup()
            raise

    def _check_system_requirements(self) -> None:
        """Verify that required Linux tools (ip, nft, dnsmasq, …) are installed."""
        if sys.platform != "linux":
            raise RuntimeError("DutNetwork driver requires Linux (network namespaces, nftables)")

        missing = []
        if not shutil.which("ip"):
            missing.append("ip (iproute2)")
        if not shutil.which("nft") and not self._nat_disabled():
            missing.append("nft (nftables)")
        if not shutil.which("dnsmasq") and self.dhcp_enabled:
            missing.append("dnsmasq")
        if not shutil.which("sysctl") and not self._nat_disabled():
            missing.append("sysctl")
        if not shutil.which("tcpdump") and self.enable_tcpdump:
            missing.append("tcpdump")

        if missing:
            raise RuntimeError(
                f"DutNetwork driver requires the following tools: {', '.join(missing)}. "
                "Install them with: apt-get install -y iproute2 nftables dnsmasq-base"
            )

    def _nat_disabled(self) -> bool:
        """Return True when NAT is explicitly disabled."""
        return self.nat_mode in ("disabled", "none")

    @staticmethod
    def _resolve_ip(value: str) -> str:
        """Resolve *value* to an IPv4 address string.

        If *value* is already a valid IP address it is returned unchanged.
        Otherwise it is treated as a DNS hostname and resolved via
        :func:`socket.getaddrinfo`.  Only the first IPv4 result is used.

        Raises :class:`ValueError` when the hostname cannot be resolved.
        """
        try:
            ipaddress.ip_address(value)
            return value
        except ValueError:
            pass

        try:
            results = socket.getaddrinfo(value, None, socket.AF_INET, socket.SOCK_STREAM)
        except socket.gaierror:
            results = []

        if not results:
            raise ValueError(
                f"Cannot resolve hostname '{value}' to an IPv4 address. "
                "Provide a valid IP address or a resolvable DNS name."
            )

        _family, _type, _proto, _canonname, sockaddr = results[0]
        address, _port = sockaddr
        return address

    def _validate_config(self) -> None:
        """Parse and validate the driver configuration fields."""
        network = ipaddress.ip_network(self.subnet, strict=False)
        self._prefix_len = network.prefixlen

        gateway = ipaddress.ip_address(self.gateway_ip)
        if gateway not in network:
            raise ValueError(f"Gateway {self.gateway_ip} is not within subnet {self.subnet}")

        # Convert raw dicts to AddressEntry instances (YAML deserialization produces dicts).
        self.addresses = [AddressEntry.from_dict(e) for e in self.addresses]

        if self.nat_mode == "1to1":
            has_public = any(entry.public_ip for entry in self.addresses)
            if not has_public:
                raise ValueError("At least one address entry must have public_ip for 1:1 NAT mode")

        # Convert raw dict to FilterConfig (YAML deserialization produces dicts).
        if isinstance(self.filter, dict):
            if self.filter:
                self.filter = FilterConfig.from_dict(self.filter)
            else:
                self.filter = None

    def _setup_network(self) -> None:
        """Configure interface, forwarding, VLAN/PBR, DHCP, NAT, and NTP."""
        if not self._nat_disabled():
            self._upstream = self.upstream_interface or iproute.detect_upstream_interface()
            if not self._upstream:
                raise RuntimeError("Cannot detect upstream interface and none was configured")
            upstream_for_alias = self.public_interface or self._upstream
            detected = iproute.get_interface_prefix_len(upstream_for_alias)
            if detected is not None:
                self._upstream_prefix_len = detected
        else:
            self._upstream = self.upstream_interface

        self._state_path = Path(self.state_dir) if self.state_dir else dnsmasq.state_dir_for_interface(self.interface)
        dnsmasq.ensure_state_dir(self._state_path)

        iproute.nm_set_unmanaged(self.interface)
        iproute.configure_interface(self.interface, self.gateway_ip, self._prefix_len)

        if not self._nat_disabled():
            self._prev_fwd_iface = iproute.get_interface_forwarding(self.interface)
            self._prev_fwd_upstream = iproute.get_interface_forwarding(self._upstream)
            iproute.set_interface_forwarding(self.interface, True)
            iproute.set_interface_forwarding(self._upstream, True)
            self._setup_vlans_and_pbr()
            extra_ifaces = list(self._created_vlans) or None
            if extra_ifaces:
                self._fwd_rule_handles = nftables.ensure_filter_forward(
                    self.interface, self._upstream, extra_interfaces=extra_ifaces,
                )
            else:
                self._fwd_rule_handles = nftables.ensure_filter_forward(self.interface, self._upstream)

        if self.dhcp_enabled:
            dnsmasq.write_config(
                state_dir=self._state_path,
                interface=self.interface,
                range_start=self.dhcp_range_start,
                range_end=self.dhcp_range_end,
                static_leases=[e.to_dict() for e in self.addresses if e.mac],
                dns_servers=self.dns_servers,
                gateway_ip=self.gateway_ip,
                dns_entries=self.dns_entries,
            )
            self._dnsmasq_process = dnsmasq.start(self._state_path)

        if self.nat_mode == "masquerade":
            self._apply_masquerade_rules()
        elif self.nat_mode == "1to1":
            self._apply_1to1_aliases_and_rules()

        if self.local_ntp:
            self._ntp_server = NtpServer(self.gateway_ip)
            self._ntp_server.start()
            nftables.apply_ntp_redirect(self.interface, self.gateway_ip, self._table_name)

        self.logger.info(
            "DUT network configured: interface=%s subnet=%s nat=%s local_ntp=%s",
            self.interface,
            self.subnet,
            self.nat_mode,
            self.local_ntp,
        )

    def _vlan_parent(self) -> str:
        """Parent interface on which VLAN sub-interfaces are created."""
        return self._upstream or ""

    def _alias_parent(self) -> str:
        """Interface that receives 1:1 IP aliases when no VLAN is set."""
        return self.public_interface or self._upstream or ""

    def _vlan_name(self, vlan_id: int) -> str:
        """Return the ``<parent>.<vlan_id>`` sub-interface name."""
        return f"{self._vlan_parent()}.{vlan_id}"

    def _nat_iface_for(self, entry: AddressEntry) -> str:
        """Return the NAT/upstream interface for a given address entry."""
        if entry.vlan_id is not None:
            return self._vlan_name(entry.vlan_id)
        return self._alias_parent()

    @staticmethod
    def _pbr_table_id(entry: AddressEntry) -> int:
        """Routing-table ID for policy-based routing.

        VLAN mappings use the VLAN ID so tagged traffic shares one table.
        Untagged mappings use the DUT private IPv4 address as a unique
        32-bit table ID (``from <ip> lookup <int(ip)>``).
        """
        if entry.vlan_id is not None:
            return entry.vlan_id
        return int(ipaddress.IPv4Address(entry.ip))

    def _outbound_interfaces(self) -> list[str]:
        """NAT interfaces used for masquerade/forward rules.

        Untagged-only (or empty addresses) returns the original upstream so
        existing behaviour is unchanged.  VLAN-only returns just the VLAN
        sub-interfaces — the untagged upstream is omitted because no DUT
        traffic should egress there.  Mixed configurations include both.
        Runtime ``add_address`` / ``remove_address`` trigger a full rebuild
        via ``_sync_nat``, so the list stays consistent.
        """
        parent = self._upstream or ""
        vlan_ifaces: list[str] = []
        has_untagged = False
        for entry in self.addresses:
            if entry.vlan_id is not None:
                name = self._vlan_name(entry.vlan_id)
                if name not in vlan_ifaces:
                    vlan_ifaces.append(name)
            else:
                has_untagged = True
        if not vlan_ifaces:
            return [parent] if parent else []
        if has_untagged:
            result = [parent] if parent else []
            for name in vlan_ifaces:
                if name not in result:
                    result.append(name)
            return result
        return vlan_ifaces

    def _setup_vlans_and_pbr(self) -> None:
        """Create VLAN sub-interfaces, sysctls, IP aliases, and PBR rules."""
        parent = self._vlan_parent()
        if not parent:
            return
        for entry in self.addresses:
            nat_if = self._nat_iface_for(entry)
            if entry.vlan_id is not None:
                name = nat_if
                iproute.create_vlan_interface(parent, entry.vlan_id)
                self._created_vlans.add(name)
                iproute.set_interface_forwarding(name, True)
                iproute.set_interface_rp_filter(name, 2)
                nat_if = name
                if entry.public_ip:
                    resolved = self._resolve_ip(entry.public_ip)
                    iproute.add_ip_alias(name, resolved, self._upstream_prefix_len)
                    self._added_aliases.add(resolved)
                    self._alias_ifaces[resolved] = name
                if entry.public_gateway is None:
                    self.logger.warning(
                        "Address %s has vlan_id=%s but no public_gateway; "
                        "VLAN interface %s is configured without policy-based "
                        "routing, so DUT traffic may not egress via the VLAN",
                        entry.ip,
                        entry.vlan_id,
                        name,
                    )
            if entry.public_gateway:
                gateway = self._resolve_ip(entry.public_gateway)
                table = self._pbr_table_id(entry)
                iproute.add_policy_route(gateway, nat_if, table)
                try:
                    iproute.add_ip_rule(entry.ip, table, priority=_PBR_PRIORITY)
                except RuntimeError:
                    iproute.flush_routing_table(table)
                    raise
                self._pbr_rules.append((entry.ip, table))
                self._pbr_tables.add(table)

    def _refresh_fwd_rule_handles(self) -> None:
        """Re-insert nft FORWARD ACCEPT rules for any new VLAN sub-interfaces.

        Docker sets the FORWARD chain policy to drop; we need an explicit
        accept for each interface we create.  At init time this is done
        once, but runtime ``add_address`` / ``remove_address`` may add or
        remove VLAN sub-interfaces, so we tear down the old handles and
        re-create them.
        """
        if self._fwd_rule_handles:
            nftables.remove_filter_forward(self._fwd_rule_handles)
            self._fwd_rule_handles = []
        extra_ifaces = list(self._created_vlans) or None
        if extra_ifaces:
            self._fwd_rule_handles = nftables.ensure_filter_forward(
                self.interface, self._upstream, extra_interfaces=extra_ifaces,
            )
        else:
            self._fwd_rule_handles = nftables.ensure_filter_forward(
                self.interface, self._upstream,
            )

    def _teardown_vlans_and_pbr(self) -> None:
        """Reverse VLAN sub-interfaces, PBR rules, and custom routing tables."""
        for from_ip, table in self._pbr_rules:
            iproute.delete_ip_rule(from_ip, table)
        self._pbr_rules.clear()
        for table in list(self._pbr_tables):
            iproute.flush_routing_table(table)
        self._pbr_tables.clear()
        for name in list(self._created_vlans):
            iproute.delete_vlan_interface(name)
        self._created_vlans.clear()

    def _apply_masquerade_rules(self) -> None:
        """Apply nftables masquerade NAT rules for outbound DUT traffic."""
        kwargs: dict[str, Any] = {
            "table_name": self._table_name,
            "filter_config": self.filter,
        }
        outbound = self._outbound_interfaces()
        if outbound != [self._upstream]:
            kwargs["nat_interfaces"] = outbound
        nftables.apply_masquerade_rules(self.interface, self._upstream, self.subnet, **kwargs)

    def _apply_1to1_aliases_and_rules(self) -> None:
        """Add IP aliases and apply nftables 1:1 NAT rules per mapping."""
        mappings = self._get_1to1_mappings()
        parent = self._alias_parent()
        for m in mappings:
            ip = m["public_ip"]
            if ip in self._added_aliases:
                continue
            nat_if = m.get("nat_interface") or parent
            iproute.add_ip_alias(nat_if, ip, self._upstream_prefix_len)
            self._added_aliases.add(ip)
            self._alias_ifaces[ip] = nat_if
        nftables.apply_1to1_rules(
            self.interface, parent, mappings, self.subnet,
            table_name=self._table_name,
            filter_config=self.filter,
        )

    def _get_1to1_mappings(self) -> list[dict[str, str]]:
        """Build the list of private→public IP mappings for 1:1 NAT."""
        mappings: list[dict[str, str]] = []
        parent = self._alias_parent()
        for entry in self.addresses:
            if not entry.public_ip:
                continue
            mapping: dict[str, str] = {
                "private_ip": entry.ip,
                "public_ip": self._resolve_ip(entry.public_ip),
            }
            nat_if = self._nat_iface_for(entry)
            if nat_if != parent:
                mapping["nat_interface"] = nat_if
            mappings.append(mapping)
        return mappings

    def _stop_tcpdump(self) -> None:
        """Terminate any running tcpdump process."""
        if self._tcpdump_process is not None:
            try:
                self._tcpdump_process.terminate()
            except ProcessLookupError:
                pass
            self._tcpdump_process = None

    def _stop_ntp(self) -> None:
        """Stop the local NTP server and remove its redirect rules."""
        if self._ntp_server is not None:
            self._ntp_server.stop()
            self._ntp_server = None
            nftables.remove_ntp_redirect(self._table_name)

    def cleanup(self) -> None:
        """Tear down all network state created by this driver instance."""
        self.logger.info("Cleaning up DUT network configuration")

        self._stop_ntp()
        self._stop_tcpdump()

        if self._dnsmasq_process:
            dnsmasq.stop(process=self._dnsmasq_process, state_dir=self._state_path)
            self._dnsmasq_process = None

        if self._state_path:
            dnsmasq.cleanup_state_dir(self._state_path)

        nftables.flush_rules(self._table_name)

        if self._added_aliases:
            for ip in list(self._added_aliases):
                iface = self._alias_ifaces.get(ip) or self._alias_parent()
                if iface:
                    iproute.remove_ip_alias(iface, ip, self._upstream_prefix_len)
            self._added_aliases.clear()
            self._alias_ifaces.clear()

        self._teardown_vlans_and_pbr()

        if self._fwd_rule_handles:
            nftables.remove_filter_forward(self._fwd_rule_handles)
            self._fwd_rule_handles = []

        if not self._nat_disabled():
            if self._prev_fwd_iface == "0":
                iproute.set_interface_forwarding(self.interface, False)
            if self._prev_fwd_upstream == "0" and self._upstream:
                iproute.set_interface_forwarding(self._upstream, False)

        iproute.deconfigure_interface(self.interface)
        iproute.nm_set_managed(self.interface)

    def close(self):
        """Clean up and close the driver."""
        self.cleanup()
        super().close()

    @export
    def status(self) -> NetworkStatus:
        """Return the current network status (interface, leases, NAT rules)."""
        iface_exists = iproute.interface_exists(self.interface)
        addresses = iproute.get_interface_addresses(self.interface) if iface_exists else []
        leases = self._get_leases_list()
        nat_rules = nftables.list_rules(self._table_name)

        return NetworkStatus(
            interface_status=InterfaceStatus(
                name=self.interface,
                exists=iface_exists,
                addresses=addresses,
            ),
            interface=self.interface,
            upstream=self._upstream,
            subnet=self.subnet,
            nat_mode=self.nat_mode,
            dhcp_enabled=self.dhcp_enabled,
            leases=leases,
            dns_entries=self.dns_entries,
            nat_rules=nat_rules,
        )

    @export
    def ntp_status(self) -> dict:
        """Return the status of the local NTP server."""
        return {
            "enabled": self.local_ntp,
            "running": self._ntp_server is not None and self._ntp_server.running,
        }

    @export
    def get_dut_ip(self, mac: str) -> str | None:
        """Look up the assigned IP for a DUT by its MAC address."""
        if not self._state_path:
            return None
        lease = dnsmasq.get_lease_by_mac(self._state_path, mac)
        return lease.ip if lease else None

    @export
    def get_leases(self) -> list[dict]:
        """Return all current DHCP leases (dynamic + static)."""
        return self._get_leases_list()

    def _get_leases_list(self) -> list[dict]:
        """Parse lease file and return a list of lease dictionaries."""
        if not self._state_path:
            return []
        leases = dnsmasq.parse_leases(self._state_path)
        return [
            {"mac": lease.mac, "ip": lease.ip, "hostname": lease.hostname, "expiry": lease.expiry} for lease in leases
        ]

    @export
    def add_address(
        self,
        ip: str,
        mac: str | None = None,
        hostname: str = "",
        public_ip: str | None = None,
        vlan_id: int | None = None,
        public_gateway: str | None = None,
    ) -> None:
        """Add or replace an address entry, then resync DHCP and NAT."""
        new_entry = AddressEntry(
            ip=ip,
            mac=mac,
            hostname=hostname or "",
            public_ip=self._resolve_ip(public_ip) if public_ip else None,
            vlan_id=vlan_id,
            public_gateway=public_gateway,
        )

        self.addresses = [entry for entry in self.addresses if entry.ip != ip]
        self.addresses.append(new_entry)
        self._reload_dnsmasq_config()
        self._sync_nat()
        self.logger.info("Added address: ip=%s mac=%s hostname=%s", ip, mac, hostname)

    @export
    def remove_address(self, ip: str) -> None:
        """Remove an address entry by IP and resync DHCP and NAT."""
        self.addresses = [entry for entry in self.addresses if entry.ip != ip]
        self._reload_dnsmasq_config()
        self._sync_nat()
        self.logger.info("Removed address for ip=%s", ip)

    def _sync_nat(self) -> None:
        """Re-apply VLAN/PBR/NAT state after a runtime address change."""
        if self._nat_disabled():
            return
        needs_vlan = bool(self._created_vlans) or any(e.vlan_id is not None for e in self.addresses)
        needs_pbr = bool(self._pbr_rules) or any(e.public_gateway is not None for e in self.addresses)
        if needs_vlan or needs_pbr:
            self._teardown_vlans_and_pbr()
            self._setup_vlans_and_pbr()
            self._refresh_fwd_rule_handles()
        if self.nat_mode == "1to1":
            self._sync_1to1_nat()
        elif self.nat_mode == "masquerade" and (needs_vlan or needs_pbr):
            self._apply_masquerade_rules()

    def _sync_1to1_nat(self) -> None:
        """Rebuild 1:1 NAT aliases and nftables rules after address changes."""
        parent = self._alias_parent()
        if not parent:
            return
        mappings = self._get_1to1_mappings()
        wanted: dict[str, str] = {
            m["public_ip"]: m.get("nat_interface") or parent for m in mappings
        }

        for ip in list(self._added_aliases):
            if ip not in wanted:
                iface = self._alias_ifaces.get(ip, parent)
                iproute.remove_ip_alias(iface, ip, self._upstream_prefix_len)
                self._added_aliases.discard(ip)
                self._alias_ifaces.pop(ip, None)
            elif self._alias_ifaces.get(ip) != wanted[ip]:
                old_iface = self._alias_ifaces.get(ip, parent)
                iproute.remove_ip_alias(old_iface, ip, self._upstream_prefix_len)
                iproute.add_ip_alias(wanted[ip], ip, self._upstream_prefix_len)
                self._alias_ifaces[ip] = wanted[ip]

        for ip, iface in wanted.items():
            if ip not in self._added_aliases:
                iproute.add_ip_alias(iface, ip, self._upstream_prefix_len)
                self._added_aliases.add(ip)
                self._alias_ifaces[ip] = iface

        nftables.flush_rules(self._table_name)
        nftables.apply_1to1_rules(
            self.interface, parent, mappings, self.subnet,
            table_name=self._table_name,
            filter_config=self.filter,
        )

    @export
    def get_nat_rules(self) -> str:
        """Return the active nftables rules for this driver's table."""
        return nftables.list_rules(self._table_name)

    @export
    def get_dns_entries(self) -> list[dict[str, str]]:
        """Return the list of configured DNS entries."""
        return list(self.dns_entries)

    @export
    def add_dns_entry(self, hostname: str, ip: str) -> None:
        """Add or replace a custom DNS entry, then reload dnsmasq."""
        self.dns_entries = [e for e in self.dns_entries if e["hostname"] != hostname]
        self.dns_entries.append({"hostname": hostname, "ip": ip})
        self._reload_dnsmasq_config()
        self.logger.info("Added DNS entry: %s -> %s", hostname, ip)

    @export
    def remove_dns_entry(self, hostname: str) -> None:
        """Remove a DNS entry by hostname, then reload dnsmasq."""
        self.dns_entries = [e for e in self.dns_entries if e["hostname"] != hostname]
        self._reload_dnsmasq_config()
        self.logger.info("Removed DNS entry: %s", hostname)

    def _reload_dnsmasq_config(self) -> None:
        """Rewrite the dnsmasq config and signal the daemon to reload."""
        if self._state_path and self.dhcp_enabled:
            dnsmasq.write_config(
                state_dir=self._state_path,
                interface=self.interface,
                range_start=self.dhcp_range_start,
                range_end=self.dhcp_range_end,
                static_leases=[e.to_dict() for e in self.addresses if e.mac],
                dns_servers=self.dns_servers,
                gateway_ip=self.gateway_ip,
                dns_entries=self.dns_entries,
            )
            dnsmasq.reload_config(process=self._dnsmasq_process, state_dir=self._state_path)

    @staticmethod
    def _sanitize_tcpdump_args(args: list[str]) -> list[str]:
        """Filter out disallowed tcpdump flags from user-supplied arguments.

        Prevents the caller from overriding the interface (``-i``) or writing
        to files (``-w``), which could conflict with the driver's own
        interface enforcement or expose the host filesystem.
        """
        blocked = {"-i", "--interface", "-w"}
        sanitized: list[str] = []
        skip_next = False
        for arg in args:
            if skip_next:
                skip_next = False
                continue
            if arg in blocked:
                skip_next = True
                continue
            # Handle --flag=value form
            if any(arg.startswith(f"{b}=") for b in blocked):
                continue
            sanitized.append(arg)
        return sanitized

    @export
    async def tcpdump(self, args: list[str] | None = None) -> AsyncGenerator[str, None]:
        """Run tcpdump on the configured interface and stream output lines.

        The interface is always enforced to ``self.interface``; callers
        cannot override it. The ``-w`` flag is also blocked to prevent
        writing to the host filesystem. Additional tcpdump arguments can
        be passed via *args*.

        Requires ``enable_tcpdump: true`` in the driver config.

        Note on pcap streaming: tcpdump supports ``-w -`` to write pcap
        data to stdout, but this produces binary output that is not
        suitable for the text-based streaming transport used here. For
        pcap capture, consider running tcpdump directly on the exporter
        host and transferring the file via a separate mechanism.

        Args:
            args: Optional list of additional tcpdump arguments.

        Yields:
            Lines of tcpdump text output.
        """
        if not self.enable_tcpdump:
            raise RuntimeError(
                "tcpdump is not enabled. Set 'enable_tcpdump: true' in the driver config."
            )

        cmd = ["tcpdump", "-i", self.interface, "-l", "--packet-buffered"]
        if args:
            cmd.extend(self._sanitize_tcpdump_args(args))

        self.logger.info("Starting tcpdump: %s", " ".join(cmd))

        proc = await asyncio.subprocess.create_subprocess_exec(
            cmd[0],
            *cmd[1:],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._tcpdump_process = proc

        try:
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                yield line.decode("utf-8", errors="replace").rstrip("\n")
        finally:
            if proc.returncode is None:
                try:
                    proc.terminate()
                    await proc.wait()
                except ProcessLookupError:
                    pass
            self._tcpdump_process = None
