from __future__ import annotations

from dataclasses import dataclass

from .common import (
    SomeIpEventNotification,
    SomeIpMessageResponse,
    SomeIpOfferedService,
    SomeIpPayload,
    SomeIpServiceEntry,
)
from jumpstarter.client import DriverClient


@dataclass(kw_only=True)
class SomeIpDriverClient(DriverClient):
    """Client interface for SOME/IP operations.

    Provides methods for RPC calls, raw messaging, service discovery,
    and event subscriptions over the Jumpstarter remoting layer.
    """

    def start(self) -> None:
        """Force start the SOME/IP client."""
        self.call("start")

    # --- RPC ---

    def rpc_call(
        self,
        service_id: int,
        method_id: int,
        payload: bytes,
        timeout: float = 5.0,
    ) -> SomeIpMessageResponse:
        """Make a SOME/IP RPC call and return the response."""
        msg = SomeIpPayload(data=payload.hex())
        return SomeIpMessageResponse.model_validate(
            self.call("rpc_call", service_id, method_id, msg, timeout)
        )

    # --- Raw Messaging ---

    def send_message(
        self,
        service_id: int,
        method_id: int,
        payload: bytes,
    ) -> None:
        """Send a raw SOME/IP message."""
        msg = SomeIpPayload(data=payload.hex())
        self.call("send_message", service_id, method_id, msg)

    def receive_message(self, timeout: float = 2.0) -> SomeIpMessageResponse:
        """Receive a raw SOME/IP message."""
        return SomeIpMessageResponse.model_validate(
            self.call("receive_message", timeout)
        )

    # --- Service Discovery ---

    def find_service(
        self,
        service_id: int,
        instance_id: int = 0xFFFF,
        timeout: float = 5.0,
    ) -> list[SomeIpServiceEntry]:
        """Find services via SOME/IP-SD."""
        result = self.call("find_service", service_id, instance_id, timeout)
        return [SomeIpServiceEntry.model_validate(v) for v in result]

    # --- Events ---

    def subscribe_eventgroup(self, eventgroup_id: int) -> None:
        """Subscribe to a SOME/IP event group."""
        self.call("subscribe_eventgroup", eventgroup_id)

    def unsubscribe_eventgroup(self, eventgroup_id: int) -> None:
        """Unsubscribe from a SOME/IP event group."""
        self.call("unsubscribe_eventgroup", eventgroup_id)

    def receive_event(self, timeout: float = 5.0) -> SomeIpEventNotification:
        """Receive the next event notification."""
        return SomeIpEventNotification.model_validate(
            self.call("receive_event", timeout)
        )

    # --- Connection Management ---

    def close_connection(self) -> None:
        """Close the SOME/IP connection."""
        self.call("close_connection")

    def reconnect(self) -> None:
        """Reconnect to the SOME/IP endpoint."""
        self.call("reconnect")

    # --- Server / provider side ---

    def start_server(self) -> None:
        """Force-start the SOME/IP server (otherwise started on first offer)."""
        self.call("start_server")

    def offer_service(
        self,
        service_id: int,
        instance_id: int = 0x0001,
        major_version: int = 1,
        minor_version: int = 0,
    ) -> None:
        """Offer a service instance for discovery (act as the providing ECU)."""
        self.call("offer_service", service_id, instance_id, major_version, minor_version)

    def stop_offer_service(
        self,
        service_id: int,
        instance_id: int = 0x0001,
        major_version: int = 1,
        minor_version: int = 0,
    ) -> None:
        """Withdraw a previously offered service instance."""
        self.call("stop_offer_service", service_id, instance_id, major_version, minor_version)

    def list_offered_services(self) -> list[SomeIpOfferedService]:
        """Return the set of services this server currently offers."""
        result = self.call("list_offered_services")
        return [SomeIpOfferedService.model_validate(v) for v in result]

    def set_method_response(
        self,
        service_id: int,
        method_id: int,
        payload: bytes,
        return_code: int = 0,
    ) -> None:
        """Configure the canned response the server returns for an RPC method."""
        msg = SomeIpPayload(data=payload.hex())
        self.call("set_method_response", service_id, method_id, msg, return_code)

    def clear_method_response(self, service_id: int, method_id: int) -> None:
        """Remove a configured RPC response."""
        self.call("clear_method_response", service_id, method_id)

    def register_event(self, service_id: int, event_id: int, eventgroup_id: int) -> None:
        """Register an event for publishing under an event group."""
        self.call("register_event", service_id, event_id, eventgroup_id)

    def publish_event(self, service_id: int, event_id: int, payload: bytes) -> None:
        """Publish an event notification to subscribers of its event group.

        ``service_id`` is accepted for API symmetry but currently unused;
        events are addressed by ``event_id`` only.
        """
        msg = SomeIpPayload(data=payload.hex())
        self.call("publish_event", service_id, event_id, msg)

    def set_field(self, service_id: int, event_id: int, payload: bytes) -> None:
        """Set a field event value (served to new subscribers and notified).

        ``service_id`` is accepted for API symmetry but currently unused;
        fields are addressed by ``event_id`` only.
        """
        msg = SomeIpPayload(data=payload.hex())
        self.call("set_field", service_id, event_id, msg)

    def stop_server(self) -> None:
        """Stop the SOME/IP server, withdrawing all offers."""
        self.call("stop_server")
