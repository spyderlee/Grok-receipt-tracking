"""Secret redaction for everything the bot writes to stdout/stderr/journald (audit H1).

python-telegram-bot puts the bot token in request URLs and in some exception texts
(e.g. InvalidToken / NetworkError tracebacks from Application.initialize()). We never
want those in journald, so:

* every root handler gets a RedactingFilter (rewrites msg/args/exc_text/stack_info) and
  a RedactingFormatter (scrubs the final formatted line, tracebacks included);
* uncaught exceptions (sys.excepthook, threading.excepthook, sys.unraisablehook for errors in
  __del__/finalizers) are routed through logging instead of being printed raw (audit N8);
* a LogRecord factory redacts every record when it is CREATED, so loggers with their own
  handlers and propagate=False (which never reach the root handlers) are covered too (N8);
* besides the exact configured secrets, anything shaped like a Telegram bot token
  (``<digits>:<35 url-safe chars>``) or a ``/bot<token>/`` URL segment is masked.
"""
from __future__ import annotations

import logging
import re
import sys
import threading
from typing import Iterable, Optional, Set
from urllib.parse import quote

REDACTED = "[REDACTED]"
_TOKEN_SHAPE = re.compile(r"(?<![\w-])\d{5,15}:[A-Za-z0-9_-]{25,}")
_BOT_URL = re.compile(r"(/bot)[^/\s'\"<>]+")


class Redactor:
    def __init__(self) -> None:
        self._secrets: Set[str] = set()

    def add(self, *values: Optional[str]) -> None:
        for v in values:
            if not v or len(v) < 6:  # too short to mask without mangling ordinary text
                continue
            for form in (v, quote(v, safe=""), quote(v)):
                self._secrets.add(form)

    def clear(self) -> None:
        self._secrets.clear()

    def __call__(self, text: str) -> str:
        if not text:
            return text
        for s in sorted(self._secrets, key=len, reverse=True):
            if s in text:
                text = text.replace(s, REDACTED)
        text = _BOT_URL.sub(r"\1" + REDACTED, text)
        return _TOKEN_SHAPE.sub(REDACTED, text)


REDACTOR = Redactor()


def _redact_record(record: logging.LogRecord) -> logging.LogRecord:
    try:
        msg = record.getMessage()
    except Exception:  # bad format args - still log something useful
        msg = f"{record.msg!r} % {record.args!r}"
    record.msg, record.args = REDACTOR(msg), None
    if record.exc_info and not record.exc_text:
        # Formatter.format() uses a pre-set exc_text instead of re-formatting exc_info.
        record.exc_text = logging.Formatter().formatException(record.exc_info)
    if record.exc_text:
        record.exc_text = REDACTOR(record.exc_text)
    if record.stack_info:
        record.stack_info = REDACTOR(record.stack_info)
    return record


_base_factory = logging.getLogRecordFactory()


def _redacting_factory(*args, **kwargs) -> logging.LogRecord:
    return _redact_record(_base_factory(*args, **kwargs))


_redacting_factory._maidbot = True  # type: ignore[attr-defined]


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        _redact_record(record)
        return True


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return REDACTOR(super().format(record))


FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def install(secrets: Iterable[Optional[str]] = (), level: str = "INFO") -> None:
    """Configure root logging with redaction. Safe to call more than once."""
    REDACTOR.add(*secrets)
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=level.upper(), format=FORMAT)
    else:
        root.setLevel(level.upper())
    for handler in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())
        if not isinstance(handler.formatter, RedactingFormatter):
            handler.setFormatter(RedactingFormatter(FORMAT))
    logging.captureWarnings(True)
    global _base_factory
    current = logging.getLogRecordFactory()
    if not getattr(current, "_maidbot", False):
        _base_factory = current
        logging.setLogRecordFactory(_redacting_factory)
    # Belt and braces for handlers attached directly to named loggers (propagate=False).
    for lg in [logging.getLogger(n) for n in list(logging.Logger.manager.loggerDict)]:
        if isinstance(lg, logging.Logger):
            for handler in lg.handlers:
                if not any(isinstance(f, RedactingFilter) for f in handler.filters):
                    handler.addFilter(RedactingFilter())
    # Request URLs contain the token: keep the HTTP client loggers quiet even at DEBUG.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)

    def excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logging.getLogger("maid_receipt_bot").critical("Uncaught exception", exc_info=(exc_type, exc, tb))

    def thread_hook(args):
        logging.getLogger("maid_receipt_bot").critical(
            "Uncaught exception in thread %s", getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    def unraisable_hook(u):
        logging.getLogger("maid_receipt_bot").error(
            "Unraisable exception (%s) in %r", getattr(u, "err_msg", None) or "Exception ignored",
            getattr(u, "object", None),
            exc_info=(u.exc_type, u.exc_value, u.exc_traceback) if u.exc_type else None)

    sys.excepthook = excepthook
    threading.excepthook = thread_hook
    sys.unraisablehook = unraisable_hook


def scrub(text: str) -> str:
    return REDACTOR(text)
