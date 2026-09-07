"""Test fixtures for the SOME/IP driver.

Provides:
- ``MockSomeIpServer``: a minimal TCP server that speaks the SOME/IP wire
  protocol (header + payload) for integration testing.
- ``StatefulOsipClient``: a drop-in replacement for ``opensomeip.SomeIpClient``
  that enforces SOME/IP state rules (connection lifecycle, service registry,
  event subscriptions, message ordering).  Used by the stateful scenario tests
  to exercise realistic multi-step workflows through the full gRPC boundary.
"""

from __future__ import annotations

import queue
import socket
import struct
import threading
from unittest.mock import MagicMock, patch

import pytest
from opensomeip.message import Message as OsipMessage
from opensomeip.types import MessageId as OsipMessageId
from opensomeip.types import RequestId as OsipRequestId

# =========================================================================
# Wire-protocol constants
# =========================================================================

PROTOCOL_VERSION = 0x01
INTERFACE_VERSION = 0x01

MSG_TYPE_REQUEST = 0x00
MSG_TYPE_RESPONSE = 0x80
MSG_TYPE_NOTIFICATION = 0x02
MSG_TYPE_ERROR = 0x81

RC_OK = 0x00
RC_NOT_OK = 0x01
RC_UNKNOWN_SERVICE = 0x02
RC_UNKNOWN_METHOD = 0x03

HEADER_SIZE = 16


# =========================================================================
# Wire-protocol helpers
# =========================================================================


def _pack_someip(
    service_id: int,
    method_id: int,
    client_id: int,
    session_id: int,
    message_type: int,
    return_code: int,
    payload: bytes,
) -> bytes:
    """Pack a SOME/IP message into wire format (big-endian)."""
    length = 8 + len(payload)
    header = struct.pack(
        "!HHIHHBBBB",
        service_id,
        method_id,
        length,
        client_id,
        session_id,
        PROTOCOL_VERSION,
        INTERFACE_VERSION,
        message_type,
        return_code,
    )
    return header + payload


def _read_someip_message(conn: socket.socket) -> tuple[int, int, int, int, int, int, bytes] | None:
    """Read a single SOME/IP message from a TCP socket.

    Returns (service_id, method_id, client_id, session_id,
             message_type, return_code, payload) or None on disconnect.
    """
    header = b""
    while len(header) < HEADER_SIZE:
        chunk = conn.recv(HEADER_SIZE - len(header))
        if not chunk:
            return None
        header += chunk

    service_id, method_id, length, client_id, session_id, proto_ver, iface_ver, msg_type, ret_code = struct.unpack(
        "!HHIHHBBBB", header
    )

    payload_len = length - 8
    payload = b""
    while len(payload) < payload_len:
        chunk = conn.recv(payload_len - len(payload))
        if not chunk:
            return None
        payload += chunk

    return service_id, method_id, client_id, session_id, msg_type, ret_code, payload


# =========================================================================
# MockSomeIpServer - minimal TCP server for wire-level integration tests
# =========================================================================


class MockSomeIpServer:
    """Minimal SOME/IP TCP server for integration testing.

    Handles RPC requests by echoing the payload back in a response.
    """

    def __init__(self):
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(2)
        self._server.settimeout(1.0)
        self.port = self._server.getsockname()[1]
        self._running = True
        self._clients: list[socket.socket] = []
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self):
        while self._running:
            try:
                conn, _ = self._server.accept()
                conn.settimeout(1.0)
                self._clients.append(conn)
                handler = threading.Thread(target=self._handle_client, args=(conn,), daemon=True)
                handler.start()
            except OSError:
                pass

    def _handle_client(self, conn: socket.socket):
        try:
            while self._running:
                try:
                    result = _read_someip_message(conn)
                    if result is None:
                        break
                    service_id, method_id, client_id, session_id, msg_type, ret_code, payload = result
                    responses = self._dispatch(
                        service_id, method_id, client_id, session_id, msg_type, payload
                    )
                    for resp in responses:
                        conn.sendall(resp)
                except OSError:
                    break
        finally:
            conn.close()

    def _dispatch(
        self,
        service_id: int,
        method_id: int,
        client_id: int,
        session_id: int,
        msg_type: int,
        payload: bytes,
    ) -> list[bytes]:
        if msg_type == MSG_TYPE_REQUEST:
            return [
                _pack_someip(
                    service_id,
                    method_id,
                    client_id,
                    session_id,
                    MSG_TYPE_RESPONSE,
                    RC_OK,
                    payload,
                )
            ]
        return []

    def stop(self):
        self._running = False
        self._server.close()
        for c in self._clients:
            try:
                c.close()
            except OSError:
                pass
        self._thread.join(timeout=3)


@pytest.fixture
def mock_someip_server():
    """Start a MockSomeIpServer on a dynamic port and yield the port number."""
    server = MockSomeIpServer()
    try:
        yield server.port
    finally:
        server.stop()


# =========================================================================
# StatefulOsipClient - drop-in for opensomeip.SomeIpClient
#
# Tracks connection state, service registry, event subscriptions,
# message history, and enforces ordering rules.  Designed to be
# injected via ``@patch("jumpstarter_driver_someip.driver.OsipClient")``.
# =========================================================================


class _FakeMessageId:
    def __init__(self, service_id: int, method_id: int):
        self.service_id = service_id
        self.method_id = method_id


class _FakeRequestId:
    def __init__(self, client_id: int = 0x0001, session_id: int = 0x0001):
        self.client_id = client_id
        self.session_id = session_id


class _FakeMessage:
    """Mimics ``opensomeip.message.Message`` with the attributes the driver reads."""

    def __init__(
        self,
        service_id: int,
        method_id: int,
        payload: bytes,
        *,
        message_type: int = MSG_TYPE_RESPONSE,
        return_code: int = RC_OK,
        client_id: int = 0x0001,
        session_id: int = 0x0001,
    ):
        self.message_id = _FakeMessageId(service_id, method_id)
        self.request_id = _FakeRequestId(client_id, session_id)
        self.protocol_version = PROTOCOL_VERSION
        self.interface_version = INTERFACE_VERSION
        self.message_type = message_type
        self.return_code = return_code
        self.payload = payload


class _FakeReceiver:
    """Mimics the opensomeip MessageReceiver with ``_sync_queue``."""

    def __init__(self):
        self._sync_queue: queue.Queue = queue.Queue()


class _FakeTransport:
    def __init__(self):
        self.receiver = _FakeReceiver()


class _FakeServiceInstance:
    """Mimics ``opensomeip.sd.ServiceInstance``."""

    def __init__(self, service_id: int, instance_id: int, major_version: int = 1, minor_version: int = 0):
        self.service_id = service_id
        self.instance_id = instance_id
        self.major_version = major_version
        self.minor_version = minor_version


class SomeIpNotStarted(RuntimeError):
    pass


class StatefulOsipClient:
    """A drop-in replacement for ``opensomeip.SomeIpClient`` that enforces
    SOME/IP state rules.

    Tracks:
    - Connection lifecycle (start/stop)
    - Registered services (for ``find`` / SD)
    - Event subscriptions
    - RPC call history and configurable responses
    - Sent messages (for verification)
    - Inbound message queue (for ``receive_message``)
    """

    def __init__(self, config=None) -> None:
        self._started = False
        self._config = config

        self._registered_services: list[_FakeServiceInstance] = [
            _FakeServiceInstance(0x1234, 0x0001),
            _FakeServiceInstance(0x1234, 0x0002, major_version=2),
            _FakeServiceInstance(0x5678, 0x0001),
        ]

        self._subscribed_eventgroups: set[int] = set()

        self._rpc_responses: dict[tuple[int, int], bytes] = {
            (0x1234, 0x0001): b"\x0A\x0B\x0C",
            (0x1234, 0x0002): b"\x01\x02\x03\x04",
        }
        self._rpc_history: list[tuple[int, int, bytes]] = []

        self._sent_messages: list[_FakeMessage] = []

        self.transport = _FakeTransport()

        self._event_notifications: list[_FakeMessage] = []
        self._event_receiver = _FakeReceiver()

        self.event_subscriber = MagicMock()
        self.event_subscriber.notifications.return_value = self._event_receiver

    def _require_started(self):
        if not self._started:
            raise SomeIpNotStarted("Client not started - call start() first")

    def start(self):
        self._started = True

    def stop(self):
        self._started = False
        self._subscribed_eventgroups.clear()

    def call(self, message_id, *, payload: bytes = b"", timeout: float = 5.0):
        """Simulate an RPC call. Returns a canned response or echoes the payload."""
        self._require_started()
        sid = message_id.service_id
        mid = message_id.method_id
        self._rpc_history.append((sid, mid, payload))

        resp_payload = self._rpc_responses.get((sid, mid), payload)
        return _FakeMessage(sid, mid, resp_payload)

    def send(self, msg):
        """Record a sent message and optionally echo it back into the receive queue."""
        self._require_started()
        self._sent_messages.append(msg)
        echo = _FakeMessage(
            msg.message_id.service_id,
            msg.message_id.method_id,
            msg.payload,
            message_type=MSG_TYPE_RESPONSE,
        )
        self.transport.receiver._sync_queue.put(echo)

    def find(self, service, *, callback=None):
        """Simulate service discovery by calling back with matching registered services."""
        self._require_started()
        for svc in self._registered_services:
            if svc.service_id == service.service_id:
                if service.instance_id == 0xFFFF or svc.instance_id == service.instance_id:
                    if callback:
                        callback(svc)

    def subscribe_events(self, eventgroup_id: int):
        self._require_started()
        self._subscribed_eventgroups.add(eventgroup_id)

    def unsubscribe_events(self, eventgroup_id: int):
        self._require_started()
        self._subscribed_eventgroups.discard(eventgroup_id)

    # - test helpers --

    def inject_event(self, service_id: int, event_id: int, payload: bytes):
        """Push a fake event notification into the event receiver queue."""
        msg = _FakeMessage(service_id, event_id, payload, message_type=MSG_TYPE_NOTIFICATION)
        self._event_receiver._sync_queue.put(msg)

    def inject_message(self, service_id: int, method_id: int, payload: bytes):
        """Push a fake inbound message into the transport receiver queue."""
        msg = _FakeMessage(service_id, method_id, payload)
        self.transport.receiver._sync_queue.put(msg)

    def register_rpc_response(self, service_id: int, method_id: int, payload: bytes):
        """Configure a canned RPC response for a specific service/method pair."""
        self._rpc_responses[(service_id, method_id)] = payload

    def register_service(
        self, service_id: int, instance_id: int, major_version: int = 1, minor_version: int = 0
    ):
        """Add a service to the SD registry."""
        self._registered_services.append(
            _FakeServiceInstance(service_id, instance_id, major_version, minor_version)
        )

    def unregister_service(self, service_id: int, instance_id: int):
        """Remove a service from the SD registry."""
        self._registered_services = [
            s
            for s in self._registered_services
            if not (s.service_id == service_id and s.instance_id == instance_id)
        ]


# =========================================================================
# StatefulOsipServer / LoopbackOsipClient - simulated-ECU loopback pair
#
# The server fake tracks offers, RPC handlers, and events like a real
# opensomeip.SomeIpServer.  The loopback client dispatches RPC calls to the
# server's registered handlers and discovers the server's offers, so tests
# can exercise the full simulated-ECU workflow (offer + canned response +
# client RPC) through the gRPC boundary with a single driver.
# =========================================================================


class StatefulOsipServer:
    """A drop-in replacement for ``opensomeip.SomeIpServer`` that tracks
    offers, RPC handlers, and events, and loops published events back to an
    attached ``LoopbackOsipClient``.
    """

    def __init__(self, config=None) -> None:
        self._started = False
        self._config = config
        self._offered: list = []
        self.handlers: dict = {}
        self.registered_events: dict = {}
        self.fields: dict = {}
        self._client: LoopbackOsipClient | None = None

    def attach_client(self, client: LoopbackOsipClient) -> None:
        self._client = client

    def start(self):
        self._started = True

    def stop(self):
        self._started = False
        self._offered.clear()

    def offer(self, service):
        self._offered.append(service)

    def stop_offer(self, service):
        self._offered = [
            s
            for s in self._offered
            if not (s.service_id == service.service_id and s.instance_id == service.instance_id)
        ]

    @property
    def offered_services(self):
        return list(self._offered)

    def register_method(self, message_id, handler):
        self.handlers[(message_id.service_id, message_id.method_id)] = handler

    def register_event(self, event_id, eventgroup_id):
        self.registered_events[event_id] = eventgroup_id

    def publish_event(self, event_id, payload):
        """Deliver the event to the attached client if it is subscribed."""
        eventgroup_id = self.registered_events.get(event_id)
        client = self._client
        if client is not None and eventgroup_id in client._subscribed_eventgroups:
            client.inject_event(0x0000, event_id, payload)

    def set_field(self, event_id, payload):
        self.fields[event_id] = payload
        self.publish_event(event_id, payload)


class LoopbackOsipClient(StatefulOsipClient):
    """A ``StatefulOsipClient`` wired to a ``StatefulOsipServer``.

    RPC calls dispatch to the server's registered method handlers (built as
    real ``opensomeip`` request messages) and service discovery reflects the
    server's current offers.
    """

    def __init__(self, server: StatefulOsipServer, config=None) -> None:
        super().__init__(config)
        self._server = server
        # Discovery and RPC reflect the server side, not a static registry.
        self._registered_services = []
        self._rpc_responses = {}

    def call(self, message_id, *, payload: bytes = b"", timeout: float = 5.0):
        self._require_started()
        sid = message_id.service_id
        mid = message_id.method_id
        self._rpc_history.append((sid, mid, payload))
        handler = self._server.handlers.get((sid, mid))
        if handler is None:
            raise TimeoutError(f"no handler registered for service 0x{sid:04X} method 0x{mid:04X}")
        request = OsipMessage(
            message_id=OsipMessageId(sid, mid),
            request_id=OsipRequestId(0x0001, len(self._rpc_history)),
            payload=payload,
        )
        return handler(request)

    def find(self, service, *, callback=None):
        self._require_started()
        for svc in self._server.offered_services:
            if svc.service_id == service.service_id:
                if service.instance_id == 0xFFFF or svc.instance_id == service.instance_id:
                    if callback:
                        callback(svc)


@pytest.fixture
def loopback_pair():
    """Provide a wired (StatefulOsipServer, LoopbackOsipClient) pair."""
    server = StatefulOsipServer()
    client = LoopbackOsipClient(server)
    server.attach_client(client)
    return server, client


@pytest.fixture(autouse=True)
def _mock_get_ext():
    """Ensure the native-extension guard passes in all tests by default."""
    with patch("jumpstarter_driver_someip.driver.get_ext", return_value=object()):
        yield


@pytest.fixture
def stateful_osip():
    """Provide a fresh StatefulOsipClient instance."""
    return StatefulOsipClient()
