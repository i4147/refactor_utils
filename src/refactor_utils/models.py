"""Data models shared across the consolidator.

The 3.x series is built on **libcst**: blocks hold their concrete-syntax-tree
statement nodes instead of raw source bytes with byte-offset occurrences.
Rewrites are node-level CST transformations, so there is no offset bookkeeping
left to get out of sync.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import libcst as cst

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


@dataclass
class Ref:
    """A reference inside a block that may need rewriting or ordering.

    ``kind`` is ``"name"`` (a bare identifier), ``"attr"`` (the attribute of a
    dotted chain rooted at ``obj``) or ``"nested_import"`` (an import statement
    inside the block). ``eager`` marks references that evaluate at import time
    (module level, class bodies, decorators, parameter defaults) and therefore
    constrain the emit order.
    """

    name: str
    kind: str
    eager: bool = True
    obj: str = ""
    specs: tuple[ImportSpec, ...] = ()


@dataclass(eq=False)
class Block:
    """A chunk of a module (class, function, const, ...).

    ``nodes`` holds the libcst statement nodes that make up the block,
    including their ``leading_lines`` so attached comments and blank lines
    travel with the block.
    """

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
        """Rendered source of the block (lossless, incl. leading comments)."""
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
