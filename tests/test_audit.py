"""Tests for `brain.audit` — the SessionStart top-N surface.

Covers ranking (contested > dedupe > low-confidence), the empty-vault
silent-stdout contract, the ledger-driven freshness guarantee for dedupe
items, and per-signal fault isolation (one broken signal must not crash
the hook).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
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


# ── reviewed / decay / walker ─────────────────────────────────────────────


def _write_reviewed_entity(type_dir: Path, slug: str, reviewed: str,
                           *, source_count: int = 1,
                           first_seen: str = "2026-01-01") -> Path:
    """Single-source entity with a `reviewed: <date>` stamp already in
    its frontmatter — simulates an item the user has previously walked."""
    p = type_dir / f"{slug}.md"
    p.write_text(
        "---\n"
        f"type: {type_dir.name.rstrip('s')}\n"
        f"first_seen: {first_seen}\n"
        f"source_count: {source_count}\n"
        f"reviewed: {reviewed}\n"
        "---\n\n# x\n"
    )
    return p


def test_recently_reviewed_item_is_suppressed(tmp_vault):
    """An item stamped reviewed within the last REVIEW_DECAY_DAYS must
    not appear in the low-confidence surface — that's the whole point of
    the new mechanism."""
    today = date.today().isoformat()
    _write_reviewed_entity(tmp_vault / "entities" / "insights",
                           "freshly-reviewed", reviewed=today)
    items = audit.top_n(limit=10)
    assert items == []


def test_old_review_re_surfaces_after_decay_window(tmp_vault):
    """Past the REVIEW_DECAY_DAYS window the item must come back — facts
    decay, and an audit a year ago doesn't vouch for today."""
    long_ago = (date.today() - timedelta(days=audit.REVIEW_DECAY_DAYS + 5)
                ).isoformat()
    _write_reviewed_entity(tmp_vault / "entities" / "insights",
                           "stale-review", reviewed=long_ago)
    items = audit.top_n(limit=10)
    assert any("Stale Review" in it.label for it in items)


def test_malformed_reviewed_date_does_not_hide_item(tmp_vault):
    """Typo in the reviewed line must fail open — better to nag than to
    permanently disappear an item because of a hand-edit."""
    _write_reviewed_entity(tmp_vault / "entities" / "insights",
                           "bad-date", reviewed="not-a-date")
    items = audit.top_n(limit=10)
    assert any("Bad Date" in it.label for it in items)


def test_mark_reviewed_round_trips_through_top_n(tmp_vault):
    """End-to-end: an item that surfaces today should NOT surface after
    `mark_reviewed` is called on it. This is the exact bug the user
    flagged ("audit xong vẫn nhảy ra y nguyên")."""
    insights = tmp_vault / "entities" / "insights"
    p = _write_entity(insights, "audit-me", source_count=1,
                     first_seen="2026-01-01")
    before = audit.top_n(limit=5)
    assert any(it.path == p for it in before)

    assert audit.mark_reviewed(p) is True

    after = audit.top_n(limit=5)
    assert all(it.path != p for it in after)


def test_mark_reviewed_is_idempotent_same_day(tmp_vault):
    """Re-stamping with today's date is a no-op (returns False)."""
    insights = tmp_vault / "entities" / "insights"
    p = _write_entity(insights, "x", source_count=1)
    assert audit.mark_reviewed(p) is True
    assert audit.mark_reviewed(p) is False


def test_mark_contested_flips_into_contested_bucket(tmp_vault):
    """`contest` action moves a low-conf item into the higher-priority
    contested bucket — it's NOT silently hidden, because a wrong fact
    matters more than an unverified one."""
    insights = tmp_vault / "entities" / "insights"
    p = _write_entity(insights, "wrong-fact", source_count=1,
                     first_seen="2026-01-01")
    assert audit.mark_contested(p) is True
    items = audit.top_n(limit=5)
    contested = [it for it in items if it.kind == "contested"]
    assert any(it.path == p for it in contested)


def test_resolve_contested_clears_status_line(tmp_vault):
    insights = tmp_vault / "entities" / "insights"
    p = _write_entity(insights, "was-wrong", contested=True, source_count=2)
    assert audit.resolve_contested(p) is True
    assert "status: contested" not in p.read_text()
    # And the contested bucket should be empty for it now.
    items = audit.top_n(limit=5)
    assert all(it.kind != "contested" for it in items)


def test_set_frontmatter_field_preserves_other_lines(tmp_vault):
    insights = tmp_vault / "entities" / "insights"
    p = _write_entity(insights, "preserve", source_count=1,
                     first_seen="2026-04-11")
    body_before = p.read_text()
    audit.mark_reviewed(p, today=date(2026, 4, 21))
    body_after = p.read_text()
    # Original frontmatter fields survive verbatim.
    assert "first_seen: 2026-04-11" in body_after
    assert "source_count: 1" in body_after
    assert "reviewed: 2026-04-21" in body_after
    # Body content untouched.
    assert "# x" in body_after
    assert body_after != body_before  # but the file did change


class _ScriptedInput:
    """Replays a fixed sequence of answers, raising EOFError if the walker
    asks more questions than scripted (catches infinite-loop regressions)."""
    def __init__(self, answers):
        self._answers = list(answers)

    def __call__(self, _prompt=""):
        if not self._answers:
            raise EOFError
        return self._answers.pop(0)


def test_walker_keep_marks_reviewed_and_returns_tally(tmp_vault):
    insights = tmp_vault / "entities" / "insights"
    _write_entity(insights, "alpha", source_count=1, first_seen="2026-01-01")
    items = audit.top_n(limit=5)
    tally = audit.walk(items, _input=_ScriptedInput(["k"]))
    assert tally["reviewed"] == 1
    # Suppression actually took effect.
    assert audit.top_n(limit=5) == []


def test_walker_quit_breaks_loop_without_touching_remaining(tmp_vault):
    """Quitting on item 1 must NOT review/contest items 2+ as a side effect."""
    insights = tmp_vault / "entities" / "insights"
    p1 = _write_entity(insights, "first",  source_count=1,
                      first_seen="2025-01-01")  # oldest, comes first
    p2 = _write_entity(insights, "second", source_count=1,
                      first_seen="2026-04-01")
    items = audit.top_n(limit=5)
    tally = audit.walk(items, _input=_ScriptedInput(["q"]))
    assert tally["quit"] == 1
    assert tally["reviewed"] == 0
    # Neither file should have been stamped.
    assert "reviewed:" not in p1.read_text()
    assert "reviewed:" not in p2.read_text()


def test_walker_contest_routes_to_contested_bucket(tmp_vault):
    insights = tmp_vault / "entities" / "insights"
    _write_entity(insights, "smells-fishy", source_count=1,
                  first_seen="2026-01-01")
    items = audit.top_n(limit=5)
    tally = audit.walk(items, _input=_ScriptedInput(["c"]))
    assert tally["contested"] == 1
    after = audit.top_n(limit=5)
    assert any(it.kind == "contested" for it in after)


def test_walker_resolve_clears_contested(tmp_vault):
    domains = tmp_vault / "entities" / "domains"
    _write_entity(domains, "was-wrong", contested=True)
    items = audit.top_n(limit=5)
    assert items[0].kind == "contested"
    tally = audit.walk(items, _input=_ScriptedInput(["r"]))
    assert tally["resolved"] == 1
    assert audit.top_n(limit=5) == []


def test_walker_eof_treated_as_quit(tmp_vault):
    """Piped/empty stdin must not spin — EOF acts like `q`."""
    insights = tmp_vault / "entities" / "insights"
    _write_entity(insights, "x", source_count=1, first_seen="2026-01-01")
    items = audit.top_n(limit=5)
    tally = audit.walk(items, _input=_ScriptedInput([]))  # immediate EOF
    assert tally["quit"] == 1
    assert tally["reviewed"] == 0


def test_main_walk_flag_invokes_walker(tmp_vault, capsys, monkeypatch):
    """`python -m brain.audit --walk` must drive walk(), print summary."""
    insights = tmp_vault / "entities" / "insights"
    _write_entity(insights, "walk-me", source_count=1,
                  first_seen="2026-01-01")
    monkeypatch.setattr("builtins.input", _ScriptedInput(["k"]))
    rc = audit.main(["--walk", "--limit", "3"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "🧠 Brain audit" in out
    assert "Done — 1 reviewed" in out
