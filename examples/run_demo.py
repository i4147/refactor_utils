#!/usr/bin/env python
"""End-to-end demo: consolidate ``demo_pkg`` into a self-contained file.

Run from anywhere:

    python examples/run_demo.py

It consolidates the demo package with ``--embed-data`` (text data as plain
text, the fake font as base64), then proves the consolidated file is a
drop-in replacement for the package and shows the resource accessor API.
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pyrefactor import consolidate  # noqa: E402

DEMO = ROOT / "examples" / "demo_project" / "demo_pkg"


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def main():
    print("== 1. consolidate with embedded data ==")
    with tempfile.TemporaryDirectory() as out:
        result = consolidate(DEMO, out, embed_data=True, copy_data=False)
        target = result.files[0]
        print(
            f"  wrote {target} ({target.stat().st_size} bytes),",
            f"{result.modules} modules, {result.exports} exports,",
            f"{len(result.embedded_data_files)} embedded resources",
        )

        print("\n== 2. drop-in replacement check (package vs consolidated) ==")
        sys.path.insert(0, str(DEMO.parent))
        import demo_pkg as original  # the real package

        consolidated = load_module(target, "consolidated_demo_pkg")
        checks = [
            ("VERSION", original.VERSION, consolidated.VERSION),
            ("greet", original.greet("Ada"), consolidated.greet("Ada")),
            (
                "Person.greeting",
                original.Person("Ada").greeting(),
                consolidated.Person("Ada").greeting(),
            ),
            ("helper", original.helper("x"), consolidated.helper("x")),
            ("shared", original.shared(), consolidated.shared()),
        ]
        for label, a, b in checks:
            status = "OK  " if a == b else "FAIL"
            print(f"  [{status}] {label}: {b!r}")
            assert a == b, f"{label} mismatch"

        print("\n== 3. embedded resources ==")
        names = consolidated.list_resources()
        print("  list_resources():", names)
        settings_text = consolidated.get_resource("data/settings.json")
        assert isinstance(settings_text, str)
        print(
            "  get_resource('data/settings.json') ->",
            json.loads(settings_text)["greeting"],
            "(plain text, not base64)",
        )
        ini_text = consolidated.get_resource("data/config.ini")
        assert "[metadata]" in ini_text
        print("  get_resource('data/config.ini') -> readable ini text, OK")
        font = consolidated.get_resource("assets/logo.ttf")
        orig_font = (DEMO / "assets" / "logo.ttf").read_bytes()
        assert isinstance(font, bytes) and font == orig_font
        print(
            f"  get_resource('assets/logo.ttf') -> {type(font).__name__},",
            f"{len(font)} bytes, base64-embedded, matches original: True",
        )
        with consolidated.resource_path("assets/logo.ttf") as p:
            ok = Path(p).read_bytes() == orig_font
            print(f"  resource_path('assets/logo.ttf') -> {p} (readable: {ok})")
            assert ok
        try:
            consolidated.get_resource("nope.txt")
        except KeyError as exc:
            print("  get_resource('nope.txt') -> KeyError:", exc)

        print("\n== 4. run the consolidated file directly as a script ==")
        proc = subprocess.run(
            [sys.executable, str(target), "Grace"],
            capture_output=True,
            text=True,
        )
        print("  exit code:", proc.returncode)
        for line in proc.stdout.strip().splitlines():
            print("   ", line)
        assert proc.returncode == 0, proc.stderr

        print("\n== 5. equivalent CLI invocation ==")
        print("  $ pyrefactor examples/demo_project/demo_pkg -o dist "
              "--embed-data --no-copy-data")

    print("\nAll demo checks passed.")


if __name__ == "__main__":
    main()
