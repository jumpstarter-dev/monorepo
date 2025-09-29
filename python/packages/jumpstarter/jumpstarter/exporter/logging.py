import logging
from collections import deque
from contextlib import contextmanager
from threading import RLock
from typing import TYPE_CHECKING

from jumpstarter_protocol import jumpstarter_pb2

from jumpstarter.common import LogSource

if TYPE_CHECKING:
    from .session import Session


class LogHandler(logging.Handler):
    def __init__(self, queue: deque, source: LogSource = LogSource.UNSPECIFIED):
        logging.Handler.__init__(self)
        self.queue = queue
        self.listener = None
        self.source = source  # LogSource enum value
        self._lock = RLock()
        self._child_handlers = {}  # Dict of logger_name -> LogSource mappings

    def add_child_handler(self, logger_name: str, source: LogSource):
        """Add a child handler that will route logs from a specific logger with a different source."""
        with self._lock:
            self._child_handlers[logger_name] = source

    def remove_child_handler(self, logger_name: str):
        """Remove a child handler mapping."""
        with self._lock:
            self._child_handlers.pop(logger_name, None)

    def get_source_for_record(self, record):
        """Determine the appropriate log source for a record."""
        with self._lock:
            # Check if this record comes from a logger with a specific source mapping
            logger_name = record.name
            for mapped_logger, source in self._child_handlers.items():
                if logger_name.startswith(mapped_logger):
                    return source
            return self.source

    def enqueue(self, record):
        self.queue.append(record)

    def prepare(self, record):
        source = self.get_source_for_record(record)
        return jumpstarter_pb2.LogStreamResponse(
            uuid="",
            severity=record.levelname,
            message=self.format(record),
            source=source.value,  # Convert to proto value
        )

    def emit(self, record):
        try:
            self.enqueue(self.prepare(record))
        except Exception:
            self.handleError(record)

    @contextmanager
    def context_log_source(self, logger_name: str, source: LogSource):
        """Context manager to temporarily set a log source for a specific logger."""
        self.add_child_handler(logger_name, source)
        try:
            yield
        finally:
            self.remove_child_handler(logger_name)


def get_logger(name: str, source: LogSource = LogSource.SYSTEM, session: "Session" = None) -> logging.Logger:
    """
    Get a logger with automatic LogSource mapping.

    Args:
        name: Logger name (e.g., __name__ or custom name)
        source: The LogSource to associate with this logger
        session: Optional session to register with immediately

    Returns:
        A standard Python logger instance
    """
    logger = logging.getLogger(name)

    # If session provided, register the source mapping
    if session:
        session.add_logger_source(name, source)

    return logger
