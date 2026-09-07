"""Client interface for the DUT Network isolation driver."""

import json
from typing import Generator

import click

from jumpstarter.client import DriverClient
from jumpstarter.client.decorators import driver_click_group


class DutNetworkClient(DriverClient):
    """Client for the DutNetwork driver.

    Provides methods to query network status, manage DHCP leases,
    and inspect NAT rules.
    """

    def status(self) -> dict:
        """Get network status including interface state, leases, and NAT info."""
        return self.call("status")

    def get_dut_ip(self, mac: str) -> str | None:
        """Look up the assigned IP for a DUT by MAC address."""
        return self.call("get_dut_ip", mac)

    def get_leases(self) -> list[dict]:
        """List all current DHCP leases (dynamic + static)."""
        return self.call("get_leases")

    def add_address(
        self,
        ip: str,
        mac: str | None = None,
        hostname: str = "",
        public_ip: str | None = None,
        vlan_id: int | None = None,
        public_gateway: str | None = None,
    ) -> None:
        """Add an address entry (with optional MAC for DHCP static lease)."""
        self.call("add_address", ip, mac, hostname, public_ip, vlan_id, public_gateway)

    def remove_address(self, ip: str) -> None:
        """Remove an address entry by IP."""
        self.call("remove_address", ip)

    def get_nat_rules(self) -> str:
        """List active nftables rules."""
        return self.call("get_nat_rules")

    def ntp_status(self) -> dict:
        """Get the status of the local NTP server."""
        return self.call("ntp_status")

    def get_dns_entries(self) -> list[dict[str, str]]:
        """List configured DNS entries."""
        return self.call("get_dns_entries")

    def add_dns_entry(self, hostname: str, ip: str) -> None:
        """Add a custom DNS entry."""
        self.call("add_dns_entry", hostname, ip)

    def remove_dns_entry(self, hostname: str) -> None:
        """Remove a custom DNS entry."""
        self.call("remove_dns_entry", hostname)

    def tcpdump(self, args: list[str] | None = None) -> Generator[str, None, None]:
        """Stream tcpdump output from the configured interface.

        Requires ``enable_tcpdump: true`` in the driver config.

        Args:
            args: Optional list of additional tcpdump arguments.

        Yields:
            Lines of tcpdump text output.
        """
        for line in self.streamingcall("tcpdump", args):
            yield line

    def cli(self):  # noqa: C901
        """Build the Click CLI command group for this driver."""
        @driver_click_group(self)
        def base():
            """DUT Network Isolation"""
            pass

        @base.command()
        def status():
            """Show network status (interface, leases, NAT)."""
            result = self.status()
            click.echo(json.dumps(result, indent=2))

        @base.command()
        def leases():
            """Show all current DHCP leases."""
            result = self.get_leases()
            if not result:
                click.echo("No active DHCP leases.")
                return
            click.echo(f"{'MAC':<20} {'IP':<16} {'Hostname':<20} {'Expiry'}")
            click.echo("-" * 70)
            for lease in result:
                click.echo(f"{lease['mac']:<20} {lease['ip']:<16} {lease.get('hostname', ''):<20} {lease['expiry']}")

        @base.command("get-ip")
        @click.argument("mac")
        def get_ip(mac: str):
            """Look up assigned IP for a DUT by MAC address."""
            ip = self.get_dut_ip(mac)
            if ip:
                click.echo(ip)
            else:
                raise click.ClickException(f"No lease found for MAC {mac}")

        @base.command("add-address")
        @click.argument("ip")
        @click.option("--mac", "-m", default=None, help="MAC address for DHCP static lease")
        @click.option("--hostname", "-n", default="", help="Hostname for the entry")
        @click.option("--public-ip", default=None, help="Public IP for 1:1 NAT mapping")
        @click.option("--vlan-id", type=int, default=None, help="VLAN ID for a tagged sub-interface")
        @click.option("--public-gateway", default=None, help="Gateway for policy-based routing")
        def add_address(
            ip: str,
            mac: str | None,
            hostname: str,
            public_ip: str | None,
            vlan_id: int | None,
            public_gateway: str | None,
        ):
            """Add an address entry (with optional MAC for DHCP static lease)."""
            self.add_address(
                ip, mac, hostname, public_ip, vlan_id, public_gateway,
            )
            msg = f"Added address: {ip}"
            if mac:
                msg += f" (mac={mac})"
            click.echo(msg)

        @base.command("remove-address")
        @click.argument("ip")
        def remove_address(ip: str):
            """Remove an address entry by IP."""
            self.remove_address(ip)
            click.echo(f"Removed address for {ip}")

        @base.command("nat-rules")
        def nat_rules():
            """Show active nftables NAT rules."""
            rules = self.get_nat_rules()
            if rules:
                click.echo(rules)
            else:
                click.echo("No active NAT rules.")

        @base.command("ntp-status")
        def ntp_status():
            """Show status of the local NTP server."""
            result = self.ntp_status()
            enabled = result.get("enabled", False)
            running = result.get("running", False)
            click.echo(f"NTP enabled: {enabled}")
            click.echo(f"NTP running: {running}")

        @base.command("dns-entries")
        def dns_entries():
            """Show configured DNS entries."""
            entries = self.get_dns_entries()
            if not entries:
                click.echo("No DNS entries configured.")
                return
            click.echo(f"{'Hostname':<40} {'IP'}")
            click.echo("-" * 56)
            for entry in entries:
                click.echo(f"{entry['hostname']:<40} {entry['ip']}")

        @base.command("add-dns")
        @click.argument("hostname")
        @click.argument("ip")
        def add_dns(hostname: str, ip: str):
            """Add a custom DNS entry."""
            self.add_dns_entry(hostname, ip)
            click.echo(f"Added DNS entry: {hostname} -> {ip}")

        @base.command("remove-dns")
        @click.argument("hostname")
        def remove_dns(hostname: str):
            """Remove a custom DNS entry."""
            self.remove_dns_entry(hostname)
            click.echo(f"Removed DNS entry: {hostname}")

        @base.command("tcpdump", context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False})
        @click.argument("args", nargs=-1, type=click.UNPROCESSED)
        def tcpdump_cmd(args: tuple[str, ...]):
            """Run tcpdump on the DUT network interface (Ctrl+C to stop).

            Additional tcpdump arguments can be passed after the command.
            The interface is always enforced to the configured DUT interface.
            """
            arg_list = list(args) if args else None
            try:
                for line in self.tcpdump(arg_list):
                    click.echo(line)
            except KeyboardInterrupt:
                pass

        return base
