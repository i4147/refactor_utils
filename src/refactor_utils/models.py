from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import libcst as cst

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
    type_checking: bool = False

    @property
    def local_name(self) -> str | None:
        if self.star:
            return None
        if self.is_from:
            return self.alias or self.name
        return self.alias or self.module.split(".")[0]


@dataclass
class Ref:
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
    nodes: list = field(default_factory=list)
    refs: list[Ref] = field(default_factory=list)
    order: int = 0
    bid: int = -1
    orig_path: str = ""
    start_line: int = 0
    end_line: int = 0

    @property
    def primary(self) -> str:
        return self.names[0] if self.names else ""

    def code(self) -> str:
        if not self.nodes:
            return ""
        return cst.Module(body=list(self.nodes)).code

    @property
    def source(self) -> bytes:
        return self.code().encode("utf-8")

    def merge(self, other: Block) -> None:
        self.nodes.extend(other.nodes)
        self.refs.extend(other.refs)
        for name in other.names:
            if name not in self.names:
                self.names.append(name)


@dataclass
class GuardBlock:
    text: str
    specs: list[ImportSpec] = field(default_factory=list)
    bound_names: list[str] = field(default_factory=list)
    conditional: bool = False


@dataclass
class ModuleInfo:
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
    path: Path
    module: str
    is_pkg: bool = False


@dataclass(frozen=True)
class DataFile:
    path: Path
    relative: str
    kind: str = "data"
