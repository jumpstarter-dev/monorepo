"""Tests for LogHandler and log source routing."""

import logging
from collections import deque
from unittest.mock import MagicMock

import pytest

from jumpstarter.common import LogSource
from jumpstarter.exporter.logging import LogHandler, get_logger


class TestLogHandler:
    def test_init_with_default_source(self) -> None:
        """Test that LogHandler initializes with the correct default source."""
        queue = deque()
        handler = LogHandler(queue)

        assert handler.queue is queue
        assert handler.source == LogSource.UNSPECIFIED

    def test_init_with_specified_source(self) -> None:
        """Test that LogHandler initializes with a specified source."""
        queue = deque()
        handler = LogHandler(queue, source=LogSource.DRIVER)

        assert handler.source == LogSource.DRIVER

    def test_emit_appends_to_queue(self) -> None:
        """Test that emit() adds LogStreamResponse to the queue."""
        queue = deque()
        handler = LogHandler(queue, source=LogSource.SYSTEM)

        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test message",
            args=(),
            exc_info=None,
        )

        handler.emit(record)

        assert len(queue) == 1
        response = queue[0]
        assert response.message == "Test message"
        assert response.severity == "INFO"
        assert response.source == LogSource.SYSTEM.value

    def test_prepare_creates_log_stream_response(self) -> None:
        """Test that prepare() creates a LogStreamResponse with correct fields."""
        queue = deque()
        handler = LogHandler(queue, source=LogSource.DRIVER)

        record = logging.LogRecord(
            name="driver.test",
            level=logging.WARNING,
            pathname="driver.py",
            lineno=42,
            msg="Warning: %s",
            args=("something",),
            exc_info=None,
        )

        response = handler.prepare(record)

        assert response.message == "Warning: something"
        assert response.severity == "WARNING"
        assert response.source == LogSource.DRIVER.value
        assert response.uuid == ""

    def test_add_child_handler(self) -> None:
        """Test that add_child_handler() registers logger-to-source mapping."""
        queue = deque()
        handler = LogHandler(queue, source=LogSource.SYSTEM)

        handler.add_child_handler("hook.before", LogSource.BEFORE_LEASE_HOOK)

        assert "hook.before" in handler._child_handlers
        assert handler._child_handlers["hook.before"] == LogSource.BEFORE_LEASE_HOOK

    def test_remove_child_handler(self) -> None:
        """Test that remove_child_handler() removes the mapping."""
        queue = deque()
        handler = LogHandler(queue, source=LogSource.SYSTEM)

        handler.add_child_handler("hook.before", LogSource.BEFORE_LEASE_HOOK)
        handler.remove_child_handler("hook.before")

        assert "hook.before" not in handler._child_handlers

    def test_remove_child_handler_nonexistent_no_error(self) -> None:
        """Test that remove_child_handler() handles nonexistent loggers gracefully."""
        queue = deque()
        handler = LogHandler(queue, source=LogSource.SYSTEM)

        # Should not raise an error
        handler.remove_child_handler("nonexistent.logger")

    def test_get_source_for_record_default(self) -> None:
        """Test that get_source_for_record() returns default source for unmapped loggers."""
        queue = deque()
        handler = LogHandler(queue, source=LogSource.SYSTEM)

        record = logging.LogRecord(
            name="random.logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test",
            args=(),
            exc_info=None,
        )

        source = handler.get_source_for_record(record)

        assert source == LogSource.SYSTEM

    def test_get_source_for_record_child_mapping(self) -> None:
        """Test that get_source_for_record() uses child handler mapping."""
        queue = deque()
        handler = LogHandler(queue, source=LogSource.SYSTEM)
        handler.add_child_handler("hook.before", LogSource.BEFORE_LEASE_HOOK)

        record = logging.LogRecord(
            name="hook.before.subprocess",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test",
            args=(),
            exc_info=None,
        )

        source = handler.get_source_for_record(record)

        assert source == LogSource.BEFORE_LEASE_HOOK

    def test_get_source_for_record_exact_match(self) -> None:
        """Test that get_source_for_record() works with exact logger name match."""
        queue = deque()
        handler = LogHandler(queue, source=LogSource.SYSTEM)
        handler.add_child_handler("hook.after", LogSource.AFTER_LEASE_HOOK)

        record = logging.LogRecord(
            name="hook.after",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test",
            args=(),
            exc_info=None,
        )

        source = handler.get_source_for_record(record)

        assert source == LogSource.AFTER_LEASE_HOOK

    def test_emit_different_log_levels(self) -> None:
        """Test that emit() preserves different log levels."""
        queue = deque()
        handler = LogHandler(queue, source=LogSource.SYSTEM)

        levels = [
            (logging.DEBUG, "DEBUG"),
            (logging.INFO, "INFO"),
            (logging.WARNING, "WARNING"),
            (logging.ERROR, "ERROR"),
            (logging.CRITICAL, "CRITICAL"),
        ]

        for level, expected_severity in levels:
            record = logging.LogRecord(
                name="test.logger",
                level=level,
                pathname="test.py",
                lineno=1,
                msg=f"{expected_severity} message",
                args=(),
                exc_info=None,
            )
            handler.emit(record)

        assert len(queue) == 5
        for i, (_, expected_severity) in enumerate(levels):
            assert queue[i].severity == expected_severity


class TestLogHandlerContextManager:
    def test_context_log_source_adds_mapping(self) -> None:
        """Test that context_log_source() adds mapping within context."""
        queue = deque()
        handler = LogHandler(queue, source=LogSource.SYSTEM)

        with handler.context_log_source("hook.before", LogSource.BEFORE_LEASE_HOOK):
            assert "hook.before" in handler._child_handlers
            assert handler._child_handlers["hook.before"] == LogSource.BEFORE_LEASE_HOOK

    def test_context_log_source_removes_on_exit(self) -> None:
        """Test that context_log_source() removes mapping on context exit."""
        queue = deque()
        handler = LogHandler(queue, source=LogSource.SYSTEM)

        with handler.context_log_source("hook.before", LogSource.BEFORE_LEASE_HOOK):
            pass

        assert "hook.before" not in handler._child_handlers

    def test_context_log_source_handles_exception(self) -> None:
        """Test that context_log_source() removes mapping even on exception."""
        queue = deque()
        handler = LogHandler(queue, source=LogSource.SYSTEM)

        with pytest.raises(ValueError):
            with handler.context_log_source("hook.before", LogSource.BEFORE_LEASE_HOOK):
                assert "hook.before" in handler._child_handlers
                raise ValueError("Test exception")

        assert "hook.before" not in handler._child_handlers

    def test_context_log_source_routes_logs_correctly(self) -> None:
        """Test that logs are routed correctly within context."""
        queue = deque()
        handler = LogHandler(queue, source=LogSource.SYSTEM)

        with handler.context_log_source("hook.before", LogSource.BEFORE_LEASE_HOOK):
            record = logging.LogRecord(
                name="hook.before.script",
                level=logging.INFO,
                pathname="hook.py",
                lineno=1,
                msg="Hook output",
                args=(),
                exc_info=None,
            )
            handler.emit(record)

        assert len(queue) == 1
        assert queue[0].source == LogSource.BEFORE_LEASE_HOOK.value


class TestGetLogger:
    def test_get_logger_returns_logger(self) -> None:
        """Test that get_logger() returns a Python logger."""
        logger = get_logger("test.module")

        assert isinstance(logger, logging.Logger)
        assert logger.name == "test.module"

    def test_get_logger_registers_with_session(self) -> None:
        """Test that get_logger() registers source with session when provided."""
        mock_session = MagicMock()

        logger = get_logger("driver.test", source=LogSource.DRIVER, session=mock_session)

        mock_session.add_logger_source.assert_called_once_with("driver.test", LogSource.DRIVER)
        assert isinstance(logger, logging.Logger)

    def test_get_logger_without_session(self) -> None:
        """Test that get_logger() works without session."""
        logger = get_logger("standalone.logger", source=LogSource.SYSTEM)

        assert isinstance(logger, logging.Logger)
        # Should not raise any error
