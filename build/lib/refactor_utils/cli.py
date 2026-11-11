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
        description=(
            "Consolidate a python module/package/project into a single, "
            "dependency-ordered file."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument(
        "source", type=Path, help="package directory, project root or single .py file"
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("output"),
        help="output directory (default: ./output)",
    )
    p.add_argument(
        "--name",
        default=None,
        metavar="NAME",
        help=(
            "name of the generated file\n"
            "(default: the current directory name, e.g. output/<cwd>.py)"
        ),
    )
    # Parallelism
    p.add_argument(
        "-j",
        "--workers",
        type=int,
        default=None,
        help="parallel workers (default: CPU count)",
    )
    p.add_argument(
        "--executor",
        choices=["process", "thread"],
        default="process",
        help="executor type for parallel parsing (default: process)",
    )
    p.add_argument(
        "--progress", action="store_true", help="show progress bar during parsing"
    )

    # Discovery / filtering
    p.add_argument(
        "-x",
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help=(
            "extra path glob to skip (repeatable)\n"
            "precedence: default excludes < --include < --exclude"
        ),
    )
    p.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="GLOB",
        help="glob to re-include files that would otherwise be excluded",
    )
    p.add_argument(
        "--copy-data",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="copy data files (json, yaml, csv, txt, ...) to output (default: on)",
    )
    p.add_argument(
        "--data-glob",
        action="append",
        default=[],
        metavar="GLOB",
        help="only copy data files matching this glob (repeatable)",
    )

    # Entry / main handling
    p.add_argument(
        "--entry",
        action="append",
        default=[],
        metavar="MODULE",
        help="only keep the __main__ guard of this module (repeatable)",
    )
    p.add_argument(
        "--entry-keep-all",
        action="store_true",
        help="keep all __main__ guards instead of filtering to --entry",
    )
    p.add_argument(
        "--main-name",
        type=str,
        default=None,
        metavar="NAME",
        help="rename `main` function to NAME (e.g., --main-name cli_main)",
    )
    p.add_argument(
        "--generate-guard",
        action="store_true",
        help='auto-generate `if __name__ == "__main__": main()` if no guard exists',
    )

    # Imports
    p.add_argument(
        "--imports",
        choices=["prune", "keep", "strict"],
        default="prune",
        help=(
            "import handling: prune (default, drop unused), keep (keep all),\n"
            "strict (prune and warn on unused)"
        ),
    )

    # Output content
    p.add_argument(
        "--keep-docstrings",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="keep docstrings (default: on)",
    )
    p.add_argument(
        "--keep-comments",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="keep comments (default: on)",
    )

    # Output control
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="show the plan and stats without writing files",
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="verify no changes are needed (fails if the output would change)",
    )
    p.add_argument(
        "--stdout",
        action="store_true",
        help="write consolidated output to stdout instead of files",
    )
    p.add_argument(
        "--diff",
        action="store_true",
        help="emit a unified diff against the existing output (if present)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="always write, even if the output directory already exists",
    )

    # Config / formatting
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="path to pyproject.toml containing a [tool.pyrefactor] section",
    )
    p.add_argument(
        "--format",
        action="store_true",
        help="run the configured formatter on the generated file",
    )
    p.add_argument(
        "--formatter",
        choices=["black", "ruff"],
        default="black",
        help="formatter used by --format (default: black)",
    )

    # Logging
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["TRACE", "DEBUG", "INFO", "WARNING", "ERROR"],
        help="log level (default: INFO)",
    )
    p.add_argument("--log-file", type=Path, default=None, help="also log to this file")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level, args.log_file)
    try:
        result = consolidate(
            args.source,
            args.output,
            filename=args.name,
            workers=args.workers,
            executor=args.executor,
            exclude=tuple(args.exclude),
            include=tuple(args.include),
            imports_mode=args.imports,
            entry=tuple(args.entry),
            dry_run=args.dry_run,
            check=args.check,
            stdout=args.stdout,
            config=args.config,
            do_format=args.format,
            formatter=args.formatter,
            diff=args.diff,
            force=args.force,
            keep_docstrings=args.keep_docstrings,
            strip_comments=not args.keep_comments,
            keep_comments=args.keep_comments,
            progress=args.progress,
            copy_data=args.copy_data,
            data_glob=tuple(args.data_glob),
            entry_keep_all=args.entry_keep_all,
            main_name=args.main_name,
            generate_guard=args.generate_guard,
        )
    except ConsolidationError as exc:
        logger.error("{}", exc)
        return 2
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected failure")
        return 1

    if result.dry_run:
        logger.info(
            "{}: {} modules, {} skipped, {} exports, {} warnings",
            "Check" if args.check else "Dry-run",
            result.modules,
            len(result.skipped),
            result.exports,
            len(result.warnings),
        )
        return 0

    if result.files:
        logger.success(
            "{} modules -> {} ({} exported names)",
            result.modules,
            result.output_dir,
            result.exports,
        )
    elif args.stdout:
        logger.success(
            "{} modules -> stdout ({} exported names)",
            result.modules,
            result.exports,
        )
    if result.copied_data_files:
        logger.info("Copied {} data file(s)", len(result.copied_data_files))
    if result.skipped:
        logger.error("{} file(s) skipped because of errors", len(result.skipped))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
