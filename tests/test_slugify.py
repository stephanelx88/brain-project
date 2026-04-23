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


def test_slugify_vietnamese_ascii_folds():
    # Vietnamese with tone/diacritic marks round-trips via NFKD ASCII fold.
    assert slugify("Nguyễn Sơn") == "nguyen-son"
    assert slugify("Việt Nam") == "viet-nam"


def test_slugify_cjk_falls_back_to_unicode():
    # Pure non-Latin text: ASCII fold returns "", so previously the
    # slug was empty and validate_slug raised ValueError mid-extraction.
    # Now it keeps Unicode word chars so the entity is findable.
    slug = slugify("田中さん")
    assert slug != ""
    assert slug == slugify(slug)  # idempotent → passes validate_slug
    validate_slug(slug)


def test_slugify_mixed_keeps_ascii_when_available():
    # Vietnamese (ASCII-foldable) + CJK — ASCII-fold produces a
    # non-empty slug, so we prefer that over the Unicode form.
    assert slugify("Sơn 田中") == "son"
