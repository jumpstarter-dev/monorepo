import subprocess
from unittest.mock import patch

import pytest

from . import nftables
from .driver import FilterConfig, FilterDirection, FilterRule


class TestBuildForwardChain:
    """Unit tests for the _build_forward_chain() helper."""

    def test_no_filter_produces_legacy_rules(self):
        result = nftables._build_forward_chain("br-jmp0", "eth0")
        assert 'iifname "br-jmp0" oifname "eth0" accept' in result
        assert 'iifname "eth0" oifname "br-jmp0" ct state related,established accept' in result
        # Should NOT have a standalone ct state line at the top
        lines = result.strip().splitlines()
        assert not any(line.strip() == "ct state related,established accept" for line in lines)

    def test_no_filter_with_extra_rules(self):
        extras = ['        iifname "eth0" oifname "br-jmp0" ip daddr 192.168.100.10 accept']
        result = nftables._build_forward_chain("br-jmp0", "eth0", extra_forward_rules=extras)
        assert "ip daddr 192.168.100.10 accept" in result
        # Extras come after the conntrack line in legacy mode
        lines = result.strip().splitlines()
        ct_idx = next(i for i, ln in enumerate(lines) if "ct state" in ln)
        extra_idx = next(i for i, ln in enumerate(lines) if "192.168.100.10" in ln)
        assert extra_idx > ct_idx

    def test_egress_only_drop_rfc1918(self):
        fc = FilterConfig(
            egress=FilterDirection(
                policy="accept",
                rules=[
                    FilterRule(action="drop", destination="10.0.0.0/8"),
                    FilterRule(action="drop", destination="172.16.0.0/12"),
                ],
            ),
        )
        result = nftables._build_forward_chain("br-jmp0", "eth0", filter_config=fc)
        # Conntrack at the top
        lines = result.strip().splitlines()
        rule_lines = [ln for ln in lines if ln.strip() and not ln.strip().startswith(("chain", "type", "}"))]
        assert rule_lines[0].strip() == "ct state related,established accept"
        # Egress rules present
        assert 'ip daddr 10.0.0.0/8 drop' in result
        assert 'ip daddr 172.16.0.0/12 drop' in result
        # Egress catch-all
        assert 'iifname "br-jmp0" oifname "eth0" accept' in result
        # Ingress catch-all (default accept)
        assert 'iifname "eth0" oifname "br-jmp0" accept' in result

    def test_ingress_only_with_port_protocol(self):
        fc = FilterConfig(
            ingress=FilterDirection(
                policy="drop",
                rules=[
                    FilterRule(action="accept", source="198.51.100.0/24", port=22, protocol="tcp"),
                ],
            ),
        )
        result = nftables._build_forward_chain("br-jmp0", "eth0", filter_config=fc)
        assert "ip saddr 198.51.100.0/24 tcp dport 22 accept" in result
        # Ingress catch-all should be drop
        lines = result.strip().splitlines()
        # The last rule before "}" should be the ingress catch-all
        ingress_catchall = [ln for ln in lines if 'iifname "eth0" oifname "br-jmp0"' in ln and "saddr" not in ln]
        assert any("drop" in ln for ln in ingress_catchall)

    def test_combined_egress_ingress(self):
        fc = FilterConfig(
            egress=FilterDirection(
                policy="accept",
                rules=[FilterRule(action="drop", destination="10.0.0.0/8")],
            ),
            ingress=FilterDirection(
                policy="drop",
                rules=[FilterRule(action="accept", source="198.51.100.0/24", port=22, protocol="tcp")],
            ),
        )
        result = nftables._build_forward_chain("br-jmp0", "eth0", filter_config=fc)
        lines = result.strip().splitlines()
        # Find positions of key lines
        egress_drop = next(i for i, ln in enumerate(lines) if "10.0.0.0/8" in ln)
        egress_catchall = next(
            i for i, ln in enumerate(lines)
            if 'iifname "br-jmp0"' in ln and "daddr" not in ln
        )
        ingress_rule = next(i for i, ln in enumerate(lines) if "198.51.100.0/24" in ln)
        ingress_catchall = next(
            i for i, ln in enumerate(lines)
            if 'iifname "eth0"' in ln and "saddr" not in ln and "ct state" not in ln
        )
        # Order: egress rules < egress catch-all < ingress rules < ingress catch-all
        assert egress_drop < egress_catchall < ingress_rule < ingress_catchall

    def test_filter_with_extra_forward_rules(self):
        """Extra forward rules (1:1 NAT) are placed between egress and ingress catch-all rules."""
        fc = FilterConfig(
            egress=FilterDirection(policy="accept"),
            ingress=FilterDirection(policy="drop"),
        )
        extras = ['        iifname "eth0" oifname "br-jmp0" ip daddr 192.168.100.10 accept']
        result = nftables._build_forward_chain("br-jmp0", "eth0", filter_config=fc, extra_forward_rules=extras)
        lines = result.strip().splitlines()
        egress_idx = next(
            i for i, ln in enumerate(lines)
            if 'iifname "br-jmp0"' in ln and "accept" in ln
        )
        extra_idx = next(i for i, ln in enumerate(lines) if "192.168.100.10" in ln)
        ingress_idx = next(
            i for i, ln in enumerate(lines)
            if 'iifname "eth0" oifname "br-jmp0"' in ln and "drop" in ln and "daddr" not in ln
        )
        assert egress_idx < extra_idx < ingress_idx

    def test_egress_rule_without_destination(self):
        """An egress rule without destination matches all destinations."""
        fc = FilterConfig(
            egress=FilterDirection(
                policy="drop",
                rules=[FilterRule(action="accept", protocol="tcp", port=443)],
            ),
        )
        result = nftables._build_forward_chain("br-jmp0", "eth0", filter_config=fc)
        assert 'iifname "br-jmp0" oifname "eth0" tcp dport 443 accept' in result

    def test_ingress_rule_without_source(self):
        """An ingress rule without source matches all sources."""
        fc = FilterConfig(
            ingress=FilterDirection(
                policy="drop",
                rules=[FilterRule(action="accept", protocol="udp", port=53)],
            ),
        )
        result = nftables._build_forward_chain("br-jmp0", "eth0", filter_config=fc)
        assert 'iifname "eth0" oifname "br-jmp0" udp dport 53 accept' in result

    def test_default_policies_are_accept(self):
        """When policies are omitted, they default to accept."""
        fc = FilterConfig(egress=FilterDirection(), ingress=FilterDirection())
        result = nftables._build_forward_chain("br-jmp0", "eth0", filter_config=fc)
        lines = result.strip().splitlines()
        # Both catch-all lines should be accept
        catchalls = [ln for ln in lines if ("br-jmp0" in ln or "eth0" in ln) and "ct state" not in ln]
        assert all("accept" in ln for ln in catchalls)


class TestMasqueradeRuleset:
    def test_ruleset_contains_expected_elements(self):
        with patch.object(nftables, "_run_nft"), \
             patch.object(nftables, "_load_ruleset") as mock_load:
            nftables.apply_masquerade_rules("br-jmp0", "eth0", "192.168.100.0/24")
            ruleset = mock_load.call_args[0][0]
            assert "masquerade" in ruleset
            assert "br-jmp0" in ruleset
            assert "eth0" in ruleset
            assert "192.168.100.0/24" in ruleset
            assert "postrouting" in ruleset
            assert "forward" in ruleset

    def test_uses_custom_table_name(self):
        with patch.object(nftables, "_run_nft"), \
             patch.object(nftables, "_load_ruleset") as mock_load:
            nftables.apply_masquerade_rules("br-jmp0", "eth0", "192.168.100.0/24", table_name="my_table")
            ruleset = mock_load.call_args[0][0]
            assert "table ip my_table" in ruleset

    def test_with_filter_config(self):
        fc = FilterConfig(
            egress=FilterDirection(
                policy="accept",
                rules=[FilterRule(action="drop", destination="10.0.0.0/8")],
            ),
            ingress=FilterDirection(policy="drop"),
        )
        with patch.object(nftables, "_run_nft"), \
             patch.object(nftables, "_load_ruleset") as mock_load:
            nftables.apply_masquerade_rules(
                "br-jmp0", "eth0", "192.168.100.0/24", filter_config=fc,
            )
            ruleset = mock_load.call_args[0][0]
            assert "ct state related,established accept" in ruleset
            assert "ip daddr 10.0.0.0/8 drop" in ruleset
            assert "masquerade" in ruleset

    def test_without_filter_backward_compatible(self):
        with patch.object(nftables, "_run_nft"), \
             patch.object(nftables, "_load_ruleset") as mock_load:
            nftables.apply_masquerade_rules("br-jmp0", "eth0", "192.168.100.0/24")
            ruleset = mock_load.call_args[0][0]
            # Legacy rules present
            assert 'iifname "br-jmp0" oifname "eth0" accept' in ruleset
            assert 'iifname "eth0" oifname "br-jmp0" ct state related,established accept' in ruleset


class TestOneToOneRuleset:
    def test_single_mapping(self):
        mappings = [{"private_ip": "192.168.100.10", "public_ip": "10.0.0.50"}]
        with patch.object(nftables, "_run_nft"), \
             patch.object(nftables, "_load_ruleset") as mock_load:
            nftables.apply_1to1_rules("br-jmp0", "eth0", mappings, "192.168.100.0/24")
            ruleset = mock_load.call_args[0][0]
            assert "dnat to 192.168.100.10" in ruleset
            assert "snat to 10.0.0.50" in ruleset
            assert "masquerade" in ruleset

    def test_multiple_mappings(self):
        mappings = [
            {"private_ip": "192.168.100.10", "public_ip": "10.0.0.50"},
            {"private_ip": "192.168.100.11", "public_ip": "10.0.0.51"},
        ]
        with patch.object(nftables, "_run_nft"), \
             patch.object(nftables, "_load_ruleset") as mock_load:
            nftables.apply_1to1_rules("br-jmp0", "eth0", mappings, "192.168.100.0/24")
            ruleset = mock_load.call_args[0][0]
            assert "dnat to 192.168.100.10" in ruleset
            assert "dnat to 192.168.100.11" in ruleset
            assert "snat to 10.0.0.50" in ruleset
            assert "snat to 10.0.0.51" in ruleset
            assert ruleset.count("dnat to") == 4  # 2x prerouting + 2x output
            assert ruleset.count("snat to") == 2

    def test_empty_mappings(self):
        with patch.object(nftables, "_run_nft"), \
             patch.object(nftables, "_load_ruleset") as mock_load:
            nftables.apply_1to1_rules("br-jmp0", "eth0", [], "192.168.100.0/24")
            ruleset = mock_load.call_args[0][0]
            assert "masquerade" in ruleset
            assert "dnat to" not in ruleset

    def test_output_chain_for_hairpin_nat(self):
        """The output chain enables the exporter host to reach DUTs via public IPs."""
        mappings = [{"private_ip": "192.168.100.10", "public_ip": "10.0.0.50"}]
        with patch.object(nftables, "_run_nft"), \
             patch.object(nftables, "_load_ruleset") as mock_load:
            nftables.apply_1to1_rules("br-jmp0", "eth0", mappings, "192.168.100.0/24")
            ruleset = mock_load.call_args[0][0]
            assert "chain output" in ruleset
            assert "type nat hook output priority dstnat" in ruleset
            assert "ip daddr 10.0.0.50 dnat to 192.168.100.10" in ruleset

    def test_with_filter_config(self):
        mappings = [{"private_ip": "192.168.100.10", "public_ip": "10.0.0.50"}]
        fc = FilterConfig(
            egress=FilterDirection(policy="accept"),
            ingress=FilterDirection(policy="drop"),
        )
        with patch.object(nftables, "_run_nft"), \
             patch.object(nftables, "_load_ruleset") as mock_load:
            nftables.apply_1to1_rules(
                "br-jmp0", "eth0", mappings, "192.168.100.0/24",
                filter_config=fc,
            )
            ruleset = mock_load.call_args[0][0]
            # Conntrack at top of forward chain
            assert "ct state related,established accept" in ruleset
            # Per-mapping forward rule still present
            assert "ip daddr 192.168.100.10 accept" in ruleset
            # NAT rules still present
            assert "dnat to 192.168.100.10" in ruleset
            assert "snat to 10.0.0.50" in ruleset

    def test_without_filter_backward_compatible(self):
        mappings = [{"private_ip": "192.168.100.10", "public_ip": "10.0.0.50"}]
        with patch.object(nftables, "_run_nft"), \
             patch.object(nftables, "_load_ruleset") as mock_load:
            nftables.apply_1to1_rules("br-jmp0", "eth0", mappings, "192.168.100.0/24")
            ruleset = mock_load.call_args[0][0]
            assert 'iifname "br-jmp0" oifname "eth0" accept' in ruleset
            assert 'iifname "eth0" oifname "br-jmp0" ct state related,established accept' in ruleset
            assert "ip daddr 192.168.100.10 accept" in ruleset


class TestVlanNatRules:
    """VLAN sub-interface names must appear in nftables instead of the parent."""

    def test_masquerade_uses_vlan_interface(self):
        with patch.object(nftables, "_run_nft"), \
             patch.object(nftables, "_load_ruleset") as mock_load:
            nftables.apply_masquerade_rules(
                "enp1s0u1", "end0", "192.168.100.0/24",
                nat_interfaces=["end0.905"],
            )
            ruleset = mock_load.call_args[0][0]
            assert 'oifname "end0.905" ip saddr 192.168.100.0/24 masquerade' in ruleset
            assert 'iifname "enp1s0u1" oifname "end0.905" accept' in ruleset
            assert 'iifname "end0.905" oifname "enp1s0u1" ct state related,established accept' in ruleset
            assert 'oifname "end0" ip saddr' not in ruleset

    def test_1to1_uses_vlan_interface(self):
        mappings = [{
            "private_ip": "192.168.100.125",
            "public_ip": "203.0.113.1",
            "nat_interface": "end0.905",
        }]
        with patch.object(nftables, "_run_nft"), \
             patch.object(nftables, "_load_ruleset") as mock_load:
            nftables.apply_1to1_rules("enp1s0u1", "end0", mappings, "192.168.100.0/24")
            ruleset = mock_load.call_args[0][0]
            assert 'iifname "end0.905" ip daddr 203.0.113.1 dnat to 192.168.100.125' in ruleset
            assert 'ip saddr 192.168.100.125 oifname "end0.905" snat to 203.0.113.1' in ruleset
            assert 'iifname "end0.905" oifname "enp1s0u1" ip daddr 192.168.100.125 accept' in ruleset
            assert 'iifname "enp1s0u1" oifname "end0.905" accept' in ruleset
            assert 'iifname "end0.905" oifname "enp1s0u1" ct state related,established accept' in ruleset
            # Untagged parent is kept for unmapped-DUT masquerade fallback
            assert 'oifname "end0" ip saddr 192.168.100.0/24 masquerade' in ruleset

    def test_1to1_without_nat_interface_stays_on_upstream(self):
        mappings = [{"private_ip": "192.168.100.10", "public_ip": "10.0.0.50"}]
        with patch.object(nftables, "_run_nft"), \
             patch.object(nftables, "_load_ruleset") as mock_load:
            nftables.apply_1to1_rules("br-jmp0", "eth0", mappings, "192.168.100.0/24")
            ruleset = mock_load.call_args[0][0]
            assert 'iifname "eth0" ip daddr 10.0.0.50 dnat to 192.168.100.10' in ruleset
            assert 'oifname "eth0" snat to 10.0.0.50' in ruleset

    def test_masquerade_without_nat_interfaces_unchanged(self):
        with patch.object(nftables, "_run_nft"), \
             patch.object(nftables, "_load_ruleset") as mock_load:
            nftables.apply_masquerade_rules("br-jmp0", "eth0", "192.168.100.0/24")
            ruleset = mock_load.call_args[0][0]
            assert 'oifname "eth0" ip saddr 192.168.100.0/24 masquerade' in ruleset
            assert 'iifname "br-jmp0" oifname "eth0" accept' in ruleset


class TestInterfaceNameValidation:
    def test_rejects_invalid_names(self):
        with pytest.raises(ValueError, match="Invalid interface name"):
            nftables.apply_masquerade_rules("br jmp; drop", "eth0", "192.168.0.0/24")

    def test_rejects_too_long_names(self):
        with pytest.raises(ValueError, match="Invalid interface name"):
            nftables.apply_masquerade_rules("a" * 16, "eth0", "192.168.0.0/24")

    def test_accepts_valid_names(self):
        with patch.object(nftables, "_run_nft"), \
             patch.object(nftables, "_load_ruleset"):
            nftables.apply_masquerade_rules("br-jmp.0", "eth_0", "192.168.0.0/24")


class TestSubnetValidation:
    def test_rejects_invalid_subnet(self):
        with pytest.raises(ValueError):
            nftables.apply_masquerade_rules("br0", "eth0", "not-a-subnet")

    def test_rejects_invalid_ip_in_mapping(self):
        mappings = [{"private_ip": "not-an-ip", "public_ip": "10.0.0.1"}]
        with pytest.raises(ValueError):
            nftables.apply_1to1_rules("br0", "eth0", mappings, "192.168.0.0/24")


class TestTableNameFor:
    def test_replaces_hyphens(self):
        assert nftables._table_name_for("br-jmp-eth0") == "jumpstarter_br_jmp_eth0"

    def test_preserves_underscores(self):
        assert nftables._table_name_for("br_test") == "jumpstarter_br_test"


class TestFilterForwardDrop:
    def test_detects_policy_drop(self):
        fake = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='table ip filter {\n  chain FORWARD {\n    policy drop;\n  }\n}\n',
        )
        with patch.object(nftables, "_run_nft", return_value=fake):
            assert nftables.is_filter_forward_drop() is True

    def test_returns_false_for_policy_accept(self):
        fake = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='table ip filter {\n  chain FORWARD {\n    policy accept;\n  }\n}\n',
        )
        with patch.object(nftables, "_run_nft", return_value=fake):
            assert nftables.is_filter_forward_drop() is False

    def test_returns_false_when_table_missing(self):
        fake = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
        with patch.object(nftables, "_run_nft", return_value=fake):
            assert nftables.is_filter_forward_drop() is False

    def test_ensure_inserts_rules_when_drop(self):
        insert_output = 'insert rule ip filter FORWARD iifname "br-jmp0" accept # handle 42\n'
        fake_insert = subprocess.CompletedProcess(args=[], returncode=0, stdout=insert_output)
        with patch.object(nftables, "is_filter_forward_drop", return_value=True), \
             patch.object(nftables, "_run_nft", return_value=fake_insert) as mock_nft:
            handles = nftables.ensure_filter_forward("br-jmp0", "eth0")
            assert len(handles) == 4
            assert all(h == 42 for h in handles)
            assert mock_nft.call_count == 4

    def test_ensure_inserts_extra_interfaces(self):
        insert_output = 'insert rule ip filter FORWARD iifname "end0.905" accept # handle 7\n'
        fake_insert = subprocess.CompletedProcess(args=[], returncode=0, stdout=insert_output)
        with patch.object(nftables, "is_filter_forward_drop", return_value=True), \
             patch.object(nftables, "_run_nft", return_value=fake_insert) as mock_nft:
            handles = nftables.ensure_filter_forward("br-jmp0", "eth0", extra_interfaces=["end0.905"])
            assert len(handles) == 6
            assert mock_nft.call_count == 6

    def test_ensure_returns_empty_when_accept(self):
        with patch.object(nftables, "is_filter_forward_drop", return_value=False):
            handles = nftables.ensure_filter_forward("br-jmp0", "eth0")
            assert handles == []

    def test_ensure_handles_insert_failure(self):
        fail = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="error")
        with patch.object(nftables, "is_filter_forward_drop", return_value=True), \
             patch.object(nftables, "_run_nft", return_value=fail):
            handles = nftables.ensure_filter_forward("br-jmp0", "eth0")
            assert handles == []

    def test_remove_deletes_by_handle(self):
        with patch.object(nftables, "_run_nft") as mock_nft:
            nftables.remove_filter_forward([42, 43])
            assert mock_nft.call_count == 2
            mock_nft.assert_any_call(
                ["delete", "rule", "ip", "filter", "FORWARD", "handle", "42"],
                check=False,
            )
            mock_nft.assert_any_call(
                ["delete", "rule", "ip", "filter", "FORWARD", "handle", "43"],
                check=False,
            )

    def test_remove_noop_with_empty_handles(self):
        with patch.object(nftables, "_run_nft") as mock_nft:
            nftables.remove_filter_forward([])
            mock_nft.assert_not_called()


class TestFlushRules:
    def test_flushes_specific_table(self):
        with patch.object(nftables, "_run_nft") as mock:
            nftables.flush_rules("my_custom_table")
            mock.assert_called_once_with(["delete", "table", "ip", "my_custom_table"], check=False)

    def test_flushes_default_table(self):
        with patch.object(nftables, "_run_nft") as mock:
            nftables.flush_rules()
            mock.assert_called_once_with(["delete", "table", "ip", "jumpstarter"], check=False)
