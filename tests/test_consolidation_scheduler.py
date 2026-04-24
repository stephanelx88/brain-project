"""Tests for WS8 scheduler + rollback (round 3 follow-up).

Covers:
  * `consolidation.list_actions` filters (since / id / action / limit)
  * `consolidation.rollback` restores contributors, deletes the
    semantic derivative, writes dual audit rows (`consolidation.jsonl`
    action=rollback + WS5 hash-chained `consolidation_rollback`).
  * Rollback is idempotent — a second call reports `already_rolled_back`.
  * `brain consolidate --respect-guard` skips when clearance < min_level.
  * Template rendering populates `{{BRAIN_DIR}}`, `{{BRAIN_CMD}}`,
    `{{USERNAME}}` placeholders; no stray substitution markers left.
  * `install_scheduler(enable=False)` writes files without invoking
    systemctl/launchctl.
"""

from __future__ import annotations

import json
import platform
import time
from pathlib import Path

import pytest


@pytest.fixture
def tmp_vault(tmp_path, monkeypatch):
    vault = tmp_path / "brain"
    (vault / ".audit").mkdir(parents=True)
    (vault / "entities" / "people").mkdir(parents=True)

    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", vault)
    monkeypatch.setattr(config, "ENTITIES_DIR", vault / "entities")
    monkeypatch.setenv("BRAIN_DIR", str(vault))

    from brain import db
    monkeypatch.setattr(db, "DB_PATH", vault / ".brain.db")
    with db.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO entities "
            "(path, type, slug, name) VALUES (?,?,?,?)",
            ("entities/people/stephane.md", "people", "stephane", "stephane"),
        )
    return vault


def _insert_claim(conn, **kwargs):
    defaults = {
        "entity_id": 1,
        "subject_slug": "stephane",
        "predicate": "locatedIn",
        "predicate_key": "locatedin",
        "predicate_group": "location",
        "object_entity": None,
        "object_text": "Paris",
        "object_slug": None,
        "object_type": "string",
        "text": "currently in Paris",
        "fact_time": None,
        "observed_at": time.time() - 72 * 3600,
        "source_kind": "session",
        "source_path": "session-a",
        "source_sha": None,
        "scrub_tag": "ws4",
        "episode_id": "session-a",
        "confidence": 0.5,
        "risk_level": "trusted",
        "trust_source": "extracted",
        "salience": 1.0,
        "kind": "episodic",
        "status": "current",
        "claim_key": "k-default",
    }
    defaults.update(kwargs)
    cols = ",".join(defaults.keys())
    qs = ",".join(["?"] * len(defaults))
    conn.execute(
        f"INSERT INTO fact_claims ({cols}) VALUES ({qs})",
        tuple(defaults.values()),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _promote(tmp_vault):
    """Insert two agreeing episodes and run promotion. Return the
    new semantic row id."""
    from brain import consolidation, db
    with db.connect() as conn:
        _insert_claim(conn, episode_id="ep1", source_path="p1",
                      salience=1.0, claim_key="k1")
        _insert_claim(conn, episode_id="ep2", source_path="p2",
                      salience=1.0, claim_key="k2")
    summary = consolidation.promote_episodic_ready(apply=True)
    assert summary["promoted"] == 1
    return summary["promoted_ids"][0]


# ---------------------------------------------------------------------------
# list_actions
# ---------------------------------------------------------------------------


def test_list_actions_empty_when_no_audit_file(tmp_vault):
    from brain import consolidation
    assert consolidation.list_actions() == []


def test_list_actions_returns_promote_row_newest_first(tmp_vault):
    from brain import consolidation
    promoted_id = _promote(tmp_vault)
    rows = consolidation.list_actions()
    assert any(r.get("action") == "promote"
               and r.get("promoted_id") == promoted_id
               for r in rows)


def test_list_actions_filter_by_action(tmp_vault):
    from brain import consolidation
    promoted_id = _promote(tmp_vault)
    consolidation.rollback(promoted_id)
    promote_only = consolidation.list_actions(action="promote")
    rollback_only = consolidation.list_actions(action="rollback")
    assert all(r.get("action") == "promote" for r in promote_only)
    assert all(r.get("action") == "rollback" for r in rollback_only)


def test_list_actions_filter_by_id(tmp_vault):
    from brain import consolidation
    promoted_id = _promote(tmp_vault)
    hits = consolidation.list_actions(action_id=promoted_id)
    assert all(int(r.get("promoted_id") or 0) == promoted_id for r in hits)
    assert consolidation.list_actions(action_id=promoted_id + 999) == []


def test_list_actions_limit(tmp_vault):
    from brain import consolidation
    _promote(tmp_vault)   # writes one `promote` row
    rows = consolidation.list_actions(limit=1)
    assert len(rows) == 1


def test_list_actions_since_date(tmp_vault):
    from brain import consolidation
    _promote(tmp_vault)
    # Tomorrow — no matches.
    assert consolidation.list_actions(since="2099-01-01") == []
    # Epoch — everything matches.
    all_rows = consolidation.list_actions(since="1970-01-01")
    assert len(all_rows) >= 1


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------


def test_rollback_restores_contributors_and_deletes_semantic(tmp_vault):
    from brain import consolidation, db
    promoted_id = _promote(tmp_vault)

    result = consolidation.rollback(promoted_id, reason="pytest")
    assert result["restored"] == 2
    assert result["semantic_deleted"] is True
    assert result["already_rolled_back"] is False

    with db.connect() as conn:
        # Contributors are back to episodic/current.
        contribs = conn.execute(
            "SELECT kind, status, superseded_by FROM fact_claims "
            "WHERE superseded_by IS NOT NULL OR id IN (SELECT id FROM fact_claims WHERE kind='episodic')"
        ).fetchall()
        assert all(c[0] == "episodic" and c[1] == "current" and c[2] is None
                   for c in contribs if c[0] == "episodic")
        # Semantic row is gone.
        semantic = conn.execute(
            "SELECT COUNT(*) FROM fact_claims WHERE id=?",
            (promoted_id,),
        ).fetchone()
        assert semantic[0] == 0


def test_rollback_is_idempotent(tmp_vault):
    from brain import consolidation
    promoted_id = _promote(tmp_vault)
    consolidation.rollback(promoted_id)
    second = consolidation.rollback(promoted_id)
    assert second["already_rolled_back"] is True
    assert second["restored"] == 0


def test_rollback_writes_dual_audit_rows(tmp_vault):
    from brain import consolidation
    promoted_id = _promote(tmp_vault)
    consolidation.rollback(promoted_id, reason="pytest")

    # consolidation.jsonl — action=rollback row
    cons_audit = tmp_vault / ".audit" / "consolidation.jsonl"
    assert cons_audit.exists()
    rollback_rows = [
        json.loads(l) for l in cons_audit.read_text().splitlines()
        if l.strip() and json.loads(l).get("action") == "rollback"
    ]
    assert len(rollback_rows) == 1
    r = rollback_rows[0]
    assert r["promoted_id"] == promoted_id
    assert r["restored"] == 2
    # Counter-only: no raw object_text / text leak.
    assert "text" not in r
    assert "object_text" not in r

    # WS5 ledger — hash-chained consolidation_rollback row
    ledger = tmp_vault / ".audit" / "ledger.jsonl"
    assert ledger.exists()
    ledger_rows = [
        json.loads(l) for l in ledger.read_text().splitlines() if l.strip()
    ]
    rb = [r for r in ledger_rows if r.get("op") == "consolidation_rollback"]
    assert len(rb) == 1
    assert rb[0]["target"]["promoted_id"] == promoted_id
    assert "prev_hash" in rb[0]
    assert "hash" in rb[0]


def test_rollback_refuses_to_delete_non_consolidation_row(tmp_vault):
    """Safety check: rollback must not delete arbitrary fact_claims
    rows whose source_kind isn't 'consolidation'. That's what
    protects user-written rows from a typo'd --id."""
    from brain import consolidation, db
    with db.connect() as conn:
        claim_id = _insert_claim(conn, source_kind="session",
                                 claim_key="user-fact")

    result = consolidation.rollback(claim_id, reason="pytest-safety")
    assert result["semantic_deleted"] is False
    # Row still there.
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM fact_claims WHERE id=?", (claim_id,)
        ).fetchone()
    assert row is not None


# ---------------------------------------------------------------------------
# resource_guard gate (CLI surface)
# ---------------------------------------------------------------------------


def test_consolidate_cli_skips_when_clearance_too_low(tmp_vault, monkeypatch, capsys):
    from brain import cli, resource_guard

    monkeypatch.setattr(resource_guard, "clearance_level", lambda: 0)
    rc = cli.main([
        "consolidate", "--apply", "--respect-guard", "--min-level", "2",
        "--json",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["skipped"] is True
    assert parsed["reason"] == "resource_guard"
    assert parsed["clearance_level"] == 0
    assert parsed["min_level"] == 2


def test_consolidate_cli_runs_when_clearance_passes(tmp_vault, monkeypatch, capsys):
    from brain import cli, resource_guard

    monkeypatch.setattr(resource_guard, "clearance_level", lambda: 3)
    rc = cli.main([
        "consolidate", "--respect-guard", "--min-level", "2", "--json",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    # Real run shape — no skip markers.
    assert "checked_groups" in parsed
    assert "skipped" not in parsed


def test_consolidate_cli_unconditional_when_guard_off(tmp_vault, monkeypatch, capsys):
    """Manual invocation without --respect-guard must run even on a
    busy system (humans poking at the worker should not be silently
    no-op'd)."""
    from brain import cli, resource_guard

    monkeypatch.setattr(resource_guard, "clearance_level", lambda: 0)
    rc = cli.main(["consolidate", "--json"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert "checked_groups" in parsed
    assert "skipped" not in parsed


# ---------------------------------------------------------------------------
# Template rendering + install_scheduler (non-enable path)
# ---------------------------------------------------------------------------


def _templates_dir() -> Path:
    here = Path(__file__).resolve()
    return here.parent.parent / "templates"


def test_systemd_service_template_exists_and_has_placeholders():
    svc = _templates_dir() / "systemd" / "brain-consolidate.service.tmpl"
    assert svc.exists()
    body = svc.read_text()
    assert "{{BRAIN_DIR}}" in body
    assert "{{BRAIN_CMD}}" in body
    assert "--respect-guard" in body
    assert "--min-level" in body


def test_systemd_timer_template_exists_and_has_30min():
    tim = _templates_dir() / "systemd" / "brain-consolidate.timer.tmpl"
    assert tim.exists()
    body = tim.read_text()
    assert "OnUnitActiveSec=30min" in body
    assert "Unit=brain-consolidate.service" in body


def test_launchd_plist_template_exists_and_has_30min():
    plist = _templates_dir() / "launchd" / "brain-consolidate.plist.tmpl"
    assert plist.exists()
    body = plist.read_text()
    # 1800 s = 30 min.
    assert "<integer>1800</integer>" in body
    assert "{{BRAIN_DIR}}" in body
    assert "--respect-guard" in body


def test_install_scheduler_no_enable_writes_unit_files(tmp_vault, monkeypatch, tmp_path):
    """Linux path without enable — write the unit files to a temp
    systemd user dir and verify placeholders got rendered. Skip on
    platforms where install_scheduler is a no-op."""
    if platform.system() not in ("Linux", "Darwin"):
        pytest.skip("platform-specific installer; skipped")

    # Redirect systemd/launchd target dirs into the tmp path so the
    # test doesn't touch the real system.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    from brain import consolidation
    result = consolidation.install_scheduler(enable=False)
    assert "error" not in result

    if platform.system() == "Linux":
        assert result["platform"] == "linux"
        assert result["enabled"] is False
        svc = Path(result["service"])
        tim = Path(result["timer"])
        assert svc.exists() and tim.exists()
        body = svc.read_text()
        # Placeholders rendered, no stray markers.
        assert "{{" not in body
        assert "--respect-guard" in body
    else:
        assert result["platform"] == "darwin"
        assert result["enabled"] is False
        plist = Path(result["plist"])
        assert plist.exists()
        body = plist.read_text()
        assert "{{" not in body
