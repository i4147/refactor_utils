import argparse
from argparse import Namespace
from pathlib import Path
from .core import run
from .options import Options
def parse_args() -> Namespace:
    p = argparse.ArgumentParser(
        prog="pyrefactor",
        description="Refactor small python packages",
    )
    p.add_argument("run", nargs="?", default="run")
    p.add_argument(
        "--mode",
        choices=[
            "small",
            "merge",
            "subpkg",
            "dry-run",
            "single",
            "single_file",
        ],
        default="single",
    )
    p.add_argument("--root", default=".")
    p.add_argument("--single", action="store_true")
    p.add_argument("--no-backup", action="store_true")
    p.add_argument("--no-format", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--undo", action="store_true")
    return p.parse_args()
def main() -> None:
    args = parse_args()
    cwd = Path.cwd()
    folder_name = cwd.name
    func_file = f"{folder_name}_func.py"
    class_file = f"{folder_name}_class.py"
    const_file = f"{folder_name}_const.py"
    opts = Options(
        mode=args.mode,
        root=args.root,
        backup=not args.no_backup,
        format=not args.no_format,
        verbose=not args.quiet,
        undo=args.undo,
        single=args.single or args.mode == "single" or args.mode == "single_file",
        target_funcs=func_file,
        target_consts=const_file,
        target_classes=class_file,
    )
    print(f"Running pyrefactor mode={opts.mode} root={opts.root}")
    run(opts)
if __name__ == "__main__":
    main()
