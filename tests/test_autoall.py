"""Tests for the ``autoall`` helpers used by ``pyrefactor``.

``pyrefactor`` calls :func:`autoall.ensure_all_file` after writing
``consolidated/<pkgname>.py`` so the generated module gets an ``__all__``
section in place. These tests pin the behaviour that matters for that flow:
the public block builder, idempotency, and preservation of CRLF line endings.
"""

import textwrap

from autoall import build_all_block, ensure_all_file


def test_build_all_block_is_public_and_matches_written_block(tmp_path):
    """``build_all_block`` returns exactly what ``ensure_all_file`` writes."""
    target = tmp_path / "mod.py"
    target.write_text("A = 1\n\ndef f():\n    return A\n")

    names = ensure_all_file(target)
    assert names == ["A", "f"]
    assert build_all_block(names).strip() in target.read_text()

    # The helper is the same text that ends up on disk.
    block = build_all_block(names)
    assert "# --- auto-generated __all__" in block
    assert "__all__ = [\n    'A',\n    'f',\n]" in block


def test_ensure_all_file_second_run_is_a_no_op(tmp_path):
    """Re-running does not rewrite (or churn) the file."""
    target = tmp_path / "mod.py"
    target.write_text("A = 1\nB = 2\n")
    ensure_all_file(target)
    before = target.read_bytes()
    ensure_all_file(target)
    assert target.read_bytes() == before


def test_ensure_all_file_preserves_crlf(tmp_path):
    """CRLF files stay CRLF, including the generated block."""
    target = tmp_path / "mod.py"
    target.write_bytes(b"A = 1\r\nB = 2\r\n")
    ensure_all_file(target)
    raw = target.read_bytes()
    assert raw.count(b"\r\n") > 0
    assert raw.count(b"\n") == raw.count(b"\r\n")
    # Second run is byte-for-byte identical.
    ensure_all_file(target)
    assert target.read_bytes() == raw


def test_ensure_all_file_replaces_existing_block(tmp_path):
    """A stale block is regenerated with the current public names."""
    target = tmp_path / "mod.py"
    target.write_text("OLD = 0\n")
    ensure_all_file(target)
    target.write_text(textwrap.dedent("""\
            OLD = 0
            NEW = 1

            # --- auto-generated __all__ (delete to regenerate) ---
            __all__ = [
                'OLD',
            ]
            # --- end auto-generated __all__ ---
            """))
    names = ensure_all_file(target)
    text = target.read_text()
    assert names == ["NEW", "OLD"]
    assert text.count("# --- auto-generated __all__") == 1
    assert "'NEW'" in text


def test_ensure_all_file_respects_user_all_outside_block(tmp_path):
    """A user-defined ``__all__`` is not duplicated or overwritten by us."""
    target = tmp_path / "mod.py"
    original = 'A = 1\n__all__ = ["A"]\n'
    target.write_text(original)
    names = ensure_all_file(target)
    assert names == ["A"]
    assert target.read_text() == original


def test_ensure_all_file_persist_false_does_not_write(tmp_path):
    """``persist=False`` only computes names."""
    target = tmp_path / "mod.py"
    target.write_text("A = 1\n")
    assert ensure_all_file(target, persist=False) == ["A"]
    assert target.read_text() == "A = 1\n"


def test_ensure_all_file_handles_unparsable_file(tmp_path):
    """A broken file is reported through a warning and left alone."""
    target = tmp_path / "mod.py"
    target.write_text("def broken(:\n")
    with_target = target.read_text()
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert ensure_all_file(target) == []
    assert target.read_text() == with_target
    assert any("cannot parse" in str(w.message) for w in caught)
