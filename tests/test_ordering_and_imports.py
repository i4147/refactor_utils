"""Tests for ordering.py and imports.py."""

from refactor_utils.ordering import topo_order
from refactor_utils.models import Block
from refactor_utils.imports import (
    build_import_section,
    render_import_line,
    future_section,
    _wrap,
)
from refactor_utils.models import ImportSpec


def make_block(bid, module, order, kind="const", primary=""):
    return Block(
        module=module,
        kind=kind,
        names=[primary] if primary else [],
        source=b"dummy",
        occs=[],
        order=order,
        bid=bid,
    )


def test_topo_order_simple():
    b1 = make_block(0, "a", 0, primary="X")
    b2 = make_block(1, "b", 0, primary="Y")
    deps = {1: {0}}  # b2 depends on b1
    ordered = topo_order([b1, b2], deps, lambda b: 0, lambda b: (b.order,))
    assert ordered[0].bid == 0
    assert ordered[1].bid == 1


def test_topo_order_cycle():
    b1 = make_block(0, "a", 0)
    b2 = make_block(1, "b", 0)
    deps = {0: {1}, 1: {0}}  # cycle
    ordered = topo_order([b1, b2], deps, lambda b: 0, lambda b: (b.order,))
    # Should still return both, with warning
    assert len(ordered) == 2


def test_render_import_line():
    spec = ImportSpec("os")
    assert render_import_line(spec) == "import os"
    spec2 = ImportSpec("os", alias="myos")
    assert "as myos" in render_import_line(spec2)
    spec3 = ImportSpec("os", name="path", is_from=True)
    assert render_import_line(spec3) == "from os import path"
    spec4 = ImportSpec("os", name="path", alias="p", is_from=True)
    assert "as p" in render_import_line(spec4)
    spec5 = ImportSpec("pkg", star=True, is_from=True)
    assert render_import_line(spec5) == "from pkg import *"


def test_future_section():
    assert future_section(["annotations"]) == "from __future__ import annotations"
    assert future_section([]) == ""


def test_wrap():
    # Short line not wrapped
    assert "from os import path" in _wrap("os", ["path"])
    # Long line wrapped
    items = [f"item{i}" for i in range(20)]
    wrapped = _wrap("pkg", items)
    assert "(" in wrapped
    assert "\n" in wrapped


def test_build_import_section():
    keys = [
        ("import", "os", None),
        ("from", "sys", "path", None),
        ("star", "typing"),
    ]
    section = build_import_section(keys, {})
    assert "import os" in section
    assert "from sys import path" in section
    assert "from typing import *" in section


def test_build_import_section_grouping():
    # stdlib vs third-party grouping
    keys = [("import", "os", None), ("import", "requests", None)]
    section = build_import_section(keys, {})
    # Should have two groups separated by blank line
    assert "import os" in section
    assert "import requests" in section
