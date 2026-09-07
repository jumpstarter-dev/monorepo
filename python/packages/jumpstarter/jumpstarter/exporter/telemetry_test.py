"""Unit tests for TelemetryLogHandler."""

import logging
from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog.contextvars

from jumpstarter.exporter.telemetry import (
    _MAX_EXTRA_FIELDS,
    _MAX_KEY_LEN,
    _MAX_QUEUE_SIZE,
    _MAX_VAL_LEN,
    TelemetryLogHandler,
    _severity,
)


def make_record(
    msg: str = "test message",
    level: int = logging.INFO,
    name: str = "test.logger",
    **extra,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name=name,
        level=level,
        pathname="test.py",
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for k, v in extra.items():
        setattr(record, k, v)
    return record


def make_handler(namespace: str = "", token: str = "", component: str = "exporter") -> TelemetryLogHandler:
    stub = MagicMock()
    stub.PushLogs = AsyncMock()
    return TelemetryLogHandler(stub, namespace=namespace, token=token, component=component)


@pytest.fixture(autouse=False)
def clean_structlog_context():
    """Ensure structlog contextvars are empty before and after each test that uses them."""
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()



class TestSeverityHelper:
    @pytest.mark.parametrize(
        "levelname,expected",
        [
            ("DEBUG", "debug"),
            ("INFO", "info"),
            ("WARNING", "warning"),
            ("ERROR", "error"),
            ("CRITICAL", "critical"),
            ("TRACE", "trace"),  # unknown → lower-cased
        ],
    )
    def test_maps_known_levels(self, levelname, expected):
        assert _severity(levelname) == expected



class TestPrepare:
    def test_basic_fields(self):
        handler = make_handler()
        record = make_record("hello", logging.WARNING)
        entry = handler.prepare(record)

        assert entry.message == "hello"
        assert entry.severity == "warning"
        assert entry.component == "exporter"

    def test_cli_component(self):
        handler = make_handler(component="cli")
        entry = handler.prepare(make_record("from jmp"))
        assert entry.component == "cli"

    def test_timestamp_is_set(self):
        handler = make_handler()
        entry = handler.prepare(make_record())
        assert entry.timestamp.seconds > 0

    def test_well_known_correlation_fields_extracted(self):
        handler = make_handler()
        record = make_record(
            lease="lease-42",
            client="ci-client",
            exporter="lab-exporter",
            operation="flash",
            result="success",
            driver_type="storage",
        )
        entry = handler.prepare(record)

        assert entry.lease == "lease-42"
        assert entry.client == "ci-client"
        assert entry.exporter == "lab-exporter"
        assert entry.operation == "flash"
        assert entry.result == "success"
        assert entry.driver_type == "storage"

    def test_well_known_fields_not_in_extra_fields(self):
        handler = make_handler()
        record = make_record(lease="lease-42", driver_type="power")
        entry = handler.prepare(record)

        assert "lease" not in entry.extra_fields
        assert "driver_type" not in entry.extra_fields

    def test_custom_fields_go_to_extra_fields(self):
        handler = make_handler()
        record = make_record(build_id="nightly-42", pipeline="ci-main")
        entry = handler.prepare(record)

        assert entry.extra_fields["build_id"] == "nightly-42"
        assert entry.extra_fields["pipeline"] == "ci-main"

    def test_extra_fields_key_truncated(self):
        handler = make_handler()
        long_key = "k" * (_MAX_KEY_LEN + 10)
        record = make_record()
        setattr(record, long_key, "value")
        entry = handler.prepare(record)

        for k in entry.extra_fields:
            assert len(k) <= _MAX_KEY_LEN, f"key {k!r} exceeds max length"

    def test_extra_fields_value_truncated(self):
        handler = make_handler()
        record = make_record(my_field="x" * (_MAX_VAL_LEN + 100))
        entry = handler.prepare(record)

        assert "my_field" in entry.extra_fields
        assert len(entry.extra_fields["my_field"]) == _MAX_VAL_LEN

    def test_extra_fields_count_capped(self):
        handler = make_handler()
        record = make_record()
        for i in range(_MAX_EXTRA_FIELDS + 5):
            setattr(record, f"custom_field_{i}", f"val_{i}")
        entry = handler.prepare(record)

        assert len(entry.extra_fields) <= _MAX_EXTRA_FIELDS

    def test_none_values_not_in_extra_fields(self):
        handler = make_handler()
        record = make_record()
        record.nullable_field = None
        entry = handler.prepare(record)

        assert "nullable_field" not in entry.extra_fields

    def test_stdlib_attrs_not_in_extra_fields(self):
        """Standard LogRecord attributes (name, lineno, etc.) must not appear in extra_fields."""
        handler = make_handler()
        entry = handler.prepare(make_record())

        stdlib_names = {"name", "lineno", "pathname", "levelname", "created", "msg"}
        for key in stdlib_names:
            assert key not in entry.extra_fields, f"stdlib attr {key!r} leaked into extra_fields"

    def test_namespace_from_handler_set_on_entry(self):
        """namespace passed to TelemetryLogHandler is stamped on every LogEntry."""
        handler = make_handler(namespace="my-namespace")
        entry = handler.prepare(make_record())
        assert entry.namespace == "my-namespace"

    def test_namespace_empty_by_default(self):
        """When no namespace is passed the field is empty; the telemetry service fills it from the token."""
        handler = make_handler()
        entry = handler.prepare(make_record())
        assert entry.namespace == ""

    def test_structlog_contextvars_lease_id_maps_to_lease_field(self, clean_structlog_context):
        """lease_id in structlog context maps to the proto lease field, not extra_fields."""
        structlog.contextvars.bind_contextvars(lease_id="ctx-lease-99", exporter="ctx-exporter")
        entry = make_handler().prepare(make_record())
        assert entry.lease == "ctx-lease-99"
        assert entry.exporter == "ctx-exporter"

    def test_structlog_contextvars_spec_context_in_extra_fields(self, clean_structlog_context):
        """spec.context fields like build_id and pipeline end up in extra_fields."""
        structlog.contextvars.bind_contextvars(build_id="nightly-99", pipeline="ci-branch")
        entry = make_handler().prepare(make_record())
        assert entry.extra_fields["build_id"] == "nightly-99"
        assert entry.extra_fields["pipeline"] == "ci-branch"

    def test_record_attribute_overrides_contextvars(self, clean_structlog_context):
        """A field set explicitly on the LogRecord takes priority over structlog contextvars."""
        structlog.contextvars.bind_contextvars(exporter="ctx-exporter", lease_id="ctx-lease")
        entry = make_handler().prepare(make_record(exporter="record-exporter"))
        assert entry.exporter == "record-exporter"
        assert entry.lease == "ctx-lease"

    def test_lease_id_not_in_extra_fields(self, clean_structlog_context):
        """lease_id must not leak into extra_fields — it is promoted to entry.lease."""
        structlog.contextvars.bind_contextvars(lease_id="lease-abc")
        entry = make_handler().prepare(make_record())
        assert "lease_id" not in entry.extra_fields
        assert entry.lease == "lease-abc"

    def test_none_contextvar_values_skipped(self, clean_structlog_context):
        """None values in structlog context must not appear in the entry."""
        structlog.contextvars.bind_contextvars(exporter=None, lease_id=None, build_id=None)
        entry = make_handler().prepare(make_record())
        assert entry.exporter == ""
        assert entry.lease == ""
        assert "build_id" not in entry.extra_fields



class TestEmit:
    def test_emit_adds_to_queue(self):
        handler = make_handler()
        handler.emit(make_record("queued message"))
        assert len(handler._queue) == 1
        assert handler._queue[0].message == "queued message"

    def test_queue_max_size_drops_oldest(self):
        handler = make_handler()
        for i in range(_MAX_QUEUE_SIZE + 5):
            handler.emit(make_record(f"msg-{i}"))
        assert len(handler._queue) == _MAX_QUEUE_SIZE
        assert handler._queue[-1].message == f"msg-{_MAX_QUEUE_SIZE + 4}"

    def test_emit_handles_prepare_error_gracefully(self):
        """emit() must not raise even when prepare() fails."""
        handler = make_handler()
        with patch.object(handler, "prepare", side_effect=RuntimeError("oops")):
            handler.emit(make_record())
        assert len(handler._queue) == 0



class TestFlush:
    @pytest.mark.anyio
    async def test_flush_sends_bearer_token_in_metadata(self):
        handler = make_handler(token="my-jwt")
        handler.emit(make_record("hello"))
        await handler._flush()

        _, kwargs = handler._stub.PushLogs.call_args
        assert ("authorization", "Bearer my-jwt") in kwargs["metadata"]

    @pytest.mark.anyio
    async def test_flush_no_metadata_when_token_empty(self):
        handler = make_handler(token="")
        handler.emit(make_record("hello"))
        await handler._flush()

        _, kwargs = handler._stub.PushLogs.call_args
        assert kwargs["metadata"] == []

    @pytest.mark.anyio
    async def test_flush_sends_batch_to_stub(self):
        handler = make_handler()
        handler.emit(make_record("a"))
        handler.emit(make_record("b"))

        await handler._flush()

        handler._stub.PushLogs.assert_awaited_once()
        call_args = handler._stub.PushLogs.call_args[0][0]
        assert len(call_args.entries) == 2
        assert handler._queue == deque()

    @pytest.mark.anyio
    async def test_flush_empty_queue_does_not_call_stub(self):
        handler = make_handler()
        await handler._flush()
        handler._stub.PushLogs.assert_not_awaited()

    @pytest.mark.anyio
    async def test_flush_error_does_not_raise(self):
        """If the stub raises, _flush must not propagate the exception."""
        handler = make_handler()
        handler._stub.PushLogs.side_effect = Exception("network error")
        handler.emit(make_record("drop me"))
        await handler._flush()

    @pytest.mark.anyio
    async def test_close_async_flushes_remaining(self):
        handler = make_handler()
        handler.emit(make_record("final entry"))
        await handler.close_async()
        handler._stub.PushLogs.assert_awaited_once()

    @pytest.mark.anyio
    async def test_flush_loop_flushes_when_queue_has_entries(self):
        """flush_loop calls _flush when the queue is non-empty."""
        handler = make_handler()
        handler.emit(make_record("pending"))

        flush_calls = []

        async def fake_flush():
            flush_calls.append(1)
            handler._queue.clear()

        with patch.object(handler, "_flush", side_effect=fake_flush):
            with patch("jumpstarter.exporter.telemetry.sleep", new_callable=AsyncMock) as mock_sleep:
                mock_sleep.side_effect = [None, Exception("stop")]
                try:
                    await handler.flush_loop()
                except Exception:
                    pass

        assert len(flush_calls) >= 1

    @pytest.mark.anyio
    async def test_flush_respects_batch_size_limit(self):
        from jumpstarter.exporter.telemetry import _BATCH_SIZE

        handler = make_handler()
        for i in range(_BATCH_SIZE + 10):
            handler.emit(make_record(f"msg-{i}"))

        await handler._flush()

        call_args = handler._stub.PushLogs.call_args[0][0]
        assert len(call_args.entries) == _BATCH_SIZE
        assert len(handler._queue) == 10
