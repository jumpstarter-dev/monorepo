"""Protocol for logger registration to avoid circular dependencies."""

from typing import Protocol

from jumpstarter.common import LogSource


class LoggerRegistration(Protocol):
    """Protocol for objects that can register logger sources.

    This protocol defines the interface for objects that can associate
    logger names with log sources, enabling proper routing of log messages.
    """

    def add_logger_source(self, logger_name: str, source: LogSource) -> None:
        """Register a logger name with its corresponding log source.

        Args:
            logger_name: Name of the logger to register
            source: The log source category for this logger
        """
        ...
