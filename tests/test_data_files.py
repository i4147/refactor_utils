"""Tests for data-file preservation — discovery + copy semantics."""

import filecmp
from pathlib import Path
from refactor_utils.core import consolidate


def test_data_files_copied(sample_project, tmp_path):
    """Check that data files are copied into the output."""
    out = tmp_path / "out"
    result = consolidate(sample_project, out, copy_data=True)
    assert len(result.copied_data_files) == 3
    # Check json content preserved
    src_json = sample_project / "data" / "sample.json"
    dst_candidates = list(out.rglob("sample.json"))
    assert len(dst_candidates) == 1
    assert filecmp.cmp(src_json, dst_candidates[0], shallow=False)


def test_data_files_no_copy(sample_project, tmp_path):
    """Check that ``--no-copy-data`` disables copying."""
    out = tmp_path / "out"
    result = consolidate(sample_project, out, copy_data=False)
    assert not list(out.rglob("sample.json"))
    assert result.copied_data_files == []


def test_data_glob_filter(sample_project, tmp_path):
    """Check that ``--data-glob`` limits which data files are copied."""
    out = tmp_path / "out"
    result = consolidate(sample_project, out, copy_data=True, data_glob=("*.json",))
    assert len(result.copied_data_files) == 1
    assert result.copied_data_files[0].suffix == ".json"


def test_data_files_preserve_structure(sample_project, tmp_path):
    """Check that copied data files keep their relative paths."""
    out = tmp_path / "out"
    consolidate(sample_project, out, copy_data=True)
    # Relative structure should be preserved
    # sample_project is .../pkg, base is .../ , so relative is pkg/data/sample.json -> output/pkg/data/sample.json
    # Check that output contains pkg subdirectory with data
    found = list(out.rglob("sample.json"))
    assert found
    assert "data" in str(found[0].parent)


def test_data_files_with_modes(sample_project, tmp_path):
    """Check that data copying works in every output mode."""
    for mode in ["single", "split", "flat"]:
        out = tmp_path / f"out_{mode}"
        result = consolidate(sample_project, out, mode=mode, copy_data=True)
        assert len(result.copied_data_files) >= 1


def test_data_files_exclude_via_pattern(sample_project, tmp_path):
    # Exclude csv via exclude pattern
    """Check that exclude patterns also filter data files."""
    out = tmp_path / "out"
    result = consolidate(sample_project, out, exclude=("*.csv",), copy_data=True)
    rels = [p.name for p in result.copied_data_files]
    assert "sample.csv" not in rels
    assert "sample.json" in rels


def test_data_files_real_project(tmp_path):
    # Realistic project with multiple data types
    """Check data discovery against the repository's own data files."""
    pkg = tmp_path / "app"
    pkg.mkdir()
    (pkg / "core.py").write_text("def hi(): return 1\n")
    (pkg / "data.csv").write_text("a,b\n1,2\n")
    (pkg / "config.ini").write_text("[section]\nkey=val\n")
    (pkg / "template.html").write_text("<html></html>\n")
    out = tmp_path / "out"
    result = consolidate(pkg, out, copy_data=True)
    assert len(result.copied_data_files) == 3
    for p in result.copied_data_files:
        assert p.exists()
