"""Log redaction (spec §13): bearer tokens never reach log output.

Uvicorn's access and WebSocket lines include the full path, and the browser
can only authenticate WebSockets with ``?token=…``; the filter scrubs tokens
from both the message and its ``%``-arguments (uvicorn's access formatter
reads the arguments directly, so they must keep their shape).
"""

import copy
import logging
import re

_PATTERNS = [
    (re.compile(r"(token=)[^&\s\"']+", re.I), r"\1[REDACTED]"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.I), r"\1[REDACTED]"),
    (re.compile(r"\bpo_[A-Za-z0-9_-]{16,}"), "po_[REDACTED]"),
]


def redact(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {
                k: redact(v) if isinstance(v, str) else v for k, v in record.args.items()
            }
        return True


def log_config() -> dict:
    """Uvicorn's default logging config with the redacting filter on every handler."""
    from uvicorn.config import LOGGING_CONFIG

    config = copy.deepcopy(LOGGING_CONFIG)
    config.setdefault("filters", {})["redact"] = {"()": RedactingFilter}
    for handler in config["handlers"].values():
        handler.setdefault("filters", []).append("redact")
    config.setdefault("root", {"handlers": ["default"], "level": "INFO"})
    return config
