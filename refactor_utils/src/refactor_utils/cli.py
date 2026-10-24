"""Command line interface: ``pyrefactor``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from loguru import logger

from . import __version__
from .core import ConsolidationError, consolidate
from .logging_setup import setup_logging


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pyrefactor",
        description="Consolidate a python module/package/project into a single, dependency-ordered file.",
    )
    p.add_argument("source", type=Path, help="package directory, project root or single .py file")
    p.add_argument("-o", "--output", type=Path, default=Path("output"), help="output directory (default: ./output)")
    p.add_argument("-m", "--mode", choices=["single"], default="single",
                   help="single: output/(__init__.py, _consolidated.py)")
    p.add_argument("-j", "--workers", type=int, default=None, help="parallel workers (default: CPU count)")
    p.add_argument("--executor", choices=["process", "thread"], default="process")
    p.add_argument("-x", "--exclude", action="append", default=[], metavar="GLOB",
                   help="extra path glob to skip (repeatable)")
    p.add_argument("--entry", action="append", default=[], metavar="MODULE",
                   help="only keep the __main__ guard of this module (repeatable)")
    p.add_argument("--keep-unused-imports", action="store_true", help="do not drop unused third-party/stdlib imports")
    p.add_argument("--respect-all", action="store_true",
                   help="restrict exported names to those listed in the original __all__ declarations")
    p.add_argument("--strict", action="store_true", help="exit non-zero if any file had to be skipped")
    p.add_argument("--log-level", default="INFO", choices=["TRACE", "DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--log-file", type=Path, default=None)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level, args.log_file)
    try:
        result = consolidate(
            args.source, args.output, mode=args.mode, workers=args.workers, executor=args.executor,
            exclude=tuple(args.exclude), keep_unused_imports=args.keep_unused_imports,
            respect_all=args.respect_all, entry=tuple(args.entry),
        )
    except ConsolidationError as exc:
        logger.error("{}", exc)
        return 2
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected failure")
        return 1
    logger.success("{} modules -> {} ({} exported names)", result.modules, result.output_dir, result.exports)
    if result.skipped:
        logger.error("{} file(s) skipped because of errors", len(result.skipped))
        if args.strict:
            return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
