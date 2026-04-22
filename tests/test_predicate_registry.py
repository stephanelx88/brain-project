"""Tests for brain.predicate_registry.

Covers the 10 cases listed in docs/ontologist-adaptive-vocabulary-spec.md §1.6.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest


@pytest.fixture(autouse=True)
def tmp_brain(tmp_path, monkeypatch):
    """Redirect the registry to a tmp path for every test. Mirrors test_graph.py."""
    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(config, "IDENTITY_DIR", tmp_path / "identity")
    monkeypatch.setattr(
        config, "PREDICATE_REGISTRY_PATH", tmp_path / "identity" / "predicates.jsonl"
    )
    monkeypatch.setattr(config, "GRAPH_STORE_DIR", tmp_path / ".brain.rdf")
    monkeypatch.setattr(
        config, "PENDING_TRIPLES_PATH", tmp_path / "pending_triples.jsonl"
    )
    monkeypatch.setattr(
        config, "TRIPLE_RULES_PATH", tmp_path / "identity" / "triple_rules.jsonl"
    )
    monkeypatch.setattr(
        config, "TRIPLE_RULES_MD_PATH", tmp_path / "identity" / "triple_rules.md"
    )
    (tmp_path / "identity").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _load_rows(tmp_brain):
    p = tmp_brain / "identity" / "predicates.jsonl"
    if not p.exists():
        return []
    rows = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


class TestObserveAndStatus:
    def test_observe_creates_proposed_row(self, tmp_brain):
        from brain import predicate_registry
        predicate_registry.observe("presentedAt", basis="Son presentedAt PyVN")
        row = next(r for r in _load_rows(tmp_brain) if r["predicate"] == "presentedAt")
        assert row["status"] == "proposed"
        assert row["confirmed"] == 0
        assert row["rejected"] == 0
        assert "Son presentedAt PyVN" in row["examples"]

    def test_unknown_predicate_status(self):
        from brain import predicate_registry
        # worksAt bootstraps to active, so anything unseen is 'unknown'
        assert predicate_registry.status("neverHeardOfThis") == "unknown"

    def test_bootstrap_seeds_legacy_15_as_active(self):
        from brain import predicate_registry
        predicate_registry.bootstrap_from_legacy()
        for pred in ("worksAt", "knows", "manages", "contradicts"):
            assert predicate_registry.status(pred) == "active"
        assert predicate_registry.bootstrap_from_legacy() == 0  # idempotent

    def test_observe_appends_example_without_duplicating(self, tmp_brain):
        from brain import predicate_registry
        predicate_registry.observe("mentoredBy", basis="A mentoredBy B")
        predicate_registry.observe("mentoredBy", basis="A mentoredBy B")  # dupe
        predicate_registry.observe("mentoredBy", basis="C mentoredBy D")
        row = next(r for r in _load_rows(tmp_brain) if r["predicate"] == "mentoredBy")
        assert len(row["examples"]) == 2


class TestPromotionGate:
    def test_three_confirms_within_30d_promotes(self, tmp_brain):
        from brain import predicate_registry
        predicate_registry.observe("presentedAt", basis="x")
        for _ in range(3):
            predicate_registry.record_decision("presentedAt", "y")
        assert predicate_registry.status("presentedAt") == "active"
        row = next(r for r in _load_rows(tmp_brain) if r["predicate"] == "presentedAt")
        assert row["promoted_at"] == date.today().isoformat()

    def test_three_confirms_outside_window_does_not_promote(self, tmp_brain, monkeypatch):
        from brain import predicate_registry
        predicate_registry.observe("ancientPredicate", basis="x")
        # Rewrite first_seen to 60 days ago → outside 30d window
        rows = _load_rows(tmp_brain)
        old = (date.today() - timedelta(days=60)).isoformat()
        for r in rows:
            if r["predicate"] == "ancientPredicate":
                r["first_seen"] = old
        (tmp_brain / "identity" / "predicates.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n"
        )
        for _ in range(3):
            predicate_registry.record_decision("ancientPredicate", "y")
        assert predicate_registry.status("ancientPredicate") == "proposed"

    def test_three_rejects_retires(self):
        from brain import predicate_registry
        predicate_registry.observe("junkPred", basis="x")
        for _ in range(3):
            predicate_registry.record_decision("junkPred", "n")
        assert predicate_registry.status("junkPred") == "retired"

    def test_single_rejection_does_not_kill(self):
        from brain import predicate_registry
        predicate_registry.observe("maybePred", basis="x")
        predicate_registry.record_decision("maybePred", "n")
        assert predicate_registry.status("maybePred") == "proposed"


class TestAddTripleIntegration:
    def test_active_predicate_passes_add_triple(self):
        from brain.graph import add_triple
        assert add_triple("Son", "worksAt", "Aitomatic") is True

    def test_proposed_predicate_routes_to_audit(self, tmp_brain):
        from brain.graph import add_triple
        from brain import predicate_registry
        assert add_triple("Son", "mentored", "Trinh") is False
        # First sighting → proposed, with basis recorded
        assert predicate_registry.status("mentored") == "proposed"
        row = next(r for r in _load_rows(tmp_brain) if r["predicate"] == "mentored")
        assert any("mentored" in ex for ex in row["examples"])

    def test_retired_predicate_drops_and_records_failure(self, tmp_brain):
        from brain import predicate_registry
        from brain.graph import add_triple

        # failures._ledger_path() honours config.BRAIN_DIR already — the
        # autouse fixture points BRAIN_DIR at tmp_brain, so a
        # retired-predicate write should land at tmp_brain/failures.jsonl.
        failures_path = tmp_brain / "failures.jsonl"
        predicate_registry.observe("badPred", basis="x")
        predicate_registry.retire("badPred")

        assert add_triple("Son", "badPred", "X") is False
        assert failures_path.exists()
        rows = [
            json.loads(line) for line in failures_path.read_text().splitlines() if line.strip()
        ]
        assert any(r.get("source") == "retired_predicate" for r in rows)


class TestAliasNormalization:
    def test_alias_collapses_predicates(self, tmp_brain):
        from brain import predicate_registry
        predicate_registry.observe("presentedAt", basis="a")
        predicate_registry.observe("presented_at", basis="b")  # alias → same row
        predicate_registry.observe("PRESENTED-AT", basis="c")  # alias → same row
        preds = [r["predicate"] for r in _load_rows(tmp_brain)]
        # Only one row should exist for the normalised predicate
        assert preds.count("presentedAt") == 1
        assert "presented_at" not in preds
        # All three bases accumulated on the one row
        row = next(r for r in _load_rows(tmp_brain) if r["predicate"] == "presentedAt")
        assert set(row["examples"]) == {"a", "b", "c"}


class TestAuditWalker:
    def test_list_proposed_feeds_walker(self):
        from brain import predicate_registry
        predicate_registry.observe("p1", basis="x")
        predicate_registry.observe("p2", basis="y")
        predicate_registry.observe("worksAt", basis="z")  # active, not proposed
        proposed = predicate_registry.list_proposed()
        names = {r["predicate"] for r in proposed}
        assert names == {"p1", "p2"}

    def test_walker_surfaces_proposed_predicates(self):
        from brain import predicate_registry
        from brain.audit import _proposed_predicate_items

        predicate_registry.observe("myNewPred", basis="A myNewPred B")
        items = _proposed_predicate_items()
        assert len(items) == 1
        assert items[0].kind == "proposed_predicate"
        assert "myNewPred" in items[0].label

    def test_walker_promotes_via_three_y_answers(self):
        from brain import predicate_registry
        from brain.audit import AuditItem, walk

        predicate_registry.observe("shoutOutTo", basis="A shoutOutTo B")

        # Three separate audit items for the same predicate simulate the
        # three independent sightings it takes to promote.
        items = [
            AuditItem(
                kind="proposed_predicate",
                label=f"Predicate? · `shoutOutTo` (try {i})",
                detail="",
                priority=58,
                extra={"predicate": "shoutOutTo"},
            )
            for i in range(3)
        ]
        answers = iter(["y", "y", "y"])
        walk(items, _input=lambda _: next(answers))
        assert predicate_registry.status("shoutOutTo") == "active"
