"""Convert names and titles to filesystem-safe slugs."""

import re
import unicodedata


def slugify(text: str) -> str:
    """Convert text to a lowercase, hyphen-separated slug.

    'Sarah Chen' -> 'sarah-chen'
    'NovaMind Payment Service' -> 'novamind-payment-service'
    '2026-04-09 Auth Strategy' -> '2026-04-09-auth-strategy'
    """
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


def validate_slug(slug: str) -> str:
    """Validate a slug is filesystem-safe. Raises ValueError if invalid."""
    if not slug or slug != slugify(slug) or " " in slug:
        raise ValueError(f"Invalid slug: {slug!r}")
    return slug
