"""auto_clean anchor-token extraction — vault-aware stopwords."""
from __future__ import annotations

import pytest

from brain import auto_clean, db


@pytest.fixture
def tmp_brain_db(tmp_path, monkeypatch):
    """Fresh brain dir + empty SQLite, isolated from the host vault."""
    brain_dir = tmp_path / "brain"
    brain_dir.mkdir()
    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", brain_dir)
    monkeypatch.setattr(db, "DB_PATH", brain_dir / ".brain.db")
    return brain_dir


def _seed_entities(names: list[str]) -> None:
    with db.connect() as conn:
        for i, n in enumerate(names, start=1):
            conn.execute(
                "INSERT INTO entities (path, type, slug, name, summary) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"entities/insights/e{i}.md", "insights", f"e{i}", n, ""),
            )


def test_extract_anchor_tokens_strips_dates_and_generic_stopwords(tmp_brain_db):
    """Generic English stopwords + ISO date prefixes are filtered."""
    out = auto_clean._extract_anchor_tokens("2026-04-11 the brain is alive")
    # "the", "is" stripped; "brain" / "alive" remain (no vault-specific
    # carve-out by default — that's caller-supplied).
    assert "the" not in out
    assert "is" not in out
    assert len(out) <= 2


def test_extract_anchor_tokens_no_longer_hardcodes_son_brain_project(tmp_brain_db):
    """Pre-fix the function had `{"son", "brain", "project"}` hardcoded
    as stopwords — a leak from the original maintainer's vault. After
    the fix, these are NOT generic stopwords; callers can opt them in
    via `extra_stopwords` if their vault distribution warrants it.

    Concretely: a fresh user's name "son" is not magically stopworded
    just because it appeared in son's hardcoded list. Same for "brain"
    and "project" — those happen to be common in son's vault but there's
    no a-priori reason they should be globally suppressed.
    """
    out = auto_clean._extract_anchor_tokens("son notes about brain project")
    # Without an extra_stopwords override, all four tokens are valid
    # candidates; the function returns the first 2.
    assert out == ["son", "notes"]


def test_extract_anchor_tokens_respects_extra_stopwords(tmp_brain_db):
    """When the caller passes a vault-specific stopword set,
    high-frequency tokens are correctly skipped."""
    out = auto_clean._extract_anchor_tokens(
        "son notes about brain project",
        extra_stopwords={"son", "brain", "project"},
    )
    # "son", "brain", "project" filtered; "notes" + nothing else (3+ chars).
    assert out == ["notes", "about"]


def test_vault_common_tokens_empty_on_sparse_vault(tmp_brain_db):
    """Vaults with fewer than 20 entities are too sparse for the
    frequency heuristic — return empty rather than over-aggressively
    flag rare tokens as common."""
    _seed_entities(["alpha foo", "beta foo", "gamma foo"])
    common = auto_clean._vault_common_tokens()
    assert common == set()


def test_vault_common_tokens_finds_dominant_tokens(tmp_brain_db):
    """Once the entity table reaches threshold size, tokens that
    appear in ≥10% of entity names get returned as common.

    For son's vault, "son" / "brain" / "project" would appear here and
    the caller would inject them as stopwords — same outcome as the
    pre-fix hardcoded list, but computed from data instead of pinned
    to one user's vocabulary.
    """
    # 30 entities, all containing "vulcan" — ensures vulcan is "common".
    names = [f"vulcan {ch} insight {i}" for i, ch in enumerate("abcdefghijklmnopqrstuvwxyz0123")]
    _seed_entities(names)
    common = auto_clean._vault_common_tokens()
    assert "vulcan" in common
    # "insight" also appears in every name → also common.
    assert "insight" in common
    # First-name-only tokens (a, b, c) are 1-char and stripped by the
    # 3+ char regex, so they don't appear in `common`.
    assert "a" not in common


def test_vault_common_tokens_counts_document_frequency_not_term_frequency(
    tmp_brain_db,
):
    """A token appearing twice in one entity name is one document, not
    two — so a token in 1 of 25 entities can't accidentally "dominate"
    just because it repeats."""
    # 25 entities; "alpha" appears once in 24 of them but TWICE in one.
    names = [f"alpha note {i}" for i in range(24)]
    names.append("alpha alpha note 25")
    _seed_entities(names)
    common = auto_clean._vault_common_tokens()
    # alpha is in 25/25 = 100% of names → common. note is in 25/25 too.
    assert "alpha" in common
    assert "note" in common
