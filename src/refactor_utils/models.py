"""Plain, picklable data structures shared by the parser and the resolver."""

from __future__ import annotations
from dataclasses import dataclass, field

CONST = "const"
CLASS = "class"
FUNC = "func"
MISC = "misc"
MAIN = "main"


@dataclass(frozen=True)
class ImportSpec:
    module: str
    name: str | None = None
    alias: str | None = None
    is_from: bool = False
    star: bool = False
    relative: bool = False

    @property
    def local_name(self) -> str | None:
        if self.star:
            return None
        if self.is_from:
            return self.alias or self.name
        return self.alias or self.module.split(".")[0]


@dataclass(slots=True)
class Occ:
    start: int
    end: int
    name: str
    kind: str
    eager: bool = True
    obj: str = ""
    specs: tuple[ImportSpec, ...] = ()


@dataclass(eq=False)
class Block:
    module: str
    kind: str
    names: list[str]
    source: bytes
    occs: list[Occ]
    order: int
    bid: int = -1
    # extended fields for source-map / preserve-modules (picklable)
    orig_path: str = ""
    start_line: int = 0
    end_line: int = 0
    stripped_docstring: bool = False

    @property
    def primary(self) -> str:
        return self.names[0] if self.names else ""

    def merge(self, other: "Block") -> None:
        shift = len(self.source) + 1
        for occ in other.occs:
            occ.start += shift
            occ.end += shift
        self.source += b"\n" + other.source
        self.occs.extend(other.occs)
        for name in other.names:
            if name not in self.names:
                self.names.append(name)


@dataclass
class GuardBlock:
    text: str
    specs: list[ImportSpec]


@dataclass
class ModuleInfo:
    module: str
    path: str
    is_pkg: bool
    imports: list[ImportSpec] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    guards: list[GuardBlock] = field(default_factory=list)
    declared_all: list[str] | None = None
    future_annotations: bool = False
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SourceFile:
    path: "object"
    module: str
    is_pkg: bool


@dataclass(frozen=True)
class DataFile:
    """Non-Python data file discovered alongside the source tree."""

    path: "object"
    relative: str  # relative to source base, for output placement
    kind: str = "data"


@dataclass
class ConsolidationOptions:
    """Picklable bundle of user options that affect parser/resolver/writer.

    Kept in models.py so workers can receive it if needed; but currently
    most flags are handled in the main process (resolver/writer).
    """

    keep_docstrings: bool = True
    strip_comments: bool = False
    keep_comments: bool = True
    allow_syntax_errors: bool = False
    rename_strategy: str = "prefix"  # prefix|suffix|module_path|error
    dedupe: str = "safe"  # none|safe|aggressive
    imports_mode: str = "prune"  # prune|keep|strict
    all_mode: str = "ignore"  # ignore|respect|merge|recompute
    namespace: bool = False
    preserve_modules: bool = False
    source_map: bool = False
    entry_keep_all: bool = False
    main_name: str | None = None
    generate_guard: bool = False
