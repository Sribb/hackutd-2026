"""Keep tokens out of the server log (#126 verification round 2).

/ws takes its token in the query string (?token=...), and uvicorn logs every request
with its full path: HTTP requests on the "uvicorn.access" logger, WebSocket handshakes
("WebSocket /ws?role=web&token=..." [accepted] or 403) on "uvicorn.error". So under the
documented run command a valid operator or phone token would land in the log in clear
text. security_log.py strips query strings from its own events; this does the same for
uvicorn's.

install() puts a filter on those loggers that replaces the value of every query
parameter named like a secret (token, key, secret, password, auth, ...) with "***",
in the message and in each of its arguments. The rest of the line is kept, so the log
still shows who connected and how it went. uvicorn configures its loggers before it
imports the app and never removes a filter, so installing at import time is enough.
"""
from __future__ import annotations

import logging
import re

LOGGERS = ("uvicorn.access", "uvicorn.error", "uvicorn")
MASK = "***"
# name=value in a query string, for any name that holds a secret
SECRET_PARAM = re.compile(
    r"(?i)(?P<name>(?:^|(?<=[?&;\s\"']))[\w.-]*(?:token|secret|password|passwd|pwd|api[_-]?key|key|auth|session|sig)"
    r"[\w.-]*=)(?P<value>[^&\s\"'#]*)")


def redact(text: str) -> str:
    """text with the value of every secret-looking query parameter masked."""
    if not isinstance(text, str) or "=" not in text:
        return text
    return SECRET_PARAM.sub(lambda m: m.group("name") + (MASK if m.group("value") else ""), text)


class RedactTokens(logging.Filter):
    """Masks secret query parameters in a record's message and arguments. Never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: redact(v) if isinstance(v, str) else v for k, v in record.args.items()}
        except Exception:  # noqa: BLE001 - logging must never fail because of this
            pass
        return True


_FILTER = RedactTokens()


def install() -> None:
    """Add the filter to uvicorn's loggers, once."""
    for name in LOGGERS:
        logger = logging.getLogger(name)
        if _FILTER not in logger.filters:
            logger.addFilter(_FILTER)
