"""Core module with data dependency."""

import json
from pathlib import Path

DATA_PATH = Path(__file__).parent / "data" / "config.json"


def hello():
    """Return hello using data file if present."""
    try:
        data = json.loads((Path(__file__).parent / "data" / "config.json").read_text())
        return f"Hello {data.get('name', 'world')}"
    except FileNotFoundError:
        return "Hello world"


def add(a, b):
    """Add two numbers (example of a second public function)."""
    return a + b
