from __future__ import annotations

import logging
import queue
import threading
from dataclasses import field

from opensomeip import ClientConfig, ServerConfig, TransportMode
from opensomeip import SomeIpClient as OsipClient
from opensomeip import SomeIpServer as OsipServer
from opensomeip.message import Message
from opensomeip.sd import SdConfig, ServiceInstance
from opensomeip.transport import Endpoint
from opensomeip.types import MessageId, MessageType, ReturnCode

try:
    from opensomeip._bridge import get_ext
except ImportError:
    get_ext = lambda: None  # noqa: E731  # ty: ignore[invalid-assignment]
from pydantic import ConfigDict, SkipValidation, validate_call
from pydantic.dataclasses import dataclass

from .common import (
    SomeIpEventNotification,
    SomeIpMessageResponse,
    SomeIpOfferedService,
    SomeIpPayload,
    SomeIpServiceEntry,
)
from jumpstarter.driver import Driver, export

logger = logging.getLogger(__name__)

_VALID_TRANSPORT_MODES = {"TCP", "UDP"}

# SOME/IP reserves Instance ID 0x0000; 0xFFFF means "all instances" and is
# only valid in find/subscribe contexts, never as a concrete offered instance.
_RESERVED_INSTANCE_IDS = {0x0000, 0xFFFF}


def _message_to_response(msg: Message) -> SomeIpMessageResponse:
    return SomeIpMessageResponse(
        service_id=msg.message_id.service_id,
        method_id=msg.message_id.method_id,
        client_id=msg.request_id.client_id,
        session_id=msg.request_id.session_id,
        protocol_version=int(msg.protocol_version),
        interface_version=msg.interface_version,
        message_type=int(msg.message_type),
        return_code=int(msg.return_code),
        payload=msg.payload.hex(),
    )


def _receive_from_queue(receiver: object, timeout: float, error_msg: str) -> Message:
    """Receive a message from a MessageReceiver's internal queue.

    opensomeip's MessageReceiver exposes __iter__/__next__ but no public
    blocking-with-timeout method. We access the internal _sync_queue as a
    pragmatic workaround until a public API is provided.
    """
    sync_queue = getattr(receiver, "_sync_queue", None)
    if sync_queue is None:
        raise RuntimeError("opensomeip MessageReceiver missing _sync_queue; API may have changed")
    try:
        return sync_queue.get(timeout=timeout)
    except queue.Empty:
        raise TimeoutError(error_msg) from None


@dataclass(kw_only=True, config=ConfigDict(arbitrary_types_allowed=True))
class SomeIp(Driver):
    """SOME/IP driver wrapping the opensomeip Python binding.

    Provides remote access to SOME/IP protocol operations including
    RPC calls, service discovery, raw messaging, and event subscriptions
    per the SOME/IP specification.
    """

    driver_type = "automotive"

    host: str
    port: int = 30490
    transport_mode: str = "UDP"
    multicast_group: str = "239.127.0.1"
    multicast_port: int = 30490
    remote_host: str | None = None
    remote_port: int | None = None

    _osip_client: OsipClient | None = field(init=False, repr=False, default=None)
    _osip_config: ClientConfig = field(init=False, repr=False)
    _osip_lock: SkipValidation[threading.Lock] = field(init=False, repr=False, default_factory=threading.Lock)

    # Server / provider side (offer services, answer RPC, publish events).
    _osip_server: OsipServer | None = field(init=False, repr=False, default=None)
    _server_lock: SkipValidation[threading.Lock] = field(init=False, repr=False, default_factory=threading.Lock)
    # Canned RPC responses keyed by (service_id, method_id) -> (payload bytes, return_code).
    _method_responses: SkipValidation[dict] = field(init=False, repr=False, default_factory=dict)
    # Methods already registered with the underlying RpcServer (register_method is one-shot).
    _registered_methods: SkipValidation[set] = field(init=False, repr=False, default_factory=set)
    # Events registered for publishing: (service_id, event_id) -> eventgroup_id.
    _registered_events: SkipValidation[dict] = field(init=False, repr=False, default_factory=dict)

    @classmethod
    def client(cls) -> str:
        return "jumpstarter_driver_someip.client.SomeIpDriverClient"

    def __post_init__(self):
        if hasattr(super(), "__post_init__"):
            super().__post_init__()

        transport_upper = self.transport_mode.upper()
        if transport_upper not in _VALID_TRANSPORT_MODES:
            raise ValueError(
                f"Invalid transport_mode: {self.transport_mode!r}. Must be 'TCP' or 'UDP'."
            )

        if get_ext() is None:
            raise RuntimeError(
                "opensomeip C++ extension (_opensomeip) is not available. "
                "The SOME/IP driver requires the native extension for network I/O. "
                "On macOS, rebuild with the system compiler: "
                "CC=/usr/bin/clang CXX=/usr/bin/clang++ "
                "pip install --no-cache-dir --force-reinstall --no-binary=opensomeip opensomeip. "
                "On other platforms, reinstall opensomeip and ensure the C++ "
                "build toolchain is available."
            )

        mode = TransportMode.TCP if transport_upper == "TCP" else TransportMode.UDP

        local_ep = Endpoint(self.host, self.port)
        sd_cfg = SdConfig(
            multicast_endpoint=Endpoint(self.multicast_group, self.multicast_port),
            unicast_endpoint=Endpoint(self.host, self.port),
        )

        if self.remote_port is not None and self.remote_host is None:
            raise ValueError("remote_port requires remote_host to be set")

        if self.remote_host is not None:
            remote_ep = Endpoint(
                self.remote_host,
                self.remote_port if self.remote_port is not None else self.port,
            )
            self._osip_config = ClientConfig(
                local_endpoint=local_ep,
                sd_config=sd_cfg,
                transport_mode=mode,
                remote_endpoint=remote_ep,
            )
        else:
            self._osip_config = ClientConfig(
                local_endpoint=local_ep,
                sd_config=sd_cfg,
                transport_mode=mode,
            )

    def _ensure_client(self) -> OsipClient:
        """Create and start the OsipClient on first use (thread-safe)."""
        if self._osip_client is None:
            with self._osip_lock:
                if self._osip_client is None:
                    self._osip_client = OsipClient(self._osip_config)
                    self._osip_client.start()
        return self._osip_client

    def close(self):
        """Stop the opensomeip client and server."""
        if self._osip_client is not None:
            try:
                self._osip_client.stop()
            except Exception:
                logger.warning("failed to close opensomeip client", exc_info=True)
        if self._osip_server is not None:
            try:
                self._osip_server.stop()
            except Exception:
                logger.warning("failed to close opensomeip server", exc_info=True)
        super().close()

    # --- RPC ---

    @export
    @validate_call(validate_return=True)
    def start(self) -> None:
        """Force start the SOME/IP client."""
        self._ensure_client()

    @export
    @validate_call(validate_return=True)
    def rpc_call(
        self,
        service_id: int,
        method_id: int,
        payload: SomeIpPayload,
        timeout: float = 5.0,
    ) -> SomeIpMessageResponse:
        """Make a SOME/IP RPC call and return the response."""
        response = self._ensure_client().call(
            MessageId(service_id, method_id),
            payload=bytes.fromhex(payload.data),
            timeout=timeout,
        )
        return _message_to_response(response)

    # --- Raw Messaging ---

    @export
    @validate_call(validate_return=True)
    def send_message(
        self,
        service_id: int,
        method_id: int,
        payload: SomeIpPayload,
    ) -> None:
        """Send a raw SOME/IP message."""
        msg = Message(
            message_id=MessageId(service_id, method_id),
            payload=bytes.fromhex(payload.data),
        )
        self._ensure_client().send(msg)

    @export
    @validate_call(validate_return=True)
    def receive_message(self, timeout: float = 2.0) -> SomeIpMessageResponse:
        """Receive a raw SOME/IP message."""
        receiver = self._ensure_client().transport.receiver
        msg = _receive_from_queue(receiver, timeout, f"No message received within {timeout}s")
        return _message_to_response(msg)

    # --- Service Discovery ---

    @export
    @validate_call(validate_return=True)
    def find_service(
        self,
        service_id: int,
        instance_id: int = 0xFFFF,
        timeout: float = 5.0,
    ) -> list[SomeIpServiceEntry]:
        """Find services via SOME/IP-SD."""
        service = ServiceInstance(
            service_id=service_id,
            instance_id=instance_id,
        )
        found: list[SomeIpServiceEntry] = []
        event = threading.Event()
        lock = threading.Lock()

        def on_found(svc: ServiceInstance) -> None:
            with lock:
                found.append(
                    SomeIpServiceEntry(
                        service_id=svc.service_id,
                        instance_id=svc.instance_id,
                        major_version=svc.major_version,
                        minor_version=svc.minor_version,
                    )
                )
            event.set()

        self._ensure_client().find(service, callback=on_found)
        event.wait(timeout=timeout)
        with lock:
            return list(found)

    @export
    @validate_call(validate_return=True)
    def subscribe_eventgroup(self, eventgroup_id: int) -> None:
        """Subscribe to a SOME/IP event group."""
        self._ensure_client().subscribe_events(eventgroup_id)

    @export
    @validate_call(validate_return=True)
    def unsubscribe_eventgroup(self, eventgroup_id: int) -> None:
        """Unsubscribe from a SOME/IP event group."""
        self._ensure_client().unsubscribe_events(eventgroup_id)

    @export
    @validate_call(validate_return=True)
    def receive_event(self, timeout: float = 5.0) -> SomeIpEventNotification:
        """Receive the next event notification."""
        receiver = self._ensure_client().event_subscriber.notifications()
        msg = _receive_from_queue(receiver, timeout, f"No event received within {timeout}s")
        return SomeIpEventNotification(
            service_id=msg.message_id.service_id,
            event_id=msg.message_id.method_id,
            payload=msg.payload.hex(),
        )

    # --- Connection Management ---

    @export
    @validate_call(validate_return=True)
    def close_connection(self) -> None:
        """Close the SOME/IP connection."""
        if self._osip_client is not None:
            try:
                self._osip_client.stop()
            except Exception:
                logger.warning("failed to stop opensomeip client during close_connection", exc_info=True)

    @export
    @validate_call(validate_return=True)
    def reconnect(self) -> None:
        """Reconnect to the SOME/IP endpoint."""
        if self._osip_client is not None:
            try:
                self._osip_client.stop()
            except Exception:
                logger.warning("failed to stop opensomeip client during reconnect", exc_info=True)
            self._osip_client.start()
        else:
            self._ensure_client()

    # =====================================================================
    # Server / provider side
    #
    # Lets this endpoint ACT AS an ECU: offer services via SD, answer RPC
    # requests with canned responses, and publish events / fields. This is
    # the foundation for building external ECU simulators.
    #
    # RPC handlers run in the opensomeip receive thread inside the exporter
    # process, so they cannot call back to the Jumpstarter client per
    # request. Instead the client configures a canned response per method
    # (set_method_response) and the in-process handler serves it. Getter-
    # style SOME/IP methods (identity + status fields exposed by an ECU) map
    # cleanly onto this model; update the response to change what a client
    # of the simulated ECU reads.
    # =====================================================================

    def _build_server_config(self) -> ServerConfig:
        transport_upper = self.transport_mode.upper()
        mode = TransportMode.TCP if transport_upper == "TCP" else TransportMode.UDP
        return ServerConfig(
            local_endpoint=Endpoint(self.host, self.port),
            sd_config=SdConfig(
                multicast_endpoint=Endpoint(self.multicast_group, self.multicast_port),
                unicast_endpoint=Endpoint(self.host, self.port),
            ),
            transport_mode=mode,
            multicast_group=self.multicast_group if mode == TransportMode.UDP else None,
        )

    def _ensure_server(self) -> OsipServer:
        """Create and start the OsipServer on first use (thread-safe)."""
        if self._osip_server is None:
            with self._server_lock:
                if self._osip_server is None:
                    server = OsipServer(self._build_server_config())
                    server.start()
                    self._osip_server = server
        return self._osip_server

    def _make_method_handler(self, service_id: int, method_id: int):
        """Build an RPC handler that replies with the currently-configured
        canned response for (service_id, method_id).

        The handler is registered once per method; it reads _method_responses
        live on each call so set_method_response updates take effect without
        re-registration.
        """
        key = (service_id, method_id)

        def handler(request: Message) -> Message:
            payload, return_code = self._method_responses.get(key, (b"", int(ReturnCode.E_OK)))
            rc = ReturnCode(return_code) if return_code in ReturnCode._value2member_map_ else ReturnCode.E_NOT_OK
            return Message(
                message_id=MessageId(service_id, method_id),
                request_id=request.request_id,
                message_type=MessageType.RESPONSE if rc == ReturnCode.E_OK else MessageType.ERROR,
                return_code=rc,
                interface_version=request.interface_version,
                payload=payload,
            )

        return handler

    @export
    @validate_call(validate_return=True)
    def start_server(self) -> None:
        """Force-start the SOME/IP server (otherwise started on first offer)."""
        self._ensure_server()

    @export
    @validate_call(validate_return=True)
    def offer_service(
        self,
        service_id: int,
        instance_id: int = 0x0001,
        major_version: int = 1,
        minor_version: int = 0,
    ) -> None:
        """Offer a service instance for discovery (acts as the providing ECU)."""
        if instance_id in _RESERVED_INSTANCE_IDS:
            raise ValueError(
                f"Invalid instance_id 0x{instance_id:04X}: 0x0000 is reserved and "
                "0xFFFF means 'all instances'; neither can be offered."
            )
        service = ServiceInstance(
            service_id=service_id,
            instance_id=instance_id,
            major_version=major_version,
            minor_version=minor_version,
        )
        self._ensure_server().offer(service)

    @export
    @validate_call(validate_return=True)
    def stop_offer_service(
        self,
        service_id: int,
        instance_id: int = 0x0001,
        major_version: int = 1,
        minor_version: int = 0,
    ) -> None:
        """Withdraw a previously offered service instance."""
        if self._osip_server is None:
            return
        service = ServiceInstance(
            service_id=service_id,
            instance_id=instance_id,
            major_version=major_version,
            minor_version=minor_version,
        )
        self._osip_server.stop_offer(service)

    @export
    @validate_call(validate_return=True)
    def list_offered_services(self) -> list[SomeIpOfferedService]:
        """Return the set of services this server currently offers."""
        if self._osip_server is None:
            return []
        result: list[SomeIpOfferedService] = []
        for svc in self._osip_server.offered_services:
            result.append(
                SomeIpOfferedService(
                    service_id=svc.service_id,
                    instance_id=svc.instance_id,
                    major_version=svc.major_version,
                    minor_version=svc.minor_version,
                )
            )
        return result

    @export
    @validate_call(validate_return=True)
    def set_method_response(
        self,
        service_id: int,
        method_id: int,
        payload: SomeIpPayload,
        return_code: int = 0,
    ) -> None:
        """Configure the canned response the server returns for an RPC method.

        Registers a handler for (service_id, method_id) on first call and
        stores the response; subsequent calls just update the stored value.
        """
        key = (service_id, method_id)
        self._method_responses[key] = (bytes.fromhex(payload.data), return_code)
        server = self._ensure_server()
        if key not in self._registered_methods:
            server.register_method(MessageId(service_id, method_id), self._make_method_handler(service_id, method_id))
            self._registered_methods.add(key)

    @export
    @validate_call(validate_return=True)
    def clear_method_response(self, service_id: int, method_id: int) -> None:
        """Remove a configured RPC response.

        The handler stays registered (opensomeip has no unregister); it falls
        back to an empty E_OK reply until reconfigured.
        """
        self._method_responses.pop((service_id, method_id), None)

    @export
    @validate_call(validate_return=True)
    def register_event(self, service_id: int, event_id: int, eventgroup_id: int) -> None:
        """Register an event for publishing under an event group."""
        server = self._ensure_server()
        key = (service_id, event_id)
        if self._registered_events.get(key) != eventgroup_id:
            server.register_event(event_id, eventgroup_id)
            self._registered_events[key] = eventgroup_id

    @export
    @validate_call(validate_return=True)
    def publish_event(self, service_id: int, event_id: int, payload: SomeIpPayload) -> None:
        """Publish an event notification to subscribers of its event group.

        ``service_id`` is accepted for API symmetry with the other server
        verbs but is currently unused: opensomeip addresses events by
        ``event_id`` only (one server endpoint per driver instance).
        """
        self._ensure_server().publish_event(event_id, bytes.fromhex(payload.data))

    @export
    @validate_call(validate_return=True)
    def set_field(self, service_id: int, event_id: int, payload: SomeIpPayload) -> None:
        """Set a field event value (served to new subscribers and notified).

        ``service_id`` is accepted for API symmetry with the other server
        verbs but is currently unused: opensomeip addresses fields by
        ``event_id`` only (one server endpoint per driver instance).
        """
        self._ensure_server().set_field(event_id, bytes.fromhex(payload.data))

    @export
    @validate_call(validate_return=True)
    def stop_server(self) -> None:
        """Stop the SOME/IP server, withdrawing all offers.

        All server state is discarded: canned method responses, registered
        methods, and registered events must be reconfigured after a restart.
        """
        if self._osip_server is not None:
            try:
                self._osip_server.stop()
            except Exception:
                logger.warning("failed to stop opensomeip server", exc_info=True)
            self._osip_server = None
            self._method_responses.clear()
            self._registered_methods.clear()
            self._registered_events.clear()
