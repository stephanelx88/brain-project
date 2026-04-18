"""Tests for slug generation and validation."""

from brain.slugify import slugify, validate_slug
import pytest


def test_slugify_basic():
    assert slugify("Sarah Chen") == "sarah-chen"


def test_slugify_with_date():
    assert slugify("2026-04-09 Auth Strategy") == "2026-04-09-auth-strategy"


def test_slugify_camelcase():
    assert slugify("NovaMind Payment Service") == "novamind-payment-service"


def test_slugify_special_chars():
    assert slugify("Bug #216 — Pump Issue") == "bug-216-pump-issue"


def test_slugify_strips_leading_trailing_hyphens():
    assert slugify("  --test-- ") == "test"


def test_validate_slug_accepts_valid():
    assert validate_slug("sarah-chen") == "sarah-chen"


def test_validate_slug_rejects_spaces():
    with pytest.raises(ValueError):
        validate_slug("sarah chen")


def test_validate_slug_rejects_uppercase():
    with pytest.raises(ValueError):
        validate_slug("Sarah-Chen")


def test_validate_slug_rejects_empty():
    with pytest.raises(ValueError):
        validate_slug("")
