"""Core logic of the demo package."""

import json
import sys
from pathlib import Path

try:
    import yaml  # optional dependency: kept verbatim as an import guard

    HAS_YAML = True
except ImportError:
    yaml = None
    HAS_YAML = False

from .helpers import UTIL, helper

DEFAULT_NAME = "world"


def shared():
    # identical duplicate of helpers.shared() — the consolidator dedupes it
    return "shared implementation"


def load_settings():
    """Load data/settings.json — from embedded resources or the package dir."""
    try:
        # get_resource exists only in the consolidated file.
        return json.loads(get_resource("data/settings.json"))
    except NameError:
        path = Path(__file__).parent / "data" / "settings.json"
        return json.loads(path.read_text(encoding="utf-8"))


class Person:
    """Someone to greet."""

    def __init__(self, name=DEFAULT_NAME):
        self.name = name

    def greeting(self):
        tail = shared()
        return f"Hello, {self.name}! ({helper(self.name)}) [{tail}]"


def greet(name=DEFAULT_NAME):
    settings = load_settings()
    return settings["greeting"].format(name=name) + f" [util={UTIL}]"


def main(argv=None):
    """CLI entry used to prove the merged ``__main__`` guard runs."""
    args = list(argv) if argv is not None else sys.argv[1:]
    name = args[0] if args else DEFAULT_NAME
    print("core.main:", greet(name))
    print("core.main:", Person(name).greeting())
    return 0


if __name__ == "__main__":
    main()
