"""Command line interface for ``pyrefactor``.

Thin argument-parsing layer over :func:`refactor_utils.core.consolidate`; all
defaults and behaviour live in :mod:`refactor_utils.core` so the library API and
the CLI can never drift apart.
"""

from __future__ import annotations
import argparse
import sys
from pathlib import Path
from loguru import logger
from . import __version__
from .core import ConsolidationError, consolidate
from .logging_setup import setup_logging


def build_parser() -> argparse.ArgumentParser:
    """Build the ``pyrefactor`` argument parser.

    Every flag documents the module that implements it, which keeps the CLI
    help and the library options in sync.

    Returns:
        A fully configured :class:`argparse.ArgumentParser`.

    """
    p = argparse.ArgumentParser(
        prog="pyrefactor",
        description="Consolidate a python module/package/project into a single, dependency-ordered file.",
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
        "-m",
        "--mode",
        choices=["single", "package", "module", "flat", "split"],
        default="single",
        help="output layout: single (default: output/__init__.py+_consolidated.py), package (output/<pkg>/...), module (single file), flat (no section headers), split (const/class/func files) [core.py, writer.py] (non-breaking, default single)",
    )
    # Parallelism
    p.add_argument(
        "-j",
        "--workers",
        type=int,
        default=None,
        help="parallel workers (default: CPU count) [cli.py, core.py]",
    )
    p.add_argument(
        "--jobs", type=int, default=None, help="alias for --workers [cli.py]"
    )
    p.add_argument(
        "--executor",
        choices=["process", "thread"],
        default="process",
        help="executor type for parallel parsing (default: process) [core.py]",
    )
    p.add_argument(
        "--no-parallel",
        action="store_true",
        help="disable parallel parsing (workers=1) [core.py, discovery.py]",
    )
    p.add_argument(
        "--progress",
        action="store_true",
        help="show progress bar during parsing (requires tqdm) [core.py]",
    )
    p.add_argument("--progress-bar", action="store_true", help="alias for --progress")

    # Discovery / filtering
    p.add_argument(
        "-x",
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="extra path glob to skip (repeatable) [discovery.py] — precedence: default excludes < --include < --exclude",
    )
    p.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="GLOB",
        help="glob to re-include files that would otherwise be excluded; explicit --exclude always wins [discovery.py]",
    )
    p.add_argument(
        "--copy-data",
        action="store_true",
        default=True,
        help="copy data files (json, yaml, csv, txt, etc.) to output, preserving relative paths [discovery.py, core.py] (default: enabled, use --no-copy-data to disable)",
    )
    p.add_argument(
        "--no-copy-data",
        action="store_false",
        dest="copy_data",
        help="disable copying of data files [discovery.py]",
    )
    p.add_argument(
        "--data-glob",
        action="append",
        default=[],
        metavar="GLOB",
        help="only copy data files matching this glob (repeatable) [discovery.py]",
    )

    # Entry / main handling
    p.add_argument(
        "--entry",
        action="append",
        default=[],
        metavar="MODULE",
        help="only keep the __main__ guard of this module (repeatable) [resolver.py]; with --entry-keep-all, keep all guards",
    )
    p.add_argument(
        "--entry-keep-all",
        action="store_true",
        help="keep all __main__ guards instead of filtering to --entry list [resolver.py]",
    )
    p.add_argument(
        "--main-name",
        type=str,
        default=None,
        metavar="NAME",
        help="rename `main` function to NAME (e.g., --main-name cli_main) [resolver.py]",
    )
    p.add_argument(
        "--generate-guard",
        action="store_true",
        help='auto-generate `if __name__ == "__main__": main()` if no guard exists [resolver.py, writer.py]',
    )
    p.add_argument(
        "--keep-all-mains",
        action="store_true",
        help="alias for --entry-keep-all [resolver.py]",
    )

    # Imports / exports
    p.add_argument(
        "--keep-unused-imports",
        action="store_true",
        help="do not drop unused third-party/stdlib imports (deprecated, use --imports keep) [resolver.py]",
    )
    p.add_argument(
        "--imports",
        choices=["prune", "keep", "strict"],
        default="prune",
        help="import handling: prune (default, drop unused), keep (keep all), strict (warn/error on unused) [resolver.py, imports.py]",
    )
    p.add_argument(
        "--respect-all",
        action="store_true",
        help="restrict exported names to those listed in original __all__ (deprecated, use --all respect) [resolver.py]",
    )
    p.add_argument(
        "--all",
        dest="all_mode",
        choices=["ignore", "respect", "merge", "recompute"],
        default="ignore",
        help="__all__ handling: ignore (default, export all public), respect (only __all__), merge (union), recompute (sorted public) [resolver.py]",
    )

    # Renaming / dedupe
    p.add_argument(
        "--rename-strategy",
        choices=["prefix", "suffix", "module_path", "error"],
        default="prefix",
        help="collision policy for same-named objects: prefix (default: <module>_<name>), suffix (<name>_<module>), module_path (full path), error (fail) [resolver.py]",
    )
    p.add_argument(
        "--dedupe",
        choices=["none", "safe", "aggressive"],
        default="safe",
        help="duplicate-block removal: none (keep all), safe (default, identical class/func), aggressive (also const/misc, whitespace-insensitive); a post-rename pass also catches blocks that differ only by import aliasing [resolver.py]",
    )

    # Comments / docstrings / source maps
    p.add_argument(
        "--keep-docstrings",
        action="store_true",
        default=True,
        help="keep docstrings (default: keep) [parser.py]",
    )
    p.add_argument(
        "--strip-docstrings",
        action="store_false",
        dest="keep_docstrings",
        help="strip docstrings from output [parser.py]",
    )
    p.add_argument(
        "--keep-comments",
        action="store_true",
        default=True,
        help="keep comments (default: keep) [parser.py]",
    )
    p.add_argument(
        "--strip-comments",
        action="store_true",
        help="strip standalone comment lines (keeps shebang/encoding) [parser.py]",
    )
    p.add_argument(
        "--namespace",
        action="store_true",
        help="emit module-like namespace comments before each block [writer.py]",
    )
    p.add_argument(
        "--preserve-modules",
        action="store_true",
        help="emit origin comments mapping each block to its original module/file [writer.py]",
    )
    p.add_argument(
        "--source-map",
        action="store_true",
        help="inline comments mapping each block to original file:line range [writer.py, parser.py]",
    )

    # Output control
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="show the plan and stats without writing files (never writes) [core.py]",
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="verify no changes needed (like --dry-run but fails if diff not empty) [core.py]",
    )
    p.add_argument(
        "--stdout",
        action="store_true",
        help="write consolidated output to stdout instead of files [core.py]",
    )
    p.add_argument(
        "--diff",
        action="store_true",
        help="emit unified diff against existing output (if present) [core.py]",
    )
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="allow overwriting non-empty output directory [core.py]",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="alias for --overwrite (force overwrite) [core.py]",
    )

    # Config / formatting / validation
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="path to pyproject.toml containing [tool.pyrefactor] config (excludes, entry, formatting, strictness) [cli.py, core.py, discovery.py]",
    )
    p.add_argument(
        "--pyproject",
        action="store_true",
        help="enable pyproject.toml/setup.cfg awareness for package discovery (auto-read [tool.pyrefactor]) [discovery.py]",
    )
    p.add_argument(
        "--setup-cfg",
        action="store_true",
        help="alias for --pyproject (also check setup.cfg) [discovery.py]",
    )
    p.add_argument(
        "--format",
        action="store_true",
        help="run black/ruff format on generated files (requires black or ruff) [writer.py]",
    )
    p.add_argument(
        "--post-format", action="store_true", help="alias for --format [writer.py]"
    )
    p.add_argument(
        "--formatter",
        choices=["black", "ruff"],
        default="black",
        help="formatter to use with --format (default: black) [writer.py]",
    )
    p.add_argument(
        "--validate",
        action="store_true",
        help="compile output and optionally smoke-import it; fail if invalid [core.py]",
    )
    p.add_argument(
        "--validate-import",
        action="store_true",
        help="with --validate, also attempt to import the generated module [core.py]",
    )

    # Strictness / error handling
    p.add_argument(
        "--strict",
        action="store_true",
        help=(
            "exit non-zero if any file had to be skipped, on import-cycle "
            "warnings, or on warnings that mean the output may be wrong [core.py]"
        ),
    )
    p.add_argument(
        "--fail-on-warning",
        action="store_true",
        help="exit non-zero if any warning was emitted [core.py]",
    )
    p.add_argument(
        "--max-skipped",
        type=int,
        default=None,
        metavar="N",
        help="fail if more than N files were skipped (e.g., --max-skipped 0) [core.py]",
    )
    p.add_argument(
        "--allow-syntax-errors",
        action="store_true",
        help="try partial parse for files with syntax errors instead of skipping [parser.py, core.py]",
    )
    p.add_argument(
        "--eol",
        choices=["lf", "crlf", "preserve"],
        default="lf",
        help="line endings for generated files: lf (default), crlf, or preserve (match the input files) [core.py]",
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


def _resolve_progress(args) -> bool:
    """Resolve the ``--progress``/``--progress-bar`` aliases.

    Args:
        args: Parsed command line namespace.

    Returns:
        ``True`` when either progress flag was supplied.

    """
    # --progress and --progress-bar are aliases
    return bool(args.progress or getattr(args, "progress_bar", False))


def main(argv: list[str] | None = None) -> int:
    """Run the command line entry point.

    Args:
        argv: Argument list to parse; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code: ``0`` success, ``1`` unexpected failure or strictness
        violation, ``2`` for a :class:`ConsolidationError`.

    """
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.log_level, args.log_file)
    # Resolve aliases
    if args.jobs is not None and args.workers is None:
        args.workers = args.jobs
    do_format = bool(args.format or args.post_format)
    # --keep-all-mains alias
    entry_keep_all = bool(args.entry_keep_all or args.keep_all_mains)
    progress = _resolve_progress(args)
    # --pyproject / --setup-cfg awareness: if set, treat config as auto-detected (core already handles)
    # If --pyproject given without --config, we pass config=None but core will auto-detect
    try:
        result = consolidate(
            args.source,
            args.output,
            mode=args.mode,
            workers=args.workers,
            executor=args.executor,
            exclude=tuple(args.exclude),
            include=tuple(args.include),
            keep_unused_imports=args.keep_unused_imports,
            respect_all=args.respect_all,
            entry=tuple(args.entry),
            dry_run=args.dry_run,
            check=args.check,
            stdout=args.stdout,
            config=args.config,
            do_format=do_format,
            formatter=args.formatter,
            diff=args.diff,
            overwrite=args.overwrite,
            force=args.force,
            keep_docstrings=args.keep_docstrings,
            strip_comments=args.strip_comments,
            keep_comments=args.keep_comments,
            rename_strategy=args.rename_strategy,
            dedupe=args.dedupe,
            imports_mode=args.imports,
            all_mode=args.all_mode,
            namespace=args.namespace,
            preserve_modules=args.preserve_modules,
            source_map=args.source_map,
            fail_on_warning=args.fail_on_warning,
            max_skipped=args.max_skipped,
            allow_syntax_errors=args.allow_syntax_errors,
            no_parallel=args.no_parallel,
            progress=progress,
            validate=args.validate or args.validate_import,
            validate_import=args.validate_import,
            copy_data=args.copy_data,
            data_glob=tuple(args.data_glob),
            entry_keep_all=entry_keep_all,
            main_name=args.main_name,
            generate_guard=args.generate_guard,
            strict=args.strict,
            eol=args.eol,
        )
    except ConsolidationError as exc:
        logger.error("{}", exc)
        return 2
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected failure")
        return 1
    # Handle --dry-run / --check reporting
    if args.dry_run or args.check:
        logger.info(
            "Dry-run: {} modules, {} skipped, {} exports, {} warnings",
            result.modules,
            len(result.skipped),
            result.exports,
            len(result.warnings),
        )
        if result.diff:
            logger.info("Diff would be {} chars", len(result.diff))
        # For --check, consolidate already raised on diff non-empty, so we reach here only if check passed
        if args.check and result.diff:
            logger.error("Check failed: output would change")
            return 1
        # Do not treat skipped as failure unless --strict etc.
        if result.skipped:
            logger.warning("{} file(s) would have been skipped", len(result.skipped))
            if args.strict or args.fail_on_warning:
                return 1
            if args.max_skipped is not None and len(result.skipped) > args.max_skipped:
                return 1
        return 0
    # Normal reporting
    # If --stdout was used, files list may be empty; handle gracefully
    if result.files:
        logger.success(
            "{} modules -> {} ({} exported names)",
            result.modules,
            result.output_dir,
            result.exports,
        )
    else:
        if args.stdout:
            # Already printed to stdout; just log success
            logger.success(
                "{} modules -> stdout ({} exported names)",
                result.modules,
                result.exports,
            )
        else:
            logger.success(
                "{} modules -> {} ({} exported names)",
                result.modules,
                result.output_dir,
                result.exports,
            )
    if result.copied_data_files:
        logger.info("Copied {} data file(s)", len(result.copied_data_files))
    if result.skipped:
        logger.error("{} file(s) skipped because of errors", len(result.skipped))
        if args.strict:
            return 1
        if args.max_skipped is not None and len(result.skipped) > args.max_skipped:
            logger.error(
                "Exceeded --max-skipped {}: {} skipped",
                args.max_skipped,
                len(result.skipped),
            )
            return 1
    if args.fail_on_warning and result.warnings:
        logger.error(
            "Failing due to --fail-on-warning: {} warnings", len(result.warnings)
        )
        return 1
    # --validate already handled in core; but if validate failed it would have raised
    if args.validate and not result.validated and not args.stdout:
        # In non-dry-run, validated flag indicates compile passed; if not validated but validate requested, warn
        logger.warning("Validation requested but not marked passed")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
