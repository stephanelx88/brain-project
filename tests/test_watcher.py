"""Tests for brain.watcher (WS3 fs-event daemon)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest


@pytest.fixture
def tmp_vault(tmp_path, monkeypatch):
    vault = tmp_path / "brain"
    vault.mkdir()
    (vault / "entities" / "people").mkdir(parents=True)
    (vault / "raw").mkdir()

    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", vault)
    monkeypatch.setattr(config, "ENTITIES_DIR", vault / "entities")

    from brain import db
    monkeypatch.setattr(db, "DB_PATH", vault / ".brain.db")
    return vault


def test_should_handle_accepts_md_under_vault(tmp_vault):
    from brain import watcher
    note = tmp_vault / "hello.md"
    note.write_text("# hi")
    assert watcher._should_handle(note) is True


def test_should_handle_rejects_non_md(tmp_vault):
    from brain import watcher
    other = tmp_vault / "hello.txt"
    other.write_text("nope")
    assert watcher._should_handle(other) is False


def test_should_handle_rejects_machine_dirs(tmp_vault):
    from brain import watcher
    (tmp_vault / ".git").mkdir()
    junk = tmp_vault / ".git" / "commit.md"
    junk.write_text("git internals")
    assert watcher._should_handle(junk) is False

    transient = tmp_vault / "raw" / "session.md"
    transient.write_text("raw")
    assert watcher._should_handle(transient) is False


def test_should_handle_rejects_path_outside_vault(tmp_vault, tmp_path):
    from brain import watcher
    outside = tmp_path / "elsewhere.md"
    outside.write_text("not in vault")
    assert watcher._should_handle(outside) is False


def test_is_entity_file_requires_entities_subtree(tmp_vault):
    from brain import watcher
    ef = tmp_vault / "entities" / "people" / "anna.md"
    ef.write_text("- fact")
    assert watcher._is_entity_file(ef) is True

    note = tmp_vault / "random.md"
    note.write_text("not an entity")
    assert watcher._is_entity_file(note) is False


def test_dispatch_note_calls_ingest_one(tmp_vault, monkeypatch):
    from brain import watcher

    calls: list[Path] = []

    def fake_ingest_one(path):
        calls.append(Path(path))
        return {"status": "changed", "rel_path": str(path), "changed": True, "deleted": False}

    from brain import ingest_notes
    monkeypatch.setattr(ingest_notes, "ingest_one", fake_ingest_one)

    # Silence semantic side-effect.
    monkeypatch.setattr(
        "brain.semantic.ensure_built",
        lambda *a, **k: None,
    )

    note = tmp_vault / "journal.md"
    note.write_text("# journal")
    watcher._dispatch(note, verbose=False)
    assert calls == [note]


def test_dispatch_entity_calls_upsert(tmp_vault, monkeypatch):
    from brain import watcher

    upsert_calls: list[Path] = []
    monkeypatch.setattr(
        "brain.db.upsert_entity_from_file",
        lambda p: upsert_calls.append(Path(p)) or 1,
    )
    monkeypatch.setattr(
        "brain.semantic.ensure_built",
        lambda *a, **k: None,
    )
    # ingest_one must NOT fire for entity-path writes.
    from brain import ingest_notes
    ingest_calls = []
    monkeypatch.setattr(ingest_notes, "ingest_one",
                        lambda p: ingest_calls.append(p) or {"status": "skipped"})

    ef = tmp_vault / "entities" / "people" / "tom.md"
    ef.write_text("- tom fact")
    watcher._dispatch(ef, verbose=False)
    assert upsert_calls == [ef]
    assert ingest_calls == []


def test_dispatch_swallows_exceptions(tmp_vault, monkeypatch):
    from brain import watcher
    from brain import ingest_notes

    def boom(_p):
        raise RuntimeError("boom")
    monkeypatch.setattr(ingest_notes, "ingest_one", boom)
    monkeypatch.setattr("brain.semantic.ensure_built", lambda *a, **k: None)

    # Must not raise.
    watcher._dispatch(tmp_vault / "x.md", verbose=False)


def test_dispatch_bumps_freshness_watermarks(tmp_vault, monkeypatch):
    from brain import watcher
    from brain import freshness

    # Neutralise the real ingest / upsert to isolate the watermark.
    monkeypatch.setattr("brain.ingest_notes.ingest_one",
                        lambda p: {"status": "changed"})
    monkeypatch.setattr("brain.db.upsert_entity_from_file", lambda p: 1)
    monkeypatch.setattr("brain.semantic.ensure_built", lambda *a, **k: None)

    before = freshness.load()["notes"]
    watcher._dispatch(tmp_vault / "note.md", verbose=False)
    after = freshness.load()["notes"]
    assert after > before

    before_ent = freshness.load()["entities"]
    watcher._dispatch(tmp_vault / "entities" / "people" / "t.md", verbose=False)
    after_ent = freshness.load()["entities"]
    assert after_ent > before_ent


def test_debouncer_coalesces_bursts():
    from brain.watcher import _Debouncer

    seen: list[Path] = []

    def sink(path, verbose=False):
        seen.append(path)

    deb = _Debouncer(sink, delay=0.05)
    p = Path("/tmp/nonexistent/file.md")
    for _ in range(10):
        deb.arm(p)
        time.sleep(0.005)  # within debounce window
    # Wait for the debounce timer to fire
    time.sleep(0.15)
    assert len(seen) == 1
    assert seen[0] == p


def test_debouncer_separates_distinct_paths():
    from brain.watcher import _Debouncer

    seen: list[Path] = []
    done = threading.Event()

    def sink(path, verbose=False):
        seen.append(path)
        if len(seen) >= 2:
            done.set()

    deb = _Debouncer(sink, delay=0.05)
    deb.arm(Path("/x/a.md"))
    deb.arm(Path("/x/b.md"))
    assert done.wait(timeout=1.0), "both paths should fire"
    assert {p.name for p in seen} == {"a.md", "b.md"}


def test_debouncer_drain_fires_pending():
    from brain.watcher import _Debouncer

    seen: list[Path] = []

    def sink(path, verbose=False):
        seen.append(path)

    deb = _Debouncer(sink, delay=10.0)  # long delay; drain must pre-empt
    deb.arm(Path("/x/c.md"))
    deb.arm(Path("/x/d.md"))
    deb.drain()
    assert {p.name for p in seen} == {"c.md", "d.md"}


def test_watch_vault_on_windows_returns_zero(monkeypatch, tmp_vault):
    from brain import watcher
    monkeypatch.setattr("platform.system", lambda: "Windows")
    assert watcher.watch_vault(verbose=False) == 0


def test_install_unit_non_linux_returns_one(monkeypatch, tmp_vault):
    from brain import watcher
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    assert watcher.install_unit() == 1


def test_install_unit_renders_template(monkeypatch, tmp_vault, tmp_path):
    """Unit file renders with BRAIN_DIR + HOME + BRAIN_CMD substitutions."""
    from brain import watcher
    monkeypatch.setattr("platform.system", lambda: "Linux")
    # Redirect systemd user dir to a tmp spot + block `systemctl` side effects.
    systemd_dir = tmp_path / "systemd-user"
    monkeypatch.setattr(watcher, "_systemd_user_dir", lambda: systemd_dir)
    monkeypatch.setattr(watcher.subprocess, "run", lambda *a, **k: None)
    # Force a predictable resolve for `brain` to avoid PATH-sensitivity.
    monkeypatch.setattr(watcher, "_which", lambda *a: "/usr/bin/brain")

    rc = watcher.install_unit(enable=False)
    assert rc == 0
    unit = systemd_dir / "brain-watcher.service"
    assert unit.exists()
    body = unit.read_text()
    assert "ExecStart=/usr/bin/brain watch" in body
    assert str(tmp_vault) in body
