"""Plain, picklable data structures shared by the parser and the resolver."""

from __future__ import annotations

from dataclasses import dataclass, field

CONST = "const"
CLASS = "class"
FUNC = "func"
MISC = "misc"  # top-level statements that are not plain definitions
MAIN = "main"  # ``if __name__ == "__main__":`` blocks


@dataclass(frozen=True)
class ImportSpec:
    """One imported binding (``import a.b as c`` / ``from a import b as c``)."""

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
    """A rewritable occurrence inside a block (byte offsets relative to the block).

    kind:
      * ``name``          - a bare identifier reference/definition
      * ``attr``          - ``a.b.c`` attribute chain (``obj`` = ``a.b``, ``name`` = ``c``)
      * ``nested_import`` - an import statement inside a function/class body
    """

    start: int
    end: int
    name: str
    kind: str
    eager: bool = True  # evaluated at import time (not inside a function body)
    obj: str = ""  # dotted object for attr; indentation for nested_import
    specs: tuple[ImportSpec, ...] = ()


@dataclass(eq=False)
class Block:
    """A top-level unit of code: a constant, class, function or statement."""

    module: str
    kind: str
    names: list[str]
    source: bytes
    occs: list[Occ]
    order: int
    bid: int = -1

    @property
    def primary(self) -> str:
        return self.names[0] if self.names else ""

    def merge(self, other: "Block") -> None:
        """Append another block that (re)binds the same name (overloads, ``X += 1`` ...)."""
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
    """A ``try/except ImportError`` style block kept verbatim in the import section."""

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
    path: "object"  # pathlib.Path (kept loose to stay trivially picklable)
    module: str
    is_pkg: bool
