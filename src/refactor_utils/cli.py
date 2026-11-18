from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .bundle import consolidate_bundle
from .core import ConsolidationError, consolidate
from .logging_setup import logger, setup_logging


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pyrefactor",
        description=(
            "Consolidate a python module/package/project into a single, dependency-ordered file,\n"
            "or pack it into a self-extracting zip bundle (--bundle)."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument(
        "source",
        nargs="?",
        type=str,
        help=(
            "package directory, project root or single .py file\n"
            "(optional with --merged-file; treated as a module name with -m/--module)"
        ),
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("output"),
        help=(
            "output directory for source mode (default: ./output);\n"
            "output file for bundle mode (default: ./output/<name>_standalone.py)"
        ),
    )
    p.add_argument(
        "--name",
        default=None,
        metavar="NAME",
        help=(
            "name of the generated file\n(default: the source name, e.g. output/<source>.py)"
        ),
    )
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
            "extra path glob to skip (repeatable)\nprecedence: default excludes < --include < --exclude"
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
        "--embed-data",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "embed data files into the consolidated file so it is fully\n"
            "self-contained: text files (json, ini, ...) as plain text,\n"
            "binary files (fonts, ...) as base64\n"
            "(the module then exposes get_resource()/resource_path())"
        ),
    )
    p.add_argument(
        "--data-glob",
        action="append",
        default=[],
        metavar="GLOB",
        help="only copy/embed data files matching this glob (repeatable)",
    )
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
    p.add_argument(
        "--imports",
        choices=["prune", "keep", "strict"],
        default="prune",
        help=(
            "import handling: prune (default, drop unused), keep (keep all),\nstrict (prune and warn on unused)"
        ),
    )
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
    # Modernization (ported from consolidate.py)
    p.add_argument(
        "--modernize",
        default=None,
        metavar="PASSES",
        help=(
            "comma-separated modernization passes to apply to the output:\n"
            "six (six -> python3), ospath (os.path -> pathlib)\n"
            "e.g. --modernize six,ospath"
        ),
    )
    # Merged-file input (ported from consolidate.py)
    p.add_argument(
        "-f",
        "--merged-file",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "read source from a single merged file using `# File: <relpath>`\n"
            "section headers (the ru.txt format) instead of a directory"
        ),
    )
    # Bundle mode (ported from consolidator.py)
    bundle = p.add_argument_group("bundle mode (--bundle)")
    bundle.add_argument(
        "--bundle",
        action="store_true",
        help=(
            "pack the package verbatim into a self-extracting zip bundle\n"
            "(embeds a bootstrap that extracts to a temp dir at runtime and\n"
            "runs the entry via runpy; preserves dynamic features intact)"
        ),
    )
    bundle.add_argument(
        "-m",
        "--module",
        action="store_true",
        help="treat SOURCE as a module name, emulating `python -m SOURCE`",
    )
    bundle.add_argument(
        "--main-module",
        default=None,
        metavar="MOD",
        help=(
            "module inside the entry package to run as its __main__\n"
            "(dotted, relative to the package; auto-detected if omitted)"
        ),
    )
    bundle.add_argument(
        "--include-package",
        action="append",
        default=[],
        metavar="PKG",
        help="pure-Python third-party package to bundle (name or path; repeatable)",
    )
    bundle.add_argument(
        "--site-path",
        action="append",
        default=[],
        metavar="DIR",
        help="extra directories searched for --include-package names (repeatable)",
    )
    bundle.add_argument(
        "-p",
        "--add-python-path",
        action="append",
        default=[],
        metavar="DIR",
        help=(
            "extra directories to search for local packages (repeatable;\n"
            "':'-separated lists allowed)"
        ),
    )
    bundle.add_argument(
        "--exclude-module",
        action="append",
        default=[],
        metavar="PAT",
        help=(
            "exclude modules/packages (fnmatch on dotted names;\n"
            "a bare name excludes its whole subtree; repeatable)"
        ),
    )
    bundle.add_argument(
        "--include-data",
        action="append",
        default=[],
        metavar="SPEC",
        help=(
            "embed data files: 'PKG:GLOB', 'GLOB' (relative to entry dir and\n"
            "--add-python-path roots), or an existing dir/file path (repeatable)"
        ),
    )
    bundle.add_argument(
        "--include-package-data",
        action="append",
        default=[],
        metavar="PKG",
        help=("embed data files of a bundled package ('*' for all; repeatable)"),
    )
    bundle.add_argument(
        "--exclude-data",
        action="append",
        default=[],
        metavar="GLOB",
        help="exclude data files (glob matched against bundle-relative paths)",
    )
    bundle.add_argument(
        "--no-whole-package",
        action="store_true",
        help="embed only statically reachable modules instead of whole packages",
    )
    bundle.add_argument(
        "--compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="payload compression for --bundle (default: deflate)",
    )
    bundle.add_argument(
        "--python",
        default=None,
        metavar="INTERP",
        help="shebang interpreter for the bundle (default: current python)",
    )
    bundle.add_argument(
        "--bundle-runtime-debug",
        action="store_true",
        help="the generated bundle prints its extraction directory to stderr",
    )
    bundle.add_argument(
        "--bundle-keep-tmpdir",
        action="store_true",
        help="the generated bundle keeps its extraction directory",
    )
    bundle.add_argument(
        "--bundle-strict",
        action="store_true",
        help="fail the bundle build on unresolved imports",
    )
    bundle.add_argument(
        "--no-deterministic",
        action="store_true",
        help="embed the build date in the bundle instead of a fixed timestamp",
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
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["TRACE", "DEBUG", "INFO", "WARNING", "ERROR"],
        help="log level (default: INFO)",
    )
    p.add_argument("--log-file", type=Path, default=None, help="also log to this file")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def _bundle_default_output(source: str | None, requested: Path) -> Path:
    stem = "bundle"
    if source:
        pp = Path(source)
        candidate = pp.stem if pp.stem and pp.suffix == ".py" else pp.name
        if candidate:
            stem = candidate
    if requested.suffix == ".py":
        return requested
    # Treat the requested path as a directory.
    return requested / f"{stem}_standalone.py"


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level, args.log_file)
    if args.merged_file is None and args.source is None:
        logger.error("provide a SOURCE, or use --merged-file PATH")
        return 2
    if args.bundle and args.merged_file is not None:
        logger.error("--bundle cannot be combined with --merged-file")
        return 2
    try:
        if args.bundle:
            return _run_bundle(args)
        result = consolidate(
            args.source if args.source is not None else str(args.merged_file),
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
            keep_comments=args.keep_comments,
            progress=args.progress,
            copy_data=args.copy_data,
            embed_data=args.embed_data,
            data_glob=tuple(args.data_glob),
            entry_keep_all=args.entry_keep_all,
            main_name=args.main_name,
            generate_guard=args.generate_guard,
            modernize=args.modernize,
            merged_file=args.merged_file,
        )
    except ConsolidationError as exc:
        logger.error("{}", exc)
        return 2
    except Exception:
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
    if result.embedded_data_files:
        logger.info(
            "Embedded {} data file(s): {}",
            len(result.embedded_data_files),
            ", ".join(result.embedded_data_files),
        )
    if result.skipped:
        logger.error("{} file(s) skipped because of errors", len(result.skipped))
    return 0


def _run_bundle(args) -> int:
    if args.source is None:
        logger.error(
            "--bundle requires a SOURCE (entry script, package dir, or module name)"
        )
        return 2
    output: str | Path | None
    if args.stdout:
        output = None
    else:
        output = _bundle_default_output(args.source, args.output)
    consolidate_bundle(
        args.source,
        output=output,
        add_python_path=tuple(args.add_python_path),
        module_mode=args.module,
        include_packages=tuple(args.include_package),
        site_paths=tuple(args.site_path),
        exclude_modules=tuple(args.exclude_module),
        include_data=tuple(args.include_data),
        include_package_data=tuple(args.include_package_data),
        exclude_data=tuple(args.exclude_data),
        whole_package=not args.no_whole_package,
        strict=args.bundle_strict,
        main_module=args.main_module,
        python=args.python,
        compression=args.compression,
        runtime_debug=args.bundle_runtime_debug,
        keep_tmpdir=args.bundle_keep_tmpdir,
        deterministic=not args.no_deterministic,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
