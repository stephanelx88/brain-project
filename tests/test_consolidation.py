"""Tests for WS8 episodic → semantic promotion worker (minimal scope).

Covers spec §A + §C gates: scrub_tag, trust, contested sibling, age
floor, aggregate salience, and the daily token budget counter.
"""

from __future__ import annotations

import json
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

    from brain import db
    monkeypatch.setattr(db, "DB_PATH", vault / ".brain.db")
    # Establish the schema.
    with db.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO entities "
            "(path, type, slug, name) VALUES (?,?,?,?)",
            ("entities/people/stephane.md", "people", "stephane", "stephane"),
        )
    return vault


def _insert_claim(conn, **kwargs):
    """Compact helper that fills the WS6 NOT-NULL columns with sane
    defaults unless the caller overrides them."""
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
        "observed_at": time.time() - 72 * 3600,   # 72h old (passes age floor)
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


# ---------------------------------------------------------------------------
# Budget counter
# ---------------------------------------------------------------------------


def test_remaining_budget_default(tmp_vault, monkeypatch):
    from brain import consolidation
    monkeypatch.delenv("BRAIN_CONSOLIDATE_DAILY_BUDGET_TOK", raising=False)
    assert consolidation.remaining_budget() == 25000


def test_budget_can_be_overridden(tmp_vault, monkeypatch):
    from brain import consolidation
    monkeypatch.setenv("BRAIN_CONSOLIDATE_DAILY_BUDGET_TOK", "1000")
    assert consolidation.remaining_budget() == 1000


def test_charge_budget_reduces_remaining(tmp_vault):
    from brain import consolidation
    before = consolidation.remaining_budget()
    consolidation.charge_budget(300, reason="unit-test")
    assert consolidation.remaining_budget() == before - 300


def test_charge_budget_ignores_non_positive(tmp_vault):
    from brain import consolidation
    before = consolidation.remaining_budget()
    consolidation.charge_budget(0, reason="zero")
    consolidation.charge_budget(-5, reason="negative")
    assert consolidation.remaining_budget() == before


def test_budget_exhausted_short_circuits_worker(tmp_vault, monkeypatch):
    monkeypatch.setenv("BRAIN_CONSOLIDATE_DAILY_BUDGET_TOK", "0")
    from brain import consolidation
    out = consolidation.promote_episodic_ready(apply=True)
    assert out.get("status") == "budget_exhausted"
    assert out["promoted"] == 0


# ---------------------------------------------------------------------------
# Trust weight + salience helpers
# ---------------------------------------------------------------------------


def test_trust_weight_table():
    from brain import consolidation
    assert consolidation._trust_weight("user", "trusted") == 1.0
    assert consolidation._trust_weight("correction", "trusted") == 1.0
    assert consolidation._trust_weight("note", "trusted") == 0.85
    assert consolidation._trust_weight("extracted", "trusted") == 0.70
    assert consolidation._trust_weight("user", "low") == 0.3
    assert consolidation._trust_weight("user", "quarantined") == 0.0


def test_decayed_salience_halves_at_one_tau(monkeypatch):
    monkeypatch.setenv("BRAIN_SALIENCE_TAU_DAYS", "14")
    from brain import consolidation
    now = 1_000_000_000.0
    # observed exactly one tau back
    s = consolidation._decayed_salience(1.0, now - 14 * 86400, now)
    assert 0.36 < s < 0.38      # exp(-1) ≈ 0.3679


def test_aggregate_salience_formula():
    from brain import consolidation
    # Two independent 0.5 signals → 1 - (1-0.5)^2 = 0.75.
    assert consolidation._aggregate_salience([0.5, 0.5]) == pytest.approx(0.75)
    # Three 0.3 signals → 1 - 0.7^3 = 0.657.
    assert consolidation._aggregate_salience([0.3, 0.3, 0.3]) == pytest.approx(0.657, abs=1e-3)


# ---------------------------------------------------------------------------
# Gate rejections
# ---------------------------------------------------------------------------


def test_scrub_tag_gate_rejects_pre_ws4(tmp_vault):
    from brain import consolidation, db
    with db.connect() as conn:
        _insert_claim(conn, episode_id="ep1", source_path="p1",
                      scrub_tag="pre-ws4", claim_key="k1")
        _insert_claim(conn, episode_id="ep2", source_path="p2",
                      scrub_tag="pre-ws4", claim_key="k2")
    out = consolidation.promote_episodic_ready(apply=True)
    assert out["promoted"] == 0
    assert out["blocked_scrub"] == 2


def test_scrub_tag_gate_rejects_null_tag(tmp_vault):
    from brain import consolidation, db
    with db.connect() as conn:
        _insert_claim(conn, episode_id="ep1", source_path="p1",
                      scrub_tag=None, claim_key="k1")
        _insert_claim(conn, episode_id="ep2", source_path="p2",
                      scrub_tag=None, claim_key="k2")
    out = consolidation.promote_episodic_ready(apply=True)
    assert out["promoted"] == 0
    assert out["blocked_scrub"] == 2


def test_trust_gate_rejects_low_risk(tmp_vault):
    from brain import consolidation, db
    with db.connect() as conn:
        _insert_claim(conn, episode_id="ep1", source_path="p1",
                      risk_level="low", claim_key="k1")
        _insert_claim(conn, episode_id="ep2", source_path="p2",
                      risk_level="trusted", claim_key="k2")
    out = consolidation.promote_episodic_ready(apply=True)
    assert out["blocked_trust"] == 1
    # Only one trusted survivor → N-agreement fails.
    assert out["promoted"] == 0
    assert out["blocked_disagreement"] == 1


def test_age_floor_blocks_same_day_burst(tmp_vault, monkeypatch):
    from brain import consolidation, db
    now = time.time()
    with db.connect() as conn:
        _insert_claim(conn, episode_id="ep1", source_path="p1",
                      observed_at=now - 10,                # 10s old
                      claim_key="k1")
        _insert_claim(conn, episode_id="ep2", source_path="p2",
                      observed_at=now - 20,
                      claim_key="k2")
    out = consolidation.promote_episodic_ready(apply=True)
    assert out["blocked_age"] == 1
    assert out["promoted"] == 0


def test_independence_requires_distinct_episode_and_path(tmp_vault):
    """Two rows from the SAME session/path don't count as independent
    evidence — attacker can't double-promote from one compromised
    source."""
    from brain import consolidation, db
    with db.connect() as conn:
        # Same episode_id + same source_path → fails independence.
        _insert_claim(conn, episode_id="same", source_path="same",
                      claim_key="k1")
        _insert_claim(conn, episode_id="same", source_path="same",
                      claim_key="k2")
    out = consolidation.promote_episodic_ready(apply=True)
    assert out["promoted"] == 0
    assert out["blocked_disagreement"] == 1


def test_contested_sibling_blocks_promotion(tmp_vault, monkeypatch):
    """A current claim with same (subject_slug, predicate_key) but
    different object blocks promotion."""
    monkeypatch.setenv("BRAIN_CONSOLIDATE_SALIENCE_MIN", "0.0")  # skip salience
    from brain import consolidation, db
    with db.connect() as conn:
        # Two independent agreeing episodes on "Paris".
        _insert_claim(conn, episode_id="ep1", source_path="p1",
                      object_text="Paris", claim_key="k1")
        _insert_claim(conn, episode_id="ep2", source_path="p2",
                      object_text="Paris", claim_key="k2")
        # A contested live claim saying Lyon.
        _insert_claim(conn, episode_id="ep3", source_path="p3",
                      object_text="Lyon", kind="semantic",
                      status="current", claim_key="k3")
    out = consolidation.promote_episodic_ready(apply=True)
    assert out["blocked_contested"] == 1
    assert out["promoted"] == 0


def test_salience_floor_blocks_low_aggregate(tmp_vault):
    """Default salience 0.3 × trust weight 0.7 = 0.21 per row. Two
    rows → aggregate 0.375 < 0.6 → blocked."""
    from brain import consolidation, db
    with db.connect() as conn:
        _insert_claim(conn, episode_id="ep1", source_path="p1",
                      salience=0.3, claim_key="k1")
        _insert_claim(conn, episode_id="ep2", source_path="p2",
                      salience=0.3, claim_key="k2")
    out = consolidation.promote_episodic_ready(apply=True)
    assert out["blocked_salience"] == 1
    assert out["promoted"] == 0


# ---------------------------------------------------------------------------
# Successful promotion
# ---------------------------------------------------------------------------


def test_promotes_two_independent_high_salience_episodes(tmp_vault):
    from brain import consolidation, db
    with db.connect() as conn:
        _insert_claim(conn, episode_id="ep1", source_path="p1",
                      salience=1.0, claim_key="k1")
        _insert_claim(conn, episode_id="ep2", source_path="p2",
                      salience=1.0, claim_key="k2")

    out = consolidation.promote_episodic_ready(apply=True)
    assert out["eligible"] == 1
    assert out["promoted"] == 1
    assert len(out["promoted_ids"]) == 1

    with db.connect() as conn:
        # One new semantic row exists.
        semantic = conn.execute(
            "SELECT id, kind, status, salience, source_kind "
            "FROM fact_claims WHERE kind='semantic' AND status='current'"
        ).fetchall()
        assert len(semantic) == 1
        assert semantic[0][1] == "semantic"
        assert semantic[0][2] == "current"
        assert semantic[0][3] >= 0.6
        assert semantic[0][4] == "consolidation"
        # Contributors are now superseded and point at the new row.
        contributors = conn.execute(
            "SELECT status, superseded_by FROM fact_claims "
            "WHERE kind='episodic' ORDER BY id"
        ).fetchall()
        assert all(row[0] == "superseded" for row in contributors)
        assert all(row[1] == semantic[0][0] for row in contributors)


def test_dry_run_makes_no_changes(tmp_vault):
    from brain import consolidation, db
    with db.connect() as conn:
        _insert_claim(conn, episode_id="ep1", source_path="p1",
                      salience=1.0, claim_key="k1")
        _insert_claim(conn, episode_id="ep2", source_path="p2",
                      salience=1.0, claim_key="k2")
        before = conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0]

    out = consolidation.promote_episodic_ready(apply=False)
    assert out["eligible"] == 1
    assert out["promoted"] == 0

    with db.connect() as conn:
        after = conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0]
        episodic_current = conn.execute(
            "SELECT COUNT(*) FROM fact_claims "
            "WHERE kind='episodic' AND status='current'"
        ).fetchone()[0]
    assert after == before
    assert episodic_current == 2


def test_promotion_writes_audit_jsonl(tmp_vault):
    from brain import consolidation, db
    with db.connect() as conn:
        _insert_claim(conn, episode_id="ep1", source_path="p1",
                      salience=1.0, claim_key="k1")
        _insert_claim(conn, episode_id="ep2", source_path="p2",
                      salience=1.0, claim_key="k2")
    consolidation.promote_episodic_ready(apply=True)
    audit = tmp_vault / ".audit" / "consolidation.jsonl"
    assert audit.exists()
    row = json.loads(audit.read_text().splitlines()[-1])
    assert row["action"] == "promote"
    assert row["subject_slug"] == "stephane"
    assert row["n_contributors"] == 2
    assert row["aggregate_salience"] >= 0.6
    # No fact text in audit — spec mandates counters only.
    assert "text" not in row
    assert "object_text" not in row


def test_max_promotions_caps_run(tmp_vault):
    from brain import consolidation, db
    # Two independent promotable groups.
    with db.connect() as conn:
        # Group A: Paris
        _insert_claim(conn, episode_id="a1", source_path="pa1",
                      object_text="Paris", salience=1.0, claim_key="ka1")
        _insert_claim(conn, episode_id="a2", source_path="pa2",
                      object_text="Paris", salience=1.0, claim_key="ka2")
        # Group B: different subject + different predicate, same pattern.
        conn.execute(
            "INSERT OR IGNORE INTO entities (path, type, slug, name) "
            "VALUES (?,?,?,?)",
            ("entities/people/other.md", "people", "other", "other"),
        )
        other_id = conn.execute(
            "SELECT id FROM entities WHERE slug='other'"
        ).fetchone()[0]
        _insert_claim(conn, entity_id=other_id, subject_slug="other",
                      predicate="worksAt", predicate_key="worksat",
                      predicate_group="employer",
                      object_text="Acme",
                      episode_id="b1", source_path="pb1", salience=1.0,
                      claim_key="kb1")
        _insert_claim(conn, entity_id=other_id, subject_slug="other",
                      predicate="worksAt", predicate_key="worksat",
                      predicate_group="employer",
                      object_text="Acme",
                      episode_id="b2", source_path="pb2", salience=1.0,
                      claim_key="kb2")

    out = consolidation.promote_episodic_ready(apply=True, max_promotions=1)
    assert out["promoted"] == 1


def test_min_trust_source_picks_weakest(tmp_vault):
    from brain import consolidation
    members = [
        {"trust_source": "user"},
        {"trust_source": "extracted"},
        {"trust_source": "note"},
    ]
    assert consolidation._min_trust_source(members) == "extracted"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_dry_run_prints_summary(tmp_vault, capsys):
    from brain import cli
    rc = cli.main(["consolidate"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "[DRY-RUN] consolidate" in captured.out
    assert "checked=" in captured.out
    assert "budget=" in captured.out


def test_cli_apply_runs_worker(tmp_vault, capsys):
    from brain import cli, db
    with db.connect() as conn:
        _insert_claim(conn, episode_id="ep1", source_path="p1",
                      salience=1.0, claim_key="k1")
        _insert_claim(conn, episode_id="ep2", source_path="p2",
                      salience=1.0, claim_key="k2")
    rc = cli.main(["consolidate", "--apply"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[APPLY] consolidate" in out
    assert "promoted=1" in out


def test_cli_json_mode(tmp_vault, capsys):
    from brain import cli
    rc = cli.main(["consolidate", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is False
    assert "checked_groups" in payload
    assert "budget_remaining" in payload
