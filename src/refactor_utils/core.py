"""Orchestration: discover -> parse (parallel) -> resolve -> write."""

from __future__ import annotations
import os
from concurrent.futures import BrokenExecutor, Executor, ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence
from loguru import logger
from .discovery import discover
from .models import ModuleInfo, SourceFile
from .parser import parse_module_file
from .resolver import Plan, Resolver
from .writer import render_consolidated, render_init


class ConsolidationError(RuntimeError):
    pass


@dataclass
class ConsolidationResult:
    output_dir: Path
    files: list[Path]
    modules: int
    skipped: list[str] = field(default_factory=list)
    exports: int = 0


def _parse_all(files: Sequence[SourceFile], workers: int, executor: str) -> dict[str, ModuleInfo]:
    results: dict[str, ModuleInfo] = {}

    def run_pool(pool_cls: Callable[..., Executor], todo: Sequence[SourceFile]) -> None:
        with pool_cls(max_workers=workers) as pool:
            futures = {pool.submit(parse_module_file, str(f.path), f.module, f.is_pkg): f for f in todo}
            for fut in as_completed(futures):
                src = futures[fut]
                try:
                    results[src.module] = fut.result()
                except BrokenExecutor:
                    raise
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to parse {}", src.path)

    if workers <= 1 or len(files) <= 1:
        for f in files:
            try:
                results[f.module] = parse_module_file(str(f.path), f.module, f.is_pkg)
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


def consolidate(
    source: Path,
    output: Path,
    *,
    mode: str = "single",
    workers: int | None = None,
    executor: str = "process",
    exclude: tuple[str, ...] = (),
    keep_unused_imports: bool = False,
    respect_all: bool = False,
    entry: tuple[str, ...] = (),
) -> ConsolidationResult:
    if mode != "single":
        raise ConsolidationError(f"unknown mode {mode!r}")
    source, output = Path(source), Path(output)
    if not source.exists():
        raise ConsolidationError(f"source does not exist: {source}")
    workers = workers or os.cpu_count() or 1
    files = discover(source, exclude=exclude, skip_dirs=[output])
    if not files:
        raise ConsolidationError(f"no python files found under {source}")
    logger.info("Parsing {} files with {} {} worker(s)", len(files), workers, executor)
    parsed = _parse_all(files, workers, executor)
    modules: dict[str, ModuleInfo] = {}
    skipped: list[str] = []
    for name, info in parsed.items():
        for w in info.warnings:
            logger.warning(w)
        if info.errors:
            for e in info.errors:
                logger.error(e)
            skipped.append(info.path)
            continue
        modules[name] = info
    if not modules:
        raise ConsolidationError("no module could be parsed")
    resolver = Resolver(
        modules, keep_unused_imports=keep_unused_imports, respect_all=respect_all, entry=entry, workers=workers
    )
    plan: Plan = resolver.build()
    source_name = source.resolve().name if source.is_dir() else source.name
    mod_text = render_consolidated(plan, source_name)
    init_text = render_init(plan, source_name)
    for fname, text in (("_consolidated.py", mod_text), ("__init__.py", init_text)):
        try:
            compile(text, fname, "exec")
        except SyntaxError as exc:
            logger.error("Generated {} is not valid python: {} (line {})", fname, exc.msg, exc.lineno)
    output.mkdir(parents=True, exist_ok=True)
    targets = [(output / "_consolidated.py", mod_text), (output / "__init__.py", init_text)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda t: t[0].write_text(t[1], encoding="utf-8"), targets))
    exports = sum(len(v) for v in plan.exports.values())
    return ConsolidationResult(output, [p for p, _ in targets], len(modules), skipped, exports)
