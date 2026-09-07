# SOME/IP Driver

`jumpstarter-driver-someip` provides SOME/IP (Scalable service-Oriented Middleware over IP)
protocol operations for Jumpstarter. This driver wraps the
[opensomeip](https://github.com/vtz/opensomeip-python) Python binding to enable remote
RPC calls, service discovery, raw messaging, and event subscriptions with automotive
ECUs over Ethernet.

## Installation

```shell
pip3 install --extra-index-url https://pkg.jumpstarter.dev/simple/ jumpstarter-driver-someip
```

## Configuration

| Parameter         | Type        | Default       | Description                                           |
|-------------------|-------------|---------------|-------------------------------------------------------|
| `host`            | str         | required      | Local IP address to bind                              |
| `port`            | int         | 30490         | Local SOME/IP port                                    |
| `transport_mode`  | str         | `UDP`         | Transport protocol: `UDP` or `TCP`                    |
| `multicast_group` | str         | `239.127.0.1` | SD multicast group address                            |
| `multicast_port`  | int         | 30490         | SD multicast port                                     |
| `remote_host`     | str \| None | `None`        | Remote ECU IP for static routing (bypasses SD)        |
| `remote_port`     | int \| None | `None`        | Remote ECU port (defaults to `port` when `remote_host` is set) |

### UDP (default)

```yaml
export:
  someip:
    type: jumpstarter_driver_someip.driver.SomeIp
    config:
      host: "192.168.1.100"
      port: 30490
      transport_mode: UDP
      multicast_group: "239.127.0.1"
      multicast_port: 30490
```

### TCP

```yaml
export:
  someip:
    type: jumpstarter_driver_someip.driver.SomeIp
    config:
      host: "192.168.1.100"
      port: 30490
      transport_mode: TCP
```

### Static remote endpoint (no Service Discovery)

When the target ECU does not run SOME/IP-SD (e.g. Zephyr firmware with
multicast TX disabled), set `remote_host` and optionally `remote_port`
to send messages directly without Service Discovery:

```yaml
export:
  someip:
    type: jumpstarter_driver_someip.driver.SomeIp
    config:
      host: "192.168.100.1"
      port: 30490
      transport_mode: UDP
      remote_host: "192.168.100.10"
      remote_port: 30490
```

## Usage

### RPC Call

```python
from jumpstarter.common.utils import env

with env() as client:
    someip = client.someip

    response = someip.rpc_call(0x1234, 0x0001, b"\x01\x02\x03")
    print(f"Response: {bytes.fromhex(response.payload)}")
    print(f"Return code: {response.return_code}")
```

### Service Discovery + RPC

```python
from jumpstarter.common.utils import env

with env() as client:
    someip = client.someip

    # Discover available services
    services = someip.find_service(0x1234, timeout=3.0)
    for svc in services:
        print(f"Found: service={svc.service_id:#06x} instance={svc.instance_id:#06x}")

    # Call the first discovered service
    if services:
        resp = someip.rpc_call(0x1234, 0x0001, b"\x10\x20")
        print(f"RPC result: {resp.payload}")
```

### Event Subscription

```python
from jumpstarter.common.utils import env

with env() as client:
    someip = client.someip

    # Subscribe to event group 1
    someip.subscribe_eventgroup(1)

    # Wait for event notifications
    try:
        event = someip.receive_event(timeout=10.0)
        print(f"Event service={event.service_id:#06x} id={event.event_id:#06x}")
        print(f"Payload: {bytes.fromhex(event.payload)}")
    finally:
        someip.unsubscribe_eventgroup(1)
```

### Raw Messaging

```python
from jumpstarter.common.utils import env

with env() as client:
    someip = client.someip

    someip.send_message(0x1234, 0x0001, b"\xAA\xBB")
    msg = someip.receive_message(timeout=2.0)
    print(f"Received from service={msg.service_id:#06x}: {msg.payload}")
```

### Connection Management

```python
from jumpstarter.common.utils import env

with env() as client:
    someip = client.someip

    # Perform operations...
    someip.rpc_call(0x1234, 0x0001, b"\x01")

    # Reconnect after network disruption
    someip.reconnect()

    # Continue operations
    someip.rpc_call(0x1234, 0x0001, b"\x02")

    # Clean up
    someip.close_connection()
```

### Server / Provider (act as an ECU)

The same driver can also *provide* SOME/IP services — offer them via Service
Discovery, answer RPC requests with canned responses, and publish events. This
turns a Jumpstarter exporter into a simulated ECU, useful for exercising a
device-under-test that is a SOME/IP *client* of another ECU.

RPC handlers run inside the exporter process, so responses are configured
declaratively rather than via a per-request callback: set a canned response for
a `(service_id, method_id)` and the server serves it. This maps naturally onto
getter-style SOME/IP methods; update the response to change what a client reads.

```python
from jumpstarter.common.utils import env

with env() as client:
    someip = client.someip

    # Offer a service instance (starts the server on first use)
    someip.offer_service(0x1801, instance_id=0x0001, major_version=1)

    # Answer an RPC method with a fixed payload (E_OK by default)
    someip.set_method_response(0x1801, 0x0005, b"\x01\x02\x03\x04")
    # ...or return an error return code
    someip.set_method_response(0x1801, 0x0006, b"", return_code=0x01)

    # Publish events to subscribers of an event group
    someip.register_event(0x1801, 0x8001, eventgroup_id=1)
    someip.publish_event(0x1801, 0x8001, b"\x2d\x00")
    # Field events are cached and served to new subscribers
    someip.set_field(0x1801, 0x8002, b"\x01")

    # Introspect / tear down
    print(someip.list_offered_services())
    someip.stop_offer_service(0x1801, 0x0001)
    someip.stop_server()
```

## API Reference

```{eval-rst}
.. autoclass:: jumpstarter_driver_someip.driver.SomeIp()
```
