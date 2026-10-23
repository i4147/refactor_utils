from concurrent.futures import ThreadPoolExecutor
import os
import pathlib
from pathlib import Path
from . import movers, utils
from .options import Options
from .utils import _optimize_imports
def _target_module(opts: Options) -> str:
    pkg = Path(Path(opts.root).resolve()).name
    return os.path.join(opts.root, f"{pkg}.py")
def _get_target_paths(opts: Options) -> dict:
    return {
        "funcs": os.path.join(opts.root, opts.target_funcs),
        "consts": os.path.join(opts.root, opts.target_consts),
        "classes": os.path.join(opts.root, opts.target_classes),
    }
def write_top_init(init_imports: list[str], root: str, dry: bool, opts: Options) -> None:
    pkg_name = Path(root).name
    optimized = _optimize_imports(init_imports)
    content = optimized.rstrip() + f"\n\nfrom .{pkg_name} import *\n"
    utils.overwrite_file(os.path.join(root, "__init__.py"), content, dry_run=dry)
    if opts.format and not dry:
        utils.try_format(os.path.join(root, "__init__.py"))
def run_single_file(opts: Options) -> None:
    if opts.verbose:
        print(f"Running single_file mode: merging all Python files into one module")
    py_files = utils.list_py_files(opts.root, recursive=True)
    init_path = os.path.join(opts.root, "__init__.py")
    init_abs = pathlib.Path(init_path).resolve()
    paths = []
    init_imports = []
    for p in py_files:
        abs_p = pathlib.Path(p).resolve()
        if abs_p == init_abs:
            _, _, _, init_imports = movers.parse_top_level_items(p)
        else:
            paths.append(p)
    if opts.verbose:
        print(f"Processing {len(paths)} Python files...")
    _, _, _, _, file_map = movers.collect_from_files(paths)
    merged = movers.build_merged_module(file_map)
    if init_imports:
        optimized_init_imports = _optimize_imports(init_imports)
        merged = optimized_init_imports + merged
    pkg = os.path.basename(os.path.abspath(opts.root))
    target = os.path.join(opts.root, f"{pkg}.py")
    if opts.verbose:
        print(f"Creating merged module: {target}")
    if opts.backup:
        utils.ensure_backups([target, init_path])
    dry_run = opts.mode == "dry-run"
    utils.overwrite_file(target, merged, dry_run)
    init_content = f"from .{pkg} import *\n"
    utils.overwrite_file(init_path, init_content, dry_run)
    if opts.format and not dry_run:
        if opts.verbose:
            print("Formatting files...")
        utils.try_format(target)
        utils.try_format(init_path)
    if opts.verbose and not dry_run:
        funcs, consts, classes, imports, _ = movers.collect_from_files([target])
        print(
            f"Successfully merged {len(funcs)} functions, {len(classes)} classes, "
            f"and {len(consts)} constants into {pkg}.py"
        )
def run_single(opts: Options) -> None:
    py_files = utils.list_py_files(opts.root, recursive=True)
    init_path = os.path.join(opts.root, "__init__.py")
    paths = [p for p in py_files if pathlib.Path(p).resolve() != pathlib.Path(init_path).resolve()]
    _, _, _, _, file_map = movers.collect_from_files(paths)
    merged = movers.build_merged_module(file_map)
    pkg = os.path.basename(os.path.abspath(opts.root))
    target = os.path.join(opts.root, f"{pkg}.py")
    if opts.backup:
        utils.ensure_backups([target, init_path])
    dry_run = opts.mode == "dry-run"
    utils.overwrite_file(target, merged, dry_run)
    utils.overwrite_file(init_path, f"from .{pkg} import *\n", dry_run)
    if opts.format and not dry_run:
        utils.try_format(target)
        utils.try_format(init_path)
def run_small(opts: Options) -> None:
    py_files = utils.list_py_files(opts.root, recursive=False)
    init_path = os.path.join(opts.root, "__init__.py")
    paths = [p for p in py_files if p != init_path]
    funcs, consts, classes, _imports, file_map = movers.collect_from_files(paths)
    if opts.verbose:
        print(f"Collected {len(funcs)} funcs, {len(classes)} classes, {len(consts)} consts")
    merged = movers.build_merged_module(file_map)
    target = _target_module(opts)
    pkg_name = os.path.splitext(Path(target).name)[0]
    if opts.backup:
        utils.ensure_backups([target, init_path])
    dry_run = opts.mode == "dry-run"
    utils.overwrite_file(target, merged, dry_run)
    utils.overwrite_file(init_path, f"from .{pkg_name} import *\n", dry_run)
    if opts.format and not dry_run:
        with ThreadPoolExecutor() as ex:
            ex.map(utils.try_format, [target, init_path])
def run_merge(opts: Options) -> None:
    root = opts.root
    py_files = utils.list_py_files(root, recursive=True)
    target_files = {
        os.path.join(root, opts.target_funcs),
        os.path.join(root, opts.target_consts),
        os.path.join(root, opts.target_classes),
    }
    skip_abs = {pathlib.Path(f).resolve() for f in target_files}
    init_file = os.path.join(root, "__init__.py")
    init_abs = pathlib.Path(init_file).resolve()
    other_paths = []
    for p in py_files:
        abs_p = pathlib.Path(p).resolve()
        if abs_p in skip_abs or abs_p == init_abs:
            continue
        other_paths.append(p)
    (all_funcs, all_consts, all_classes, all_imports, file_map) = movers.collect_from_files(other_paths)
    init_funcs, init_consts, init_classes, init_imports = movers.parse_top_level_items(init_file)
    if opts.verbose:
        print(f"Found {len(all_imports) + len(init_imports)} imports")
        print(f"Found {len(all_funcs) + len(init_funcs)} functions")
        print(f"Found {len(all_consts) + len(init_consts)} constants")
        print(f"Found {len(all_classes) + len(init_classes)} classes")
    func_content, const_content, class_content = movers.build_top_level_modules(file_map)
    if init_funcs:
        func_content = (func_content + "\n\n" + "\n\n".join(init_funcs)).strip()
    if init_consts:
        const_content = (const_content + "\n\n" + "\n\n".join(init_consts)).strip()
    if init_classes:
        class_content = (class_content + "\n\n" + "\n\n".join(init_classes)).strip()
    targets = _get_target_paths(opts)
    if opts.backup:
        backup_files = list(targets.values()) + [init_file]
        utils.ensure_backups(backup_files)
    dry = opts.mode == "dry-run"
    contents = {
        "funcs": func_content,
        "consts": const_content,
        "classes": class_content,
    }
    for key, path in targets.items():
        utils.overwrite_file(path, contents[key], dry_run=dry)
    write_top_init(init_imports, root, dry, opts)
    if opts.format and not dry:
        with ThreadPoolExecutor() as executor:
            all_targets = list(targets.values()) + [init_file]
            executor.map(utils.try_format, all_targets)
def run_subpkg(opts: Options) -> None:
    py_files = utils.list_py_files(opts.root, recursive=True)
    init_path = pathlib.Path(os.path.join(opts.root, "__init__.py")).resolve()
    paths = [p for p in py_files if pathlib.Path(p).resolve() != init_path]
    (
        _all_funcs,
        _all_consts,
        _all_classes,
        _all_imports,
        file_map,
    ) = movers.collect_from_files(paths)
    pkg_map = movers.build_subpkg_modules(opts.root, file_map)
    for pkg, (
        func_content,
        const_content,
        class_content,
    ) in pkg_map.items():
        folder = os.path.join(opts.root, pkg) if pkg else opts.root
        targets = {
            "funcs": os.path.join(folder, opts.target_funcs),
            "consts": os.path.join(folder, opts.target_consts),
            "classes": os.path.join(folder, opts.target_classes),
        }
        if opts.backup:
            backup_files = [
                *list(targets.values()),
                os.path.join(folder, "__init__.py"),
            ]
            utils.ensure_backups(backup_files)
        is_dry_run = opts.mode == "dry-run"
        contents = {
            "funcs": func_content,
            "consts": const_content,
            "classes": class_content,
        }
        for key, path in targets.items():
            utils.overwrite_file(path, contents[key], dry_run=is_dry_run)
        actual_buckets = {
            "funcs": [],
            "consts": [],
            "classes": [],
        }
        for path, b in file_map.items():
            rel_path = pathlib.Path(path).relative_to(opts.root) if opts.root in path else pathlib.Path(path)
            file_pkg = str(rel_path.parent) if rel_path.parent != pathlib.Path(".") else ""
            if file_pkg == pkg:
                actual_buckets["funcs"].extend(b["funcs"])
                actual_buckets["consts"].extend(b["consts"])
                actual_buckets["classes"].extend(b["classes"])
        if actual_buckets["funcs"] or actual_buckets["consts"] or actual_buckets["classes"]:
            imports = []
            items = []
            if actual_buckets["funcs"]:
                func_names = [utils.parse_name_from_block(b) for b in actual_buckets["funcs"]]
                func_names = [n for n in func_names if n]
                if func_names:
                    imports.append(f"from .{os.path.splitext(opts.target_funcs)[0]} import {', '.join(func_names)}")
                    items.extend(func_names)
            if actual_buckets["consts"]:
                const_names = [utils.parse_name_from_block(b) for b in actual_buckets["consts"]]
                const_names = [n for n in const_names if n]
                if const_names:
                    imports.append(f"from .{os.path.splitext(opts.target_consts)[0]} import {', '.join(const_names)}")
                    items.extend(const_names)
            if actual_buckets["classes"]:
                class_names = [utils.parse_name_from_block(b) for b in actual_buckets["classes"]]
                class_names = [n for n in class_names if n]
                if class_names:
                    imports.append(f"from .{os.path.splitext(opts.target_classes)[0]} import {', '.join(class_names)}")
                    items.extend(class_names)
            content = "\n".join(imports) + f"\n\n__all__ = {movers.make_all_list(items)}\n"
            utils.overwrite_file(
                os.path.join(folder, "__init__.py"),
                content,
                dry_run=is_dry_run,
            )
def run(opts: Options) -> None:
    if opts.undo:
        pkg = os.path.basename(os.path.abspath(opts.root))
        targets = [
            os.path.join(opts.root, f"{pkg}.py"),
            os.path.join(opts.root, "__init__.py"),
            os.path.join(opts.root, opts.target_funcs),
            os.path.join(opts.root, opts.target_consts),
            os.path.join(opts.root, opts.target_classes),
        ]
        restored = utils.restore_backups(targets)
        if opts.verbose:
            print(f"Restored {len(restored)} files from backups")
        return
    mode_handlers = {
        "single_file": run_single_file,
        "single": run_single,
        "small": run_small,
        "merge": run_merge,
        "subpkg": run_subpkg,
        "dry-run": lambda o: run_merge(o.__class__(**{**o.__dict__, "mode": "dry-run"})),
    }
    handler = mode_handlers.get(opts.mode)
    if not handler:
        msg = f"Unknown mode: {opts.mode}"
        raise ValueError(msg)
    handler(opts)
