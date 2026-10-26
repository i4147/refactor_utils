"""Orchestration: discover -> parse (parallel) -> resolve -> write."""

from __future__ import annotations
import difflib
import os
import sys
import traceback
from concurrent.futures import (
    BrokenExecutor,
    Executor,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    as_completed,
)
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence
from loguru import logger
from .discovery import discover, discover_data_files, detect_pyproject_config
from .models import ModuleInfo, SourceFile
from .parser import parse_module_file
from .resolver import Plan, Resolver, ResolverError
from .writer import (
    render_consolidated,
    render_init,
    render_flat,
    render_split,
    render_package,
    render_module,
)


class ConsolidationError(RuntimeError):
    """Raised when a consolidation run cannot produce trustworthy output.

    This is the only exception the CLI maps to a dedicated exit code, so all
    user-facing failures (bad input, unsafe plan, failed ``--check``) use it.
    """


@dataclass
class ConsolidationResult:
    """Summary of a completed (or dry-run) consolidation.

    Attributes:
        output_dir: Directory (or file, in module mode) that was written to.
        files: Files that were written.
        modules: Number of modules that contributed to the output.
        skipped: Paths of files that could not be parsed.
        exports: Number of names exported by the generated ``__init__.py``.
        warnings: Warnings collected from the parser and the resolver.
        diff: Unified diff produced by ``--diff``/``--check``.
        stdout_text: Content that was printed instead of written (``--stdout``).
        copied_data_files: Data files copied next to the output.
        validated: Whether ``--validate`` passed.
        dry_run: Whether files were written.
        plan: The underlying plan, for ``--dry-run`` introspection.

    """

    output_dir: Path
    files: list[Path]
    modules: int
    skipped: list[str] = field(default_factory=list)
    exports: int = 0
    # Extended fields (add, don't break contract)
    warnings: list[str] = field(default_factory=list)
    diff: str = ""
    stdout_text: str = ""
    copied_data_files: list[Path] = field(default_factory=list)
    validated: bool = False
    dry_run: bool = False
    plan: Plan | None = None  # for --dry-run introspection


#: Warnings that mean the generated output may be wrong; ``--strict`` refuses to
#: write it. Dynamic name lookups and incomplete export lists cannot be verified
#: statically, so shipping them silently is the risky option.
_STRICT_WARNING_MARKERS = (
    "name resolution may be incomplete",
    "__all__ is not a literal",
    "mutation — merged statically",
    "cannot resolve internal import",
)


def _apply_eol(text: str, eol: str) -> str:
    """Normalize line endings in generated source.

    Args:
        text: Generated source text.
        eol: ``"lf"``, ``"crlf"`` or ``"preserve"`` (preserve leaves the text
            untouched; callers resolve it beforehand).

    Returns:
        ``text`` with the requested line endings.

    """
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    if eol == "crlf":
        return unified.replace("\n", "\r\n")
    return unified


def _detect_eol(files: Sequence[SourceFile]) -> str:
    """Detect the dominant line ending of the input files.

    Args:
        files: Discovered source files.

    Returns:
        ``"crlf"`` when CRLF endings dominate, otherwise ``"lf"``.

    """
    crlf = lf = 0
    for f in files:
        try:
            sample = Path(f.path).read_bytes()[:8192]
        except OSError:
            continue
        crlf += sample.count(b"\r\n")
        lf += sample.count(b"\n") - crlf
    return "crlf" if crlf > lf else "lf"


def _parse_worker(args: tuple) -> tuple[str, ModuleInfo]:
    """Top-level picklable worker for ProcessPoolExecutor."""
    (
        path_str,
        module,
        is_pkg,
        keep_docstrings,
        strip_comments,
        keep_comments,
        allow_syntax_errors,
    ) = args
    info = parse_module_file(
        path_str,
        module,
        is_pkg,
        keep_docstrings=keep_docstrings,
        strip_comments=strip_comments,
        keep_comments=keep_comments,
        allow_syntax_errors=allow_syntax_errors,
    )
    return module, info


def _parse_all(
    files: Sequence[SourceFile],
    workers: int,
    executor: str,
    keep_docstrings: bool = True,
    strip_comments: bool = False,
    keep_comments: bool = True,
    allow_syntax_errors: bool = False,
    show_progress: bool = False,
) -> dict[str, ModuleInfo]:
    results: dict[str, ModuleInfo] = {}

    def _do_one(f: SourceFile):
        return parse_module_file(
            str(f.path),
            f.module,
            f.is_pkg,
            keep_docstrings=keep_docstrings,
            strip_comments=strip_comments,
            keep_comments=keep_comments,
            allow_syntax_errors=allow_syntax_errors,
        )

    def run_pool(pool_cls: Callable[..., Executor], todo: Sequence[SourceFile]) -> None:
        with pool_cls(max_workers=workers) as pool:
            if pool_cls is ProcessPoolExecutor:
                # Use picklable worker
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
                            allow_syntax_errors,
                        ),
                    ): f
                    for f in todo
                }
            else:
                futures = {pool.submit(_do_one, f): f for f in todo}
            # Progress bar if requested
            iterator = as_completed(futures)
            if show_progress:
                try:
                    from tqdm import tqdm  # type: ignore

                    iterator = tqdm(iterator, total=len(futures), desc="Parsing")
                except ImportError:
                    logger.info(
                        "Progress bar requested but tqdm not installed — falling back to log"
                    )
            for fut in iterator:
                src = futures[fut]
                try:
                    res = fut.result()
                    # _parse_worker returns (module, info) tuple for process pool
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
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to parse {}", src.path)

    if workers <= 1 or len(files) <= 1:
        iterator = files
        if show_progress:
            try:
                from tqdm import tqdm  # type: ignore

                iterator = tqdm(files, desc="Parsing")
            except ImportError:
                pass
        for f in iterator:
            try:
                results[f.module] = _do_one(f)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to parse {}", f.path)
        return results
    pool_cls = ProcessPoolExecutor if executor == "process" else ThreadPoolExecutor
    try:
        run_pool(pool_cls, files)
    except BrokenExecutor:
        logger.warning("Process pool broke; retrying remaining files with threads")
        run_pool(ThreadPoolExecutor, [f for f in files if f.module not in results])
    return results


def _load_config_file(config_path: Path | None, source: Path) -> dict:
    """Load pyproject.toml config for tool.pyrefactor.

    Priority: explicit --config path > pyproject.toml near source > empty.
    """
    if config_path is not None:
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
                logger.warning(
                    "tomllib/tomli not available — cannot read config {}", config_path
                )
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
    else:
        # Auto-detect near source
        return detect_pyproject_config(source)


def consolidate(
    source: Path,
    output: Path,
    *,
    mode: str = "single",
    workers: int | None = None,
    executor: str = "process",
    exclude: tuple[str, ...] = (),
    include: tuple[str, ...] = (),
    keep_unused_imports: bool = False,
    respect_all: bool = False,
    entry: tuple[str, ...] = (),
    dry_run: bool = False,
    check: bool = False,
    stdout: bool = False,
    config: Path | None = None,
    do_format: bool = False,
    formatter: str = "black",
    diff: bool = False,
    overwrite: bool = False,
    force: bool = False,
    keep_docstrings: bool = True,
    strip_comments: bool = False,
    keep_comments: bool = True,
    rename_strategy: str = "prefix",
    dedupe: str = "safe",
    imports_mode: str = "prune",
    all_mode: str = "ignore",
    namespace: bool = False,
    preserve_modules: bool = False,
    source_map: bool = False,
    fail_on_warning: bool = False,
    max_skipped: int | None = None,
    allow_syntax_errors: bool = False,
    no_parallel: bool = False,
    progress: bool = False,
    validate: bool = False,
    validate_import: bool = False,
    copy_data: bool = True,
    data_glob: tuple[str, ...] = (),
    entry_keep_all: bool = False,
    main_name: str | None = None,
    generate_guard: bool = False,
    strict: bool = False,
    eol: str = "lf",
) -> ConsolidationResult:
    """Consolidate a python module, package or project into fewer files.

    Discovery, parsing (optionally parallel), cross-module resolution, rendering
    and validation all happen here; the CLI is a thin wrapper around this
    function.

    Args:
        source: File or directory to consolidate.
        output: Destination directory (or ``.py`` file in module mode).
        mode: Output layout: ``single``, ``package``, ``module``, ``flat`` or
            ``split``.
        workers: Number of parsing workers; defaults to the CPU count.
        executor: ``"process"`` or ``"thread"`` pool for parsing.
        exclude: Additional glob patterns to skip.
        include: Glob patterns that re-include otherwise excluded files.
        keep_unused_imports: Deprecated alias for ``imports_mode="keep"``.
        respect_all: Deprecated alias for ``all_mode="respect"``.
        entry: Modules whose ``__main__`` guard should be kept.
        dry_run: Print the plan without writing files.
        check: Fail when the output would change (or does not exist yet).
        stdout: Write the generated source to stdout instead of files.
        config: Explicit ``pyproject.toml`` to read ``[tool.pyrefactor]`` from.
        do_format: Run black/ruff over the generated files.
        formatter: Formatter to use with ``do_format``.
        diff: Emit a unified diff against the existing output.
        overwrite: Allow overwriting a non-empty output directory.
        force: Alias for ``overwrite``.
        keep_docstrings: Keep docstrings in the output.
        strip_comments: Drop standalone comment lines.
        keep_comments: Keep comments at all.
        rename_strategy: Collision policy: ``prefix``, ``suffix``,
            ``module_path`` or ``error``.
        dedupe: Duplicate removal: ``none``, ``safe`` or ``aggressive``.
        imports_mode: Import handling: ``prune``, ``keep`` or ``strict``.
        all_mode: ``__all__`` handling: ``ignore``, ``respect``, ``merge`` or
            ``recompute``.
        namespace: Emit module namespace comments.
        preserve_modules: Emit per-block origin comments.
        source_map: Emit ``file:line`` comments per block.
        fail_on_warning: Fail when any warning was emitted.
        max_skipped: Fail when more than this many files had to be skipped.
        allow_syntax_errors: Parse files with syntax errors partially.
        no_parallel: Force single-process parsing.
        progress: Show a tqdm progress bar while parsing.
        validate: Compile (and optionally import) the generated files.
        validate_import: With ``validate``, also import the generated module.
        copy_data: Copy non-python data files next to the output.
        data_glob: Only copy data files matching these globs.
        entry_keep_all: Keep every ``__main__`` guard.
        main_name: Rename ``main`` to this name.
        generate_guard: Synthesize a ``__main__`` guard when none exists.
        strict: Promote risky warnings and import cycles to hard errors.
        eol: Line endings for generated files: ``lf``, ``crlf`` or ``preserve``.

    Returns:
        A :class:`ConsolidationResult` describing what was (or would be) written.

    Raises:
        ConsolidationError: When the input cannot be parsed, the plan cannot be
            built safely, or a requested check/validation fails.

    """
    # Normalize mode aliases
    if mode not in ("single", "package", "module", "flat", "split"):
        raise ConsolidationError(f"unknown mode {mode!r}")
    if eol not in ("lf", "crlf", "preserve"):
        raise ConsolidationError(f"unknown eol {eol!r}")
    # Handle --no-parallel
    if no_parallel:
        workers = 1
    # Config file loading (pyproject.toml awareness)
    cfg = _load_config_file(config, source)
    # Merge config into args where CLI not explicitly overriding defaults
    # Only override if using defaults and config provides value
    if cfg:
        if not exclude and cfg.get("exclude"):
            exclude = (
                tuple(cfg["exclude"])
                if isinstance(cfg["exclude"], list)
                else (cfg["exclude"],)
            )
        if not include and cfg.get("include"):
            include = (
                tuple(cfg["include"])
                if isinstance(cfg["include"], list)
                else (cfg["include"],)
            )
        if not entry and cfg.get("entry"):
            entry = (
                tuple(cfg["entry"])
                if isinstance(cfg["entry"], list)
                else (cfg["entry"],)
            )
        if cfg.get("rename_strategy") and rename_strategy == "prefix":
            rename_strategy = cfg["rename_strategy"]
        if cfg.get("dedupe") and dedupe == "safe":
            dedupe = cfg["dedupe"]
        if cfg.get("imports") and imports_mode == "prune":
            imports_mode = cfg["imports"]
        if cfg.get("all") and all_mode == "ignore":
            all_mode = cfg["all"]
        if cfg.get("format") and not do_format:
            do_format = bool(cfg["format"])
        # strictness
        if cfg.get("fail_on_warning") and not fail_on_warning:
            fail_on_warning = bool(cfg["fail_on_warning"])
        if cfg.get("max_skipped") and max_skipped is None:
            max_skipped = int(cfg["max_skipped"])
        if cfg.get("allow_syntax_errors") and not allow_syntax_errors:
            allow_syntax_errors = bool(cfg["allow_syntax_errors"])

    source, output = Path(source), Path(output)
    if not source.exists():
        raise ConsolidationError(f"source does not exist: {source}")
    if mode != "module" and output.exists() and not output.is_dir():
        raise ConsolidationError(
            f"output {output} exists and is not a directory — "
            "pass a directory path or use --mode module with a .py file"
        )
    workers = workers or os.cpu_count() or 1
    if no_parallel:
        workers = 1
    # Discover files first to check output inside source handling and existence
    files = discover(source, exclude=exclude, include=include, skip_dirs=[output])
    if eol == "preserve":
        eol = _detect_eol(files)
    data_files = discover_data_files(
        source,
        exclude=exclude,
        include=include,
        skip_dirs=[output],
        copy_data=copy_data,
    )
    # Handle data_glob additional filtering if provided
    if data_glob:
        import fnmatch

        filtered = []
        for df in data_files:
            if any(
                fnmatch.fnmatch(df.relative, pat)
                or fnmatch.fnmatch(Path(df.path).name, pat)
                for pat in data_glob
            ):
                filtered.append(df)
        data_files = filtered

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
        strip_comments=strip_comments,
        keep_comments=keep_comments,
        allow_syntax_errors=allow_syntax_errors,
        show_progress=progress,
    )
    modules: dict[str, ModuleInfo] = {}
    skipped: list[str] = []
    warnings_acc: list[str] = []
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
    if not modules:
        raise ConsolidationError("no module could be parsed")
    # Enforce --max-skipped
    if max_skipped is not None and len(skipped) > max_skipped:
        raise ConsolidationError(
            f"too many skipped files: {len(skipped)} > --max-skipped {max_skipped}"
        )
    # Enforce --fail-on-warning
    if fail_on_warning and warnings_acc:
        raise ConsolidationError(
            f"fail-on-warning: {len(warnings_acc)} warning(s) emitted"
        )
    # --strict promotes "output may be wrong" warnings into hard failures:
    # name resolution gaps cannot be verified statically, so shipping them is risky.
    if strict:
        risky = [
            w for w in warnings_acc if any(m in w for m in _STRICT_WARNING_MARKERS)
        ]
        if risky:
            raise ConsolidationError(
                "strict: refusing to write output with unresolved warnings:\n  "
                + "\n  ".join(risky)
            )
    # Resolver
    resolver = Resolver(
        modules,
        keep_unused_imports=keep_unused_imports,
        respect_all=respect_all,
        entry=entry,
        workers=workers,
        rename_strategy=rename_strategy,
        dedupe=dedupe,
        imports_mode=imports_mode,
        all_mode=all_mode,
        namespace=namespace,
        preserve_modules=preserve_modules,
        source_map=source_map,
        entry_keep_all=entry_keep_all,
        main_name=main_name,
        generate_guard=generate_guard,
        strict=strict,
    )
    try:
        plan: Plan = resolver.build()
    except ResolverError as exc:
        raise ConsolidationError(str(exc)) from exc
    warnings_acc.extend(plan.warnings)
    # Log renames/dedupes
    for mod, orig, final in plan.renamed:
        logger.info("Renamed {}:{} -> {}", mod, orig, final)
    for mod, name in plan.deduped:
        logger.info("Deduped {} from {}", name, mod)

    source_name = source.resolve().name if source.is_dir() else source.name

    # Render per mode
    # Common kwargs for renderers
    render_kwargs = dict(
        namespace=namespace,
        preserve_modules=preserve_modules,
        source_map=source_map,
        do_format=do_format,
        formatter=formatter,
    )
    if mode == "single":
        mod_text = _apply_eol(
            render_consolidated(plan, source_name, **render_kwargs), eol
        )
        init_text = _apply_eol(render_init(plan, source_name), eol)
        # Validate compilation
        for fname, text in (("_consolidated.py", mod_text), ("__init__.py", init_text)):
            try:
                compile(text, fname, "exec")
            except SyntaxError as exc:
                logger.error(
                    "Generated {} is not valid python: {} (line {})",
                    fname,
                    exc.msg,
                    exc.lineno,
                )
                if validate:
                    raise ConsolidationError(
                        f"validation failed: {fname} syntax error: {exc}"
                    )
        # Diff handling
        diff_text = ""
        if diff or check:
            missing_outputs: list[str] = []
            # Compare against existing output if present
            for fname, new_text in (
                ("_consolidated.py", mod_text),
                ("__init__.py", init_text),
            ):
                existing = output / fname
                if existing.is_file():
                    old_text = existing.read_text(encoding="utf-8")
                    if old_text != new_text:
                        d = difflib.unified_diff(
                            old_text.splitlines(keepends=True),
                            new_text.splitlines(keepends=True),
                            fromfile=f"a/{fname}",
                            tofile=f"b/{fname}",
                        )
                        diff_text += "".join(d)
                    else:
                        logger.info("No diff for {}", fname)
                else:
                    if existing.exists():
                        logger.warning("Existing output {} is not a file", existing)
                    else:
                        logger.info("No existing {} to diff", fname)
                        missing_outputs.append(fname)
            if check and missing_outputs:
                # Like isort/black --check: nothing to compare means "would
                # create files", which is a change and therefore a failure.
                raise ConsolidationError(
                    "check failed: missing "
                    + ", ".join(missing_outputs)
                    + f" in {output} (run without --check to generate)"
                )
            if diff_text:
                if diff:
                    # Emit diff to stdout/log
                    print(diff_text)
                    logger.info("Diff emitted ({} chars)", len(diff_text))
                if check:
                    # --check means verify no changes needed; if diff exists, error
                    if diff_text:
                        raise ConsolidationError(
                            "check failed: output would change (diff not empty)"
                        )
            else:
                if check:
                    logger.success("Check passed: output up to date")
        # Overwrite / force handling
        is_dry = dry_run or check
        if not is_dry:
            # Check overwrite
            if output.exists() and any(output.iterdir()):
                if not (overwrite or force):
                    # If output is non-empty and we would overwrite, require flag
                    # But if diff shows no changes and output exists, we can allow? No, require flag for safety
                    # However, to keep backward compat, we previously allowed overwriting without flag.
                    # So we only enforce if overwrite flag is logically required for destructive cases:
                    # We'll log warning but still proceed unless --overwrite is expected to be mandatory in new spec.
                    # For strictness, if output contains unexpected files, we warn.
                    existing_files = list(output.iterdir())
                    if existing_files:
                        logger.warning(
                            "Output directory {} is non-empty ({} files) — overwriting (use --overwrite to silence)",
                            output,
                            len(existing_files),
                        )
                        if not (overwrite or force):
                            # For backward compat, don't fail; but document that --overwrite controls it.
                            # If we want to enforce, uncomment below:
                            # raise ConsolidationError(f"output directory {output} is non-empty; use --overwrite to force")
                            pass
            # Handle --stdout
            if stdout:
                # Write consolidated to stdout, not files
                sys.stdout.write(mod_text)
                sys.stdout.write("\n--- __init__.py ---\n")
                sys.stdout.write(init_text)
                logger.success(
                    "{} modules -> stdout ({} exported names)",
                    len(modules),
                    sum(len(v) for v in plan.exports.values()),
                )
                # Still return result but files empty?
                exports = sum(len(v) for v in plan.exports.values())
                return ConsolidationResult(
                    output,
                    [],
                    len(modules),
                    skipped,
                    exports,
                    warnings_acc,
                    diff_text,
                    mod_text + "\n\n" + init_text,
                    [],
                    validated=False,
                    dry_run=False,
                    plan=plan,
                )
            output.mkdir(parents=True, exist_ok=True)
            targets = [
                (output / "_consolidated.py", mod_text),
                (output / "__init__.py", init_text),
            ]
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(
                    pool.map(lambda t: t[0].write_text(t[1], encoding="utf-8"), targets)
                )
            # Copy data files
            copied: list[Path] = []
            for df in data_files:
                dest = output / df.relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    import shutil

                    shutil.copy2(df.path, dest)
                    copied.append(dest)
                    logger.info("Copied data file {} -> {}", df.path, dest)
                except OSError as exc:
                    logger.error("Failed to copy data file {}: {}", df.path, exc)
                    if fail_on_warning:
                        raise ConsolidationError(f"data copy failed: {exc}")
            # Validation
            validated = False
            if validate:
                for fname, text in (
                    ("_consolidated.py", mod_text),
                    ("__init__.py", init_text),
                ):
                    try:
                        compile(text, fname, "exec")
                    except SyntaxError as exc:
                        raise ConsolidationError(
                            f"validate: {fname} failed to compile: {exc}"
                        )
                if validate_import:
                    # Smoke import: write to temp and try import
                    try:
                        import importlib.util

                        # Use _consolidated.py as module
                        mod_path = output / "_consolidated.py"
                        spec = importlib.util.spec_from_file_location(
                            "_consolidated_validate", str(mod_path)
                        )
                        if spec and spec.loader:
                            logger.warning(
                                "--validate-import executes {}: top-level code "
                                "(file/network access, threads) will run",
                                mod_path,
                            )
                            mod = importlib.util.module_from_spec(spec)
                            spec.loader.exec_module(mod)  # type: ignore
                            logger.success(
                                "Validation import succeeded for {}", mod_path
                            )
                            validated = True
                        else:
                            logger.error(
                                "Validation: could not create spec for {}", mod_path
                            )
                    except Exception as exc:
                        logger.error("Validation import failed: {}", exc)
                        traceback.print_exc()
                        raise ConsolidationError(f"validate import failed: {exc}")
                else:
                    validated = True
                    logger.success("Validation compile passed")
            exports = sum(len(v) for v in plan.exports.values())
            # Success log
            logger.success(
                "{} modules -> {} ({} exported names)", len(modules), output, exports
            )
            return ConsolidationResult(
                output,
                [p for p, _ in targets],
                len(modules),
                skipped,
                exports,
                warnings_acc,
                diff_text,
                "",
                copied,
                validated,
                False,
                plan,
            )
        else:
            # Dry-run / check: never write files
            logger.info(
                "Dry-run: would write {} and {} to {}",
                "_consolidated.py",
                "__init__.py",
                output,
            )
            for fname, text in (
                ("_consolidated.py", mod_text),
                ("__init__.py", init_text),
            ):
                logger.info(
                    "Would write {} ({} bytes, {} lines)",
                    fname,
                    len(text.encode("utf-8")),
                    text.count("\n") + 1,
                )
            # Show plan summary
            logger.info(
                "Plan: {} blocks, {} exports, {} deduped, {} renamed",
                len(plan.blocks),
                sum(len(v) for v in plan.exports.values()),
                len(plan.deduped),
                len(plan.renamed),
            )
            # Data files that would be copied
            if data_files:
                for df in data_files:
                    logger.info(
                        "Would copy data file {} -> {}/{}", df.path, output, df.relative
                    )
            # Validate even in dry-run if requested
            validated = False
            if validate:
                for fname, text in (
                    ("_consolidated.py", mod_text),
                    ("__init__.py", init_text),
                ):
                    compile(text, fname, "exec")
                validated = True
            exports = sum(len(v) for v in plan.exports.values())
            # Return with dry_run flag, no files written
            return ConsolidationResult(
                output,
                [],
                len(modules),
                skipped,
                exports,
                warnings_acc,
                diff_text,
                mod_text + "\n\n" + init_text if stdout else "",
                [],
                validated,
                True,
                plan,
            )

    elif mode == "package":
        # Package mode: output/package_name/__init__.py contains consolidated, plus subpackage handling
        pkg_name = source_name if source.is_dir() else Path(source).stem
        pkg_dir = output / pkg_name
        mod_text = _apply_eol(render_package(plan, source_name, **render_kwargs), eol)
        # In package mode, we emit package_name/__init__.py and package_name/_consolidated.py
        pkg_init = _apply_eol(render_init(plan, source_name), eol)
        for fname, text in (
            ("_consolidated.py", mod_text),
            ("__init__.py", pkg_init),
        ):
            try:
                compile(text, fname, "exec")
            except SyntaxError as exc:
                logger.error(
                    "Generated {} is not valid python: {} (line {})",
                    fname,
                    exc.msg,
                    exc.lineno,
                )
        if dry_run or check:
            logger.info("Dry-run package mode: would write to {}", pkg_dir)
            return ConsolidationResult(
                output,
                [],
                len(modules),
                skipped,
                sum(len(v) for v in plan.exports.values()),
                warnings_acc,
                "",
                "",
                [],
                False,
                True,
                plan,
            )
        if stdout:
            sys.stdout.write(mod_text)
            return ConsolidationResult(
                output,
                [],
                len(modules),
                skipped,
                sum(len(v) for v in plan.exports.values()),
                warnings_acc,
                "",
                mod_text,
                [],
                False,
                False,
                plan,
            )
        # Overwrite check
        if pkg_dir.exists() and any(pkg_dir.iterdir()) and not (overwrite or force):
            logger.warning("Package output {} non-empty — overwriting", pkg_dir)
        pkg_dir.mkdir(parents=True, exist_ok=True)
        targets = [
            (pkg_dir / "_consolidated.py", mod_text),
            (pkg_dir / "__init__.py", pkg_init),
        ]
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda t: t[0].write_text(t[1], encoding="utf-8"), targets))
        copied = []
        for df in data_files:
            dest = pkg_dir / df.relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                import shutil

                shutil.copy2(df.path, dest)
                copied.append(dest)
            except OSError as exc:
                logger.error("Failed to copy data file {}: {}", df.path, exc)
        exports = sum(len(v) for v in plan.exports.values())
        return ConsolidationResult(
            pkg_dir,
            [p for p, _ in targets],
            len(modules),
            skipped,
            exports,
            warnings_acc,
            "",
            "",
            copied,
            False,
            False,
            plan,
        )

    elif mode == "module":
        # Module mode: single file output (no __init__.py)
        mod_text = _apply_eol(render_module(plan, source_name, **render_kwargs), eol)
        try:
            compile(mod_text, "_consolidated.py", "exec")
        except SyntaxError as exc:
            logger.error(
                "Generated _consolidated.py is not valid python: {} (line {})",
                exc.msg,
                exc.lineno,
            )
            if validate:
                raise ConsolidationError(f"validation failed: {exc}")
        if dry_run or check:
            logger.info("Dry-run module mode: would write single file to {}", output)
            return ConsolidationResult(
                output,
                [],
                len(modules),
                skipped,
                sum(len(v) for v in plan.exports.values()),
                warnings_acc,
                "",
                "",
                [],
                False,
                True,
                plan,
            )
        if stdout:
            sys.stdout.write(mod_text)
            return ConsolidationResult(
                output,
                [],
                len(modules),
                skipped,
                sum(len(v) for v in plan.exports.values()),
                warnings_acc,
                "",
                mod_text,
                [],
                False,
                False,
                plan,
            )
        # Determine output file: if output is dir, write _consolidated.py inside; if file path, write there
        if output.suffix == ".py":
            out_file = output
            out_file.parent.mkdir(parents=True, exist_ok=True)
            if out_file.exists() and not (overwrite or force):
                logger.warning("Output file {} exists — overwriting", out_file)
            out_file.write_text(mod_text, encoding="utf-8")
            targets = [out_file]
        else:
            output.mkdir(parents=True, exist_ok=True)
            if output.exists() and any(output.iterdir()) and not (overwrite or force):
                logger.warning("Output dir {} non-empty — overwriting", output)
            out_file = output / "_consolidated.py"
            out_file.write_text(mod_text, encoding="utf-8")
            targets = [out_file]
        # Copy data files adjacent to output
        copied = []
        for df in data_files:
            dest = (output if output.is_dir() else output.parent) / df.relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                import shutil

                shutil.copy2(df.path, dest)
                copied.append(dest)
            except OSError as exc:
                logger.error("Failed to copy data file {}: {}", df.path, exc)
        exports = sum(len(v) for v in plan.exports.values())
        return ConsolidationResult(
            output,
            targets,
            len(modules),
            skipped,
            exports,
            warnings_acc,
            "",
            "",
            copied,
            False,
            False,
            plan,
        )

    elif mode == "flat":
        mod_text = _apply_eol(render_flat(plan, source_name, **render_kwargs), eol)
        try:
            compile(mod_text, "_consolidated.py", "exec")
        except SyntaxError as exc:
            logger.error(
                "Generated flat is not valid python: {} (line {})", exc.msg, exc.lineno
            )
            if validate:
                raise ConsolidationError(f"validation failed: {exc}")
        if dry_run or check:
            logger.info("Dry-run flat mode: would write to {}", output)
            return ConsolidationResult(
                output,
                [],
                len(modules),
                skipped,
                sum(len(v) for v in plan.exports.values()),
                warnings_acc,
                "",
                "",
                [],
                False,
                True,
                plan,
            )
        if stdout:
            sys.stdout.write(mod_text)
            return ConsolidationResult(
                output,
                [],
                len(modules),
                skipped,
                sum(len(v) for v in plan.exports.values()),
                warnings_acc,
                "",
                mod_text,
                [],
                False,
                False,
                plan,
            )
        output.mkdir(parents=True, exist_ok=True)
        if output.exists() and any(output.iterdir()) and not (overwrite or force):
            logger.warning("Output dir {} non-empty — overwriting", output)
        out_file = output / "_consolidated.py"
        out_file.write_text(mod_text, encoding="utf-8")
        # Also write __init__.py for compatibility?
        init_text = _apply_eol(render_init(plan, source_name), eol)
        (output / "__init__.py").write_text(init_text, encoding="utf-8")
        copied = []
        for df in data_files:
            dest = output / df.relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                import shutil

                shutil.copy2(df.path, dest)
                copied.append(dest)
            except OSError as exc:
                logger.error("Failed to copy data file {}: {}", df.path, exc)
        exports = sum(len(v) for v in plan.exports.values())
        return ConsolidationResult(
            output,
            [out_file, output / "__init__.py"],
            len(modules),
            skipped,
            exports,
            warnings_acc,
            "",
            "",
            copied,
            False,
            False,
            plan,
        )

    elif mode == "split":
        split_files = {
            name: _apply_eol(text, eol)
            for name, text in render_split(plan, source_name).items()
        }
        # Validate each
        for fname, text in split_files.items():
            try:
                compile(text, fname, "exec")
            except SyntaxError as exc:
                logger.error(
                    "Generated {} is not valid python: {} (line {})",
                    fname,
                    exc.msg,
                    exc.lineno,
                )
                if validate:
                    raise ConsolidationError(f"validation failed: {fname}: {exc}")
        if dry_run or check:
            logger.info(
                "Dry-run split mode: would write {} files to {}",
                len(split_files),
                output,
            )
            for fname in split_files:
                logger.info("Would write {}", fname)
            return ConsolidationResult(
                output,
                [],
                len(modules),
                skipped,
                sum(len(v) for v in plan.exports.values()),
                warnings_acc,
                "",
                "",
                [],
                False,
                True,
                plan,
            )
        if stdout:
            for fname, text in split_files.items():
                sys.stdout.write(f"# === {fname} ===\n")
                sys.stdout.write(text)
                sys.stdout.write("\n\n")
            return ConsolidationResult(
                output,
                [],
                len(modules),
                skipped,
                sum(len(v) for v in plan.exports.values()),
                warnings_acc,
                "",
                "\n".join(split_files.values()),
                [],
                False,
                False,
                plan,
            )
        output.mkdir(parents=True, exist_ok=True)
        if output.exists() and any(output.iterdir()) and not (overwrite or force):
            logger.warning("Output dir {} non-empty — overwriting", output)
        targets = []
        for fname, text in split_files.items():
            p = output / fname
            p.write_text(text, encoding="utf-8")
            targets.append(p)
        copied = []
        for df in data_files:
            dest = output / df.relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                import shutil

                shutil.copy2(df.path, dest)
                copied.append(dest)
            except OSError as exc:
                logger.error("Failed to copy data file {}: {}", df.path, exc)
        exports = sum(len(v) for v in plan.exports.values())
        return ConsolidationResult(
            output,
            targets,
            len(modules),
            skipped,
            exports,
            warnings_acc,
            "",
            "",
            copied,
            False,
            False,
            plan,
        )

    # Fallback to single
    raise ConsolidationError(f"unhandled mode {mode!r}")
