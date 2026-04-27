"""Persistent semantic-embedding worker.

Why this exists: cold-loading sentence-transformers takes ~10 s. The launchd
plist re-runs auto-extract.sh every WatchPaths fire, in a fresh Python
process, so every changed note paid that cost — driving end-to-end semantic
latency to ~15 s and missing the 10 s sync goal (Goal 4 in the project spec).

This worker stays resident under launchd (KeepAlive=true), holds the model
warm, and exposes a one-shot UNIX-socket protocol so `ingest_notes` can hand
off the changed-paths diff without paying cold-start.

Protocol — newline-delimited JSON, one request → one reply:

  request                                            reply
  -------                                            -----
  {"op":"ping"}                                      {"ok":true,"pid":N,"model_warm":bool}
  {"op":"upsert_notes","items":[{path,title,body}]}  {"ok":true,"changed":N,"took_ms":M}
  {"op":"delete_notes","paths":["a.md","b.md"]}      {"ok":true,"deleted":N,"took_ms":M}
  {"op":"incremental_facts_entities"}                {"ok":true,"facts_added":N,
                                                      "entities_added":M,"took_ms":T}
  {"op":"shutdown"}                                  {"ok":true,"shutting_down":true}

The `incremental_facts_entities` op is what auto_extract / note_extract /
promote want post-write: the warm worker reads the DB high-water mark, embeds
only newly-added facts/entities, and appends them to .vec/{facts,entities}.npy.
Without this op those callers paid ~10 s cold-load + ~1.6 s full rebuild on
every batch — the worker was warming the model only for the notes path.

Failure modes the client must handle (see semantic.update_notes_via_worker):
  - socket missing  → worker not running → fall back in-process
  - connect refused → worker dying       → fall back in-process
  - timeout         → worker stuck       → fall back in-process

The worker is single-threaded by design — embedding runs on one thread, and
the index files (.vec/notes.npy + .vec/notes.json) have a single writer.
That keeps the on-disk layout race-free without adding lock files.
"""

from __future__ import annotations

import json
import os
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path

import brain.config as config
from brain import semantic


SOCKET_PATH = config.BRAIN_DIR / ".semantic.sock"
PID_FILE = config.BRAIN_DIR / ".semantic.pid"

# The on-disk semantic index has a single writer; serialize so two threaded
# requests cannot race during a read-modify-write of notes.npy / notes.json.
_INDEX_LOCK = threading.Lock()


class _Handler(socketserver.StreamRequestHandler):
    # Per-connection read timeout. Without this, a client that connects but
    # never sends would pin the worker forever (esp. with a single-threaded
    # server). 60 s is generous: legitimate requests reply in <1 s.
    timeout = 60

    def handle(self) -> None:
        # One-shot protocol: read a single request line, reply, return.
        # The original loop-over-rfile design pinned the connection open
        # waiting for "the next line", which blocked all other clients on
        # a single-threaded server.
        line = self.rfile.readline()
        if not line:
            return
        try:
            req = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._reply({"ok": False, "error": f"bad json: {exc}"})
            return
        try:
            resp = self._dispatch(req)
        except Exception as exc:  # noqa: BLE001
            resp = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self._reply(resp)
        if resp.get("shutting_down"):
            self.server.shutdown_thread()

    def _reply(self, obj: dict) -> None:
        self.wfile.write((json.dumps(obj) + "\n").encode("utf-8"))
        self.wfile.flush()

    def _dispatch(self, req: dict) -> dict:
        op = req.get("op")
        if op == "ping":
            return {
                "ok": True,
                "pid": os.getpid(),
                "model_warm": semantic._model is not None,
            }
        if op == "upsert_notes":
            items = req.get("items") or []
            t0 = time.time()
            with _INDEX_LOCK:
                res = semantic.update_notes(
                    changed=[
                        (i["path"], i.get("title", ""), i.get("body", ""))
                        for i in items
                    ],
                    deleted_paths=[],
                )
            took_ms = int((time.time() - t0) * 1000)
            print(f"upsert {len(items)} items took {took_ms}ms",
                  file=sys.stderr, flush=True)
            return {"ok": True, "took_ms": took_ms, **res}
        if op == "delete_notes":
            paths = req.get("paths") or []
            t0 = time.time()
            with _INDEX_LOCK:
                res = semantic.update_notes(changed=[], deleted_paths=paths)
            took_ms = int((time.time() - t0) * 1000)
            print(f"delete {len(paths)} paths took {took_ms}ms",
                  file=sys.stderr, flush=True)
            return {"ok": True, "took_ms": took_ms, **res}
        if op == "incremental_facts_entities":
            # Warm-model fast path for the post-extract pipeline. The
            # underlying helper reads the DB, finds rows past the last
            # high-water mark, embeds them, appends to .vec/{facts,
            # entities}.npy. Held under _INDEX_LOCK because it does a
            # read-modify-write of the same on-disk arrays a concurrent
            # upsert_notes call would touch (meta.json, npy bundle).
            t0 = time.time()
            with _INDEX_LOCK:
                res = semantic.incremental_update_facts_entities()
            took_ms = int((time.time() - t0) * 1000)
            print(
                f"incremental_facts_entities took {took_ms}ms: {res}",
                file=sys.stderr, flush=True,
            )
            return {"ok": True, "took_ms": took_ms, **res}
        if op == "shutdown":
            return {"ok": True, "shutting_down": True}
        return {"ok": False, "error": f"unknown op: {op}"}


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    # Threaded so a slow / hung client cannot stall other ingest calls.
    # daemon_threads=True so threads die with the worker on shutdown.
    allow_reuse_address = True
    daemon_threads = True

    def shutdown_thread(self) -> None:
        # serve_forever() blocks the main thread; spawn a tiny helper that
        # calls shutdown() so we don't deadlock.
        import threading
        threading.Thread(target=self.shutdown, daemon=True).start()


def build_server(socket_path: Path) -> _Server:
    """Construct a configured _Server bound to socket_path. Used by tests."""
    socket_path = Path(socket_path)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        try:
            socket_path.unlink()
        except OSError:
            pass
    server = _Server(str(socket_path), _Handler)
    try:
        os.chmod(socket_path, 0o600)
    except OSError:
        pass
    return server


def serve(socket_path: Path = SOCKET_PATH, warm_model: bool = True) -> None:
    """Run the worker until shutdown. Logs to stderr (launchd captures it)."""
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    server = build_server(socket_path)
    if warm_model:
        # Pay the cold-start once, at boot — this is the whole point.
        t0 = time.time()
        semantic._get_model()
        print(
            f"semantic-worker: model warm in {time.time()-t0:.1f}s, "
            f"socket={socket_path}, pid={os.getpid()}",
            file=sys.stderr, flush=True,
        )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        try:
            socket_path.unlink()
        except OSError:
            pass
        try:
            PID_FILE.unlink()
        except OSError:
            pass


def main() -> None:
    serve()


if __name__ == "__main__":
    main()
