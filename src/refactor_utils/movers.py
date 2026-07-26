"""Functions for parsing and reorganizing Python source files."""

import ast
import pathlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

from .utils import (
    _optimize_imports,
    parse_name_from_block,
    topological_sort,
)


def make_all(names: list[str]) -> str:
    """Generate __all__ list from a list of names."""
    uniq = sorted(set(names))
    items = ",\n    ".join(f'"{n}"' for n in uniq)
    return f"__all__ = [\n    {items}\n]\n"


@lru_cache(maxsize=256)
def _parse_import_line(imp: str) -> list[str]:
    """Parse an import line to extract module names."""
    if imp.startswith("import "):
        return [n.strip().split()[0] for n in imp[7:].split(",")]
    if imp.startswith("from "):
        return [imp.split()[1]]
    return []


def _get_block(lines: list[str], node) -> str:
    """Extract source code block for an AST node."""
    start = node.lineno - 1
    end = getattr(node, "end_lineno", start + 1)
    return "\n".join(lines[start:end]) + "\n"


def parse_top_level_items(path: str):
    """
    Parse a Python file and extract top-level functions, constants,
    classes, and imports.
    """
    try:
        src = pathlib.Path(path).open(encoding="utf-8").read()
        tree = ast.parse(src)
    except Exception:
        return [], [], [], []

    lines = src.splitlines()
    funcs, consts, classes, imports = [], [], [], []

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(_get_block(lines, node))
        elif isinstance(node, ast.FunctionDef):
            funcs.append(_get_block(lines, node))
        elif isinstance(node, ast.ClassDef):
            classes.append(_get_block(lines, node))
        elif isinstance(node, ast.Assign) and (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id.isupper()
        ):
            consts.append(_get_block(lines, node))

    return funcs, consts, classes, imports


def collect_from_files(paths: list[str]):
    """
    Collect top-level items from multiple Python files in parallel.
    Returns aggregated lists and a file mapping.
    """
    with ThreadPoolExecutor() as ex:
        results = list(ex.map(parse_top_level_items, paths))

    file_map = {}
    all_funcs, all_consts, all_classes, all_imports = [], [], [], []

    for p, (f, c, cl, im) in zip(paths, results, strict=False):
        file_map[p] = {
            "funcs": f,
            "consts": c,
            "classes": cl,
            "imports": im,
        }
        all_funcs += f
        all_consts += c
        all_classes += cl
        all_imports += im

    return all_funcs, all_consts, all_classes, all_imports, file_map


def build_merged_module(file_map: dict) -> str:
    """
    Build a single merged module from file map, with:
    - Deduplicated and optimized imports (excluding relative imports)
    - Topologically sorted functions and classes
    - Proper __all__ declaration
    """
    imports = []
    consts, funcs, classes = [], [], []
    const_names, func_names, class_names = [], [], []
    name_sources: dict[str, str] = {}

    # Collect all items from all files
    for path, b in file_map.items():
        imports.extend(b["imports"])

        for block in b["consts"]:
            name = parse_name_from_block(block)
            if name:
                if name in name_sources:
                    print(
                        f'WARNING: Constant "{name}" defined in both '
                        f"{name_sources[name]} and {path}"
                    )
                name_sources[name] = path
                consts.append(block)
                const_names.append(name)

        for block in b["funcs"]:
            name = parse_name_from_block(block)
            if name:
                if name in name_sources:
                    print(
                        f'WARNING: Function "{name}" defined in both '
                        f"{name_sources[name]} and {path}"
                    )
                name_sources[name] = path
                funcs.append(block)
                func_names.append(name)

        for block in b["classes"]:
            name = parse_name_from_block(block)
            if name:
                if name in name_sources:
                    print(
                        f'WARNING: Class "{name}" defined in both '
                        f"{name_sources[name]} and {path}"
                    )
                name_sources[name] = path
                classes.append(block)
                class_names.append(name)

    # Filter out relative imports before optimization
    filtered_imports = [
        imp for imp in imports if not imp.strip().startswith(("from .", "import ."))
    ]
    optimized_imports = _optimize_imports(filtered_imports)

    # Sort functions and classes by dependency order
    all_defined_names = set(const_names + func_names + class_names)
    if funcs:
        funcs = topological_sort(funcs, all_defined_names)
    if classes:
        classes = topological_sort(classes, all_defined_names)

    # Build body content (constants first, then functions, then classes)
    body_parts = []
    if consts:
        body_parts.append("\n".join(consts).strip())
    if funcs:
        body_parts.append("\n\n".join(funcs).strip())
    if classes:
        body_parts.append("\n\n".join(classes).strip())

    body = "\n\n\n".join(part for part in body_parts if part)
    all_block = make_all(const_names + func_names + class_names)

    # Assemble final output
    result_parts = []
    if optimized_imports.strip():
        result_parts.append(optimized_imports.strip())
    if body:
        result_parts.append(body)
    result_parts.append(all_block)

    return "\n\n\n".join(result_parts) + "\n"


def build_top_level_modules(file_map: dict):
    """
    Build content for separate funcs, consts, and classes modules.
    """
    funcs_content = []
    consts_content = []
    classes_content = []

    for path, b in file_map.items():
        if b["funcs"]:
            funcs_content.extend(b["funcs"])
        if b["consts"]:
            consts_content.extend(b["consts"])
        if b["classes"]:
            classes_content.extend(b["classes"])

    return (
        "\n\n".join(funcs_content),
        "\n\n".join(consts_content),
        "\n\n".join(classes_content),
    )


def build_subpkg_modules(root: str, file_map: dict):
    """
    Build subpackage structure from file map.
    Returns a mapping of package name to (funcs, consts, classes) content.
    """
    pkg_map = {}

    for path, buckets in file_map.items():
        # Determine the subpackage based on directory structure
        rel_path = (
            pathlib.Path(path).relative_to(root) if root in path else pathlib.Path(path)
        )
        pkg = str(rel_path.parent) if rel_path.parent != pathlib.Path(".") else ""

        if pkg not in pkg_map:
            pkg_map[pkg] = {"funcs": [], "consts": [], "classes": []}

        pkg_map[pkg]["funcs"].extend(buckets["funcs"])
        pkg_map[pkg]["consts"].extend(buckets["consts"])
        pkg_map[pkg]["classes"].extend(buckets["classes"])

    # Convert lists to content strings
    result = {}
    for pkg, buckets in pkg_map.items():
        result[pkg] = (
            "\n\n".join(buckets["funcs"]),
            "\n\n".join(buckets["consts"]),
            "\n\n".join(buckets["classes"]),
            buckets,  # Keep the original buckets for reference
        )

    return result


def make_all_list(items: list[str]) -> str:
    """Create a formatted list for __all__."""
    return (
        "[\n    " + ",\n    ".join(f'"{item}"' for item in sorted(set(items))) + "\n]"
    )
