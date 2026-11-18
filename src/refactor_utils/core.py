from __future__ import annotations

import ast
import difflib
import fnmatch
import os
import shutil
import sys
from concurrent.futures import (
    BrokenExecutor,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    as_completed,
)
from dataclasses import dataclass, field
from pathlib import Path

import libcst as cst

from .discovery import (
    detect_pyproject_config,
    discover,
    discover_data_files,
    read_pyproject_section,
)
from .logging_setup import logger
from .merged_file import load_merged_sources
from .models import ModuleInfo
from .modernize import modernize_tree
from .parser import parse_module_file
from .resolver import Plan, Resolver, is_valid_identifier
from .writer import render_consolidated


class ConsolidationError(RuntimeError):
    pass


@dataclass(slots=True)
class ConsolidationResult:
    output_dir: Path
    files: list
    modules: int
    skipped: list
    exports: int
    warnings: list
    diff: str
    stdout_text: str
    copied_data_files: list
    validated: bool = False
    dry_run: bool = False
    plan: Plan | None = None
    embedded_data_files: list = field(default_factory=list)


def _parse_worker(args: tuple):
    path_str, module, is_pkg, keep_docstrings, keep_comments = args
    info = parse_module_file(
        path_str,
        module,
        is_pkg,
        keep_docstrings=keep_docstrings,
        keep_comments=keep_comments,
    )
    return module, info


def _parse_all(
    files,
    workers: int,
    executor: str,
    keep_docstrings: bool = True,
    keep_comments: bool = True,
    show_progress: bool = False,
) -> dict:
    results: dict = {}

    def _do_one(f):
        return parse_module_file(
            str(f.path),
            f.module,
            f.is_pkg,
            keep_docstrings=keep_docstrings,
            keep_comments=keep_comments,
        )

    def _progress(iterator, total=None):
        if not show_progress:
            return iterator
        try:
            from tqdm import tqdm  # type: ignore

            return tqdm(iterator, total=total, desc="Parsing")
        except ImportError:
            logger.info(
                "Progress bar requested but tqdm not installed — falling back to log"
            )
            return iterator

    def run_pool(pool_cls, todo) -> None:
        with pool_cls(max_workers=workers) as pool:
            if pool_cls is ProcessPoolExecutor:
                futures = {
                    pool.submit(
                        _parse_worker,
                        (
                            str(f.path),
                            f.module,
                            f.is_pkg,
                            keep_docstrings,
                            keep_comments,
                        ),
                    ): f
                    for f in todo
                }
            else:
                futures = {pool.submit(_do_one, f): f for f in todo}
            for fut in _progress(as_completed(futures), len(futures)):
                src = futures[fut]
                try:
                    res = fut.result()
                    if (
                        isinstance(res, tuple)
                        and len(res) == 2
                        and isinstance(res[1], ModuleInfo)
                    ):
                        results[res[0]] = res[1]
                    else:
                        results[src.module] = res
                except BrokenExecutor:
                    raise
                except Exception:
                    logger.exception("Failed to parse {}", src.path)

    if workers <= 1 or len(files) <= 1:
        for f in _progress(files, len(files)):
            try:
                results[f.module] = _do_one(f)
            except Exception:
                logger.exception("Failed to parse {}", f.path)
        return results
    pool_cls = ProcessPoolExecutor if executor == "process" else ThreadPoolExecutor
    try:
        run_pool(pool_cls, files)
    except BrokenExecutor:
        logger.warning("Process pool broke; retrying remaining files with threads")
        run_pool(ThreadPoolExecutor, [f for f in files if f.module not in results])
    return results


def _load_config_file(config_path, source) -> dict:
    if config_path is None:
        return detect_pyproject_config(source)
    config_path = Path(config_path)
    if not config_path.is_file():
        logger.warning("Config file not found: {}", config_path)
        return {}
    tool = read_pyproject_section(config_path)
    if tool:
        logger.info("Loaded config from {}", config_path)
    return tool


def _config_list(cfg: dict, key: str) -> tuple:
    value = cfg.get(key)
    if not value:
        return ()
    return tuple(value) if isinstance(value, list) else (value,)


def validate_syntax(text: str, filename: str = "<consolidated>") -> ast.Module:
    try:
        return ast.parse(text, filename=filename)
    except SyntaxError as exc:
        line = (exc.text or "").strip()
        if not line and exc.lineno:
            lines = text.splitlines()
            if 0 < exc.lineno <= len(lines):
                line = lines[exc.lineno - 1].strip()
        where = f"line {exc.lineno}" if exc.lineno else "unknown line"
        detail = f": {line!r}" if line else ""
        raise ConsolidationError(
            f"generated {filename} is not valid python: {exc.msg} ({where}){detail}"
        ) from exc
    except ValueError as exc:
        raise ConsolidationError(
            f"generated {filename} is not valid python: {exc}"
        ) from exc


def consolidate(
    source,
    output,
    *,
    filename: str | None = None,
    workers: int | None = None,
    executor: str = "process",
    exclude: tuple = (),
    include: tuple = (),
    imports_mode: str = "prune",
    entry: tuple = (),
    dry_run: bool = False,
    check: bool = False,
    stdout: bool = False,
    config: Path | None = None,
    do_format: bool = False,
    formatter: str = "black",
    diff: bool = False,
    force: bool = False,
    keep_docstrings: bool = True,
    keep_comments: bool = True,
    progress: bool = False,
    copy_data: bool = True,
    embed_data: bool = False,
    data_glob: tuple = (),
    entry_keep_all: bool = False,
    main_name: str | None = None,
    generate_guard: bool = False,
    modernize: str | None = None,
    merged_file: Path | str | None = None,
) -> ConsolidationResult:
    cfg = _load_config_file(config, source)
    if cfg:
        if not exclude:
            exclude = _config_list(cfg, "exclude")
        if not include:
            include = _config_list(cfg, "include")
        if not entry:
            entry = _config_list(cfg, "entry")
        if cfg.get("imports"):
            imports_mode = str(cfg["imports"])
        if cfg.get("format") and not do_format:
            do_format = bool(cfg["format"])
        if cfg.get("formatter"):
            formatter = str(cfg["formatter"])
        if cfg.get("copy_data") is not None:
            copy_data = bool(cfg["copy_data"])
        if cfg.get("embed_data"):
            embed_data = bool(cfg["embed_data"])
        if cfg.get("data_glob"):
            data_glob = _config_list(cfg, "data_glob")
        if not modernize and cfg.get("modernize"):
            modernize = str(cfg["modernize"])
    if imports_mode not in ("prune", "keep", "strict"):
        raise ConsolidationError(
            f"imports_mode must be one of prune/keep/strict, got {imports_mode!r}"
        )
    if main_name is not None and not is_valid_identifier(main_name):
        raise ConsolidationError(
            f"--main-name {main_name!r} is not a valid python identifier"
        )
    source, output = Path(source), Path(output)
    merged_path: Path | None = Path(merged_file) if merged_file else None
    if merged_path is not None:
        if not merged_path.is_file():
            raise ConsolidationError(f"merged file does not exist: {merged_path}")
        # The merged file (# File: headers) is the real source of truth here;
        # `source` is only used for config discovery / naming.
    elif not source.exists():
        raise ConsolidationError(f"source does not exist: {source}")
    workers = workers or os.cpu_count() or 1
    if merged_path is not None:
        source_name = merged_path.stem
    else:
        source_name = source.resolve().name if source.is_dir() else source.stem
    filename = filename or source_name
    if not filename.endswith(".py"):
        filename = f"{filename}.py"
    if merged_path is not None:
        files, _staged_root = load_merged_sources(merged_path)
        # Data-file discovery is not meaningful for a merged-file input: the
        # merged format only carries python sources.
        data_files = []
    else:
        files = discover(source, exclude=exclude, include=include, skip_dirs=[output])
        data_files = discover_data_files(
            source,
            exclude=exclude,
            include=include,
            skip_dirs=[output],
            copy_data=copy_data,
            collect=copy_data or embed_data,
        )
        if data_glob:
            data_files = [
                df
                for df in data_files
                if any(
                    fnmatch.fnmatch(df.relative, pat)
                    or fnmatch.fnmatch(Path(df.path).name, pat)
                    for pat in data_glob
                )
            ]
    if not files:
        raise ConsolidationError(f"no python files found under {source}")
    logger.info("Parsing {} files with {} {} worker(s)", len(files), workers, executor)
    if data_files:
        logger.info("Found {} data file(s) to preserve", len(data_files))
    parsed = _parse_all(
        files,
        workers,
        executor,
        keep_docstrings=keep_docstrings,
        keep_comments=keep_comments,
        show_progress=progress,
    )
    modules: dict = {}
    skipped: list = []
    warnings_acc: list = []
    for name, info in parsed.items():
        for w in info.warnings:
            logger.warning(w)
            warnings_acc.append(w)
        if info.errors:
            for e in info.errors:
                logger.error(e)
            skipped.append(info.path)
            continue
        modules[name] = info
    if skipped:
        raise ConsolidationError(
            f"{len(skipped)} file(s) could not be parsed; refusing to emit a "
            f"partial consolidation: {', '.join(skipped)}"
        )
    if not modules:
        raise ConsolidationError("no module could be parsed")
    resources: list[tuple[str, bytes]] | None = None
    if embed_data and data_files:
        resources = []
        for df in data_files:
            try:
                resources.append((df.relative, Path(df.path).read_bytes()))
            except OSError as exc:
                logger.error("Cannot read data file {}: {}", df.path, exc)
                raise ConsolidationError(f"cannot read data file {df.path}: {exc}")
    resolver = Resolver(
        modules,
        entry=entry,
        workers=workers,
        imports_mode=imports_mode,
        entry_keep_all=entry_keep_all,
        main_name=main_name,
        generate_guard=generate_guard,
        resources=resources,
    )
    plan = resolver.build()
    for mod, orig, final in plan.renamed:
        logger.info("Renamed {}:{} -> {}", mod, orig, final)
    for mod, name in plan.deduped:
        logger.info("Deduped {} from {}", name, mod)
    mod_text = render_consolidated(
        plan, source_name, do_format=do_format, formatter=formatter
    )
    exports = sum(len(v) for v in plan.exports.values())
    if modernize:
        # Apply modernization passes (six -> py3, os.path -> pathlib) to the
        # rendered tree. libcst round-trips so formatting is preserved; the
        # result is re-validated below just like the unmodified output.
        try:
            tree = cst.parse_module(mod_text)
            tree, report = modernize_tree(tree, modernize)
            if report.changed:
                mod_text = tree.code
        except Exception as exc:
            raise ConsolidationError(
                f"modernize pass failed on consolidated output: {exc}"
            ) from exc
    validate_syntax(mod_text, filename)
    diff_text = ""
    target = output / filename
    if diff or check:
        if target.is_file():
            old_text = target.read_text(encoding="utf-8")
            if old_text != mod_text:
                diff_text = "".join(
                    difflib.unified_diff(
                        old_text.splitlines(keepends=True),
                        mod_text.splitlines(keepends=True),
                        fromfile=f"a/{filename}",
                        tofile=f"b/{filename}",
                    )
                )
            else:
                logger.info("No diff for {}", filename)
        else:
            logger.info("No existing {} to diff", target)
        if diff_text:
            logger.info("Diff ({} chars):", len(diff_text))
            sys.stdout.write(diff_text)
        if check:
            if diff_text:
                raise ConsolidationError(
                    "check failed: output would change (diff not empty)"
                )
            logger.success("Check passed: output up to date")
            return ConsolidationResult(
                output,
                [],
                len(modules),
                skipped,
                exports,
                warnings_acc,
                "",
                "",
                [],
                validated=True,
                dry_run=True,
                plan=plan,
            )
    if dry_run:
        logger.info("Dry-run: would write {} to {}", filename, output)
        logger.info(
            "Would write {} ({} bytes, {} lines)",
            filename,
            len(mod_text.encode("utf-8")),
            mod_text.count("\n") + 1,
        )
        logger.info(
            "Plan: {} blocks, {} exports, {} deduped, {} renamed",
            len(plan.blocks),
            exports,
            len(plan.deduped),
            len(plan.renamed),
        )
        if resources:
            logger.info("Would embed {} data file(s)", len(resources))
        if copy_data:
            for df in data_files:
                logger.info(
                    "Would copy data file {} -> {}/{}", df.path, output, df.relative
                )
        return ConsolidationResult(
            output,
            [],
            len(modules),
            skipped,
            exports,
            warnings_acc,
            "",
            mod_text if stdout else "",
            [],
            validated=True,
            dry_run=True,
            plan=plan,
        )
    if stdout:
        sys.stdout.write(mod_text)
        return ConsolidationResult(
            output,
            [],
            len(modules),
            skipped,
            exports,
            warnings_acc,
            diff_text,
            mod_text,
            [],
            validated=True,
            plan=plan,
            embedded_data_files=[name for name, _ in resources] if resources else [],
        )
    if target.exists() and not force:
        logger.warning("{} exists — overwriting (use --force to silence)", target)
    output.mkdir(parents=True, exist_ok=True)
    tmp_target = target.with_suffix(target.suffix + ".tmp")
    tmp_target.write_text(mod_text, encoding="utf-8")
    tmp_target.replace(target)
    written = [target]
    copied: list = []
    if copy_data:
        for df in data_files:
            dest = output / df.relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(df.path, dest)
                copied.append(dest)
                logger.debug("Copied data file {} -> {}", df.path, dest)
            except OSError as exc:
                logger.error("Failed to copy data file {}: {}", df.path, exc)
    if resources:
        logger.info("Embedded {} data file(s) into {}", len(resources), target)
    return ConsolidationResult(
        output,
        written,
        len(modules),
        skipped,
        exports,
        warnings_acc,
        diff_text,
        "",
        copied,
        validated=True,
        plan=plan,
        embedded_data_files=[name for name, _ in resources] if resources else [],
    )
