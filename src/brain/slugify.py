"""Convert names and titles to filesystem-safe slugs."""

import re
import unicodedata


def _clean(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


def slugify(text: str) -> str:
    """Convert text to a lowercase, hyphen-separated slug.

    'Sarah Chen' -> 'sarah-chen'
    'NovaMind Payment Service' -> 'novamind-payment-service'
    '2026-04-09 Auth Strategy' -> '2026-04-09-auth-strategy'
    'Nguyễn Sơn' -> 'nguyen-son'        (Vietnamese → ASCII-fold)
    '田中さん' -> '田中さん'                 (pure CJK → keep Unicode)

    The ASCII-fold path is preferred because filesystem tooling /
    grep / URL copies all handle ASCII better. But for pure non-Latin
    names (Chinese, Japanese, Korean) ASCII-fold returns the empty
    string, which used to raise ValueError from validate_slug and
    crash extraction. We fall back to a Unicode-preserving slug so
    multilingual entity names round-trip instead of hard-failing.
    """
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    ascii_slug = _clean(ascii_text)
    if ascii_slug:
        return ascii_slug
    return _clean(text)


def validate_slug(slug: str) -> str:
    """Validate a slug is filesystem-safe. Raises ValueError if invalid."""
    if not slug or slug != slugify(slug) or " " in slug:
        raise ValueError(f"Invalid slug: {slug!r}")
    return slug
