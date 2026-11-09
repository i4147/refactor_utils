"""Basic usage of pyrefactor — CLI and Python API.

Run:
    python examples/basic_usage.py
    pyrefactor examples/data_files_example -o /tmp/basic_out --overwrite
"""

from pathlib import Path
import tempfile
import textwrap
from refactor_utils.core import consolidate


def demo_programmatic():
    """Consolidate a tiny package through the Python API."""
    print("=== Programmatic API: basic consolidation ===")
    with tempfile.TemporaryDirectory() as tmp:
        pkg = Path(tmp) / "mypackage"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("from .a import hello\n")
        (pkg / "a.py").write_text(
            textwrap.dedent('''\
            """Module a"""
            def hello():
                return "Hello from a"
            ''')
        )
        (pkg / "b.py").write_text(
            textwrap.dedent('''\
            """Module b"""
            from .a import hello
            def greet():
                return hello() + " and b"
            ''')
        )

        out = Path(tmp) / "output"
        result = consolidate(pkg, out)
        print(f"Modules: {result.modules}, Exports: {result.exports}")
        print(f"Files: {result.files}")
        print((out / "_consolidated.py").read_text()[:500])


def demo_cli():
    """Show the equivalent command line invocation."""
    print("\n=== CLI: equivalent to ===")
    print("pyrefactor path/to/package -o output --overwrite --validate")
    print("See examples/data_files_example for a runnable CLI example.")


if __name__ == "__main__":
    demo_programmatic()
    demo_cli()
