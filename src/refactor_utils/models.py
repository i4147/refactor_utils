"""Data models shared across the consolidator.

NOTE: every dataclass below declares its fields with full annotations.
A previous revision lost the annotations, which silently turned all
``field(...)`` defaults into plain class attributes and made the dataclasses
accept zero constructor arguments (every ``ImportSpec(...)``, ``Block(...)``,
``ConsolidationResult(...)`` call crashed).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Block kinds
CONST = "const"
CLASS = "class"
FUNC = "func"
MISC = "misc"
MAIN = "main"


@dataclass(frozen=True)
class ImportSpec:
    """One parsed import statement entry."""

    module: str
    name: str | None = None
    alias: str | None = None
    is_from: bool = False
    star: bool = False
    relative: bool = False
    type_checking: bool = False

    @property
    def local_name(self) -> str | None:
        if self.star:
            return None
        if self.is_from:
            return self.alias or self.name
        return self.alias or self.module.split(".")[0]


@dataclass(slots=True)
class Occ:
    """A reference inside a block source that may need rewriting."""

    start: int
    end: int
    name: str
    kind: str  # "name" | "attr" | "nested_import"
    eager: bool = True
    obj: str = ""
    specs: tuple[ImportSpec, ...] = ()


@dataclass(eq=False)
class Block:
    """A contiguous chunk of a module (class, function, const, ...)."""

    module: str
    kind: str
    names: list[str]
    source: bytes
    occs: list[Occ] = field(default_factory=list)
    order: int = 0
    bid: int = -1
    orig_path: str = ""
    start_line: int = 0
    end_line: int = 0
    stripped_docstring: bool = False

    @property
    def primary(self) -> str:
        return self.names[0] if self.names else ""

    def merge(self, other: Block) -> None:
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
    """A module-level conditional (try/if) import guard kept verbatim."""

    text: str
    specs: list[ImportSpec] = field(default_factory=list)
    bound_names: list[str] = field(default_factory=list)
    conditional: bool = False


@dataclass
class ModuleInfo:
    """Parsed representation of one source module."""

    module: str
    path: str
    is_pkg: bool = False
    imports: list[ImportSpec] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    guards: list[GuardBlock] = field(default_factory=list)
    declared_all: list[str] | None = None
    future_annotations: bool = False
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SourceFile:
    """A discovered Python source file."""

    path: Path
    module: str
    is_pkg: bool = False


@dataclass(frozen=True)
class DataFile:
    """A discovered non-Python (data) file."""

    path: Path
    relative: str
    kind: str = "data"
