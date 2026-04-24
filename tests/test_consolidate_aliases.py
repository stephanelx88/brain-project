"""Tests for WS8 Part B — alias canonicalisation via LLM pair-judge."""

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
    (vault / "entities" / "projects").mkdir(parents=True)
    (vault / "identity").mkdir()
    # Minimal owner file — needed so owner-carve-out can resolve.
    (vault / "identity" / "who-i-am.md").write_text(
        "---\ntype: identity\n---\n\n# Who\n\n- Name: stephane\n"
    )

    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", vault)
    monkeypatch.setattr(config, "ENTITIES_DIR", vault / "entities")
    monkeypatch.setattr(config, "IDENTITY_DIR", vault / "identity")

    from brain import db
    monkeypatch.setattr(db, "DB_PATH", vault / ".brain.db")

    # Drop any cached state from prior tests.
    from brain import subject_reject
    subject_reject.reset_caches()

    return vault


def _seed_entity(conn, *, slug, name, etype="people") -> int:
    conn.execute(
        "INSERT OR IGNORE INTO entities (path, type, slug, name) "
        "VALUES (?,?,?,?)",
        (f"entities/{etype}/{slug}.md", etype, slug, name),
    )
    row = conn.execute(
        "SELECT id FROM entities WHERE slug=?", (slug,)
    ).fetchone()
    return row[0]


def _seed_claim(conn, **kw):
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
        "text": "in Paris",
        "fact_time": None,
        "observed_at": time.time() - 3600,
        "source_kind": "session",
        "source_path": "session-a",
        "source_sha": None,
        "scrub_tag": "ws4",
        "episode_id": "session-a",
        "confidence": 0.5,
        "risk_level": "trusted",
        "trust_source": "extracted",
        "salience": 0.5,
        "kind": "episodic",
        "status": "current",
        "claim_key": "k-default",
    }
    defaults.update(kw)
    cols = ",".join(defaults.keys())
    qs = ",".join(["?"] * len(defaults))
    conn.execute(
        f"INSERT INTO fact_claims ({cols}) VALUES ({qs})",
        tuple(defaults.values()),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------


def test_levenshtein_basic():
    from brain import consolidation
    assert consolidation._levenshtein("abc", "abc") == 0
    assert consolidation._levenshtein("abc", "abd") == 1
    assert consolidation._levenshtein("abcd", "abdc") == 2
    # Early exit — returns cutoff+1 without computing the full matrix.
    assert consolidation._levenshtein("short", "a very long string",
                                      cutoff=2) == 3


def test_norm_phrase_collapses_whitespace_and_case():
    from brain import consolidation
    assert consolidation._norm_phrase("  Thu   Hà  ") == "thu hà"


def test_parse_alias_verdict_strict_json():
    from brain import consolidation
    good = '{"decision": "merge", "winner_slug": "thuha", "confidence": 0.95, "reasoning": "same person"}'
    out = consolidation._parse_alias_verdict(good)
    assert out and out["decision"] == "merge"


def test_parse_alias_verdict_tolerates_fence_and_prose():
    from brain import consolidation
    wrapped = (
        "Sure thing:\n```json\n"
        '{"decision": "keep_distinct", "winner_slug": null, '
        '"confidence": 0.5, "reasoning": "different people"}\n```'
    )
    out = consolidation._parse_alias_verdict(wrapped)
    assert out and out["decision"] == "keep_distinct"


def test_parse_alias_verdict_rejects_bad_decision():
    from brain import consolidation
    assert consolidation._parse_alias_verdict(
        '{"decision": "merge_if_confident"}'
    ) is None


def test_parse_alias_verdict_none_on_none():
    from brain import consolidation
    assert consolidation._parse_alias_verdict(None) is None


def test_tokens_per_pair_env_override(monkeypatch):
    from brain import consolidation
    monkeypatch.setenv("BRAIN_ALIAS_TOKENS_PER_PAIR", "400")
    assert consolidation._tokens_per_pair() == 400


# ---------------------------------------------------------------------------
# Candidate detection
# ---------------------------------------------------------------------------


def test_find_candidates_requires_min_mentions(tmp_vault):
    from brain import consolidation, db
    with db.connect() as conn:
        thuha_id = _seed_entity(conn, slug="thuha", name="Thuha")
        _seed_entity(conn, slug="stephane", name="stephane")
        # Single mention of "Thu Ha" — below ALIAS_MIN_MENTIONS.
        _seed_claim(conn, object_text="Thu Ha", claim_key="k1",
                    episode_id="a", source_path="p1")
        candidates = consolidation._find_alias_candidates(conn)
    assert candidates == []


def test_find_candidates_returns_levenshtein_match(tmp_vault):
    from brain import consolidation, db
    with db.connect() as conn:
        thuha_id = _seed_entity(conn, slug="thuha", name="Thuha")
        _seed_entity(conn, slug="stephane", name="stephane")
        # "Thu Ha" → Levenshtein(thuha, "thu ha") = 1.
        _seed_claim(conn, object_text="Thu Ha", claim_key="k1",
                    episode_id="a", source_path="p1")
        _seed_claim(conn, object_text="Thu Ha", claim_key="k2",
                    episode_id="b", source_path="p2")
        candidates = consolidation._find_alias_candidates(conn)
    assert len(candidates) == 1
    c = candidates[0]
    assert c["object_text"] == "Thu Ha"
    assert c["candidate_slug"] == "thuha"
    assert c["distance"] <= 2
    assert c["mentions"] == 2
    assert c["correction_sourced"] is False


def test_find_candidates_skips_resolved_rows(tmp_vault):
    """A row that already has object_slug must not appear as a candidate."""
    from brain import consolidation, db
    with db.connect() as conn:
        _seed_entity(conn, slug="thuha", name="Thuha")
        _seed_entity(conn, slug="stephane", name="stephane")
        # Both resolved.
        _seed_claim(conn, object_text="Thuha", object_slug="thuha",
                    claim_key="k1", episode_id="a", source_path="p1")
        _seed_claim(conn, object_text="Thuha", object_slug="thuha",
                    claim_key="k2", episode_id="b", source_path="p2")
        candidates = consolidation._find_alias_candidates(conn)
    assert candidates == []


def test_find_candidates_skips_owner_entity(tmp_vault):
    """Owner entity never gets auto-aliased."""
    from brain import consolidation, db
    with db.connect() as conn:
        _seed_entity(conn, slug="stephane", name="stephane")
        _seed_claim(conn, object_text="stephan", claim_key="k1",
                    episode_id="a", source_path="p1")
        _seed_claim(conn, object_text="stephan", claim_key="k2",
                    episode_id="b", source_path="p2")
        candidates = consolidation._find_alias_candidates(conn)
    assert candidates == []
    # But the disambiguations.jsonl should have a needs_user row.
    disambig = tmp_vault / "disambiguations.jsonl"
    assert disambig.exists()
    last = json.loads(disambig.read_text().splitlines()[-1])
    assert last["decision"] == "needs_user"
    assert last["slug"] == "stephane"


def test_find_candidates_flags_correction_sourced(tmp_vault):
    """When any contributor is trust_source='correction', the candidate
    is listed but marked so the worker skips it."""
    from brain import consolidation, db
    with db.connect() as conn:
        _seed_entity(conn, slug="thuha", name="Thuha")
        _seed_entity(conn, slug="stephane", name="stephane")
        _seed_claim(conn, object_text="Thu Ha", claim_key="k1",
                    episode_id="a", source_path="p1",
                    trust_source="correction")
        _seed_claim(conn, object_text="Thu Ha", claim_key="k2",
                    episode_id="b", source_path="p2")
        candidates = consolidation._find_alias_candidates(conn)
    assert len(candidates) == 1
    assert candidates[0]["correction_sourced"] is True


def test_find_candidates_skips_too_distant(tmp_vault):
    from brain import consolidation, db
    with db.connect() as conn:
        _seed_entity(conn, slug="thuha", name="Thuha")
        _seed_entity(conn, slug="stephane", name="stephane")
        _seed_claim(conn, object_text="CompletelyUnrelatedWord",
                    claim_key="k1", episode_id="a", source_path="p1")
        _seed_claim(conn, object_text="CompletelyUnrelatedWord",
                    claim_key="k2", episode_id="b", source_path="p2")
        candidates = consolidation._find_alias_candidates(conn)
    assert candidates == []


# ---------------------------------------------------------------------------
# Disambiguation override
# ---------------------------------------------------------------------------


def test_load_disambiguations_roundtrip(tmp_vault):
    from brain import consolidation
    consolidation._append_disambiguation(
        text="Thu Ha", slug="thuha", decision="not_same",
        reasoning="different people",
    )
    loaded = consolidation._load_disambiguations()
    assert loaded.get(("thu ha", "thuha")) == "not_same"


def test_consolidate_aliases_skips_existing_disambig(tmp_vault):
    from brain import consolidation, db

    with db.connect() as conn:
        _seed_entity(conn, slug="thuha", name="Thuha")
        _seed_entity(conn, slug="stephane", name="stephane")
        _seed_claim(conn, object_text="Thu Ha", claim_key="k1",
                    episode_id="a", source_path="p1")
        _seed_claim(conn, object_text="Thu Ha", claim_key="k2",
                    episode_id="b", source_path="p2")
    consolidation._append_disambiguation(
        text="Thu Ha", slug="thuha", decision="not_same",
        reasoning="user override",
    )

    # Judge must not be called — return a tripwire to prove it.
    calls: list[str] = []

    def tripwire(_prompt):
        calls.append(_prompt)
        return '{"decision":"merge","winner_slug":"thuha","confidence":1.0,"reasoning":"x"}'

    out = consolidation.consolidate_aliases(
        apply=True,
        judge_fn=tripwire,
        budget_tokens=10_000,
    )
    assert calls == []
    assert out["skipped_disambig"] == 1
    assert out["merged"] == 0


# ---------------------------------------------------------------------------
# End-to-end merge / keep_distinct / needs_user
# ---------------------------------------------------------------------------


def test_consolidate_aliases_merge_rewrites_object_slug(tmp_vault):
    from brain import consolidation, db
    with db.connect() as conn:
        thuha_id = _seed_entity(conn, slug="thuha", name="Thuha")
        _seed_entity(conn, slug="stephane", name="stephane")
        _seed_claim(conn, object_text="Thu Ha", claim_key="k1",
                    episode_id="a", source_path="p1")
        _seed_claim(conn, object_text="Thu Ha", claim_key="k2",
                    episode_id="b", source_path="p2")

    def judge(_prompt):
        return ('{"decision":"merge","winner_slug":"thuha",'
                '"confidence":0.95,"reasoning":"same person"}')

    out = consolidation.consolidate_aliases(
        apply=True,
        judge_fn=judge,
        budget_tokens=5_000,
    )
    assert out["judged"] == 1
    assert out["merged"] == 1
    assert out["rewritten_rows"] == 2
    # object_slug on both claims is now 'thuha'.
    with db.connect() as conn:
        slugs = [r[0] for r in conn.execute(
            "SELECT object_slug FROM fact_claims WHERE object_text='Thu Ha'"
        ).fetchall()]
        assert slugs == ["thuha", "thuha"]
        # Alias row is present.
        row = conn.execute(
            "SELECT alias FROM aliases WHERE entity_id=?",
            (thuha_id,),
        ).fetchone()
        assert row and row[0] == "thu ha"


def test_consolidate_aliases_keep_distinct_writes_disambig(tmp_vault):
    from brain import consolidation, db
    with db.connect() as conn:
        _seed_entity(conn, slug="thuha", name="Thuha")
        _seed_entity(conn, slug="stephane", name="stephane")
        _seed_claim(conn, object_text="Thu Ha", claim_key="k1",
                    episode_id="a", source_path="p1")
        _seed_claim(conn, object_text="Thu Ha", claim_key="k2",
                    episode_id="b", source_path="p2")

    def judge(_prompt):
        return ('{"decision":"keep_distinct","winner_slug":null,'
                '"confidence":0.3,"reasoning":"different person"}')

    out = consolidation.consolidate_aliases(
        apply=True, judge_fn=judge, budget_tokens=5_000,
    )
    assert out["kept_distinct"] == 1
    assert out["merged"] == 0
    # Disambiguations file records the decision.
    disambig = tmp_vault / "disambiguations.jsonl"
    last = json.loads(disambig.read_text().splitlines()[-1])
    assert last["decision"] == "not_same"
    assert last["slug"] == "thuha"


def test_consolidate_aliases_merge_requires_confidence_90(tmp_vault):
    """LLM says 'merge' but confidence is only 0.80 → downgrade to
    needs_user; no alias written, no slug rewrite."""
    from brain import consolidation, db
    with db.connect() as conn:
        _seed_entity(conn, slug="thuha", name="Thuha")
        _seed_entity(conn, slug="stephane", name="stephane")
        _seed_claim(conn, object_text="Thu Ha", claim_key="k1",
                    episode_id="a", source_path="p1")
        _seed_claim(conn, object_text="Thu Ha", claim_key="k2",
                    episode_id="b", source_path="p2")

    def judge(_prompt):
        return ('{"decision":"merge","winner_slug":"thuha",'
                '"confidence":0.8,"reasoning":"probably"}')

    out = consolidation.consolidate_aliases(
        apply=True, judge_fn=judge, budget_tokens=5_000,
    )
    assert out["merged"] == 0
    assert out["needs_user"] == 1
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT object_slug FROM fact_claims WHERE object_text='Thu Ha'"
        ).fetchall()
        assert all(r[0] is None for r in rows)


def test_consolidate_aliases_needs_user_on_ambiguous(tmp_vault):
    from brain import consolidation, db
    with db.connect() as conn:
        _seed_entity(conn, slug="thuha", name="Thuha")
        _seed_entity(conn, slug="stephane", name="stephane")
        _seed_claim(conn, object_text="Thu Ha", claim_key="k1",
                    episode_id="a", source_path="p1")
        _seed_claim(conn, object_text="Thu Ha", claim_key="k2",
                    episode_id="b", source_path="p2")

    def judge(_prompt):
        return ('{"decision":"needs_user","winner_slug":null,'
                '"confidence":0.5,"reasoning":"unsure"}')

    out = consolidation.consolidate_aliases(
        apply=True, judge_fn=judge, budget_tokens=5_000,
    )
    assert out["needs_user"] == 1


def test_consolidate_aliases_skips_correction_sourced(tmp_vault):
    from brain import consolidation, db
    with db.connect() as conn:
        _seed_entity(conn, slug="thuha", name="Thuha")
        _seed_entity(conn, slug="stephane", name="stephane")
        _seed_claim(conn, object_text="Thu Ha", claim_key="k1",
                    episode_id="a", source_path="p1",
                    trust_source="correction")
        _seed_claim(conn, object_text="Thu Ha", claim_key="k2",
                    episode_id="b", source_path="p2")

    called: list[str] = []

    def judge(p):
        called.append(p)
        return None

    out = consolidation.consolidate_aliases(
        apply=True, judge_fn=judge, budget_tokens=5_000,
    )
    assert called == []
    assert out["skipped_correction"] == 1


def test_consolidate_aliases_budget_exhausted_short_circuits(tmp_vault):
    from brain import consolidation, db
    with db.connect() as conn:
        _seed_entity(conn, slug="thuha", name="Thuha")
        _seed_entity(conn, slug="stephane", name="stephane")
        _seed_claim(conn, object_text="Thu Ha", claim_key="k1",
                    episode_id="a", source_path="p1")
        _seed_claim(conn, object_text="Thu Ha", claim_key="k2",
                    episode_id="b", source_path="p2")

    out = consolidation.consolidate_aliases(
        apply=False, judge_fn=lambda p: None,
        budget_tokens=10,           # below 1500 cap
    )
    assert out["status"] == "budget_exhausted"
    assert out["judged"] == 0


def test_consolidate_aliases_judge_failure_is_counted(tmp_vault):
    from brain import consolidation, db
    with db.connect() as conn:
        _seed_entity(conn, slug="thuha", name="Thuha")
        _seed_entity(conn, slug="stephane", name="stephane")
        _seed_claim(conn, object_text="Thu Ha", claim_key="k1",
                    episode_id="a", source_path="p1")
        _seed_claim(conn, object_text="Thu Ha", claim_key="k2",
                    episode_id="b", source_path="p2")

    def judge(_p):
        return "not valid json"

    out = consolidation.consolidate_aliases(
        apply=True, judge_fn=judge, budget_tokens=5_000,
    )
    assert out["judge_failed"] == 1
    assert out["merged"] == 0


def test_consolidate_aliases_dry_run_makes_no_changes(tmp_vault):
    from brain import consolidation, db
    with db.connect() as conn:
        _seed_entity(conn, slug="thuha", name="Thuha")
        _seed_entity(conn, slug="stephane", name="stephane")
        _seed_claim(conn, object_text="Thu Ha", claim_key="k1",
                    episode_id="a", source_path="p1")
        _seed_claim(conn, object_text="Thu Ha", claim_key="k2",
                    episode_id="b", source_path="p2")

    def judge(_p):
        return ('{"decision":"merge","winner_slug":"thuha",'
                '"confidence":1.0,"reasoning":"same"}')

    out = consolidation.consolidate_aliases(
        apply=False, judge_fn=judge, budget_tokens=5_000,
    )
    assert out["merged"] == 1
    # BUT: no alias row + no slug rewrite, no disambiguation row.
    with db.connect() as conn:
        alias_count = conn.execute(
            "SELECT COUNT(*) FROM aliases"
        ).fetchone()[0]
        slug_resolved = conn.execute(
            "SELECT object_slug FROM fact_claims WHERE object_text='Thu Ha'"
        ).fetchall()
    assert alias_count == 0
    assert all(r[0] is None for r in slug_resolved)
    assert not (tmp_vault / "disambiguations.jsonl").exists()


def test_consolidate_aliases_max_pairs_caps(tmp_vault):
    from brain import consolidation, db
    with db.connect() as conn:
        # Two candidate pairs: Thu Ha → thuha, Sttephane → stephane
        _seed_entity(conn, slug="thuha", name="Thuha")
        _seed_entity(conn, slug="stephane", name="stephane")
        _seed_entity(conn, slug="alice", name="Alice")
        _seed_claim(conn, object_text="Thu Ha", claim_key="k1",
                    episode_id="a", source_path="p1")
        _seed_claim(conn, object_text="Thu Ha", claim_key="k2",
                    episode_id="b", source_path="p2")
        _seed_claim(conn, object_text="Alise", claim_key="k3",
                    episode_id="c", source_path="p3")
        _seed_claim(conn, object_text="Alise", claim_key="k4",
                    episode_id="d", source_path="p4")

    def judge(_p):
        return ('{"decision":"keep_distinct","winner_slug":null,'
                '"confidence":0.1,"reasoning":"nope"}')

    out = consolidation.consolidate_aliases(
        apply=False, judge_fn=judge, budget_tokens=10_000, max_pairs=1,
    )
    assert out["judged"] == 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_aliases_flag_dry_run(tmp_vault, capsys):
    from brain import cli
    rc = cli.main(["consolidate", "--aliases"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[DRY-RUN] consolidate --aliases" in out
    assert "checked=" in out
    assert "budget=" in out


def test_cli_aliases_json_mode(tmp_vault, capsys):
    from brain import cli
    rc = cli.main(["consolidate", "--aliases", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is False
    assert "checked" in payload
    assert "tokens_spent" in payload
