"""Tests for `brain.round_robin` — picker-driven autoresearch wheel.

Each picker gets fed a tiny, hand-built SQLite mirror so the assertions
stay deterministic. We never touch ~/.brain or live data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import brain.db as db
import brain.round_robin as rr


def _days_ago_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """A throwaway SQLite mirror at `<tmp>/.brain.db` with the real schema.

    Tests insert minimal rows and assert pickers see them. Each test gets
    its own DB so we never get cross-pollination between cases.
    """
    db_path = tmp_path / ".brain.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    with db.connect() as conn:
        yield conn


def _add_entity(conn, *, name, type_, last_updated, status="current",
                first_seen=None, slug=None) -> int:
    cur = conn.execute(
        "INSERT INTO entities "
        "  (path, type, slug, name, status, first_seen, last_updated) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            f"entities/{type_}/{slug or name.lower()}.md",
            type_,
            slug or name.lower().replace(" ", "-"),
            name,
            status,
            first_seen or _days_ago_iso(0),
            last_updated,
        ),
    )
    return cur.lastrowid


def _add_fact(conn, entity_id: int, text: str, source: str = "test") -> None:
    conn.execute(
        "INSERT INTO facts (entity_id, text, source, fact_date) VALUES (?,?,?,?)",
        (entity_id, text, source, _days_ago_iso(0)),
    )


# ---------- pick_stale_entity --------------------------------------------


def test_stale_entity_returns_none_on_empty_db(fresh_db):
    assert rr.pick_stale_entity() is None


def test_stale_entity_finds_old_referenced_subject(fresh_db):
    # Stale subject — old + name long enough to dodge the 4-char skip
    stale_id = _add_entity(
        fresh_db, name="Madhav Kamath", type_="people",
        last_updated=_days_ago_iso(90),
    )
    # Two newer entities whose facts mention the stale subject
    fresh_a = _add_entity(
        fresh_db, name="Project Alpha", type_="projects",
        last_updated=_days_ago_iso(5), slug="project-alpha",
    )
    fresh_b = _add_entity(
        fresh_db, name="Project Beta", type_="projects",
        last_updated=_days_ago_iso(3), slug="project-beta",
    )
    _add_fact(fresh_db, fresh_a, "Madhav Kamath drives the BMS rollout.")
    _add_fact(fresh_db, fresh_b, "Madhav Kamath asked for a daily report.")
    fresh_db.commit()

    q = rr.pick_stale_entity()
    assert q is not None
    assert "Madhav Kamath" in q
    assert "stale-entity-sweep" in q
    assert "2 newer entities" in q


def test_stale_entity_skips_short_names(fresh_db):
    """A 3-char name like 'Son' would match too many fact rows — skip it
    so we don't waste a cycle on noise."""
    _add_entity(
        fresh_db, name="Son", type_="people",
        last_updated=_days_ago_iso(90),
    )
    fresh = _add_entity(
        fresh_db, name="X Project", type_="projects",
        last_updated=_days_ago_iso(5),
    )
    _add_fact(fresh_db, fresh, "Son did stuff")
    _add_fact(fresh_db, fresh, "Son also did other stuff")
    fresh_db.commit()
    assert rr.pick_stale_entity() is None


def test_stale_entity_skips_archived(fresh_db):
    stale = _add_entity(
        fresh_db, name="Old Project", type_="projects",
        last_updated=_days_ago_iso(120), status="archived",
    )
    fresh = _add_entity(
        fresh_db, name="Newer Project", type_="projects",
        last_updated=_days_ago_iso(1),
    )
    _add_fact(fresh_db, fresh, "Old Project stuff")
    _add_fact(fresh_db, fresh, "Old Project more")
    fresh_db.commit()
    assert rr.pick_stale_entity() is None


# ---------- pick_cross_project_person ------------------------------------


def test_cross_project_finds_person_in_three_projects(fresh_db):
    person_id = _add_entity(
        fresh_db, name="Annie Wong", type_="people",
        last_updated=_days_ago_iso(2),
    )
    for i in range(3):
        proj = _add_entity(
            fresh_db, name=f"Proj {i}", type_="projects",
            last_updated=_days_ago_iso(i + 1), slug=f"proj-{i}",
        )
        _add_fact(fresh_db, proj, "Annie Wong leads this work.")
    fresh_db.commit()

    q = rr.pick_cross_project_person()
    assert q is not None
    assert "Annie Wong" in q
    assert "3 project entities" in q


def test_cross_project_skips_recently_written_subject(fresh_db):
    """If an article about the person was written in the last 14 days,
    skip — the wheel just covered them, no point repeating."""
    _add_entity(
        fresh_db, name="Annie Wong", type_="people",
        last_updated=_days_ago_iso(1),
    )
    for i in range(3):
        proj = _add_entity(
            fresh_db, name=f"Proj {i}", type_="projects",
            last_updated=_days_ago_iso(i + 1), slug=f"proj-{i}",
        )
        _add_fact(fresh_db, proj, "Annie Wong leads this work.")
    # Recent article whose name contains the person — picker should skip
    _add_entity(
        fresh_db, name="Annie Wong cross-project role", type_="articles",
        last_updated=_days_ago_iso(2), slug="annie-wong-roles",
    )
    fresh_db.commit()
    assert rr.pick_cross_project_person() is None


def test_cross_project_returns_none_when_no_one_qualifies(fresh_db):
    _add_entity(
        fresh_db, name="Solo Person", type_="people",
        last_updated=_days_ago_iso(1),
    )
    proj = _add_entity(
        fresh_db, name="Only Proj", type_="projects",
        last_updated=_days_ago_iso(1),
    )
    _add_fact(fresh_db, proj, "Solo Person did one thing.")
    fresh_db.commit()
    assert rr.pick_cross_project_person() is None


# ---------- pick_open_decision -------------------------------------------


def test_open_decision_finds_oldest(fresh_db):
    _add_entity(fresh_db, name="Decide A", type_="decisions",
                last_updated=_days_ago_iso(60), status="open",
                slug="decide-a")
    _add_entity(fresh_db, name="Decide B", type_="decisions",
                last_updated=_days_ago_iso(45), status="open",
                slug="decide-b")
    fresh_db.commit()

    q = rr.pick_open_decision()
    assert q is not None
    assert "Decide A" in q  # the older one, by SQL ORDER BY
    assert "decision-audit" in q


def test_open_decision_treats_current_as_unsettled(fresh_db):
    """The vault's default status is `current`, not `open`. The picker
    must treat it as auditable — otherwise nothing in a real vault ever
    surfaces."""
    _add_entity(fresh_db, name="Default Status One", type_="decisions",
                last_updated=_days_ago_iso(60), status="current",
                slug="d1")
    fresh_db.commit()
    q = rr.pick_open_decision()
    assert q is not None
    assert "Default Status One" in q


def test_open_decision_skips_brand_new(fresh_db):
    """Decisions made within the last 3 days aren't worth auditing —
    nothing to follow up on yet."""
    _add_entity(fresh_db, name="Just Decided", type_="decisions",
                last_updated=_days_ago_iso(1), status="open")
    fresh_db.commit()
    assert rr.pick_open_decision() is None


def test_open_decision_relaxes_on_young_vault(fresh_db):
    """If no decision is ≥30 days old, fall back to the oldest >3 days
    so a young vault still gets a concrete subject. Without this the
    new-vault case would always hit the generic fallback prompt."""
    _add_entity(fresh_db, name="Mid Age", type_="decisions",
                last_updated=_days_ago_iso(7), status="open", slug="mid")
    fresh_db.commit()
    q = rr.pick_open_decision()
    assert q is not None
    assert "Mid Age" in q


def test_open_decision_skips_closed(fresh_db):
    _add_entity(fresh_db, name="Done", type_="decisions",
                last_updated=_days_ago_iso(60), status="accepted")
    _add_entity(fresh_db, name="Shipped", type_="decisions",
                last_updated=_days_ago_iso(60), status="committed",
                slug="shipped-d")
    _add_entity(fresh_db, name="Approved", type_="decisions",
                last_updated=_days_ago_iso(60), status="approved",
                slug="approved-d")
    fresh_db.commit()
    assert rr.pick_open_decision() is None


# ---------- pick_correction_cluster --------------------------------------


def test_correction_cluster_fires_on_three_or_more(fresh_db):
    for i in range(3):
        _add_entity(fresh_db, name=f"Correction {i}", type_="corrections",
                    last_updated=_days_ago_iso(i + 1), slug=f"corr-{i}")
    fresh_db.commit()
    q = rr.pick_correction_cluster()
    assert q is not None
    assert "3 corrections" in q


def test_correction_cluster_silent_below_threshold(fresh_db):
    for i in range(2):
        _add_entity(fresh_db, name=f"Correction {i}", type_="corrections",
                    last_updated=_days_ago_iso(i + 1), slug=f"corr-{i}")
    fresh_db.commit()
    assert rr.pick_correction_cluster() is None


# ---------- pick_open_issue ----------------------------------------------


def test_open_issue_picks_oldest_open(fresh_db):
    _add_entity(fresh_db, name="Bug A", type_="issues",
                last_updated=_days_ago_iso(40), status="open", slug="bug-a")
    _add_entity(fresh_db, name="Bug B", type_="issues",
                last_updated=_days_ago_iso(20), status="open", slug="bug-b")
    fresh_db.commit()
    q = rr.pick_open_issue()
    assert q is not None
    assert "Bug A" in q


def test_open_issue_treats_current_as_unresolved(fresh_db):
    """Same vault default-status quirk as decisions — `current` issues
    must be auditable, otherwise nothing fires on a real vault."""
    _add_entity(fresh_db, name="Pending Bug", type_="issues",
                last_updated=_days_ago_iso(20), status="current",
                slug="pb")
    fresh_db.commit()
    q = rr.pick_open_issue()
    assert q is not None
    assert "Pending Bug" in q


def test_open_issue_skips_resolved(fresh_db):
    _add_entity(fresh_db, name="Done Bug", type_="issues",
                last_updated=_days_ago_iso(40), status="fixed", slug="db1")
    _add_entity(fresh_db, name="Closed Bug", type_="issues",
                last_updated=_days_ago_iso(40), status="resolved", slug="db2")
    fresh_db.commit()
    assert rr.pick_open_issue() is None


def test_open_issue_silent_when_brand_new(fresh_db):
    """Issues created within the last 3 days are still being worked on —
    no point asking 'has this been fixed?' so soon."""
    _add_entity(fresh_db, name="Newish", type_="issues",
                last_updated=_days_ago_iso(1), status="open")
    fresh_db.commit()
    assert rr.pick_open_issue() is None


def test_open_issue_relaxes_on_young_vault(fresh_db):
    """Mirror of the decision relaxation — young vault still gets a
    concrete issue picked even when nothing is 14+ days old yet."""
    _add_entity(fresh_db, name="Mid Bug", type_="issues",
                last_updated=_days_ago_iso(7), status="open", slug="mid-bug")
    fresh_db.commit()
    q = rr.pick_open_issue()
    assert q is not None
    assert "Mid Bug" in q


# ---------- pick_under_covered_domain ------------------------------------


def test_domain_gap_finds_thin_domain(fresh_db):
    thin_id = _add_entity(
        fresh_db, name="Obscure Domain", type_="domains",
        last_updated=_days_ago_iso(30), first_seen=_days_ago_iso(30),
    )
    _add_fact(fresh_db, thin_id, "single fact")
    fresh_db.commit()
    q = rr.pick_under_covered_domain()
    assert q is not None
    assert "Obscure Domain" in q
    assert "1 fact" in q


def test_domain_gap_skips_brand_new(fresh_db):
    """Domain created in the last 7 days hasn't had a chance to accrue
    facts — don't flag it as a coverage gap."""
    _add_entity(
        fresh_db, name="Brand New", type_="domains",
        last_updated=_days_ago_iso(1), first_seen=_days_ago_iso(1),
    )
    fresh_db.commit()
    assert rr.pick_under_covered_domain() is None


def test_domain_gap_skips_well_covered(fresh_db):
    well = _add_entity(
        fresh_db, name="Rich Domain", type_="domains",
        last_updated=_days_ago_iso(20), first_seen=_days_ago_iso(20),
    )
    for i in range(5):
        _add_fact(fresh_db, well, f"fact {i}")
    fresh_db.commit()
    assert rr.pick_under_covered_domain() is None


# ---------- next_question --------------------------------------------------


def test_next_question_rotates_slot_by_cycle(fresh_db, monkeypatch):
    """Confirm cycle_n picks the right slot. Patch each picker to a
    sentinel so we don't need to seed every table to check rotation."""
    sentinels = [f"slot-{i}" for i in range(6)]
    for i, fn_name in enumerate([
        "pick_stale_entity",
        "pick_cross_project_person",
        "pick_open_decision",
        "pick_correction_cluster",
        "pick_open_issue",
        "pick_under_covered_domain",
    ]):
        monkeypatch.setattr(rr, fn_name, lambda i=i: sentinels[i])
    monkeypatch.setattr(rr, "PICKERS", [
        rr.pick_stale_entity, rr.pick_cross_project_person,
        rr.pick_open_decision, rr.pick_correction_cluster,
        rr.pick_open_issue, rr.pick_under_covered_domain,
    ])
    for cyc in range(12):
        assert rr.next_question(cyc) == sentinels[cyc % 6]


def test_next_question_falls_back_when_picker_returns_none(fresh_db, monkeypatch):
    """Empty vault → picker returns None → fallback question kicks in.

    The fallback strings are the legacy verbatim questions so the cycle
    never sees a None / empty prompt, even on a brand-new vault."""
    for cyc in range(6):
        q = rr.next_question(cyc)
        assert q == rr.FALLBACK_QUESTIONS[cyc % 6]


def test_autoresearch_wraps_round_robin(monkeypatch):
    """`autoresearch._next_round_robin` should now delegate to the new
    module — preserving the public surface used by tests + scripts."""
    from brain import autoresearch
    monkeypatch.setattr(rr, "next_question", lambda n: f"sentinel-{n}")
    assert autoresearch._next_round_robin(7) == "sentinel-7"
