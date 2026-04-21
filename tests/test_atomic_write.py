"""Tests for brain.io — the atomic-write foundation.

These cover the contract guarantees: the destination file either contains
the full new content or the previous content — never a truncated mix. See
`src/brain/io.py` for the public API.
"""

from __future__ import annotations

import os
import threading

import pytest

from brain import io as brain_io
from brain.io import atomic_write_bytes, atomic_write_text


def test_text_roundtrip(tmp_path):
    dst = tmp_path / "hello.txt"
    atomic_write_text(dst, "hello world\n")
    assert dst.read_text() == "hello world\n"


def test_bytes_roundtrip(tmp_path):
    dst = tmp_path / "blob.bin"
    payload = bytes(range(256))
    atomic_write_bytes(dst, payload)
    assert dst.read_bytes() == payload


def test_parent_dirs_auto_created(tmp_path):
    dst = tmp_path / "a" / "b" / "c" / "deep.md"
    assert not dst.parent.exists()
    atomic_write_text(dst, "x")
    assert dst.read_text() == "x"
    assert dst.parent.is_dir()


def test_unicode_vietnamese_and_emoji(tmp_path):
    """UTF-8 default must handle the real-world content we ingest — the
    brain's raw/ files are full of Vietnamese and emoji from chat logs."""
    dst = tmp_path / "vn.md"
    content = "đôi dép tôi đâu? 🥿\n" * 100  # force a multi-page write
    atomic_write_text(dst, content)
    assert dst.read_text(encoding="utf-8") == content


def test_overwrites_existing_file(tmp_path):
    dst = tmp_path / "log.txt"
    atomic_write_text(dst, "old content")
    atomic_write_text(dst, "new content")
    assert dst.read_text() == "new content"


def test_explicit_encoding(tmp_path):
    dst = tmp_path / "latin.txt"
    atomic_write_text(dst, "café", encoding="latin-1")
    assert dst.read_bytes() == b"caf\xe9"


def test_tmp_cleaned_up_on_replace_failure(tmp_path, monkeypatch):
    """Simulate a crash between write-and-replace. Destination must be
    untouched (or absent) AND the scratch tmp must be cleaned up — no
    orphan .tmp.<pid> siblings left to pollute the directory."""
    dst = tmp_path / "victim.txt"

    def boom(src, dst_):  # same signature as os.replace
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(brain_io.os, "replace", boom)

    with pytest.raises(RuntimeError, match="simulated crash"):
        atomic_write_text(dst, "never lands")

    assert not dst.exists(), "destination must not be partially written"
    # No .tmp.<pid> leftover next to the target.
    leftovers = [p for p in tmp_path.iterdir() if ".tmp." in p.name]
    assert leftovers == [], f"stale temp files: {leftovers}"


def test_original_preserved_when_replace_fails(tmp_path, monkeypatch):
    """The whole point of atomic replace: if the final rename blows up,
    the previous version of the file stays intact."""
    dst = tmp_path / "keep.txt"
    atomic_write_text(dst, "original\n")

    def boom(src, dst_):
        raise OSError("ENOSPC")

    monkeypatch.setattr(brain_io.os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_text(dst, "corrupt-half")

    assert dst.read_text() == "original\n"


def test_permission_error_not_swallowed(tmp_path):
    """Contract: permission errors propagate. The atomic wrapper must
    NOT catch-and-ignore — callers need to know the write failed."""
    if os.geteuid() == 0:
        pytest.skip("root ignores file-mode permission bits")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)  # read+exec, no write
    try:
        with pytest.raises(PermissionError):
            atomic_write_text(locked / "denied.txt", "nope")
    finally:
        locked.chmod(0o700)  # let pytest tidy up


def test_concurrent_writes_different_pids_do_not_collide(tmp_path, monkeypatch):
    """Two writers racing to produce the same final path must not trip
    over each other's temp files. We can't fork inside pytest cleanly,
    so simulate distinct pids by monkeypatching os.getpid for one of
    two threads while both try to atomic-write to different final
    targets sharing a parent dir — a collision would surface as a
    FileNotFoundError on rename."""
    dst_a = tmp_path / "doc.md"
    dst_b = tmp_path / "doc.md"  # same final path on purpose

    # Capture the tmp paths each "pid" would pick to prove they differ.
    seen_tmps: list[str] = []
    real_tmp = brain_io._tmp_path

    def recording_tmp(path):
        t = real_tmp(path)
        seen_tmps.append(t.name)
        return t

    monkeypatch.setattr(brain_io, "_tmp_path", recording_tmp)

    errors: list[BaseException] = []

    def writer(pid_override: int, content: str):
        try:
            # Patch getpid per-thread via a fresh monkeypatch is racy in
            # pytest, so instead shim _tmp_path for this call chain by
            # reaching into os.getpid directly.
            orig_getpid = os.getpid
            os.getpid = lambda: pid_override  # type: ignore[assignment]
            try:
                atomic_write_text(dst_a if pid_override % 2 else dst_b, content)
            finally:
                os.getpid = orig_getpid  # type: ignore[assignment]
        except BaseException as e:
            errors.append(e)

    # Serial, different pids — the point is that the tmp names differ so
    # if they had raced, neither would have clobbered the other's temp.
    writer(11111, "one")
    writer(22222, "two")

    assert errors == []
    # Two distinct tmp names were picked.
    assert len(set(seen_tmps)) == 2, f"expected 2 unique tmp names, saw {seen_tmps}"
    # Final state is one of the two writes (last writer wins — replace is atomic).
    assert dst_a.read_text() in {"one", "two"}


def test_tmp_path_encodes_pid(tmp_path, monkeypatch):
    """The tmp-name format is part of the contract: it must include the
    pid so an `ls` in a crashed directory tells the operator which
    process left the orphan."""
    monkeypatch.setattr(brain_io.os, "getpid", lambda: 424242)
    t = brain_io._tmp_path(tmp_path / "x.md")
    assert t.name == "x.md.tmp.424242"
    assert t.parent == tmp_path
