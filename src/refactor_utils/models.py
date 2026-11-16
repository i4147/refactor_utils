from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

CONST = "const"
CLASS = "class"
FUNC = "func"
MISC = "misc"
MAIN = "main"


@dataclass(frozen=True)
class ImportSpec:
    module: str
    name: Optional[str] = None
    alias: Optional[str] = None
    is_from: bool = False
    star: bool = False
    relative: bool = False
    type_checking: bool = False

    @property
    def local_name(self) -> Optional[str]:
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
    kind: str  # "name" | "attr" | "nested_import"
    eager: bool = True
    obj: str = ""
    specs: tuple = ()


@dataclass(eq=False)
class Block:
    module: str
    kind: str
    names: list
    source: bytes
    occs: list
    order: int
    bid: int = -1
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
    specs: list = field(default_factory=list)
    bound_names: list = field(default_factory=list)
    conditional: bool = False


@dataclass
class ModuleInfo:
    module: str
    path: str
    is_pkg: bool = False
    imports: list = field(default_factory=list)
    blocks: list = field(default_factory=list)
    guards: list = field(default_factory=list)
    declared_all: Optional[list] = None
    future_annotations: bool = False
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)


@dataclass(frozen=True)
class SourceFile:
    path: Path
    module: str
    is_pkg: bool = False


@dataclass(frozen=True)
class DataFile:
    path: Path
    relative: str
    kind: str = "data"
