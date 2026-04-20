"""Tests for `brain.promote` — the playground → entities promotion path.

The production code deliberately bypasses LLMs; these tests can stay
unit-tier (no semantic index, no subprocess) and just exercise the
rule matcher, frontmatter rewrite, and timeline audit.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

import brain.config as config
import brain.promote as promote


def _now_iso(dt: datetime | None = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def vault(tmp_path, monkeypatch):
    brain = tmp_path / "brain"
    (brain / "playground" / "insights").mkdir(parents=True)
    (brain / "playground" / "hypotheses").mkdir(parents=True)
    (brain / "playground" / "articles").mkdir(parents=True)
    (brain / "entities" / "insights").mkdir(parents=True)
    (brain / "timeline").mkdir()
    monkeypatch.setattr(config, "BRAIN_DIR", brain)
    monkeypatch.setattr(config, "ENTITIES_DIR", brain / "entities")
    monkeypatch.setattr(config, "TIMELINE_DIR", brain / "timeline")
    # ensure_dirs is called inside main(); stub it out so it doesn't try
    # to mkdir the hard-coded ~/.brain subtree.
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)
    # Don't hit the real semantic index during apply() tests.
    monkeypatch.setattr(promote, "_reingest_safely", lambda: None)
    return brain


def _write_playground_item(
    vault_dir: Path,
    sub: str,
    name: str,
    *,
    title: str = "A Title",
    body: str = "some body",
    confidence: str = "high",
    refs: list[str] | None = None,
    created_at: str | None = None,
    extra_front: dict | None = None,
) -> Path:
    refs = refs if refs is not None else ["entities/insights/a.md",
                                          "entities/insights/b.md"]
    created_at = created_at or _now_iso()
    front = [
        "---",
        f"type: playground-{sub.rstrip('s') if sub == 'insights' else sub.rstrip('s')}",
        f"created_at: {created_at}",
        "cycle: 1",
        f"confidence: {confidence}",
        f"refs: {json.dumps(refs)}",
    ]
    for k, v in (extra_front or {}).items():
        front.append(f"{k}: {v}")
    front.append("---")
    path = vault_dir / "playground" / sub / name
    path.write_text("\n".join(front) + f"\n\n# {title}\n\n{body}\n")
    return path


# ---------- rule matcher --------------------------------------------------


def test_dry_run_writes_nothing(vault):
    _write_playground_item(vault, "insights", "0001-foo.md")
    report = promote.run(apply=False)
    assert len(report.promoted) == 1
    assert not (vault / "entities" / "insights").glob("*.md").__iter__().__next__().exists() \
        if list((vault / "entities" / "insights").glob("*.md")) else True
    assert list((vault / "entities" / "insights").glob("*.md")) == []


def test_apply_writes_entity(vault):
    _write_playground_item(vault, "insights", "0001-foo.md",
                           title="The Foo Principle",
                           body="payload body here")
    report = promote.run(apply=True)
    assert len(report.promoted) == 1
    out = list((vault / "entities" / "insights").glob("*.md"))
    assert len(out) == 1
    text = out[0].read_text()
    assert "type: insight" in text
    assert "name: The Foo Principle" in text
    assert "promoted_from: playground/insights/0001-foo.md" in text
    assert "payload body here" in text
    # H1 gets re-rendered, not doubled
    assert text.count("# The Foo Principle") == 1


def test_low_confidence_skipped(vault):
    _write_playground_item(vault, "insights", "0001-low.md", confidence="low")
    _write_playground_item(vault, "insights", "0002-med.md", confidence="medium")
    _write_playground_item(vault, "insights", "0003-high.md", confidence="high")
    report = promote.run(apply=False)
    titles = [p["src"] for p in report.promoted]
    assert "playground/insights/0003-high.md" in titles
    assert len(report.promoted) == 1
    reasons = [s["reason"] for s in report.skipped]
    assert any("confidence=low" in r for r in reasons)
    assert any("confidence=medium" in r for r in reasons)


def test_too_few_refs_skipped(vault):
    _write_playground_item(vault, "insights", "0001-solo.md", refs=["entities/a.md"])
    _write_playground_item(vault, "insights", "0002-empty.md", refs=[])
    report = promote.run(apply=False)
    assert report.promoted == []
    assert all("ref" in s["reason"] for s in report.skipped)


def test_old_items_skipped(vault):
    old_ts = _now_iso(datetime.now(timezone.utc) - timedelta(days=30))
    _write_playground_item(vault, "insights", "0001-old.md", created_at=old_ts)
    report = promote.run(apply=False)
    assert report.promoted == []
    assert "age" in report.skipped[0]["reason"]


def test_already_promoted_skipped(vault):
    _write_playground_item(vault, "insights", "0001-done.md",
                           extra_front={"status": "promoted"})
    report = promote.run(apply=False)
    assert report.promoted == []
    assert "already promoted" in report.skipped[0]["reason"]


def test_hypothesis_promotes_as_insight(vault):
    """A confirmed hypothesis is an insight — target folder is entities/insights/"""
    _write_playground_item(vault, "hypotheses", "0001-h.md",
                           title="Testable Claim")
    report = promote.run(apply=True)
    assert len(report.promoted) == 1
    out_file = vault / "entities" / "insights" / "testable-claim.md"
    assert out_file.exists()
    assert "type: insight" in out_file.read_text()


def test_articles_not_promoted(vault):
    """Articles are narrative — only insights and hypotheses promote."""
    art_dir = vault / "playground" / "articles"
    art = art_dir / "0001-essay.md"
    art.write_text(
        "---\ntype: playground-article\ncreated_at: " + _now_iso() +
        '\ncycle: 1\nconfidence: high\nrefs: ["a", "b", "c"]\n---\n\n# Essay\n\nbody\n'
    )
    report = promote.run(apply=False)
    # Articles aren't in PROMOTE_MAP, so they're never scanned as candidates
    assert all("articles" not in c.path.parts for c in report.candidates)


# ---------- annotation ---------------------------------------------------


def test_source_annotated_after_apply(vault):
    src = _write_playground_item(vault, "insights", "0001-foo.md",
                                 title="Foo")
    promote.run(apply=True)
    annotated = src.read_text()
    assert "status: promoted" in annotated
    assert "promoted_to: entities/insights/foo.md" in annotated
    assert "promoted_at:" in annotated
    # Original body preserved
    assert "# Foo" in annotated


def test_annotation_is_idempotent(vault):
    """Re-running promote (which will skip on status:promoted) must not
    corrupt the frontmatter or re-annotate."""
    src = _write_playground_item(vault, "insights", "0001-foo.md")
    promote.run(apply=True)
    first = src.read_text()
    promote.run(apply=True)
    second = src.read_text()
    # Frontmatter keys aren't duplicated on a second apply pass
    assert first.count("status: promoted") == 1
    assert second.count("status: promoted") == 1


# ---------- collision ----------------------------------------------------


def test_existing_entity_gets_promoted_suffix(vault):
    """We never overwrite a hand-written entity — add -promoted suffix."""
    (vault / "entities" / "insights" / "the-foo.md").write_text(
        "---\ntype: insight\nname: The Foo\n---\n\n# The Foo\n\nhand-written\n"
    )
    _write_playground_item(vault, "insights", "0001-foo.md", title="The Foo")
    report = promote.run(apply=True)
    assert len(report.promoted) == 1
    expected = vault / "entities" / "insights" / "the-foo-promoted.md"
    assert expected.exists()
    # Original hand-written file untouched
    assert "hand-written" in (vault / "entities" / "insights" / "the-foo.md").read_text()


# ---------- timeline audit ------------------------------------------------


def test_timeline_entry_written_on_apply(vault):
    _write_playground_item(vault, "insights", "0001-foo.md", title="Foo")
    _write_playground_item(vault, "insights", "0002-low.md", confidence="low")
    report = promote.run(apply=True)
    assert report.timeline_path is not None
    assert report.timeline_path.exists()
    text = report.timeline_path.read_text()
    assert "# Promotion" in text
    assert "Foo" in text
    assert "confidence=low" in text


def test_no_timeline_on_dry_run(vault):
    _write_playground_item(vault, "insights", "0001-foo.md")
    report = promote.run(apply=False)
    assert report.timeline_path is None
    assert list((vault / "timeline").glob("*.md")) == []


# ---------- limit knob ----------------------------------------------------


def test_limit_caps_promotions(vault):
    for i in range(5):
        _write_playground_item(vault, "insights", f"{i:04d}-x.md",
                               title=f"Item {i}")
    report = promote.run(apply=True, limit=2)
    assert len(report.promoted) == 2
    assert len(list((vault / "entities" / "insights").glob("*.md"))) == 2


# ---------- CLI ----------------------------------------------------------


def test_main_dry_run_exits_zero(vault, capsys):
    _write_playground_item(vault, "insights", "0001-foo.md")
    rc = promote.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "promote:" in out
    # No entity file written
    assert list((vault / "entities" / "insights").glob("*.md")) == []


def test_main_apply_writes(vault, capsys):
    _write_playground_item(vault, "insights", "0001-foo.md", title="Bar")
    rc = promote.main(["--apply"])
    assert rc == 0
    assert (vault / "entities" / "insights" / "bar.md").exists()


def test_main_json_output(vault, capsys):
    _write_playground_item(vault, "insights", "0001-foo.md")
    rc = promote.main(["--json"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert "promoted" in parsed
    assert "skipped" in parsed
    assert parsed["dry_run"] is True


def test_main_quiet_silent_when_nothing_promoted(vault, capsys):
    # empty vault → nothing to do
    rc = promote.main(["--quiet"])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_main_quiet_still_prints_when_promoting(vault, capsys):
    """--quiet is only meant to mute no-ops; real work should still show
    up in the log so cron users see something happened."""
    _write_playground_item(vault, "insights", "0001-foo.md", title="Bar")
    rc = promote.main(["--apply", "--quiet"])
    assert rc == 0
    assert "promote:" in capsys.readouterr().out


# ---------- frontmatter resilience ---------------------------------------


def test_refs_as_string_not_list_tolerated(vault):
    """Hand-edited playground files might have refs as a plain string
    rather than JSON — we shouldn't crash, just treat as zero refs."""
    path = vault / "playground" / "insights" / "0001-weird.md"
    path.write_text(
        "---\ntype: playground-insight\n"
        f"created_at: {_now_iso()}\ncycle: 1\nconfidence: high\n"
        "refs: not-a-list\n---\n\n# Weird\n\nbody\n"
    )
    report = promote.run(apply=False)
    assert report.promoted == []
    # Must be skipped for ref-count, not crashed
    assert report.skipped and "ref" in report.skipped[0]["reason"]


# ---------- Key Facts synthesis ------------------------------------------


def test_key_facts_section_rendered(vault):
    """Promoted entities must carry a `## Key Facts` block with sourced
    bullets — otherwise `db._facts_from_body()` has nothing to index and
    fact-search stays blind to the promotion."""
    _write_playground_item(
        vault, "insights", "0001-foo.md",
        title="The Foo Principle",
        body=(
            "## Observation\n\n"
            "Autoresearch cycle outputs never reach the entities index. "
            "This breaks the feedback loop in a measurable way.\n\n"
            "## Testable Claim\n\n"
            "Enabling promotion raises coverage by at least five points.\n"
        ),
    )
    promote.run(apply=True)
    text = (vault / "entities" / "insights" / "the-foo-principle.md").read_text()
    assert "## Key Facts" in text
    # Bullet format must match `db._SOURCE_RE` so facts get extracted
    assert re.search(
        r"^- .+ \(source: promoted:0001-foo, \d{4}-\d{2}-\d{2}\)",
        text, re.MULTILINE,
    )
    # At least one synthesized fact should name the subject
    assert "feedback loop" in text or "coverage" in text


def test_key_facts_skips_testable_via_metadata(vault):
    """Hypothesis bullets like `- testable_via: ...` are scaffolding, not
    facts — they shouldn't land in Key Facts or they'll dilute signal."""
    _write_playground_item(
        vault, "hypotheses", "0001-h.md",
        title="Interesting Claim",
        body=(
            "Some hypothesis body with enough prose to survive truncation.\n\n"
            "- testable_via: run the experiment\n"
            "- status: unverified\n"
        ),
    )
    promote.run(apply=True)
    text = (vault / "entities" / "insights" / "interesting-claim.md").read_text()
    assert "testable_via" not in text.split("## Key Facts")[1].split("\n\n")[0]


def test_key_facts_falls_back_to_title_on_empty_body(vault):
    """Empty bodies still need a Key Facts bullet — otherwise the entity
    is a dead row in the database."""
    _write_playground_item(
        vault, "insights", "0001-bare.md",
        title="Bare Bones", body="",
    )
    promote.run(apply=True)
    text = (vault / "entities" / "insights" / "bare-bones.md").read_text()
    assert "## Key Facts" in text
    assert "Bare Bones" in text.split("## Key Facts")[1]


def test_extract_fact_paragraphs_respects_max_n():
    body = "\n\n".join(f"Sentence {i}. More text." for i in range(10))
    out = promote._extract_fact_paragraphs(body, max_n=3)
    assert len(out) == 3


# ---------- rerender ------------------------------------------------------


def test_rerender_updates_legacy_promotions(vault):
    """Already-promoted entities get regenerated against the current
    render (e.g. to pick up a new Key Facts section)."""
    src = _write_playground_item(
        vault, "insights", "0001-foo.md",
        title="Foo", body="The brain promotes insights to entities.",
    )
    promote.run(apply=True)
    out = vault / "entities" / "insights" / "foo.md"
    # Simulate a legacy render that forgot to synthesize Key Facts
    old = out.read_text()
    trimmed = re.sub(r"## Key Facts.*?(?=\n[#]|\Z)", "", old, count=1, flags=re.DOTALL)
    out.write_text(trimmed)
    assert "## Key Facts" not in out.read_text()

    report = promote.rerender(apply=True)
    assert len(report.promoted) == 1
    refreshed = out.read_text()
    assert "## Key Facts" in refreshed
    # Playground source is NOT re-annotated — status was already set
    assert src.read_text().count("status: promoted") == 1


def test_rerender_dry_run_touches_nothing(vault):
    _write_playground_item(vault, "insights", "0001-foo.md", title="Foo")
    promote.run(apply=True)
    out = vault / "entities" / "insights" / "foo.md"
    original = out.read_text()
    # Simulate legacy: strip Key Facts to ensure dry-run would otherwise change something
    out.write_text(re.sub(r"## Key Facts.*?(?=\n[#]|\Z)", "", original,
                          count=1, flags=re.DOTALL))
    snapshot = out.read_text()
    report = promote.rerender(apply=False)
    assert len(report.promoted) == 1  # flagged for rerender
    assert out.read_text() == snapshot


def test_rerender_reports_missing_playground_source(vault):
    """If someone deletes the playground source, rerender flags it
    rather than crashing — those entities will just keep their old body."""
    src = _write_playground_item(vault, "insights", "0001-foo.md", title="Foo")
    promote.run(apply=True)
    src.unlink()
    report = promote.rerender(apply=True)
    assert any("missing" in s["reason"] for s in report.skipped)


def test_main_rerender_flag(vault, capsys):
    _write_playground_item(vault, "insights", "0001-foo.md", title="Foo")
    promote.main(["--apply"])
    rc = promote.main(["--rerender", "--apply"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "promote:" in out


# ---------- frontmatter resilience ---------------------------------------


def test_missing_created_at_skipped_cleanly(vault):
    path = vault / "playground" / "insights" / "0001-noTs.md"
    path.write_text(
        "---\ntype: playground-insight\ncycle: 1\nconfidence: high\n"
        'refs: ["a","b"]\n---\n\n# NoTs\n\nbody\n'
    )
    report = promote.run(apply=False)
    assert report.promoted == []
    assert "created_at" in report.skipped[0]["reason"]
