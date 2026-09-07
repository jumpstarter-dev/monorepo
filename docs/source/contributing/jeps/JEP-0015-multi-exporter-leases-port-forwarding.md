# JEP-0015: Multi-Exporter Leases and Inter-Exporter Port Forwarding

| Field             | Value                                                    |
| ----------------- | -------------------------------------------------------- |
| **JEP**           | 0015                                                     |
| **Title**         | Multi-Exporter Leases and Inter-Exporter Port Forwarding |
| **Author(s)**     | @kirkbrauer (Kirk Brauer, kbrauer@hatci.com)             |
| **Status**        | Discussion                                               |
| **Type**          | Standards Track                                          |
| **Created**       | 2026-09-01                                               |
| **Updated**       | 2026-09-05                                               |
| **Discussion**    | [PR #1069](https://github.com/jumpstarter-dev/jumpstarter/pull/1069) |
| **Requires**      | JEP-0014                                                 |
| **Supersedes**    |                                                          |
| **Superseded-By** |                                                          |

---

## Abstract

This JEP extends `Lease` to acquire multiple exporters together and forward
traffic between their named driver ports. Optional `spec.members[]` assigns
roles to exporters, and `spec.forwards[]` declares connections between them
without exposing local addresses. All member claims are committed in one
status update, and the members share a lease lifetime. Forwards reuse the
existing port-forwarding primitives and router, with mutually authenticated,
encrypted direct connections preferred within a network zone. The examples focus on phone
projection, but the same model supports CAN, serial, and other socket-based
connections.

## Motivation

A Jumpstarter lease currently grants exclusive access to one exporter.
Tests involving several exporters must acquire separate leases, coordinate
their lifetimes, and arrange any connections between devices themselves.
If one request succeeds while another waits, a test holds hardware it cannot
use; concurrent tests can each hold a device the other needs.

An exporter remains the unit of allocation. It owns a DUT and its harness,
which may include several physically connected devices. Leasing those devices
separately could give different clients control of the same assembly. This
proposal instead joins independently managed exporters into one lease,
called a **bench**. A pool of N phones and M head units can then support N×M
pairings without pre-wiring each pair.

Phone projection illustrates the need: a test must control both a phone and
a head unit, pair them over Bluetooth, and observe the handover to Wi-Fi.
The devices may run different operating systems and belong to exporters on
different hosts. Other examples include two ECUs testing a CAN gateway, a
BLE peripheral and its central, or a DUT and a serial companion.

Separate leases leave four gaps:

- **Acquisition:** there is no all-or-nothing claim across exporters.
- **Lifetime:** devices can expire or be released independently.
- **Policy and observability:** requests have no shared bench identity.
- **Connectivity:** existing streams connect clients to exporters, with no
  managed exporter-to-exporter path.

For virtual devices, connectivity is also constrained by host-local simulator
interfaces. Cuttlefish instances can share rootcanal, netsim, and `wmediumd`
on one host. Separately scheduled exporter Pods need a path between those
interfaces. HCI over TCP can use a byte forward; interfaces such as
vhost-user require a local bridge as well (DD-9, DD-10).

The initial scope covers two virtual devices or two physical devices.
Physical radio peers must remain within RF range even when their exporters
run on different hosts. Connecting physical and simulated radios requires
additional hardware and is deferred (DD-11).

### User Stories

- A projection test acquires a phone and head unit together, controls both
  by role, and retains access to surviving devices for diagnostics if one
  fails.
- A CI test pairs virtual devices in separate Pods and runs a projection
  session without physical phone or head unit hardware.
- A lab test joins exporters on different hosts, such as two ECUs connected
  through a CAN-over-TCP bridge.

## Proposal

The proposal adds three concepts to existing resources:

- **Members.** A lease gains optional `spec.members[]`, each a role name plus
  the same `selector` / `exporterRef` fields a single-exporter lease already
  uses. A lease binds all required members or holds none.
- **Ports.** A driver may declare named ports it **provides** (a service
  listening locally) or **requires** (a socket it will dial). Ports travel in
  the exporter's existing report; addresses never leave the exporter.
- **Forwards.** A lease gains optional `spec.forwards[]`, each joining a
  provided port on one member to a required port on another. The exporters
  establish the forward themselves over the existing router.

A bench appears in `jmp get leases` and uses the existing expiry and
`spec.release` behavior.

### Ports

A port is a named connection point on a driver. Each forward connects a
`provides` port to a `requires` port.

| Direction | Meaning | Example |
| --- | --- | --- |
| `provides` | A service is listening; something may be forwarded *from* it | `rootcanal` on a Cuttlefish exporter — HCI on `127.0.0.1:7300` |
| `requires` | The driver will dial a local address; a forward may be delivered *to* it | `controller` on a `bt-peer` exporter — where its bumble stack expects an HCI controller |

A `TcpNetwork` child can expose a provided port:

```yaml
export:
  cuttlefish:
    type: jumpstarter_driver_cuttlefish.driver.Cuttlefish
    children:
      rootcanal:
        type: jumpstarter_driver_network.driver.TcpNetwork
        config: { host: 127.0.0.1, port: 7300 }
        ports:
          - name: rootcanal
            direction: provides
            protocol: hci-h4        # optional
```

For a `requires` port, the exporter binds a local listener that the driver
dials:

```yaml
export:
  bt_peer:
    type: jumpstarter_driver_bt_peer.driver.BtPeer
    config:
      transport: "tcp-client:127.0.0.1:7300"   # unchanged
    ports:
      - name: controller
        direction: requires
        listen: 127.0.0.1:7300                 # local; never reported
        protocol: hci-h4                       # optional
```

The `bt-peer` driver still dials `127.0.0.1:7300`. The exporter forwards that
connection to the remote rootcanal; using the forward requires no changes to
the driver's Python code.

### Declaring a bench

```yaml
apiVersion: jumpstarter.dev/v1alpha1
kind: Lease
metadata:
  name: projection-bench
  namespace: jumpstarter-lab
spec:
  clientRef:
    name: ci-runner
  duration: 45m
  members:
    - name: phone
      selector:
        matchLabels:
          device-type: android-phone
          android-version: "15"
    - name: headunit
      selector:
        matchLabels:
          device-type: aaos-headunit
  forwards:
    - name: bt
      between:
        - { member: headunit, port: rootcanal }
        - { member: phone,    port: controller }
```

The lease names members and ports. Exporters resolve addresses locally, and
the controller determines direction from reported ports at bind time (DD-6,
DD-13).

Each member uses the existing `selector` or `exporterRef` fields with a role
name. The scalar lease form remains supported (DD-2).

A member marked `optional: true` may be omitted at binding; its role then
resolves to `None` on the client. A forward that references an omitted
optional member is `Disabled`, with a status message naming that member. It
is not established and does not prevent the rest of the lease from becoming
`Ready`. If the optional member binds, every forward that references it is
required to connect normally.

#### CAN example

Two ECUs can share a CAN segment through `socketcand` or another TCP bridge:

```yaml
spec:
  members:
    - name: gateway
      selector: { matchLabels: { ecu-role: gateway } }
    - name: node
      selector: { matchLabels: { ecu-role: body-controller } }
  forwards:
    - name: powertrain-bus
      between:
        - { member: gateway, port: can0 }
        - { member: node,    port: can }
```

The controller checks that both ports exist, their directions complement
each other, and any declared protocols agree.

### Acquiring and using a bench

The existing commands take members and forwards; there is no parallel
command set.

```console
$ jmp create lease \
    --member phone=device-type=phone \
    --member headunit=device-type=headunit \
    --forward bt=headunit.rootcanal:phone.controller \
    --duration 45m
projection-bench

$ jmp get lease projection-bench
NAME                 ENDED   CLIENT      EXPORTER                       AGE
projection-bench   false   ci-runner   phone=rack3-phone-4,            12s
                                         headunit=virt-hu-7b2c
```

Users can discover port names from the exporter report (DD-7):

```console
$ jmp get exporter virt-hu-7b2c -o json | jq '.status.devices[].ports'
[{"name":"rootcanal","direction":"provides","protocol":"hci-h4"}]
```

In a shell, roles become top-level names alongside the usual driver clients:

```console
$ jmp shell --lease projection-bench
jumpstarter ⚡ projection-bench ➤ j phone adb shell getprop ro.product.model
phone-under-test
jumpstarter ⚡ projection-bench ➤ j headunit power on
jumpstarter ⚡ projection-bench ➤ j forward status
NAME   FROM                  TO                  MODE     STATE       A→B       B→A
bt     headunit.rootcanal    phone.controller    direct   connected   1.2 MiB   0.9 MiB
```

In Python, the existing `lease()` context manager grows `members` and
`forwards`, and a lease requested through `members` yields a bench whose
`members` mapping contains the same client objects a scalar lease yields
directly. Roles are deliberately not installed as arbitrary object
attributes: mapping access safely supports names such as `head-unit` without
allowing a role such as `__class__` or `forwards` to shadow client state:

```python
from jumpstarter.config.client import ClientConfigV1Alpha1

config = ClientConfigV1Alpha1.load("default")

with config.lease(
    members={"phone": "device-type=phone", "headunit": "device-type=headunit"},
    forwards=[Forward("bt", frm=("headunit", "rootcanal"), to=("phone", "controller"))],
    duration=timedelta(minutes=45),
) as lease:
    with lease.connect() as bench:
        phone = bench.members["phone"]
        headunit = bench.members["headunit"]

        headunit.power.on()
        phone.adb.wait_for_device()

        # Wait for the forward before starting the peer.
        bench.forwards["bt"].wait_connected(timeout=30)
        phone.bt_peer.start({"name": "Bumble-Phone"})
        phone.bt_peer.wait_connection(timeout=60)

        assert phone.adb.shell("dumpsys bluetooth_manager | grep -c Connected") == "1"
```

`config.lease(selector=...)` keeps its existing scalar behavior, with
`connect()` yielding a single driver client. Explicit `members`, including a
list with exactly one entry, always uses `status.members` and yields the bench
shape with a role mapping (DD-2).

`JumpstarterTest` grows `members` and `forwards` class variables next to
`selector`, so existing pytest suites extend without a second base class.

### Running existing multi-device suites

A bench with ADB-capable members can be exported as a Mobly testbed:

```console
$ jmp get lease projection-bench -o mobly > testbed.yml
$ mobly_test.py -c testbed.yml --test_bed projection-bench
```

The exported `AndroidDevice` controllers use locally forwarded ADB endpoints
and retain the member names as device labels. Existing tests and results
pipelines can use these endpoints. The lifetime of the local ADB forwards
remains an open question.

### How a forward comes up

The exporter reuses `TemporaryTcpListener` and `forward_stream()` from
`TcpPortforwardAdapter`, replacing the client stream with a peer stream:

1. After binding, the controller validates the ports and sends setup
   instructions over each exporter's existing `Listen` stream.
2. Each exporter calls `DialPeer` for connection details and credentials.
3. For a direct-eligible pair, the `requires` side first establishes mTLS
   with a bounded timeout. Both peers validate their controller-issued,
   per-forward certificate identities; only then does the requiring side send
   `peer_token` inside the encrypted channel. Otherwise, or if that attempt
   fails in `Auto` mode, both endpoints call `RouterService.Stream` with
   tokens sharing one unique per-forward subject (DD-4, DD-5).
4. The `provides` side dials its local service. The `requires` side listens
   on its configured address. Both splice each accepted local connection to
   that connection's peer stream using `forward_stream()`.

```{mermaid}
flowchart TD
    lease["Lease: projection-bench"]
    phone["Exporter: rack3-phone-4<br/>bt_peer · requires: controller<br/>Listens on 127.0.0.1:7300"]
    headunit["Exporter: virt-hu-7b2c<br/>cuttlefish · provides: rootcanal<br/>Dials 127.0.0.1:7300"]
    router["RouterService"]

    lease -.->|"status.members: phone"| phone
    lease -.->|"status.members: headunit"| headunit
    phone <-->|"Direct peer: preferred in same zone"| headunit
    phone <-->|"Router fallback"| router
    router <--> headunit
```

The client is not in the data path.

### Attaching media, simulated and physical

Drivers expose the connection points that forwards carry:

| Stack | Port | Direction | Notes |
| --- | --- | --- | --- |
| Shared virtual controller (Bumble) | `controller` | provides | Accepts multiple hosts and mediates between them (DD-9) |
| `jumpstarter-driver-bt-peer` | `controller` | requires | A Bumble `Device` dialing an external controller |
| rootcanal HCI (Cuttlefish, emulator) | `rootcanal` | provides | HCI on TCP (`7300 + rootcanal_instance_num`); hosts attach to it |
| rootcanal link layer | `rootcanal-link` | provides | Controller-to-controller federation (`7400`, `7600` BLE); standalone rootcanal only, not netsim |
| `wmediumd` / `mac80211_hwsim` | `hwsim` | provides | vhost-user, not a byte stream — reached through a frame bridge (DD-10) |
| Projection server on a phone (developer mode) | `projection`, `projection-wifi` | provides | Same service, reached over ADB (USB-like) or the guest's Wi-Fi address (wireless-like); the receiver dials it |
| Projection receiver (desktop or head unit) | `phone` | requires | Dials the phone's port (DD-10) |
| Wireless projection receiver on a head unit | `projection-rx` | provides | A TCP port on the head unit; the phone dials it after the Bluetooth handover — physical head units only |
| `socketcand` / CAN-over-TCP bridge | `can` | provides | A CAN segment reachable as a socket |
| Serial bridge (pty or TCP) | `console` | requires/provides | Cross-over between a DUT and a companion |

Control drivers configure and observe a medium separately from its forwarded
traffic. For example, `jumpstarter-driver-netsim` uses the REST API to list
devices, toggle radios, reset state, and collect pcap captures. It is not a
forward endpoint.

HCI connects a host to a controller. Joining two controllers requires a
link-layer port instead; joining two HCI `provides` ports is invalid (DD-6,
DD-9). Some interfaces also need protocol handling at the endpoint: netsim's
`PacketStreamer` requires a gRPC call carrying `ChipInfo`, so its consumer
must implement that handshake.

Virtual radio benches forward simulator traffic. Physical radio peers
communicate over the air and may need only the shared lease; forwards can
still carry wired traffic such as CAN or serial.

### Projection example

**Virtual.** A Cuttlefish phone exporter runs a vendor phone image with the
projection app and its developer-mode server. A `projection-rx` driver runs
a receiver process in the head unit exporter, since AOSP automotive images
do not include that receiver. The phone provides the port and the receiver
dials it:

```yaml
spec:
  members:
    - name: phone
      selector: { matchLabels: { device-type: phone, projection: "true" } }
    - name: headunit
      selector: { matchLabels: { device-type: projection-receiver } }
  forwards:
    - name: session
      between:
        - { member: phone,    port: projection-wifi }
        - { member: headunit, port: phone }
```

`projection-wifi` reaches the server through the guest's Wi-Fi interface;
`projection` reaches it over ADB. The test chooses which path to exercise
(DD-13). Neither forward alone tests the Bluetooth-to-Wi-Fi handover (DD-10).

**Physical.** A phone and head unit on different exporters pair over the air.
The lease coordinates their access and lifetime; forwards can carry wired
connections such as a CAN segment feeding the head unit.

### API / Protocol Changes

The API adds fields to existing types and one RPC. Existing fields retain
their meanings.

**Driver report** — ports are optional, and an exporter that reports none
simply cannot participate in forwards (DD-7):

```protobuf
message DriverInstanceReport {
  // ... fields 1-5 unchanged ...
  repeated PortReport ports = 6;   // NEW, optional
}

message PortReport {
  string name = 1;                 // "rootcanal", "controller"
  PortDirection direction = 2;     // PROVIDES | REQUIRES
  optional string protocol = 3;    // free-form; compared only if both ends set it
}

enum PortDirection {
  PORT_DIRECTION_UNSPECIFIED = 0;
  PORT_DIRECTION_PROVIDES = 1;
  PORT_DIRECTION_REQUIRES = 2;
}
```

Port names are unique across all `DriverInstanceReport` entries from one
exporter, not merely within one driver instance. The exporter validates this
before registration, and the controller rejects a registration containing a
duplicate with `INVALID_ARGUMENT`. A `(member, port)` therefore resolves to
exactly one driver UUID; the resolved UUID is included in setup instructions
so the exporter never selects a local endpoint by name alone. The `listen`
address of a `requires` port is deliberately **absent**: it is local to the
exporter and no other component needs it (DD-6).

**`LeaseSpec`** gains two optional lists:

```go
// Member and forward names must each be unique before binding or token creation.
// +kubebuilder:validation:XValidation:rule="self.members.all(m, self.members.filter(x, x.name == m.name).size() == 1)",message="member names must be unique"
// +kubebuilder:validation:XValidation:rule="self.forwards.all(f, self.forwards.filter(x, x.name == f.name).size() == 1)",message="forward names must be unique"
type LeaseSpec struct {
    // ... all existing fields unchanged ...

    // Members of a member-form lease. When empty, the lease binds a single
    // exporter using the top-level Selector/ExporterRef exactly as before.
    // +kubebuilder:validation:MaxItems=8
    Members []LeaseMember `json:"members,omitempty"`

    // Port forwards between members. Requires Members.
    Forwards []LeaseForward `json:"forwards,omitempty"`
}

// Exactly one non-empty selection source is required for every member.
// +kubebuilder:validation:XValidation:rule="((((has(self.selector.matchLabels) && size(self.selector.matchLabels) > 0) || (has(self.selector.matchExpressions) && size(self.selector.matchExpressions) > 0)) ? 1 : 0) + ((has(self.exporterRef) && has(self.exporterRef.name) && size(self.exporterRef.name) > 0) ? 1 : 0)) == 1",message="exactly one of selector or exporterRef.name is required"
// +kubebuilder:validation:XValidation:rule="self.name != 'forward' && self.name != 'forwards'",message="member name is reserved"
type LeaseMember struct {
    // DNS-label syntax keeps names usable in the CLI and generated formats.
    // Python accesses them only through bench.members[name].
    // +kubebuilder:validation:MaxLength=63
    // +kubebuilder:validation:Pattern=`^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`
    Name          string                       `json:"name"`
    Selector      metav1.LabelSelector         `json:"selector,omitempty"`
    ExporterRef   *corev1.LocalObjectReference `json:"exporterRef,omitempty"`
    Optional      bool                         `json:"optional,omitempty"`
    AllowDisabled bool                         `json:"allowDisabled,omitempty"`
}

type LeaseForward struct {
    Name string `json:"name"`

    // Symmetric form (preferred): exactly two endpoints, in any order. The
    // controller resolves which is `provides` and which is `requires` from
    // the reported ports at bind time (DD-13).
    // +kubebuilder:validation:MinItems=2
    // +kubebuilder:validation:MaxItems=2
    Between []ForwardEndpoint `json:"between,omitempty"`

    // Explicit form: use when the wiring should be pinned regardless of what
    // the exporters report. Mutually exclusive with Between.
    From *ForwardEndpoint `json:"from,omitempty"`  // must resolve to `provides`
    To   *ForwardEndpoint `json:"to,omitempty"`    // must resolve to `requires`

    // Auto (default) | Router | Direct | ClientRelay.
    // Auto prefers a direct peer connection when both members are in the
    // same network zone and falls back to the router; Direct fails rather
    // than falling back; Router never attempts a direct dial (DD-4).
    Mode string `json:"mode,omitempty"`
}

type ForwardEndpoint struct {
    Member string `json:"member"`
    Port   string `json:"port"` // exporter-wide unique port name
}
```

**`LeaseStatus`** gains parallel lists and keeps its scalar:

```go
type LeaseStatus struct {
    // ... all existing fields unchanged ...
    // ExporterRef stays authoritative for scalar leases and is left nil for
    // every lease requested through Members, including one-member lists.

    Members  []LeaseMemberStatus  `json:"members,omitempty"`
    Forwards []LeaseForwardStatus `json:"forwards,omitempty"`
}

type LeaseMemberStatus struct {
    Name        string                       `json:"name"`
    ExporterRef *corev1.LocalObjectReference `json:"exporterRef,omitempty"`
    Priority    int                          `json:"priority,omitempty"`
    SpotAccess  bool                         `json:"spotAccess,omitempty"`
}

type LeaseForwardStatus struct {
    Name    string `json:"name"`
    State   string `json:"state"` // Pending|Disabled|Connecting|Connected|Reconnecting|Failed
    Mode    string `json:"mode,omitempty"`
    Message string `json:"message,omitempty"`
}
```

`ExporterStatus.Devices[]` gains the reported ports so the controller can
validate forwards against bound exporters. `ExporterStatus` itself gains two
optional fields used only to decide direct eligibility:

```go
    // Address peers in the same zone can dial for a direct forward, if this
    // exporter runs a peer listener. Never a device port (see Security).
    PeerEndpoint string `json:"peerEndpoint,omitempty"`
    // Opaque reachability domain. Two exporters are candidates for a direct
    // forward only if both report the same value.
    NetworkZone  string `json:"networkZone,omitempty"`
```

The existing CEL rules are extended, not replaced. The current top-level
"one of selector or exporterRef is required" rule gains a `members` arm and
mutual exclusion. Per-member CEL requires exactly one *non-empty* `selector`
or `exporterRef.name`; both set and both unset are rejected. Additional rules
enforce unique and immutable member names, unique forward names, forwards
referencing declared members, member immutability (mirroring `tags` and
`context`), and exactly one of `between` or `from`+`to` per forward. Duplicate
forward names are thus rejected before the controller derives stream subjects
or creates status maps.

**Protocol** — additions to existing messages and one new RPC. These are the
wire definitions; the similarly named Go structs above describe the CRD only:

```protobuf
enum LeaseForwardMode {
  LEASE_FORWARD_MODE_UNSPECIFIED = 0; // Auto
  LEASE_FORWARD_MODE_AUTO = 1;
  LEASE_FORWARD_MODE_ROUTER = 2;
  LEASE_FORWARD_MODE_DIRECT = 3;
  LEASE_FORWARD_MODE_CLIENT_RELAY = 4;
}

enum LeaseForwardState {
  LEASE_FORWARD_STATE_UNSPECIFIED = 0;
  LEASE_FORWARD_STATE_PENDING = 1;
  LEASE_FORWARD_STATE_DISABLED = 2;
  LEASE_FORWARD_STATE_CONNECTING = 3;
  LEASE_FORWARD_STATE_CONNECTED = 4;
  LEASE_FORWARD_STATE_RECONNECTING = 5;
  LEASE_FORWARD_STATE_FAILED = 6;
}

enum ForwardSide {
  FORWARD_SIDE_UNSPECIFIED = 0;
  FORWARD_SIDE_PROVIDES = 1;
  FORWARD_SIDE_REQUIRES = 2;
}

message LeaseMember {
  string name = 1;
  oneof selection {
    LabelSelector selector = 2;
    string exporter_name = 3;
  }
  bool optional = 4;
  bool allow_disabled = 5;
}

message ForwardEndpoint {
  string member_name = 1;
  string port_name = 2; // Unique within the member exporter.
}

message LeaseForwardBetween {
  repeated ForwardEndpoint endpoints = 1; // Exactly two.
}

message LeaseForwardDirected {
  ForwardEndpoint from = 1; // Must resolve to PROVIDES.
  ForwardEndpoint to = 2;   // Must resolve to REQUIRES.
}

message LeaseForward {
  string name = 1;
  oneof topology {
    LeaseForwardBetween between = 2;
    LeaseForwardDirected directed = 3;
  }
  LeaseForwardMode mode = 4;
}

message LeaseMemberStatus {
  string name = 1;
  optional string exporter_uuid = 2; // Absent when an optional member is omitted.
  int32 priority = 3;
  bool spot_access = 4;
}

message LeaseForwardStatus {
  string name = 1;
  LeaseForwardState state = 2;
  LeaseForwardMode mode = 3; // Transport actually in use when connected.
  optional string message = 4;
}

message RequestLeaseRequest {
  google.protobuf.Duration duration = 1; // unchanged
  LabelSelector selector = 2;            // unchanged; scalar form only
  repeated LeaseMember members = 3;      // NEW
  repeated LeaseForward forwards = 4;    // NEW
}

message GetLeaseResponse {
  // ... fields 1-6 unchanged; exporter_uuid set only for scalar leases ...
  repeated LeaseMemberStatus members = 7;   // NEW
  repeated LeaseForwardStatus forwards = 8; // NEW
}

message DialRequest {
  string lease_name = 1;             // unchanged
  optional string member_name = 2;   // NEW: required for member-form leases
}

// Listen is a server stream from the controller to an authenticated exporter.
// Fields 1 and 2 retain the existing client-connection instruction. Exactly
// one instruction is populated. Forward setup is idempotent by
// (lease_uid, forward_name, member_name).
message ListenResponse {
  string router_endpoint = 1;                // unchanged
  string router_token = 2;                   // unchanged
  optional ForwardSetup forward_setup = 3;   // NEW
  optional ForwardTeardown forward_teardown = 4; // NEW
}

message ForwardSetup {
  string lease_name = 1;
  string lease_uid = 2;
  string forward_name = 3;
  string member_name = 4;
  string peer_member_name = 5;
  ForwardSide side = 6;
  string local_driver_uuid = 7; // Resolved from the exporter-wide unique port.
  string local_port_name = 8;
  string peer_port_name = 9;
  LeaseForwardMode mode = 10;
}

message ForwardTeardown {
  string lease_uid = 1;
  string forward_name = 2;
}

service ControllerService {
  // ... existing RPCs unchanged ...
  rpc DialPeer(DialPeerRequest) returns (DialPeerResponse);
}

message DialPeerRequest {
  string lease_name = 1;
  string forward_name = 2;
  string member_name = 3;
}

message DirectPeerParameters {
  string endpoint = 1; // Dial target for REQUIRES; empty for PROVIDES.
  bytes ca_certificate = 2; // Trust root for the opposite endpoint.
  bytes certificate = 3;    // This endpoint's short-lived certificate.
  bytes private_key = 4;    // This endpoint's short-lived private key.
  string expected_peer_identity = 5;
}

message DialPeerResponse {
  string router_endpoint = 1;
  string router_token = 2;
  optional DirectPeerParameters direct = 3;
  optional string peer_token = 4;
  bool prefer_direct = 5;
}
```

Forward credentials are not carried on `Listen`. After receiving
`ForwardSetup`, each exporter calls authenticated `DialPeer`; the controller
returns side-specific, short-lived router and (when eligible) per-forward mTLS
credentials. The provider uses them for its configured peer listener, and the
requiring side uses them to dial and verify that listener. Private keys remain
inside the authenticated controller channel.

For an ordinary client connection, fields 1 and 2 are both populated and
fields 3 and 4 are absent. For setup or teardown, only the corresponding
optional message is populated. An old exporter reports no ports, so it cannot
be selected for a forward and never receives fields 3 or 4 of
`ListenResponse`. A new exporter checks those fields before treating fields 1
and 2 as a client connection. Unknown fields remain safe under proto3. `ReleaseLeaseRequest` and `ListLeasesRequest` are
untouched. `RouterService.Stream` and its protobuf remain unchanged, but its
token validation changes as described in DD-5.

**CLI surface** — existing commands, new flags:

- `jmp create lease --member role=selector --forward name=m.port,m.port`
  (endpoint order is irrelevant — direction is resolved at bind time; where a
  lab uses the same port name on both sides, `--forward bt` expands to it)
- `jmp get lease[s]` prints per-role exporters; `-o json|yaml|name` unchanged
- `jmp get lease <name> -o mobly`
- `jmp get exporter <name>` shows declared ports
- `jmp shell --lease <name>`, `j <role> <driver> ...`, `j forward status`
- `jmp delete lease` / `jmp update lease` need no changes

### Hardware Considerations

- **Hardware:** virtual benches need KVM-capable hosts but no physical
  radios. Physical benches use existing devices and harnesses. Mixed radio
  benches require additional adapters and are deferred (DD-11).
- **RF range and isolation:** physical radio peers must be within range.
  Shared labs may need shielded enclosures or channel planning. Exporter
  labels such as `rf-domain: rack-3` express placement constraints for
  selectors; the controller does not measure RF interference.
- **Bluetooth latency:** HCI flow control and audio buffering can be more
  sensitive than supervision timeouts. Measure both router and direct paths
  against the intended workloads (DD-4).
- **Wi-Fi simulation:** vhost-user needs a frame bridge, and TCP adds
  head-of-line blocking that may affect medium timing (DD-10).
- **Projection receiver:** virtual benches use a software receiver, such as
  Google's Desktop Head Unit (see *Projection example*).
- **Listener isolation:** fixed `requires` addresses rely on each exporter
  owning its network namespace. Multiple host-networked exporters sharing
  one machine are unsupported.
- **Member loss:** report `Ready=False` with the failed role and retain
  surviving members for diagnostics (DD-3).

## Design Decisions

### DD-1: Multi-exporter representation — extend `Lease` vs. a new CR

**Alternatives considered:**

1. **Extend `Lease`** with `spec.members[]` / `status.members[]`; a
   single-exporter lease uses the same selection logic.
2. **A new `LeaseGroup` CR owning N child `Lease` CRs.**
3. **Client-side coordination only** — the client acquires N leases and
   correlates them by tag.

**Decision:** Option 1 — extend `Lease`.

**Rationale:** An exporter claim is stored on the lease. The current controller writes
`lease.Status.ExporterRef` and checks other active leases through
`ListActiveLeases` → `attachExistingLeases` → `filterOutLeasedExporters`.
Writing all member claims in one `Status().Update` prevents a lease from
persisting a partial acquisition.

Child leases would bind independently and require acquisition timeouts,
release-and-retry behavior, and contention handling. Client-side coordination
has the same partial-acquisition problem. Both approaches also need a way to
select a member when dialing.

This atomic write does **not** guarantee exclusivity across leases. Two
reconcilers can read stale claims and write conflicting selections to
different lease objects. That race already exists; larger benches create
more opportunities to encounter it. A global scheduler remains separate
work, as noted in the controller's existing TODO.

### DD-2: Keep `status.exporterRef` scalar; add `status.members[]` alongside

**Alternatives considered:**

1. **Keep the scalar, add a parallel list.** `status.exporterRef` stays
   authoritative for scalar-form leases and is left **nil** for every
   member-form lease, which populates `status.members[]` instead.
2. **Promote the scalar to a list** and migrate every reader.
3. **Always populate both**, setting `status.exporterRef` to the first member.

**Decision:** Option 1.

**Rationale:** Existing consumers retain the scalar field for leases requested
through the top-level selector or exporter reference. For a lease requested
through `members`, even when the list contains exactly one entry, an old
reader sees nil and treats the lease as unbound rather than selecting an
arbitrary role. Populating the scalar with the first member would misroute
consumers such as the JEP-0016 Host Orchestrator façade. Replacing it with a
list would require every consumer to migrate.

`GetLeaseResponse.exporter_uuid` follows the same convention. A scalar-form
lease returns a bare driver client. An explicit one-member or multi-member
lease returns a bench with `members[role]` and member status. The request
form, rather than the number of bound exporters, therefore determines a
stable response shape.

`DialRequest.member_name` selects a role. Omitting it for any member-form
lease returns `INVALID_ARGUMENT` listing the available roles.

### DD-3: Partial-bench behavior when a member is lost mid-lease

**Alternatives considered:**

1. **Fail the lease** — set `Ready=False`, name the failed role, keep the
   surviving members held until release or expiry.
2. **End the whole lease immediately** on any member loss.
3. **Continue silently** with the surviving members.

**Decision:** Option 1.

**Rationale:** Keeping surviving members leased lets the client collect logs and
artifacts before release. The lease reports the failed role with
`Ready=False`, and the client raises on the next call into that role.
Immediate release would remove that diagnostic access; silently continuing
could hide an incomplete test. This behavior applies after binding;
acquisition remains all-or-nothing.

### DD-4: Forward transport — router peer streams vs. client relay vs. pure P2P

**Alternatives considered:**

1. **Router peer streams with an optional direct P2P fast path.**
2. **Client-relayed** — the client pumps bytes between two streams.
3. **Direct peer-to-peer only.**

**Decision:** Option 1.

**Rationale:** The router connects exporters that cannot reach each other, including
edge devices behind NAT. Direct connections avoid router load and reduce
latency where peers are reachable. Client relay adds a dependency on the
client's network and lifetime; it remains an explicit debug mode.

In `Auto` mode, same-zone pairs attempt a direct connection first, then fall
back to the router after a bounded timeout. `Router` forces the router path
for testing, and `Direct` fails if a direct connection cannot be established.
Both paths authenticate against the lease.

Direct connections also avoid ingress-related stream failures where the
router route passes through an ingress. In the single-node prototype, a
router forward had a median round-trip time of 0.70 ms versus 0.05 ms direct;
rootcanal HCI commands took about 45 ms either way (DD-9). These measurements
do not establish cross-node or sustained-throughput limits. Phase 4's Wi-Fi
frame bridge requires separate latency testing.

### DD-5: Keep the router protobuf; bind pairing to forward claims

**Alternatives considered:**

1. **Reuse the `RouterService.Stream` RPC and forwarding path**, while
   strengthening its JWT validation for peer-pair claims.
2. **Add a peer-specific RPC** to `router.proto` with explicit A/B roles.
3. **Reuse the current shared exporter subject unchanged**, relying only on
   controller-side token issuance.

**Decision:** Option 1 — no `router.proto` change, with authorization changes
inside `RouterService.Stream`.

**Rationale:** The current router uses the JWT `sub` as its pending-stream key;
a shared subject such as `jumpstarter exporter` would allow unrelated
forwards to collide. For each forward, the controller instead sets `sub` to
the stable UUIDv5 derived from `(lease UID, forward name)`. Each signed token
also carries `lease_uid`, `forward_name`, `source_exporter`, `target_exporter`,
`source_member`, `target_member`, and `side` (`provides` or `requires`).

The router parses these claims, keys pending streams by the unique subject,
and pairs only two tokens whose exporter/member fields are reciprocal and
whose sides are complementary. A duplicate token from the same side is
rejected rather than paired. `DialPeer` has already authenticated the caller
as the bound source exporter before issuing its token, and token expiry is
bounded by the lease. This preserves the existing byte-forwarding RPC while
preventing cross-forward and same-side pairing. A second RPC would duplicate
the stream path without improving these checks.

### DD-6: Named ports with direction, not addresses

**Alternatives considered:**

1. **Named ports on both ends**, each declaring `provides` or `requires`;
   the lease references names only.
2. **Raw addresses in the lease** — `from: headunit.rootcanal`,
   `to: {member: phone, listen: 127.0.0.1:7300}`.
3. **Untyped named endpoints** — names on both ends but no direction.

**Decision:** Option 1.

**Rationale:** Named ports let exporter configuration own addresses. A lease can
reference `headunit.rootcanal` and `phone.controller` without knowing their
loopback addresses or port numbers.

Direction allows the controller to reject invalid connections without
understanding the device protocol. For example, a Bumble host can attach to
a rootcanal HCI controller; connecting two rootcanal HCI services cannot
provide that relationship. The controller rejects the latter because both
ports declare `provides`.

### DD-7: Ports are an optional part of the exporter report

**Alternatives considered:**

1. **A new optional repeated `ports` field** on `DriverInstanceReport`,
   mirrored into `ExporterStatus.Devices[]`.
2. **Encode ports as driver-instance labels**, which already flow through to
   `ExporterStatus.Devices[].Labels` — zero proto and CRD change.
3. **No reporting** — ports live only in exporter config, and forwards fail
   at connect time if misconfigured.

**Decision:** Option 1. Exporters without reported ports can join leases
but cannot participate in forwards.

**Rationale:** Reported ports support discovery through `jmp get exporter` and
validation before a forward starts. Configuration alone would defer errors
to connection time and leave clients unable to discover available names.

Labels would overload driver metadata: a provided port can correspond to a
`TcpNetwork` child, while a required port describes a connection the driver
needs. A structured field represents both without synthetic driver entries
or separate label conventions.

An absent `ports` field defaults to an empty list, so old exporters continue
to register and serve ordinary leases. Reported names are exporter-wide
unique; duplicate names across driver instances reject registration, making a
lease endpoint unambiguous. The local `listen` address stays in exporter
configuration and is not reported.

### DD-8: No protocol taxonomy; direction plus an optional tag

**Alternatives considered:**

1. **A `medium` enum** (`bluetooth | wifi | uwb | serial | can`) matched
   between endpoints.
2. **A `format`/wire-protocol token** matched for equality.
3. **Direction only**, with an optional free-form `protocol` compared solely
   when both ends declare it.

**Decision:** Option 3.

**Rationale:** A medium name does not establish wire compatibility: rootcanal HCI
and netsim `PacketStreamer` both carry Bluetooth traffic but use different
protocols. A wire-format token alone also misses direction errors such as
connecting two HCI controllers.

Direction is required. A free-form `protocol` adds an optional compatibility
check when both ends declare it, without a centrally maintained taxonomy.
If either end omits the tag, the forward may connect despite incompatible
protocols and fail when data is exchanged. Forwarding guarantees byte
transport, not protocol compatibility.

### DD-9: Where simulated media attach

**Alternatives considered:**

1. **Share one rootcanal** — the second CVD is launched with
   `--rootcanal_instance_num` pointing at the first, and a forward supplies
   that instance's HCI port.
2. **Federate two rootcanals at the link layer** — each CVD keeps its own
   controller, and a forward joins them at rootcanal's `link_port`.
3. **A shared virtual controller from Bumble** — its virtual `Controller`
   plus `RemoteLink` relay, replacing the simulator entirely.
4. **A dedicated bridge driver tier** — purpose-built `LinkEndpoint` drivers
   that know about radios.
5. **Inside the guest** — a shim in Android proxying Bluetooth/Wi-Fi at the
   HAL or socket layer.

**Decision:** Use option 2 for Phase 1, with option 1 as a fallback.
Option 3 is the proposed extension beyond Cuttlefish; its use as a Cuttlefish
controller remains unverified.

**Rationale:** Cuttlefish reaches rootcanal through TCP ports derived from
`rootcanal_instance_num`: HCI `7300+N`, link `7400+N`, test `7500+N`, and
BLE link `7600+N`. These provide two attachment choices:

- Sharing one controller requires the forward before the second CVD boots.
  That controller becomes a shared failure point.
- Federating controllers lets each CVD keep its own radio controller. It
  requires standalone rootcanal (`--netsim_bt=false`), two forwarded link
  ports, and an `add_remote` command on the private test channel after the
  forwards are established. Netsim does not expose these link ports.

Bumble's virtual `Controller` and `RemoteLink` offer a programmable shared
medium. This requires a controller driver; the existing `bt-peer` creates a
host `Device` and can use a forwarded HCI transport without Python changes.
Sustained A2DP performance with a Python controller still needs testing.

A dedicated bridge-driver interface is unnecessary for these socket
connections. Guest-side shims would change the stack under test. Protocol
setup remains the responsibility of endpoint drivers.

**Prototype results (2026-09-01–02).** Manual tests used two Cuttlefish Pods
on one kind node with stand-in TCP relays, exercising shared and federated
rootcanal configurations. Tests covered discovery, SSP pairing, HFP, A2DP,
AVRCP, audio streaming, and reconnection after toggling Bluetooth. A later
test connected a CVD to a Bumble `bt-peer` over Jumpstarter's router. Adding
an HFP Audio Gateway to the peer also delivered a simulated incoming call
to the head unit. These tests exercised Bumble as a host, not as a controller.

The measured router-forward round-trip was 0.70 ms median (p90 0.91 ms,
n=100), versus 0.05 ms direct to the same endpoint. Rootcanal HCI commands
took about 45 ms on either path. Multi-node tests remain required.

The prototypes identified these implementation requirements:

- **Controller recovery:** connecting to rootcanal's test port and closing
  before its banner is written can abort rootcanal. Its process restart did
  not restore the guest connector; recovery required `cvd restart`. Keep
  the test channel private, derive forward health from the stream rather
  than connect-and-close probes, and report controller loss as `Failed` and
  lease `Degraded`.
- **Unique Bluetooth addresses:** rootcanal assigns and reuses addresses
  such as `da:4c:10:de:00:<n>`. Federated controllers and attaching peers can
  collide. Assign member addresses before host power-on and pairing.
- **Guest addressing:** Cuttlefish guests use identical network address
  plans. L2/L3 bridges require NAT or re-addressing; L4 forwards do not.
- **Reconnection:** an ingress reload followed by a 240-second worker drain
  cut the prototype's controller and HCI streams. The exporter reconnected
  its controller stream but the stand-in forward stayed down. Forward
  endpoints must reconnect, update readiness, and notify drivers that need
  to restore protocol state.
- **Exporter identity and versions:** duplicate exporter processes split
  sessions and caused driver-UUID `KeyError` failures. A separately built
  forward endpoint with a mismatched network-driver version returned EOF.
  Run one process per identity and keep forwarding in the exporter runtime.
- **Image and bond state:** the tested GSI needed classic Bluetooth profile
  properties enabled. Bumble bonds need a keystore to survive peer restarts;
  otherwise the DUT must forget the old bond. Release leases through their
  lifecycle API; deleting a lease during acquisition left the prototype
  client retrying `not found`.

### DD-10: Wi-Fi and projection — what a forward carries

**Alternatives considered:**

1. **Forward the `mac80211_hwsim` frame socket** between members so both
   share one simulated medium.
2. **Attach at netsim's 802.11 MAC chip** — Wi-Fi as another chip kind on the
   `PacketStreamer` port.
3. **Forward the projection session at L4** — carry the projection's own TCP
   connection, over the guest's real Wi-Fi NIC, and simulate no radio.

**Decision:** Deliver option 3 in Phase 2. Defer simulated Wi-Fi and the
Bluetooth-to-Wi-Fi handover to Phase 4, using netsim where supported or a
frame bridge for `mac80211_hwsim`/`wmediumd`.

**Rationale:** An L4 forward exercises projection version negotiation, TLS,
service discovery, video, audio, and input. It does not exercise Bluetooth
credential exchange, Wi-Fi Direct association, RSSI, roaming, or channel
loss. This provides a useful projection test before medium simulation is
available.

Cuttlefish's `virtio_mac80211_hwsim` connects to `wmediumd` through vhost-user,
which uses shared memory and file-descriptor passing. That connection cannot
be forwarded as a byte stream across hosts. Option 1 needs a bridge on each
exporter to terminate the local interface and exchange 802.11 frames, with
datagram support considered separately. Option 2 can reuse netsim's packet
transport where its Wi-Fi support is sufficient.

For L4 forwarding, a route and forwarding rule expose the guest's Wi-Fi
address through its OpenWrt AP. The driver can therefore provide the same
projection server through ADB (`projection`) or the guest's Wi-Fi address
(`projection-wifi`). The test selects the path (DD-13).

**Prototype results (2026-09-01).** A vendor phone image in one Cuttlefish Pod
projected to a desktop receiver in another Pod through a stand-in relay,
first over ADB and then over the guest's Wi-Fi address. The session completed
version negotiation, TLS, service discovery, and the phone's first-run flow,
and displayed the launcher with maps, media, and telephony. A mostly static
screen transferred about 0.6 MB of video in three minutes; this does not
establish a sustained-throughput limit.

The receiver required a display and open stdin. An aborted session required
a phone-side server restart. The phone joined Wi-Fi only after its validated
Ethernet connection was removed. These requirements belong in the reference
drivers.

### DD-11: Mixed physical/virtual benches — deferred

**Alternatives considered:**

1. **Defer** — v1 supports homogeneous benches only: two virtual devices or
   two physical devices.
2. **In scope now**, via a gateway exporter owning a real radio adapter,
   presented as a `provides` port that the virtual side attaches to as it
   would to any other controller.

**Decision:** Option 1 — defer.

**Rationale:** Joining physical and simulated radios requires a nearby hardware
adapter and a way to allocate that shared RF resource. Bluetooth may use a
USB HCI adapter; Wi-Fi needs suitable radio hardware. Requiring this now
would add hardware integration to the lease and forwarding work.

A future gateway exporter can own the adapter and expose a provided port.
Its allocation model remains open (Future Possibilities). A software model
of a physical peer is useful but does not test the physical device's stack.

### DD-12: Access policy and port validation timing

**Alternatives considered:**

1. **Per-member policy evaluation, bind-time port validation.** Each member
   is evaluated against `ExporterAccessPolicy` exactly as a standalone lease
   would be; forwards are validated against the bound exporters' reports.
2. **Selection-time port validation** — ports surfaced as exporter CR labels
   so member selectors only match exporters that have the required ports.
3. **A bench-shaped policy CRD** with rules over lease shape, size, and count.

**Decision:** Option 1 for v1; option 2 recorded as a follow-on that depends
on JEP-0017; option 3 deferred.

**Rationale:** Each member must satisfy the same access policy as an independent
lease request. Lease priority is the minimum member priority, and duration
is bounded by the minimum per-member `maximumDuration`.
`status.members[].priority` preserves the individual values for inspection.

The existing selector pipeline matches exporter CR metadata labels, while
ports are reported in `ExporterStatus.Devices[]`. V1 therefore validates
ports after selecting and binding exporters. An invalid forward leaves the
members held for inspection until release or expiry.

Selection-time validation would avoid holding exporters whose ports cannot
satisfy the request. JEP-0017 proposes exposing reported device information
through selectable labels. Bench-level policy and quota are deferred until
there is operational experience with multi-member leases.

### DD-13: Infer direction, never infer topology

**Alternatives considered:**

1. **Infer direction only.** A forward names its two endpoints in any order
   (`between`); the controller decides which is `provides` and which is
   `requires` from the reported ports. Which ports are joined stays explicit.
2. **Infer topology too** — auto-forward every `provides`/`requires` pair the
   bound exporters happen to expose, with no `forwards` stanza at all.
3. **Infer nothing** — the author states `from` and `to` on every forward.

**Decision:** Option 1, with option 3 retained as an explicit form.

**Rationale:** Port direction is already in the exporter report. The
controller can resolve it without requiring the lease author to repeat it.
The test must still choose which ports to connect: forwarding `projection`
and forwarding `projection-wifi` exercise different paths.

Automatic topology would make connections depend on the ports exposed by
whichever exporters were selected. Multiple possible matches would be
ambiguous and could create data paths the test did not request.

Explicit `from`/`to` remains available when a test requires a particular
direction; the controller validates it against the report. CLI shorthand may
expand into explicit `spec.forwards[]` entries before submission so the
stored topology remains inspectable.

## Design Details

### Deployment assumptions

Each exporter must own its network namespace and run one process per
exporter identity.

A virtual exporter can run in its own Pod, as proposed in JEP-0016. A
physical exporter can run on a dedicated edge device or in an isolated
container. This allows `requires` ports to use fixed loopback addresses and
keeps host-scoped simulator operations, such as netsim reset, within one
exporter's lease.

Multiple host-networked exporters on one machine are unsupported: their
listeners can collide and simulator control operations can cross lease
boundaries. Host networking remains usable for a single-exporter host or
local development.

Duplicate processes under one identity can both register but hold different
driver sessions, causing routed calls to fail. Each member exporter runs one
replica; duplicate registration must be rejected.

Network-zone configuration determines direct eligibility. Same-zone peers
attempt direct connections; exporters without a suitable peer route use the
router (DD-4).

### Binding: one pass, one write

The existing `reconcileStatusExporterRef` generalizes to
`reconcileStatusMembers`, keeping its selection pipeline intact per member:

```{mermaid}
flowchart TD
    select["Select policy-approved exporters<br/>matching the member selector"]
    filter["Exclude offline exporters, active claims,<br/>earlier picks, and exporters still cleaning up"]
    candidate["Keep the best candidate in memory<br/>Write no claims yet"]
    more{"More members?"}
    complete{"Every required member<br/>has a candidate?"}
    pending["Set Pending / Unsatisfiable<br/>Name the failing role; write no claims"]
    requeue["Requeue"]
    bind["Set status.members to all candidates<br/>Set priority to the minimum member priority"]
    commit["Commit one atomic Status().Update()"]

    select --> filter --> candidate --> more
    more -->|"Yes: next member"| select
    more -->|No| complete
    complete -->|No| pending --> requeue
    complete -->|Yes| bind --> commit
```

Candidates remain in memory until every required member resolves. The
selection pass excludes exporters already assigned to another member, so
two roles with the same selector receive distinct exporters.

The scalar path uses the same selection code with a synthetic member and
writes the result to `status.exporterRef` (DD-2).

The existing cross-lease race remains: reconcilers can read stale claims
and commit conflicting selections to different lease objects. A later
reconcile detects the conflict and rebinds; a global scheduler is separate
work (DD-1).

### Lease state

```{mermaid}
flowchart TD
    pending["Pending"]
    available{"All required members available?"}
    unsatisfiable["Unsatisfiable<br/>No exporters held"]
    bound["All member claims committed<br/>in one status write"]
    forwards["ForwardsUp"]
    ready["Ready"]
    degraded["Degraded"]
    ended["Ended"]

    pending --> available
    available -->|No| unsatisfiable
    unsatisfiable -->|Requeue| pending
    available -->|Yes| bound
    bound -->|"Forwards validated and requested"| forwards
    forwards -->|"All forwards connected"| ready
    forwards -->|"Forward failure"| degraded
    ready -->|"Member lost or forward failure"| degraded
    ready -->|"Release or expiry"| ended
    degraded -->|"Release or expiry"| ended
```

Conditions reuse the existing `LeaseConditionType` values — `Pending`,
`Ready`, `Unsatisfiable`, `Invalid` — with `ForwardsReady` and `Degraded`
added. `Ready` for a member-form lease requires all required members bound and every
non-disabled forward connected. A forward whose optional endpoint was omitted
has explicit `Disabled` status and does not gate readiness. Expiry,
`status.ended`, the `jumpstarter.dev/lease-ended` label, and `spec.release`
retain their existing behavior.

### Forward validation and establishment

Validation happens after binding, because the report that proves a port
exists belongs to a bound exporter, not to a selector (DD-12). For each
`spec.forwards[]` entry the controller checks, against
`ExporterStatus.Devices[].Ports`:

1. Every endpoint names a declared member — enforced by CEL at admission,
   before this point. If either role is an omitted optional member, validation
   stops for that entry and records `Disabled` with the omitted role named.
2. Both named ports exist on the respective bound exporters, and each port
   resolves to one driver UUID because report registration enforces
   exporter-wide unique names.
3. **Direction resolves.** For a `between` forward, exactly one endpoint must
   report `PROVIDES` and the other `REQUIRES`; the controller assigns the
   roles accordingly (DD-13). For an explicit `from`/`to` forward, the stated
   roles must match what the exporters report.
4. If both ports declare `protocol`, the values are equal (DD-8).

A failure sets `Invalid`, names the forward and reason, and leaves members
bound for inspection. A disabled optional-member forward is not a validation
failure and receives no setup instruction or token.

Setup follows *How a forward comes up*. The controller derives a router
subject from `(lease UID, forward name)` using UUIDv5 in a fixed namespace,
keeping it stable across reconciles. Both tokens use that value as `sub`, use
`aud: https://jumpstarter.dev/router`, carry reciprocal member, exporter, and
side claims, and expire no later than the lease. `RouterService.Stream`
validates those claims as described in DD-5.

The direct attempt has a short, bounded timeout and is not raced with a
router connection. Direct mode requires controller-issued per-forward mTLS:
both sides validate the peer certificate identity before the requiring side
transmits `peer_token` inside the encrypted channel. A plaintext or
server-authentication-only connection is rejected. `Direct` fails without
fallback; `Router` skips the direct attempt. Status records the transport
used and the reason for any fallback.

**Direct eligibility.** Both members must report the same non-empty
`NetworkZone`, and the `provides` side must report a `PeerEndpoint`. The zone
is an opaque value supplied by deployment configuration, such as one value
per cluster network. The controller does not infer reachability from IP
addresses. An unreachable peer falls back to the router in `Auto` mode.

**Reconnection.** The `requires` endpoint keeps its listener open and
re-establishes the peer path with backoff after a network interruption, router
restart, or ingress reload. One accepted local TCP connection and one peer
stream form a single splice: if the peer stream closes, the exporter closes
that local connection and never attaches a replacement stream to it. A driver
that reconnects gets a fresh splice. Reconnect events invoke an explicit
endpoint-driver recovery hook for protocols that dial only once or must
restore state, such as restarting a projection server. Forward state comes
from the stream, without probing the service.

**Failure modes and handling:**

| Failure | Behavior |
| --- | --- |
| Named port absent on a bound exporter | Lease `Invalid`; members stay bound for inspection |
| Both endpoints `provides`, or both `requires` | Lease `Invalid`; direction cannot resolve (DD-6, DD-13) |
| Explicit `from`/`to` contradicts the reported directions | Lease `Invalid` naming the forward and the reported roles |
| Declared protocols disagree | Lease `Invalid` (DD-8) |
| Protocols differ and at least one tag is absent | Validation passes; protocol errors may occur when data is exchanged (DD-8) |
| Forward references an undeclared member | Rejected by CEL at admission; lease never created |
| Forward references an omitted optional member | Forward `Disabled` naming the role; no setup or token; lease may become `Ready` |
| Duplicate port names in one exporter's reports | Exporter registration rejected with `INVALID_ARGUMENT` |
| `listen` address already bound on the exporter | Lease `Invalid` naming the port and address |
| Router stream drops mid-lease | Re-dial with backoff; `Reconnecting`; `Degraded` after a grace period |
| Ingress/proxy reload cuts the peer stream | Reconnect with backoff and notify endpoint drivers |
| Direct dial fails or times out | In `Auto`, fall back to the router and record the reason; in `Direct`, fail |
| A member's exporter disappears | Peer's stream resets; lease `Degraded` naming the role (DD-3) |
| Client releases the lease | Forwards torn down first, then the lease ends normally |

### Reference drivers for projection

The reference drivers handle device setup and protocol recovery:

- **`cuttlefish` (phone).** Prepare the image with ADB enabled, the exporter
  key installed, and the Bluetooth profiles needed by the test. These are
  per-image preparation steps. Use `cvd restart` for recovery that retains
  userdata. For `projection-wifi`, disable the guest's Ethernet connection,
  join the instance's AP, and configure the route to the guest. Start the
  projection server on request and restart it on forward-reset events.
- **`projection-rx` (head unit).** Run the receiver with a display, dummy
  audio device, and open console for input commands. Capture screenshots
  from the display. Start the receiver on a client call after the forward
  listener is available, since it dials immediately on startup.

- **`bt-peer` (phone).** Add a `profiles:` list to configure HFP Audio
  Gateway alongside A2DP, and persist the bond keystore for the lease lifetime
  so restarting the peer does not invalidate the DUT's link key.

The drivers configure only local endpoints. The phone exposes named ports;
`projection-rx` dials its local listener, such as `127.0.0.1:5277`. The lease
specifies the connection between them.

### Concurrency and ordering

Reconciliation is single-writer per lease (standard controller-runtime work
queue), so no intra-lease locking is needed, and member selection is pure
computation followed by one write. Forward splicing on the exporter side runs
in the existing per-driver task group, so a stalled forward cannot block
driver calls on other children.

For client-started drivers such as `bt-peer`, the listener must be available
before `start()` dials it. Shared-rootcanal guests need the forward before
boot, while federated rootcanals need a join after establishment (DD-9).
Provisioner ordering remains an unresolved question.

### Security

- **Access policy:** evaluate each member against `ExporterAccessPolicy` as
  for a direct request (DD-12).
- **Forward authorization:** `DialPeer` verifies that the caller is the
  exporter bound to the named member and that the forward includes it.
  Tokens expire with the lease. Access is limited to the explicitly named
  peer and port.
- **Port exposure:** only declared ports can participate in forwards. A
  `requires` listener uses an exporter-configured address and exists only
  while leased.
- **Direct authentication and confidentiality:** the optional peer listener
  is disabled by default and accepts only controller-issued, short-lived
  per-forward mTLS credentials. Both sides verify the expected exporter and
  member identity from the certificate before `peer_token` is sent inside
  the encrypted channel. It is separate from device ports. Network policies
  should allow the authenticated peer port while blocking peer access to
  unauthenticated simulator ports. JEP-0016 is expected to supply this policy
  with exporter Pods.
- **Broader exporter authentication:** mTLS is also a stronger future
  authentication mechanism for exporter-to-controller and exporter-to-router
  connections. Unlike the current bearer token, it proves possession of a
  private key while protecting the channel and can bind the certificate
  identity to one `Exporter`. Lease and forward authorization still apply;
  possession of an exporter certificate alone never grants access to a port.
  Kubernetes Pod Certificates are one possible source of this workload
  identity, while physical and non-Kubernetes exporters require an equivalent
  issuer and enrollment path.
- **Membership:** `members` is immutable after creation.
- **Physical RF:** devices in a shared lab are audible to others in range;
  lease authorization does not isolate radio traffic.
- **Simulator control:** standalone rootcanal listens on `0.0.0.0` without
  client authentication. Keep its test channel private because it can
  re-address devices, join controllers, and trigger the crash described in
  DD-9. Drivers may expose HCI and link ports through authorized forwards;
  network policy must block direct access to the underlying ports.

### Observability

JEP-0013 telemetry adds `lease.member` alongside `lease.name` and records
member count on the lease-acquisition metric (creation to `Ready`).
Per-forward telemetry records bytes in each direction, reconnect count,
selected transport, and direct-dial fallback rate. Same-zone fallback can
indicate an unavailable peer listener, incorrect zone configuration, or a
blocking `NetworkPolicy`.

Reconnects also emit events for drivers that must restore protocol state,
such as the phone's projection server. Forward status exposes reconnection
and degradation.

Where netsim supplies the simulated medium, `jumpstarter-driver-netsim` can
start, stop, and download pcap captures through its REST control API. These
captures can be attached to test results alongside forward metrics.

## Test Plan

### Unit Tests

- `LeaseSpec` CEL validation: extended top-level one-of rule,
  members/scalar mutual exclusion, per-member rejection when both or neither
  of `selector` and `exporterRef` are usable, DNS-label and reserved role-name
  checks, unique member and forward names, forwards referencing declared
  members, and member immutability.
- Member selection: all-or-nothing binding, no self-collision, correct
  `Unsatisfiable` role naming, and — the property DD-1 rests on — that a
  reconcile which cannot satisfy every member writes **no** member claims.
- Scalar-form regression: existing lease controller tests pass unmodified
  and retain `status.exporterRef`. An explicit one-member lease instead
  populates one `status.members` entry and leaves `status.exporterRef` nil.
- Aggregation: priority = min(member priorities), duration clamped to
  min(member `maximumDuration`).
- Port report round-trip: driver-declared ports reach
  `ExporterStatus.Devices[].Ports`; an exporter reporting none is treated as
  non-forwardable; a report with no `ports` field is accepted unchanged; and
  duplicate names across two driver reports reject registration.
- Forward validation: missing port, `provides→provides`,
  `requires→requires`, protocol disagreement, and `listen` collision each
  produce `Invalid` with the offending forward named. A forward with an
  omitted optional endpoint instead becomes `Disabled` and does not gate
  lease readiness.
- `Dial` without `member_name` on any member-form lease returns
  `INVALID_ARGUMENT` listing roles; with a valid role, routes correctly.
- `DialPeer` token issuance: identical unique `sub` for both ends, reciprocal
  member/exporter and complementary side claims, stability across reconciles,
  expiry clamped to lease end, and rejection when the caller is not bound to
  the named member. Router tests reject unrelated or same-side tokens that
  carry the same subject.
- `ListenResponse`: forward setup and teardown decode alongside the unchanged
  client-connection fields; setup carries the resolved local driver UUID;
  old exporters never receive forward instructions.
- Python client: scalar `connect()` returns a bare client; explicit one-member
  and multi-member requests both return a bench with `members[...]`; arbitrary
  role attributes are not exposed; `bench.forwards[...]` reports state; and
  calls into a `Degraded` role raise.

### Integration Tests

Against a kind cluster with the controller and mock exporters (`e2e/`):

- Two mock exporters, one two-member lease, one forward between an
  `EchoNetwork` provides-port and a requires-port — end-to-end establishment
  through the real router with no device-specific code.
- Contention: more concurrent multi-member leases than capacity, asserting
  every lease is either fully bound or holding nothing.
- Lease expiry, explicit release, and client disconnect; assert no leaked
  router streams, no exporters left claimed, and no listeners left bound.
- Router-mode vs. direct-mode selection: `Auto` picks direct for two
  same-zone exporters only after mutual TLS identity verification, never
  sends `peer_token` before the encrypted handshake, falls back to the router
  when the peer dial is blocked, records the mode actually used, and never
  falls back under `mode: direct`.
- `jmp get lease -o mobly` output validated against Mobly's testbed schema.
- **Compatibility**: an N-1 client against an N controller for the full
  single-exporter workflow; an N client issuing a single-exporter lease
  against an N-1 controller; an N-1 *exporter* (reporting no ports)
  registering against an N controller.

### Hardware-in-the-Loop Tests

- **Virtual devices:** two Cuttlefish exporters in separate Pods pair over
  forwarded rootcanal ports. Phase 4 adds Wi-Fi association and netsim pcap
  capture where supported. Run on KVM-capable CI nodes.
- **Virtual device and peer:** connect a Cuttlefish exporter to `bt-peer`.
  Assert pairing and `avdtp_connected`; with HFP Audio Gateway enabled,
  assert that an incoming call reaches the head unit's telephony stack.
  This test needs only one guest and should run on each merge.
- **Virtual projection:** connect a phone CVD and `projection-rx` in separate
  Pods through `projection-wifi`. Assert that the receiver reaches the
  launcher and matches an expected screenshot.
- **Physical devices:** run a phone and head unit on exporters on different
  lab hosts, using a labeled runner with the required hardware.
- **Latency:** publish HCI round-trip distributions for router, direct, and
  host-local paths. Include cross-node, sustained A2DP, and physical-controller
  tests to establish supported workloads.
- **Forward resilience:** cut a live stream through a router restart or
  ingress reload. Assert that the affected local TCP connection closes, a
  later local connection receives a new peer stream, metrics and events are
  emitted, and an endpoint-driver recovery hook restores protocols that do
  not reconnect themselves.

### Manual Verification

- `jmp shell --lease` ergonomics: role-prefixed driver calls,
  `j forward status`, Ctrl+C teardown leaving no held leases.
- `jmp get exporter` port discovery: a user who has never seen an exporter's
  config can construct a working forward from its output alone.
- An existing Mobly multi-device suite run unmodified against an exported
  testbed.
- `jmp get leases` and `kubectl get leases` on a mixed set of single- and
  multi-member leases.

## Acceptance Criteria

**Lease plane**

- [ ] `spec.members[]` / `status.members[]` on `Lease`, with extended CEL
      validation and member immutability
- [ ] Binding is one `Status().Update`: a reconcile that cannot satisfy every
      required member writes no member claims (verified by test)
- [ ] Two members with identical selectors bind two distinct exporters
- [ ] Contention test: concurrent multi-member leases over insufficient
      capacity leave no lease partially holding devices
- [ ] `ExporterAccessPolicy` evaluated per member; a multi-member lease
      cannot reach an exporter its client could not lease directly
- [ ] Lease priority = min(member priorities); duration clamped to
      min(member `maximumDuration`)
- [ ] `status.exporterRef` unchanged for scalar-form leases and nil for every
      member-form lease, including an explicit one-member list; existing
      scalar controller tests pass unmodified
- [ ] Scalar Python connections return a bare client; explicit one-member and
      multi-member connections return `bench.members[...]`
- [ ] `Dial` without `member_name` on a member-form lease returns
      `INVALID_ARGUMENT` naming the roles

**Ports and forwards**

- [ ] `PortReport` is optional on `DriverInstanceReport`; exporters reporting
      no ports register and operate unchanged
- [ ] Port names are exporter-wide unique; duplicate names across driver
      instances reject registration, and setup names the resolved driver UUID
- [ ] Declared ports appear in `ExporterStatus.Devices[].Ports` and in
      `jmp get exporter` output
- [ ] `listen` addresses never appear in any report, CR status, or lease spec
- [ ] Admission rejects duplicate forward names before deriving router
      subjects, and rejects member selection with both/neither source set
- [ ] Forward validation rejects missing ports, `provides→provides`,
      `requires→requires`, and declared-protocol mismatch, naming the forward
- [ ] A forward whose optional endpoint is omitted is `Disabled`, receives no
      credentials, and does not prevent lease readiness
- [ ] Additive `ListenResponse` setup and teardown instructions identify the
      lease, forward, side, local driver UUID, and ports; credentials are
      returned only by authenticated `DialPeer`; old exporters receive neither
- [ ] Forwards establish over `RouterService` with no `router.proto` change;
      its authorization pairs only reciprocal claims under a unique subject
- [ ] Direct fast path uses per-forward mTLS, verifies peer identity before
      sending `peer_token`, falls back automatically, and is observable
      (mode + fallback-rate metrics)
- [ ] `Auto` resolves to a direct peer connection for two same-zone
      in-cluster exporters with peer listeners; router fallback is recorded
- [ ] `bt-peer` participates as a `requires` endpoint with **no Python
      changes** — exporter configuration only, relying on its existing
      `open_transport(self.transport)` passthrough
- [ ] Phase 1 reuses network drivers for byte forwarding, with rootcanal
      join, address assignment, and recovery handled by endpoint drivers (DD-9)
- [ ] A Bumble-based shared controller exists as a driver exposing a
      `provides` port, for benches outside Cuttlefish and for N-way media
- [ ] After a peer-stream cut, the endpoint closes the associated local TCP
      connection and re-establishes the peer path with backoff; a reconnecting
      driver obtains a new splice, while protocols without reconnect behavior
      recover through an explicit endpoint-driver hook. No replacement stream
      is attached to an existing local TCP connection
- [ ] A second exporter process registering under a live identity is
      detected and refused
- [ ] Byte fidelity and reset semantics verified by the `EchoNetwork`
      integration test

**Topologies** (each a phase gate, in order)

- [ ] **Phase 1 — Virtual Bluetooth:** two CVDs in Pods on separate nodes
      complete BR/EDR discovery and pairing through federated rootcanals or
      a shared HCI instance, in CI
- [ ] **Phase 2 — Virtual projection:** a phone CVD and `projection-rx` in
      separate Pods reach the projection launcher over a forwarded port,
      in CI without lab hardware
- [ ] **Phase 3 — Physical devices across hosts:** a phone and head unit on
      exporters on different lab hosts complete a projection session
- [ ] **Phase 4 — Virtual Wi-Fi medium:** two CVDs in separate Pods associate
      through bridged `mac80211_hwsim`/`wmediumd` or a shared netsim 802.11
      chip and complete the Bluetooth-to-Wi-Fi projection handover
- [ ] Publish router-forward HCI latency measurements and supported workload
      limits, including cross-node and sustained A2DP tests

DD-9 and DD-10 record the manual prototype results. Automated CI, CVD-to-CVD
forwarding over the router, and multi-node verification remain outstanding.

## Graduation Criteria

### Experimental

`members`, `forwards`, and port reporting ship behind a controller feature
gate after Phases 1–3 are complete. Collect feedback on member counts,
direct-connection and fallback rates, the eight-member limit, listener
collisions, scalar-field compatibility, and bind-time port validation.

### Stable

- Phases 1–4 complete, with Phase 4 green in CI for 30 consecutive days
- At least two `requires`-side drivers outside this JEP's reference set
  (evidence the port model generalizes)
- No API changes to `members` / `forwards` / `PortReport` for one release
  cycle
- Selection-time port validation either shipped on JEP-0017 or explicitly
  deferred with a rationale

## Backward Compatibility

The schema and protocol changes are additive. DD-2 defines compatibility
for `status.exporterRef`.

- **CRD**: `Lease` gains two optional spec lists and two optional status
  lists; `ExporterStatus.Devices[]` gains an optional `ports` list. No
  existing field changes type, meaning, or default. `Exporter`,
  `ExporterAccessPolicy`, `ExporterSet`, and `VirtualTargetClass` are
  otherwise untouched. Every lease that exists today validates unchanged.
- **`status.exporterRef`**: unchanged for every lease that does not pass
  `members`. Every member-form lease, including an explicit one-member list,
  leaves it nil, which existing consumers already read as "not bound yet"
  (DD-2), so the JEP-0016 façade, `jmp get leases`, `Dial` and JEP-0013
  telemetry keep working; they change only to *support* benches.
- **Driver report and drivers**: `ports` is a new optional repeated field, so
  an exporter built before this JEP reports none and is treated as
  unable to participate in forwards. Ports are declared in exporter
  configuration; `bt-peer` needs no Python changes to use a forwarded HCI
  endpoint.
- **Protocol**: new fields on existing messages and one new RPC.
  Unknown fields are ignored by proto3, so an N-1 client talks to an N
  controller unchanged. An N client requesting `members` from an N-1
  controller has them silently dropped — so the client probes for `DialPeer`
  (or a controller version) and fails with a clear message rather than
  acquiring a one-device lease it will misuse.
- **Operator upgrade**: a CRD schema addition, a standard bundle bump with no
  conversion webhook. Member-form leases must be removed before rollback;
  scalar leases remain compatible.
- **Coexistence**: scalar- and member-form leases share one exporter pool,
  one scheduler, and one selection implementation.

## Consequences

### Positive

- One lease acquires all required members in one status write and gives them
  a shared lifetime.
- Exporters can run on different hosts while retaining role-based access,
  lease policy, and telemetry.
- Forwards reuse existing stream primitives and `RouterService`; named ports
  are discoverable through exporter reports.
- The port model supports Bluetooth, projection, CAN, and serial connections
  without adding protocol-specific logic to the controller.

### Negative

- Consumers must handle scalar and member-list lease forms.
- Port declarations add configuration, and fixed listeners depend on network
  namespace isolation.
- Bind-time port validation holds devices even when a forward is invalid.
  Omitted protocol tags allow compatibility errors to surface at runtime.
- Individual members cannot be released early.
- Forwarding adds local listeners and, optionally, an authenticated peer
  listener to exporters.
- Timing-sensitive protocols and Wi-Fi simulation require further testing
  and may need upstream changes.

### Risks

- **Wi-Fi transport:** vhost-user requires a frame bridge, and TCP
  head-of-line blocking may prevent reliable medium simulation. Phase 4 may
  require direct mode or separate datagram support (DD-10).
- **Projection artifacts:** the phone image, app, and receiver must be
  supplied by the lab. Phase 2 CI needs an artifact source that does not
  require project redistribution. Drivers accept artifact locations as
  configuration.
- **Bluetooth latency:** single-node pairing results do not establish
  cross-node or sustained A2DP performance. Publish workload-specific
  latency measurements.
- **Simulator failures:** the rootcanal test-channel crash and guest recovery
  behavior require private control ports, explicit health reporting, and
  driver-owned recovery (DD-9).
- **Stream interruption:** ingress reloads and router restarts can cut
  long-lived forwards. Endpoints reconnect and emit recovery events; tests
  must cover deliberate stream loss. Direct connections avoid the ingress
  path where available.
- **Deployment isolation:** shared network namespaces can cause listener
  collisions and simulator resets across leases. Enforce the documented
  deployment assumptions and report bind failures clearly.
- **Binding contention:** larger benches have more opportunities to hit the
  existing cross-lease race. `MaxItems=8` bounds bench size; a global scheduler
  remains the full solution.
- **Consumer compatibility:** readers may assume every bound lease has
  `status.exporterRef`. Cover known consumers in compatibility tests.
- **Upstream interfaces:** netsim and vhost-user integration may change.
  Pin runtime images and track upstream compatibility.
- **Capacity:** per-member policy and the eight-member limit bound access;
  bench-level quota remains future work.

## Rejected Alternatives

DD-1 through DD-13 record the API and transport alternatives. Higher-level
alternatives are:

- **Keep client-managed leases:** leaves partial acquisition, independent
  lifetimes, and unmanaged device connections.
- **Add `LeaseGroup` or `LeaseSet`:** child leases require partial-acquisition
  recovery (DD-1). `LeaseSet` also suggests the interchangeable replicas of
  `ExporterSet`, rather than members with distinct roles.
- **Lease devices independently inside one exporter:** can divide control
  of a physically connected harness. A composite DUT behind one exporter
  remains supported.
- **Build a multi-device test runner:** existing runners can consume the
  leased devices. This proposal supplies allocation and connectivity.
- **Adopt a mobile test framework as the fleet layer:** this would require
  adapting its device and allocation model to Jumpstarter. Integration
  through a device or Mobly controller shim remains possible.

## Prior Art

- **LAVA MultiNode** assigns named device roles within one job and provides
  `lava-sync`, `lava-send`, and `lava-wait` for test-script coordination.
  It is a reference for grouping devices within the existing work unit.
- **Mobly** provides the testbed format used by the proposed export command.
- **Cuttlefish multi-instance connectivity** supplies the local simulator
  interfaces considered in DD-9 and DD-10.
- **Android emulator networking** provides another model for multi-device
  connectivity within one host.
- **Bumble** supplies virtual hosts, controllers, and link relays. The existing
  `jumpstarter-driver-bt-peer` demonstrates the required-port model.
- **Kubernetes gang scheduling** illustrates the coordination needed when
  claims live on separate objects. Lease member claims instead live in one
  status update, with the cross-lease race described in DD-1.
- **`kubectl port-forward`** provides a comparable byte-transport guarantee
  without checking application protocol compatibility.

## Unresolved Questions

To resolve during review:

- **Bumble controller:** validate option 3 from DD-9 with Cuttlefish.
- **Link-layer setup:** decide whether rootcanal join and address assignment
  run through a driver post-establish hook, a lease-level action, or the
  test. A driver hook keeps Bluetooth handling outside the controller.
- **Launch ordering:** define how the provisioner waits for a shared HCI
  forward before booting the second CVD, and how federation runs its join
  after the link forwards are available.
- **Readiness:** distinguish a forward listener being available from an
  application connection being established. Client-started drivers need
  the former before they can create the latter.
- **Listener allocation:** keep fixed addresses with collision validation,
  or allocate ephemeral ports and pass the address to the driver.
- **Synchronization:** determine whether client-side barriers are sufficient
  or independently controlled roles need a controller-mediated barrier.
- **Mobly endpoints:** decide whether exported ADB endpoints depend on a live
  shell session or on longer-lived client-managed forwards.

To resolve during implementation:

- The direct-dial timeout before router fallback.
- How deployments supply and validate `NetworkZone`; the proposed default
  is deployment configuration.
- Protocol-specific reconnect behavior, including projection-server restart
  and persistence of Bumble bond keys.
- How Phase 2 CI obtains vendor artifacts without redistributing them.
- Readable multi-member output in `jmp get leases`.

## Future Possibilities

These are outside the initial scope:

- **Mixed radio benches:** a gateway exporter owns a physical radio adapter.
  Decide whether it is a separate leased member or part of the physical
  device's exporter (DD-11).
- **Selection-time port validation:** expose reported ports through JEP-0017
  labels so an unsatisfiable request holds no exporters (DD-12).
- **Global scheduling:** resolve the existing cross-lease binding race.
- **Fan-out forwards:** connect one provided port to several required ports,
  including shared media such as Bumble relay rooms.
- **Ephemeral listeners:** allocate addresses dynamically if shared network
  namespaces are supported later.
- **Bench policy and quota:** add limits across members and support individual
  member release if needed.
- **Kubernetes-native mTLS identity:** Kubernetes 1.37 graduates Pod
  Certificates and ClusterTrustBundles to Stable. Exporter Pods could mount a
  signer-issued, automatically rotated X.509 identity and trust bundle for
  direct peer, controller, and router mTLS. This would keep the workload
  private key generated and managed by the kubelet instead of returning a
  private key from `DialPeer`; the per-forward token would remain as
  lease-scoped authorization after workload authentication. Adoption requires
  a configured signer (Kubernetes 1.37 does not ship a production signer in
  core), live certificate reload, an identity-to-`Exporter` binding, and a
  platform-neutral fallback for exporters outside Kubernetes.
- **Datagram transport:** define a separate protocol extension for framed
  traffic, avoiding TCP head-of-line blocking in the Phase 4 bridge.
- **Vehicle-bus simulation:** integrate a restbus simulator as a provided
  socket alongside existing CAN, DoIP, SOME/IP, UDS, XCP, and OBD drivers.
- **Test-framework integration:** supply a device or Mobly-controller shim
  backed by a lease.
- **Bench templates:** define reusable named topologies that instantiate
  ordinary leases.
- **On-demand members:** provision JEP-0014 pool instances to satisfy a lease.

## Implementation History

- 2026-09-01: Drafted the JEP. Manually verified shared and federated
  rootcanal configurations and L4 projection with stand-in relays (DD-9,
  DD-10).
- 2026-09-02: Verified a CVD-to-Bumble bench over Jumpstarter's router,
  measured single-node latency, and identified stream-recovery requirements.
  Updated `Auto` to prefer direct connections for same-zone peers.
- 2026-09-04: Consolidated rationale and prototype notes; clarified scope,
  implementation requirements, and remaining verification.
- 2026-09-04: Submitted for discussion in
  [PR #1069](https://github.com/jumpstarter-dev/jumpstarter/pull/1069).
- 2026-09-05: Resolved review questions around optional endpoints, explicit
  one-member response shape, port and name uniqueness, protobuf setup
  messages, router claim binding, direct-path encryption, and reconnect
  semantics. Recorded Kubernetes Pod Certificates and broader exporter mTLS
  authentication as future paths.

## References

- [JEP-0014: Virtual Scalable Exporters](JEP-0014-virtual-scalable-exporters.md)
  — "Composite leases — multiple exporters linked into one logical lease"
  (Future Possibilities)
- JEP-0016: Cuttlefish Kubernetes-Native Orchestration (draft, not yet
  submitted) — DD-8, whose option 1 this JEP implements
- JEP-0017: Dynamic Exporter Labels (draft, not yet submitted) — the
  mechanism selection-time port validation depends on (DD-12)
- [JEP-0013: Metrics, Tracing, and Log Observability](JEP-0013-observability-telemetry-logs.md)
- [JEP-0011: Protobuf Introspection and Interface Generation](JEP-0011-protobuf-introspection-interface-generation.md)
  — the introspection direction port reporting extends
- [Cuttlefish: test connectivity of multiple devices](https://source.android.com/docs/devices/cuttlefish/connectivity)
- [Mobly](https://github.com/google/mobly) — [testbed tutorial](https://github.com/google/mobly/blob/master/docs/tutorial.md)
- [Bumble, a Python Bluetooth stack](https://google.github.io/bumble/) —
  [transports](https://google.github.io/bumble/transports/index.html),
  [Android / `android-netsim` `mode=controller`](https://google.github.io/bumble/platforms/android.html),
  [apps and tools](https://google.github.io/bumble/apps_and_tools/index.html)
- [jumpstarter-dev/jumpstarter#986](https://github.com/jumpstarter-dev/jumpstarter/pull/986)
  — `jumpstarter-driver-bt-peer` (merged); the reference `requires`-side
  endpoint
- [jumpstarter-dev/jumpstarter#980](https://github.com/jumpstarter-dev/jumpstarter/pull/980)
  — `jumpstarter-driver-netsim` (merged); the control-plane companion,
  incl. pcap capture
- [netsim (`platform/tools/netsim`)](https://android.googlesource.com/platform/tools/netsim/) —
  `proto/netsim/packet_streamer.proto`
- [google/android-cuttlefish](https://github.com/google/android-cuttlefish)
- [Kubernetes 1.37: Pod Certificates and Cluster Trust Bundles](https://kubernetes.io/blog/2026/08/28/kubernetes-v1-37-pod-certificates-and-cluster-trust-bundles/)
  — Stable projected workload certificates and trust anchors (KEP-4317 and
  KEP-3257)
- [Test Multi-Device Interactions with the Android Emulator](https://android-developers.googleblog.com/2026/04/Test-Multi-Device-Interactions-with-the-Android-Emulator.html)
- [LAVA MultiNode](https://docs.lavasoftware.org/lava/multinode.html)

---

*This JEP is licensed under the
[Apache License, Version 2.0](https://www.apache.org/licenses/LICENSE-2.0),
consistent with the Jumpstarter project.*
