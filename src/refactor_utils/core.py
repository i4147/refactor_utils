"""Orchestration: discover -> parse (parallel) -> resolve -> write.

Two entry points live here:

* :func:`consolidate_package` implements the ``pyrefactor [pkg]`` contract:
  one ``consolidated/<pkgname>.py`` file built from the temporary
  ``const.py``/``classes.py``/``func.py`` extractions, with an ``__all__``
  section added by :mod:`autoall`.
* :func:`consolidate` is the lower-level, multi-layout library API kept for
  backwards compatibility (``single``/``package``/``module``/``flat``/
  ``split``).
"""

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
from .discovery import (
    detect_pkg_name,
    detect_pyproject_config,
    discover,
    discover_data_files,
    normalize_pkg_name,
)
from .imports import future_section
from .models import ModuleInfo, SourceFile
from .parser import parse_module_file
from .resolver import Plan, Resolver, ResolverError
from .writer import (
    format_source,
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
        all_added: Whether :mod:`autoall` appended an ``__all__`` section.
        pkg_name: Detected package name (``consolidate_package`` only); also
            the file name of the generated module.

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
    all_added: bool = False  # whether autoall appended an __all__ section
    pkg_name: str = ""  # detected package name (consolidate_package only)


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
    """Parse every discovered file, optionally in parallel, and return the parsed modules keyed by dotted name."""
    results: dict[str, ModuleInfo] = {}

    def _do_one(f: SourceFile):
        """Parse one file in the current process."""
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
        """Parse ``todo`` with ``pool_cls`` and merge the results into ``results``."""
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


#: Section banner used inside ``consolidated/<pkgname>.py``.
_HEADER_RULE = "# " + "-" * 75
#: Human title per extraction section.
_SECTION_TITLES = {"const": "Constants", "class": "Classes", "func": "Functions"}
#: Temporary extraction files, in the order they are merged (prompt.md §2).
_SECTION_FILES = {"const": "const.py", "class": "classes.py", "func": "func.py"}
#: Temporary section files of this or an interrupted run; always removed after
#: the merge so the output directory holds a single file.
_TEMP_SECTION_NAMES = frozenset({"const.py", "classes.py", "func.py"})
#: Files of the legacy ``output/`` layout (``__init__.py`` +
#: ``_consolidated.py``); removed only inside a recognised output directory.
_LEGACY_LAYOUT_NAMES = frozenset({"__init__.py", "_consolidated.py"})
#: First line of every generated file; used to recognise stale outputs.
_GENERATED_PREFIX = '"""Consolidated from '


def _section_source(section: str, texts: Sequence[str]) -> str:
    """Render one extraction section with its banner header.

    Args:
        section: Section key: ``"const"``, ``"class"`` or ``"func"``.
        texts: Rendered block sources belonging to the section, in dependency
            order.

    Returns:
        The banner + blocks, or an empty string when the section is empty.
    """
    if not texts:
        return ""
    title = _SECTION_TITLES[section]
    separator = "\n\n" if section == "const" else "\n\n\n"
    body = separator.join(t.rstrip() for t in texts)
    return f"{_HEADER_RULE}\n# {title}\n{_HEADER_RULE}\n\n{body}"


def _write_section_files(sections: dict[str, list[str]], workdir: Path) -> list[Path]:
    """Write the temporary ``const.py``/``classes.py``/``func.py`` files.

    This is the on-disk half of the extraction step: every section is first
    saved to its own file inside ``workdir`` so it can be inspected/merged, and
    the files are removed again by :func:`_merge_body` once merged.

    Args:
        sections: Section key -> rendered block texts.
        workdir: Directory to create the temporary files in (usually the
            output directory).

    Returns:
        The written paths, in merge order (const, classes, func).
    """
    written: list[Path] = []
    for section in ("const", "class", "func"):
        path = workdir / _SECTION_FILES[section]
        path.write_text(
            _section_source(section, sections[section]) + "\n", encoding="utf-8"
        )
        written.append(path)
    return written


def _merge_body(header: str, section_paths: Sequence[Path], entry_text: str) -> str:
    """Merge the header, section files and entry points into one module body.

    The section files are read back (so the merge really happens from the
    files created by :func:`_write_section_files`) and then deleted.

    Args:
        header: Module docstring, optimized imports and guards.
        section_paths: Temporary section files, in merge order.
        entry_text: Rendered entry-point blocks (``__main__`` guards / mains).

    Returns:
        The assembled module source without the ``__all__`` section.
    """
    parts: list[str] = [header.rstrip()] if header.strip() else []
    try:
        for path in section_paths:
            try:
                text = path.read_text(encoding="utf-8").strip("\n")
            except OSError:
                text = ""
            if text:
                parts.append(text)
    finally:
        for path in section_paths:
            try:
                path.unlink()
            except OSError:  # pragma: no cover - best effort cleanup
                pass
    if entry_text.strip():
        parts.append(
            f"{_HEADER_RULE}\n# Entry points\n{_HEADER_RULE}\n\n"
            f"{entry_text.strip()}"
        )
    return "\n\n\n".join(parts).rstrip() + "\n"


def _autoall_names(text: str) -> list[str]:
    """Return the ``__all__`` names ``autoall`` would generate for ``text``.

    Args:
        text: Module source (without an ``__all__`` block).

    Returns:
        Sorted public names, or an empty list when ``autoall`` is unavailable.

    Raises:
        Exception: Any unexpected failure inside ``autoall`` is propagated to
            the caller, which logs it as a warning.
    """
    import tempfile

    from autoall import ensure_all_file  # type: ignore

    with tempfile.NamedTemporaryFile(
        "w", suffix=".py", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(text)
        tmp_name = handle.name
    try:
        return ensure_all_file(tmp_name, persist=False)
    finally:
        Path(tmp_name).unlink(missing_ok=True)


def _render_final(
    body_text: str,
    target: Path,
    *,
    eol: str,
    do_format: bool,
    formatter: str,
) -> tuple[str, bool]:
    """Write ``body_text`` to ``target`` and let ``autoall`` add ``__all__``.

    The file is created first (docstring, optimized imports, constants,
    classes, functions, entry points) and :func:`autoall.ensure_all_file` then
    updates it **in place** with the ``__all__`` section. When formatting is
    requested the formatted result is written back. Reading the file at the
    end guarantees the returned text is exactly what is on disk, so
    ``--check``/``--stdout``/``--diff`` agree with a real run.

    Args:
        body_text: Module source without the ``__all__`` block (LF endings).
        target: File to write (the real output file, or a temporary file for
            dry runs).
        eol: ``"lf"``, ``"crlf"`` or ``"preserve"``-resolved ending.
        do_format: Run black/ruff over the final file.
        formatter: Formatter name for ``do_format``.

    Returns:
        ``(text, all_added)`` where ``text`` is the final file content and
        ``all_added`` reports whether an ``__all__`` section is present.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_apply_eol(body_text, eol), encoding="utf-8", newline="")
    all_added = False
    try:
        from autoall import ensure_all_file  # type: ignore

        names = ensure_all_file(str(target), persist=True)
        all_added = bool(names)
    except ImportError:
        logger.info("autoall not installed — skipping __all__ generation")
    except Exception as exc:  # noqa: BLE001
        logger.warning("autoall: could not add __all__ section: {}", exc)
    with open(target, encoding="utf-8", newline="") as handle:
        text = handle.read()
    if do_format:
        formatted = format_source(text, formatter)
        if formatted != text:
            text = _apply_eol(formatted, eol)
            target.write_text(text, encoding="utf-8", newline="")
    return text, all_added


def _cleanup_stale_outputs(out_dir: Path, keep: Path, source: Path) -> list[Path]:
    """Delete leftovers so the output directory holds a single file.

    Only files directly inside ``out_dir`` are considered, never
    sub-directories (they hold copied data files), and nothing is touched when
    the output directory *is* the package directory itself.

    Args:
        out_dir: Output directory.
        keep: The file that was just generated.
        source: Package directory being consolidated.

    Returns:
        Paths that were removed.
    """
    try:
        if out_dir.resolve() == source.resolve():
            return []
    except OSError:  # pragma: no cover - defensive
        return []
    removed: list[Path] = []
    legacy = out_dir.name in {"consolidated", "output", "output_single"}
    for entry in out_dir.iterdir():
        if entry == keep or not entry.is_file():
            continue
        stale = entry.name in _TEMP_SECTION_NAMES
        if not stale and entry.name in _LEGACY_LAYOUT_NAMES and legacy:
            stale = True
        if not stale and entry.suffix == ".py":
            try:
                stale = entry.read_text(encoding="utf-8", errors="replace").startswith(
                    _GENERATED_PREFIX
                )
            except OSError:
                stale = False
        if stale:
            try:
                entry.unlink()
                removed.append(entry)
                logger.info("Removed stale output {}", entry)
            except OSError as exc:  # pragma: no cover - best effort
                logger.warning("Could not remove stale output {}: {}", entry, exc)
    return removed


def _unified_diff(old: str, new: str, name: str) -> str:
    """Return a unified diff between two module versions.

    Args:
        old: Existing file content.
        new: Content that would be written.
        name: File name used in the diff headers.

    Returns:
        The diff text (empty when the contents are equal).
    """
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{name}",
            tofile=f"b/{name}",
        )
    )


def consolidate_package(
    source: Path | str = ".",
    output: Path | None = None,
    *,
    workers: int | None = None,
    executor: str = "process",
    exclude: tuple[str, ...] = (),
    include: tuple[str, ...] = (),
    keep_docstrings: bool = True,
    strip_comments: bool = False,
    keep_comments: bool = True,
    rename_strategy: str = "prefix",
    dedupe: str = "safe",
    imports_mode: str = "prune",
    all_mode: str = "ignore",
    entry: tuple[str, ...] = (),
    entry_keep_all: bool = False,
    main_name: str | None = None,
    generate_guard: bool = False,
    keep_unused_imports: bool = False,
    respect_all: bool = False,
    fail_on_warning: bool = False,
    max_skipped: int | None = None,
    allow_syntax_errors: bool = False,
    no_parallel: bool = False,
    progress: bool = False,
    dry_run: bool = False,
    check: bool = False,
    validate: bool = False,
    validate_import: bool = False,
    stdout: bool = False,
    copy_data: bool = False,
    data_glob: tuple[str, ...] = (),
    strict: bool = False,
    eol: str = "lf",
    namespace: bool = False,
    preserve_modules: bool = False,
    source_map: bool = False,
    diff: bool = False,
    do_format: bool = False,
    formatter: str = "black",
) -> ConsolidationResult:
    """Consolidate a package into a single ``consolidated/<pkgname>.py`` file.

    Implements the ``pyrefactor <pkg path>`` contract:

    * the package name is detected from packaging metadata
      (``pyproject.toml``/``setup.cfg``/``setup.py``) in the package folder
      and up to two directory levels above it (covering ``src/<pkgname>``
      layouts); when nothing is found, the folder that was pointed at (``.``
      by default) gives the name. Names are normalised to a valid python
      identifier, so ``my-dist`` becomes ``my_dist``;
    * a ``consolidated`` subdirectory is created inside the package directory
      (or at ``output`` when given) containing a single ``<pkgname>.py``; any
      stale file left by an earlier run is removed;
    * constants, functions and classes are extracted from the whole package,
      name collisions are resolved by the resolver, and each group is first
      saved to a temporary file (``const.py``, ``func.py``, ``classes.py``);
    * imports are extracted, de-duplicated, local (relative) imports stripped
      by the resolver, and the optimized import section is written first to
      ``consolidated/<pkgname>.py``;
    * the temporary ``const.py``/``classes.py``/``func.py`` contents are
      appended and the temporary files are removed;
    * :mod:`autoall` then updates the file **in place**, appending a
      marker-delimited ``__all__`` section.

    Args:
        source: Package directory to consolidate (a single ``.py`` file is
            also accepted); defaults to the current directory.
        output: Output directory; defaults to ``source / "consolidated"``.
            The single ``<pkgname>.py`` file is written inside it.
        workers: Number of parsing workers; defaults to the CPU count.
        executor: ``"process"`` or ``"thread"`` pool for parsing.
        exclude: Additional glob patterns to skip.
        include: Glob patterns that re-include otherwise excluded files.
        keep_docstrings: Keep docstrings in the output.
        strip_comments: Drop standalone comment lines.
        keep_comments: Keep comments at all.
        rename_strategy: Collision policy: ``prefix``, ``suffix``,
            ``module_path`` or ``error``.
        dedupe: Duplicate removal: ``none``, ``safe`` or ``aggressive``.
        imports_mode: Import handling: ``prune``, ``keep`` or ``strict``.
        all_mode: ``__all__`` handling before autoall runs: ``ignore``,
            ``respect``, ``merge`` or ``recompute``.
        entry: Modules whose ``__main__`` guard should be kept.
        entry_keep_all: Keep every ``__main__`` guard instead of filtering.
        main_name: Rename the consolidated ``main`` function to this name.
        generate_guard: Synthesize a ``__main__`` guard when none exists.
        keep_unused_imports: Deprecated alias for ``imports_mode="keep"``.
        respect_all: Deprecated alias for ``all_mode="respect"``.
        fail_on_warning: Fail when any warning was emitted.
        max_skipped: Fail when more than this many files were skipped.
        allow_syntax_errors: Parse files with syntax errors partially.
        no_parallel: Force single-process parsing.
        progress: Show a tqdm progress bar while parsing.
        dry_run: Show the plan and stats without writing files.
        check: Fail when the generated file would differ from the existing one.
        validate: Compile the generated file.
        validate_import: With ``validate``, also import the generated module.
        stdout: Write the generated source to stdout instead of files.
        copy_data: Copy non-Python data files next to the output.
        data_glob: Only copy data files matching these globs.
        strict: Promote risky warnings and import cycles to hard errors.
        eol: Line endings for generated files: ``lf``, ``crlf`` or ``preserve``.
        namespace: Emit module namespace comments per block.
        preserve_modules: Emit per-block origin comments.
        source_map: Emit ``file:line`` comments per block.
        diff: Emit a unified diff against the existing output file.
        do_format: Run black/ruff over the generated file.
        formatter: ``"black"`` (default) or ``"ruff"``.

    Returns:
        A :class:`ConsolidationResult` describing what was written.

    Raises:
        ConsolidationError: When the input cannot be parsed or the plan cannot
            be built safely.
    """
    source = Path(source)
    output = Path(output) if output is not None else None
    if not source.exists():
        raise ConsolidationError(f"source does not exist: {source}")
    if source.is_file():
        pkg_name = normalize_pkg_name(source.stem)
        source_dir = source.parent
        out_dir = Path(output) if output is not None else source_dir / "consolidated"
    else:
        pkg_name = detect_pkg_name(source)
        source_dir = source
        out_dir = Path(output) if output is not None else source / "consolidated"
    if out_dir.exists() and out_dir.is_file():
        raise ConsolidationError(
            f"output path is a file, expected a directory: {out_dir}"
        )

    files = discover(source, exclude=exclude, include=include, skip_dirs=[out_dir])
    if not files:
        raise ConsolidationError(f"no python files found under {source}")
    if eol == "preserve":
        eol = _detect_eol(files)
    data_files = discover_data_files(
        source,
        exclude=exclude,
        include=include,
        skip_dirs=[out_dir],
        copy_data=copy_data,
        data_glob=data_glob,
    )
    workers = 1 if no_parallel else (workers or os.cpu_count() or 1)

    logger.info("Parsing {} files for package {!r}", len(files), pkg_name)
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
        for warning in info.warnings:
            logger.warning(warning)
            warnings_acc.append(warning)
        if info.errors:
            for error in info.errors:
                logger.error(error)
            skipped.append(info.path)
            continue
        modules[name] = info
    if not modules:
        raise ConsolidationError("no module could be parsed")
    if max_skipped is not None and len(skipped) > max_skipped:
        raise ConsolidationError(
            f"too many skipped files: {len(skipped)} > --max-skipped {max_skipped}"
        )
    if fail_on_warning and warnings_acc:
        raise ConsolidationError(
            f"fail-on-warning: {len(warnings_acc)} warning(s) emitted"
        )

    resolver = Resolver(
        modules,
        keep_unused_imports=keep_unused_imports,
        respect_all=respect_all,
        entry=tuple(entry),
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

    # --- Split the plan into extraction sections -------------------------
    sections: dict[str, list[str]] = {"const": [], "class": [], "func": []}
    entry_texts: list[str] = []
    for block in plan.blocks:
        if block.section == "entry":
            entry_texts.append(block.text)
        elif block.section in sections:
            sections[block.section].append(block.text)

    # --- Header: docstring, optimized imports (local imports stripped by
    # --- the resolver) and import guards --------------------------------
    header_parts: list[str] = [
        f'"""Consolidated from ``{pkg_name}`` by pyrefactor (generated file)."""'
    ]
    if plan.future_names:
        header_parts.append(future_section(plan.future_names))
    if plan.imports_text:
        header_parts.append(plan.imports_text)
    if plan.guards:
        header_parts.append("\n\n".join(plan.guards))
    header = "\n\n\n".join(header_parts)
    entry_text = "\n\n\n".join(t.strip() for t in entry_texts)
    exports = sum(len(names) for names in plan.exports.values())
    target_path = out_dir / f"{pkg_name}.py"

    # --- Render the final file. For dry runs the whole pipeline is executed
    # --- against a scratch directory so the bytes match a real run. -------
    write_output = not (stdout or dry_run or check)
    if write_output:
        out_dir.mkdir(parents=True, exist_ok=True)
        section_paths = _write_section_files(sections, out_dir)
        body_text = _merge_body(header, section_paths, entry_text)
        expected_text, all_added = _render_final(
            body_text,
            target_path,
            eol=eol,
            do_format=do_format,
            formatter=formatter,
        )
    else:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="pyrefactor-") as tmpdir:
            tmp_target = Path(tmpdir) / f"{pkg_name}.py"
            section_paths = _write_section_files(sections, Path(tmpdir))
            body_text = _merge_body(header, section_paths, entry_text)
            expected_text, all_added = _render_final(
                body_text,
                tmp_target,
                eol=eol,
                do_format=do_format,
                formatter=formatter,
            )

    if expected_text.strip():
        try:
            compile(expected_text, f"{pkg_name}.py", "exec")
        except SyntaxError as exc:
            raise ConsolidationError(
                f"generated {pkg_name}.py is not valid python: {exc}"
            ) from exc

    # --- Existing-output comparison (--diff / --check) --------------------
    existing_text = ""
    if target_path.is_file():
        try:
            with open(target_path, encoding="utf-8", newline="") as handle:
                existing_text = handle.read()
        except OSError:
            existing_text = ""
    diff_text = ""
    if (diff or check) and existing_text != expected_text:
        diff_text = _unified_diff(existing_text, expected_text, target_path.name)

    if stdout:
        sys.stdout.write(expected_text)
        logger.success(
            "{} modules -> stdout ({} exported names)", len(modules), exports
        )
        return ConsolidationResult(
            output_dir=out_dir,
            files=[],
            modules=len(modules),
            skipped=skipped,
            exports=exports,
            warnings=warnings_acc,
            diff=diff_text,
            stdout_text=expected_text,
            all_added=all_added,
            dry_run=True,
            plan=plan,
            pkg_name=pkg_name,
        )

    if dry_run or check:
        if check:
            if not target_path.is_file():
                raise ConsolidationError(
                    f"check failed: {target_path} does not exist "
                    "(run without --check to generate it)"
                )
            if diff_text:
                raise ConsolidationError(
                    f"check failed: output would change ({target_path})\n{diff_text}"
                )
            logger.success("Check passed: {} up to date", target_path)
        else:
            logger.info(
                "Dry-run: would write {} modules to {} ({} exported names)",
                len(modules),
                target_path,
                exports,
            )
        return ConsolidationResult(
            output_dir=out_dir,
            files=[],
            modules=len(modules),
            skipped=skipped,
            exports=exports,
            warnings=warnings_acc,
            diff=diff_text,
            all_added=all_added,
            dry_run=True,
            plan=plan,
            pkg_name=pkg_name,
        )

    if diff and diff_text:
        print(diff_text)
        logger.info("Diff emitted ({} chars)", len(diff_text))

    removed = _cleanup_stale_outputs(out_dir, target_path, source_dir)
    if removed:
        logger.debug("Removed {} stale file(s) from {}", len(removed), out_dir)

    # --- Optional validation --------------------------------------------
    validated = False
    if validate:
        try:
            with open(target_path, encoding="utf-8", newline="") as handle:
                compile(handle.read(), str(target_path), "exec")
            validated = True
            logger.success("Validation compile passed")
        except SyntaxError as exc:
            raise ConsolidationError(
                f"validate: {target_path} failed to compile: {exc}"
            ) from exc
        if validate_import and validated:
            try:
                import importlib.util

                spec = importlib.util.spec_from_file_location(
                    f"_consolidated_validate_{pkg_name}", str(target_path)
                )
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)  # type: ignore[union-attr]
                    logger.success("Validation import succeeded for {}", target_path)
            except Exception as exc:  # noqa: BLE001
                logger.error("Validation import failed: {}", exc)
                raise ConsolidationError(f"validate import failed: {exc}") from exc

    # --- Copy data files next to the output ------------------------------
    copied: list[Path] = []
    for data_file in data_files:
        # Paths are relative to the package directory (not its parent) so that
        # ``Path(__file__).parent / "data/..."`` keeps working from the
        # consolidated module.
        try:
            relative = data_file.path.resolve().relative_to(source_dir.resolve())
        except (ValueError, OSError):
            relative = Path(data_file.relative)
        destination = out_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            import shutil

            shutil.copy2(data_file.path, destination)
            copied.append(destination)
            logger.info("Copied data file {} -> {}", data_file.path, destination)
        except OSError as exc:
            logger.error("Failed to copy data file {}: {}", data_file.path, exc)
            if fail_on_warning:
                raise ConsolidationError(f"data copy failed: {exc}") from exc

    return ConsolidationResult(
        output_dir=out_dir,
        files=[target_path],
        modules=len(modules),
        skipped=skipped,
        exports=exports,
        warnings=warnings_acc,
        diff=diff_text,
        copied_data_files=copied,
        validated=validated,
        dry_run=False,
        plan=plan,
        all_added=all_added,
        pkg_name=pkg_name,
    )
