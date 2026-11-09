"""Insert missing docstrings into the project sources (one-off helper).

This is the script that was used during the arena session to document the
project files requested by ``prompt.md`` section 4. It uses :mod:`libcst` so
the existing formatting is preserved (multi-line signatures, one-line
functions are expanded into blocks only where a docstring is inserted).

Usage::

    python arena/add_docstrings.py            # apply
    python arena/add_docstrings.py --check    # report only

The docstring table below is idempotent: running the script twice changes
nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import libcst as cst

ROOT = Path(__file__).resolve().parents[1]

DOCSTRINGS: dict[str, dict[str, str]] = {
    "src/refactor_utils/core.py": {
        "_parse_all": (
            "Parse every discovered file, optionally in parallel, and return "
            "the parsed modules keyed by dotted name."
        ),
        "_parse_all._do_one": "Parse one file in the current process.",
        "_parse_all.run_pool": (
            "Parse ``todo`` with ``pool_cls`` and merge the results into "
            "``results``."
        ),
    },
    "src/refactor_utils/discovery.py": {
        "_is_data_file": (
            "Return ``True`` when ``path`` should be treated as a data file "
            "worth copying."
        ),
        "_walk": (
            "Recursively yield the python files under ``directory``, honouring "
            "the exclude/include precedence."
        ),
    },
    "src/refactor_utils/ordering.py": {
        "topo_order.push": ("Queue block ``bid`` in the heap for its rank tier."),
    },
    "src/refactor_utils/parser.py": {
        "_get_parser": (
            "Return the process-local tree-sitter parser, creating it once."
        ),
        "_t": 'Return the decoded text of a tree-sitter node (``""`` for None).',
        "_resolve_relative": (
            "Resolve a relative import to an absolute dotted module name."
        ),
        "_dotted": ("Return the dotted name of an identifier/attribute node, or None."),
        "_param_names": "Return the parameter names declared by ``params``.",
        "_push_param_exprs": (
            "Push the annotations and default values of ``params`` onto the "
            "dependency stack."
        ),
        "_lhs_names": (
            "Return the names bound by an assignment target, or None when the "
            "target is not a plain name/pattern."
        ),
        "_target_names": ("Return the names bound by a (possibly chained) assignment."),
        "_first_identifier": (
            "Return the first identifier found in ``node`` (best effort)."
        ),
        "_bound_names": (
            "Return the module-scope names bound by a compound statement."
        ),
        "_scan": ("Collect the imports and ``def``/``class`` presence of a subtree."),
        "_extent": ("Return the byte range of a node plus its attached comments."),
        "_first_error_line": (
            "Return the 1-based line of the first syntax error, or 0."
        ),
    },
    "src/refactor_utils/resolver.py": {
        "_ext_local": "Return the local name bound by an external import key.",
        "_ext_identity": "Return the identity of an external import key.",
        "_sanitize": "Turn ``text`` into a safe python identifier fragment.",
        "Resolver._entry_match": (
            "Return ``True`` when ``module`` was selected by ``entry``."
        ),
        "Resolver._is_internal": (
            "Return ``True`` when ``spec`` refers to a module inside the "
            "package being consolidated."
        ),
        "Resolver._prefix": "Return the rename prefix for ``module``.",
        "Resolver._suffix": "Return the rename suffix for ``module``.",
        "Resolver._module_path_prefix": (
            "Return the full dotted path of ``module`` as an identifier " "prefix."
        ),
        "Resolver._star_names": (
            "Return the names exported by a star import from ``module``."
        ),
        "Resolver._spec_target": (
            "Resolve an import specification to a target (definition, module "
            "or external import)."
        ),
        "Resolver._resolve_symbol": (
            "Resolve ``name`` as seen from ``module``, following re-exports."
        ),
        "Resolver._resolve_dotted": (
            "Resolve a dotted attribute chain starting at ``obj``."
        ),
        "Resolver._build_envs": (
            "Build the per-module name environments used for rewriting."
        ),
        "Resolver._analyse": (
            "Collect references and import-time dependencies between blocks."
        ),
        "Resolver._dedupe": ("Merge duplicate blocks according to ``dedupe`` mode."),
        "Resolver._assign_names": (
            "Assign final names to kept blocks, renaming collisions and "
            "main entry points."
        ),
        "Resolver._resolve_ext_conflicts": (
            "Alias external imports whose local names collide."
        ),
        "Resolver._target_name": ("Return the final name a target is bound to."),
        "Resolver._render_block_with_env": (
            "Render ``block`` applying the renames of ``env``."
        ),
        "Resolver._render_block": "Render ``block`` with its module environment.",
        "Resolver._assemble": (
            "Turn the kept blocks into the final plan (ordered blocks and "
            "import section)."
        ),
        "Resolver._exports.is_def_visible": (
            "Return ``True`` when a definition survives renaming in the " "exports map."
        ),
        "Resolver._exports.is_reexport_visible": (
            "Return ``True`` when a re-exported name should be kept in " "``__all__``."
        ),
    },
    "autoall/src/autoall/__init__.py": {
        "_install_block": (
            "Return the two-line ``ensure_all`` snippet appended by " ":func:`install`."
        ),
        "_init_path": (
            "Return the ``__init__.py`` path of ``pkg`` (None for namespace "
            "packages)."
        ),
        "_strip_between": ("Remove the text between ``start`` and ``end`` markers."),
        "_extract_between": (
            "Return the text between two markers, or None when absent."
        ),
        "_parse_all_literal": (
            "Parse a literal ``__all__`` assignment from ``src``, or None."
        ),
        "_has_all_binding": (
            "Return ``True`` when the module already binds ``__all__``."
        ),
        "_public": ("Return ``True`` when ``name`` is part of the public API."),
        "_is_own_module": (
            "Return ``True`` when ``name`` belongs to the ``root`` package."
        ),
        "_relative_target": (
            "Return the absolute dotted target of a ``from . import ...`` " "node."
        ),
        "_star_names": ("Return the public names a ``from .mod import *`` would bind."),
        "_collect_names": (
            "Collect the public names defined or re-exported by ``pkg``."
        ),
        "_collect_names.add": "Record ``name`` if it is public and new.",
        "_collect_names_module.add": (
            "Record ``name`` if it is public and not ``__all__`` itself."
        ),
        "_resolve_path": (
            "Resolve a package directory, ``__init__.py`` or dotted name to "
            "its ``__init__.py``."
        ),
        "ensure_all": (
            "Set ``__all__`` on an imported package, generating it once.\n"
            "\n"
            "        The block is written to ``__init__.py`` between the "
            "autoall markers when the\n"
            "        package does not already define ``__all__`` itself.\n"
            "\n"
            "        Args:\n"
            "            pkg_name: Dotted name of an importable package.\n"
            "            persist: Write the generated block back to "
            "``__init__.py``.\n"
            "            include_submodules: Also list imported submodules.\n"
            "            include_dunders: Also list dunder constants.\n"
            "\n"
            "        Returns:\n"
            "            The names placed in ``__all__``.\n"
            "        "
        ),
        "install": (
            "Append the ``ensure_all()`` snippet to a package's "
            "``__init__.py``.\n"
            "\n"
            "        Args:\n"
            "            target: Package directory, ``__init__.py`` or dotted "
            "name.\n"
            "            include_submodules: Passed to the generated call.\n"
            "            include_dunders: Passed to the generated call.\n"
            "\n"
            "        Returns:\n"
            "            The patched ``__init__.py`` path.\n"
            "        "
        ),
        "uninstall": (
            "Remove the autoall injection and generated block.\n"
            "\n"
            "        Args:\n"
            "            target: Package directory, ``__init__.py`` or dotted "
            "name.\n"
            "\n"
            "        Returns:\n"
            "            The cleaned ``__init__.py`` path.\n"
            "        "
        ),
        "is_installed": (
            "Return ``True`` when the package's ``__init__.py`` carries the "
            "autoall snippet."
        ),
        "regenerate": (
            "Regenerate ``__all__`` for an already imported package.\n"
            "\n"
            "        Args:\n"
            "            pkg_name: Dotted name of the package.\n"
            "            include_submodules: Also list imported submodules.\n"
            "            include_dunders: Also list dunder constants.\n"
            "\n"
            "        Returns:\n"
            "            The regenerated ``__all__`` names.\n"
            "        "
        ),
        "_cli_install": "Handle ``autoall install``.",
        "_cli_uninstall": "Handle ``autoall uninstall``.",
        "_cli_regen": "Handle ``autoall regen``.",
        "_cli_status": "Handle ``autoall status``.",
        "main": (
            "Run the ``autoall`` command line interface.\n"
            "\n"
            "    Args:\n"
            "        argv: Argument list to parse (defaults to "
            "``sys.argv[1:]``).\n"
            "\n"
            "    Returns:\n"
            "        Process exit code.\n"
            "    "
        ),
    },
}


def _has_docstring(node: cst.FunctionDef) -> bool:
    """Return ``True`` when ``node`` already starts with a docstring.

    Args:
        node: Function (or method) node to inspect.

    Returns:
        ``True`` when the first statement of the body is a string literal.
    """
    body = node.body
    if isinstance(body, cst.SimpleStatementSuite):
        statements = body.body
    elif isinstance(body, cst.IndentedBlock):
        statements = body.body
    else:  # pragma: no cover - defensive
        return False
    if not statements:
        return False
    first = statements[0]
    if isinstance(first, cst.SimpleStatementLine):
        if not first.body:
            return False
        first = first.body[0]
    return isinstance(first, cst.Expr) and isinstance(
        first.value, (cst.SimpleString, cst.ConcatenatedString)
    )


def _doc_line(text: str) -> cst.SimpleStatementLine:
    """Build a docstring statement from raw text.

    Args:
        text: Docstring content (without quotes), or a fully quoted string
            when it already starts with a ``\"\"\"`` marker.

    Returns:
        A statement line holding the string literal.
    """
    literal = text if text.lstrip().startswith('"""') else f'"""{text}"""'
    return cst.SimpleStatementLine(body=[cst.Expr(cst.SimpleString(literal))])


def _with_docstring(node: cst.FunctionDef, text: str) -> cst.FunctionDef:
    """Return ``node`` with ``text`` inserted as its docstring.

    Args:
        node: Function that lacks a docstring.
        text: Docstring content.

    Returns:
        The updated function node.
    """
    line = _doc_line(text)
    body = node.body
    if isinstance(body, cst.IndentedBlock):
        new_body = body.with_changes(body=[line, *body.body])
    else:  # SimpleStatementSuite -> expand to an indented block
        statements = [cst.SimpleStatementLine(body=[stmt]) for stmt in body.body]
        new_body = cst.IndentedBlock(body=[line, *statements])
    return node.with_changes(body=new_body)


class DocstringInserter(cst.CSTTransformer):
    """Insert the docstrings from :data:`DOCSTRINGS` into a module."""

    def __init__(self, docstrings: dict[str, str]) -> None:
        """Store the docstring table and initialise the scope stack.

        Args:
            docstrings: Mapping of qualified name to docstring text.
        """
        self.docstrings = docstrings
        self.stack: list[str] = []

    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        """Push the class name onto the scope stack."""
        self.stack.append(node.name.value)

    def leave_ClassDef(self, original_node, updated_node):
        """Pop the class name from the scope stack."""
        self.stack.pop()
        return updated_node

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        """Push the function name onto the scope stack."""
        self.stack.append(node.name.value)

    def leave_FunctionDef(self, original_node, updated_node):
        """Insert the docstring (when configured) and pop the stack."""
        qualname = ".".join(self.stack)
        self.stack.pop()
        text = self.docstrings.get(qualname)
        if not text or _has_docstring(original_node):
            return updated_node
        return _with_docstring(updated_node, text)


def patch_module(path: Path, docstrings: dict[str, str], check: bool) -> bool:
    """Rewrite one module with the configured docstrings.

    Args:
        path: Source file to patch.
        docstrings: Mapping of qualified name to docstring text.
        check: Only report whether a change is needed.

    Returns:
        ``True`` when the file changed (or would change in check mode).
    """
    source = path.read_text(encoding="utf-8")
    module = cst.parse_module(source)
    updated = module.visit(DocstringInserter(docstrings))
    new_source = updated.code
    if new_source == source:
        return False
    if not check:
        path.write_text(new_source, encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    """Apply (or check) the docstring table.

    Args:
        argv: Command line arguments; ``--check`` only reports.

    Returns:
        ``0`` on success, ``1`` when ``--check`` found missing docstrings.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    check = "--check" in args
    changed = 0
    for relative, table in DOCSTRINGS.items():
        path = ROOT / relative
        if patch_module(path, table, check):
            changed += 1
            print(f"{'would update' if check else 'updated'}: {relative}")
    if check and changed:
        return 1
    print(f"{changed} file(s) {'need' if check else ''} updated".rstrip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
