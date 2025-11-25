"""LeaseScope: Context manager for lease-related resources.

This module provides a clean abstraction for managing the lifecycle of resources
associated with a lease, including the session, socket path, and synchronization events.
"""

from dataclasses import dataclass, field

from anyio import Event

from jumpstarter.exporter.session import Session


@dataclass
class LeaseContext:
    """Encapsulates all resources associated with an active lease.

    This class bundles together the session, socket path, synchronization event,
    and lease identity information that are needed throughout the lease lifecycle.
    By grouping these resources, we make their relationships and lifecycles explicit.

    Attributes:
        lease_name: Name of the current lease assigned by the controller
        session: The Session object managing the device and gRPC services (set in handle_lease)
        socket_path: Unix socket path where the session is serving (set in handle_lease)
        before_lease_hook: Event that signals when before-lease hook completes
        client_name: Name of the client currently holding the lease (empty if unleased)
    """

    lease_name: str
    before_lease_hook: Event
    session: Session | None = None
    socket_path: str = ""
    client_name: str = field(default="")

    def __post_init__(self):
        """Validate that required resources are present."""
        assert self.before_lease_hook is not None, "LeaseScope requires a before_lease_hook event"
        assert self.lease_name, "LeaseScope requires a non-empty lease_name"

    def is_ready(self) -> bool:
        """Check if the lease scope has been fully initialized with session and socket.

        Note: This checks for resource initialization (session/socket), not lease activity.
        Use is_active() to check if the lease itself is active.
        """
        return self.session is not None and self.socket_path != ""

    def is_active(self) -> bool:
        """Check if this lease is active (has a non-empty lease name)."""
        return bool(self.lease_name)

    def has_client(self) -> bool:
        """Check if a client is currently holding the lease."""
        return bool(self.client_name)

    def update_client(self, client_name: str):
        """Update the client name for this lease."""
        self.client_name = client_name

    def clear_client(self):
        """Clear the client name when the lease is no longer held."""
        self.client_name = ""
