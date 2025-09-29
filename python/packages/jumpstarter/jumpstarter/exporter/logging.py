import logging
from collections import deque

from jumpstarter_protocol import jumpstarter_pb2

from jumpstarter.common import LogSource


class LogHandler(logging.Handler):
    def __init__(self, queue: deque, source: LogSource = LogSource.UNSPECIFIED):
        logging.Handler.__init__(self)
        self.queue = queue
        self.listener = None
        self.source = source  # LogSource enum value

    def enqueue(self, record):
        self.queue.append(record)

    def prepare(self, record):
        return jumpstarter_pb2.LogStreamResponse(
            uuid="",
            severity=record.levelname,
            message=self.format(record),
            source=self.source.value,  # Convert to proto value
        )

    def emit(self, record):
        try:
            self.enqueue(self.prepare(record))
        except Exception:
            self.handleError(record)
