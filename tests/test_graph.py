"""Tests for brain.graph, brain.triple_audit, and brain.triple_rules."""
import json
import pytest
from pathlib import Path


@pytest.fixture(autouse=True)
def tmp_brain(tmp_path, monkeypatch):
    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(config, "IDENTITY_DIR", tmp_path / "identity")
    monkeypatch.setattr(config, "GRAPH_STORE_DIR", tmp_path / ".brain.rdf")
    monkeypatch.setattr(config, "PENDING_TRIPLES_PATH", tmp_path / "pending_triples.jsonl")
    monkeypatch.setattr(config, "TRIPLE_RULES_PATH", tmp_path / "identity" / "triple_rules.jsonl")
    monkeypatch.setattr(config, "TRIPLE_RULES_MD_PATH", tmp_path / "identity" / "triple_rules.md")
    (tmp_path / "identity").mkdir(parents=True, exist_ok=True)
    return tmp_path


# ---------------------------------------------------------------------------
# graph.py
# ---------------------------------------------------------------------------

class TestGraph:
    def test_add_and_neighbors(self):
        from brain.graph import add_triple, neighbors
        assert add_triple("Son", "worksAt", "Aitomatic") is True
        result = neighbors("son")
        assert len(result) == 1
        assert result[0]["predicate"] == "worksAt"
        assert "aitomatic" in result[0]["object"]

    def test_invalid_predicate_rejected(self):
        from brain.graph import add_triple
        assert add_triple("Son", "invented_pred", "X") is False

    def test_neighbors_predicate_filter(self):
        from brain.graph import add_triple, neighbors
        add_triple("Son", "worksAt", "Aitomatic")
        add_triple("Son", "knows", "Madhav")
        result = neighbors("son", predicate="knows")
        assert len(result) == 1
        assert result[0]["predicate"] == "knows"

    def test_triple_count(self):
        from brain.graph import add_triple, triple_count
        assert triple_count() == 0
        add_triple("Son", "worksAt", "Aitomatic")
        assert triple_count() == 1

    def test_sparql_query(self):
        from brain.graph import add_triple, query
        add_triple("Son", "worksAt", "Aitomatic")
        results = query(
            "PREFIX be: <http://brain.local/e/> "
            "PREFIX bp: <http://brain.local/p/> "
            "SELECT ?org WHERE { be:son bp:worksAt ?org }"
        )
        assert isinstance(results, list)
        # At least one result with 'aitomatic' in it
        assert any("aitomatic" in str(r) for r in results)

    def test_bad_sparql_returns_error(self):
        from brain.graph import query
        result = query("NOT VALID SPARQL")
        assert "error" in result


# ---------------------------------------------------------------------------
# triple_audit.py
# ---------------------------------------------------------------------------

class TestTripleAudit:
    def test_add_and_load_pending(self):
        from brain.triple_audit import add_pending, load_pending
        add_pending([{
            "subject": "Son", "predicate": "worksAt", "object": "Aitomatic",
            "confidence": 0.6, "basis": "Son works at Aitomatic",
        }])
        items = load_pending()
        assert len(items) == 1
        assert items[0]["predicate"] == "worksAt"
        assert "id" in items[0]

    def test_remove_pending(self):
        from brain.triple_audit import add_pending, load_pending, remove_pending
        add_pending([{"subject": "A", "predicate": "knows", "object": "B",
                      "confidence": 0.5, "basis": "A knows B"}])
        items = load_pending()
        remove_pending([items[0]["id"]])
        assert load_pending() == []

    def test_walk_yes_adds_to_graph(self, tmp_brain):
        from brain.triple_audit import add_pending, walk, load_pending
        from brain.graph import neighbors
        add_pending([{
            "subject": "Son", "predicate": "knows", "object": "Madhav",
            "confidence": 0.5, "basis": "Son knows Madhav",
        }])
        tally = walk(_input=lambda _: "y")
        assert tally["yes"] == 1
        assert load_pending() == []
        result = neighbors("son", predicate="knows")
        assert len(result) == 1

    def test_walk_no_discards_and_records_rule(self, tmp_brain):
        from brain.triple_audit import add_pending, walk, load_pending
        from brain.triple_rules import _load as load_rules
        add_pending([{
            "subject": "X", "predicate": "locatedIn", "object": "Vietnam",
            "confidence": 0.5, "basis": "X is in Vietnam",
        }])
        tally = walk(_input=lambda _: "n")
        assert tally["no"] == 1
        assert load_pending() == []
        rules = load_rules()
        assert any(r["predicate"] == "locatedIn" and r["rejected"] == 1 for r in rules)

    def test_walk_quit_leaves_queue_intact(self):
        from brain.triple_audit import add_pending, walk, load_pending
        add_pending([
            {"subject": "A", "predicate": "knows", "object": "B",
             "confidence": 0.5, "basis": "A knows B"},
            {"subject": "C", "predicate": "knows", "object": "D",
             "confidence": 0.5, "basis": "C knows D"},
        ])
        tally = walk(_input=lambda _: "q")
        assert tally["quit"] == 1
        assert len(load_pending()) == 2


# ---------------------------------------------------------------------------
# triple_rules.py
# ---------------------------------------------------------------------------

class TestTripleRules:
    def test_record_and_adjust_confidence(self):
        from brain.triple_rules import record_decision, adjusted_confidence
        # No history → pass through unchanged
        assert adjusted_confidence("worksAt", 0.7) == pytest.approx(0.7)
        # Record 2 confirmed, 0 rejected (< 3 total → still pass through)
        record_decision("worksAt", "basis1", "y")
        record_decision("worksAt", "basis2", "y")
        assert adjusted_confidence("worksAt", 0.7) == pytest.approx(0.7)
        # Add one more → total = 3, accuracy = 1.0 → no change
        record_decision("worksAt", "basis3", "y")
        assert adjusted_confidence("worksAt", 0.8) == pytest.approx(0.8)

    def test_rejection_scales_down_confidence(self):
        from brain.triple_rules import record_decision, adjusted_confidence
        # 1 confirmed, 2 rejected → accuracy = 1/3 ≈ 0.33
        record_decision("locatedIn", "b1", "y")
        record_decision("locatedIn", "b2", "n")
        record_decision("locatedIn", "b3", "n")
        adj = adjusted_confidence("locatedIn", 0.9)
        assert adj < 0.9 * 0.4  # should be scaled down significantly

    def test_rules_md_generated(self):
        from brain.triple_rules import record_decision, rules_for_prompt
        record_decision("worksAt", "X works at Y", "y")
        record_decision("worksAt", "A works at B", "y")
        record_decision("worksAt", "C works at D", "y")
        md = rules_for_prompt()
        assert "worksAt" in md
