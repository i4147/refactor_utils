"""Logging setup.

Replaces the former ``loguru`` dependency with a thin stdlib-``logging``
shim that keeps the same call surface used across the package
(``logger.info("x {}", val)``, ``logger.success(...)``, ``logger.exception``).
"""
from __future__ import annotations

import logging
import sys

_FMT = "%(asctime)s | %(levelname)-8s | %(message)s"
_DATEFMT = "%H:%M:%S"

_LEVELS = {
    "TRACE": 5,
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "SUCCESS": logging.INFO + 5,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}
logging.addLevelName(5, "TRACE")
logging.addLevelName(logging.INFO + 5, "SUCCESS")

_ROOT_LOGGER = logging.getLogger("pyrefactor")
_ROOT_LOGGER.addHandler(logging.NullHandler())


def _fmt_message(message: object, args: tuple) -> str:
    if not args:
        return str(message)
    try:
        return str(message).format(*args)
    except Exception:
        try:
            return str(message) + " " + " ".join(str(a) for a in args)
        except Exception:
            return str(message)


class _ShimLogger:
    """Small stand-in for the loguru logger API on top of stdlib logging."""

    def __init__(self, inner: logging.Logger) -> None:
        self._inner = inner

    def _log(self, level: int, message: object, args: tuple, exc_info: bool = False) -> None:
        self._inner.log(level, _fmt_message(message, args), exc_info=exc_info)

    def trace(self, message: object, *args: object) -> None:
        self._log(5, message, args)

    def debug(self, message: object, *args: object) -> None:
        self._log(logging.DEBUG, message, args)

    def info(self, message: object, *args: object) -> None:
        self._log(logging.INFO, message, args)

    def success(self, message: object, *args: object) -> None:
        self._log(logging.INFO + 5, message, args)

    def warning(self, message: object, *args: object) -> None:
        self._log(logging.WARNING, message, args)

    def error(self, message: object, *args: object) -> None:
        self._log(logging.ERROR, message, args)

    def critical(self, message: object, *args: object) -> None:
        self._log(logging.CRITICAL, message, args)

    def exception(self, message: object, *args: object) -> None:
        self._log(logging.ERROR, message, args, exc_info=True)

    def remove(self) -> None:
        for handler in list(self._inner.handlers):
            if not isinstance(handler, logging.NullHandler):
                self._inner.removeHandler(handler)

    def add(self, sink, level: str = "DEBUG", rotation=None, enqueue=False, format=None):
        """Register a sink (path-like or stream). Rotation/enqueue ignored."""
        if isinstance(sink, (str, bytes)) or hasattr(sink, "__fspath__"):
            handler: logging.Handler = logging.FileHandler(sink, encoding="utf-8")
        else:
            handler = logging.StreamHandler(sink)
        handler.setLevel(_LEVELS.get(str(level).upper(), logging.DEBUG))
        handler.setFormatter(
            logging.Formatter(format or _FMT, datefmt=_DATEFMT)
        )
        self._inner.addHandler(handler)
        return handler

    def set_level(self, level: str) -> None:
        self._inner.setLevel(_LEVELS.get(str(level).upper(), logging.INFO))


logger = _ShimLogger(_ROOT_LOGGER)


def setup_logging(level: str = "INFO", log_file=None) -> None:
    """(Re)configure handlers: colored stderr + optional log file."""
    logger.remove()
    try:
        sys.stderr.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    except Exception:
        pass
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter(_FMT, datefmt=_DATEFMT))
    stream.setLevel(_LEVELS.get(str(level).upper(), logging.INFO))
    logger._inner.setLevel(min(logging.DEBUG, _LEVELS.get(str(level).upper(), logging.INFO)))
    logger._inner.addHandler(stream)
    if log_file is not None:
        logger.add(log_file, level="DEBUG")
