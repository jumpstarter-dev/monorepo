"""Telemetry log handler — batches Python log records and pushes them to jumpstarter-telemetry."""

import logging
import sys
from collections import deque
from datetime import datetime, timezone

import structlog.contextvars
from anyio import sleep
from google.protobuf.timestamp_pb2 import Timestamp
from jumpstarter_protocol import telemetry_pb2, telemetry_pb2_grpc

# Batch up to 50 entries per PushLogs call — small enough to stay well under
# default gRPC message size limits, large enough to amortize per-RPC overhead.
_BATCH_SIZE = 50
# Flush twice per second so log entries arrive at the sink with low latency
# without spinning the event loop.
_FLUSH_INTERVAL = 0.5
# Cap the in-process queue so a slow or unreachable telemetry service can never
# grow memory without bound; oldest entries are evicted when the limit is hit.
_MAX_QUEUE_SIZE = 10_000
# Give up on a PushLogs call after 10 s — telemetry is best-effort and must
# not stall the flush loop or the shutdown path.
_PUSH_TIMEOUT = 10.0

_WELL_KNOWN_KEYS = frozenset(("lease", "client", "exporter", "operation", "result", "driver_type"))
_STDLIB_RECORD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys())

# Map structlog context var names to proto field names where they differ.
_CONTEXT_KEY_MAP = {"lease_id": "lease"}
# Context keys that map to first-class proto fields (skip from extra_fields).
_CONTEXT_PROTO_KEYS = _WELL_KNOWN_KEYS | frozenset(_CONTEXT_KEY_MAP.keys())

# JEP-0013 limits for extra_fields: at most 16 pairs, keys ≤ 64 chars,
# values ≤ 256 chars. These match the limits enforced by the telemetry service.
_MAX_EXTRA_FIELDS = 16
_MAX_KEY_LEN = 64
_MAX_VAL_LEN = 256


def _severity(levelname: str) -> str:
    """Normalise Python logging level names to JEP-0013 severity strings."""
    return {
        "DEBUG": "debug",
        "INFO": "info",
        "WARNING": "warning",
        "ERROR": "error",
        "CRITICAL": "critical",
    }.get(levelname, levelname.lower())


class TelemetryLogHandler(logging.Handler):
    """Logging handler that batches records and pushes them to the telemetry service.

    Records are queued in-process and flushed every ``_FLUSH_INTERVAL`` seconds or
    when the batch reaches ``_BATCH_SIZE`` entries. If the telemetry service is
    unreachable, entries are silently dropped — device operations must never block
    on telemetry availability.
    """

    def __init__(
        self,
        stub: telemetry_pb2_grpc.TelemetryServiceStub,
        namespace: str = "",
        token: str = "",
        component: str = "exporter",
    ) -> None:
        super().__init__()
        self._stub = stub
        self._namespace = namespace
        self._token = token
        self.component = component
        self._queue: deque[telemetry_pb2.LogEntry] = deque(maxlen=_MAX_QUEUE_SIZE)

    def prepare(self, record: logging.LogRecord) -> telemetry_pb2.LogEntry:
        """Convert a LogRecord to a LogEntry proto.

        Correlation fields are drawn from two sources (record wins on conflict):
        1. structlog contextvars — set via set_log_context(lease_id=..., build_id=...)
        2. LogRecord extra attributes — set via logger.info(..., extra={...})
        """
        ts = Timestamp()
        ts.FromDatetime(datetime.fromtimestamp(record.created, tz=timezone.utc))

        entry = telemetry_pb2.LogEntry(
            timestamp=ts,
            severity=_severity(record.levelname),
            message=record.getMessage(),
            component=self.component,
            namespace=self._namespace,
        )

        extra: dict[str, str] = {}

        # Pull structlog contextvars first (lowest priority).
        # lease_id -> entry.lease; all others go directly or to extra_fields.
        ctx = structlog.contextvars.get_contextvars()
        for ctx_key, ctx_val in ctx.items():
            if ctx_val is None:
                continue
            proto_key = _CONTEXT_KEY_MAP.get(ctx_key, ctx_key)
            if proto_key in _WELL_KNOWN_KEYS:
                setattr(entry, proto_key, str(ctx_val))
            elif ctx_key not in _CONTEXT_PROTO_KEYS and len(extra) < _MAX_EXTRA_FIELDS:
                extra[ctx_key[:_MAX_KEY_LEN]] = str(ctx_val)[:_MAX_VAL_LEN]

        # Overlay with explicit LogRecord attributes (highest priority).
        # Well-known keys and anything that isn't a stdlib attribute.
        for key, val in record.__dict__.items():
            if key in _STDLIB_RECORD_ATTRS or val is None:
                continue
            proto_key = _CONTEXT_KEY_MAP.get(key, key)
            if proto_key in _WELL_KNOWN_KEYS:
                setattr(entry, proto_key, str(val))
            elif key not in _CONTEXT_PROTO_KEYS and len(extra) < _MAX_EXTRA_FIELDS:
                extra[key[:_MAX_KEY_LEN]] = str(val)[:_MAX_VAL_LEN]

        if extra:
            entry.extra_fields.update(extra)

        return entry

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._queue.append(self.prepare(record))
        except Exception:
            self.handleError(record)

    async def flush_loop(self) -> None:
        """Background task: flush batches to the telemetry service periodically."""
        while True:
            await sleep(_FLUSH_INTERVAL)
            if self._queue:
                await self._flush()

    async def _flush(self) -> None:
        """Drain up to _BATCH_SIZE entries from the queue and push to telemetry."""
        batch: list[telemetry_pb2.LogEntry] = []
        while self._queue and len(batch) < _BATCH_SIZE:
            batch.append(self._queue.popleft())

        if not batch:
            return

        metadata = [("authorization", f"Bearer {self._token}")] if self._token else []
        try:
            await self._stub.PushLogs(
                telemetry_pb2.PushLogsRequest(entries=batch),
                timeout=_PUSH_TIMEOUT,
                metadata=metadata,
            )
        except Exception as exc:
            # Avoid recursive logging: write directly to stderr.
            print(f"[telemetry] PushLogs failed, {len(batch)} entries dropped: {exc}", file=sys.stderr)

    async def close_async(self) -> None:
        """Flush all remaining entries and close the handler."""
        while self._queue:
            await self._flush()
        self.close()
