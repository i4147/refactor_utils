"""Utility functions for file operations and code analysis."""

import contextlib
import os
import pathlib
import re
import shutil
import subprocess
from collections import defaultdict

BACKUP_SUFFIX = ".bak"


def list_py_files(root: str, recursive: bool = True):
    """List all Python files in a directory, excluding backup files."""
    out = []
    for d, _, files in os.walk(root):
        out.extend(
            os.path.join(d, f)
            for f in files
            if f.endswith(".py") and not f.endswith(BACKUP_SUFFIX + ".py")
        )
        if not recursive:
            break
    return out


def ensure_backups(paths) -> None:
    """Create backup copies of files if they exist."""
    for p in paths:
        if pathlib.Path(p).exists():
            shutil.copy2(p, p + BACKUP_SUFFIX)


def restore_backups(paths):
    """Restore files from their backups."""
    restored = []
    for p in paths:
        bak = p + BACKUP_SUFFIX
        if pathlib.Path(bak).exists():
            shutil.copy2(bak, p)
            restored.append(p)
    return restored


def overwrite_file(path: str, content: str, dry_run: bool) -> None:
    """Write content to a file, creating directories if needed."""
    if dry_run:
        return
    pathlib.Path(pathlib.Path(path).parent or ".").mkdir(exist_ok=True, parents=True)
    pathlib.Path(path).write_text(content, encoding="utf-8")


def try_format(path: str) -> None:
    """Attempt to format a file using ruff."""
    with contextlib.suppress(Exception):
        subprocess.run(["ruff", "format", path], check=False)


def parse_name_from_block(block: str) -> str | None:
    """Extract the name defined by a top-level block (function, class, constant)."""
    block = block.strip()
    if block.startswith("def "):
        return block[4:].split("(")[0].strip()
    if block.startswith("class "):
        name_part = block[6:].split("(")[0].split(":")[0].strip()
        return name_part
    if "=" in block:
        # Constant assignment: UPPER_CASE = value
        name = block.split("=", 1)[0].strip()
        if name.isupper() or "_" in name:
            return name
    return None


def get_referenced_names(block: str) -> set[str]:
    """Extract all identifier names referenced inside a block."""
    return set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", block))


def topological_sort(blocks: list[str], defined_names: set[str]) -> list[str]:
    """
    Sort blocks so that dependencies come before dependents.
    Uses Kahn's algorithm for topological sorting.
    """
    name_to_block = {}
    for b in blocks:
        n = parse_name_from_block(b)
        if n:
            name_to_block[n] = b
        else:
            # For blocks we can't parse, just keep them
            pass

    if not name_to_block:
        return blocks

    # Find dependencies between blocks
    deps = {}
    for name, block in name_to_block.items():
        refs = get_referenced_names(block) & defined_names - {name}
        deps[name] = refs

    # Calculate in-degrees (number of dependencies)
    in_degree = {name: 0 for name in name_to_block}
    for name, refs in deps.items():
        for r in refs:
            if r in in_degree:
                in_degree[name] += 1

    # Build adjacency list (reverse dependency graph)
    adjacency = {name: [] for name in name_to_block}
    for name, refs in deps.items():
        for r in refs:
            if r in adjacency:
                adjacency[r].append(name)

    # Kahn's algorithm
    queue = [name for name, deg in in_degree.items() if deg == 0]
    sorted_names = []

    while queue:
        name = queue.pop(0)
        sorted_names.append(name)
        for dependent in adjacency[name]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    # If cycle detected or incomplete sort, fall back to original order
    if len(sorted_names) != len(name_to_block):
        return blocks

    return [name_to_block[n] for n in sorted_names]


def _optimize_imports(import_blocks: list) -> str:
    """Optimize and deduplicate imports, filtering out relative imports."""
    seen = set()
    merged_imports = defaultdict(list)

    for line in import_blocks:
        line = line.strip()
        if not line or line in seen:
            continue

        # Skip relative imports - they're no longer needed in merged file
        if line.startswith("from .") or line.startswith("import ."):
            continue

        seen.add(line)

        if line.startswith("from "):
            parts = line.split()
            module = parts[1]
            # Handle "from X import Y, Z" or "from X import (Y, Z)"
            import_part = line.split("import", 1)[1].strip()
            names = [
                n.strip()
                for n in import_part.replace("(", "").replace(")", "").split(",")
            ]
            merged_imports[module].extend(names)
        elif line.startswith("import "):
            modules = [m.strip() for m in line[7:].split(",")]
            for m in modules:
                if m not in merged_imports:
                    merged_imports[m] = []

    # Classify imports
    std_imports, third_party, local_imports = [], [], []
    for module, names in merged_imports.items():
        if module.startswith("."):
            continue  # Already filtered, but just in case
        elif "." in module:
            third_party.append((module, names))
        else:
            std_imports.append((module, names))

    def fmt(items):
        lines = []
        for module, names in sorted(items):
            unique_names = sorted(set(n for n in names if n))
            if unique_names:
                names_str = ", ".join(unique_names)
                if len(names_str) > 79:  # Line length consideration
                    lines.append(
                        f"from {module} import (\n    {names_str.replace(', ', ',\n    ')}\n)"
                    )
                else:
                    lines.append(f"from {module} import {names_str}")
            else:
                lines.append(f"import {module}")
        return lines

    result = fmt(std_imports) + fmt(third_party) + fmt(local_imports)
    return "\n".join(result) + "\n\n" if result else ""
