# Realtime Named-Session Messaging — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `brain.runtime` subsystem and three MCP tools (`brain_send`, `brain_inbox`, `brain_set_name`) so two live Claude/Cursor sessions can push messages to each other by human-readable names without the user copying between terminals.

**Architecture:** A new package `src/brain/runtime/` provides a file-tree inbox + name registry under `~/.brain-runtime/` (separate from `BRAIN_DIR` vault). A `UserPromptSubmit` hook reads each receiver's `pending/` directory, formats a `<system-reminder>` block, and atomically moves the messages to `delivered/`. The runtime package never imports `brain.entities`, `brain.graph`, or `brain.semantic` — knowledge persistence happens via the existing harvest+extract pipeline that already reads session transcript jsonl files.

**Tech Stack:** Python 3.11+, FastMCP (existing dep), pytest, bash for the hook wrapper. No new third-party deps.

**Spec:** `docs/realtime-named-sessions-design.md` (committed in 974a87e).

---

## File Map

**Create:**
- `src/brain/runtime/__init__.py` — re-exports the public surface
- `src/brain/runtime/paths.py` — runtime root resolution
- `src/brain/runtime/names.py` — name registry (read/write JSON files under `names/`)
- `src/brain/runtime/session_id.py` — own-UUID detection chain
- `src/brain/runtime/inbox.py` — message storage primitives (write pending, list, mark delivered)
- `src/brain/runtime/resolve.py` — recipient resolution (UUID vs name, scope rules)
- `src/brain/runtime/surface.py` — SystemReminder formatter
- `src/brain/runtime/hook.py` — entry point invoked by the UserPromptSubmit hook
- `src/brain/runtime/gc.py` — TTL prune CLI
- `bin/inbox-surface-hook.sh.template` — shell wrapper with empty-inbox fast path
- `tests/test_runtime_paths.py`
- `tests/test_runtime_names.py`
- `tests/test_runtime_session_id.py`
- `tests/test_runtime_inbox.py`
- `tests/test_runtime_resolve.py`
- `tests/test_runtime_surface.py`
- `tests/test_runtime_hook.py`
- `tests/test_runtime_isolation.py`
- `tests/test_runtime_perf.py`
- `tests/test_runtime_gc.py`
- `tests/test_runtime_integration.py` — `@pytest.mark.integration`, skipped by default

**Modify:**
- `src/brain/mcp_server.py` — add `brain_send`, `brain_inbox`, `brain_set_name` implementations
- `src/brain/mcp_server_write.py` — add `brain_send` and `brain_set_name` to `WRITE_TOOLS`
- `src/brain/mcp_server_read.py` — add `brain_inbox` to `READ_TOOLS`
- `src/brain/install_hooks.py` — add `install_claude_user_prompt_submit` and remove counterpart; new `--no-inbox-hook` arg path
- `tests/test_install_hooks.py` — cover UserPromptSubmit install/remove
- `tests/test_mcp_server_split.py` — cover the new tools in the partition assertion
- `pyproject.toml` — register the `integration` pytest marker
- `bin/install.sh` — pass the inbox-hook flag through to `python -m brain.install_hooks`

---

## Conventions

- All file writes go through `brain.io.atomic_write_text` / `atomic_write_bytes`.
- All time stamps in UTC ISO 8601 (`datetime.now(timezone.utc).isoformat(timespec="milliseconds")`).
- ULIDs are generated with a tiny inline implementation (no new dep) — see Task 4.
- Tests use `tmp_path` fixture for isolated runtime roots; tests set `BRAIN_RUNTIME_DIR` env var rather than relying on `~/.brain-runtime`.
- Each task ends with a single small commit. Commit messages start with `feat(runtime):`, `feat(mcp):`, `feat(hooks):`, `test(runtime):`, etc., matching the existing repo convention.

---

## Task 1: Bootstrap `brain.runtime` package + paths module

**Files:**
- Create: `src/brain/runtime/__init__.py`
- Create: `src/brain/runtime/paths.py`
- Test: `tests/test_runtime_paths.py`

- [ ] **Step 1: Write failing tests**

`tests/test_runtime_paths.py`:

```python
"""Runtime root + subdirectory resolution."""
from __future__ import annotations

from pathlib import Path

import pytest

from brain.runtime import paths


def test_default_root_is_home_dot_brain_runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("BRAIN_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert paths.runtime_root() == tmp_path / ".brain-runtime"


def test_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_RUNTIME_DIR", str(tmp_path / "custom"))
    assert paths.runtime_root() == tmp_path / "custom"


def test_inbox_dir_for_uuid(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_RUNTIME_DIR", str(tmp_path))
    uuid = "ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2"
    assert paths.inbox_pending_dir(uuid) == tmp_path / "inbox" / uuid / "pending"
    assert paths.inbox_delivered_dir(uuid) == tmp_path / "inbox" / uuid / "delivered"


def test_names_file_for_uuid(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_RUNTIME_DIR", str(tmp_path))
    uuid = "ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2"
    assert paths.name_file(uuid) == tmp_path / "names" / f"{uuid}.json"


def test_log_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_RUNTIME_DIR", str(tmp_path))
    assert paths.hook_log_path() == tmp_path / "log" / "inbox-hook.log"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_runtime_paths.py -v`
Expected: `ModuleNotFoundError: No module named 'brain.runtime'`

- [ ] **Step 3: Create the package + paths module**

`src/brain/runtime/__init__.py`:

```python
"""Runtime transport layer for inter-session messaging.

Strictly separate from the vault (BRAIN_DIR / entities / journal / etc.).
This package MUST NOT import brain.entities, brain.graph, brain.semantic,
or any module that touches curated knowledge — see
tests/test_runtime_isolation.py for the enforcement check.
"""
from __future__ import annotations
```

`src/brain/runtime/paths.py`:

```python
"""Filesystem layout for the runtime transport layer.

All paths live under BRAIN_RUNTIME_DIR (default ~/.brain-runtime),
which is intentionally OUTSIDE BRAIN_DIR so transport never touches
the curated-knowledge pipeline.
"""
from __future__ import annotations

import os
from pathlib import Path


def runtime_root() -> Path:
    """Resolve the runtime root from BRAIN_RUNTIME_DIR, default ~/.brain-runtime."""
    raw = os.environ.get("BRAIN_RUNTIME_DIR")
    if raw:
        return Path(os.path.expanduser(os.path.expandvars(raw)))
    return Path.home() / ".brain-runtime"


def inbox_dir() -> Path:
    return runtime_root() / "inbox"


def inbox_pending_dir(session_uuid: str) -> Path:
    return inbox_dir() / session_uuid / "pending"


def inbox_delivered_dir(session_uuid: str) -> Path:
    return inbox_dir() / session_uuid / "delivered"


def names_dir() -> Path:
    return runtime_root() / "names"


def name_file(session_uuid: str) -> Path:
    return names_dir() / f"{session_uuid}.json"


def hook_log_path() -> Path:
    return runtime_root() / "log" / "inbox-hook.log"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_runtime_paths.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/brain/runtime/__init__.py src/brain/runtime/paths.py tests/test_runtime_paths.py
git commit -m "feat(runtime): bootstrap brain.runtime package + paths module

Out-of-vault filesystem layout for inter-session messaging.
Default ~/.brain-runtime, BRAIN_RUNTIME_DIR overrides.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Name registry (read/write/lookup/validate)

**Files:**
- Create: `src/brain/runtime/names.py`
- Test: `tests/test_runtime_names.py`

- [ ] **Step 1: Write failing tests**

`tests/test_runtime_names.py`:

```python
"""Name registry — set, get, lookup, validation, collision."""
from __future__ import annotations

import pytest

from brain.runtime import names


@pytest.fixture(autouse=True)
def _runtime_root(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_RUNTIME_DIR", str(tmp_path))


def test_default_name_format_lowercase():
    assert names.default_name("Honeywell-Forge-Cognition", "68293") == \
        "honeywell-forge-cognition-68293"


def test_default_name_with_slashes_normalized():
    assert names.default_name("RICHARDSON/master/0425", "42139") == \
        "richardson-master-0425-42139"


def test_default_name_strips_unicode():
    # Non-[a-z0-9-] chars get replaced with '-'
    assert names.default_name("Đôi Dép", "1234") == "doi-dep-1234"


def test_validate_user_name_ok():
    assert names.validate_user_name("planner") is None


def test_validate_user_name_lowercase_required():
    err = names.validate_user_name("Planner")
    assert err and "lowercase" in err


def test_validate_user_name_reserved():
    for reserved in ("peer", "self", "all", "me"):
        err = names.validate_user_name(reserved)
        assert err and "reserved" in err


def test_validate_user_name_too_long():
    err = names.validate_user_name("a" * 65)
    assert err and "length" in err


def test_validate_user_name_bad_chars():
    err = names.validate_user_name("plan/ner")
    assert err and "chars" in err


def test_register_and_get():
    uuid = "ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2"
    entry = names.register(
        uuid=uuid,
        name="planner",
        project="acme",
        cwd="/tmp/acme",
        pid=1234,
    )
    assert entry["name"] == "planner"
    got = names.get(uuid)
    assert got["uuid"] == uuid
    assert got["name"] == "planner"
    assert got["project"] == "acme"


def test_lookup_by_name_in_project():
    uuid = "ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2"
    names.register(uuid, "planner", "acme", "/tmp/acme", 1)
    assert names.lookup_by_name("planner", project="acme") == uuid


def test_lookup_by_name_not_found_returns_none():
    assert names.lookup_by_name("ghost", project="acme") is None


def test_lookup_by_name_wrong_project_returns_none():
    names.register("u1", "planner", "acme", "/tmp/a", 1)
    assert names.lookup_by_name("planner", project="other") is None


def test_set_name_collision_in_same_project():
    names.register("u1", "planner", "acme", "/tmp/a", 1)
    names.register("u2", "honey-2", "acme", "/tmp/b", 2)
    err = names.set_name("u2", "planner")
    assert err == "name_taken"


def test_set_name_same_name_different_project_ok():
    names.register("u1", "planner", "acme", "/tmp/a", 1)
    names.register("u2", "honey-2", "other", "/tmp/b", 2)
    err = names.set_name("u2", "planner")
    assert err is None
    assert names.get("u2")["name"] == "planner"


def test_delete_clears_entry():
    names.register("u1", "planner", "acme", "/tmp/a", 1)
    names.delete("u1")
    assert names.get("u1") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_runtime_names.py -v`
Expected: `ImportError: cannot import name 'names' from 'brain.runtime'`

- [ ] **Step 3: Implement `names.py`**

`src/brain/runtime/names.py`:

```python
"""Per-session name registry.

One JSON file per session under ~/.brain-runtime/names/<uuid>.json.
Names are scoped per-project; the same name can exist in two
different projects without collision.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Optional

from brain.io import atomic_write_text
from brain.runtime import paths

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_RESERVED = frozenset({"peer", "self", "all", "me"})
_PROJECT_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def normalize_project(project: str) -> str:
    """Lowercase + replace non-alphanumerics with '-', collapse runs, strip ends."""
    s = _PROJECT_NORMALIZE_RE.sub("-", (project or "").lower()).strip("-")
    return s or "unknown"


def default_name(project: str, short_id: str) -> str:
    """Build the default-name format `<normalized-project>-<short_id>`."""
    return f"{normalize_project(project)}-{short_id}"


def validate_user_name(name: str) -> Optional[str]:
    """Return error code (str) or None if valid.

    Error codes: "lowercase", "length", "chars", "reserved".
    """
    if not name:
        return "length"
    if name in _RESERVED:
        return "reserved"
    if name != name.lower():
        return "lowercase"
    if len(name) > 64:
        return "length"
    if not _NAME_RE.match(name):
        return "chars"
    return None


def _write(uuid: str, payload: dict) -> None:
    atomic_write_text(paths.name_file(uuid), json.dumps(payload, indent=2) + "\n")


def register(
    uuid: str,
    name: str,
    project: str,
    cwd: str,
    pid: int | None,
) -> dict:
    """Write a name registry entry. Overwrites any prior entry for `uuid`."""
    entry = {
        "uuid": uuid,
        "name": name,
        "project": normalize_project(project),
        "cwd": cwd,
        "pid": pid,
        "set_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }
    _write(uuid, entry)
    return entry


def get(uuid: str) -> Optional[dict]:
    p = paths.name_file(uuid)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


def all_entries() -> list[dict]:
    d = paths.names_dir()
    if not d.exists():
        return []
    out: list[dict] = []
    for p in d.iterdir():
        if not p.suffix == ".json":
            continue
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def lookup_by_name(name: str, project: str) -> Optional[str]:
    """Return the UUID matching (name, project) or None."""
    project = normalize_project(project)
    name = name.lower()
    for entry in all_entries():
        if entry.get("name") == name and entry.get("project") == project:
            return entry["uuid"]
    return None


def set_name(uuid: str, new_name: str) -> Optional[str]:
    """Rename the entry for `uuid`. Returns error code or None.

    Error codes: validation codes from validate_user_name + "name_taken",
    "no_entry".
    """
    err = validate_user_name(new_name)
    if err:
        return err
    current = get(uuid)
    if not current:
        return "no_entry"
    project = current["project"]
    other_uuid = lookup_by_name(new_name, project)
    if other_uuid and other_uuid != uuid:
        return "name_taken"
    current["name"] = new_name
    current["set_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    _write(uuid, current)
    return None


def delete(uuid: str) -> None:
    p = paths.name_file(uuid)
    if p.exists():
        p.unlink()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_runtime_names.py -v`
Expected: 13 passed

- [ ] **Step 5: Commit**

```bash
git add src/brain/runtime/names.py tests/test_runtime_names.py
git commit -m "feat(runtime): name registry with per-project scope and validation

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Own-UUID detection chain

**Files:**
- Create: `src/brain/runtime/session_id.py`
- Test: `tests/test_runtime_session_id.py`

- [ ] **Step 1: Write failing tests**

`tests/test_runtime_session_id.py`:

```python
"""Detect the calling process's session UUID."""
from __future__ import annotations

import json

import pytest

from brain.runtime import session_id


def test_env_var_wins(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", "ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2")
    assert session_id.detect_own_uuid() == "ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2"


def test_env_var_invalid_uuid_falls_through(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_SESSION_ID", "not-a-uuid")
    monkeypatch.setattr(session_id, "_claude_sessions_dir",
                        lambda: tmp_path / ".claude" / "sessions")
    monkeypatch.setattr(session_id, "_get_ppid", lambda: 99999)
    assert session_id.detect_own_uuid() is None


def test_ppid_lookup_when_env_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    sessions_dir = tmp_path / ".claude" / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "12345.json").write_text(json.dumps({
        "session_id": "ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2",
        "cwd": "/tmp/acme",
    }))
    monkeypatch.setattr(session_id, "_claude_sessions_dir", lambda: sessions_dir)
    monkeypatch.setattr(session_id, "_get_ppid", lambda: 12345)
    assert session_id.detect_own_uuid() == "ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2"


def test_returns_none_when_nothing_works(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setattr(session_id, "_claude_sessions_dir", lambda: tmp_path / "missing")
    monkeypatch.setattr(session_id, "_get_ppid", lambda: 99999)
    assert session_id.detect_own_uuid() is None


def test_short_id_from_pid_when_known(monkeypatch):
    monkeypatch.setattr(session_id, "_get_ppid", lambda: 68293)
    assert session_id.short_id_for_default_name("uuid-doesnt-matter", source="claude") == "68293"


def test_short_id_from_uuid_for_cursor():
    # Cursor has no PID exposure; use first 8 chars of UUID
    out = session_id.short_id_for_default_name(
        "ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2", source="cursor"
    )
    assert out == "ab2b1fa6"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_runtime_session_id.py -v`
Expected: ImportError

- [ ] **Step 3: Implement `session_id.py`**

`src/brain/runtime/session_id.py`:

```python
"""Detect own session UUID for tools and hooks running inside a session.

Resolution chain:
  1. CLAUDE_SESSION_ID env var (if Claude Code exposes it)
  2. Parent PID lookup against ~/.claude/sessions/<pid>.json (matches
     brain.live_sessions' liveness check)
  3. None — caller decides how to surface the failure
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _claude_sessions_dir() -> Path:
    return Path.home() / ".claude" / "sessions"


def _get_ppid() -> int:
    return os.getppid()


def _is_uuid(s: str) -> bool:
    return bool(_UUID_RE.match(s or ""))


def detect_own_uuid() -> Optional[str]:
    """Return calling process's session UUID, or None if undetectable.

    Tries CLAUDE_SESSION_ID env first; falls back to PPID lookup.
    """
    env = os.environ.get("CLAUDE_SESSION_ID", "").strip()
    if env and _is_uuid(env):
        return env
    if env and not _is_uuid(env):
        # Misconfigured env — don't trust it, fall through.
        pass

    sdir = _claude_sessions_dir()
    if sdir.is_dir():
        ppid = _get_ppid()
        cand = sdir / f"{ppid}.json"
        if cand.exists():
            try:
                data = json.loads(cand.read_text())
            except (OSError, json.JSONDecodeError):
                return None
            sid = data.get("session_id", "").strip()
            if _is_uuid(sid):
                return sid

    return None


def short_id_for_default_name(uuid: str, *, source: str) -> str:
    """Choose the per-session short id used in the default name.

    Claude:  parent PID (5 digits typically, matches `ps` output)
    Cursor:  first 8 chars of UUID (no PID mapping available)
    """
    if source == "claude":
        return str(_get_ppid())
    return (uuid or "").split(":", 1)[-1][:8]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_runtime_session_id.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/brain/runtime/session_id.py tests/test_runtime_session_id.py
git commit -m "feat(runtime): own-UUID detection (env -> ppid lookup chain)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Inbox storage primitives

**Files:**
- Create: `src/brain/runtime/inbox.py`
- Test: `tests/test_runtime_inbox.py`

- [ ] **Step 1: Write failing tests**

`tests/test_runtime_inbox.py`:

```python
"""Inbox storage: write pending, list, mark delivered, prune."""
from __future__ import annotations

import json
import time

import pytest

from brain.runtime import inbox, paths


@pytest.fixture(autouse=True)
def _runtime_root(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_RUNTIME_DIR", str(tmp_path))


def test_send_writes_pending_file():
    msg = inbox.send(
        to_uuid="receiver-uuid",
        from_uuid="sender-uuid",
        from_name_at_send="planner",
        to_name_at_send="executor",
        body="hello",
    )
    assert msg["id"]
    pending_files = list(paths.inbox_pending_dir("receiver-uuid").iterdir())
    assert len(pending_files) == 1
    assert pending_files[0].name == f"{msg['id']}.json"
    payload = json.loads(pending_files[0].read_text())
    assert payload["body"] == "hello"
    assert payload["from_uuid"] == "sender-uuid"
    assert payload["to_uuid"] == "receiver-uuid"


def test_send_rejects_oversize_body():
    big = "x" * (32 * 1024 + 1)
    with pytest.raises(inbox.BodyTooLarge):
        inbox.send(
            to_uuid="receiver-uuid",
            from_uuid="sender-uuid",
            from_name_at_send="planner",
            to_name_at_send="executor",
            body=big,
        )


def test_list_pending_returns_envelopes_in_ulid_order():
    inbox.send("rcv", "snd", "a", "b", "first")
    time.sleep(0.002)
    inbox.send("rcv", "snd", "a", "b", "second")
    msgs = inbox.list_pending("rcv")
    assert [m["body"] for m in msgs] == ["first", "second"]


def test_list_pending_empty():
    assert inbox.list_pending("nobody") == []


def test_mark_delivered_moves_files():
    msg = inbox.send("rcv", "snd", "a", "b", "hello")
    inbox.mark_delivered("rcv", [msg["id"]])
    assert list(paths.inbox_pending_dir("rcv").iterdir()) == []
    delivered = list(paths.inbox_delivered_dir("rcv").iterdir())
    assert len(delivered) == 1
    assert delivered[0].name == f"{msg['id']}.json"


def test_mark_delivered_idempotent_on_missing():
    # Second call with same id is a no-op, no exception
    msg = inbox.send("rcv", "snd", "a", "b", "hello")
    inbox.mark_delivered("rcv", [msg["id"]])
    inbox.mark_delivered("rcv", [msg["id"]])  # no raise
    assert list(paths.inbox_pending_dir("rcv").iterdir()) == []


def test_prune_delivered_removes_old():
    msg = inbox.send("rcv", "snd", "a", "b", "hello")
    inbox.mark_delivered("rcv", [msg["id"]])
    delivered_file = paths.inbox_delivered_dir("rcv") / f"{msg['id']}.json"
    # Force ancient mtime
    import os
    old = time.time() - (10 * 86400)
    os.utime(delivered_file, (old, old))
    pruned = inbox.prune_delivered("rcv", ttl_days=7)
    assert pruned == 1
    assert not delivered_file.exists()


def test_ulid_monotonic_and_unique():
    ids = {inbox._ulid() for _ in range(1000)}
    assert len(ids) == 1000
    sorted_ids = sorted(ids)
    # ULIDs are time-prefixed, so sorted should be roughly creation order
    # (we don't assert strict monotonicity within ms; just uniqueness)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_runtime_inbox.py -v`
Expected: ImportError

- [ ] **Step 3: Implement `inbox.py`**

`src/brain/runtime/inbox.py`:

```python
"""Inbox storage primitives.

File-tree layout under BRAIN_RUNTIME_DIR/inbox/<receiver-uuid>/:
    pending/<ulid>.json    — unread, FIFO by ULID
    delivered/<ulid>.json  — read; pruned at TTL
"""
from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Iterable

from brain.io import atomic_write_text
from brain.runtime import paths

MAX_BODY_BYTES = 32 * 1024  # 32 KiB
DEFAULT_DELIVERED_TTL_DAYS = 7

_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class BodyTooLarge(ValueError):
    """Raised when a message body exceeds MAX_BODY_BYTES."""


def _ulid(now_ms: int | None = None) -> str:
    """Generate a Crockford-base32 ULID — 48 bits time + 80 bits randomness."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    rand = secrets.randbits(80)
    encoded_time = _b32encode(now_ms, 10)
    encoded_rand = _b32encode(rand, 16)
    return encoded_time + encoded_rand


def _b32encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_CROCKFORD_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def send(
    to_uuid: str,
    from_uuid: str,
    from_name_at_send: str,
    to_name_at_send: str,
    body: str,
) -> dict:
    """Write a message into receiver's pending/. Returns the envelope dict."""
    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise BodyTooLarge(
            f"body exceeds {MAX_BODY_BYTES} bytes "
            f"({len(body.encode('utf-8'))} given)"
        )
    msg_id = _ulid()
    envelope = {
        "id": msg_id,
        "from_uuid": from_uuid,
        "from_name_at_send": from_name_at_send,
        "to_uuid": to_uuid,
        "to_name_at_send": to_name_at_send,
        "body": body,
        "sent_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }
    target = paths.inbox_pending_dir(to_uuid) / f"{msg_id}.json"
    atomic_write_text(target, json.dumps(envelope, ensure_ascii=False, indent=2) + "\n")
    return envelope


def list_pending(to_uuid: str) -> list[dict]:
    """Return all pending envelopes for `to_uuid`, sorted by id (ULID = time-ordered)."""
    pdir = paths.inbox_pending_dir(to_uuid)
    if not pdir.exists():
        return []
    out: list[dict] = []
    for p in sorted(pdir.iterdir()):
        if not p.suffix == ".json":
            continue
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def list_delivered(to_uuid: str) -> list[dict]:
    ddir = paths.inbox_delivered_dir(to_uuid)
    if not ddir.exists():
        return []
    out: list[dict] = []
    for p in sorted(ddir.iterdir()):
        if not p.suffix == ".json":
            continue
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def mark_delivered(to_uuid: str, message_ids: Iterable[str]) -> int:
    """Atomic-rename matching pending/<id>.json to delivered/. Idempotent."""
    pdir = paths.inbox_pending_dir(to_uuid)
    ddir = paths.inbox_delivered_dir(to_uuid)
    ddir.mkdir(parents=True, exist_ok=True)
    moved = 0
    for mid in message_ids:
        src = pdir / f"{mid}.json"
        dst = ddir / f"{mid}.json"
        try:
            os.replace(src, dst)
            moved += 1
        except FileNotFoundError:
            continue
    return moved


def prune_delivered(to_uuid: str, ttl_days: int = DEFAULT_DELIVERED_TTL_DAYS) -> int:
    """Delete delivered/ files older than ttl_days. Returns count removed."""
    ddir = paths.inbox_delivered_dir(to_uuid)
    if not ddir.exists():
        return 0
    cutoff = time.time() - ttl_days * 86400
    removed = 0
    for p in ddir.iterdir():
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except (OSError, FileNotFoundError):
            continue
    return removed
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_runtime_inbox.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add src/brain/runtime/inbox.py tests/test_runtime_inbox.py
git commit -m "feat(runtime): inbox storage primitives (send, list, mark_delivered, prune)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Recipient resolution

**Files:**
- Create: `src/brain/runtime/resolve.py`
- Test: `tests/test_runtime_resolve.py`

- [ ] **Step 1: Write failing tests**

`tests/test_runtime_resolve.py`:

```python
"""Recipient resolution: UUID, name in-project, name cross-project, errors."""
from __future__ import annotations

import pytest

from brain.runtime import names, resolve


@pytest.fixture(autouse=True)
def _runtime_root(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_RUNTIME_DIR", str(tmp_path))


def test_uuid_resolves_to_itself_fire_and_forget():
    out = resolve.resolve_recipient(
        to="ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2",
        sender_project="acme",
        live_uuids={"ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2"},
    )
    assert out.ok and out.uuid == "ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2"
    assert out.name_at_send == ""


def test_uuid_dead_still_resolves_for_uuid_send():
    out = resolve.resolve_recipient(
        to="ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2",
        sender_project="acme",
        live_uuids=set(),  # nothing alive
    )
    assert out.ok and out.uuid == "ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2"


def test_cursor_uuid_rejected_in_mvp():
    out = resolve.resolve_recipient(
        to="cursor:ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2",
        sender_project="acme",
        live_uuids=set(),
    )
    assert not out.ok and out.error == "cursor_recipient_unsupported"


def test_name_in_sender_project_resolves():
    names.register("u1", "planner", "acme", "/tmp/a", 1)
    out = resolve.resolve_recipient(
        to="planner",
        sender_project="acme",
        live_uuids={"u1"},
    )
    assert out.ok and out.uuid == "u1"
    assert out.name_at_send == "planner"


def test_name_dead_session_fails_loud():
    names.register("u1", "planner", "acme", "/tmp/a", 1)
    out = resolve.resolve_recipient(
        to="planner",
        sender_project="acme",
        live_uuids=set(),
    )
    assert not out.ok and out.error == "recipient_dead"


def test_name_not_found():
    out = resolve.resolve_recipient(
        to="ghost",
        sender_project="acme",
        live_uuids=set(),
    )
    assert not out.ok and out.error == "name_not_found"


def test_cross_project_qualified():
    names.register("u1", "planner", "other", "/tmp/o", 1)
    out = resolve.resolve_recipient(
        to="other/planner",
        sender_project="acme",
        live_uuids={"u1"},
    )
    assert out.ok and out.uuid == "u1"


def test_invalid_recipient():
    out = resolve.resolve_recipient(
        to="!!not-valid!!",
        sender_project="acme",
        live_uuids=set(),
    )
    assert not out.ok and out.error == "invalid_recipient"


def test_lowercase_normalization_for_name():
    names.register("u1", "planner", "acme", "/tmp/a", 1)
    out = resolve.resolve_recipient(
        to="PLANNER",
        sender_project="acme",
        live_uuids={"u1"},
    )
    assert out.ok and out.uuid == "u1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_runtime_resolve.py -v`
Expected: ImportError

- [ ] **Step 3: Implement `resolve.py`**

`src/brain/runtime/resolve.py`:

```python
"""Recipient resolution from `to=` argument.

Modes:
  1. Bare UUIDv4               → fire-and-forget
  2. cursor:<UUIDv4>           → MVP: rejected (v2 wires Cursor)
  3. <name>                    → resolve in sender's project
  4. <project>/<name>          → resolve in named project
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Set

from brain.runtime import names

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_QUALIFIED_RE = re.compile(r"^([a-z0-9-]{1,128})/([a-z0-9-]{1,64})$")


@dataclass(frozen=True)
class Resolved:
    ok: bool
    uuid: str = ""
    name_at_send: str = ""
    error: str = ""
    detail: str = ""


def _ok(uuid: str, name: str = "") -> Resolved:
    return Resolved(ok=True, uuid=uuid, name_at_send=name)


def _err(code: str, detail: str = "") -> Resolved:
    return Resolved(ok=False, error=code, detail=detail)


def resolve_recipient(
    to: str,
    sender_project: str,
    live_uuids: Set[str],
) -> Resolved:
    raw = (to or "").strip()
    if not raw:
        return _err("invalid_recipient", "empty")

    if raw.startswith("cursor:"):
        bare = raw[len("cursor:"):]
        if _UUID_RE.match(bare):
            return _err("cursor_recipient_unsupported",
                        "Cursor recipients deferred to v2")
        return _err("invalid_recipient", "malformed cursor: prefix")

    if _UUID_RE.match(raw):
        return _ok(raw)

    lowered = raw.lower()
    qualified = _QUALIFIED_RE.match(lowered)
    if qualified:
        project, name = qualified.group(1), qualified.group(2)
        return _resolve_name(name, project, live_uuids)

    if _NAME_RE.match(lowered):
        project = names.normalize_project(sender_project)
        return _resolve_name(lowered, project, live_uuids)

    return _err("invalid_recipient", f"value {raw!r} is neither UUID nor a valid name")


def _resolve_name(
    name: str,
    project: str,
    live_uuids: Set[str],
) -> Resolved:
    uuid = names.lookup_by_name(name, project)
    if uuid is None:
        return _err("name_not_found", f"name {name!r} not found in project {project!r}")
    if uuid not in live_uuids:
        return _err(
            "recipient_dead",
            f"name {name!r} resolves to {uuid} but no live session has that UUID",
        )
    return _ok(uuid, name)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_runtime_resolve.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add src/brain/runtime/resolve.py tests/test_runtime_resolve.py
git commit -m "feat(runtime): recipient resolution (UUID/name/qualified/error codes)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Surface formatter

**Files:**
- Create: `src/brain/runtime/surface.py`
- Test: `tests/test_runtime_surface.py`

- [ ] **Step 1: Write failing tests**

`tests/test_runtime_surface.py`:

```python
"""SystemReminder formatting for surfaced messages."""
from __future__ import annotations

from brain.runtime import surface


def _msg(body: str, sender: str = "planner", at: str = "2026-04-25T17:05:11.342Z") -> dict:
    return {
        "id": "01JBXY7K9RZNG7M2XKSZP4Q3VC",
        "from_uuid": "u1",
        "from_name_at_send": sender,
        "to_uuid": "u2",
        "to_name_at_send": "executor",
        "body": body,
        "sent_at": at,
    }


def test_empty_returns_empty_string():
    assert surface.format_pending([]) == ""


def test_single_message_format():
    out = surface.format_pending([_msg("GO — read spec.md")])
    assert "<system-reminder>" in out
    assert "</system-reminder>" in out
    assert "1 new message" in out
    assert "planner" in out
    assert "GO — read spec.md" in out
    assert "17:05" in out  # HH:MM extracted


def test_body_truncated_at_default_limit():
    long_body = "x" * 1500
    out = surface.format_pending([_msg(long_body)])
    # 800-char default + ellipsis
    assert "x" * 800 in out
    assert "…" in out
    assert "x" * 801 not in out


def test_multi_message_count_in_header():
    msgs = [_msg(f"body {i}") for i in range(3)]
    out = surface.format_pending(msgs)
    assert "3 new messages" in out


def test_more_than_5_summarises_overflow():
    msgs = [_msg(f"body {i}") for i in range(7)]
    out = surface.format_pending(msgs)
    assert "2 older message" in out  # 7 - 5 surfaced = 2 in summary line
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_runtime_surface.py -v`
Expected: ImportError

- [ ] **Step 3: Implement `surface.py`**

`src/brain/runtime/surface.py`:

```python
"""Format pending messages into a `<system-reminder>` block.

Output target: ≤ 1 KB per surface; bodies truncated at 800 chars; up
to 5 messages listed; older messages summarised. The agent can call
`brain_inbox` for full bodies if needed.
"""
from __future__ import annotations

from typing import Sequence

DEFAULT_BODY_TRUNCATE = 800
DEFAULT_MAX_LISTED = 5


def _hhmm(sent_at: str) -> str:
    # sent_at format: "2026-04-25T17:05:11.342Z" or "+00:00"
    if "T" not in sent_at:
        return sent_at
    time_part = sent_at.split("T", 1)[1]
    return time_part[:5]


def _truncate(body: str, limit: int) -> str:
    if len(body) <= limit:
        return body
    return body[:limit] + "…"


def format_pending(
    messages: Sequence[dict],
    *,
    max_listed: int = DEFAULT_MAX_LISTED,
    body_truncate: int = DEFAULT_BODY_TRUNCATE,
) -> str:
    if not messages:
        return ""

    n = len(messages)
    header = f"📬 {n} new message{'s' if n != 1 else ''} (since last turn):"

    listed = list(messages[:max_listed])
    overflow = n - len(listed)

    lines: list[str] = ["<system-reminder>", header]
    for m in listed:
        sender = m.get("from_name_at_send") or m.get("from_uuid", "unknown")[:8]
        ts = _hhmm(m.get("sent_at", ""))
        body = _truncate((m.get("body") or "").strip(), body_truncate)
        lines.append(f"  - from `{sender}` at {ts}:")
        # indent body by 4 spaces per line for readability
        for body_line in body.splitlines() or [""]:
            lines.append(f"    {body_line}")

    if overflow > 0:
        lines.append(
            f"  … {overflow} older message{'s' if overflow != 1 else ''} "
            f"in inbox/pending — call `brain_inbox` to read"
        )

    lines.append("Run `brain_inbox` for full bodies. These are now marked delivered.")
    lines.append("</system-reminder>")
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_runtime_surface.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/brain/runtime/surface.py tests/test_runtime_surface.py
git commit -m "feat(runtime): surface formatter for SystemReminder injection

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Hook entry point

**Files:**
- Create: `src/brain/runtime/hook.py`
- Test: `tests/test_runtime_hook.py`

- [ ] **Step 1: Write failing tests**

`tests/test_runtime_hook.py`:

```python
"""Hook entry point — pulls pending, surfaces, marks delivered."""
from __future__ import annotations

import pytest

from brain.runtime import hook, inbox, paths


@pytest.fixture(autouse=True)
def _runtime_root(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_RUNTIME_DIR", str(tmp_path))


def test_no_uuid_silent_exit(monkeypatch, capsys):
    monkeypatch.setattr("brain.runtime.session_id.detect_own_uuid", lambda: None)
    rc = hook.run()
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""


def test_no_pending_silent_exit(monkeypatch, capsys):
    monkeypatch.setattr("brain.runtime.session_id.detect_own_uuid", lambda: "u1")
    rc = hook.run()
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""


def test_pending_messages_surfaced_and_marked(monkeypatch, capsys):
    monkeypatch.setattr("brain.runtime.session_id.detect_own_uuid", lambda: "u1")
    inbox.send("u1", "snd", "planner", "executor", "GO")
    rc = hook.run()
    assert rc == 0
    captured = capsys.readouterr()
    assert "<system-reminder>" in captured.out
    assert "GO" in captured.out
    # marked delivered = pending dir empty
    assert list(paths.inbox_pending_dir("u1").iterdir()) == []
    delivered = list(paths.inbox_delivered_dir("u1").iterdir())
    assert len(delivered) == 1


def test_exception_logged_not_raised(monkeypatch, capsys):
    monkeypatch.setattr("brain.runtime.session_id.detect_own_uuid",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    rc = hook.run()
    assert rc == 0  # never raises to caller
    log = paths.hook_log_path()
    assert log.exists()
    assert "boom" in log.read_text()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_runtime_hook.py -v`
Expected: ImportError

- [ ] **Step 3: Implement `hook.py`**

`src/brain/runtime/hook.py`:

```python
"""Entry point invoked by the UserPromptSubmit hook.

Reads pending messages for the calling session, formats a
SystemReminder block to stdout, and atomically moves the surfaced
messages from pending/ to delivered/.

Never raises to the caller — Claude Code treats hook nonzero exit as
an error and would interrupt the user. Errors go to the log file.
"""
from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone

from brain.runtime import inbox, paths, session_id, surface


def run() -> int:
    try:
        return _run()
    except Exception:  # noqa: BLE001 — broad on purpose; this is the safety net
        _log_exception()
        return 0


def _run() -> int:
    own = session_id.detect_own_uuid()
    if not own:
        return 0
    pending = inbox.list_pending(own)
    if not pending:
        return 0
    block = surface.format_pending(pending)
    if block:
        sys.stdout.write(block)
        sys.stdout.flush()
    inbox.mark_delivered(own, [m["id"] for m in pending])
    return 0


def _log_exception() -> None:
    try:
        log = paths.hook_log_path()
        log.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with log.open("a") as f:
            f.write(f"\n=== {ts} ===\n")
            traceback.print_exc(file=f)
    except Exception:  # noqa: BLE001
        # If even logging fails, swallow — never crash the hook
        pass


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_runtime_hook.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/brain/runtime/hook.py tests/test_runtime_hook.py
git commit -m "feat(runtime): UserPromptSubmit hook entry point

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Shell wrapper template + install_hooks extension

**Files:**
- Create: `bin/inbox-surface-hook.sh.template`
- Modify: `src/brain/install_hooks.py`
- Test: `tests/test_install_hooks.py` (add test cases)

- [ ] **Step 1: Create the shell wrapper template**

`bin/inbox-surface-hook.sh.template`:

```bash
#!/bin/bash
# Wired into Claude Code as UserPromptSubmit hook by `brain install`.
# Runs once per user prompt, BEFORE the assistant turn begins.
#
# Empty-inbox fast path — exit before starting Python when there is
# nothing to surface. Sub-millisecond when the receiver has no
# pending messages, which is the common case.
set -u

RUNTIME_DIR="${BRAIN_RUNTIME_DIR:-$HOME/.brain-runtime}"
SID="${CLAUDE_SESSION_ID:-}"

if [ -n "$SID" ]; then
    PENDING_DIR="$RUNTIME_DIR/inbox/$SID/pending"
    [ -d "$PENDING_DIR" ] || exit 0
    # `compgen -G` is a builtin glob check — no fork. If empty, exit.
    compgen -G "$PENDING_DIR/*.json" >/dev/null || exit 0
fi

mkdir -p "$RUNTIME_DIR/log"
exec "{{BRAIN_PYTHON}}" -m brain.runtime.hook 2>>"$RUNTIME_DIR/log/inbox-hook.log"
```

- [ ] **Step 2: Write failing tests for hook installer**

Add to `tests/test_install_hooks.py` (append, do not replace):

```python
# ─── UserPromptSubmit hook (inbox surface) ──────────────────────────


def test_install_user_prompt_submit_writes_entry(tmp_path):
    from brain import install_hooks
    home = tmp_path
    (home / ".claude").mkdir()
    block = {
        "hooks": {
            "UserPromptSubmit": [{
                "hooks": [{
                    "type": "command",
                    "command": "/abs/path/inbox-surface-hook.sh",
                }]
            }]
        }
    }
    res = install_hooks.install_claude_user_prompt_submit(home, block)
    import json
    written = json.loads((home / ".claude" / "settings.json").read_text())
    assert "UserPromptSubmit" in written["hooks"]
    assert any(
        "inbox-surface-hook" in h["command"]
        for grp in written["hooks"]["UserPromptSubmit"]
        for h in grp["hooks"]
    )
    assert res == str(home / ".claude" / "settings.json")


def test_install_user_prompt_submit_preserves_session_start(tmp_path):
    from brain import install_hooks
    import json
    home = tmp_path
    (home / ".claude").mkdir()
    settings = home / ".claude" / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "x brain.audit"}]}]
        }
    }))
    block = {
        "hooks": {
            "UserPromptSubmit": [{
                "hooks": [{"type": "command", "command": "/abs/inbox-surface-hook.sh"}]
            }]
        }
    }
    install_hooks.install_claude_user_prompt_submit(home, block)
    written = json.loads(settings.read_text())
    assert "SessionStart" in written["hooks"]
    assert "UserPromptSubmit" in written["hooks"]


def test_remove_user_prompt_submit_drops_only_brain_entry(tmp_path):
    from brain import install_hooks
    import json
    home = tmp_path
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(json.dumps({
        "hooks": {
            "UserPromptSubmit": [{
                "hooks": [
                    {"type": "command", "command": "/abs/inbox-surface-hook.sh"},
                    {"type": "command", "command": "/some/other/user-hook.sh"},
                ]
            }]
        }
    }))
    install_hooks.remove_claude_user_prompt_submit(home)
    written = json.loads((home / ".claude" / "settings.json").read_text())
    surviving = [
        h for grp in written["hooks"].get("UserPromptSubmit", [])
        for h in grp["hooks"]
    ]
    assert len(surviving) == 1
    assert "inbox-surface-hook" not in surviving[0]["command"]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_install_hooks.py -v -k user_prompt`
Expected: AttributeError on missing functions

- [ ] **Step 4: Extend `install_hooks.py`**

Add to `src/brain/install_hooks.py` (append; do not modify existing functions):

```python
# ─────────────────────────────────────────────────────────────────────────
# Claude Code (~/.claude/settings.json) — UserPromptSubmit (inbox surface)
# ─────────────────────────────────────────────────────────────────────────
INBOX_HOOK_MARKER = "inbox-surface-hook"


def install_claude_user_prompt_submit(
    home: Path, brain_block: dict[str, Any]
) -> str | None:
    """Merge `brain_block`'s UserPromptSubmit entry into Claude settings.

    Preserves any existing SessionStart wiring (and any sibling
    UserPromptSubmit groups the user has installed). Replaces the
    UserPromptSubmit array as a whole — symmetric with how
    `install_claude` handles SessionStart.
    """
    if not (home / ".claude").is_dir():
        return None
    target = _claude_settings_path(home)
    existing = _load_json(target)
    if existing is None:
        return None
    hooks = existing.setdefault("hooks", {})
    hooks["UserPromptSubmit"] = brain_block["hooks"]["UserPromptSubmit"]
    _save_json(target, existing)
    return str(target)


def _is_brain_inbox_hook(entry: dict[str, Any]) -> bool:
    cmd = entry.get("command") or ""
    return INBOX_HOOK_MARKER in cmd


def remove_claude_user_prompt_submit(home: Path) -> str | None:
    target = _claude_settings_path(home)
    existing = _load_json(target)
    if not existing:
        return None
    bag = existing.get("hooks") or {}
    starters = bag.get("UserPromptSubmit") or []
    cleaned: list[dict[str, Any]] = []
    for group in starters:
        inner = [h for h in (group.get("hooks") or []) if not _is_brain_inbox_hook(h)]
        if inner:
            cleaned.append({**group, "hooks": inner})
    if cleaned == starters:
        return None
    if cleaned:
        bag["UserPromptSubmit"] = cleaned
    else:
        bag.pop("UserPromptSubmit", None)
    if not bag:
        existing.pop("hooks", None)
    _save_json(target, existing)
    return str(target)
```

Modify `main()` to handle a 4th positional arg `<inbox_hook_src>` (optional, skipped if value is `--no-inbox-hook`). Find the `if action == "install":` block and replace its body with:

```python
    if action == "install":
        if len(argv) < 3:
            print("usage: install <settings_src> <hooks_src> [<inbox_hook_src>]", file=sys.stderr)
            return 2
        settings_block = json.loads(Path(argv[1]).read_text())
        hooks_block = json.loads(Path(argv[2]).read_text())
        inbox_block = None
        if len(argv) >= 4 and argv[3] != "--no-inbox-hook":
            inbox_block = json.loads(Path(argv[3]).read_text())
        for label, fn, payload in (
            ("Claude SessionStart hook installed",  install_claude, settings_block),
            ("Cursor sessionStart hook installed",  install_cursor, hooks_block),
        ):
            res = fn(home, payload)
            if res:
                print(f"      ✓ {label} ({res})")
            else:
                kind = "Claude" if "Claude" in label else "Cursor"
                dot = ".claude" if kind == "Claude" else ".cursor"
                if not (home / dot).is_dir():
                    print(f"      - ~/{dot} not found — {kind} hook skipped.")
                else:
                    print(f"      ! {kind} config malformed — {kind} hook skipped (fix manually).")
        if inbox_block is not None:
            res = install_claude_user_prompt_submit(home, inbox_block)
            if res:
                print(f"      ✓ Claude UserPromptSubmit (inbox surface) installed ({res})")
            elif (home / ".claude").is_dir():
                print("      ! Claude UserPromptSubmit install failed — inbox-hook skipped.")
        return 0
```

Modify the `if action == "remove":` block to also drop UserPromptSubmit:

```python
    if action == "remove":
        for label, fn in (
            ("Claude SessionStart hook removed", remove_claude),
            ("Claude UserPromptSubmit hook removed", remove_claude_user_prompt_submit),
            ("Cursor sessionStart hook removed", remove_cursor),
        ):
            res = fn(home)
            if res:
                print(f"      ✓ {label} ({res})")
        return 0
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_install_hooks.py -v`
Expected: existing tests + 3 new pass

- [ ] **Step 6: Commit**

```bash
git add bin/inbox-surface-hook.sh.template src/brain/install_hooks.py tests/test_install_hooks.py
git commit -m "feat(hooks): wire UserPromptSubmit hook for inbox surface

Idempotent install/remove that preserves SessionStart and any
non-brain UserPromptSubmit hooks the user already has.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: MCP tools — `brain_send`, `brain_inbox`, `brain_set_name`

**Files:**
- Modify: `src/brain/mcp_server.py` (add three tool implementations)
- Modify: `src/brain/mcp_server_write.py` (add tools to `WRITE_TOOLS`)
- Modify: `src/brain/mcp_server_read.py` (add tools to `READ_TOOLS`)
- Modify: `tests/test_mcp_server_split.py` (cover new tools)
- Create: `tests/test_runtime_mcp_tools.py`

- [ ] **Step 1: Write failing tests**

`tests/test_runtime_mcp_tools.py`:

```python
"""End-to-end check of the three new MCP tool implementations."""
from __future__ import annotations

import json

import pytest

from brain import mcp_server
from brain.runtime import inbox, names, paths


@pytest.fixture(autouse=True)
def _runtime_root(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_RUNTIME_DIR", str(tmp_path))


def test_brain_set_name_writes_registry(monkeypatch):
    monkeypatch.setattr("brain.runtime.session_id.detect_own_uuid", lambda: "u1")
    monkeypatch.setattr(mcp_server, "_caller_project_for_uuid", lambda u: "acme")
    monkeypatch.setattr(mcp_server, "_caller_cwd", lambda: "/tmp/acme")
    out = json.loads(mcp_server.brain_set_name("planner"))
    assert out["ok"] and out["name"] == "planner"
    assert names.get("u1")["name"] == "planner"


def test_brain_set_name_validation_error(monkeypatch):
    monkeypatch.setattr("brain.runtime.session_id.detect_own_uuid", lambda: "u1")
    monkeypatch.setattr(mcp_server, "_caller_project_for_uuid", lambda u: "acme")
    monkeypatch.setattr(mcp_server, "_caller_cwd", lambda: "/tmp/acme")
    out = json.loads(mcp_server.brain_set_name("Planner"))
    assert not out["ok"] and out["error"] == "lowercase"


def test_brain_send_to_uuid_fire_and_forget(monkeypatch):
    monkeypatch.setattr("brain.runtime.session_id.detect_own_uuid", lambda: "u1")
    monkeypatch.setattr(mcp_server, "_caller_project_for_uuid", lambda u: "acme")
    monkeypatch.setattr(mcp_server, "_caller_cwd", lambda: "/tmp/acme")
    monkeypatch.setattr(mcp_server, "_live_uuids", lambda: set())  # nothing alive
    target = "ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2"
    out = json.loads(mcp_server.brain_send(to=target, body="GO"))
    assert out["ok"] and out["to_uuid"] == target
    pending = inbox.list_pending(target)
    assert len(pending) == 1 and pending[0]["body"] == "GO"


def test_brain_send_to_name_dead(monkeypatch):
    monkeypatch.setattr("brain.runtime.session_id.detect_own_uuid", lambda: "u1")
    monkeypatch.setattr(mcp_server, "_caller_project_for_uuid", lambda u: "acme")
    monkeypatch.setattr(mcp_server, "_caller_cwd", lambda: "/tmp/acme")
    monkeypatch.setattr(mcp_server, "_live_uuids", lambda: {"u1"})
    names.register("ghost", "executor", "acme", "/tmp/g", 99)
    out = json.loads(mcp_server.brain_send(to="executor", body="GO"))
    assert not out["ok"] and out["error"] == "recipient_dead"


def test_brain_inbox_returns_pending(monkeypatch):
    monkeypatch.setattr("brain.runtime.session_id.detect_own_uuid", lambda: "u1")
    inbox.send("u1", "snd", "planner", "executor", "hello")
    out = json.loads(mcp_server.brain_inbox())
    assert out["pending_count"] == 1
    assert out["messages"][0]["body"] == "hello"


def test_brain_inbox_mark_read_moves_files(monkeypatch):
    monkeypatch.setattr("brain.runtime.session_id.detect_own_uuid", lambda: "u1")
    inbox.send("u1", "snd", "planner", "executor", "hello")
    out = json.loads(mcp_server.brain_inbox(mark_read=True))
    assert out["pending_count"] == 1  # snapshot count
    assert inbox.list_pending("u1") == []
    assert len(inbox.list_delivered("u1")) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_runtime_mcp_tools.py -v`
Expected: AttributeError on missing tools

- [ ] **Step 3: Add tool implementations to `mcp_server.py`**

Find a clean spot near the bottom of `src/brain/mcp_server.py` (after the existing tool defs, before `def main()`). Add:

```python
# ─────────────────────────────────────────────────────────────────────────
# Realtime named-session messaging — see docs/realtime-named-sessions-design.md
# ─────────────────────────────────────────────────────────────────────────
import json as _json_runtime  # local alias so this block stays self-contained


def _caller_cwd() -> str:
    """The cwd this MCP server was launched in. Overridden in tests."""
    import os as _os
    return _os.getcwd()


def _caller_project_for_uuid(uuid: str) -> str:
    """Map a session UUID to its project label.

    Reuses brain.live_sessions' project derivation so the answer matches
    what other brain tools see. Falls back to the basename of cwd.
    """
    from brain import live_sessions as _ls
    for row in _ls.list_live_sessions(include_self=True):
        if row.get("session_id") == uuid:
            return row.get("project") or _os.path.basename(_caller_cwd())
    import os as _os
    return _os.path.basename(_caller_cwd())


def _live_uuids() -> set[str]:
    from brain import live_sessions as _ls
    return {row["session_id"] for row in _ls.list_live_sessions(include_self=True)}


def _ensure_self_registered(uuid: str) -> None:
    """Lazy-create a default-name registry entry for `uuid` on first use."""
    from brain.runtime import names as _names
    from brain.runtime import session_id as _sid
    if _names.get(uuid):
        return
    project = _caller_project_for_uuid(uuid)
    short = _sid.short_id_for_default_name(uuid, source="claude")
    _names.register(
        uuid=uuid,
        name=_names.default_name(project, short),
        project=project,
        cwd=_caller_cwd(),
        pid=int(short) if short.isdigit() else None,
    )


def brain_set_name(name: str) -> str:
    """Set this session's human-readable name (per-project).

    Validation: lowercase, [a-z0-9-], 1-64 chars, not in {peer,self,all,me},
    not already taken by another session in the same project.

    Returns JSON: {ok, uuid, name, project} on success;
    {ok: false, error, detail} on failure (codes: lowercase, length, chars,
    reserved, name_taken, no_session).
    """
    from brain.runtime import names as _names
    from brain.runtime import session_id as _sid
    uuid = _sid.detect_own_uuid()
    if not uuid:
        return _json_runtime.dumps({"ok": False, "error": "no_session",
                                    "detail": "could not detect own session UUID"})
    _ensure_self_registered(uuid)
    err = _names.set_name(uuid, name)
    if err:
        return _json_runtime.dumps({"ok": False, "error": err})
    entry = _names.get(uuid) or {}
    return _json_runtime.dumps({
        "ok": True,
        "uuid": uuid,
        "name": entry.get("name"),
        "project": entry.get("project"),
    })


def brain_send(to: str, body: str) -> str:
    """Send a message to another live session by name or UUID.

    See docs/realtime-named-sessions-design.md §4.2 for resolution
    rules and error codes.
    """
    from brain.runtime import inbox as _inbox
    from brain.runtime import names as _names
    from brain.runtime import resolve as _resolve
    from brain.runtime import session_id as _sid

    sender_uuid = _sid.detect_own_uuid()
    if not sender_uuid:
        return _json_runtime.dumps({"ok": False, "error": "no_session"})
    _ensure_self_registered(sender_uuid)

    sender_project = _caller_project_for_uuid(sender_uuid)
    decision = _resolve.resolve_recipient(
        to=to,
        sender_project=sender_project,
        live_uuids=_live_uuids(),
    )
    if not decision.ok:
        return _json_runtime.dumps({
            "ok": False, "error": decision.error, "detail": decision.detail,
        })

    sender_entry = _names.get(sender_uuid) or {}
    try:
        env = _inbox.send(
            to_uuid=decision.uuid,
            from_uuid=sender_uuid,
            from_name_at_send=sender_entry.get("name") or sender_uuid[:8],
            to_name_at_send=decision.name_at_send,
            body=body,
        )
    except _inbox.BodyTooLarge as e:
        return _json_runtime.dumps({"ok": False, "error": "body_too_large",
                                    "detail": str(e)})

    return _json_runtime.dumps({
        "ok": True,
        "message_id": env["id"],
        "to_uuid": env["to_uuid"],
        "to_name_at_send": env["to_name_at_send"],
    })


def brain_inbox(unread_only: bool = True, limit: int = 50,
                mark_read: bool = False) -> str:
    """List own session's inbox.

    Default = peek (non-destructive). Pass mark_read=True to move
    listed messages from pending/ to delivered/. Note: the
    UserPromptSubmit hook is the normal mark-read path; manual calls
    default to peek so user can inspect without consuming.
    """
    from brain.runtime import inbox as _inbox
    from brain.runtime import session_id as _sid
    own = _sid.detect_own_uuid()
    if not own:
        return _json_runtime.dumps({"ok": False, "error": "no_session"})

    pending = _inbox.list_pending(own)
    delivered = _inbox.list_delivered(own)
    listed = pending if unread_only else pending + delivered
    listed = listed[: max(1, min(int(limit), 500))]

    if mark_read and pending:
        _inbox.mark_delivered(own, [m["id"] for m in pending])

    return _json_runtime.dumps({
        "ok": True,
        "messages": listed,
        "pending_count": len(pending),
        "delivered_count": len(delivered),
    })
```

- [ ] **Step 4: Register the tools in the split servers**

Modify `src/brain/mcp_server_write.py` — append two entries to `WRITE_TOOLS`:

```python
WRITE_TOOLS: tuple[str, ...] = (
    "brain_remember",
    "brain_note_add",
    "brain_retract_fact",
    "brain_correct_fact",
    "brain_forget",
    "brain_mark_reviewed",
    "brain_mark_contested",
    "brain_resolve_contested",
    "brain_failure_record",
    "brain_send",
    "brain_set_name",
)
```

Modify `src/brain/mcp_server_read.py` — append `"brain_inbox"` to `READ_TOOLS` (locate the existing tuple and add it). If you don't have the file open, run:

```bash
grep -n "READ_TOOLS" src/brain/mcp_server_read.py
```

then insert `"brain_inbox",` as the last entry of the tuple.

- [ ] **Step 5: Update split-partition test**

Modify `tests/test_mcp_server_split.py` — find the test that asserts the union of `READ_TOOLS + WRITE_TOOLS` covers all `brain_*` callables on `mcp_server`, and confirm it still passes (the new tools are now in WRITE/READ tuples). If the test enumerates expected tools, add the three new entries.

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_runtime_mcp_tools.py tests/test_mcp_server_split.py -v`
Expected: all green

- [ ] **Step 7: Register tools on aggregate FastMCP**

In `src/brain/mcp_server.py`, locate the section that calls `mcp.tool()(fn)` for each existing tool. Add the three new ones to whatever list/loop registers tools on the aggregate `mcp` instance (look for `brain_remember` registration as a reference). If they're registered individually, append:

```python
mcp.tool()(brain_send)
mcp.tool()(brain_inbox)
mcp.tool()(brain_set_name)
```

- [ ] **Step 8: Commit**

```bash
git add src/brain/mcp_server.py src/brain/mcp_server_write.py src/brain/mcp_server_read.py tests/test_mcp_server_split.py tests/test_runtime_mcp_tools.py
git commit -m "feat(mcp): add brain_send, brain_inbox, brain_set_name tools

Three MCP tools backed by the brain.runtime subsystem. Split-server
partition updated; aggregate server registers all three.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: Doctor inbox check

**Files:**
- Modify: existing doctor module (run `grep -rn "def doctor\|brain doctor" src/brain/` to locate; likely `src/brain/cli.py` or `src/brain/status.py`)
- Test: existing doctor test file (mirror pattern)

- [ ] **Step 1: Locate the doctor module**

Run:

```bash
grep -rln "doctor" src/brain/ | head -5
grep -rln "def doctor\b" src/brain/
```

Identify which module owns the doctor command. The remaining steps assume `src/brain/status.py`; adjust paths if it lives elsewhere.

- [ ] **Step 2: Write failing test**

Add to `tests/test_status.py` (or the corresponding test file):

```python
def test_doctor_reports_inbox_section(tmp_path, monkeypatch, capsys):
    from brain import status
    monkeypatch.setenv("BRAIN_RUNTIME_DIR", str(tmp_path))
    out = status.inbox_health()
    assert "inbox" in out["section"].lower()
    assert out["runtime_dir"] == str(tmp_path)
    assert out["runtime_dir_writable"] is True
    assert out["pending_total"] == 0
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_status.py -v -k inbox`
Expected: AttributeError

- [ ] **Step 4: Add the inbox health function**

Append to `src/brain/status.py`:

```python
def inbox_health() -> dict:
    """Doctor check for the runtime inbox subsystem."""
    import os
    from pathlib import Path
    from brain.runtime import paths as _rt_paths

    rt = _rt_paths.runtime_root()
    rt.mkdir(parents=True, exist_ok=True)
    writable = os.access(rt, os.W_OK)

    inbox_dir = rt / "inbox"
    pending_total = 0
    if inbox_dir.exists():
        for sid_dir in inbox_dir.iterdir():
            pending_dir = sid_dir / "pending"
            if pending_dir.is_dir():
                pending_total += sum(
                    1 for p in pending_dir.iterdir() if p.suffix == ".json"
                )

    settings = Path.home() / ".claude" / "settings.json"
    hook_wired = False
    if settings.exists():
        try:
            import json
            data = json.loads(settings.read_text())
            for grp in (data.get("hooks") or {}).get("UserPromptSubmit") or []:
                for h in grp.get("hooks") or []:
                    if "inbox-surface-hook" in (h.get("command") or ""):
                        hook_wired = True
                        break
        except Exception:
            pass

    return {
        "section": "Inbox (runtime transport)",
        "runtime_dir": str(rt),
        "runtime_dir_writable": writable,
        "pending_total": pending_total,
        "user_prompt_submit_hook_wired": hook_wired,
    }
```

Then locate the existing `doctor()` (or equivalent top-level CLI) and call `inbox_health()` alongside the other section reporters.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_status.py -v -k inbox`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/brain/status.py tests/test_status.py
git commit -m "feat(doctor): report inbox runtime health

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: Isolation enforcement test

**Files:**
- Create: `tests/test_runtime_isolation.py`

- [ ] **Step 1: Write the test**

`tests/test_runtime_isolation.py`:

```python
"""Architectural test: brain.runtime must not depend on the vault layer.

If brain.runtime.* imports from brain.entities, brain.graph, or
brain.semantic, transport and curated knowledge are coupled — which
is exactly the design we ruled out in the spec.
"""
from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path

FORBIDDEN_PREFIXES = (
    "brain.entities",
    "brain.graph",
    "brain.semantic",
    "brain.consolidation",
    "brain.dedupe",
    "brain.dedupe_judge",
    "brain.dedupe_ledger",
    "brain.note_extract",
    "brain.auto_extract",
    "brain.apply_extraction",
    "brain.reconcile",
    "brain.ontology_guard",
    "brain.predicate_registry",
    "brain.subject_reject",
    "brain.triple_audit",
    "brain.triple_rules",
)


def _runtime_modules():
    runtime = importlib.import_module("brain.runtime")
    runtime_path = Path(runtime.__file__).parent
    for info in pkgutil.iter_modules([str(runtime_path)]):
        if info.ispkg:
            continue
        yield f"brain.runtime.{info.name}", runtime_path / f"{info.name}.py"


def _imports(file_path: Path) -> list[str]:
    tree = ast.parse(file_path.read_text())
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.append(node.module)
    return out


def test_runtime_modules_dont_import_vault():
    violations: list[tuple[str, str]] = []
    for mod_name, file_path in _runtime_modules():
        for imp in _imports(file_path):
            for forbidden in FORBIDDEN_PREFIXES:
                if imp == forbidden or imp.startswith(forbidden + "."):
                    violations.append((mod_name, imp))
    assert not violations, (
        "brain.runtime modules must not import from vault layer:\n"
        + "\n".join(f"  {mod} imports {imp}" for mod, imp in violations)
    )
```

- [ ] **Step 2: Run test to verify it passes (should pass already from Tasks 1-7)**

Run: `pytest tests/test_runtime_isolation.py -v`
Expected: PASS (if any task introduced a forbidden import, fix it before continuing)

- [ ] **Step 3: Commit**

```bash
git add tests/test_runtime_isolation.py
git commit -m "test(runtime): enforce isolation from vault layer

brain.runtime.* must not import from entities/graph/semantic/extract/
ontology — transport and curated knowledge stay separate.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 12: Performance benchmark

**Files:**
- Create: `tests/test_runtime_perf.py`

- [ ] **Step 1: Write the test**

`tests/test_runtime_perf.py`:

```python
"""Hook latency budget — see spec §4.4."""
from __future__ import annotations

import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Empty-inbox fast path through the Python module — no shell wrapper.
# Shell-wrapper benchmarking is a manual smoke test (different process tree).


def _run_hook_python_only(env: dict) -> float:
    start = time.perf_counter()
    subprocess.run(
        [sys.executable, "-m", "brain.runtime.hook"],
        env=env, check=True, capture_output=True,
    )
    return (time.perf_counter() - start) * 1000  # ms


@pytest.mark.skipif(
    sys.platform == "win32", reason="latency budget tuned for unix; skip on win"
)
def test_empty_path_python_module_under_p99_budget(tmp_path, monkeypatch):
    import os
    env = os.environ.copy()
    env["BRAIN_RUNTIME_DIR"] = str(tmp_path)
    env.pop("CLAUDE_SESSION_ID", None)

    runs = [_run_hook_python_only(env) for _ in range(20)]
    median = statistics.median(runs)
    p99 = sorted(runs)[-1]  # 1/20 ≈ p95-p100, take max as crude p99 proxy

    # Python cold-start dominates. Generous budget so this passes on slow
    # CI; the shell wrapper makes the real budget when the inbox is empty.
    assert median <= 500, f"median={median:.1f}ms exceeds 500ms budget"
    assert p99 <= 1000, f"p99={p99:.1f}ms exceeds 1000ms budget"
```

- [ ] **Step 2: Run test to verify it passes**

Run: `pytest tests/test_runtime_perf.py -v`
Expected: PASS (loose budget; shell wrapper provides tighter real-world bound)

- [ ] **Step 3: Commit**

```bash
git add tests/test_runtime_perf.py
git commit -m "test(runtime): hook empty-path latency budget

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 13: Integration smoke test (skipped from default)

**Files:**
- Create: `tests/test_runtime_integration.py`
- Modify: `pyproject.toml` (register the marker)

- [ ] **Step 1: Register the pytest marker**

Find the `[tool.pytest.ini_options]` section in `pyproject.toml` (create it if absent) and ensure it contains:

```toml
[tool.pytest.ini_options]
markers = [
    "integration: long-running end-to-end tests; not run by default",
]
addopts = "-m 'not integration'"
```

(Merge with existing markers / addopts if any.)

- [ ] **Step 2: Write the integration test**

`tests/test_runtime_integration.py`:

```python
"""End-to-end same-process integration smoke test.

This stops short of spawning two real Claude Code sessions (which
needs a Claude binary on PATH and is environment-dependent). It does
exercise: name registry → resolve → send → list pending → mark
delivered, with a stubbed live_uuids set.
"""
from __future__ import annotations

import json

import pytest

from brain import mcp_server
from brain.runtime import names, paths


pytestmark = pytest.mark.integration


@pytest.fixture
def two_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_RUNTIME_DIR", str(tmp_path))

    # Two sessions, same project. We swap detect_own_uuid per call to
    # simulate cross-session traffic in one process.
    def make_caller(uuid: str, project: str = "acme", cwd: str = "/tmp/acme"):
        return {
            "uuid": uuid,
            "project": project,
            "cwd": cwd,
        }

    return [
        make_caller("11111111-2222-3333-4444-555555555555"),
        make_caller("66666666-7777-8888-9999-000000000000"),
    ]


def test_two_sessions_round_trip(two_sessions, monkeypatch):
    a, b = two_sessions

    def install_caller(caller):
        monkeypatch.setattr(
            "brain.runtime.session_id.detect_own_uuid", lambda: caller["uuid"]
        )
        monkeypatch.setattr(mcp_server, "_caller_project_for_uuid",
                            lambda u: caller["project"])
        monkeypatch.setattr(mcp_server, "_caller_cwd", lambda: caller["cwd"])
        monkeypatch.setattr(mcp_server, "_live_uuids", lambda: {a["uuid"], b["uuid"]})

    install_caller(a)
    out_a = json.loads(mcp_server.brain_set_name("planner"))
    assert out_a["ok"]

    install_caller(b)
    out_b = json.loads(mcp_server.brain_set_name("executor"))
    assert out_b["ok"]

    install_caller(a)
    out_send = json.loads(mcp_server.brain_send(to="executor", body="GO"))
    assert out_send["ok"] and out_send["to_uuid"] == b["uuid"]

    install_caller(b)
    out_inbox = json.loads(mcp_server.brain_inbox())
    assert out_inbox["pending_count"] == 1
    assert out_inbox["messages"][0]["body"] == "GO"
    assert out_inbox["messages"][0]["from_name_at_send"] == "planner"
```

- [ ] **Step 3: Run as integration suite**

Run: `pytest tests/test_runtime_integration.py -v -m integration`
Expected: PASS

Confirm default run skips it: `pytest tests/test_runtime_integration.py -v`
Expected: 1 deselected (or "no tests ran")

- [ ] **Step 4: Commit**

```bash
git add tests/test_runtime_integration.py pyproject.toml
git commit -m "test(runtime): integration round-trip (two-session simulation)

Marked @pytest.mark.integration; deselected from the default run via
addopts so unit suites stay fast.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 14: TTL prune CLI

**Files:**
- Create: `src/brain/runtime/gc.py`
- Test: `tests/test_runtime_gc.py`

- [ ] **Step 1: Write failing tests**

`tests/test_runtime_gc.py`:

```python
"""Runtime GC: delivered TTL, pending TTL for dead UUIDs, name TTL."""
from __future__ import annotations

import os
import time

import pytest

from brain.runtime import gc, inbox, names, paths


@pytest.fixture(autouse=True)
def _runtime_root(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_RUNTIME_DIR", str(tmp_path))


def _age_file(p, days):
    old = time.time() - days * 86400
    os.utime(p, (old, old))


def test_gc_prunes_old_delivered():
    msg = inbox.send("u1", "snd", "a", "b", "hi")
    inbox.mark_delivered("u1", [msg["id"]])
    delivered = paths.inbox_delivered_dir("u1") / f"{msg['id']}.json"
    _age_file(delivered, days=10)
    n = gc.run(live_uuids={"u1"})
    assert n["delivered_pruned"] == 1
    assert not delivered.exists()


def test_gc_keeps_recent_delivered():
    msg = inbox.send("u1", "snd", "a", "b", "hi")
    inbox.mark_delivered("u1", [msg["id"]])
    n = gc.run(live_uuids={"u1"})
    assert n["delivered_pruned"] == 0


def test_gc_prunes_pending_for_dead_uuid_after_ttl():
    msg = inbox.send("dead", "snd", "a", "b", "hi")
    pending_file = paths.inbox_pending_dir("dead") / f"{msg['id']}.json"
    _age_file(pending_file, days=40)
    n = gc.run(live_uuids=set())  # 'dead' not alive
    assert n["pending_pruned"] == 1
    assert not pending_file.exists()


def test_gc_keeps_pending_for_live_uuid_even_if_old():
    msg = inbox.send("alive", "snd", "a", "b", "hi")
    pending_file = paths.inbox_pending_dir("alive") / f"{msg['id']}.json"
    _age_file(pending_file, days=40)
    n = gc.run(live_uuids={"alive"})
    assert n["pending_pruned"] == 0
    assert pending_file.exists()


def test_gc_prunes_name_for_long_dead_uuid():
    names.register("ghost", "planner", "acme", "/tmp/g", 99)
    name_file = paths.name_file("ghost")
    _age_file(name_file, days=40)
    n = gc.run(live_uuids=set())
    assert n["names_pruned"] == 1
    assert not name_file.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_runtime_gc.py -v`
Expected: ImportError

- [ ] **Step 3: Implement `gc.py`**

`src/brain/runtime/gc.py`:

```python
"""TTL-based cleanup for the runtime transport directory.

Run periodically (launchd or cron) and lazily on each `brain_send`.
Hard-deletes only — there's no undo. Caller passes `live_uuids` so we
know which inboxes belong to abandoned sessions.
"""
from __future__ import annotations

import os
import time
from typing import Set

from brain.runtime import inbox, paths

DEFAULT_DELIVERED_TTL_DAYS = 7
DEFAULT_PENDING_TTL_DAYS = 30
DEFAULT_NAME_TTL_DAYS = 30


def run(
    live_uuids: Set[str],
    *,
    delivered_ttl_days: int = DEFAULT_DELIVERED_TTL_DAYS,
    pending_ttl_days: int = DEFAULT_PENDING_TTL_DAYS,
    name_ttl_days: int = DEFAULT_NAME_TTL_DAYS,
) -> dict:
    """Run all GC passes. Returns counts dict."""
    delivered_pruned = _prune_all_delivered(delivered_ttl_days)
    pending_pruned = _prune_dead_pending(live_uuids, pending_ttl_days)
    names_pruned = _prune_dead_names(live_uuids, name_ttl_days)
    return {
        "delivered_pruned": delivered_pruned,
        "pending_pruned": pending_pruned,
        "names_pruned": names_pruned,
    }


def _prune_all_delivered(ttl_days: int) -> int:
    inbox_root = paths.inbox_dir()
    if not inbox_root.exists():
        return 0
    total = 0
    for sid_dir in inbox_root.iterdir():
        if not sid_dir.is_dir():
            continue
        total += inbox.prune_delivered(sid_dir.name, ttl_days=ttl_days)
    return total


def _prune_dead_pending(live_uuids: Set[str], ttl_days: int) -> int:
    inbox_root = paths.inbox_dir()
    if not inbox_root.exists():
        return 0
    cutoff = time.time() - ttl_days * 86400
    total = 0
    for sid_dir in inbox_root.iterdir():
        if not sid_dir.is_dir():
            continue
        if sid_dir.name in live_uuids:
            continue  # don't prune pending for live recipients
        pdir = sid_dir / "pending"
        if not pdir.exists():
            continue
        for p in pdir.iterdir():
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    total += 1
            except (OSError, FileNotFoundError):
                continue
    return total


def _prune_dead_names(live_uuids: Set[str], ttl_days: int) -> int:
    ndir = paths.names_dir()
    if not ndir.exists():
        return 0
    cutoff = time.time() - ttl_days * 86400
    total = 0
    for p in ndir.iterdir():
        if not p.suffix == ".json":
            continue
        uuid = p.stem
        if uuid in live_uuids:
            continue
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                total += 1
        except (OSError, FileNotFoundError):
            continue
    return total


def main() -> int:
    """CLI entry: `python -m brain.runtime.gc` — discover live UUIDs from brain.live_sessions."""
    from brain import live_sessions as _ls
    live = {row["session_id"] for row in _ls.list_live_sessions(include_self=True)}
    counts = run(live)
    print(
        f"runtime-gc: delivered={counts['delivered_pruned']} "
        f"pending={counts['pending_pruned']} "
        f"names={counts['names_pruned']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_runtime_gc.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/brain/runtime/gc.py tests/test_runtime_gc.py
git commit -m "feat(runtime): TTL prune CLI for inbox + names

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 15: Wire `brain install` to use the inbox-hook template

**Files:**
- Modify: `bin/install.sh` (or whatever the existing install entry point is)
- Modify: `src/brain/init.py` if it exists and orchestrates install

- [ ] **Step 1: Locate the install entry point**

```bash
ls bin/
grep -rln "install_hooks" bin/ src/brain/
```

- [ ] **Step 2: Render the inbox-hook template at install time**

In the install script (likely `bin/install.sh`), find the section that renders the SessionStart hook block and writes it to a temp file. Add a parallel render for the inbox hook. Pseudocode (adapt to existing style):

```bash
# Render UserPromptSubmit hook block
INBOX_HOOK_TARGET="$BRAIN_DIR/bin/inbox-surface-hook.sh"
sed "s|{{BRAIN_PYTHON}}|$BRAIN_PYTHON|g" \
    "$BRAIN_REPO/bin/inbox-surface-hook.sh.template" \
    > "$INBOX_HOOK_TARGET"
chmod +x "$INBOX_HOOK_TARGET"

INBOX_BLOCK_JSON=$(mktemp)
cat > "$INBOX_BLOCK_JSON" <<EOF
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {"type": "command", "command": "$INBOX_HOOK_TARGET"}
        ]
      }
    ]
  }
}
EOF

# Pass through to install_hooks (positional arg 3, or --no-inbox-hook)
INBOX_ARG="$INBOX_BLOCK_JSON"
if [ "${BRAIN_NO_INBOX_HOOK:-0}" = "1" ]; then
    INBOX_ARG="--no-inbox-hook"
fi

"$BRAIN_PYTHON" -m brain.install_hooks install \
    "$SETTINGS_BLOCK_JSON" "$HOOKS_BLOCK_JSON" "$INBOX_ARG"
```

- [ ] **Step 3: Smoke-test manually**

Run a dry install in a sandbox:

```bash
BRAIN_RUNTIME_DIR=/tmp/test-brain-runtime \
HOME=/tmp/test-home \
bash bin/install.sh
cat /tmp/test-home/.claude/settings.json
```

Confirm `UserPromptSubmit` block is present and points to the rendered shell wrapper.

- [ ] **Step 4: Commit**

```bash
git add bin/install.sh
git commit -m "chore(install): render and wire inbox UserPromptSubmit hook

Honors BRAIN_NO_INBOX_HOOK=1 to skip the UserPromptSubmit wiring
for users who don't want per-prompt overhead.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

After writing each module + test pair, run the **full** runtime suite plus install_hooks plus mcp_server_split to catch cross-cutting regressions:

```bash
pytest tests/test_runtime_*.py tests/test_install_hooks.py tests/test_mcp_server_split.py -v
```

If any test fails, fix in place — do not move forward with broken state.

**Coverage check vs. spec §10 acceptance criteria:**

- ✅ runtime modules with unit tests — Tasks 1-7, 14
- ✅ MCP tools with error codes — Task 9
- ✅ `brain install` wires UserPromptSubmit + `--no-inbox-hook` — Tasks 8, 15
- ✅ `brain doctor` reports inbox status — Task 10
- ✅ End-to-end integration test — Task 13
- ✅ Performance benchmark — Task 12
- ✅ Knowledge persistence: relies on existing harvest pipeline; no test in this plan (manual smoke test in PR per spec §10)
- ✅ Isolation enforced — Task 11

**Placeholder scan:** every code block in this plan is concrete. The only deliberately-open item is the install-script edit in Task 15, which depends on the existing shell-script style — engineers adapt to it rather than blindly copying.

**Type consistency:**

- `Resolved` dataclass returned by `resolve.resolve_recipient` matches consumer in `brain_send` (Task 9).
- `inbox.send(...)` envelope keys (`id`, `from_uuid`, `from_name_at_send`, `to_uuid`, `to_name_at_send`, `body`, `sent_at`) match `surface.format_pending` reads and `brain_inbox` outputs.
- `names.register(...)` keys (`uuid`, `name`, `project`, `cwd`, `pid`, `set_at`) match `names.get` and `_ensure_self_registered` consumers.

---

## Execution Handoff

Plan complete and saved to `docs/realtime-named-sessions-plan.md`. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session using `superpowers:executing-plans`, batch execution with checkpoints.

User has granted 2-hour autonomy → defaulting to **subagent-driven**.
