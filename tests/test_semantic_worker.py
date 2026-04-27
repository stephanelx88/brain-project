"""Tests for the persistent semantic embedding worker.

The worker's whole reason to exist: cold model load is ~10 s. When ingest
runs in a fresh Python process every WatchPaths fire, every change pays
that cost. The worker stays resident, model loaded, so each upsert costs
~0.5 s instead of ~15 s end-to-end.

These tests stub _embed so they don't need torch / sentence-transformers
and run in <1 s. The actual cold-vs-warm latency is measured live, not
in the test suite.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from pathlib import Path

import numpy as np
import pytest

from brain import semantic


def _short_sock_path() -> Path:
    """macOS limits AF_UNIX paths to ~104 chars; pytest's tmp_path is too long.
    Anchor sockets in /tmp directly so the kernel's bind() accepts them."""
    return Path(f"/tmp/bw-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock")


def _fake_embed(texts, batch_size=64):
    if not texts:
        return np.zeros((0, semantic.DIM), dtype=np.float32)
    out = []
    for t in texts:
        seed = abs(hash(t)) % (2**32)
        v = np.random.default_rng(seed).standard_normal(semantic.DIM).astype(np.float32)
        v /= np.linalg.norm(v) + 1e-9
        out.append(v)
    return np.stack(out)


@pytest.fixture
def tmp_worker(tmp_path, monkeypatch):
    """Spin up a worker on a temp socket, with semantic wired to a temp dir."""
    brain_dir = tmp_path / "brain"
    brain_dir.mkdir()

    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", brain_dir)
    monkeypatch.setattr(semantic, "VEC_DIR", brain_dir / ".vec")
    for attr, name in [
        ("FACTS_NPY", "facts.npy"), ("FACTS_JSON", "facts.json"),
        ("ENT_NPY", "entities.npy"), ("ENT_JSON", "entities.json"),
        ("NOTES_NPY", "notes.npy"), ("NOTES_JSON", "notes.json"),
        ("META_JSON", "meta.json"),
    ]:
        monkeypatch.setattr(semantic, attr, brain_dir / ".vec" / name)
    monkeypatch.setattr(semantic, "_embed", _fake_embed)
    # Pre-populate empty notes index so update_notes takes the incremental path.
    (brain_dir / ".vec").mkdir()
    np.save(semantic.NOTES_NPY, np.zeros((0, semantic.DIM), dtype=np.float32))
    semantic.NOTES_JSON.write_text("[]")
    semantic.META_JSON.write_text(json.dumps({"built_at": time.time()}))

    from brain import semantic_worker
    sock_path = _short_sock_path()
    monkeypatch.setattr(semantic_worker, "SOCKET_PATH", sock_path)
    monkeypatch.setattr(semantic_worker, "PID_FILE", brain_dir / ".semantic.pid")

    server = semantic_worker.build_server(sock_path)
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    deadline = time.time() + 2
    while not sock_path.exists() and time.time() < deadline:
        time.sleep(0.01)
    yield sock_path
    server.shutdown()
    server.server_close()
    if sock_path.exists():
        sock_path.unlink()


def _request(sock_path: Path, obj: dict, timeout: float = 5.0) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(str(sock_path))
    s.sendall((json.dumps(obj) + "\n").encode())
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf.split(b"\n", 1)[0])


def test_ping(tmp_worker):
    r = _request(tmp_worker, {"op": "ping"})
    assert r["ok"] is True
    assert r["pid"] > 0


def test_upsert_notes(tmp_worker):
    r = _request(tmp_worker, {
        "op": "upsert_notes",
        "items": [
            {"path": "a.md", "title": "Alpha", "body": "first body"},
            {"path": "b.md", "title": "Beta",  "body": "second body"},
        ],
    })
    assert r["ok"] is True
    assert r["changed"] == 2
    # Index file should reflect both notes.
    meta = json.loads(semantic.NOTES_JSON.read_text())
    assert {m["path"] for m in meta} == {"a.md", "b.md"}


def test_delete_notes(tmp_worker):
    _request(tmp_worker, {
        "op": "upsert_notes",
        "items": [{"path": "doomed.md", "title": "X", "body": "y"}],
    })
    r = _request(tmp_worker, {"op": "delete_notes", "paths": ["doomed.md"]})
    assert r["ok"] is True
    assert r["deleted"] == 1
    meta = json.loads(semantic.NOTES_JSON.read_text())
    assert all(m["path"] != "doomed.md" for m in meta)


def test_unknown_op(tmp_worker):
    r = _request(tmp_worker, {"op": "frobnicate"})
    assert r["ok"] is False
    assert "unknown" in r["error"].lower()


def test_bad_json(tmp_worker):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2.0)
    s.connect(str(tmp_worker))
    s.sendall(b"{not json\n")
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    s.close()
    r = json.loads(buf.split(b"\n", 1)[0])
    assert r["ok"] is False


def test_client_uses_worker(tmp_worker, monkeypatch):
    monkeypatch.setattr(semantic, "_worker_socket_path", lambda: tmp_worker)
    res = semantic.update_notes_via_worker(
        changed=[("c.md", "Charlie", "third body")],
        deleted_paths=[],
    )
    assert res.get("via_worker") is True
    assert res.get("changed") == 1


def test_incremental_facts_entities_op_calls_helper(tmp_worker, monkeypatch):
    """The new op must invoke semantic.incremental_update_facts_entities()
    inside the warm worker (not full build) — that's the whole point."""
    calls = {"n": 0}

    def fake_inc():
        calls["n"] += 1
        return {"facts_added": 2, "entities_added": 1, "incremental": True}

    monkeypatch.setattr(
        semantic, "incremental_update_facts_entities", fake_inc
    )

    r = _request(tmp_worker, {"op": "incremental_facts_entities"})
    assert r["ok"] is True
    assert calls["n"] == 1
    assert r["facts_added"] == 2
    assert r["entities_added"] == 1
    assert r["incremental"] is True
    assert "took_ms" in r


def test_update_facts_entities_via_worker_uses_socket(tmp_worker, monkeypatch):
    """The high-level helper must round-trip through the worker socket and
    surface `via_worker: True` so callers / tests can prove the warm path
    actually ran."""
    monkeypatch.setattr(semantic, "_worker_socket_path", lambda: tmp_worker)
    monkeypatch.setattr(
        semantic,
        "incremental_update_facts_entities",
        lambda: {"facts_added": 5, "entities_added": 3, "incremental": True},
    )
    res = semantic.update_facts_entities_via_worker()
    assert res.get("via_worker") is True
    assert res.get("facts_added") == 5
    assert res.get("entities_added") == 3


def test_update_facts_entities_via_worker_falls_back(tmp_path, monkeypatch):
    """When the worker socket is absent the helper must silently fall back
    to in-process incremental — never raise into the extract pipeline."""
    brain_dir = tmp_path / "brain"
    brain_dir.mkdir()
    monkeypatch.setattr(
        semantic, "_worker_socket_path",
        lambda: brain_dir / "no-such.sock",
    )

    fallback_called = {"n": 0}

    def fake_inc():
        fallback_called["n"] += 1
        return {"facts_added": 0, "entities_added": 0, "incremental": True}

    monkeypatch.setattr(
        semantic, "incremental_update_facts_entities", fake_inc
    )

    res = semantic.update_facts_entities_via_worker()
    assert fallback_called["n"] == 1
    assert res.get("via_worker") is not True
    assert res.get("incremental") is True


def test_update_facts_via_worker_alias_delegates(tmp_worker, monkeypatch):
    """`update_facts_via_worker` and `update_entities_via_worker` are sugar
    that delegate to the unified helper — verify they both round-trip
    through the same single socket call."""
    monkeypatch.setattr(semantic, "_worker_socket_path", lambda: tmp_worker)
    monkeypatch.setattr(
        semantic,
        "incremental_update_facts_entities",
        lambda: {"facts_added": 1, "entities_added": 1, "incremental": True},
    )
    r1 = semantic.update_facts_via_worker()
    r2 = semantic.update_entities_via_worker()
    assert r1.get("via_worker") is True
    assert r2.get("via_worker") is True


def test_client_falls_back_when_socket_missing(tmp_path, monkeypatch):
    brain_dir = tmp_path / "brain"
    brain_dir.mkdir()
    (brain_dir / ".vec").mkdir()

    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", brain_dir)
    monkeypatch.setattr(semantic, "VEC_DIR", brain_dir / ".vec")
    for attr, name in [
        ("NOTES_NPY", "notes.npy"), ("NOTES_JSON", "notes.json"),
        ("META_JSON", "meta.json"),
    ]:
        monkeypatch.setattr(semantic, attr, brain_dir / ".vec" / name)
    monkeypatch.setattr(semantic, "_embed", _fake_embed)
    monkeypatch.setattr(semantic, "_worker_socket_path",
                        lambda: brain_dir / "no-such.sock")

    np.save(semantic.NOTES_NPY, np.zeros((0, semantic.DIM), dtype=np.float32))
    semantic.NOTES_JSON.write_text("[]")
    semantic.META_JSON.write_text("{}")

    res = semantic.update_notes_via_worker(
        changed=[("d.md", "Delta", "fourth body")],
        deleted_paths=[],
    )
    assert res.get("via_worker") is not True
    assert res.get("changed") == 1
