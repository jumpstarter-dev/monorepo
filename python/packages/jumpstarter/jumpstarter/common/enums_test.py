"""Tests for enum conversions and string representations."""

from jumpstarter_protocol.jumpstarter.v1 import common_pb2

from jumpstarter.common.enums import ExporterStatus, LogSource


class TestExporterStatus:
    def test_from_proto_all_values(self) -> None:
        """Test that all proto values convert correctly to ExporterStatus."""
        # Test each status value
        assert (
            ExporterStatus.from_proto(common_pb2.EXPORTER_STATUS_UNSPECIFIED)
            == ExporterStatus.UNSPECIFIED
        )
        assert (
            ExporterStatus.from_proto(common_pb2.EXPORTER_STATUS_OFFLINE)
            == ExporterStatus.OFFLINE
        )
        assert (
            ExporterStatus.from_proto(common_pb2.EXPORTER_STATUS_AVAILABLE)
            == ExporterStatus.AVAILABLE
        )
        assert (
            ExporterStatus.from_proto(common_pb2.EXPORTER_STATUS_BEFORE_LEASE_HOOK)
            == ExporterStatus.BEFORE_LEASE_HOOK
        )
        assert (
            ExporterStatus.from_proto(common_pb2.EXPORTER_STATUS_LEASE_READY)
            == ExporterStatus.LEASE_READY
        )
        assert (
            ExporterStatus.from_proto(common_pb2.EXPORTER_STATUS_AFTER_LEASE_HOOK)
            == ExporterStatus.AFTER_LEASE_HOOK
        )
        assert (
            ExporterStatus.from_proto(common_pb2.EXPORTER_STATUS_BEFORE_LEASE_HOOK_FAILED)
            == ExporterStatus.BEFORE_LEASE_HOOK_FAILED
        )
        assert (
            ExporterStatus.from_proto(common_pb2.EXPORTER_STATUS_AFTER_LEASE_HOOK_FAILED)
            == ExporterStatus.AFTER_LEASE_HOOK_FAILED
        )

    def test_to_proto_all_values(self) -> None:
        """Test that all ExporterStatus values convert correctly to proto."""
        assert (
            ExporterStatus.UNSPECIFIED.to_proto()
            == common_pb2.EXPORTER_STATUS_UNSPECIFIED
        )
        assert (
            ExporterStatus.OFFLINE.to_proto()
            == common_pb2.EXPORTER_STATUS_OFFLINE
        )
        assert (
            ExporterStatus.AVAILABLE.to_proto()
            == common_pb2.EXPORTER_STATUS_AVAILABLE
        )
        assert (
            ExporterStatus.BEFORE_LEASE_HOOK.to_proto()
            == common_pb2.EXPORTER_STATUS_BEFORE_LEASE_HOOK
        )
        assert (
            ExporterStatus.LEASE_READY.to_proto()
            == common_pb2.EXPORTER_STATUS_LEASE_READY
        )
        assert (
            ExporterStatus.AFTER_LEASE_HOOK.to_proto()
            == common_pb2.EXPORTER_STATUS_AFTER_LEASE_HOOK
        )
        assert (
            ExporterStatus.BEFORE_LEASE_HOOK_FAILED.to_proto()
            == common_pb2.EXPORTER_STATUS_BEFORE_LEASE_HOOK_FAILED
        )
        assert (
            ExporterStatus.AFTER_LEASE_HOOK_FAILED.to_proto()
            == common_pb2.EXPORTER_STATUS_AFTER_LEASE_HOOK_FAILED
        )

    def test_str_representation(self) -> None:
        """Test that __str__ returns the enum name."""
        assert str(ExporterStatus.UNSPECIFIED) == "UNSPECIFIED"
        assert str(ExporterStatus.OFFLINE) == "OFFLINE"
        assert str(ExporterStatus.AVAILABLE) == "AVAILABLE"
        assert str(ExporterStatus.BEFORE_LEASE_HOOK) == "BEFORE_LEASE_HOOK"
        assert str(ExporterStatus.LEASE_READY) == "LEASE_READY"
        assert str(ExporterStatus.AFTER_LEASE_HOOK) == "AFTER_LEASE_HOOK"
        assert str(ExporterStatus.BEFORE_LEASE_HOOK_FAILED) == "BEFORE_LEASE_HOOK_FAILED"
        assert str(ExporterStatus.AFTER_LEASE_HOOK_FAILED) == "AFTER_LEASE_HOOK_FAILED"

    def test_roundtrip_conversion(self) -> None:
        """Test that from_proto(to_proto()) returns the same value."""
        for status in ExporterStatus:
            assert ExporterStatus.from_proto(status.to_proto()) == status


class TestLogSource:
    def test_from_proto_all_values(self) -> None:
        """Test that all proto values convert correctly to LogSource."""
        assert (
            LogSource.from_proto(common_pb2.LOG_SOURCE_UNSPECIFIED)
            == LogSource.UNSPECIFIED
        )
        assert (
            LogSource.from_proto(common_pb2.LOG_SOURCE_DRIVER)
            == LogSource.DRIVER
        )
        assert (
            LogSource.from_proto(common_pb2.LOG_SOURCE_BEFORE_LEASE_HOOK)
            == LogSource.BEFORE_LEASE_HOOK
        )
        assert (
            LogSource.from_proto(common_pb2.LOG_SOURCE_AFTER_LEASE_HOOK)
            == LogSource.AFTER_LEASE_HOOK
        )
        assert (
            LogSource.from_proto(common_pb2.LOG_SOURCE_SYSTEM)
            == LogSource.SYSTEM
        )

    def test_to_proto_all_values(self) -> None:
        """Test that all LogSource values convert correctly to proto."""
        assert (
            LogSource.UNSPECIFIED.to_proto()
            == common_pb2.LOG_SOURCE_UNSPECIFIED
        )
        assert (
            LogSource.DRIVER.to_proto()
            == common_pb2.LOG_SOURCE_DRIVER
        )
        assert (
            LogSource.BEFORE_LEASE_HOOK.to_proto()
            == common_pb2.LOG_SOURCE_BEFORE_LEASE_HOOK
        )
        assert (
            LogSource.AFTER_LEASE_HOOK.to_proto()
            == common_pb2.LOG_SOURCE_AFTER_LEASE_HOOK
        )
        assert (
            LogSource.SYSTEM.to_proto()
            == common_pb2.LOG_SOURCE_SYSTEM
        )

    def test_str_representation(self) -> None:
        """Test that __str__ returns the enum name."""
        assert str(LogSource.UNSPECIFIED) == "UNSPECIFIED"
        assert str(LogSource.DRIVER) == "DRIVER"
        assert str(LogSource.BEFORE_LEASE_HOOK) == "BEFORE_LEASE_HOOK"
        assert str(LogSource.AFTER_LEASE_HOOK) == "AFTER_LEASE_HOOK"
        assert str(LogSource.SYSTEM) == "SYSTEM"

    def test_roundtrip_conversion(self) -> None:
        """Test that from_proto(to_proto()) returns the same value."""
        for source in LogSource:
            assert LogSource.from_proto(source.to_proto()) == source
