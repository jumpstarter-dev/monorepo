"""Integration tests for DutNetwork driver using veth pairs and network namespaces.

These tests require root or passwordless sudo for network namespace
and interface operations. They are skipped when neither is available.
"""

import ipaddress
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

import pytest

from ._privilege import has_privileges
from .driver import DutNetwork
from jumpstarter.common.utils import serve

requires_linux = pytest.mark.skipif(sys.platform != "linux", reason="requires Linux")
requires_privileges = pytest.mark.skipif(
    not has_privileges(), reason="requires root or passwordless sudo"
)
requires_nft = pytest.mark.skipif(not shutil.which("nft"), reason="nft not found")
requires_dnsmasq = pytest.mark.skipif(not shutil.which("dnsmasq"), reason="dnsmasq not found")
requires_dig = pytest.mark.skipif(not shutil.which("dig"), reason="dig not found")
requires_dhclient = pytest.mark.skipif(not shutil.which("dhclient"), reason="dhclient not found")

_SUDO_CMD: list[str] = ["sudo"] if os.getuid() != 0 else []


def _run(cmd: str, check: bool = True, ns: str | None = None) -> subprocess.CompletedProcess:
    """Run a command with sudo when not root, optionally inside a network namespace."""
    args = shlex.split(cmd)
    if ns:
        args = [*_SUDO_CMD, "ip", "netns", "exec", ns, *args]
    else:
        args = [*_SUDO_CMD, *args]
    return subprocess.run(args, capture_output=True, text=True, check=check)


def _popen(cmd: str, ns: str | None = None) -> subprocess.Popen:
    """Start a background process, optionally inside a network namespace."""
    args = shlex.split(cmd)
    if ns:
        args = [*_SUDO_CMD, "ip", "netns", "exec", ns, *args]
    else:
        args = [*_SUDO_CMD, *args]
    return subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _can_nat_between_namespaces() -> bool:
    """Probe whether nftables NAT actually forwards packets between namespaces.

    Sets up a veth pair with gateway IP, NAT masquerade rules, and routing
    between two network namespaces.  Returns False on any failure so tests
    that need working NAT are cleanly skipped.
    """
    if sys.platform != "linux" or not has_privileges():
        return False
    if not shutil.which("nft"):
        return False

    src_ns = "jmp-probe-src"
    dst_ns = "jmp-probe-dst"
    table = "jmp_probe"
    prev_fwd_host = "0"
    prev_fwd_up = "0"
    fwd_handles: list[str] = []
    try:
        _run(f"ip netns add {src_ns}", check=False)
        _run(f"ip netns add {dst_ns}", check=False)

        # veth: src_ns <--veth--> host <--veth--> dst_ns
        _run("ip link add jmp-ps0 type veth peer name jmp-ps1")
        _run(f"ip link set jmp-ps1 netns {src_ns}")
        _run("ip link add jmp-pd0 type veth peer name jmp-pd1")
        _run(f"ip link set jmp-pd1 netns {dst_ns}")

        # host-side DUT interface with gateway IP (no bridge)
        _run("ip addr add 172.31.0.1/24 dev jmp-ps0")
        _run("ip link set jmp-ps0 up")

        # upstream side
        _run("ip addr add 172.31.1.2/24 dev jmp-pd0")
        _run("ip link set jmp-pd0 up")

        # src namespace
        _run("ip addr add 172.31.0.2/24 dev jmp-ps1", ns=src_ns)
        _run("ip link set jmp-ps1 up", ns=src_ns)
        _run("ip link set lo up", ns=src_ns)
        _run("ip route add default via 172.31.0.1", ns=src_ns)

        # dst namespace
        _run("ip addr add 172.31.1.1/24 dev jmp-pd1", ns=dst_ns)
        _run("ip link set jmp-pd1 up", ns=dst_ns)
        _run("ip link set lo up", ns=dst_ns)
        _run("ip route add 172.31.0.0/24 via 172.31.1.2", ns=dst_ns)

        # enable per-interface forwarding + nft masquerade
        prev_fwd_host = _run("sysctl -n net.ipv4.conf.jmp-ps0.forwarding", check=False).stdout.strip() or "0"
        prev_fwd_up = _run("sysctl -n net.ipv4.conf.jmp-pd0.forwarding", check=False).stdout.strip() or "0"
        _run("sysctl -w net.ipv4.conf.jmp-ps0.forwarding=1")
        _run("sysctl -w net.ipv4.conf.jmp-pd0.forwarding=1")
        _run(f"nft add table ip {table}")
        _run(
            f"nft add chain ip {table} postrouting "
            f"'{{ type nat hook postrouting priority srcnat; policy accept; }}'"
        )
        _run(
            f"nft add rule ip {table} postrouting "
            f"oifname jmp-pd0 ip saddr 172.31.0.0/24 masquerade"
        )
        _run(
            f"nft add chain ip {table} forward "
            f"'{{ type filter hook forward priority filter; policy accept; }}'"
        )

        filter_check = _run("nft list chain ip filter FORWARD", check=False)
        if filter_check.returncode == 0 and "policy drop" in filter_check.stdout:
            for direction in ("iifname", "oifname"):
                for iface in ("jmp-ps0", "jmp-pd0"):
                    r = _run(
                        f"nft -e -a insert rule ip filter FORWARD {direction} {iface} accept",
                        check=False,
                    )
                    if r.returncode == 0:
                        m = re.search(r"# handle (\d+)", r.stdout)
                        if m:
                            fwd_handles.append(m.group(1))

        result = _run("ping -c 1 -W 2 172.31.1.1", ns=src_ns, check=False)
        return result.returncode == 0
    except Exception:
        return False
    finally:
        for handle in fwd_handles:
            _run(f"nft delete rule ip filter FORWARD handle {handle}", check=False)
        _run(f"sysctl -w net.ipv4.conf.jmp-ps0.forwarding={prev_fwd_host}", check=False)
        _run(f"sysctl -w net.ipv4.conf.jmp-pd0.forwarding={prev_fwd_up}", check=False)
        _run(f"nft delete table ip {table}", check=False)
        _run("ip link del jmp-ps0", check=False)
        _run("ip link del jmp-pd0", check=False)
        _run(f"ip netns del {src_ns}", check=False)
        _run(f"ip netns del {dst_ns}", check=False)


_nat_works: bool | None = None


def _nat_probe() -> bool:
    global _nat_works
    if _nat_works is None:
        _nat_works = _can_nat_between_namespaces()
    return bool(_nat_works)


@pytest.fixture(scope="session")
def nat_available() -> None:
    """Run the NAT probe once per session (at test time, not import time)."""
    if not _nat_probe():
        pytest.skip("nftables NAT between namespaces not available")  # type: ignore[misc]


class NetworkTestEnv:
    """Manages a veth + netns test environment simulating a DUT and external network."""

    DUT_NS = "jmp-test-dut"
    EXT_NS = "jmp-test-ext"
    VETH_HOST = "jmp-vhost"
    VETH_DUT = "jmp-vdut"
    VETH_UPSTREAM = "jmp-vup"
    VETH_EXT = "jmp-vext"
    NFT_TABLE = "jumpstarter_jmp_vhost"
    SUBNET = "192.168.200.0/24"
    GATEWAY = "192.168.200.1"
    DUT_IP = "192.168.200.10"
    DUT_MAC = "02:00:00:00:00:01"
    EXT_SUBNET = "10.99.0.0/24"
    EXT_IP = "10.99.0.1"
    UPSTREAM_IP = "10.99.0.2"

    def __init__(self):
        self.state_dir = tempfile.mkdtemp(prefix="jmp-dut-net-test-")
        os.chmod(self.state_dir, 0o755)

    def setup(self) -> None:
        """Create namespaces, veth pairs, and configure external network."""
        try:
            _run(f"ip netns add {self.DUT_NS}", check=False)
            _run(f"ip netns add {self.EXT_NS}", check=False)

            # veth pair for DUT: host side <-> DUT namespace
            _run(f"ip link add {self.VETH_HOST} type veth peer name {self.VETH_DUT}")
            _run(f"ip link set {self.VETH_DUT} netns {self.DUT_NS}")
            _run(f"ip link set {self.VETH_HOST} address {self.DUT_MAC}")

            # veth pair for upstream: host side <-> external namespace
            _run(f"ip link add {self.VETH_UPSTREAM} type veth peer name {self.VETH_EXT}")
            _run(f"ip link set {self.VETH_EXT} netns {self.EXT_NS}")

            # Configure upstream host side
            _run(f"ip addr add {self.UPSTREAM_IP}/24 dev {self.VETH_UPSTREAM}")
            _run(f"ip link set {self.VETH_UPSTREAM} up")

            # Configure external namespace
            _run(f"ip addr add {self.EXT_IP}/24 dev {self.VETH_EXT}", ns=self.EXT_NS)
            _run(f"ip link set {self.VETH_EXT} up", ns=self.EXT_NS)
            _run("ip link set lo up", ns=self.EXT_NS)
            _run(f"ip route add {self.SUBNET} via {self.UPSTREAM_IP}", ns=self.EXT_NS)

            _run("ip link set lo up", ns=self.DUT_NS)
        except Exception:
            self.teardown()
            raise

    def teardown(self) -> None:
        """Remove namespaces, veths, and state directory."""
        _run(f"ip link del {self.VETH_HOST}", check=False)
        _run(f"ip link del {self.VETH_UPSTREAM}", check=False)
        _run(f"ip netns del {self.DUT_NS}", check=False)
        _run(f"ip netns del {self.EXT_NS}", check=False)
        _run(f"nft delete table ip {self.NFT_TABLE}", check=False)
        if os.path.isdir(self.state_dir):
            shutil.rmtree(self.state_dir, ignore_errors=True)

    def configure_dut_static(self) -> None:
        """Configure static IP in the DUT namespace (as if the DUT has a static config)."""
        _run(f"ip addr add {self.DUT_IP}/24 dev {self.VETH_DUT}", ns=self.DUT_NS)
        _run(f"ip link set {self.VETH_DUT} up", ns=self.DUT_NS)
        _run(f"ip route add default via {self.GATEWAY}", ns=self.DUT_NS)

    def create_driver(self, nat_mode: str = "masquerade", **kwargs) -> DutNetwork:
        """Create a DutNetwork driver configured for this test environment."""
        params = {
            "interface": self.VETH_HOST,
            "subnet": self.SUBNET,
            "gateway_ip": self.GATEWAY,
            "upstream_interface": self.VETH_UPSTREAM,
            "nat_mode": nat_mode,
            "dhcp_enabled": True,
            "dhcp_range_start": "192.168.200.100",
            "dhcp_range_end": "192.168.200.200",
            "addresses": [{"mac": self.DUT_MAC, "ip": self.DUT_IP, "hostname": "test-dut"}],
            "dns_servers": ["8.8.8.8"],
            "state_dir": self.state_dir,
        }
        params.update(kwargs)
        return DutNetwork(**params)  # type: ignore[missing-argument]


@pytest.fixture
def net_env():
    """Pytest fixture that provides a clean network test environment."""
    env = NetworkTestEnv()
    env.setup()
    yield env
    env.teardown()


@requires_linux
@requires_privileges
@requires_nft
@requires_dnsmasq
class TestInterfaceSetup:
    """Test interface configuration (gateway IP assigned directly)."""

    def test_interface_configured_after_init(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver()
        try:
            result = _run(f"ip -o -4 addr show dev {net_env.VETH_HOST}")
            assert net_env.GATEWAY in result.stdout
        finally:
            driver.cleanup()

    def test_interface_deconfigured_after_cleanup(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver()
        driver.cleanup()

        result = _run(f"ip -o -4 addr show dev {net_env.VETH_HOST}", check=False)
        assert net_env.GATEWAY not in (result.stdout or "")


@requires_linux
@requires_privileges
@requires_nft
@requires_dnsmasq
class TestDhcp:
    """Test DHCP lease functionality."""

    def test_static_lease_in_config(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver()
        try:
            from pathlib import Path

            hosts = Path(net_env.state_dir) / "dhcp-hosts"
            content = hosts.read_text()
            assert net_env.DUT_MAC in content
            assert net_env.DUT_IP in content
            assert "test-dut" in content
        finally:
            driver.cleanup()

    def test_dnsmasq_running(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver()
        try:
            assert driver._dnsmasq_process is not None
            assert driver._dnsmasq_process.poll() is None
        finally:
            driver.cleanup()

    def test_dnsmasq_stopped_on_cleanup(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver()
        proc = driver._dnsmasq_process
        assert proc is not None
        driver.cleanup()
        proc.wait(timeout=5)
        assert proc.poll() is not None


@requires_linux
@requires_privileges
@requires_nft
@requires_dnsmasq
@pytest.mark.usefixtures("nat_available")
class TestMasqueradeNat:
    """Test masquerade NAT rules."""

    def test_nftables_rules_applied(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver(nat_mode="masquerade")
        try:
            result = _run(f"nft list table ip {net_env.NFT_TABLE}")
            assert "masquerade" in result.stdout
            assert net_env.VETH_HOST in result.stdout
            assert net_env.VETH_UPSTREAM in result.stdout
        finally:
            driver.cleanup()

    def test_dut_can_reach_upstream(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver(nat_mode="masquerade")
        try:
            net_env.configure_dut_static()
            time.sleep(0.5)
            result = _run(
                f"ping -c 1 -W 2 {net_env.UPSTREAM_IP}",
                ns=net_env.DUT_NS,
                check=False,
            )
            assert result.returncode == 0, f"Ping failed: {result.stderr}"
        finally:
            driver.cleanup()

    def test_dut_can_reach_external_via_nat(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver(nat_mode="masquerade")
        try:
            net_env.configure_dut_static()
            time.sleep(0.5)
            result = _run(
                f"ping -c 1 -W 2 {net_env.EXT_IP}",
                ns=net_env.DUT_NS,
                check=False,
            )
            assert result.returncode == 0, f"Ping to external failed: {result.stderr}"
        finally:
            driver.cleanup()

    def test_nftables_rules_flushed_on_cleanup(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver(nat_mode="masquerade")
        driver.cleanup()
        result = _run(f"nft list table ip {net_env.NFT_TABLE}", check=False)
        assert result.returncode != 0


@requires_linux
@requires_privileges
@requires_nft
@requires_dnsmasq
class TestOneToOneNat:
    """Test 1:1 NAT rules with per-lease public_ip."""

    PUBLIC_IP = "10.99.0.50"

    def _leases_with_public_ip(self, net_env: NetworkTestEnv) -> list[dict[str, str]]:
        return [{"mac": net_env.DUT_MAC, "ip": net_env.DUT_IP, "hostname": "test-dut", "public_ip": self.PUBLIC_IP}]

    def test_1to1_nftables_rules(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver(
            nat_mode="1to1",
            addresses=self._leases_with_public_ip(net_env),
        )
        try:
            result = _run(f"nft list table ip {net_env.NFT_TABLE}")
            assert "dnat" in result.stdout
            assert "snat" in result.stdout
            assert self.PUBLIC_IP in result.stdout
            assert net_env.DUT_IP in result.stdout
            assert "chain output" in result.stdout
        finally:
            driver.cleanup()

    def test_ip_alias_added(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver(
            nat_mode="1to1",
            addresses=self._leases_with_public_ip(net_env),
        )
        try:
            result = _run(f"ip -o -4 addr show dev {net_env.VETH_UPSTREAM}")
            assert self.PUBLIC_IP in result.stdout
        finally:
            driver.cleanup()

    def test_ip_alias_removed_on_cleanup(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver(
            nat_mode="1to1",
            addresses=self._leases_with_public_ip(net_env),
        )
        driver.cleanup()
        result = _run(f"ip -o -4 addr show dev {net_env.VETH_UPSTREAM}", check=False)
        assert self.PUBLIC_IP not in (result.stdout or "")


@requires_linux
@requires_privileges
@requires_nft
@requires_dnsmasq
class TestDriverRpc:
    """Test the driver RPC surface via serve()."""

    def test_status_returns_valid_dict(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver()
        try:
            with serve(driver) as client:
                result = client.status()
                assert isinstance(result, dict)
                assert result["interface_status"]["name"] == net_env.VETH_HOST
                assert result["interface_status"]["exists"] is True
                assert result["nat_mode"] == "masquerade"
                assert result["subnet"] == net_env.SUBNET
        finally:
            driver.cleanup()

    def test_get_leases_returns_list(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver()
        try:
            with serve(driver) as client:
                leases = client.get_leases()
                assert isinstance(leases, list)
        finally:
            driver.cleanup()

    def test_add_and_remove_address(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver()
        try:
            with serve(driver) as client:
                client.add_address("192.168.200.99", mac="02:00:00:00:00:99", hostname="new-dut")
                from pathlib import Path

                hosts = Path(net_env.state_dir) / "dhcp-hosts"
                assert "02:00:00:00:00:99" in hosts.read_text()

                client.remove_address("192.168.200.99")
                assert "02:00:00:00:00:99" not in hosts.read_text()
        finally:
            driver.cleanup()

    def test_get_nat_rules(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver()
        try:
            with serve(driver) as client:
                rules = client.get_nat_rules()
                assert "masquerade" in rules
        finally:
            driver.cleanup()


@requires_linux
@requires_privileges
@requires_nft
@requires_dnsmasq
class TestTeardown:
    """Test that cleanup properly restores the system."""

    def test_full_cleanup(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver()
        driver.cleanup()

        result = _run(f"ip -o -4 addr show dev {net_env.VETH_HOST}", check=False)
        assert net_env.GATEWAY not in (result.stdout or "")

        result = _run(f"nft list table ip {net_env.NFT_TABLE}", check=False)
        assert result.returncode != 0

    def test_per_interface_forwarding_set(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver()
        try:
            result = _run(f"sysctl -n net.ipv4.conf.{net_env.VETH_HOST}.forwarding")
            assert result.stdout.strip() == "1"
            result = _run(f"sysctl -n net.ipv4.conf.{net_env.VETH_UPSTREAM}.forwarding")
            assert result.stdout.strip() == "1"
        finally:
            driver.cleanup()


@requires_linux
@requires_privileges
@requires_nft
@requires_dnsmasq
class TestDisabledNat:
    """Test disabled NAT mode (DHCP only, no routing)."""

    def test_no_nftables_rules_when_disabled(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver(nat_mode="disabled")
        try:
            result = _run(f"nft list table ip {net_env.NFT_TABLE}", check=False)
            assert result.returncode != 0
        finally:
            driver.cleanup()

    def test_none_alias_works(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver(nat_mode="none")
        try:
            result = _run(f"nft list table ip {net_env.NFT_TABLE}", check=False)
            assert result.returncode != 0
        finally:
            driver.cleanup()

    def test_interface_still_configured(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver(nat_mode="disabled")
        try:
            result = _run(f"ip -o -4 addr show dev {net_env.VETH_HOST}")
            assert net_env.GATEWAY in result.stdout
        finally:
            driver.cleanup()

    def test_dnsmasq_still_running(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver(nat_mode="disabled")
        try:
            assert driver._dnsmasq_process is not None
            assert driver._dnsmasq_process.poll() is None
        finally:
            driver.cleanup()


@requires_linux
@requires_privileges
@requires_nft
@requires_dnsmasq
class TestMultiDut1to1:
    """Test multi-DUT 1:1 NAT with per-lease public_ip."""

    PUBLIC_IP_1 = "10.99.0.50"
    PUBLIC_IP_2 = "10.99.0.51"
    DUT_IP_2 = "192.168.200.11"
    DUT_MAC_2 = "02:00:00:00:00:02"

    def _multi_leases(self, net_env: NetworkTestEnv) -> list[dict[str, str]]:
        return [
            {"mac": net_env.DUT_MAC, "ip": net_env.DUT_IP, "hostname": "dut1", "public_ip": self.PUBLIC_IP_1},
            {"mac": self.DUT_MAC_2, "ip": self.DUT_IP_2, "hostname": "dut2", "public_ip": self.PUBLIC_IP_2},
        ]

    def test_both_dnat_snat_rules(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver(
            nat_mode="1to1",
            addresses=self._multi_leases(net_env),
        )
        try:
            result = _run(f"nft list table ip {net_env.NFT_TABLE}")
            assert self.PUBLIC_IP_1 in result.stdout
            assert self.PUBLIC_IP_2 in result.stdout
            assert net_env.DUT_IP in result.stdout
            assert self.DUT_IP_2 in result.stdout
        finally:
            driver.cleanup()

    def test_both_ip_aliases_added(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver(
            nat_mode="1to1",
            addresses=self._multi_leases(net_env),
        )
        try:
            result = _run(f"ip -o -4 addr show dev {net_env.VETH_UPSTREAM}")
            assert self.PUBLIC_IP_1 in result.stdout
            assert self.PUBLIC_IP_2 in result.stdout
        finally:
            driver.cleanup()

    def test_both_aliases_removed_on_cleanup(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver(
            nat_mode="1to1",
            addresses=self._multi_leases(net_env),
        )
        driver.cleanup()
        result = _run(f"ip -o -4 addr show dev {net_env.VETH_UPSTREAM}", check=False)
        assert self.PUBLIC_IP_1 not in (result.stdout or "")
        assert self.PUBLIC_IP_2 not in (result.stdout or "")

    def test_masquerade_fallback_for_unmapped_dut(self, net_env: NetworkTestEnv):
        """A lease without public_ip still gets masquerade in the ruleset."""
        leases = [
            {"mac": net_env.DUT_MAC, "ip": net_env.DUT_IP, "hostname": "dut1", "public_ip": self.PUBLIC_IP_1},
            {"mac": self.DUT_MAC_2, "ip": self.DUT_IP_2, "hostname": "dut2"},
        ]
        driver = net_env.create_driver(nat_mode="1to1", addresses=leases)
        try:
            result = _run(f"nft list table ip {net_env.NFT_TABLE}")
            assert "masquerade" in result.stdout
            assert self.PUBLIC_IP_1 in result.stdout
            assert self.DUT_IP_2 not in result.stdout
        finally:
            driver.cleanup()


@requires_linux
@requires_privileges
@requires_nft
@requires_dnsmasq
class TestDnsEntries:
    """Test custom DNS entries in dnsmasq."""

    def test_dns_entries_in_config(self, net_env: NetworkTestEnv):
        dns = [{"hostname": "myhost.lab.local", "ip": "10.0.0.99"}]
        driver = net_env.create_driver(dns_entries=dns)
        try:
            from pathlib import Path

            hosts = (Path(net_env.state_dir) / "hosts.local").read_text()
            assert "10.0.0.99 myhost.lab.local" in hosts
        finally:
            driver.cleanup()

    def test_add_dns_entry_via_rpc(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver()
        try:
            with serve(driver) as client:
                client.add_dns_entry("new.lab.local", "10.0.0.50")
                entries = client.get_dns_entries()
                assert any(e["hostname"] == "new.lab.local" for e in entries)

                from pathlib import Path

                hosts = (Path(net_env.state_dir) / "hosts.local").read_text()
                assert "10.0.0.50 new.lab.local" in hosts
        finally:
            driver.cleanup()

    def test_remove_dns_entry_via_rpc(self, net_env: NetworkTestEnv):
        dns = [{"hostname": "remove-me.lab.local", "ip": "10.0.0.77"}]
        driver = net_env.create_driver(dns_entries=dns)
        try:
            with serve(driver) as client:
                client.remove_dns_entry("remove-me.lab.local")
                entries = client.get_dns_entries()
                assert not any(e["hostname"] == "remove-me.lab.local" for e in entries)

                from pathlib import Path

                hosts = (Path(net_env.state_dir) / "hosts.local").read_text()
                assert "remove-me.lab.local" not in hosts
        finally:
            driver.cleanup()


@requires_linux
@requires_privileges
@requires_nft
@requires_dnsmasq
@pytest.mark.usefixtures("nat_available")
class TestOneToOneNatDataPlane:
    """Test actual data-plane connectivity through 1:1 NAT (not just rule presence)."""

    PUBLIC_IP = "10.99.0.50"

    def _leases_with_public_ip(self, net_env: NetworkTestEnv) -> list[dict[str, str]]:
        return [{"mac": net_env.DUT_MAC, "ip": net_env.DUT_IP, "hostname": "test-dut", "public_ip": self.PUBLIC_IP}]

    def test_dut_can_reach_external_via_snat(self, net_env: NetworkTestEnv):
        """SNAT path: DUT -> external, source is rewritten to public_ip."""
        driver = net_env.create_driver(
            nat_mode="1to1",
            addresses=self._leases_with_public_ip(net_env),
        )
        try:
            net_env.configure_dut_static()
            time.sleep(0.5)
            result = _run(
                f"ping -c 1 -W 2 {net_env.EXT_IP}",
                ns=net_env.DUT_NS,
                check=False,
            )
            assert result.returncode == 0, f"SNAT ping failed: {result.stderr}"
        finally:
            driver.cleanup()

    def test_external_can_reach_dut_via_dnat(self, net_env: NetworkTestEnv):
        """DNAT path: external -> public_ip is translated to DUT private_ip."""
        driver = net_env.create_driver(
            nat_mode="1to1",
            addresses=self._leases_with_public_ip(net_env),
        )
        try:
            net_env.configure_dut_static()
            time.sleep(0.5)
            result = _run(
                f"ping -c 1 -W 2 {self.PUBLIC_IP}",
                ns=net_env.EXT_NS,
                check=False,
            )
            assert result.returncode == 0, f"DNAT ping failed: {result.stderr}"
        finally:
            driver.cleanup()

    def test_exporter_can_reach_dut_via_public_ip(self, net_env: NetworkTestEnv):
        """Hairpin NAT: the exporter host itself can reach the DUT via its public IP.

        Locally-originated packets hit the nft output chain which DNATs the
        public_ip to the DUT's private_ip, allowing automation scripts on
        the exporter to use the same address as external hosts.
        """
        driver = net_env.create_driver(
            nat_mode="1to1",
            addresses=self._leases_with_public_ip(net_env),
        )
        try:
            net_env.configure_dut_static()
            time.sleep(0.5)
            result = _run(
                f"ping -c 1 -W 2 {self.PUBLIC_IP}",
                check=False,
            )
            assert result.returncode == 0, f"Hairpin DNAT ping failed: {result.stderr}"
        finally:
            driver.cleanup()


@requires_linux
@requires_privileges
@requires_nft
@requires_dnsmasq
@pytest.mark.usefixtures("nat_available")
class TestDisabledNatIsolation:
    """Test that disabled NAT prevents routing while still allowing local access."""

    def test_dut_cannot_reach_external_without_nat(self, net_env: NetworkTestEnv):
        prev_fwd_host = (
            _run(f"sysctl -n net.ipv4.conf.{net_env.VETH_HOST}.forwarding", check=False).stdout.strip() or "0"
        )
        prev_fwd_up = (
            _run(f"sysctl -n net.ipv4.conf.{net_env.VETH_UPSTREAM}.forwarding", check=False).stdout.strip() or "0"
        )
        _run(f"sysctl -w net.ipv4.conf.{net_env.VETH_HOST}.forwarding=0", check=False)
        _run(f"sysctl -w net.ipv4.conf.{net_env.VETH_UPSTREAM}.forwarding=0", check=False)
        driver = net_env.create_driver(nat_mode="disabled")
        try:
            net_env.configure_dut_static()
            time.sleep(0.5)
            result = _run(
                f"ping -c 1 -W 2 {net_env.EXT_IP}",
                ns=net_env.DUT_NS,
                check=False,
            )
            assert result.returncode != 0, "DUT should NOT reach external with disabled NAT"
        finally:
            driver.cleanup()
            _run(f"sysctl -w net.ipv4.conf.{net_env.VETH_HOST}.forwarding={prev_fwd_host}", check=False)
            _run(f"sysctl -w net.ipv4.conf.{net_env.VETH_UPSTREAM}.forwarding={prev_fwd_up}", check=False)

    def test_dut_can_still_reach_gateway(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver(nat_mode="disabled")
        try:
            net_env.configure_dut_static()
            time.sleep(0.5)
            result = _run(
                f"ping -c 1 -W 2 {net_env.GATEWAY}",
                ns=net_env.DUT_NS,
                check=False,
            )
            assert result.returncode == 0, f"Gateway ping failed: {result.stderr}"
        finally:
            driver.cleanup()


@requires_linux
@requires_privileges
@requires_nft
@requires_dnsmasq
@pytest.mark.usefixtures("nat_available")
class TestTcpConnectivity:
    """Test TCP connectivity through masquerade NAT (not just ICMP ping)."""

    TCP_PORT = 9999

    def test_tcp_connection_via_masquerade(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver(nat_mode="masquerade")
        listener = None
        try:
            net_env.configure_dut_static()
            time.sleep(0.5)

            server_script = (
                "import socket; "
                "s=socket.socket(); "
                "s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); "
                f"s.bind(('',{self.TCP_PORT})); "
                "s.listen(1); "
                "s.settimeout(10); "
                "conn,_=s.accept(); "
                "conn.sendall(b'OK'); "
                "conn.close(); "
                "s.close()"
            )
            listener = _popen(f'python3 -c "{server_script}"', ns=net_env.EXT_NS)
            time.sleep(0.5)

            client_script = (
                "import socket; "
                f"s=socket.create_connection(('{net_env.EXT_IP}',{self.TCP_PORT}),timeout=5); "
                "data=s.recv(10); "
                "s.close(); "
                "print(data.decode())"
            )
            result = _run(
                f'python3 -c "{client_script}"',
                ns=net_env.DUT_NS,
                check=False,
            )
            assert result.returncode == 0, f"TCP connection failed: {result.stderr}"
            assert "OK" in result.stdout
        finally:
            if listener and listener.poll() is None:
                listener.kill()
                listener.wait()
            driver.cleanup()


@requires_linux
@requires_privileges
@requires_nft
@requires_dnsmasq
@requires_dhclient
class TestDhcpAcquisition:
    """Test actual DHCP address acquisition from dnsmasq (not just config verification)."""

    def test_dut_acquires_ip_via_dhcp(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver()
        try:
            _run(f"ip link set {net_env.VETH_DUT} address {net_env.DUT_MAC}", ns=net_env.DUT_NS)
            _run(f"ip link set {net_env.VETH_DUT} up", ns=net_env.DUT_NS)

            dhclient_lease = os.path.join(net_env.state_dir, "dhclient.leases")
            dhclient_pid = os.path.join(net_env.state_dir, "dhclient.pid")
            result = _run(
                f"dhclient -1 -v -lf {dhclient_lease} -pf {dhclient_pid} {net_env.VETH_DUT}",
                ns=net_env.DUT_NS,
                check=False,
            )
            assert result.returncode == 0, f"dhclient failed: {result.stderr}"

            addr_result = _run(
                f"ip -o -4 addr show dev {net_env.VETH_DUT}",
                ns=net_env.DUT_NS,
            )
            assert net_env.DUT_IP in addr_result.stdout, (
                f"Expected DHCP to assign {net_env.DUT_IP}, got: {addr_result.stdout}"
            )
        finally:
            _run(
                f"dhclient -r -lf {os.path.join(net_env.state_dir, 'dhclient.leases')} "
                f"-pf {os.path.join(net_env.state_dir, 'dhclient.pid')} {net_env.VETH_DUT}",
                ns=net_env.DUT_NS,
                check=False,
            )
            driver.cleanup()

    def test_dhcp_lease_populates_lease_file(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver()
        try:
            _run(f"ip link set {net_env.VETH_DUT} address {net_env.DUT_MAC}", ns=net_env.DUT_NS)
            _run(f"ip link set {net_env.VETH_DUT} up", ns=net_env.DUT_NS)

            dhclient_lease = os.path.join(net_env.state_dir, "dhclient.leases")
            dhclient_pid = os.path.join(net_env.state_dir, "dhclient.pid")
            _run(
                f"dhclient -1 -v -lf {dhclient_lease} -pf {dhclient_pid} {net_env.VETH_DUT}",
                ns=net_env.DUT_NS,
            )

            from pathlib import Path

            lease_file = Path(net_env.state_dir) / "dnsmasq.leases"
            content = lease_file.read_text()
            assert net_env.DUT_MAC.lower() in content.lower()
            assert net_env.DUT_IP in content
        finally:
            _run(
                f"dhclient -r -lf {os.path.join(net_env.state_dir, 'dhclient.leases')} "
                f"-pf {os.path.join(net_env.state_dir, 'dhclient.pid')} {net_env.VETH_DUT}",
                ns=net_env.DUT_NS,
                check=False,
            )
            driver.cleanup()

    def test_get_dut_ip_with_real_lease(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver()
        try:
            _run(f"ip link set {net_env.VETH_DUT} address {net_env.DUT_MAC}", ns=net_env.DUT_NS)
            _run(f"ip link set {net_env.VETH_DUT} up", ns=net_env.DUT_NS)

            dhclient_lease = os.path.join(net_env.state_dir, "dhclient.leases")
            dhclient_pid = os.path.join(net_env.state_dir, "dhclient.pid")
            _run(
                f"dhclient -1 -v -lf {dhclient_lease} -pf {dhclient_pid} {net_env.VETH_DUT}",
                ns=net_env.DUT_NS,
            )

            with serve(driver) as client:
                ip = client.get_dut_ip(net_env.DUT_MAC)
                assert ip == net_env.DUT_IP
        finally:
            _run(
                f"dhclient -r -lf {os.path.join(net_env.state_dir, 'dhclient.leases')} "
                f"-pf {os.path.join(net_env.state_dir, 'dhclient.pid')} {net_env.VETH_DUT}",
                ns=net_env.DUT_NS,
                check=False,
            )
            driver.cleanup()


@requires_linux
@requires_privileges
@requires_nft
@requires_dnsmasq
@requires_dig
class TestDnsResolution:
    """Test that dnsmasq actually resolves custom DNS entries (not just config file checks)."""

    def test_custom_dns_entry_resolves(self, net_env: NetworkTestEnv):
        dns = [{"hostname": "myhost.lab.local", "ip": "10.0.0.99"}]
        driver = net_env.create_driver(dns_entries=dns)
        try:
            time.sleep(0.5)
            result = _run(
                f"dig @{net_env.GATEWAY} myhost.lab.local +short +time=2 +tries=1",
            )
            assert "10.0.0.99" in result.stdout, (
                f"DNS resolution failed, expected 10.0.0.99, got: {result.stdout}"
            )
        finally:
            driver.cleanup()

    def test_added_dns_entry_resolves_after_reload(self, net_env: NetworkTestEnv):
        driver = net_env.create_driver()
        try:
            with serve(driver) as client:
                client.add_dns_entry("dynamic.lab.local", "10.0.0.88")
                time.sleep(0.5)

                result = _run(
                    f"dig @{net_env.GATEWAY} dynamic.lab.local +short +time=2 +tries=1",
                )
                assert "10.0.0.88" in result.stdout, (
                    f"DNS resolution failed for dynamic entry, got: {result.stdout}"
                )
        finally:
            driver.cleanup()

    def test_removed_dns_entry_no_longer_resolves(self, net_env: NetworkTestEnv):
        dns = [{"hostname": "temp.lab.local", "ip": "10.0.0.77"}]
        driver = net_env.create_driver(dns_entries=dns)
        try:
            time.sleep(0.5)
            result = _run(f"dig @{net_env.GATEWAY} temp.lab.local +short +time=2 +tries=1")
            assert "10.0.0.77" in result.stdout

            with serve(driver) as client:
                client.remove_dns_entry("temp.lab.local")
                time.sleep(0.5)

                result = _run(
                    f"dig @{net_env.GATEWAY} temp.lab.local +short +time=2 +tries=1",
                    check=False,
                )
                assert "10.0.0.77" not in result.stdout
        finally:
            driver.cleanup()


@requires_linux
@requires_privileges
@requires_nft
@requires_dnsmasq
@pytest.mark.usefixtures("nat_available")
class TestFilterDataPlane:
    """Data-plane tests for egress/ingress traffic filtering via nftables.

    These tests verify that filter rules actually affect packet forwarding
    by using veth pairs and network namespaces.
    """

    def test_egress_drop_blocks_traffic(self, net_env: NetworkTestEnv):
        """An egress drop-all policy prevents the DUT from reaching external."""
        fc = {
            "egress": {"policy": "drop"},
        }
        driver = net_env.create_driver(nat_mode="masquerade", filter=fc)
        try:
            net_env.configure_dut_static()
            time.sleep(0.5)
            result = _run(
                f"ping -c 1 -W 2 {net_env.EXT_IP}",
                ns=net_env.DUT_NS,
                check=False,
            )
            assert result.returncode != 0, "DUT should NOT reach external with egress drop policy"
        finally:
            driver.cleanup()

    def test_egress_accept_allows_traffic(self, net_env: NetworkTestEnv):
        """An egress accept policy allows the DUT to reach external (baseline)."""
        fc = {
            "egress": {"policy": "accept"},
        }
        driver = net_env.create_driver(nat_mode="masquerade", filter=fc)
        try:
            net_env.configure_dut_static()
            time.sleep(0.5)
            result = _run(
                f"ping -c 1 -W 2 {net_env.EXT_IP}",
                ns=net_env.DUT_NS,
                check=False,
            )
            assert result.returncode == 0, f"Egress accept should allow traffic: {result.stderr}"
        finally:
            driver.cleanup()

    def test_egress_drop_with_destination_exception(self, net_env: NetworkTestEnv):
        """Egress drop policy with an accept rule for the target destination still allows it."""
        fc = {
            "egress": {
                "policy": "drop",
                "rules": [
                    {"action": "accept", "destination": net_env.EXT_SUBNET},
                ],
            },
        }
        driver = net_env.create_driver(nat_mode="masquerade", filter=fc)
        try:
            net_env.configure_dut_static()
            time.sleep(0.5)
            result = _run(
                f"ping -c 1 -W 2 {net_env.EXT_IP}",
                ns=net_env.DUT_NS,
                check=False,
            )
            assert result.returncode == 0, (
                f"Egress drop with accept exception should allow traffic: {result.stderr}"
            )
        finally:
            driver.cleanup()

    def test_egress_accept_with_destination_drop(self, net_env: NetworkTestEnv):
        """Egress accept policy with a drop rule for the target destination blocks it."""
        fc = {
            "egress": {
                "policy": "accept",
                "rules": [
                    {"action": "drop", "destination": net_env.EXT_SUBNET},
                ],
            },
        }
        driver = net_env.create_driver(nat_mode="masquerade", filter=fc)
        try:
            net_env.configure_dut_static()
            time.sleep(0.5)
            result = _run(
                f"ping -c 1 -W 2 {net_env.EXT_IP}",
                ns=net_env.DUT_NS,
                check=False,
            )
            assert result.returncode != 0, "Egress drop rule should block traffic to target subnet"
        finally:
            driver.cleanup()

    def test_ingress_drop_blocks_external_to_dut(self, net_env: NetworkTestEnv):
        """Ingress drop policy blocks new connections from external to DUT."""
        fc = {
            "egress": {"policy": "accept"},
            "ingress": {"policy": "drop"},
        }
        driver = net_env.create_driver(nat_mode="masquerade", filter=fc)
        try:
            net_env.configure_dut_static()
            time.sleep(0.5)
            # DUT egress should still work (established return traffic is allowed by conntrack)
            result = _run(
                f"ping -c 1 -W 2 {net_env.EXT_IP}",
                ns=net_env.DUT_NS,
                check=False,
            )
            assert result.returncode == 0, (
                f"DUT egress should work even with ingress drop: {result.stderr}"
            )
            # But new connections from external to DUT should fail
            result = _run(
                f"ping -c 1 -W 2 {net_env.DUT_IP}",
                ns=net_env.EXT_NS,
                check=False,
            )
            assert result.returncode != 0, "External should NOT reach DUT with ingress drop"
        finally:
            driver.cleanup()

    def test_no_filter_allows_all_traffic(self, net_env: NetworkTestEnv):
        """Without any filter, all traffic flows freely (backward compatibility)."""
        driver = net_env.create_driver(nat_mode="masquerade")
        try:
            net_env.configure_dut_static()
            time.sleep(0.5)
            result = _run(
                f"ping -c 1 -W 2 {net_env.EXT_IP}",
                ns=net_env.DUT_NS,
                check=False,
            )
            assert result.returncode == 0, f"No-filter should allow all traffic: {result.stderr}"
        finally:
            driver.cleanup()


@requires_linux
@requires_privileges
@requires_nft
@requires_dnsmasq
class TestVlanAndPbr:
    """Control-plane tests for VLAN sub-interfaces and policy-based routing."""

    VLAN_ID = 100
    PUBLIC_IP = "10.99.0.50"
    PUBLIC_GW = "10.99.0.1"

    def _vlan_name(self, net_env: NetworkTestEnv) -> str:
        return f"{net_env.VETH_UPSTREAM}.{self.VLAN_ID}"

    def _vlan_addresses(self, net_env: NetworkTestEnv) -> list[dict]:
        return [{
            "mac": net_env.DUT_MAC,
            "ip": net_env.DUT_IP,
            "hostname": "test-dut",
            "public_ip": self.PUBLIC_IP,
            "vlan_id": self.VLAN_ID,
            "public_gateway": self.PUBLIC_GW,
        }]

    def test_vlan_interface_created_and_cleaned_up(self, net_env: NetworkTestEnv):
        vlan = self._vlan_name(net_env)
        driver = net_env.create_driver(
            nat_mode="1to1",
            addresses=self._vlan_addresses(net_env),
        )
        try:
            result = _run(f"ip link show {vlan}")
            assert result.returncode == 0
            addr = _run(f"ip -o -4 addr show dev {vlan}")
            assert self.PUBLIC_IP in addr.stdout
            fwd = _run(f"sysctl -n net.ipv4.conf.{net_env.VETH_UPSTREAM}/{self.VLAN_ID}.forwarding")
            assert fwd.stdout.strip() == "1"
            rp = _run(f"sysctl -n net.ipv4.conf.{net_env.VETH_UPSTREAM}/{self.VLAN_ID}.rp_filter")
            assert rp.stdout.strip() == "2"
            rules = _run("ip rule show")
            assert net_env.DUT_IP in rules.stdout
            assert f"lookup {self.VLAN_ID}" in rules.stdout
            table = _run(f"ip route show table {self.VLAN_ID}")
            assert self.PUBLIC_GW in table.stdout
            assert vlan in table.stdout
            nft = _run(f"nft list table ip {net_env.NFT_TABLE}")
            assert vlan in nft.stdout
            assert self.PUBLIC_IP in nft.stdout
        finally:
            driver.cleanup()

        gone = _run(f"ip link show {vlan}", check=False)
        assert gone.returncode != 0
        rules = _run("ip rule show")
        assert f"from {net_env.DUT_IP} lookup {self.VLAN_ID}" not in rules.stdout

    def test_masquerade_nftables_use_vlan_interface(self, net_env: NetworkTestEnv):
        vlan = self._vlan_name(net_env)
        driver = net_env.create_driver(
            nat_mode="masquerade",
            addresses=self._vlan_addresses(net_env),
        )
        try:
            nft = _run(f"nft list table ip {net_env.NFT_TABLE}")
            assert f'oifname "{vlan}"' in nft.stdout
            assert "masquerade" in nft.stdout
        finally:
            driver.cleanup()

    def test_vlan_without_gateway_skips_pbr(self, net_env: NetworkTestEnv):
        vlan = self._vlan_name(net_env)
        driver = net_env.create_driver(
            nat_mode="masquerade",
            addresses=[{
                "mac": net_env.DUT_MAC,
                "ip": net_env.DUT_IP,
                "hostname": "test-dut",
                "vlan_id": self.VLAN_ID,
            }],
        )
        try:
            result = _run(f"ip link show {vlan}")
            assert result.returncode == 0
            rules = _run("ip rule show")
            assert f"from {net_env.DUT_IP}" not in rules.stdout
        finally:
            driver.cleanup()

        gone = _run(f"ip link show {vlan}", check=False)
        assert gone.returncode != 0

    def test_untagged_pbr_uses_private_ip_table(self, net_env: NetworkTestEnv):
        pbr_ip = "192.168.200.51"
        table = int(ipaddress.IPv4Address(pbr_ip))
        driver = net_env.create_driver(
            nat_mode="masquerade",
            addresses=[{
                "ip": pbr_ip,
                "public_gateway": self.PUBLIC_GW,
            }],
        )
        try:
            rules = _run("ip rule show")
            assert f"from {pbr_ip}" in rules.stdout
            assert f"lookup {table}" in rules.stdout
            table_routes = _run(f"ip route show table {table}")
            assert self.PUBLIC_GW in table_routes.stdout
            assert net_env.VETH_UPSTREAM in table_routes.stdout
        finally:
            driver.cleanup()

        rules = _run("ip rule show")
        assert f"from {pbr_ip} lookup {table}" not in rules.stdout
