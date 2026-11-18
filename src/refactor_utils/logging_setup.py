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
    def __init__(self, inner: logging.Logger) -> None:
        self._inner = inner

    def _log(
        self, level: int, message: object, args: tuple, exc_info: bool = False
    ) -> None:
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

    def add(
        self, sink, level: str = "DEBUG", rotation=None, enqueue=False, format=None
    ):
        if isinstance(sink, (str, bytes)) or hasattr(sink, "__fspath__"):
            handler: logging.Handler = logging.FileHandler(sink, encoding="utf-8")
        else:
            handler = logging.StreamHandler(sink)
        handler.setLevel(_LEVELS.get(str(level).upper(), logging.DEBUG))
        handler.setFormatter(logging.Formatter(format or _FMT, datefmt=_DATEFMT))
        self._inner.addHandler(handler)
        return handler

    def set_level(self, level: str) -> None:
        self._inner.setLevel(_LEVELS.get(str(level).upper(), logging.INFO))


logger = _ShimLogger(_ROOT_LOGGER)


def setup_logging(level: str = "INFO", log_file=None) -> None:
    logger.remove()
    try:
        sys.stderr.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    except Exception:
        pass
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter(_FMT, datefmt=_DATEFMT))
    requested = _LEVELS.get(str(level).upper(), logging.INFO)
    # The stream handler enforces the user-requested level; the logger itself
    # must sit at or below TRACE (5) so low-level records reach the handler.
    # The previous code capped it at DEBUG=10, which silently dropped every
    # TRACE message (level 5) before it ever reached the handler.
    stream.setLevel(requested)
    logger._inner.setLevel(min(requested, 5))
    logger._inner.addHandler(stream)
    if log_file is not None:
        logger.add(log_file, level="DEBUG")
