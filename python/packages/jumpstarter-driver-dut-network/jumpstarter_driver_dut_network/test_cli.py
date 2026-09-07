"""CLI tests for the DUT Network driver.

Tests the Click CLI interface exposed by DutNetworkClient, following the
same pattern as jumpstarter-driver-network and jumpstarter-driver-pyserial.
Mocks the client methods to avoid needing a real driver / root privileges.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from jumpstarter.common.utils import serve

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
        mock_iproute.interface_exists.return_value = True
        mock_iproute.get_interface_addresses.return_value = ["192.168.100.1/24"]
        mock_iproute.get_interface_forwarding.return_value = "0"
        mock_iproute.get_interface_prefix_len.return_value = 24
        mock_nftables.ensure_filter_forward.return_value = []
        mock_nftables.list_rules.return_value = "table ip jumpstarter_eth_dut { masquerade }"
        mock_nftables._table_name_for.return_value = "jumpstarter_eth_dut"
        driver = DutNetwork(**params)  # type: ignore[missing-argument]

    return driver


def _make_client(tmp_path, **overrides):
    """Create a DutNetworkClient by serving a mocked driver."""
    driver = _make_driver(tmp_path, **overrides)
    return serve(driver)


@pytest.fixture
def runner():
    return CliRunner()


class TestCliHelp:
    def test_base_help(self, tmp_path: Path, runner: CliRunner):
        with _make_client(tmp_path) as client:
            result = runner.invoke(client.cli(), ["--help"])
            assert result.exit_code == 0
            assert "DUT Network Isolation" in result.output

    def test_all_commands_listed(self, tmp_path: Path, runner: CliRunner):
        with _make_client(tmp_path) as client:
            result = runner.invoke(client.cli(), ["--help"])
            for cmd in ("status", "leases", "get-ip", "add-address", "remove-address",
                         "nat-rules", "dns-entries", "add-dns", "remove-dns"):
                assert cmd in result.output, f"Command {cmd!r} not in help output"


class TestStatusCommand:
    def test_outputs_json(self, tmp_path: Path, runner: CliRunner):
        with _make_client(tmp_path) as client:
            with patch.object(client, "status", return_value={
                "interface": "eth-dut",
                "subnet": "192.168.100.0/24",
                "nat_mode": "masquerade",
                "interface_status": {"name": "eth-dut", "addresses": ["192.168.100.1/24"]},
            }):
                result = runner.invoke(client.cli(), ["status"])
                assert result.exit_code == 0
                data = json.loads(result.output)
                assert data["interface"] == "eth-dut"
                assert data["subnet"] == "192.168.100.0/24"
                assert data["nat_mode"] == "masquerade"

    def test_interface_status_present(self, tmp_path: Path, runner: CliRunner):
        with _make_client(tmp_path) as client:
            with patch.object(client, "status", return_value={
                "interface": "eth-dut",
                "interface_status": {"name": "eth-dut"},
            }):
                result = runner.invoke(client.cli(), ["status"])
                data = json.loads(result.output)
                assert "interface_status" in data
                assert data["interface_status"]["name"] == "eth-dut"


class TestLeasesCommand:
    def test_no_leases_message(self, tmp_path: Path, runner: CliRunner):
        with _make_client(tmp_path) as client:
            with patch.object(client, "get_leases", return_value=[]):
                result = runner.invoke(client.cli(), ["leases"])
                assert result.exit_code == 0
                assert "No active DHCP leases" in result.output

    def test_displays_leases(self, tmp_path: Path, runner: CliRunner):
        leases = [
            {"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.100.10", "hostname": "dut1", "expiry": "2099-01-01"},
        ]
        with _make_client(tmp_path) as client:
            with patch.object(client, "get_leases", return_value=leases):
                result = runner.invoke(client.cli(), ["leases"])
                assert result.exit_code == 0
                assert "aa:bb:cc:dd:ee:ff" in result.output
                assert "192.168.100.10" in result.output
                assert "dut1" in result.output

    def test_displays_table_header(self, tmp_path: Path, runner: CliRunner):
        leases = [
            {"mac": "00:11:22:33:44:55", "ip": "192.168.100.20", "hostname": "", "expiry": "static"},
        ]
        with _make_client(tmp_path) as client:
            with patch.object(client, "get_leases", return_value=leases):
                result = runner.invoke(client.cli(), ["leases"])
                assert "MAC" in result.output
                assert "IP" in result.output
                assert "Hostname" in result.output


class TestGetIpCommand:
    def test_returns_ip_for_known_mac(self, tmp_path: Path, runner: CliRunner):
        with _make_client(tmp_path) as client:
            with patch.object(client, "get_dut_ip", return_value="192.168.100.10"):
                result = runner.invoke(client.cli(), ["get-ip", "aa:bb:cc:dd:ee:ff"])
                assert result.exit_code == 0
                assert "192.168.100.10" in result.output

    def test_error_for_unknown_mac(self, tmp_path: Path, runner: CliRunner):
        with _make_client(tmp_path) as client:
            with patch.object(client, "get_dut_ip", return_value=None):
                result = runner.invoke(client.cli(), ["get-ip", "ff:ff:ff:ff:ff:ff"])
                assert result.exit_code != 0
                assert "No lease found" in result.output

    def test_requires_mac_argument(self, tmp_path: Path, runner: CliRunner):
        with _make_client(tmp_path) as client:
            result = runner.invoke(client.cli(), ["get-ip"])
            assert result.exit_code != 0


class TestAddAddressCommand:
    def test_add_address_output(self, tmp_path: Path, runner: CliRunner):
        with _make_client(tmp_path) as client:
            with patch.object(client, "add_address"):
                result = runner.invoke(client.cli(), ["add-address", "192.168.100.50", "-m", "aa:bb:cc:dd:ee:ff"])
                assert result.exit_code == 0
                assert "Added address" in result.output
                assert "192.168.100.50" in result.output

    def test_add_address_without_mac(self, tmp_path: Path, runner: CliRunner):
        with _make_client(tmp_path) as client:
            with patch.object(client, "add_address") as mock_add:
                result = runner.invoke(client.cli(), [
                    "add-address", "192.168.100.50", "--public-ip", "10.0.0.50",
                ])
                assert result.exit_code == 0
                mock_add.assert_called_once_with(
                    "192.168.100.50", None, "", "10.0.0.50", None, None,
                )

    def test_add_address_with_hostname(self, tmp_path: Path, runner: CliRunner):
        with _make_client(tmp_path) as client:
            with patch.object(client, "add_address") as mock_add:
                result = runner.invoke(
                    client.cli(),
                    ["add-address", "192.168.100.50", "-m", "aa:bb:cc:dd:ee:ff", "-n", "my-dut"],
                )
                assert result.exit_code == 0
                mock_add.assert_called_once_with(
                    "192.168.100.50", "aa:bb:cc:dd:ee:ff", "my-dut", None, None, None,
                )

    def test_add_address_with_vlan_options(self, tmp_path: Path, runner: CliRunner):
        with _make_client(tmp_path) as client:
            with patch.object(client, "add_address") as mock_add:
                result = runner.invoke(client.cli(), [
                    "add-address", "192.168.100.50",
                    "--public-ip", "203.0.113.1",
                    "--vlan-id", "905",
                    "--public-gateway", "203.0.113.254",
                ])
                assert result.exit_code == 0
                mock_add.assert_called_once_with(
                    "192.168.100.50", None, "", "203.0.113.1", 905, "203.0.113.254",
                )

    def test_requires_ip_argument(self, tmp_path: Path, runner: CliRunner):
        with _make_client(tmp_path) as client:
            result = runner.invoke(client.cli(), ["add-address"])
            assert result.exit_code != 0


class TestRemoveAddressCommand:
    def test_remove_address_output(self, tmp_path: Path, runner: CliRunner):
        with _make_client(tmp_path) as client:
            with patch.object(client, "remove_address"):
                result = runner.invoke(client.cli(), ["remove-address", "192.168.100.50"])
                assert result.exit_code == 0
                assert "Removed address" in result.output
                assert "192.168.100.50" in result.output

    def test_calls_client_method(self, tmp_path: Path, runner: CliRunner):
        with _make_client(tmp_path) as client:
            with patch.object(client, "remove_address") as mock_rm:
                runner.invoke(client.cli(), ["remove-address", "192.168.100.50"])
                mock_rm.assert_called_once_with("192.168.100.50")

    def test_requires_ip_argument(self, tmp_path: Path, runner: CliRunner):
        with _make_client(tmp_path) as client:
            result = runner.invoke(client.cli(), ["remove-address"])
            assert result.exit_code != 0


class TestNatRulesCommand:
    def test_displays_rules(self, tmp_path: Path, runner: CliRunner):
        with _make_client(tmp_path) as client:
            with patch.object(client, "get_nat_rules", return_value="table ip jmp { masquerade }"):
                result = runner.invoke(client.cli(), ["nat-rules"])
                assert result.exit_code == 0
                assert "masquerade" in result.output

    def test_no_rules_message(self, tmp_path: Path, runner: CliRunner):
        with _make_client(tmp_path) as client:
            with patch.object(client, "get_nat_rules", return_value=""):
                result = runner.invoke(client.cli(), ["nat-rules"])
                assert result.exit_code == 0
                assert "No active NAT rules" in result.output


class TestDnsEntriesCommand:
    def test_no_entries_message(self, tmp_path: Path, runner: CliRunner):
        with _make_client(tmp_path) as client:
            with patch.object(client, "get_dns_entries", return_value=[]):
                result = runner.invoke(client.cli(), ["dns-entries"])
                assert result.exit_code == 0
                assert "No DNS entries configured" in result.output

    def test_displays_entries(self, tmp_path: Path, runner: CliRunner):
        entries = [{"hostname": "myhost.local", "ip": "10.0.0.1"}]
        with _make_client(tmp_path) as client:
            with patch.object(client, "get_dns_entries", return_value=entries):
                result = runner.invoke(client.cli(), ["dns-entries"])
                assert result.exit_code == 0
                assert "myhost.local" in result.output
                assert "10.0.0.1" in result.output

    def test_displays_table_header(self, tmp_path: Path, runner: CliRunner):
        entries = [{"hostname": "h.local", "ip": "1.2.3.4"}]
        with _make_client(tmp_path) as client:
            with patch.object(client, "get_dns_entries", return_value=entries):
                result = runner.invoke(client.cli(), ["dns-entries"])
                assert "Hostname" in result.output
                assert "IP" in result.output


class TestAddDnsCommand:
    def test_add_dns_output(self, tmp_path: Path, runner: CliRunner):
        with _make_client(tmp_path) as client:
            with patch.object(client, "add_dns_entry"):
                result = runner.invoke(client.cli(), ["add-dns", "new.local", "10.0.0.99"])
                assert result.exit_code == 0
                assert "Added DNS entry" in result.output
                assert "new.local" in result.output
                assert "10.0.0.99" in result.output

    def test_calls_client_method(self, tmp_path: Path, runner: CliRunner):
        with _make_client(tmp_path) as client:
            with patch.object(client, "add_dns_entry") as mock_add:
                runner.invoke(client.cli(), ["add-dns", "x.local", "9.8.7.6"])
                mock_add.assert_called_once_with("x.local", "9.8.7.6")

    def test_requires_hostname_and_ip(self, tmp_path: Path, runner: CliRunner):
        with _make_client(tmp_path) as client:
            result = runner.invoke(client.cli(), ["add-dns", "only-hostname"])
            assert result.exit_code != 0


class TestRemoveDnsCommand:
    def test_remove_dns_output(self, tmp_path: Path, runner: CliRunner):
        with _make_client(tmp_path) as client:
            with patch.object(client, "remove_dns_entry"):
                result = runner.invoke(client.cli(), ["remove-dns", "old.local"])
                assert result.exit_code == 0
                assert "Removed DNS entry" in result.output
                assert "old.local" in result.output

    def test_calls_client_method(self, tmp_path: Path, runner: CliRunner):
        with _make_client(tmp_path) as client:
            with patch.object(client, "remove_dns_entry") as mock_rm:
                runner.invoke(client.cli(), ["remove-dns", "gone.local"])
                mock_rm.assert_called_once_with("gone.local")

    def test_requires_hostname_argument(self, tmp_path: Path, runner: CliRunner):
        with _make_client(tmp_path) as client:
            result = runner.invoke(client.cli(), ["remove-dns"])
            assert result.exit_code != 0
