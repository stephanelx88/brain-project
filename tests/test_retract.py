"""Tests for brain.retract — user-driven fact retraction and correction."""
import os
import pytest


@pytest.fixture(autouse=True)
def set_brain_dir(tmp_brain, monkeypatch):
    monkeypatch.setenv("BRAIN_DIR", str(tmp_brain))
    import importlib
    import brain.config as cfg
    import brain.db as db_mod
    importlib.reload(cfg)
    importlib.reload(db_mod)
    yield
    importlib.reload(cfg)
    importlib.reload(db_mod)


def _make_entity(tmp_brain, entity_type, name, facts):
    from brain.entities import create_entity
    body = "## Key Facts\n" + "\n".join(
        f"- {f} (source: test-session, 2026-04-22)" for f in facts
    ) + "\n"
    path = create_entity(entity_type, name, body=body)
    # Upsert into DB so retract can find the file
    from brain import db
    db.upsert_entity_from_file(path)
    return path


def test_retract_fact_basic(tmp_brain):
    _make_entity(tmp_brain, "people", "Son",
                 ["currently in Long Xuyên", "works at Aitomatic"])
    from brain.retract import retract_fact
    retracted = retract_fact("people", "Son", "long xuyên")
    assert "Long Xuyên" in retracted

    # Markdown has strikethrough
    from brain.entities import entity_path
    text = entity_path("people", "son").read_text()
    assert "~~" in text
    assert "long xuyên" in text.lower()

    # Other fact untouched
    assert "works at Aitomatic" in text


def test_retract_is_case_insensitive(tmp_brain):
    _make_entity(tmp_brain, "people", "Son", ["Currently in HCM City"])
    from brain.retract import retract_fact
    retracted = retract_fact("people", "Son", "hcm city")
    assert "HCM City" in retracted


def test_retract_already_superseded_is_noop(tmp_brain):
    """A fact already struck-through is not matched again."""
    from brain.entities import entity_path, create_entity
    body = "## Key Facts\n- ~~old fact~~ [retracted 2026-04-21: user]\n"
    create_entity("people", "Alice", body=body)
    from brain.retract import retract_fact
    with pytest.raises(ValueError, match="no matching fact"):
        retract_fact("people", "Alice", "old fact")


def test_retract_entity_not_found(tmp_brain):
    from brain.retract import retract_fact
    with pytest.raises(ValueError, match="entity not found"):
        retract_fact("people", "Nobody", "some fact")


def test_retract_fact_not_found(tmp_brain):
    _make_entity(tmp_brain, "people", "Son", ["works at Aitomatic"])
    from brain.retract import retract_fact
    with pytest.raises(ValueError, match="no matching fact"):
        retract_fact("people", "Son", "slippers in bedroom")


def test_correct_fact(tmp_brain):
    _make_entity(tmp_brain, "people", "Son",
                 ["currently in Long Xuyên", "works at Aitomatic"])
    from brain.retract import correct_fact
    result = correct_fact(
        "people", "Son",
        wrong_fact="long xuyên",
        correct_fact_text="currently in Cần Thơ",
    )
    assert "Long Xuyên" in result["retracted"]
    assert result["appended"] == "currently in Cần Thơ"

    from brain.entities import entity_path
    text = entity_path("people", "son").read_text()
    assert "~~" in text
    assert "currently in Cần Thơ" in text
    assert "works at Aitomatic" in text


def test_retract_only_first_match(tmp_brain):
    """If two facts match, only the first is retracted."""
    _make_entity(tmp_brain, "insights", "Brain location",
                 ["location A is good", "location A is bad"])
    from brain.retract import retract_fact
    retract_fact("insights", "Brain location", "location a")
    from brain.entities import entity_path
    text = entity_path("insights", "brain-location").read_text()
    strikes = text.count("~~")
    # Only first match wrapped (opening + closing = 2 tildes per strike)
    assert strikes == 2
