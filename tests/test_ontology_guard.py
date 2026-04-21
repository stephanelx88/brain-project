"""Tests for ontology_guard — type whitelist validation."""
import os
import pytest


@pytest.fixture(autouse=True)
def set_brain_dir(tmp_brain, monkeypatch):
    monkeypatch.setenv("BRAIN_DIR", str(tmp_brain))
    # reload config so SEED_TYPES / ENTITY_TYPES reflect tmp_brain
    import importlib, brain.config as cfg
    importlib.reload(cfg)
    import brain.ontology_guard as og
    importlib.reload(og)
    yield
    importlib.reload(cfg)
    importlib.reload(og)


def _guard():
    from brain.ontology_guard import validate_entity
    return validate_entity


def test_allowed_type_passes():
    ok, reason = _guard()({'type': 'people', 'name': 'Son'})
    assert ok
    assert reason == ''


def test_all_configured_types_pass():
    for t in ('people', 'projects', 'domains', 'decisions', 'issues', 'insights'):
        ok, _ = _guard()({'type': t, 'name': 'Test entity'})
        assert ok, f"type '{t}' should be allowed"


def test_unknown_type_rejected():
    ok, reason = _guard()({'type': 'slippers', 'name': 'Son dep'})
    assert not ok
    assert 'slippers' in reason


def test_invented_type_rejected():
    ok, reason = _guard()({'type': 'bedroom-objects', 'name': 'Slippers'})
    assert not ok
    assert 'bedroom-objects' in reason


def test_missing_type_rejected():
    ok, reason = _guard()({'type': '', 'name': 'Something'})
    assert not ok
    assert 'missing entity type' in reason


def test_missing_name_rejected():
    ok, reason = _guard()({'type': 'people', 'name': ''})
    assert not ok
    assert 'missing entity name' in reason


def test_on_disk_type_allowed(tmp_brain):
    """Types already on disk (discovered) are allowed even if not in seed list."""
    (tmp_brain / 'entities' / 'techniques').mkdir(exist_ok=True)
    import importlib, brain.config as cfg, brain.ontology_guard as og
    importlib.reload(cfg)
    importlib.reload(og)
    ok, _ = og.validate_entity({'type': 'techniques', 'name': 'Some technique'})
    assert ok
