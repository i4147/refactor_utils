"""loguru configuration."""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

_FMT = "<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>"


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    logger.remove()
    logger.add(sys.stderr, level=level.upper(), format=_FMT)
    if log_file is not None:
        logger.add(log_file, level="DEBUG", rotation="5 MB", enqueue=True)
