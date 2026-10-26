"""Plain, picklable data structures shared by the parser and the resolver."""

from __future__ import annotations
from dataclasses import dataclass, field


class ResolverError(RuntimeError):
    """Raised when a consolidation plan cannot be built without breaking code.

    Lives here (rather than in ``resolver``) so that ``ordering`` can raise it
    without importing the resolver module.
    """


CONST = "const"
CLASS = "class"
FUNC = "func"
MISC = "misc"
MAIN = "main"


@dataclass(frozen=True)
class ImportSpec:
    """A single import as written in the source.

    Attributes:
        module: Dotted module the name is imported from (resolved to an
            absolute name for relative imports).
        name: Imported symbol, or ``None`` for ``import module``.
        alias: ``as`` alias, if any.
        is_from: Whether the statement was a ``from ... import ...``.
        star: Whether the statement was a star import.
        relative: Whether the statement used a relative (``.``) module.

    """

    module: str
    name: str | None = None
    alias: str | None = None
    is_from: bool = False
    star: bool = False
    relative: bool = False

    @property
    def local_name(self) -> str | None:
        """Name this import binds in the importing module.

        Returns:
            The bound name, or ``None`` for star imports (which bind many).

        """
        if self.star:
            return None
        if self.is_from:
            return self.alias or self.name
        return self.alias or self.module.split(".")[0]


@dataclass(slots=True)
class Occ:
    """A renameable occurrence inside a block.

    Offsets are relative to the start of the block source, which makes blocks
    relocatable and safe to merge.

    Attributes:
        start: Start byte offset inside the block.
        end: End byte offset inside the block.
        name: Referenced name.
        kind: ``"name"``, ``"attr"`` or ``"nested_import"``.
        eager: Whether the reference is evaluated at import time.
        obj: For ``"attr"`` occurrences, the dotted object expression.
        specs: For ``"nested_import"`` occurrences, the parsed imports.

    """

    start: int
    end: int
    name: str
    kind: str
    eager: bool = True
    obj: str = ""
    specs: tuple[ImportSpec, ...] = ()


@dataclass(eq=False)
class Block:
    """A top-level, relocatable chunk of source.

    Attributes:
        module: Module the block came from.
        kind: One of ``const``, ``class``, ``func``, ``misc`` or ``main``.
        names: Names bound by the block (the primary name comes first).
        source: Raw block source bytes.
        occs: Occurrences that may need rewriting.
        order: Position within the originating module.
        bid: Stable block id assigned by the resolver.
        orig_path: Source file path, used by source maps.
        start_line: First source line of the block.
        end_line: Last source line of the block.
        stripped_docstring: Whether docstrings were stripped from the source.

    """

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
        """Primary (first) name bound by the block.

        Returns:
            The first name, or an empty string for unnamed blocks.

        """
        return self.names[0] if self.names else ""

    def merge(self, other: "Block") -> None:
        """Append ``other`` to this block, shifting its byte offsets accordingly.

        Used when a module defines the same top-level name more than once
        (e.g. an ``@overload`` group followed by its implementation, or a
        constant that is reassigned): both fragments are kept together so the
        group is renamed and emitted atomically.

        Args:
            other: The block to append. Mutated in place (its occurrence
                offsets are shifted); it is not re-used afterwards.

        """
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
    """A verbatim ``try/except ImportError`` (or similar) import guard.

    Attributes:
        text: The guard exactly as it appeared in the source.
        specs: Import specifications found inside the guard.
        internal: Whether any of ``specs`` point at a project module. Internal
            imports cannot survive verbatim in a consolidated file, so the
            resolver hoists them out of the guard.
        spec_lines: ``(spec, verbatim statement text)`` pairs, used to remove
            exactly the hoisted statements from ``text``.

    """

    text: str
    specs: list[ImportSpec]
    internal: bool = False
    spec_lines: list[tuple[ImportSpec, str]] = field(default_factory=list)


@dataclass
class ModuleInfo:
    """Everything the parser extracted from a single source file.

    Attributes:
        module: Dotted module name the file maps to.
        path: Filesystem path the module was read from.
        is_pkg: Whether the file is a package ``__init__.py``.
        imports: Module-level import specifications.
        blocks: Top-level, renameable/orderable source blocks.
        guards: Import guards kept verbatim (``try/except ImportError``).
        declared_all: Literal contents of ``__all__``, or ``None`` when the
            module does not declare one. An explicit empty list is preserved.
        future_annotations: Whether the module uses
            ``from __future__ import annotations``.
        future_names: All ``__future__`` features requested by the module.
        encoding: Encoding detected from the source (PEP 263), default utf-8.
        warnings: Human readable warnings collected during parsing.
        errors: Fatal problems; modules with errors are skipped.

    """

    module: str
    path: str
    is_pkg: bool
    imports: list[ImportSpec] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    guards: list[GuardBlock] = field(default_factory=list)
    declared_all: list[str] | None = None
    future_annotations: bool = False
    future_names: list[str] = field(default_factory=list)
    encoding: str = "utf-8"
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SourceFile:
    """A discovered python source file.

    Attributes:
        path: Filesystem path of the file.
        module: Dotted module name the file maps to.
        is_pkg: Whether the file is a package ``__init__.py``.

    """

    path: "object"
    module: str
    is_pkg: bool


@dataclass(frozen=True)
class DataFile:
    """Non-Python data file discovered alongside the source tree.

    Attributes:
        path: Filesystem path of the data file.
        relative: Path relative to the source base, used for output placement.
        kind: Classification of the file (``"data"`` by default).

    """

    path: "object"
    relative: str  # relative to source base, for output placement
    kind: str = "data"


@dataclass
class ConsolidationOptions:
    """Picklable bundle of user options that affect parser/resolver/writer.

    Kept in models.py so workers can receive it if needed; but currently
    most flags are handled in the main process (resolver/writer).

    Attributes:
        keep_docstrings: Whether docstrings survive into the output.
        strip_comments: Whether standalone comment lines are dropped.
        keep_comments: Whether comments are kept at all.
        allow_syntax_errors: Parse files with syntax errors partially.
        rename_strategy: Collision policy (``prefix``/``suffix``/
            ``module_path``/``error``).
        dedupe: Duplicate removal mode (``none``/``safe``/``aggressive``).
        imports_mode: Import handling (``prune``/``keep``/``strict``).
        all_mode: ``__all__`` handling (``ignore``/``respect``/``merge``/
            ``recompute``).
        namespace: Emit module namespace comments.
        preserve_modules: Emit origin comments per block.
        source_map: Emit ``file:line`` comments per block.
        entry_keep_all: Keep every ``__main__`` guard.
        main_name: Name to use for the ``main`` entry point.
        generate_guard: Synthesize a ``__main__`` guard when missing.
        strict: Promote risky warnings to errors.
        eol: Line ending policy (``lf``/``crlf``/``preserve``).

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
    strict: bool = False
    eol: str = "lf"  # lf|crlf|preserve
