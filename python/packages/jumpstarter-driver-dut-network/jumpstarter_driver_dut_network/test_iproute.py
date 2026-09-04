import subprocess
from unittest.mock import patch

import pytest

from . import iproute


class TestDetectUpstreamInterface:
    def test_parses_default_route(self):
        fake = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="default via 10.0.0.1 dev eth0 proto static\n"
        )
        with patch.object(iproute, "_run", return_value=fake):
            assert iproute.detect_upstream_interface() == "eth0"

    def test_returns_none_on_failure(self):
        fake = subprocess.CompletedProcess(args=[], returncode=1, stdout="")
        with patch.object(iproute, "_run", return_value=fake):
            assert iproute.detect_upstream_interface() is None

    def test_returns_none_on_missing_dev(self):
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="unreachable default\n")
        with patch.object(iproute, "_run", return_value=fake):
            assert iproute.detect_upstream_interface() is None


class TestGetInterfaceAddresses:
    def test_parses_addresses(self):
        output = "2: eth0    inet 192.168.100.1/24 brd 192.168.100.255 scope global eth0\n"
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout=output)
        with patch.object(iproute, "_run", return_value=fake):
            assert iproute.get_interface_addresses("eth0") == ["192.168.100.1/24"]


class TestGetInterfacePrefixLen:
    def test_returns_prefix_len(self):
        output = "2: eth0    inet 10.99.0.2/24 brd 10.99.0.255 scope global eth0\n"
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout=output)
        with patch.object(iproute, "_run", return_value=fake):
            assert iproute.get_interface_prefix_len("eth0") == 24

    def test_returns_none_when_no_addresses(self):
        fake = subprocess.CompletedProcess(args=[], returncode=1, stdout="")
        with patch.object(iproute, "_run", return_value=fake):
            assert iproute.get_interface_prefix_len("eth0") is None


class TestConfigureInterface:
    def test_calls_correct_commands(self):
        with patch.object(iproute, "_run_priv") as mock:
            iproute.configure_interface("eth0", "10.0.0.1", 24)
            mock.assert_any_call(["ip", "addr", "flush", "dev", "eth0"])
            mock.assert_any_call(["ip", "addr", "add", "10.0.0.1/24", "dev", "eth0"])
            mock.assert_any_call(["ip", "link", "set", "eth0", "up"])
            assert mock.call_count == 3


class TestDeconfigureInterface:
    def test_calls_correct_commands(self):
        with patch.object(iproute, "_run_priv") as mock:
            iproute.deconfigure_interface("eth0")
            mock.assert_any_call(["ip", "addr", "flush", "dev", "eth0"], check=False)
            mock.assert_any_call(["ip", "link", "set", "eth0", "down"], check=False)
            assert mock.call_count == 2


class TestAddIpAlias:
    def test_adds_ip_when_not_present(self):
        with patch.object(iproute, "get_interface_addresses", return_value=[]), \
             patch.object(iproute, "_run_priv") as mock:
            iproute.add_ip_alias("eth0", "10.0.0.2", 24)
            mock.assert_called_once_with(["ip", "addr", "add", "10.0.0.2/24", "dev", "eth0"])

    def test_skips_add_when_already_present(self):
        with patch.object(iproute, "get_interface_addresses", return_value=["10.0.0.2/24"]), \
             patch.object(iproute, "_run_priv") as mock:
            iproute.add_ip_alias("eth0", "10.0.0.2", 24)
            mock.assert_not_called()


class TestNetworkManagerAwareness:
    def test_nm_set_unmanaged_skips_when_nm_absent(self):
        with patch.object(iproute, "is_nm_running", return_value=False), \
             patch.object(iproute, "_run_priv") as mock:
            iproute.nm_set_unmanaged("eth0")
            mock.assert_not_called()

    def test_nm_set_unmanaged_calls_nmcli_when_present(self):
        with patch.object(iproute, "is_nm_running", return_value=True), \
             patch.object(iproute, "_run_priv") as mock:
            iproute.nm_set_unmanaged("eth0")
            mock.assert_called_once_with(
                ["nmcli", "device", "set", "eth0", "managed", "no"], check=False
            )


class TestGetInterfaceForwarding:
    def test_returns_current_value(self):
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="1\n")
        with patch.object(iproute, "_run", return_value=fake):
            assert iproute.get_interface_forwarding("eth0") == "1"

    def test_returns_zero_on_failure(self):
        fake = subprocess.CompletedProcess(args=[], returncode=1, stdout="")
        with patch.object(iproute, "_run", return_value=fake):
            assert iproute.get_interface_forwarding("eth0") == "0"

    def test_uses_correct_sysctl_key(self):
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="0\n")
        with patch.object(iproute, "_run", return_value=fake) as mock_run:
            iproute.get_interface_forwarding("eth0")
            mock_run.assert_called_once_with(
                ["sysctl", "-n", "net.ipv4.conf.eth0.forwarding"], check=False
            )

    def test_set_interface_forwarding(self):
        with patch.object(iproute, "_run_priv") as mock:
            iproute.set_interface_forwarding("eth0", True)
            mock.assert_called_once_with(
                ["sysctl", "-w", "net.ipv4.conf.eth0.forwarding=1"]
            )

    def test_set_interface_forwarding_translates_vlan_dots(self):
        with patch.object(iproute, "_run_priv") as mock:
            iproute.set_interface_forwarding("end0.905", True)
            mock.assert_called_once_with(
                ["sysctl", "-w", "net.ipv4.conf.end0/905.forwarding=1"]
            )

    def test_get_interface_forwarding_translates_vlan_dots(self):
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="1\n")
        with patch.object(iproute, "_run", return_value=fake) as mock_run:
            iproute.get_interface_forwarding("end0.905")
            mock_run.assert_called_once_with(
                ["sysctl", "-n", "net.ipv4.conf.end0/905.forwarding"], check=False
            )


class TestRpFilter:
    def test_set_rp_filter_translates_vlan_dots(self):
        with patch.object(iproute, "_run_priv") as mock:
            iproute.set_interface_rp_filter("end0.905", 2)
            mock.assert_called_once_with(
                ["sysctl", "-w", "net.ipv4.conf.end0/905.rp_filter=2"]
            )


class TestVlanInterface:
    def test_vlan_subinterface_name(self):
        assert iproute.vlan_subinterface_name("end0", 905) == "end0.905"

    def test_create_vlan_interface(self):
        with patch.object(iproute, "interface_exists", return_value=False), \
             patch.object(iproute, "nm_set_unmanaged") as mock_nm, \
             patch.object(iproute, "_run_priv") as mock:
            name = iproute.create_vlan_interface("end0", 905)
            assert name == "end0.905"
            mock.assert_any_call(
                ["ip", "link", "add", "link", "end0", "name", "end0.905", "type", "vlan", "id", "905"]
            )
            mock.assert_any_call(["ip", "link", "set", "end0.905", "up"])
            mock_nm.assert_called_once_with("end0.905")

    def test_create_vlan_interface_idempotent(self):
        with patch.object(iproute, "interface_exists", return_value=True), \
             patch.object(iproute, "nm_set_unmanaged"), \
             patch.object(iproute, "_run_priv") as mock:
            iproute.create_vlan_interface("end0", 905)
            add_calls = [c for c in mock.call_args_list if c.args[0][:3] == ["ip", "link", "add"]]
            assert add_calls == []

    def test_create_vlan_rejects_long_name(self):
        with pytest.raises(ValueError, match="15-character"):
            iproute.create_vlan_interface("enx00e04c683af1", 905)

    def test_delete_vlan_interface(self):
        with patch.object(iproute, "_run_priv") as mock:
            iproute.delete_vlan_interface("end0.905")
            mock.assert_called_once_with(["ip", "link", "del", "end0.905"], check=False)


class TestPolicyRouting:
    def test_add_policy_route(self):
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch.object(iproute, "_run_priv", return_value=ok) as mock:
            iproute.add_policy_route("203.0.113.254", "end0.905", 905)
            mock.assert_called_once_with(
                ["ip", "route", "replace", "default", "via", "203.0.113.254",
                 "dev", "end0.905", "table", "905", "onlink"],
                check=False,
            )

    def test_add_policy_route_replaces_conflicting_route(self):
        """``replace`` is idempotent — a pre-existing route is overwritten."""
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch.object(iproute, "_run_priv", return_value=ok):
            iproute.add_policy_route("10.0.0.1", "eth0", 100)

    def test_add_policy_route_raises_on_failure(self):
        fail = subprocess.CompletedProcess(
            args=[], returncode=2, stdout="", stderr="Error: some failure\n",
        )
        with patch.object(iproute, "_run_priv", return_value=fail):
            with pytest.raises(RuntimeError, match="some failure"):
                iproute.add_policy_route("10.0.0.1", "eth0", 100)

    def test_add_policy_route_rejects_reserved_table(self):
        with pytest.raises(ValueError, match="reserved"):
            iproute.add_policy_route("10.0.0.1", "eth0", 254)

    def test_add_ip_rule(self):
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch.object(iproute, "_run_priv", return_value=ok) as mock:
            iproute.add_ip_rule("192.168.100.125", 905, priority=100)
            mock.assert_called_once_with(
                ["ip", "rule", "add", "from", "192.168.100.125",
                 "table", "905", "priority", "100"],
                check=False,
            )

    def test_add_ip_rule_idempotent_on_exists(self):
        exists = subprocess.CompletedProcess(
            args=[], returncode=2, stdout="", stderr="RTNETLINK answers: File exists\n",
        )
        with patch.object(iproute, "_run_priv", return_value=exists):
            iproute.add_ip_rule("192.168.100.10", 100)

    def test_add_ip_rule_raises_on_failure(self):
        fail = subprocess.CompletedProcess(
            args=[], returncode=2, stdout="", stderr="Error: some failure\n",
        )
        with patch.object(iproute, "_run_priv", return_value=fail):
            with pytest.raises(RuntimeError, match="some failure"):
                iproute.add_ip_rule("192.168.100.10", 100)

    def test_delete_ip_rule(self):
        with patch.object(iproute, "_run_priv") as mock:
            iproute.delete_ip_rule("192.168.100.125", 905)
            mock.assert_called_once_with(
                ["ip", "rule", "del", "from", "192.168.100.125", "table", "905"],
                check=False,
            )

    def test_flush_routing_table(self):
        with patch.object(iproute, "_run_priv") as mock:
            iproute.flush_routing_table(905)
            mock.assert_called_once_with(
                ["ip", "route", "flush", "table", "905"], check=False
            )
