# DUT Network Driver

`jumpstarter-driver-dut-network` provides network isolation for DUTs (Devices Under Test) by configuring a dedicated network interface with NAT, DHCP, and nftables-based firewall rules on the exporter host.

This enables scenarios where multiple DUTs share the same static IP configuration (common in automotive/embedded labs) by isolating each DUT behind its own NAT interface on the exporter.

## Installation

```shell
pip3 install --extra-index-url https://pkg.jumpstarter.dev/simple/ jumpstarter-driver-dut-network
```

### System Dependencies

The following must be available on the exporter host:

- `ip` (iproute2) - for interface management
- `nft` (nftables) - for NAT and firewall rules
- `dnsmasq` - for DHCP serving

Optional:
- `nmcli` (NetworkManager) - only needed if NM is running; the driver marks its interfaces as unmanaged

## Configuration

### Masquerade NAT (recommended for most use cases)

DUTs share the exporter's upstream IP when accessing the network:

```yaml
export:
  dut-network:
    type: jumpstarter_driver_dut_network.driver.DutNetwork
    config:
      interface: "eth2"
      subnet: "192.168.100.0/24"
      gateway_ip: "192.168.100.1"
      nat_mode: "masquerade"
      dhcp_enabled: true
      dhcp_range_start: "192.168.100.100"
      dhcp_range_end: "192.168.100.200"
      addresses:
        - mac: "8a:12:4e:25:f4:8e"
          ip: "192.168.100.10"
          hostname: "sa8775p"
      dns_servers: ["8.8.8.8", "8.8.4.4"]
```

### 1:1 NAT

Each DUT gets a dedicated public IP alias via a per-entry `public_ip` field, enabling inbound connections from the LAN. Entries without a `public_ip` fall back to masquerade for outbound traffic. Entries without a `mac` are used for 1:1 NAT mappings only and are excluded from DHCP static lease generation.

```yaml
export:
  dut-network:
    type: jumpstarter_driver_dut_network.driver.DutNetwork
    config:
      interface: "eth2"
      subnet: "192.168.100.0/24"
      gateway_ip: "192.168.100.1"
      upstream_interface: "enp2s0"
      nat_mode: "1to1"
      addresses:
        - mac: "8a:12:4e:25:f4:8e"
          ip: "192.168.100.10"
          hostname: "sa8775p-1"
          public_ip: "198.51.100.84"
        - mac: "8a:12:4e:25:f4:8f"
          ip: "192.168.100.11"
          hostname: "sa8775p-2"
          public_ip: "198.51.100.85"
        # Entry without MAC: 1:1 NAT mapping only, no DHCP static lease
        - ip: "192.168.100.12"
          hostname: "nxp-board-03"
          public_ip: "198.51.100.86"
```

### VLAN sub-interfaces and policy-based routing

When an address entry sets `vlan_id`, the driver creates a tagged sub-interface
on `upstream_interface` (for example `end0.905` when upstream is `end0` and
the VLAN is 905), assigns `public_ip` to that sub-interface, and generates
nftables NAT/forward rules against it instead of the untagged parent.

If `public_gateway` is also set, policy-based routing (PBR) forces traffic
from that DUT's private IP out via the VLAN: a default route in routing table
`<vlan_id>` and an `ip rule` matching the DUT source address.

`vlan_id` without `public_gateway` still creates the VLAN and NAT rules, but
logs a warning: without PBR, DUT traffic may not egress via the tagged
interface.

`public_gateway` without `vlan_id` is supported on the untagged upstream:
source-IP PBR uses routing table `int(<dut private IPv4>)`.

**Untagged source-IP PBR:**

```yaml
      addresses:
        - mac: "8a:12:4e:25:f4:8e"
          ip: "192.168.100.125"
          public_gateway: "203.0.113.254"
```

Omit both fields to keep today's untagged-upstream behaviour.

**1:1 NAT on a VLAN:**

```yaml
export:
  dut-network:
    type: jumpstarter_driver_dut_network.driver.DutNetwork
    config:
      interface: "enp1s0u1"
      subnet: "192.168.100.0/24"
      gateway_ip: "192.168.100.1"
      upstream_interface: "end0"
      nat_mode: "1to1"
      addresses:
        - mac: "8a:12:4e:25:f4:8e"
          ip: "192.168.100.125"
          hostname: "sa8775p"
          public_ip: "203.0.113.1"
          vlan_id: 905
          public_gateway: "203.0.113.254"
```

**Masquerade on a VLAN:**

```yaml
export:
  dut-network:
    type: jumpstarter_driver_dut_network.driver.DutNetwork
    config:
      interface: "enp1s0u1"
      subnet: "192.168.100.0/24"
      gateway_ip: "192.168.100.1"
      upstream_interface: "end0"
      nat_mode: "masquerade"
      addresses:
        - mac: "8a:12:4e:25:f4:8e"
          ip: "192.168.100.125"
          hostname: "sa8775p"
          public_ip: "203.0.113.1"
          vlan_id: 905
          public_gateway: "203.0.113.254"
```

Linux interface names are limited to 15 characters, so
`<upstream>.<vlan_id>` must fit that limit.  VLAN IDs 253–255 cannot be used
with `public_gateway` because those routing-table IDs are reserved by the kernel.

#### Mixed VLAN and untagged addresses

Tagged and untagged entries can coexist on the same exporter.  The driver
builds NAT and forwarding rules only for the interfaces actually in use:
VLAN-only configs omit the untagged upstream; untagged-only configs ignore
VLANs; mixed configs include both.  Adding or removing addresses at runtime
(via `add-address` / `remove-address`) triggers a full rebuild, so the rule
set always matches the current address list.


### Disabled NAT (DHCP only)

DHCP works normally but no NAT rules or IP forwarding are configured. Useful for pure L2 isolation or when routing is handled externally:

```yaml
export:
  dut-network:
    type: jumpstarter_driver_dut_network.driver.DutNetwork
    config:
      interface: "enx00e04c683af1"
      nat_mode: "disabled"   # also accepts "none"
      dhcp_enabled: true
```

### Custom DNS Entries

Register custom DNS records that dnsmasq will respond to. Useful for pointing DUTs at local services without a full DNS infrastructure:

```yaml
export:
  dut-network:
    type: jumpstarter_driver_dut_network.driver.DutNetwork
    config:
      interface: "eth2"
      nat_mode: "masquerade"
      dns_entries:
        - hostname: "controller.lab.local"
          ip: "198.51.100.1"
        - hostname: "registry.lab.local"
          ip: "198.51.100.2"
```

### Reference

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `interface` | str | *required* | Physical NIC for DUT connectivity (e.g., USB NIC name) |
| `subnet` | str | `192.168.100.0/24` | Private subnet for DUTs |
| `gateway_ip` | str | `192.168.100.1` | IP assigned to the interface (acts as gateway for DUTs) |
| `upstream_interface` | str | auto-detect | Interface for outbound NAT traffic |
| `dhcp_enabled` | bool | `true` | Whether to run DHCP on the interface |
| `dhcp_range_start` | str | `192.168.100.100` | DHCP dynamic range start |
| `dhcp_range_end` | str | `192.168.100.200` | DHCP dynamic range end |
| `addresses` | list | `[]` | Address entries: `{ip, mac?, hostname?, public_ip?, vlan_id?, public_gateway?}`. Entries with `mac` generate DHCP static leases; entries without `mac` are used for 1:1 NAT only. |
| `dns_servers` | list | `[8.8.8.8, 8.8.4.4]` | DNS servers for DHCP clients |
| `dns_entries` | list | `[]` | Custom DNS records: `{hostname, ip}` |
| `state_dir` | str | `/var/lib/jumpstarter/dut-network-{interface}/` | Directory for dnsmasq state files |
| `nat_mode` | str | `masquerade` | NAT mode: `masquerade`, `1to1`, `disabled`, or `none` |
| `public_interface` | str | None | Interface for IP alias (defaults to upstream) |

#### Address Entry Fields

| Field | Required | Description |
|-------|----------|-------------|
| `ip` | yes | Private IP to assign |
| `mac` | no | MAC address of the DUT. Required for DHCP static lease; omit for 1:1 NAT-only entries |
| `hostname` | no | Hostname for DHCP |
| `public_ip` | no | Public IP for 1:1 NAT (per-entry). At least one entry must have `public_ip` when `nat_mode=1to1`. Also assigned to the VLAN sub-interface when `vlan_id` is set |
| `vlan_id` | no | 802.1Q VLAN ID (1–4094). When set, NAT uses `<upstream>.<vlan_id>` instead of the untagged upstream. Omit for untagged behaviour |
| `public_gateway` | no | Gateway for policy-based routing. With `vlan_id`, DUT traffic exits via the VLAN (table ID = VLAN ID). Without `vlan_id`, traffic exits via the untagged upstream (table ID = DUT private IPv4 as an integer) |

## Usage

### CLI

Inside a `jmp shell` session:

```shell
# Show full network status
j dut-network status

# List DHCP leases
j dut-network leases

# Look up DUT IP by MAC
j dut-network get-ip 8a:12:4e:25:f4:8e

# Add an address entry with a MAC (creates a DHCP static lease)
j dut-network add-address 192.168.100.50 --mac 02:00:00:aa:bb:cc --hostname my-dut

# Add an address entry with VLAN and PBR
j dut-network add-address 192.168.100.125 --public-ip 203.0.113.1 --vlan-id 905 --public-gateway 203.0.113.254

# Remove an address entry by IP
j dut-network remove-address 192.168.100.50

# Show nftables NAT rules
j dut-network nat-rules

# List configured DNS entries
j dut-network dns-entries

# Add a custom DNS entry
j dut-network add-dns controller.lab.local 198.51.100.1

# Remove a DNS entry
j dut-network remove-dns controller.lab.local
```

### Python

```python
from jumpstarter.common.utils import env

with env() as client:
    # Get network status
    status = client.dut_network.status()
    print(status["interface_status"]["name"])

    # Get all DHCP leases
    leases = client.dut_network.get_leases()
    for lease in leases:
        print(f"{lease['mac']} -> {lease['ip']}")

    # Look up DUT IP
    ip = client.dut_network.get_dut_ip("8a:12:4e:25:f4:8e")

    # Manage address entries at runtime
    # With MAC: creates a DHCP static lease + optional 1:1 NAT mapping
    client.dut_network.add_address("192.168.100.50", mac="02:00:00:aa:bb:cc", hostname="new-dut")
    # Without MAC: 1:1 NAT mapping only (no DHCP lease)
    client.dut_network.add_address("192.168.100.51", public_ip="198.51.100.90")
    # VLAN + PBR
    client.dut_network.add_address(
        "192.168.100.125",
        public_ip="203.0.113.1",
        vlan_id=905,
        public_gateway="203.0.113.254",
    )
    client.dut_network.remove_address("192.168.100.50")

    # Manage DNS entries at runtime
    client.dut_network.add_dns_entry("myhost.lab.local", "10.0.0.99")
    entries = client.dut_network.get_dns_entries()
    client.dut_network.remove_dns_entry("myhost.lab.local")
```

## Architecture

The driver configures an isolated network for the DUT:

1. Takes over a dedicated Ethernet interface (e.g., USB NIC) and assigns a gateway IP directly to it
2. Runs dnsmasq to provide DHCP to DUTs connected to that interface
3. Configures nftables rules for NAT (masquerade or 1:1)
4. Enables IP forwarding so DUT traffic routes through the exporter

When NetworkManager is detected, the driver marks managed interfaces as `unmanaged` to prevent interference. On cleanup, existing addresses are flushed and the interface is restored to NetworkManager control.

```text
                     Exporter Host
 ┌─────────┐        ┌──────────────────────────────────────┐          ┌─────────┐
 │   DUT   │        │                                      │          │   LAN   │
 │         │  eth   │  eth2               ┌──────────┐     │          │         │
 │  DHCP   │◄──────►│  192.168.100.1/24   │ dnsmasq  │     │          │         │
 │  client │        │  (gateway)          │ DHCP+DNS │     │          │         │
 │         │        │       │             └──────────┘     │          │         │
 │ 192.168.│        │       │  forwarding                  │  eth     │         │
 │ 100.10  │        │       ▼             ┌──────────┐     │          │         │
 │         │        │  ┌─────────┐        │ nftables │     │ enp2s0   │ 10.26.  │
 └─────────┘        │  │ ip_fwd  │───────►│ NAT      │────►│◄──────►  │ 28.0/24 │
                    │  └─────────┘        │          │     │(upstream)│         │
                    │                     │masq/1:1  │     │          └─────────┘
                    │                     └──────────┘     │
                    └──────────────────────────────────────┘

  ─── Masquerade: DUT traffic appears as exporter's upstream IP
  ─── 1:1 NAT:    DUT gets a dedicated public IP on the upstream interface
```

### Disabled NAT (DHCP-only isolation)

```text
                     Exporter Host
 ┌─────────┐        ┌──────────────────────────────┐
 │   DUT   │        │                              │
 │         │  eth   │  eth2          ┌──────────┐  │
 │  DHCP   │◄──────►│  192.168.100.1 │ dnsmasq  │  │
 │  client │        │  (gateway)     │ DHCP+DNS │  │
 │         │        │                └──────────┘  │
 │ 192.168.│        │                              │
 │ 100.10  │        │  No forwarding, no NAT.      │
 │         │        │  L2-isolated network only.   │
 └─────────┘        └──────────────────────────────┘

  The DUT can reach the exporter on 192.168.100.1 but has
  no route to the LAN or internet. Useful for pure L2
  isolation or when routing is handled externally.
```

## API Reference

```{eval-rst}
.. autoclass:: jumpstarter_driver_dut_network.driver.DutNetwork()
```

```{note}
The driver uses a dedicated nftables table (named after the interface) that
does not conflict with firewalld or other nftables users.
```

## Troubleshooting

### NAT traffic not forwarding (Docker hosts)

On hosts running Docker, the default iptables policy is often set to
`iptables -P FORWARD DROP` to isolate container networks.  Since modern
Linux translates iptables rules into nftables under the hood, this creates
a `table ip filter { chain FORWARD { policy drop } }` base chain that
**all** forwarded packets must pass - including traffic routed through
the DUT interface.

The driver **automatically** detects this situation using native nftables:
when NAT is enabled, it checks if the `ip filter` table's FORWARD chain
has `policy drop`.  If so, targeted `accept` rules are inserted directly
into that chain for the DUT and upstream interfaces on startup, and
removed by handle on cleanup.  No manual intervention or `iptables`
binary is required.

### Per-interface IP forwarding

The driver enables IPv4 forwarding only on the DUT and upstream
interfaces (`net.ipv4.conf.<iface>.forwarding=1`) rather than the global
`net.ipv4.ip_forward` sysctl.  This avoids turning a multi-homed host
into a full router on every interface.  If forwarding still does not work,
verify with:

```shell
sysctl net.ipv4.conf.<interface>.forwarding
sysctl net.ipv4.conf.<upstream>.forwarding
```

