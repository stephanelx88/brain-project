"""Tests for `brain.audit` — the SessionStart top-N surface.

Covers ranking (contested > dedupe > low-confidence), the empty-vault
silent-stdout contract, the ledger-driven freshness guarantee for dedupe
items, and per-signal fault isolation (one broken signal must not crash
the hook).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import brain.audit as audit
import brain.config as config


@pytest.fixture
def tmp_vault(tmp_path, monkeypatch):
    """Minimal brain layout for audit tests."""
    brain = tmp_path / "brain"
    (brain / "entities" / "domains").mkdir(parents=True)
    (brain / "entities" / "insights").mkdir(parents=True)
    (brain / "entities" / "decisions").mkdir(parents=True)
    (brain / "timeline").mkdir(parents=True)
    (brain / "identity").mkdir()
    (brain / "raw").mkdir()

    types = {
        "domains": brain / "entities" / "domains",
        "insights": brain / "entities" / "insights",
        "decisions": brain / "entities" / "decisions",
    }
    monkeypatch.setattr(audit, "ENTITY_TYPES", types)
    monkeypatch.setattr(audit, "BRAIN_DIR", brain)
    monkeypatch.setattr(audit, "TIMELINE_DIR", brain / "timeline")
    # ensure_dirs() is called by main() — point config at our tmp dirs too
    monkeypatch.setattr(config, "BRAIN_DIR", brain)
    monkeypatch.setattr(config, "ENTITIES_DIR", brain / "entities")
    monkeypatch.setattr(config, "RAW_DIR", brain / "raw")
    monkeypatch.setattr(config, "INDEX_FILE", brain / "index.md")
    monkeypatch.setattr(config, "LOG_FILE", brain / "log.md")
    monkeypatch.setattr(config, "IDENTITY_DIR", brain / "identity")
    monkeypatch.setattr(config, "ENTITY_TYPES", types)
    return brain


def _write_entity(type_dir: Path, slug: str, *, contested: bool = False,
                  source_count: int = 2, first_seen: str = "2026-04-15") -> Path:
    p = type_dir / f"{slug}.md"
    fm = [
        "---",
        f"type: {type_dir.name.rstrip('s')}",
        f"first_seen: {first_seen}",
        f"source_count: {source_count}",
    ]
    if contested:
        fm.append("status: contested")
    fm.append("---")
    p.write_text("\n".join(fm) + "\n\n# x\n")
    return p


def test_empty_vault_returns_empty_string(tmp_vault):
    """Clean brain → no items → no stdout pollution at session start."""
    items = audit.top_n(limit=3)
    assert items == []
    assert audit.format_for_session(items) == ""


def test_contested_outranks_low_confidence(tmp_vault):
    """Contested fact should always rank above a low-conf one."""
    _write_entity(tmp_vault / "entities" / "insights", "old-low-conf",
                  source_count=1, first_seen="2026-01-01")
    _write_entity(tmp_vault / "entities" / "domains", "disputed-thing",
                  contested=True)
    items = audit.top_n(limit=3)
    assert items[0].kind == "contested"
    assert "Disputed Thing" in items[0].label


def test_low_confidence_oldest_first(tmp_vault):
    """Among single-source items, the oldest should surface first."""
    insights = tmp_vault / "entities" / "insights"
    _write_entity(insights, "newish", source_count=1, first_seen="2026-04-01")
    _write_entity(insights, "ancient", source_count=1, first_seen="2025-01-01")
    _write_entity(insights, "middle", source_count=1, first_seen="2026-02-01")

    items = audit.top_n(limit=3)
    labels = [it.label for it in items]
    # All three are low_confidence; ordered by first_seen ascending.
    assert "Ancient" in labels[0]
    assert "Middle" in labels[1]
    assert "Newish" in labels[2]


def test_low_confidence_detail_includes_path(tmp_vault):
    """Detail line must carry the relative entity path so the user can
    click/paste it directly from the SessionStart audit block — the
    title-cased label on its own isn't round-trippable through
    `brain_get` (slugs are lowercase-hyphenated)."""
    insights = tmp_vault / "entities" / "insights"
    _write_entity(insights, "some-lowconf-slug",
                  source_count=1, first_seen="2026-04-11")
    items = audit.top_n(limit=3)
    lc = [it for it in items if it.kind == "low_confidence"]
    assert len(lc) == 1
    assert "entities/insights/some-lowconf-slug.md" in lc[0].detail


def test_contested_excludes_from_low_conf(tmp_vault):
    """An entity that's BOTH single-source and contested should appear
    only as contested (not double-counted)."""
    _write_entity(tmp_vault / "entities" / "insights", "both",
                  contested=True, source_count=1, first_seen="2025-01-01")
    items = audit.top_n(limit=10)
    kinds = [it.kind for it in items]
    assert kinds.count("contested") == 1
    assert kinds.count("low_confidence") == 0


def test_skips_underscore_files(tmp_vault):
    """`_MOC.md` and `_placeholder.md` aren't entities — must be ignored."""
    insights = tmp_vault / "entities" / "insights"
    (insights / "_MOC.md").write_text(
        "---\nstatus: contested\nsource_count: 1\n---\n"
    )
    items = audit.top_n(limit=10)
    assert items == []


def _write_ledger(brain_dir: Path, entries: dict) -> Path:
    """Write the canonical dedupe ledger that audit now reads."""
    ledger = brain_dir / ".dedupe.ledger.json"
    ledger.write_text(json.dumps(entries))
    return ledger


def test_dedupe_only_surfaces_merge_verdict(tmp_vault):
    """Only `verdict: merge` ledger entries should appear."""
    insights = tmp_vault / "entities" / "insights"
    _write_entity(insights, "foo")
    _write_entity(insights, "bar")
    _write_entity(insights, "alpha")
    _write_entity(insights, "beta")
    _write_entity(insights, "zed")
    _write_entity(insights, "qux")
    _write_ledger(tmp_vault, {
        "insights|foo|bar":   {"verdict": "merge",  "cosine": 0.92},
        "insights|alpha|beta": {"verdict": "unsure", "cosine": 0.71},
        "insights|zed|qux":   {"verdict": "split",  "cosine": 0.60},
    })
    items = audit.top_n(limit=10)
    dedupe_items = [it for it in items if it.kind == "dedupe"]
    assert len(dedupe_items) == 1
    assert "foo ⇄ bar" in dedupe_items[0].label


def test_dedupe_skips_applied_entries(tmp_vault):
    """Once a merge has been applied, the ledger marks it and audit must
    drop it — otherwise users get re-nagged about already-resolved items."""
    insights = tmp_vault / "entities" / "insights"
    _write_entity(insights, "foo")
    _write_entity(insights, "bar")
    _write_ledger(tmp_vault, {
        "insights|foo|bar": {"verdict": "merge", "cosine": 0.9, "applied": True},
    })
    items = audit.top_n(limit=10)
    assert all(it.kind != "dedupe" for it in items)


def test_dedupe_skips_missing_entity_files(tmp_vault):
    """If one side of the pair was archived/deleted on disk, the ledger
    entry is stale and must not surface — file status is the tiebreaker."""
    insights = tmp_vault / "entities" / "insights"
    _write_entity(insights, "foo")
    # note: `bar` is NOT written, simulating a completed/reverted merge
    _write_ledger(tmp_vault, {
        "insights|foo|bar": {"verdict": "merge", "cosine": 0.9},
    })
    items = audit.top_n(limit=10)
    assert all(it.kind != "dedupe" for it in items)


def test_dedupe_skips_superseded_pairs(tmp_vault):
    """An entity whose frontmatter was flipped to `status: superseded`
    is already resolved; the ledger row is stale — drop it."""
    insights = tmp_vault / "entities" / "insights"
    _write_entity(insights, "foo")
    bar = insights / "bar.md"
    bar.write_text(
        "---\ntype: insight\nfirst_seen: 2026-04-10\n"
        "source_count: 2\nstatus: superseded\n---\n\n# x\n"
    )
    _write_ledger(tmp_vault, {
        "insights|foo|bar": {"verdict": "merge", "cosine": 0.9},
    })
    items = audit.top_n(limit=10)
    assert all(it.kind != "dedupe" for it in items)


def test_limit_is_respected(tmp_vault):
    """`limit=2` returns at most 2 items even when more candidates exist."""
    insights = tmp_vault / "entities" / "insights"
    for i in range(5):
        _write_entity(insights, f"item-{i}", source_count=1,
                      first_seen=f"2026-04-{i+1:02d}")
    assert len(audit.top_n(limit=2)) == 2
    assert len(audit.top_n(limit=0)) == 0


def test_format_starts_with_brain_emoji(tmp_vault):
    """The session-context block must be greppable by the agent rule
    ('When the SessionStart hook prepends a `🧠 Brain audit —` block …')."""
    items = [audit.AuditItem(kind="contested", label="X", priority=100)]
    block = audit.format_for_session(items)
    assert block.startswith("🧠 Brain audit")
    assert "1. X" in block


def test_per_signal_failure_does_not_crash(tmp_vault, monkeypatch):
    """A bug in one signal must not nuke the entire audit."""
    def boom():
        raise RuntimeError("simulated dedupe parser failure")

    monkeypatch.setattr(audit, "_dedupe_items", boom)
    _write_entity(tmp_vault / "entities" / "domains", "still-works",
                  contested=True)

    items = audit.top_n(limit=3)
    # Contested signal still fires even though dedupe blew up.
    assert any(it.kind == "contested" for it in items)


def test_main_prints_nothing_for_clean_vault(tmp_vault, capsys):
    """SessionStart hook contract: silent on a clean brain."""
    rc = audit.main([])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""


def test_main_prints_block_when_items_present(tmp_vault, capsys):
    _write_entity(tmp_vault / "entities" / "domains", "needs-review",
                  contested=True)
    rc = audit.main(["--limit", "3"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "🧠 Brain audit" in captured.out
    assert "Needs Review" in captured.out


def test_main_always_returns_zero_even_on_failure(tmp_vault, monkeypatch, capsys):
    """SessionStart hook must never get a nonzero exit."""
    def boom(*a, **k):
        raise RuntimeError("simulated full failure")
    monkeypatch.setattr(audit, "top_n", boom)
    assert audit.main([]) == 0
