# Structured JSON logs (design section 7, "No patient content anywhere in the telemetry").
#
# Each line is one JSON object: timestamp, level, logger, the event name (the log message),
# and every structured field passed via `extra`. Callers pass only opaque IDs, statuses,
# fixed enum values, hashes, timestamps and durations; patient_id appears only in the
# identity_conflict line (section 1).
#
# Exceptions are reduced to their type. The message and traceback are never written, since
# exception text can carry request data (a database error's DETAIL, a provider's response).
# This applies to every logger, including uvicorn's "Exception in ASGI application".
import json
import logging
import sys
from datetime import datetime, timezone

_RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        line = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                line[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            line["error_type"] = record.exc_info[0].__name__
        return json.dumps(line, default=str)


_HANDLER_MARK = "_json_handler"


def configure_logging(service: str, level: int = logging.INFO) -> None:
    """Idempotent: installs one JSON handler on the root logger and routes uvicorn's loggers
    through it instead of their own plain-text handlers."""
    root = logging.getLogger()
    if not any(getattr(h, _HANDLER_MARK, False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter(service))
        setattr(handler, _HANDLER_MARK, True)
        root.addHandler(handler)
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
    # One INFO line per provider request is noise; the worker logs its own outcome per attempt
    logging.getLogger("httpx").setLevel(logging.WARNING)
