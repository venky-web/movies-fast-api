"""Logging for the `app.*` loggers, with the request id on every line.

Uvicorn configures only its own loggers, so without this every `app.*` INFO line (the audit log, for example)
is dropped. Uvicorn's loggers, including the access log, are left untouched.
"""

import logging
from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

HANDLER_NAME = "app-console"
LOG_FORMAT = "%(levelname)-8s [%(request_id)s] %(name)s: %(message)s"


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # An explicit `extra={"request_id": ...}` wins: the 500 handler runs after the context var was reset.
        record.request_id = getattr(record, "request_id", None) or request_id_var.get() or "-"
        return True


class OneLineFormatter(logging.Formatter):
    """Messages contain client input such as paths, and `%0A` in a path must not start a fake log line."""

    def formatMessage(self, record: logging.LogRecord) -> str:
        return super().formatMessage(record).replace("\r", "\\r").replace("\n", "\\n")


def configure_logging() -> None:
    """Called at every startup. The handler is recreated each time, so it writes to the current stderr."""
    logger = logging.getLogger("app")
    logger.setLevel(logging.INFO)
    for handler in list(logger.handlers):
        if handler.get_name() == HANDLER_NAME:
            logger.removeHandler(handler)
    console = logging.StreamHandler()
    console.set_name(HANDLER_NAME)
    console.addFilter(RequestIdFilter())
    console.setFormatter(OneLineFormatter(LOG_FORMAT))
    logger.addHandler(console)
