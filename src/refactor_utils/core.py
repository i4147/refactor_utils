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

from loguru import logger

from .data_embed import collect_embedded_data
from .discovery import discover, discover_data_files, detect_pyproject_config
from .models import ModuleInfo, SourceFile
from .parser import parse_module_file
from .resolver import Plan, Resolver, is_valid_identifier
from .writer import render_consolidated


class ConsolidationError(RuntimeError):
    pass


@dataclass
class ConsolidationResult:
    output_dir: Path = None
    files: list = field(default_factory=list)
    modules: int = 0
    skipped: list = field(default_factory=list)
    exports: int = 0
    warnings: list = field(default_factory=list)
    diff: str = ""
    stdout_text: str = ""
    copied_data_files: list = field(default_factory=list)
    embedded_data_files: list = field(default_factory=list)
    validated: bool = False
    dry_run: bool = False
    plan: object = None


def _parse_worker(args):
    path_str, module, is_pkg, keep_docstrings, strip_comments, keep_comments = args
    info = parse_module_file(
        path_str,
        module,
        is_pkg,
        keep_docstrings=keep_docstrings,
        strip_comments=strip_comments,
        keep_comments=keep_comments,
    )
    return module, info


def _parse_all(
    files,
    workers,
    executor,
    keep_docstrings=True,
    strip_comments=False,
    keep_comments=True,
    show_progress=False,
):
    results = {}
    failures = []

    def _do_one(f):
        return parse_module_file(
            str(f.path),
            f.module,
            f.is_pkg,
            keep_docstrings=keep_docstrings,
            strip_comments=strip_comments,
            keep_comments=keep_comments,
        )

    def _progress(iterator, total=None):
        if not show_progress:
            return iterator
        try:
            from tqdm import tqdm  # type: ignore

            return tqdm(iterator, total=total, desc="Parsing")
        except ImportError:
            logger.info("Progress bar requested but tqdm not installed — falling back to log")
            return iterator

    def run_pool(pool_cls, todo):
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
                            strip_comments,
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
                    if isinstance(res, tuple) and len(res) == 2 and isinstance(res[1], ModuleInfo):
                        results[res[0]] = res[1]
                    else:
                        results[src.module] = res
                except BrokenExecutor:
                    raise
                except Exception as exc:
                    logger.exception("Failed to parse {}", src.path)
                    failures.append((str(src.path), str(exc)))

    if workers <= 1 or len(files) <= 1:
        for f in _progress(files, len(files)):
            try:
                results[f.module] = _do_one(f)
            except Exception as exc:
                logger.exception("Failed to parse {}", f.path)
                failures.append((str(f.path), str(exc)))
        return results, failures

    pool_cls = ProcessPoolExecutor if executor == "process" else ThreadPoolExecutor
    try:
        run_pool(pool_cls, files)
    except BrokenExecutor:
        logger.warning("Process pool broke; retrying remaining files with threads")
        run_pool(ThreadPoolExecutor, [f for f in files if f.module not in results])
    return results, failures


def _load_config_file(config_path, source):
    if config_path is None:
        return detect_pyproject_config(source)
    config_path = Path(config_path)
    if not config_path.is_file():
        logger.warning("Config file not found: {}", config_path)
        return {}
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore
        except ImportError:
            logger.warning("tomllib/tomli not available — cannot read config {}", config_path)
            return {}
    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
        tool = data.get("tool", {}).get("pyrefactor", {})
        if tool:
            logger.info("Loaded config from {}", config_path)
        return tool if isinstance(tool, dict) else {}
    except Exception as exc:
        logger.warning("Failed to parse config {}: {}", config_path, exc)
        return {}


def _config_list(cfg, key):
    value = cfg.get(key)
    if not value:
        return ()
    return tuple(value) if isinstance(value, list) else (value,)


def validate_syntax(text, filename="<consolidated>"):
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
        raise ConsolidationError(f"generated {filename} is not valid python: {exc.msg} ({where}){detail}") from exc
    except ValueError as exc:
        raise ConsolidationError(f"generated {filename} is not valid python: {exc}") from exc


def consolidate(
    source,
    output,
    *,
    filename=None,
    workers=None,
    executor="process",
    exclude=(),
    include=(),
    imports_mode="prune",
    entry=(),
    dry_run=False,
    check=False,
    stdout=False,
    config=None,
    do_format=False,
    formatter="black",
    diff=False,
    force=False,
    keep_docstrings=True,
    strip_comments=False,
    keep_comments=True,
    progress=False,
    copy_data=True,
    data_glob=(),
    embed_data=None,
    entry_keep_all=False,
    main_name=None,
    generate_guard=False,
):
    if check and stdout:
        raise ConsolidationError("--check and --stdout cannot be used together")

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
        if cfg.get("data_glob"):
            data_glob = _config_list(cfg, "data_glob")

    if embed_data is None:
        embed_data = copy_data

    if main_name is not None and not is_valid_identifier(main_name):
        raise ConsolidationError(f"--main-name {main_name!r} is not a valid python identifier")

    source, output = Path(source), Path(output)
    if not source.exists():
        raise ConsolidationError(f"source does not exist: {source}")

    workers = workers or os.cpu_count() or 1
    source_name = source.resolve().name if source.is_dir() else source.stem
    filename = filename or Path.cwd().name
    if not filename.endswith(".py"):
        filename = f"{filename}.py"

    files = discover(source, exclude=exclude, include=include, skip_dirs=[output])
    data_files = discover_data_files(
        source,
        exclude=exclude,
        include=include,
        skip_dirs=[output],
        copy_data=copy_data or embed_data,
    )
    if data_glob:
        data_files = [
            df
            for df in data_files
            if any(fnmatch.fnmatch(df.relative, pat) or fnmatch.fnmatch(Path(df.path).name, pat) for pat in data_glob)
        ]
    if not files:
        raise ConsolidationError(f"no python files found under {source}")

    logger.info("Parsing {} files with {} {} worker(s)", len(files), workers, executor)
    if data_files:
        logger.info("Found {} data file(s) to preserve", len(data_files))

    parsed, parse_failures = _parse_all(
        files,
        workers,
        executor,
        keep_docstrings=keep_docstrings,
        strip_comments=strip_comments,
        keep_comments=keep_comments,
        show_progress=progress,
    )

    modules = {}
    skipped = []
    warnings_acc = []
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

    for path, err in parse_failures:
        logger.error("Failed to parse {}: {}", path, err)
        skipped.append(path)

    if skipped:
        raise ConsolidationError(
            f"{len(skipped)} file(s) could not be parsed; refusing to emit a "
            f"partial consolidation: {', '.join(skipped)}"
        )
    if not modules:
        raise ConsolidationError("no module could be parsed")

    embedded = {}
    if embed_data and data_files:
        embedded = collect_embedded_data(data_files)
        if embedded:
            logger.info("Embedding {} data file(s) in output", len(embedded))

    resolver = Resolver(
        modules,
        entry=entry,
        workers=workers,
        imports_mode=imports_mode,
        entry_keep_all=entry_keep_all,
        main_name=main_name,
        generate_guard=generate_guard,
    )
    plan = resolver.build()
    for mod, orig, final in plan.renamed:
        logger.info("Renamed {}:{} -> {}", mod, orig, final)
    for mod, name in plan.deduped:
        logger.info("Deduped {} from {}", name, mod)

    mod_text = render_consolidated(
        plan,
        source_name,
        do_format=do_format,
        formatter=formatter,
        embedded_data=embedded,
    )
    exports = sum(len(v) for v in plan.exports.values())
    validate_syntax(mod_text, filename)

    diff_text = ""
    target = output / filename

    if diff or check:
        target_exists = target.is_file()
        if target_exists:
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
            if diff_text or not target_exists:
                if diff_text:
                    msg = "check failed: output would change (diff not empty)"
                else:
                    msg = f"check failed: output does not exist at {target}"
                raise ConsolidationError(msg)
            logger.success("Check passed: output up to date")
            return ConsolidationResult(
                output_dir=output,
                files=[],
                modules=len(modules),
                skipped=skipped,
                exports=exports,
                warnings=warnings_acc,
                validated=True,
                dry_run=True,
                plan=plan,
                embedded_data_files=list(embedded),
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
        for df in data_files:
            logger.info("Would copy data file {} -> {}/{}", df.path, output, df.relative)
        if embedded:
            logger.info("Would embed {} data file(s)", len(embedded))
        return ConsolidationResult(
            output_dir=output,
            files=[],
            modules=len(modules),
            skipped=skipped,
            exports=exports,
            warnings=warnings_acc,
            stdout_text=mod_text if stdout else "",
            validated=True,
            dry_run=True,
            plan=plan,
            embedded_data_files=list(embedded),
        )

    if stdout:
        sys.stdout.write(mod_text)
        return ConsolidationResult(
            output_dir=output,
            files=[],
            modules=len(modules),
            skipped=skipped,
            exports=exports,
            warnings=warnings_acc,
            diff=diff_text,
            stdout_text=mod_text,
            validated=True,
            plan=plan,
            embedded_data_files=list(embedded),
        )

    if target.exists() and not force:
        logger.warning("{} exists — overwriting (use --force to silence)", target)

    output.mkdir(parents=True, exist_ok=True)
    tmp_target = target.with_suffix(target.suffix + ".tmp")
    tmp_target.write_text(mod_text, encoding="utf-8")
    tmp_target.replace(target)
    written = [target]

    copied = []
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

    return ConsolidationResult(
        output_dir=output,
        files=written,
        modules=len(modules),
        skipped=skipped,
        exports=exports,
        warnings=warnings_acc,
        diff=diff_text,
        copied_data_files=copied,
        embedded_data_files=list(embedded),
        validated=True,
        plan=plan,
    )
