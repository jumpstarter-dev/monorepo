import os
import queue as _queue
from unittest.mock import MagicMock, patch

import pytest
from opensomeip.message import Message
from opensomeip.types import MessageId, MessageType, RequestId, ReturnCode
from pydantic import ValidationError

from .common import (
    SomeIpEventNotification,
    SomeIpMessageResponse,
    SomeIpOfferedService,
    SomeIpPayload,
    SomeIpServiceEntry,
)
from .driver import SomeIp
from jumpstarter.client.core import DriverError
from jumpstarter.common.utils import serve

# =========================================================================
# Mock helpers (for isolated unit tests)
# =========================================================================


def _make_mock_message():
    mock_response = MagicMock()
    mock_response.message_id.service_id = 0x1234
    mock_response.message_id.method_id = 0x0001
    mock_response.request_id.client_id = 0x0001
    mock_response.request_id.session_id = 0x0001
    mock_response.protocol_version = 1
    mock_response.interface_version = 1
    mock_response.message_type = 0x80
    mock_response.return_code = 0x00
    mock_response.payload = b"\x01\x02\x03"
    return mock_response


def _make_mock_osip_client():
    """Build a mock OsipClient wired to return canned messages.

    The driver reads from opensomeip's internal ``_sync_queue`` on the
    ``MessageReceiver`` (no public blocking-with-timeout API exists yet).
    We replicate that structure here so the driver's ``_receive_from_queue``
    helper works as expected.
    """
    mock = MagicMock()

    mock_response = _make_mock_message()
    mock.call.return_value = mock_response

    sync_queue = _queue.Queue()
    sync_queue.put(mock_response)
    mock.transport.receiver._sync_queue = sync_queue

    event_queue = _queue.Queue()
    event_queue.put(mock_response)
    mock_event_receiver = MagicMock()
    mock_event_receiver._sync_queue = event_queue
    mock.event_subscriber.notifications.return_value = mock_event_receiver

    return mock


# =========================================================================
# Unit tests - happy paths
# =========================================================================


@patch("jumpstarter_driver_someip.driver.OsipClient")
def test_someip_rpc_call(mock_osip_cls):
    mock_client = _make_mock_osip_client()
    mock_osip_cls.return_value = mock_client

    driver = SomeIp(host="127.0.0.1", port=30490)
    with serve(driver) as client:
        resp = client.rpc_call(0x1234, 0x0001, b"\x01\x02\x03")
        assert resp.service_id == 0x1234
        assert resp.method_id == 0x0001
        assert resp.payload == "010203"
        assert resp.return_code == 0x00


@patch("jumpstarter_driver_someip.driver.OsipClient")
def test_someip_send_message(mock_osip_cls):
    mock_client = _make_mock_osip_client()
    mock_osip_cls.return_value = mock_client

    driver = SomeIp(host="127.0.0.1", port=30490)
    with serve(driver) as client:
        client.send_message(0x1234, 0x0001, b"\xAA\xBB")
        mock_client.send.assert_called_once()
        sent_msg = mock_client.send.call_args[0][0]
        assert sent_msg.message_id.service_id == 0x1234
        assert sent_msg.message_id.method_id == 0x0001
        assert sent_msg.payload == b"\xAA\xBB"


@patch("jumpstarter_driver_someip.driver.OsipClient")
def test_someip_receive_message(mock_osip_cls):
    mock_client = _make_mock_osip_client()
    mock_osip_cls.return_value = mock_client

    driver = SomeIp(host="127.0.0.1", port=30490)
    with serve(driver) as client:
        resp = client.receive_message(timeout=1.0)
        assert resp.service_id == 0x1234
        assert resp.payload == "010203"


@patch("jumpstarter_driver_someip.driver.OsipClient")
def test_someip_subscribe_eventgroup(mock_osip_cls):
    mock_client = _make_mock_osip_client()
    mock_osip_cls.return_value = mock_client

    driver = SomeIp(host="127.0.0.1", port=30490)
    with serve(driver) as client:
        client.subscribe_eventgroup(1)
        mock_client.subscribe_events.assert_called_once_with(1)


@patch("jumpstarter_driver_someip.driver.OsipClient")
def test_someip_unsubscribe_eventgroup(mock_osip_cls):
    mock_client = _make_mock_osip_client()
    mock_osip_cls.return_value = mock_client

    driver = SomeIp(host="127.0.0.1", port=30490)
    with serve(driver) as client:
        client.unsubscribe_eventgroup(1)
        mock_client.unsubscribe_events.assert_called_once_with(1)


@patch("jumpstarter_driver_someip.driver.OsipClient")
def test_someip_receive_event(mock_osip_cls):
    mock_client = _make_mock_osip_client()
    mock_osip_cls.return_value = mock_client

    driver = SomeIp(host="127.0.0.1", port=30490)
    with serve(driver) as client:
        resp = client.receive_event(timeout=1.0)
        assert resp.service_id == 0x1234
        assert resp.event_id == 0x0001
        assert resp.payload == "010203"


@patch("jumpstarter_driver_someip.driver.OsipClient")
def test_someip_find_service(mock_osip_cls):
    mock_client = _make_mock_osip_client()

    def fake_find(service, *, callback=None):
        svc = MagicMock()
        svc.service_id = service.service_id
        svc.instance_id = 0x0001
        svc.major_version = 1
        svc.minor_version = 0
        if callback:
            callback(svc)

    mock_client.find.side_effect = fake_find
    mock_osip_cls.return_value = mock_client

    driver = SomeIp(host="127.0.0.1", port=30490)
    with serve(driver) as client:
        results = client.find_service(0x1234, timeout=0.1)
        assert len(results) == 1
        assert results[0].service_id == 0x1234
        assert results[0].instance_id == 0x0001
        assert results[0].major_version == 1
        mock_client.find.assert_called_once()


@patch("jumpstarter_driver_someip.driver.OsipClient")
def test_someip_find_service_no_results(mock_osip_cls):
    mock_client = _make_mock_osip_client()
    mock_client.find.side_effect = lambda service, *, callback=None: None
    mock_osip_cls.return_value = mock_client

    driver = SomeIp(host="127.0.0.1", port=30490)
    with serve(driver) as client:
        results = client.find_service(0x9999, timeout=0.1)
        assert results == []
        mock_client.find.assert_called_once()


@patch("jumpstarter_driver_someip.driver.OsipClient")
def test_someip_find_service_forwards_instance_id(mock_osip_cls):
    mock_client = _make_mock_osip_client()
    mock_client.find.side_effect = lambda service, *, callback=None: None
    mock_osip_cls.return_value = mock_client

    driver = SomeIp(host="127.0.0.1", port=30490)
    with serve(driver) as client:
        client.find_service(0x1234, instance_id=0x0042, timeout=0.1)
        call_args = mock_client.find.call_args
        service_arg = call_args[0][0]
        assert service_arg.service_id == 0x1234
        assert service_arg.instance_id == 0x0042


@patch("jumpstarter_driver_someip.driver.OsipClient")
def test_someip_close_connection(mock_osip_cls):
    mock_client = _make_mock_osip_client()
    mock_osip_cls.return_value = mock_client

    driver = SomeIp(host="127.0.0.1", port=30490)
    with serve(driver) as client:
        # Start the client first so close_connection has something to stop
        client.start()
        client.close_connection()
        mock_client.stop.assert_called()


@patch("jumpstarter_driver_someip.driver.OsipClient")
def test_someip_reconnect(mock_osip_cls):
    mock_client = _make_mock_osip_client()
    mock_osip_cls.return_value = mock_client

    driver = SomeIp(host="127.0.0.1", port=30490)
    with serve(driver) as client:
        # Start the client first so reconnect has something to stop/restart
        client.start()
        client.reconnect()
        assert mock_client.stop.call_count >= 1
        assert mock_client.start.call_count >= 2


@patch("jumpstarter_driver_someip.driver.OsipClient")
def test_someip_reconnect_survives_stop_failure(mock_osip_cls):
    mock_client = _make_mock_osip_client()
    mock_osip_cls.return_value = mock_client
    mock_client.stop.side_effect = [RuntimeError("stop failed"), None]

    driver = SomeIp(host="127.0.0.1", port=30490)
    with serve(driver) as client:
        # Start the client first so reconnect has something to stop
        client.start()
        client.reconnect()
        assert mock_client.start.call_count >= 2


# =========================================================================
# Error-path tests
# =========================================================================


@patch("jumpstarter_driver_someip.driver.OsipClient")
def test_someip_rpc_call_timeout(mock_osip_cls):
    mock_client = _make_mock_osip_client()
    mock_client.call.side_effect = TimeoutError("No response from service")
    mock_osip_cls.return_value = mock_client

    driver = SomeIp(host="127.0.0.1", port=30490)
    with serve(driver) as client:
        with pytest.raises(DriverError, match="No response from service"):
            client.rpc_call(0x1234, 0x0001, b"\x01")


@patch("jumpstarter_driver_someip.driver.OsipClient")
def test_someip_receive_message_timeout(mock_osip_cls):
    mock_client = _make_mock_osip_client()
    mock_client.transport.receiver._sync_queue = _queue.Queue()
    mock_osip_cls.return_value = mock_client

    driver = SomeIp(host="127.0.0.1", port=30490)
    with serve(driver) as client:
        with pytest.raises(DriverError, match="No message received"):
            client.receive_message(timeout=0.1)


@patch("jumpstarter_driver_someip.driver.OsipClient")
def test_someip_receive_event_timeout(mock_osip_cls):
    mock_client = _make_mock_osip_client()
    empty_receiver = MagicMock()
    empty_receiver._sync_queue = _queue.Queue()
    mock_client.event_subscriber.notifications.return_value = empty_receiver
    mock_osip_cls.return_value = mock_client

    driver = SomeIp(host="127.0.0.1", port=30490)
    with serve(driver) as client:
        with pytest.raises(DriverError, match="No event received"):
            client.receive_event(timeout=0.1)


@patch("jumpstarter_driver_someip.driver.OsipClient")
def test_someip_connection_error(mock_osip_cls):
    mock_osip_cls.return_value.start.side_effect = ConnectionRefusedError("Connection refused")

    driver = SomeIp(host="192.168.1.100", port=30490)
    with serve(driver) as client:
        with pytest.raises(DriverError, match="Connection refused"):
            client.start()


# =========================================================================
# Lazy init tests
# =========================================================================


@patch("jumpstarter_driver_someip.driver.OsipClient")
def test_someip_lazy_start(mock_osip_cls):
    """Verify the OsipClient is NOT created during construction but IS created on first use."""
    mock_client = _make_mock_osip_client()
    mock_osip_cls.return_value = mock_client

    driver = SomeIp(host="127.0.0.1", port=30490)

    # OsipClient constructor should not have been called yet
    mock_osip_cls.assert_not_called()

    with serve(driver) as client:
        # Still not called until we actually use it
        mock_osip_cls.assert_not_called()

        # Trigger first use
        client.start()

        # Now it should have been created and started
        mock_osip_cls.assert_called_once()
        mock_client.start.assert_called_once()


@patch("jumpstarter_driver_someip.driver.OsipClient")
def test_someip_start_method(mock_osip_cls):
    """Verify the exported start() method creates and starts the client."""
    mock_client = _make_mock_osip_client()
    mock_osip_cls.return_value = mock_client

    driver = SomeIp(host="127.0.0.1", port=30490)
    with serve(driver) as client:
        client.start()
        mock_osip_cls.assert_called_once()
        mock_client.start.assert_called_once()

        # Calling start() again should not create a second client
        client.start()
        mock_osip_cls.assert_called_once()
        mock_client.start.assert_called_once()


# =========================================================================
# Config validation tests
# =========================================================================


def test_someip_missing_required_host():
    with pytest.raises(ValidationError, match="host"):
        SomeIp(port=30490)  # ty: ignore[missing-argument]


def test_someip_invalid_port_type():
    with pytest.raises(ValidationError):
        SomeIp(host="127.0.0.1", port="not_a_port")  # ty: ignore[invalid-argument-type]


@patch("jumpstarter_driver_someip.driver.get_ext", return_value=None)
def test_someip_raises_when_native_extension_unavailable(_mock_get_ext):
    with pytest.raises(RuntimeError, match=r"_opensomeip.*not available"):
        SomeIp(host="127.0.0.1", port=30490)


@patch("jumpstarter_driver_someip.driver.OsipClient")
def test_someip_invalid_transport_mode(mock_osip_cls):
    mock_osip_cls.return_value = _make_mock_osip_client()
    with pytest.raises(ValueError, match="Invalid transport_mode"):
        SomeIp(host="127.0.0.1", transport_mode="INVALID")


@patch("jumpstarter_driver_someip.driver.OsipClient")
def test_someip_custom_config_forwarded(mock_osip_cls):
    """Verify non-default config values are passed to opensomeip."""
    mock_osip_cls.return_value = _make_mock_osip_client()

    driver = SomeIp(
        host="10.0.0.1",
        port=9999,
        transport_mode="TCP",
        multicast_group="239.1.1.1",
        multicast_port=31000,
    )

    # Client is created lazily; trigger first use to instantiate it
    with serve(driver) as client:
        client.start()

    mock_osip_cls.assert_called_once()
    config = mock_osip_cls.call_args[0][0]
    assert config.local_endpoint.ip == "10.0.0.1"
    assert config.local_endpoint.port == 9999
    assert config.sd_config.multicast_endpoint.ip == "239.1.1.1"
    assert config.sd_config.multicast_endpoint.port == 31000


@pytest.mark.parametrize("odd_hex", ["A", "ABC", "12345", "0"])
def test_someip_rejects_odd_length_hex_payload(odd_hex):
    """Odd-length hex strings are not valid byte sequences and must be rejected."""
    with pytest.raises(ValidationError, match="even length"):
        SomeIpPayload(data=odd_hex)


@pytest.mark.parametrize("even_hex", ["", "AA", "0102", "aabbccdd"])
def test_someip_accepts_valid_hex_payload(even_hex):
    """Even-length hex strings (including empty) are accepted."""
    p = SomeIpPayload(data=even_hex)
    assert p.data == even_hex


@pytest.mark.parametrize(
    "model_cls, field, value",
    [
        (SomeIpMessageResponse, "service_id", 0x1FFFF),
        (SomeIpMessageResponse, "method_id", -1),
        (SomeIpServiceEntry, "service_id", 0x10000),
        (SomeIpServiceEntry, "instance_id", 0x10000),
        (SomeIpEventNotification, "service_id", 0x10000),
        (SomeIpEventNotification, "event_id", 0x10000),
    ],
)
def test_someip_rejects_out_of_range_16bit_ids(model_cls, field, value):
    """16-bit SOME/IP ID fields must reject values outside 0..0xFFFF."""
    defaults = {
        SomeIpMessageResponse: {
            "service_id": 1, "method_id": 1, "client_id": 1, "session_id": 1,
            "message_type": 0, "return_code": 0, "payload": "AA",
        },
        SomeIpServiceEntry: {"service_id": 1, "instance_id": 1},
        SomeIpEventNotification: {"service_id": 1, "event_id": 1, "payload": "AA"},
    }
    kwargs = {**defaults[model_cls], field: value}
    with pytest.raises(ValidationError):
        model_cls(**kwargs)


@patch("jumpstarter_driver_someip.driver.OsipClient")
def test_someip_close_connection_survives_stop_failure(mock_osip_cls):
    """close_connection must not propagate exceptions from stop()."""
    mock_client = _make_mock_osip_client()
    mock_client.stop.side_effect = [RuntimeError("stop failed"), None]
    mock_osip_cls.return_value = mock_client

    driver = SomeIp(host="127.0.0.1", port=30490)
    with serve(driver) as client:
        # Start the client first so close_connection has something to stop
        client.start()
        client.close_connection()


@patch("jumpstarter_driver_someip.driver.OsipClient")
def test_someip_tcp_transport_mode(mock_osip_cls):
    """Verify TCP transport mode is forwarded correctly."""
    mock_osip_cls.return_value = _make_mock_osip_client()

    driver = SomeIp(host="127.0.0.1", transport_mode="TCP")

    # Client is created lazily; trigger first use to instantiate it
    with serve(driver) as client:
        client.start()

    config = mock_osip_cls.call_args[0][0]
    from opensomeip import TransportMode
    assert config.transport_mode == TransportMode.TCP


# =========================================================================
# Static remote endpoint tests
# =========================================================================


@patch("jumpstarter_driver_someip.driver.ClientConfig")
@patch("jumpstarter_driver_someip.driver.OsipClient")
def test_someip_remote_endpoint_forwarded(mock_osip_cls, mock_config_cls):
    """Verify remote_host/remote_port are forwarded to ClientConfig."""
    mock_osip_cls.return_value = _make_mock_osip_client()

    driver = SomeIp(
        host="192.168.100.1",
        port=30490,
        remote_host="192.168.100.10",
        remote_port=31000,
    )

    with serve(driver) as client:
        client.start()

    kwargs = mock_config_cls.call_args[1]
    assert kwargs["remote_endpoint"].ip == "192.168.100.10"
    assert kwargs["remote_endpoint"].port == 31000


@patch("jumpstarter_driver_someip.driver.ClientConfig")
@patch("jumpstarter_driver_someip.driver.OsipClient")
def test_someip_remote_endpoint_defaults_port(mock_osip_cls, mock_config_cls):
    """When remote_port is omitted, it defaults to the local port."""
    mock_osip_cls.return_value = _make_mock_osip_client()

    driver = SomeIp(
        host="192.168.100.1",
        port=30490,
        remote_host="192.168.100.10",
    )

    with serve(driver) as client:
        client.start()

    kwargs = mock_config_cls.call_args[1]
    assert kwargs["remote_endpoint"].ip == "192.168.100.10"
    assert kwargs["remote_endpoint"].port == 30490


@patch("jumpstarter_driver_someip.driver.ClientConfig")
@patch("jumpstarter_driver_someip.driver.OsipClient")
def test_someip_no_remote_endpoint_by_default(mock_osip_cls, mock_config_cls):
    """Without remote_host, remote_endpoint is not passed (SD-based discovery)."""
    mock_osip_cls.return_value = _make_mock_osip_client()

    driver = SomeIp(host="127.0.0.1", port=30490)

    with serve(driver) as client:
        client.start()

    kwargs = mock_config_cls.call_args[1]
    assert "remote_endpoint" not in kwargs


def test_someip_remote_port_without_remote_host_rejected():
    """remote_port without remote_host is rejected."""
    with pytest.raises(ValueError, match="remote_port requires remote_host"):
        SomeIp(host="127.0.0.1", port=30490, remote_port=31000)


@patch("jumpstarter_driver_someip.driver.ClientConfig")
@patch("jumpstarter_driver_someip.driver.OsipClient")
def test_someip_rpc_call_with_remote_endpoint(mock_osip_cls, _mock_config_cls):
    """RPC call works when a static remote endpoint is configured."""
    mock_client = _make_mock_osip_client()
    mock_osip_cls.return_value = mock_client

    driver = SomeIp(
        host="192.168.100.1",
        port=30490,
        remote_host="192.168.100.10",
        remote_port=30490,
    )
    with serve(driver) as client:
        resp = client.rpc_call(0x1234, 0x0001, b"\x01\x02\x03")
        assert resp.service_id == 0x1234
        assert resp.method_id == 0x0001
        assert resp.payload == "010203"


# =========================================================================
# Stateful integration tests
#
# These use a StatefulOsipClient (conftest.py) that behaves like a real
# SOME/IP service: it tracks connection state, service registry, event
# subscriptions, RPC history, and sent messages.  Each test exercises a
# realistic multi-step workflow through the full gRPC boundary.
# =========================================================================


def _stateful_client_ctx(stateful_osip):
    """Context manager: serve() a SomeIp driver backed by the stateful mock."""
    with patch(
        "jumpstarter_driver_someip.driver.OsipClient",
        return_value=stateful_osip,
    ):
        instance = SomeIp(host="127.0.0.1", port=30490)
        with serve(instance) as c:
            yield c


@pytest.fixture
def stateful_client(stateful_osip):
    yield from _stateful_client_ctx(stateful_osip)


# - RPC workflows ---------------------------------------------------------


def test_stateful_rpc_call_returns_canned_response(stateful_client, stateful_osip):
    """RPC call to a known service/method returns the pre-configured response."""
    resp = stateful_client.rpc_call(0x1234, 0x0001, b"\xFF")
    assert resp.service_id == 0x1234
    assert resp.method_id == 0x0001
    assert resp.payload == "0a0b0c"
    assert resp.return_code == 0x00
    assert len(stateful_osip._rpc_history) == 1
    assert stateful_osip._rpc_history[0] == (0x1234, 0x0001, b"\xFF")


def test_stateful_rpc_call_unknown_echoes_payload(stateful_client, stateful_osip):
    """RPC call to an unknown service/method echoes the request payload."""
    resp = stateful_client.rpc_call(0x9999, 0x0001, b"\xDE\xAD")
    assert resp.service_id == 0x9999
    assert resp.payload == "dead"


def test_stateful_multiple_rpc_calls(stateful_client, stateful_osip):
    """Multiple sequential RPC calls are tracked independently."""
    stateful_client.rpc_call(0x1234, 0x0001, b"\x01")
    stateful_client.rpc_call(0x1234, 0x0002, b"\x02")
    stateful_client.rpc_call(0x5678, 0x0001, b"\x03")

    assert len(stateful_osip._rpc_history) == 3
    assert stateful_osip._rpc_history[0][0] == 0x1234
    assert stateful_osip._rpc_history[1][2] == b"\x02"
    assert stateful_osip._rpc_history[2][0] == 0x5678


def test_stateful_custom_rpc_response(stateful_client, stateful_osip):
    """Register a custom RPC response and verify it's returned."""
    stateful_osip.register_rpc_response(0xAAAA, 0x0001, b"\xCA\xFE")
    resp = stateful_client.rpc_call(0xAAAA, 0x0001, b"\x00")
    assert resp.payload == "cafe"


# - send / receive messaging workflow -------------------------------------


def test_stateful_send_then_receive(stateful_client, stateful_osip):
    """send_message echoes into the receive queue; receive_message reads it."""
    stateful_client.send_message(0x1234, 0x0001, b"\xAA\xBB")
    resp = stateful_client.receive_message(timeout=1.0)
    assert resp.service_id == 0x1234
    assert resp.method_id == 0x0001
    assert resp.payload == "aabb"
    assert len(stateful_osip._sent_messages) == 1


def test_stateful_inject_message(stateful_client, stateful_osip):
    """Injected messages appear in the receive queue."""
    stateful_osip.inject_message(0x5678, 0x0002, b"\x01\x02\x03")
    resp = stateful_client.receive_message(timeout=1.0)
    assert resp.service_id == 0x5678
    assert resp.method_id == 0x0002
    assert resp.payload == "010203"


def test_stateful_multiple_messages_fifo(stateful_client, stateful_osip):
    """Multiple injected messages are received in FIFO order."""
    stateful_osip.inject_message(0x1111, 0x0001, b"\x01")
    stateful_osip.inject_message(0x2222, 0x0002, b"\x02")
    stateful_osip.inject_message(0x3333, 0x0003, b"\x03")

    r1 = stateful_client.receive_message(timeout=1.0)
    r2 = stateful_client.receive_message(timeout=1.0)
    r3 = stateful_client.receive_message(timeout=1.0)

    assert r1.service_id == 0x1111
    assert r2.service_id == 0x2222
    assert r3.service_id == 0x3333


# - service discovery workflow --------------------------------------------


def test_stateful_find_service_all_instances(stateful_client):
    """find_service with wildcard instance_id returns all matching services."""
    results = stateful_client.find_service(0x1234, timeout=0.1)
    assert len(results) == 2
    ids = {r.instance_id for r in results}
    assert ids == {0x0001, 0x0002}


def test_stateful_find_service_specific_instance(stateful_client):
    """find_service with specific instance_id returns only that instance."""
    results = stateful_client.find_service(0x1234, instance_id=0x0001, timeout=0.1)
    assert len(results) == 1
    assert results[0].instance_id == 0x0001
    assert results[0].major_version == 1


def test_stateful_find_service_not_found(stateful_client):
    """find_service for a non-existent service returns empty list."""
    results = stateful_client.find_service(0xDEAD, timeout=0.1)
    assert results == []


def test_stateful_find_service_version_info(stateful_client):
    """find_service returns correct version information."""
    results = stateful_client.find_service(0x1234, instance_id=0x0002, timeout=0.1)
    assert len(results) == 1
    assert results[0].major_version == 2
    assert results[0].minor_version == 0


def test_stateful_find_different_services(stateful_client):
    """find_service for different service IDs returns independent results."""
    results_1234 = stateful_client.find_service(0x1234, timeout=0.1)
    results_5678 = stateful_client.find_service(0x5678, timeout=0.1)

    assert len(results_1234) == 2
    assert len(results_5678) == 1
    assert results_5678[0].service_id == 0x5678
    assert results_5678[0].instance_id == 0x0001


def test_stateful_find_service_dynamic_registration(stateful_client, stateful_osip):
    """Services registered after startup appear in subsequent discoveries."""
    results_before = stateful_client.find_service(0xAAAA, timeout=0.1)
    assert results_before == []

    stateful_osip.register_service(0xAAAA, 0x0001, major_version=3, minor_version=1)
    results_after = stateful_client.find_service(0xAAAA, timeout=0.1)
    assert len(results_after) == 1
    assert results_after[0].service_id == 0xAAAA
    assert results_after[0].major_version == 3
    assert results_after[0].minor_version == 1


def test_stateful_find_service_dynamic_unregistration(stateful_client, stateful_osip):
    """Services removed from the registry no longer appear in discoveries."""
    results_before = stateful_client.find_service(0x5678, timeout=0.1)
    assert len(results_before) == 1

    stateful_osip.unregister_service(0x5678, 0x0001)
    results_after = stateful_client.find_service(0x5678, timeout=0.1)
    assert results_after == []


def test_stateful_find_service_after_reconnect(stateful_client, stateful_osip):
    """Service registry persists across reconnect."""
    results_before = stateful_client.find_service(0x1234, timeout=0.1)
    assert len(results_before) == 2

    stateful_client.reconnect()

    results_after = stateful_client.find_service(0x1234, timeout=0.1)
    assert len(results_after) == 2


def test_stateful_find_service_default_instance_wildcard(stateful_client):
    """find_service without explicit instance_id uses 0xFFFF wildcard."""
    results_explicit = stateful_client.find_service(0x1234, instance_id=0xFFFF, timeout=0.1)
    results_default = stateful_client.find_service(0x1234, timeout=0.1)

    assert len(results_explicit) == len(results_default)
    explicit_ids = {r.instance_id for r in results_explicit}
    default_ids = {r.instance_id for r in results_default}
    assert explicit_ids == default_ids


def test_stateful_discover_then_rpc_to_each_instance(stateful_client, stateful_osip):
    """Discover all instances, then make RPC calls to each one."""
    services = stateful_client.find_service(0x1234, timeout=0.1)
    assert len(services) == 2

    for svc in services:
        resp = stateful_client.rpc_call(svc.service_id, 0x0001, b"\xAA")
        assert resp.service_id == svc.service_id

    assert len(stateful_osip._rpc_history) == 2


# - event subscription workflow -------------------------------------------


def test_stateful_subscribe_receive_unsubscribe(stateful_client, stateful_osip):
    """Full event lifecycle: subscribe, receive events, unsubscribe."""
    stateful_client.subscribe_eventgroup(1)
    assert 1 in stateful_osip._subscribed_eventgroups

    stateful_osip.inject_event(0x1234, 0x8001, b"\xCA\xFE")
    event = stateful_client.receive_event(timeout=1.0)
    assert event.service_id == 0x1234
    assert event.event_id == 0x8001
    assert event.payload == "cafe"

    stateful_client.unsubscribe_eventgroup(1)
    assert 1 not in stateful_osip._subscribed_eventgroups


def test_stateful_multiple_events_fifo(stateful_client, stateful_osip):
    """Multiple events are received in FIFO order."""
    stateful_client.subscribe_eventgroup(1)

    stateful_osip.inject_event(0x1234, 0x8001, b"\x01")
    stateful_osip.inject_event(0x1234, 0x8002, b"\x02")
    stateful_osip.inject_event(0x1234, 0x8003, b"\x03")

    e1 = stateful_client.receive_event(timeout=1.0)
    e2 = stateful_client.receive_event(timeout=1.0)
    e3 = stateful_client.receive_event(timeout=1.0)

    assert e1.event_id == 0x8001
    assert e2.event_id == 0x8002
    assert e3.event_id == 0x8003

    stateful_client.unsubscribe_eventgroup(1)


def test_stateful_subscribe_multiple_eventgroups(stateful_client, stateful_osip):
    """Subscribing to multiple event groups tracks all of them."""
    stateful_client.subscribe_eventgroup(1)
    stateful_client.subscribe_eventgroup(2)
    stateful_client.subscribe_eventgroup(3)

    assert stateful_osip._subscribed_eventgroups == {1, 2, 3}

    stateful_client.unsubscribe_eventgroup(2)
    assert stateful_osip._subscribed_eventgroups == {1, 3}


def test_stateful_event_timeout_when_no_events(stateful_client):
    """receive_event times out when no events are available."""
    with pytest.raises(DriverError, match="No event received"):
        stateful_client.receive_event(timeout=0.1)


# - connection management workflows ---------------------------------------


def test_stateful_reconnect_resets_subscriptions(stateful_client, stateful_osip):
    """reconnect() stops and restarts the client, clearing subscriptions."""
    stateful_client.subscribe_eventgroup(1)
    assert 1 in stateful_osip._subscribed_eventgroups

    stateful_client.reconnect()
    assert stateful_osip._started is True
    assert stateful_osip._subscribed_eventgroups == set()


def test_stateful_close_then_reconnect(stateful_client, stateful_osip):
    """close_connection + reconnect restores the client."""
    stateful_client.close_connection()
    assert stateful_osip._started is False

    stateful_client.reconnect()
    assert stateful_osip._started is True


# - end-to-end composite workflows ----------------------------------------


def test_stateful_full_rpc_session(stateful_client, stateful_osip):
    """Simulate a complete RPC session: discover, call, verify, disconnect."""
    services = stateful_client.find_service(0x1234, timeout=0.1)
    assert len(services) >= 1

    resp = stateful_client.rpc_call(0x1234, 0x0001, b"\x01\x02\x03")
    assert resp.service_id == 0x1234
    assert resp.return_code == 0x00

    assert len(stateful_osip._rpc_history) == 1

    stateful_client.close_connection()
    assert stateful_osip._started is False


def test_stateful_messaging_with_reconnect(stateful_client, stateful_osip):
    """Send messages, reconnect, verify the client is operational again."""
    stateful_client.send_message(0x1234, 0x0001, b"\x01")
    resp = stateful_client.receive_message(timeout=1.0)
    assert resp.payload == "01"

    stateful_client.reconnect()

    stateful_client.send_message(0x5678, 0x0002, b"\x02")
    resp = stateful_client.receive_message(timeout=1.0)
    assert resp.payload == "02"

    assert len(stateful_osip._sent_messages) == 2


def test_stateful_event_session_with_reconnect(stateful_client, stateful_osip):
    """Subscribe, receive events, reconnect, re-subscribe, receive again."""
    stateful_client.subscribe_eventgroup(1)
    stateful_osip.inject_event(0x1234, 0x8001, b"\xAA")
    e1 = stateful_client.receive_event(timeout=1.0)
    assert e1.payload == "aa"

    stateful_client.reconnect()
    assert stateful_osip._subscribed_eventgroups == set()

    stateful_client.subscribe_eventgroup(1)
    stateful_osip.inject_event(0x1234, 0x8002, b"\xBB")
    e2 = stateful_client.receive_event(timeout=1.0)
    assert e2.payload == "bb"

    stateful_client.unsubscribe_eventgroup(1)


def test_stateful_discover_rpc_events_workflow(stateful_client, stateful_osip):
    """Full workflow: discover services, make RPC calls, subscribe to events,
    receive notifications, and clean up."""
    services = stateful_client.find_service(0x1234, timeout=0.1)
    assert len(services) == 2

    resp1 = stateful_client.rpc_call(0x1234, 0x0001, b"\x10")
    assert resp1.payload == "0a0b0c"

    resp2 = stateful_client.rpc_call(0x1234, 0x0002, b"\x20")
    assert resp2.payload == "01020304"

    stateful_client.subscribe_eventgroup(1)
    stateful_osip.inject_event(0x1234, 0x8001, b"\xEE")
    event = stateful_client.receive_event(timeout=1.0)
    assert event.payload == "ee"

    stateful_client.unsubscribe_eventgroup(1)
    stateful_client.close_connection()

    assert len(stateful_osip._rpc_history) == 2
    assert stateful_osip._started is False


# =========================================================================
# Server / provider side tests
#
# The server verbs let the endpoint ACT AS an ECU (offer services, answer
# RPC with canned responses, publish events). We patch the opensomeip
# SomeIpServer so these run without real networking, and assert the driver
# drives the underlying server API correctly.
# =========================================================================


class _FakeOsipServer:
    """Minimal stand-in for opensomeip.SomeIpServer for server-side unit tests."""

    def __init__(self, config=None):
        self.config = config
        self.started = False
        self._offered: set = set()
        self.handlers: dict = {}
        self.registered_events: dict = {}
        self.register_event_calls: list = []
        self.published: list = []
        self.fields: dict = {}

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def offer(self, service):
        self._offered.add((service.service_id, service.instance_id, service.major_version, service.minor_version))

    def stop_offer(self, service):
        self._offered.discard((service.service_id, service.instance_id, service.major_version, service.minor_version))

    @property
    def offered_services(self):
        out = []
        for sid, iid, maj, minr in self._offered:
            out.append(
                type(
                    "Svc",
                    (),
                    {
                        "service_id": sid,
                        "instance_id": iid,
                        "major_version": maj,
                        "minor_version": minr,
                    },
                )()
            )
        return out

    def register_method(self, message_id, handler):
        self.handlers[(message_id.service_id, message_id.method_id)] = handler

    def register_event(self, event_id, eventgroup_id):
        self.register_event_calls.append((event_id, eventgroup_id))
        self.registered_events[event_id] = eventgroup_id

    def publish_event(self, event_id, payload):
        self.published.append((event_id, payload))

    def set_field(self, event_id, payload):
        self.fields[event_id] = payload


@patch("jumpstarter_driver_someip.driver.OsipServer")
def test_server_offer_and_list(mock_server_cls):
    fake = _FakeOsipServer()
    mock_server_cls.return_value = fake

    driver = SomeIp(host="127.0.0.1", port=30490)
    with serve(driver) as client:
        client.offer_service(0x1801, 0x0001, major_version=2)
        offered = client.list_offered_services()
        assert len(offered) == 1
        assert isinstance(offered[0], SomeIpOfferedService)
        assert offered[0].service_id == 0x1801
        assert offered[0].instance_id == 0x0001
        assert offered[0].major_version == 2
        assert fake.started is True


@pytest.mark.parametrize("reserved_instance_id", [0x0000, 0xFFFF])
@patch("jumpstarter_driver_someip.driver.OsipServer")
def test_server_offer_rejects_reserved_instance_ids(mock_server_cls, reserved_instance_id):
    """Instance ID 0x0000 is reserved and 0xFFFF means 'all instances';
    neither is a valid concrete instance to offer."""
    fake = _FakeOsipServer()
    mock_server_cls.return_value = fake

    driver = SomeIp(host="127.0.0.1", port=30490)
    with serve(driver) as client:
        with pytest.raises(DriverError, match="instance_id"):
            client.offer_service(0x1801, reserved_instance_id)
        # Rejected before the server is even created; nothing is offered
        assert client.list_offered_services() == []
        mock_server_cls.assert_not_called()


@patch("jumpstarter_driver_someip.driver.OsipServer")
def test_server_stop_offer(mock_server_cls):
    fake = _FakeOsipServer()
    mock_server_cls.return_value = fake

    driver = SomeIp(host="127.0.0.1", port=30490)
    with serve(driver) as client:
        client.offer_service(0x1801, 0x0001)
        assert len(client.list_offered_services()) == 1
        client.stop_offer_service(0x1801, 0x0001)
        assert client.list_offered_services() == []


@patch("jumpstarter_driver_someip.driver.OsipServer")
def test_server_set_method_response_registers_handler(mock_server_cls):
    fake = _FakeOsipServer()
    mock_server_cls.return_value = fake

    driver = SomeIp(host="127.0.0.1", port=30490)
    with serve(driver) as client:
        client.set_method_response(0x1801, 0x0005, b"\xde\xad\xbe\xef")
        # Handler registered exactly once for the method
        assert (0x1801, 0x0005) in fake.handlers

        # Invoke the registered handler as the RpcServer would, and check the reply

        req = Message(message_id=MessageId(0x1801, 0x0005), request_id=RequestId(0x0001, 0x0007))
        resp = fake.handlers[(0x1801, 0x0005)](req)
        assert resp.payload == b"\xde\xad\xbe\xef"
        assert resp.return_code == ReturnCode.E_OK
        assert resp.message_type == MessageType.RESPONSE
        # Response echoes the request's request_id (client/session correlation)
        assert resp.request_id.session_id == 0x0007


@patch("jumpstarter_driver_someip.driver.OsipServer")
def test_server_method_response_updates_without_reregister(mock_server_cls):
    fake = _FakeOsipServer()
    mock_server_cls.return_value = fake

    driver = SomeIp(host="127.0.0.1", port=30490)
    with serve(driver) as client:
        client.set_method_response(0x1801, 0x0005, b"\x01")
        handler = fake.handlers[(0x1801, 0x0005)]
        client.set_method_response(0x1801, 0x0005, b"\x02")
        # Same handler object reused (registered once); response value updated live
        assert fake.handlers[(0x1801, 0x0005)] is handler

        req = Message(message_id=MessageId(0x1801, 0x0005), request_id=RequestId(1, 1))
        assert handler(req).payload == b"\x02"


@patch("jumpstarter_driver_someip.driver.OsipServer")
def test_server_method_response_error_code(mock_server_cls):
    fake = _FakeOsipServer()
    mock_server_cls.return_value = fake

    driver = SomeIp(host="127.0.0.1", port=30490)
    with serve(driver) as client:
        client.set_method_response(0x1801, 0x0006, b"", return_code=0x01)

        req = Message(message_id=MessageId(0x1801, 0x0006), request_id=RequestId(1, 1))
        resp = fake.handlers[(0x1801, 0x0006)](req)
        assert resp.return_code == ReturnCode.E_NOT_OK
        assert resp.message_type == MessageType.ERROR


@patch("jumpstarter_driver_someip.driver.OsipServer")
def test_server_clear_method_response_falls_back_to_empty_ok(mock_server_cls):
    fake = _FakeOsipServer()
    mock_server_cls.return_value = fake

    driver = SomeIp(host="127.0.0.1", port=30490)
    with serve(driver) as client:
        client.set_method_response(0x1801, 0x0005, b"\xaa")
        handler = fake.handlers[(0x1801, 0x0005)]
        client.clear_method_response(0x1801, 0x0005)

        req = Message(message_id=MessageId(0x1801, 0x0005), request_id=RequestId(1, 1))
        resp = handler(req)
        assert resp.payload == b""
        assert resp.return_code == ReturnCode.E_OK


@patch("jumpstarter_driver_someip.driver.OsipServer")
def test_server_register_and_publish_event(mock_server_cls):
    fake = _FakeOsipServer()
    mock_server_cls.return_value = fake

    driver = SomeIp(host="127.0.0.1", port=30490)
    with serve(driver) as client:
        client.register_event(0x1801, 0x8001, eventgroup_id=1)
        assert fake.registered_events[0x8001] == 1

        client.publish_event(0x1801, 0x8001, b"\x2d\x00")  # e.g. signal level payload
        assert fake.published == [(0x8001, b"\x2d\x00")]


@patch("jumpstarter_driver_someip.driver.OsipServer")
def test_server_register_event_idempotent(mock_server_cls):
    fake = _FakeOsipServer()
    mock_server_cls.return_value = fake

    driver = SomeIp(host="127.0.0.1", port=30490)
    with serve(driver) as client:
        client.register_event(0x1801, 0x8001, eventgroup_id=1)
        client.register_event(0x1801, 0x8001, eventgroup_id=1)
        # not re-registered with the underlying server for the same (event, group)
        assert fake.register_event_calls == [(0x8001, 1)]


@patch("jumpstarter_driver_someip.driver.OsipServer")
def test_server_set_field(mock_server_cls):
    fake = _FakeOsipServer()
    mock_server_cls.return_value = fake

    driver = SomeIp(host="127.0.0.1", port=30490)
    with serve(driver) as client:
        client.set_field(0x1801, 0x8002, b"\x01")
        assert fake.fields[0x8002] == b"\x01"


@patch("jumpstarter_driver_someip.driver.OsipServer")
def test_server_stop_server_clears_state(mock_server_cls):
    fake = _FakeOsipServer()
    mock_server_cls.return_value = fake

    driver = SomeIp(host="127.0.0.1", port=30490)
    with serve(driver) as client:
        client.set_method_response(0x1801, 0x0005, b"\xaa")
        client.register_event(0x1801, 0x8001, eventgroup_id=1)
        client.stop_server()
        assert fake.started is False

        # After stop, a new offer builds a fresh server and re-registers cleanly
        fake2 = _FakeOsipServer()
        mock_server_cls.return_value = fake2
        client.offer_service(0x1802, 0x0001)
        assert fake2.started is True
        assert len(client.list_offered_services()) == 1


@patch("jumpstarter_driver_someip.driver.OsipServer")
def test_server_list_offered_when_not_started(mock_server_cls):
    fake = _FakeOsipServer()
    mock_server_cls.return_value = fake

    driver = SomeIp(host="127.0.0.1", port=30490)
    with serve(driver) as client:
        # No offer yet -> server never created -> empty list, no crash
        assert client.list_offered_services() == []
        mock_server_cls.assert_not_called()


@patch("jumpstarter_driver_someip.driver.OsipServer")
def test_server_lazy_start(mock_server_cls):
    """Server is not created at construction; created on first server verb."""
    fake = _FakeOsipServer()
    mock_server_cls.return_value = fake

    driver = SomeIp(host="127.0.0.1", port=30490)
    mock_server_cls.assert_not_called()
    with serve(driver) as client:
        mock_server_cls.assert_not_called()
        client.start_server()
        mock_server_cls.assert_called_once()
        assert fake.started is True


# =========================================================================
# Stateful loopback server tests
#
# These use the StatefulOsipServer / LoopbackOsipClient pair (conftest.py):
# RPC calls made through the driver's client verbs are dispatched to the
# handlers the driver registered through its server verbs, so each test
# exercises the full simulated-ECU workflow (offer + canned response +
# client RPC) through the gRPC boundary.
# =========================================================================


def _loopback_client_ctx(loopback_pair):
    """Context manager: serve() a SomeIp driver backed by the loopback pair."""
    server, osip_client = loopback_pair
    with (
        patch("jumpstarter_driver_someip.driver.OsipServer", return_value=server),
        patch("jumpstarter_driver_someip.driver.OsipClient", return_value=osip_client),
    ):
        instance = SomeIp(host="127.0.0.1", port=30490)
        with serve(instance) as c:
            yield c


@pytest.fixture
def loopback_client(loopback_pair):
    yield from _loopback_client_ctx(loopback_pair)


def test_loopback_offer_discover_rpc(loopback_client):
    """Simulated-ECU loop: offer a service, configure a canned response, and
    call it back through the client RPC verbs."""
    loopback_client.offer_service(0x1801, 0x0001, major_version=2)

    services = loopback_client.find_service(0x1801, timeout=0.1)
    assert len(services) == 1
    assert services[0].instance_id == 0x0001
    assert services[0].major_version == 2

    loopback_client.set_method_response(0x1801, 0x0005, b"\xde\xad\xbe\xef")
    resp = loopback_client.rpc_call(0x1801, 0x0005, b"\x00")
    assert resp.service_id == 0x1801
    assert resp.method_id == 0x0005
    assert resp.payload == "deadbeef"
    assert resp.return_code == 0x00


def test_loopback_method_response_update_takes_effect(loopback_client):
    """Updating a canned response changes what subsequent RPC calls read."""
    loopback_client.offer_service(0x1801)
    loopback_client.set_method_response(0x1801, 0x0005, b"\x01")
    assert loopback_client.rpc_call(0x1801, 0x0005, b"").payload == "01"

    loopback_client.set_method_response(0x1801, 0x0005, b"\x02")
    assert loopback_client.rpc_call(0x1801, 0x0005, b"").payload == "02"


def test_loopback_method_error_return_code(loopback_client):
    """A canned error return code is surfaced to the RPC caller."""
    loopback_client.offer_service(0x1801)
    loopback_client.set_method_response(0x1801, 0x0006, b"", return_code=0x01)

    resp = loopback_client.rpc_call(0x1801, 0x0006, b"")
    assert resp.return_code == 0x01


def test_loopback_publish_event_roundtrip(loopback_client):
    """Events published by the server side reach a subscribed client."""
    loopback_client.register_event(0x1801, 0x8001, eventgroup_id=1)
    loopback_client.subscribe_eventgroup(1)

    loopback_client.publish_event(0x1801, 0x8001, b"\xca\xfe")
    event = loopback_client.receive_event(timeout=1.0)
    assert event.event_id == 0x8001
    assert event.payload == "cafe"


def test_loopback_stop_server_clears_canned_responses(loopback_client):
    """Canned responses must not survive stop_server + restart."""
    loopback_client.offer_service(0x1801)
    loopback_client.set_method_response(0x1801, 0x0005, b"\xaa")
    assert loopback_client.rpc_call(0x1801, 0x0005, b"").payload == "aa"

    loopback_client.stop_server()

    # After a restart the stale payload is gone; the handler serves the
    # empty E_OK default until reconfigured.
    loopback_client.offer_service(0x1801)
    resp = loopback_client.rpc_call(0x1801, 0x0005, b"")
    assert resp.payload == ""
    assert resp.return_code == 0x00


# =========================================================================
# Wire-level integration tests with MockSomeIpServer
#
# opensomeip uses Service Discovery to locate services, so connecting to a
# raw TCP mock server requires a full SD-capable SOME/IP environment.
# These tests are intended for CI environments with proper SOME/IP networking
# and are skipped by default.  Set SOMEIP_INTEGRATION_TESTS=1 to enable.
# =========================================================================

_RUN_INTEGRATION = os.environ.get("SOMEIP_INTEGRATION_TESTS", "0") == "1"


@pytest.mark.skipif(not _RUN_INTEGRATION, reason="SOMEIP_INTEGRATION_TESTS not set")
def test_someip_simulated_rpc_call(mock_someip_server):
    driver = SomeIp(
        host="127.0.0.1",
        port=mock_someip_server,
        transport_mode="TCP",
    )
    with serve(driver) as client:
        resp = client.rpc_call(0x1234, 0x0001, b"\x01\x02\x03")
        assert resp.service_id == 0x1234
        assert resp.method_id == 0x0001
        assert resp.return_code == 0x00
        assert resp.payload == "010203"


@pytest.mark.skipif(not _RUN_INTEGRATION, reason="SOMEIP_INTEGRATION_TESTS not set")
def test_someip_simulated_send_receive(mock_someip_server):
    driver = SomeIp(
        host="127.0.0.1",
        port=mock_someip_server,
        transport_mode="TCP",
    )
    with serve(driver) as client:
        client.send_message(0x1234, 0x0001, b"\xAA\xBB\xCC")
        resp = client.receive_message(timeout=2.0)
        assert resp.service_id == 0x1234
        assert resp.payload == "aabbcc"
