"""Loguru configuration shared by the CLI and the library entry points."""

from __future__ import annotations
import sys
from pathlib import Path
from loguru import logger

_FMT = "<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>"


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    """Configure loguru output for the current process.

    Args:
        level: Minimum level written to stderr (case insensitive).
        log_file: Optional file to additionally log to at ``DEBUG`` level with
            5 MB rotation.

    """
    logger.remove()
    logger.add(sys.stderr, level=level.upper(), format=_FMT)
    if log_file is not None:
        logger.add(log_file, level="DEBUG", rotation="5 MB", enqueue=True)
