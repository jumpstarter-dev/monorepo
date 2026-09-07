import ipaddress
import logging
import socket
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from .driver import FilterConfig, FilterDirection, FilterRule

_DRIVER_MODULE = "jumpstarter_driver_dut_network.driver"


def _make_driver(tmp_path, **overrides):
    """Create a DutNetwork driver with all system calls mocked."""
    params = {
        "interface": "eth-dut",
        "subnet": "192.168.100.0/24",
        "gateway_ip": "192.168.100.1",
        "upstream_interface": "eth-up",
        "nat_mode": "masquerade",
        "dhcp_enabled": True,
        "dhcp_range_start": "192.168.100.100",
        "dhcp_range_end": "192.168.100.200",
        "addresses": [],
        "dns_servers": ["8.8.8.8"],
        "state_dir": str(tmp_path),
    }
    params.update(overrides)

    from .driver import DutNetwork

    with patch(f"{_DRIVER_MODULE}.sys") as mock_sys, \
         patch(f"{_DRIVER_MODULE}.shutil") as mock_shutil, \
         patch(f"{_DRIVER_MODULE}.iproute") as mock_iproute, \
         patch(f"{_DRIVER_MODULE}.nftables") as mock_nftables, \
         patch(f"{_DRIVER_MODULE}.dnsmasq") as mock_dnsmasq:
        mock_sys.platform = "linux"
        mock_shutil.which.return_value = "/usr/bin/fake"
        mock_dnsmasq.state_dir_for_interface.return_value = tmp_path
        mock_dnsmasq.start.return_value = MagicMock()
        mock_iproute.detect_upstream_interface.return_value = "eth-up"
        mock_iproute.interface_exists.return_value = False
        mock_iproute.get_interface_addresses.return_value = []
        mock_iproute.get_interface_forwarding.return_value = "0"
        mock_iproute.get_interface_prefix_len.return_value = 24
        mock_nftables.ensure_filter_forward.return_value = []
        mock_nftables.list_rules.return_value = ""
        mock_nftables._table_name_for.return_value = "jumpstarter_eth_dut"
        driver = DutNetwork(**params)  # type: ignore[missing-argument]

    return driver, mock_iproute, mock_nftables, mock_dnsmasq


class TestDriverValidation:
    def test_gateway_outside_subnet_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="not within subnet"):
            _make_driver(tmp_path, gateway_ip="10.0.0.1", subnet="192.168.100.0/24")

    def test_1to1_without_public_ip_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="public_ip"):
            _make_driver(
                tmp_path,
                nat_mode="1to1",
                addresses=[{"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.100.10"}],
            )

    def test_1to1_with_public_ip_ok(self, tmp_path: Path):
        driver, _, _, _ = _make_driver(
            tmp_path,
            nat_mode="1to1",
            addresses=[{"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.100.10", "public_ip": "10.0.0.50"}],
        )
        assert driver.nat_mode == "1to1"

    def test_valid_masquerade_config(self, tmp_path: Path):
        driver, _, _, _ = _make_driver(tmp_path)
        assert driver._prefix_len == 24


class TestFilterValidation:
    """Tests for the filter config validation in _validate_config()."""

    def test_valid_filter_config_from_dict(self, tmp_path: Path):
        """Raw dicts (YAML deserialization path) are converted to FilterConfig."""
        fc = {
            "egress": {
                "policy": "accept",
                "rules": [
                    {"action": "drop", "destination": "10.0.0.0/8"},
                    {"action": "drop", "destination": "172.16.0.0/12"},
                ],
            },
            "ingress": {
                "policy": "drop",
                "rules": [
                    {"action": "accept", "source": "198.51.100.0/24", "port": 22, "protocol": "tcp"},
                ],
            },
        }
        driver, _, _, _ = _make_driver(tmp_path, filter=fc)
        assert isinstance(driver.filter, FilterConfig)
        assert driver.filter.egress is not None
        assert driver.filter.egress.policy == "accept"
        assert len(driver.filter.egress.rules) == 2
        assert driver.filter.ingress is not None
        assert driver.filter.ingress.policy == "drop"

    def test_valid_filter_config_from_typed(self, tmp_path: Path):
        """Typed FilterConfig objects are accepted directly."""
        fc = FilterConfig(
            egress=FilterDirection(
                policy="accept",
                rules=[FilterRule(action="drop", destination="10.0.0.0/8")],
            ),
        )
        driver, _, _, _ = _make_driver(tmp_path, filter=fc)
        assert driver.filter is fc

    def test_empty_filter_is_valid(self, tmp_path: Path):
        driver, _, _, _ = _make_driver(tmp_path, filter={})
        assert driver.filter is None

    def test_no_filter_is_valid(self, tmp_path: Path):
        driver, _, _, _ = _make_driver(tmp_path)
        assert driver.filter is None

    def test_invalid_egress_policy(self, tmp_path: Path):
        fc = {"egress": {"policy": "reject"}}
        with pytest.raises(ValueError, match="policy"):
            _make_driver(tmp_path, filter=fc)

    def test_invalid_ingress_policy(self, tmp_path: Path):
        fc = {"ingress": {"policy": "invalid"}}
        with pytest.raises(ValueError, match="policy"):
            _make_driver(tmp_path, filter=fc)

    def test_invalid_action(self, tmp_path: Path):
        fc = {"egress": {"rules": [{"action": "reject", "destination": "10.0.0.0/8"}]}}
        with pytest.raises(ValueError, match="action"):
            _make_driver(tmp_path, filter=fc)

    def test_missing_action(self, tmp_path: Path):
        fc = {"egress": {"rules": [{"destination": "10.0.0.0/8"}]}}
        with pytest.raises(TypeError):
            _make_driver(tmp_path, filter=fc)

    def test_invalid_destination(self, tmp_path: Path):
        fc = {"egress": {"rules": [{"action": "drop", "destination": "not-a-cidr"}]}}
        with pytest.raises(ValueError, match="destination"):
            _make_driver(tmp_path, filter=fc)

    def test_invalid_source(self, tmp_path: Path):
        fc = {"ingress": {"rules": [{"action": "accept", "source": "bad-addr"}]}}
        with pytest.raises(ValueError, match="source"):
            _make_driver(tmp_path, filter=fc)

    def test_port_without_protocol(self, tmp_path: Path):
        fc = {"egress": {"rules": [{"action": "drop", "port": 443}]}}
        with pytest.raises(ValueError, match="port requires protocol"):
            _make_driver(tmp_path, filter=fc)

    def test_invalid_protocol(self, tmp_path: Path):
        fc = {"egress": {"rules": [{"action": "drop", "port": 443, "protocol": "icmp"}]}}
        with pytest.raises(ValueError, match="protocol"):
            _make_driver(tmp_path, filter=fc)

    def test_egress_only(self, tmp_path: Path):
        fc = {"egress": {"policy": "drop"}}
        driver, _, _, _ = _make_driver(tmp_path, filter=fc)
        assert isinstance(driver.filter, FilterConfig)
        assert driver.filter.egress is not None
        assert driver.filter.egress.policy == "drop"
        assert driver.filter.ingress is None

    def test_ingress_only(self, tmp_path: Path):
        fc = {"ingress": {"policy": "accept", "rules": []}}
        driver, _, _, _ = _make_driver(tmp_path, filter=fc)
        assert isinstance(driver.filter, FilterConfig)
        assert driver.filter.ingress is not None
        assert driver.filter.ingress.policy == "accept"


class TestFilterPassedToNftables:
    """Tests that filter config is passed through to nftables calls."""

    def test_masquerade_passes_filter(self, tmp_path: Path):
        fc = FilterConfig(egress=FilterDirection(policy="drop"))
        _, _, mock_nft, _ = _make_driver(tmp_path, nat_mode="masquerade", filter=fc)
        call_args = mock_nft.apply_masquerade_rules.call_args
        assert call_args[1]["filter_config"] is fc

    def test_1to1_passes_filter(self, tmp_path: Path):
        fc = FilterConfig(ingress=FilterDirection(policy="drop"))
        leases = [
            {"mac": "aa:bb:cc:dd:ee:01", "ip": "192.168.100.10", "public_ip": "10.0.0.50"},
        ]
        _, _, mock_nft, _ = _make_driver(tmp_path, nat_mode="1to1", addresses=leases, filter=fc)
        call_args = mock_nft.apply_1to1_rules.call_args
        assert call_args[1]["filter_config"] is fc

    def test_empty_filter_passes_none(self, tmp_path: Path):
        _, _, mock_nft, _ = _make_driver(tmp_path, nat_mode="masquerade", filter={})
        mock_nft.apply_masquerade_rules.assert_called_once_with(
            "eth-dut", "eth-up", "192.168.100.0/24",
            table_name="jumpstarter_eth_dut",
            filter_config=None,
        )

    def test_no_filter_passes_none(self, tmp_path: Path):
        _, _, mock_nft, _ = _make_driver(tmp_path, nat_mode="masquerade")
        mock_nft.apply_masquerade_rules.assert_called_once_with(
            "eth-dut", "eth-up", "192.168.100.0/24",
            table_name="jumpstarter_eth_dut",
            filter_config=None,
        )


class TestTransactionalSetup:
    def test_cleanup_called_on_setup_failure(self, tmp_path: Path):
        with pytest.raises(RuntimeError, match="Cannot detect upstream"):
            with patch(f"{_DRIVER_MODULE}.sys") as mock_sys, \
                 patch(f"{_DRIVER_MODULE}.shutil") as mock_shutil, \
                 patch(f"{_DRIVER_MODULE}.iproute") as mock_iproute, \
                 patch(f"{_DRIVER_MODULE}.nftables") as mock_nft, \
                 patch(f"{_DRIVER_MODULE}.dnsmasq"):
                mock_sys.platform = "linux"
                mock_shutil.which.return_value = "/usr/bin/fake"
                mock_iproute.interface_exists.return_value = False
                mock_iproute.detect_upstream_interface.return_value = None
                mock_nft._table_name_for.return_value = "jumpstarter_eth0"
                from .driver import DutNetwork
                DutNetwork(
                    interface="eth0",
                    subnet="192.168.100.0/24",
                    gateway_ip="192.168.100.1",
                    upstream_interface=None,
                    nat_mode="masquerade",
                    state_dir=str(tmp_path),
                )  # type: ignore[missing-argument]


class TestDriverSetupMasquerade:
    def test_calls_configure_and_nat(self, tmp_path: Path):
        _, mock_ip, mock_nft, mock_dns = _make_driver(tmp_path, nat_mode="masquerade")
        mock_ip.nm_set_unmanaged.assert_called_once_with("eth-dut")
        mock_ip.configure_interface.assert_called_once_with("eth-dut", "192.168.100.1", 24)
        mock_ip.set_interface_forwarding.assert_any_call("eth-dut", True)
        mock_ip.set_interface_forwarding.assert_any_call("eth-up", True)
        mock_nft.apply_masquerade_rules.assert_called_once_with(
            "eth-dut", "eth-up", "192.168.100.0/24",
            table_name="jumpstarter_eth_dut",
            filter_config=None,
        )
        mock_dns.write_config.assert_called_once()
        mock_dns.start.assert_called_once()

    def test_saves_previous_forwarding_per_interface(self, tmp_path: Path):
        driver, mock_ip, _, _ = _make_driver(tmp_path, nat_mode="masquerade")
        assert mock_ip.get_interface_forwarding.call_count == 2

    def test_calls_ensure_filter_forward(self, tmp_path: Path):
        _, _, mock_nft, _ = _make_driver(tmp_path, nat_mode="masquerade")
        mock_nft.ensure_filter_forward.assert_called_once_with("eth-dut", "eth-up")


class TestDriverSetup1to1:
    def test_creates_aliases_and_rules(self, tmp_path: Path):
        leases = [
            {"mac": "aa:bb:cc:dd:ee:01", "ip": "192.168.100.10", "public_ip": "10.0.0.50"},
            {"mac": "aa:bb:cc:dd:ee:02", "ip": "192.168.100.11", "public_ip": "10.0.0.51"},
        ]
        _, mock_ip, mock_nft, _ = _make_driver(tmp_path, nat_mode="1to1", addresses=leases)
        mock_ip.add_ip_alias.assert_any_call("eth-up", "10.0.0.50", 24)
        mock_ip.add_ip_alias.assert_any_call("eth-up", "10.0.0.51", 24)
        assert mock_ip.add_ip_alias.call_count == 2
        expected_mappings = [
            {"private_ip": "192.168.100.10", "public_ip": "10.0.0.50"},
            {"private_ip": "192.168.100.11", "public_ip": "10.0.0.51"},
        ]
        mock_nft.apply_1to1_rules.assert_called_once_with(
            "eth-dut", "eth-up", expected_mappings, "192.168.100.0/24",
            table_name="jumpstarter_eth_dut",
            filter_config=None,
        )

    def test_skips_lease_without_public_ip(self, tmp_path: Path):
        leases = [
            {"mac": "aa:bb:cc:dd:ee:01", "ip": "192.168.100.10", "public_ip": "10.0.0.50"},
            {"mac": "aa:bb:cc:dd:ee:02", "ip": "192.168.100.11"},
        ]
        _, mock_ip, mock_nft, _ = _make_driver(tmp_path, nat_mode="1to1", addresses=leases)
        assert mock_ip.add_ip_alias.call_count == 1
        mappings = mock_nft.apply_1to1_rules.call_args[0][2]
        assert len(mappings) == 1


class TestDriverSetupDisabled:
    def test_skips_forwarding_and_nat(self, tmp_path: Path):
        _, mock_ip, mock_nft, _ = _make_driver(tmp_path, nat_mode="disabled")
        mock_ip.set_interface_forwarding.assert_not_called()
        mock_nft.ensure_filter_forward.assert_not_called()
        mock_nft.apply_masquerade_rules.assert_not_called()
        mock_nft.apply_1to1_rules.assert_not_called()

    def test_none_alias_same_as_disabled(self, tmp_path: Path):
        _, mock_ip, mock_nft, _ = _make_driver(tmp_path, nat_mode="none")
        mock_ip.set_interface_forwarding.assert_not_called()
        mock_nft.apply_masquerade_rules.assert_not_called()

    def test_interface_still_configured(self, tmp_path: Path):
        _, mock_ip, _, _ = _make_driver(tmp_path, nat_mode="disabled")
        mock_ip.configure_interface.assert_called_once()

    def test_upstream_not_required(self, tmp_path: Path):
        driver, mock_ip, _, _ = _make_driver(
            tmp_path, nat_mode="disabled", upstream_interface=None,
        )
        mock_ip.detect_upstream_interface.assert_not_called()


class TestDriverCleanup:
    def test_cleanup_masquerade(self, tmp_path: Path):
        driver, mock_ip, mock_nft, mock_dns = _make_driver(tmp_path, nat_mode="masquerade")
        with patch(f"{_DRIVER_MODULE}.iproute") as mock_ip2, \
             patch(f"{_DRIVER_MODULE}.nftables") as mock_nft2, \
             patch(f"{_DRIVER_MODULE}.dnsmasq") as mock_dns2:
            driver.cleanup()
            mock_nft2.flush_rules.assert_called_once_with(driver._table_name)
            mock_ip2.deconfigure_interface.assert_called_once_with("eth-dut")
            mock_ip2.nm_set_managed.assert_called_once_with("eth-dut")
            mock_dns2.stop.assert_called_once()

    def test_cleanup_1to1_removes_aliases(self, tmp_path: Path):
        leases = [
            {"mac": "aa:bb:cc:dd:ee:01", "ip": "192.168.100.10", "public_ip": "10.0.0.50"},
            {"mac": "aa:bb:cc:dd:ee:02", "ip": "192.168.100.11", "public_ip": "10.0.0.51"},
        ]
        driver, _, _, _ = _make_driver(tmp_path, nat_mode="1to1", addresses=leases)
        assert driver._added_aliases == {"10.0.0.50", "10.0.0.51"}
        with patch(f"{_DRIVER_MODULE}.iproute") as mock_ip2, \
             patch(f"{_DRIVER_MODULE}.nftables"), \
             patch(f"{_DRIVER_MODULE}.dnsmasq"):
            driver.cleanup()
            mock_ip2.remove_ip_alias.assert_any_call("eth-up", "10.0.0.50", 24)
            mock_ip2.remove_ip_alias.assert_any_call("eth-up", "10.0.0.51", 24)
            assert mock_ip2.remove_ip_alias.call_count == 2
        assert driver._added_aliases == set()

    def test_cleanup_restores_forwarding_per_interface(self, tmp_path: Path):
        driver, _, _, _ = _make_driver(tmp_path, nat_mode="masquerade")
        with patch(f"{_DRIVER_MODULE}.iproute") as mock_ip2, \
             patch(f"{_DRIVER_MODULE}.nftables"), \
             patch(f"{_DRIVER_MODULE}.dnsmasq"):
            driver.cleanup()
            mock_ip2.set_interface_forwarding.assert_any_call("eth-dut", False)
            mock_ip2.set_interface_forwarding.assert_any_call("eth-up", False)

    def test_cleanup_removes_filter_forward_rules(self, tmp_path: Path):
        driver, _, _, _ = _make_driver(tmp_path, nat_mode="masquerade")
        driver._fwd_rule_handles = [42, 43]
        with patch(f"{_DRIVER_MODULE}.iproute"), \
             patch(f"{_DRIVER_MODULE}.nftables") as mock_nft2, \
             patch(f"{_DRIVER_MODULE}.dnsmasq"):
            driver.cleanup()
            mock_nft2.remove_filter_forward.assert_called_once_with([42, 43])
        assert driver._fwd_rule_handles == []

    def test_cleanup_skips_filter_forward_when_no_handles(self, tmp_path: Path):
        driver, _, _, _ = _make_driver(tmp_path, nat_mode="masquerade")
        assert driver._fwd_rule_handles == []
        with patch(f"{_DRIVER_MODULE}.iproute"), \
             patch(f"{_DRIVER_MODULE}.nftables") as mock_nft2, \
             patch(f"{_DRIVER_MODULE}.dnsmasq"):
            driver.cleanup()
            mock_nft2.remove_filter_forward.assert_not_called()

    def test_cleanup_removes_state_directory(self, tmp_path: Path):
        driver, _, _, _ = _make_driver(tmp_path, nat_mode="masquerade")
        with patch(f"{_DRIVER_MODULE}.iproute"), \
             patch(f"{_DRIVER_MODULE}.nftables"), \
             patch(f"{_DRIVER_MODULE}.dnsmasq") as mock_dns2:
            driver.cleanup()
            mock_dns2.cleanup_state_dir.assert_called_once_with(tmp_path)


class TestDriverDnsEntries:
    def test_dns_entries_passed_to_dnsmasq(self, tmp_path: Path):
        entries = [{"hostname": "foo.local", "ip": "10.0.0.1"}]
        _, _, _, mock_dns = _make_driver(tmp_path, dns_entries=entries)
        write_call = mock_dns.write_config.call_args
        assert write_call.kwargs.get("dns_entries") == entries or \
            any(a == entries for a in write_call.args)

    def test_get_dns_entries(self, tmp_path: Path):
        entries = [{"hostname": "a.local", "ip": "1.2.3.4"}]
        driver, _, _, _ = _make_driver(tmp_path, dns_entries=entries)
        assert driver.get_dns_entries() == entries

    def test_add_dns_entry(self, tmp_path: Path):
        driver, _, _, _ = _make_driver(tmp_path)
        with patch(f"{_DRIVER_MODULE}.dnsmasq") as mock_dns:
            driver.add_dns_entry("new.local", "5.6.7.8")
            assert {"hostname": "new.local", "ip": "5.6.7.8"} in driver.dns_entries
            mock_dns.write_config.assert_called_once()
            mock_dns.reload_config.assert_called_once()

    def test_add_replaces_existing_hostname(self, tmp_path: Path):
        entries = [{"hostname": "a.local", "ip": "1.1.1.1"}]
        driver, _, _, _ = _make_driver(tmp_path, dns_entries=entries)
        with patch(f"{_DRIVER_MODULE}.dnsmasq"):
            driver.add_dns_entry("a.local", "2.2.2.2")
            assert len(driver.dns_entries) == 1
            assert driver.dns_entries[0]["ip"] == "2.2.2.2"

    def test_remove_dns_entry(self, tmp_path: Path):
        entries = [{"hostname": "a.local", "ip": "1.1.1.1"}]
        driver, _, _, _ = _make_driver(tmp_path, dns_entries=entries)
        with patch(f"{_DRIVER_MODULE}.dnsmasq"):
            driver.remove_dns_entry("a.local")
            assert driver.dns_entries == []


class TestDriverAddresses:
    def test_add_address_with_mac(self, tmp_path: Path):
        driver, _, _, _ = _make_driver(tmp_path)
        with patch(f"{_DRIVER_MODULE}.dnsmasq"):
            driver.add_address("192.168.100.50", mac="aa:bb:cc:dd:ee:ff", hostname="new-dut")
            assert any(entry.mac == "aa:bb:cc:dd:ee:ff" for entry in driver.addresses)

    def test_add_address_without_mac(self, tmp_path: Path):
        driver, _, _, _ = _make_driver(tmp_path)
        with patch(f"{_DRIVER_MODULE}.dnsmasq"):
            driver.add_address("192.168.100.50", hostname="nat-only", public_ip="10.0.0.50")
            entry = driver.addresses[0]
            assert entry.mac is None
            assert entry.public_ip == "10.0.0.50"

    def test_add_address_with_public_ip(self, tmp_path: Path):
        driver, _, _, _ = _make_driver(tmp_path)
        with patch(f"{_DRIVER_MODULE}.dnsmasq"):
            driver.add_address("192.168.100.50", mac="aa:bb:cc:dd:ee:ff", hostname="dut", public_ip="10.0.0.50")
            entry = driver.addresses[0]
            assert entry.public_ip == "10.0.0.50"

    def test_add_replaces_existing_ip(self, tmp_path: Path):
        addrs = [{"mac": "AA:BB:CC:DD:EE:FF", "ip": "192.168.100.10"}]
        driver, _, _, _ = _make_driver(tmp_path, addresses=addrs)
        with patch(f"{_DRIVER_MODULE}.dnsmasq"):
            driver.add_address("192.168.100.10", mac="11:22:33:44:55:66")
            assert len(driver.addresses) == 1
            assert driver.addresses[0].mac == "11:22:33:44:55:66"

    def test_remove_address(self, tmp_path: Path):
        addrs = [{"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.100.10"}]
        driver, _, _, _ = _make_driver(tmp_path, addresses=addrs)
        with patch(f"{_DRIVER_MODULE}.dnsmasq"):
            driver.remove_address("192.168.100.10")
            assert driver.addresses == []


class TestGet1to1Mappings:
    def test_extracts_mappings(self, tmp_path: Path):
        leases = [
            {"mac": "aa:bb:cc:dd:ee:01", "ip": "192.168.100.10", "public_ip": "10.0.0.50"},
            {"mac": "aa:bb:cc:dd:ee:02", "ip": "192.168.100.11"},
            {"mac": "aa:bb:cc:dd:ee:03", "ip": "192.168.100.12", "public_ip": "10.0.0.52"},
        ]
        driver, _, _, _ = _make_driver(
            tmp_path, nat_mode="1to1", addresses=leases,
        )
        mappings = driver._get_1to1_mappings()
        assert len(mappings) == 2
        assert {"private_ip": "192.168.100.10", "public_ip": "10.0.0.50"} in mappings
        assert {"private_ip": "192.168.100.12", "public_ip": "10.0.0.52"} in mappings


class TestResolveIp:
    """Tests for DutNetwork._resolve_ip() DNS resolution helper."""

    def test_valid_ipv4_returned_unchanged(self):
        from .driver import DutNetwork

        assert DutNetwork._resolve_ip("10.0.0.50") == "10.0.0.50"
        assert DutNetwork._resolve_ip("192.168.1.1") == "192.168.1.1"
        assert DutNetwork._resolve_ip("255.255.255.255") == "255.255.255.255"

    def test_hostname_resolved_to_ip(self):
        from .driver import DutNetwork

        fake_result = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.99", 0))]
        with patch(f"{_DRIVER_MODULE}.socket.getaddrinfo", return_value=fake_result):
            assert DutNetwork._resolve_ip("myhost.example.com") == "10.0.0.99"

    def test_unresolvable_hostname_raises(self):
        from .driver import DutNetwork

        with patch(f"{_DRIVER_MODULE}.socket.getaddrinfo", side_effect=socket.gaierror("Name or service not known")):
            with pytest.raises(ValueError, match="Cannot resolve hostname"):
                DutNetwork._resolve_ip("no-such-host.invalid")

    def test_empty_getaddrinfo_result_raises(self):
        from .driver import DutNetwork

        with patch(f"{_DRIVER_MODULE}.socket.getaddrinfo", return_value=[]):
            with pytest.raises(ValueError, match="Cannot resolve hostname"):
                DutNetwork._resolve_ip("empty-result.invalid")


class TestDnsNameIn1to1:
    """Integration tests: DNS hostnames in public_ip with 1:1 NAT setup."""

    def test_hostname_public_ip_resolved_during_setup(self, tmp_path: Path):
        fake_result = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.99", 0))]
        leases = [
            {"mac": "aa:bb:cc:dd:ee:01", "ip": "192.168.100.10", "public_ip": "myhost.example.com"},
        ]
        with patch(f"{_DRIVER_MODULE}.socket.getaddrinfo", return_value=fake_result) as mock_gai:
            driver, mock_ip, mock_nft, _ = _make_driver(
                tmp_path, nat_mode="1to1", addresses=leases,
            )
            mock_gai.assert_called_once_with("myhost.example.com", None, socket.AF_INET, socket.SOCK_STREAM)
            mock_ip.add_ip_alias.assert_called_once_with("eth-up", "10.0.0.99", 24)
            expected_mappings = [{"private_ip": "192.168.100.10", "public_ip": "10.0.0.99"}]
            mock_nft.apply_1to1_rules.assert_called_once_with(
                "eth-dut", "eth-up", expected_mappings, "192.168.100.0/24",
                table_name="jumpstarter_eth_dut",
                filter_config=None,
            )
            assert "10.0.0.99" in driver._added_aliases

    def test_mixed_ip_and_hostname(self, tmp_path: Path):
        fake_result = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.99", 0))]
        leases = [
            {"mac": "aa:bb:cc:dd:ee:01", "ip": "192.168.100.10", "public_ip": "10.0.0.50"},
            {"mac": "aa:bb:cc:dd:ee:02", "ip": "192.168.100.11", "public_ip": "myhost.example.com"},
        ]
        with patch(f"{_DRIVER_MODULE}.socket.getaddrinfo", return_value=fake_result):
            driver, mock_ip, mock_nft, _ = _make_driver(
                tmp_path, nat_mode="1to1", addresses=leases,
            )
            assert mock_ip.add_ip_alias.call_count == 2
            mock_ip.add_ip_alias.assert_any_call("eth-up", "10.0.0.50", 24)
            mock_ip.add_ip_alias.assert_any_call("eth-up", "10.0.0.99", 24)

    def test_unresolvable_hostname_raises_during_setup(self, tmp_path: Path):
        leases = [
            {"mac": "aa:bb:cc:dd:ee:01", "ip": "192.168.100.10", "public_ip": "bad-host.invalid"},
        ]
        with patch(f"{_DRIVER_MODULE}.socket.getaddrinfo", side_effect=socket.gaierror("fail")):
            with pytest.raises(ValueError, match="Cannot resolve hostname"):
                _make_driver(tmp_path, nat_mode="1to1", addresses=leases)


class TestAddressEntryValidation:
    def test_omitted_vlan_fields_ok(self, tmp_path: Path):
        driver, _, _, _ = _make_driver(
            tmp_path,
            addresses=[{"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.100.10"}],
        )
        entry = driver.addresses[0]
        assert entry.vlan_id is None
        assert entry.public_gateway is None

    def test_vlan_id_out_of_range(self, tmp_path: Path):
        with pytest.raises(ValueError, match="vlan_id"):
            _make_driver(
                tmp_path,
                addresses=[{"ip": "192.168.100.10", "vlan_id": 5000}],
            )

    def test_public_gateway_without_vlan_is_allowed(self, tmp_path: Path):
        driver, mock_ip, _, _ = _make_driver(
            tmp_path,
            addresses=[{
                "ip": "192.168.100.10",
                "public_gateway": "203.0.113.254",
            }],
        )
        table = int(ipaddress.IPv4Address("192.168.100.10"))
        mock_ip.create_vlan_interface.assert_not_called()
        mock_ip.add_policy_route.assert_called_once_with("203.0.113.254", "eth-up", table)
        mock_ip.add_ip_rule.assert_called_once_with("192.168.100.10", table, priority=100)
        assert driver._pbr_tables == {table}

    def test_public_gateway_without_vlan_requires_ipv4(self, tmp_path: Path):
        with pytest.raises(ValueError, match="valid IPv4"):
            _make_driver(
                tmp_path,
                addresses=[{"ip": "not-an-ip", "public_gateway": "10.0.0.1"}],
            )

    def test_reserved_vlan_table_rejected_with_gateway(self, tmp_path: Path):
        with pytest.raises(ValueError, match="reserved"):
            _make_driver(
                tmp_path,
                addresses=[{
                    "ip": "192.168.100.10",
                    "vlan_id": 254,
                    "public_gateway": "203.0.113.254",
                }],
            )

    def test_invalid_public_gateway(self, tmp_path: Path):
        with pytest.raises(ValueError, match="public_gateway"):
            _make_driver(
                tmp_path,
                addresses=[{
                    "ip": "192.168.100.10",
                    "vlan_id": 905,
                    "public_gateway": "not-an-ip",
                }],
            )


class TestVlanSetupMasquerade:
    def test_creates_vlan_and_sysctls(self, tmp_path: Path):
        addrs = [{
            "mac": "aa:bb:cc:dd:ee:ff",
            "ip": "192.168.100.125",
            "vlan_id": 905,
            "public_ip": "203.0.113.1",
            "public_gateway": "203.0.113.254",
        }]
        _, mock_ip, mock_nft, _ = _make_driver(tmp_path, nat_mode="masquerade", addresses=addrs)
        mock_ip.create_vlan_interface.assert_called_once_with("eth-up", 905)
        mock_ip.set_interface_forwarding.assert_any_call("eth-up.905", True)
        mock_ip.set_interface_rp_filter.assert_called_once_with("eth-up.905", 2)
        mock_ip.add_ip_alias.assert_called_once_with("eth-up.905", "203.0.113.1", 24)
        mock_ip.add_policy_route.assert_called_once_with("203.0.113.254", "eth-up.905", 905)
        mock_ip.add_ip_rule.assert_called_once_with("192.168.100.125", 905, priority=100)
        call_kwargs = mock_nft.apply_masquerade_rules.call_args[1]
        assert call_kwargs["nat_interfaces"] == ["eth-up.905"]

    def test_no_vlan_does_not_pass_nat_interfaces(self, tmp_path: Path):
        _, _, mock_nft, _ = _make_driver(tmp_path, nat_mode="masquerade")
        mock_nft.apply_masquerade_rules.assert_called_once_with(
            "eth-dut", "eth-up", "192.168.100.0/24",
            table_name="jumpstarter_eth_dut",
            filter_config=None,
        )

    def test_ensure_filter_forward_includes_vlan(self, tmp_path: Path):
        addrs = [{"ip": "192.168.100.125", "vlan_id": 905}]
        _, mock_ip, mock_nft, _ = _make_driver(tmp_path, nat_mode="masquerade", addresses=addrs)
        mock_nft.ensure_filter_forward.assert_called_once_with(
            "eth-dut", "eth-up", extra_interfaces=["eth-up.905"],
        )
        mock_ip.add_policy_route.assert_not_called()
        mock_ip.add_ip_rule.assert_not_called()

    def test_add_address_creates_vlan_and_pbr(self, tmp_path: Path):
        driver, _, _, _ = _make_driver(tmp_path)
        with patch(f"{_DRIVER_MODULE}.dnsmasq"), \
             patch(f"{_DRIVER_MODULE}.iproute") as mock_ip, \
             patch(f"{_DRIVER_MODULE}.nftables") as mock_nft:
            mock_nft.list_rules.return_value = ""
            driver.add_address(
                "192.168.100.50",
                public_ip="10.99.0.50",
                vlan_id=100,
                public_gateway="10.99.0.1",
            )
            mock_ip.create_vlan_interface.assert_called_with("eth-up", 100)
            mock_ip.add_policy_route.assert_called_with("10.99.0.1", "eth-up.100", 100)
            mock_ip.add_ip_rule.assert_called_with("192.168.100.50", 100, priority=100)
            mock_nft.apply_masquerade_rules.assert_called()

    def test_vlan_without_gateway_emits_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture):
        addrs = [{"ip": "192.168.100.125", "vlan_id": 905}]
        with caplog.at_level(logging.WARNING, logger="driver.DutNetwork"):
            _make_driver(tmp_path, nat_mode="masquerade", addresses=addrs)
        assert any(
            "no public_gateway" in rec.message and "192.168.100.125" in rec.message
            for rec in caplog.records
        )

    def test_vlan_with_gateway_does_not_warn(self, tmp_path: Path, caplog: pytest.LogCaptureFixture):
        addrs = [{
            "ip": "192.168.100.125",
            "vlan_id": 905,
            "public_gateway": "203.0.113.254",
        }]
        with caplog.at_level(logging.WARNING, logger="driver.DutNetwork"):
            _make_driver(tmp_path, nat_mode="masquerade", addresses=addrs)
        assert not any("no public_gateway" in rec.message for rec in caplog.records)


class TestVlanSetup1to1:
    def test_alias_and_rules_use_vlan_interface(self, tmp_path: Path):
        addrs = [{
            "mac": "aa:bb:cc:dd:ee:ff",
            "ip": "192.168.100.125",
            "public_ip": "203.0.113.1",
            "vlan_id": 905,
            "public_gateway": "203.0.113.254",
        }]
        driver, mock_ip, mock_nft, _ = _make_driver(tmp_path, nat_mode="1to1", addresses=addrs)
        mock_ip.create_vlan_interface.assert_called_once_with("eth-up", 905)
        mock_ip.add_ip_alias.assert_called_once_with("eth-up.905", "203.0.113.1", 24)
        mappings = mock_nft.apply_1to1_rules.call_args[0][2]
        assert mappings == [{
            "private_ip": "192.168.100.125",
            "public_ip": "203.0.113.1",
            "nat_interface": "eth-up.905",
        }]
        assert driver._added_aliases == {"203.0.113.1"}
        assert driver._alias_ifaces["203.0.113.1"] == "eth-up.905"

    def test_untagged_1to1_unchanged(self, tmp_path: Path):
        addrs = [{
            "mac": "aa:bb:cc:dd:ee:ff",
            "ip": "192.168.100.10",
            "public_ip": "10.0.0.50",
        }]
        _, mock_ip, mock_nft, _ = _make_driver(tmp_path, nat_mode="1to1", addresses=addrs)
        mock_ip.create_vlan_interface.assert_not_called()
        mock_ip.add_ip_alias.assert_called_once_with("eth-up", "10.0.0.50", 24)
        mappings = mock_nft.apply_1to1_rules.call_args[0][2]
        assert mappings == [{"private_ip": "192.168.100.10", "public_ip": "10.0.0.50"}]


class TestVlanCleanup:
    def test_cleanup_reverses_vlan_and_pbr(self, tmp_path: Path):
        addrs = [{
            "ip": "192.168.100.125",
            "public_ip": "203.0.113.1",
            "vlan_id": 905,
            "public_gateway": "203.0.113.254",
        }]
        driver, _, _, _ = _make_driver(tmp_path, nat_mode="1to1", addresses=addrs)
        with patch(f"{_DRIVER_MODULE}.iproute") as mock_ip2, \
             patch(f"{_DRIVER_MODULE}.nftables"), \
             patch(f"{_DRIVER_MODULE}.dnsmasq"):
            driver.cleanup()
            mock_ip2.remove_ip_alias.assert_called_once_with("eth-up.905", "203.0.113.1", 24)
            mock_ip2.delete_ip_rule.assert_called_once_with("192.168.100.125", 905)
            mock_ip2.flush_routing_table.assert_called_once_with(905)
            mock_ip2.delete_vlan_interface.assert_called_once_with("eth-up.905")
        assert driver._created_vlans == set()
        assert driver._pbr_rules == []
        assert driver._added_aliases == set()


class TestUntaggedPbr:
    def test_uses_private_ip_as_table_id(self, tmp_path: Path):
        table = int(ipaddress.IPv4Address("192.168.100.10"))
        addrs = [{
            "mac": "aa:bb:cc:dd:ee:ff",
            "ip": "192.168.100.10",
            "public_gateway": "10.0.0.1",
        }]
        _, mock_ip, mock_nft, _ = _make_driver(tmp_path, nat_mode="masquerade", addresses=addrs)
        mock_ip.create_vlan_interface.assert_not_called()
        mock_ip.add_policy_route.assert_called_once_with("10.0.0.1", "eth-up", table)
        mock_ip.add_ip_rule.assert_called_once_with("192.168.100.10", table, priority=100)
        mock_nft.apply_masquerade_rules.assert_called_once_with(
            "eth-dut", "eth-up", "192.168.100.0/24",
            table_name="jumpstarter_eth_dut",
            filter_config=None,
        )

    def test_cleanup_removes_untagged_pbr(self, tmp_path: Path):
        table = int(ipaddress.IPv4Address("192.168.100.10"))
        addrs = [{"ip": "192.168.100.10", "public_gateway": "10.0.0.1"}]
        driver, _, _, _ = _make_driver(tmp_path, nat_mode="masquerade", addresses=addrs)
        with patch(f"{_DRIVER_MODULE}.iproute") as mock_ip2, \
             patch(f"{_DRIVER_MODULE}.nftables"), \
             patch(f"{_DRIVER_MODULE}.dnsmasq"):
            driver.cleanup()
            mock_ip2.delete_ip_rule.assert_called_once_with("192.168.100.10", table)
            mock_ip2.flush_routing_table.assert_called_once_with(table)
            mock_ip2.delete_vlan_interface.assert_not_called()

    def test_add_address_applies_untagged_pbr(self, tmp_path: Path):
        table = int(ipaddress.IPv4Address("192.168.100.51"))
        driver, _, _, _ = _make_driver(tmp_path)
        with patch(f"{_DRIVER_MODULE}.dnsmasq"), \
             patch(f"{_DRIVER_MODULE}.iproute") as mock_ip, \
             patch(f"{_DRIVER_MODULE}.nftables"):
            driver.add_address("192.168.100.51", public_gateway="10.99.0.1")
            mock_ip.create_vlan_interface.assert_not_called()
            mock_ip.add_policy_route.assert_called_once_with("10.99.0.1", "eth-up", table)
            mock_ip.add_ip_rule.assert_called_once_with("192.168.100.51", table, priority=100)

    def test_reserved_untagged_table_rejected(self, tmp_path: Path):
        """Untagged PBR where int(IPv4) hits a reserved table should fail."""
        with pytest.raises(ValueError, match="reserved"):
            _make_driver(
                tmp_path,
                addresses=[{"ip": "0.0.0.253", "public_gateway": "10.0.0.1"}],
            )


class TestSyncNatRefreshesFwdHandles:
    """Verify _sync_nat re-creates FORWARD ACCEPT rules for new VLAN interfaces."""

    def test_runtime_add_address_refreshes_fwd_handles(self, tmp_path: Path):
        driver, _, _, _ = _make_driver(tmp_path)
        driver._fwd_rule_handles = [42, 43]
        with patch(f"{_DRIVER_MODULE}.dnsmasq"), \
             patch(f"{_DRIVER_MODULE}.iproute"), \
             patch(f"{_DRIVER_MODULE}.nftables") as mock_nft:
            mock_nft.list_rules.return_value = ""
            driver.add_address(
                "192.168.100.50",
                public_ip="10.99.0.50",
                vlan_id=100,
                public_gateway="10.99.0.1",
            )
            mock_nft.remove_filter_forward.assert_called_once_with([42, 43])
            mock_nft.ensure_filter_forward.assert_called_once_with(
                "eth-dut", "eth-up", extra_interfaces=["eth-up.100"],
            )

    def test_runtime_untagged_pbr_does_not_add_extra_ifaces(self, tmp_path: Path):
        driver, _, _, _ = _make_driver(tmp_path)
        driver._fwd_rule_handles = [42]
        with patch(f"{_DRIVER_MODULE}.dnsmasq"), \
             patch(f"{_DRIVER_MODULE}.iproute"), \
             patch(f"{_DRIVER_MODULE}.nftables") as mock_nft:
            mock_nft.list_rules.return_value = ""
            driver.add_address("192.168.100.51", public_gateway="10.99.0.1")
            mock_nft.remove_filter_forward.assert_called_once_with([42])
            call_kwargs = mock_nft.ensure_filter_forward.call_args
            assert call_kwargs == (("eth-dut", "eth-up"),)
