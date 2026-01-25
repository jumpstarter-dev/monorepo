"""Human-readable enum wrappers for protobuf-generated constants."""

from enum import IntEnum

from jumpstarter_protocol.jumpstarter.v1 import common_pb2


class ExporterStatus(IntEnum):
    """Exporter status states."""

    UNSPECIFIED = common_pb2.EXPORTER_STATUS_UNSPECIFIED
    """Unknown/unspecified exporter status"""

    OFFLINE = common_pb2.EXPORTER_STATUS_OFFLINE
    """The exporter is currently offline"""

    AVAILABLE = common_pb2.EXPORTER_STATUS_AVAILABLE
    """Exporter is available to be leased"""

    BEFORE_LEASE_HOOK = common_pb2.EXPORTER_STATUS_BEFORE_LEASE_HOOK
    """Exporter is leased, but currently executing before lease hook"""

    LEASE_READY = common_pb2.EXPORTER_STATUS_LEASE_READY
    """Exporter is leased and ready to accept commands"""

    AFTER_LEASE_HOOK = common_pb2.EXPORTER_STATUS_AFTER_LEASE_HOOK
    """Lease was released, but exporter is executing after lease hook"""

    BEFORE_LEASE_HOOK_FAILED = common_pb2.EXPORTER_STATUS_BEFORE_LEASE_HOOK_FAILED
    """The before lease hook failed and the exporter is no longer available"""

    AFTER_LEASE_HOOK_FAILED = common_pb2.EXPORTER_STATUS_AFTER_LEASE_HOOK_FAILED
    """The after lease hook failed and the exporter is no longer available"""

    def __str__(self):
        return self.name

    @classmethod
    def from_proto(cls, value: int) -> "ExporterStatus":
        """Convert from protobuf integer to enum."""
        return cls(value)

    def to_proto(self) -> int:
        """Convert to protobuf integer."""
        return self.value


class LogSource(IntEnum):
    """Log source types."""

    UNSPECIFIED = common_pb2.LOG_SOURCE_UNSPECIFIED
    """Unspecified/unknown log source"""

    DRIVER = common_pb2.LOG_SOURCE_DRIVER
    """Logs produced by a Jumpstarter driver"""

    BEFORE_LEASE_HOOK = common_pb2.LOG_SOURCE_BEFORE_LEASE_HOOK
    """Logs produced by a before lease hook"""

    AFTER_LEASE_HOOK = common_pb2.LOG_SOURCE_AFTER_LEASE_HOOK
    """Logs produced by an after lease hook"""

    SYSTEM = common_pb2.LOG_SOURCE_SYSTEM
    """System/exporter logs"""

    def __str__(self):
        return self.name

    @classmethod
    def from_proto(cls, value: int) -> "LogSource":
        """Convert from protobuf integer to enum."""
        return cls(value)

    def to_proto(self) -> int:
        """Convert to protobuf integer."""
        return self.value
