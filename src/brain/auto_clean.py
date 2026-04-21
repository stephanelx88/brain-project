"""Auto-clean rules engine for the brain audit pipeline.

Reads ~/.brain/auto_clean.yaml and deletes entity files matching a rule
before the audit surface presents items for manual review.

Public API:
  load_rules(rules_file=None) -> list[dict]       # parse rules from vault
  apply_rules(dry_run=False) -> list[Path]         # delete matching files
  update_rules(decisions) -> int                   # learn from audit session
  main() -> int                                    # CLI: `brain auto-clean`

`update_rules` is called by `brain audit` at the end of an interactive
session. It receives the list of (path, action) pairs the user chose
("delete" vs "keep"/"skip") and derives new rule patterns from the
deleted items, merging them into auto_clean.yaml so future audits clean
those shapes automatically.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

import brain.config as config
from brain.config import BRAIN_DIR, ENTITY_TYPES

AUTO_CLEAN_FILE = BRAIN_DIR / "auto_clean.yaml"

_SOURCE_COUNT_RE = re.compile(r"source_count:\s*(\d+)")
_FIRST_SEEN_RE = re.compile(r"first_seen:\s*(\d{4}-\d{2}-\d{2})")
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_NAME_FM_RE = re.compile(r"^name:\s*(.+)", re.MULTILINE)


# ---------------------------------------------------------------------------
# Rule loading
# ---------------------------------------------------------------------------

def _rules_file() -> Path:
    return AUTO_CLEAN_FILE


def load_rules(rules_file: Path | None = None) -> list[dict]:
    """Parse auto_clean.yaml. Returns [] on any error."""
    path = rules_file or _rules_file()
    if not path.exists():
        return []
    try:
        import yaml
        data = yaml.safe_load(path.read_text())
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    rules = data.get("rules", [])
    if not isinstance(rules, list):
        return []
    return [r for r in rules if isinstance(r, dict)]


# ---------------------------------------------------------------------------
# Entity introspection helpers
# ---------------------------------------------------------------------------

def _entity_name(text: str) -> str:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return ""
    nm = _NAME_FM_RE.search(m.group(1))
    return nm.group(1).strip() if nm else ""


def _entity_source_count(text: str) -> int | None:
    m = _SOURCE_COUNT_RE.search(text[:600])
    return int(m.group(1)) if m else None


def _entity_age_days(text: str, today: date | None = None) -> int | None:
    m = _FIRST_SEEN_RE.search(text[:600])
    if not m:
        return None
    try:
        first = date.fromisoformat(m.group(1))
    except ValueError:
        return None
    return ((today or date.today()) - first).days


# ---------------------------------------------------------------------------
# Rule matching
# ---------------------------------------------------------------------------

def _matches_rule(path: Path, text: str, rule: dict,
                  today: date | None = None) -> bool:
    """True when the entity satisfies every condition in `rule`."""
    match = rule.get("match", {})
    if not isinstance(match, dict):
        return False

    types = match.get("types")
    if types and path.parent.name not in types:
        return False

    required_sc = match.get("source_count")
    if required_sc is not None:
        if _entity_source_count(text) != required_sc:
            return False

    min_age = match.get("min_age_days")
    if min_age is not None:
        age = _entity_age_days(text, today)
        if age is None or age < min_age:
            return False

    patterns = match.get("name_patterns", [])
    if patterns:
        name = _entity_name(text)
        if not any(re.search(p, name) for p in patterns):
            return False

    return True


# ---------------------------------------------------------------------------
# Apply rules (the pre-audit clean pass)
# ---------------------------------------------------------------------------

def apply_rules(
    dry_run: bool = False,
    today: date | None = None,
    rules_file: Path | None = None,
) -> list[Path]:
    """Delete entity files matching any rule. Returns list of deleted paths.

    Designed to run silently as a pre-step in `brain audit`. Errors on
    individual files are swallowed so one bad file can't abort the pass.
    """
    rules = load_rules(rules_file)
    if not rules:
        return []

    deleted: list[Path] = []
    for type_dir in ENTITY_TYPES.values():
        if not type_dir.exists():
            continue
        for f in sorted(type_dir.glob("*.md")):
            if f.name.startswith("_"):
                continue
            try:
                text = f.read_text()
            except OSError:
                continue
            for rule in rules:
                if rule.get("action") != "delete":
                    continue
                if _matches_rule(f, text, rule, today=today):
                    if not dry_run:
                        try:
                            f.unlink()
                        except OSError:
                            continue
                    deleted.append(f)
                    break  # first match wins
    return deleted


# ---------------------------------------------------------------------------
# Rule learning — update_rules()
# ---------------------------------------------------------------------------

# These keywords in an entity name reliably signal "historical record, no
# future value". We build new patterns from them rather than a literal slug
# match so the rule generalises to similar future entities.
_MILESTONE_KEYWORDS = [
    "complete", "completed", "operational", "done", "shipped", "launched",
]
_CHANGELOG_KEYWORDS = [
    "fixed", "retrospective", "code review", "code-review",
    "findings", "bugs fixed", "issues fixed",
]
_METRIC_KEYWORDS = [
    "% reduction", "% token", "percent reduction", "token reduction",
    "% faster", "% improvement",
]
_GOAL_KEYWORDS = [
    "feedback loop", "accelerat",
]

_KEYWORD_GROUPS: list[tuple[str, list[str]]] = [
    ("milestone_announcements", _MILESTONE_KEYWORDS),
    ("changelog_entries", _CHANGELOG_KEYWORDS),
    ("stale_metric_estimates", _METRIC_KEYWORDS),
    ("feedback_loop_goal_statements", _GOAL_KEYWORDS),
]


def _classify_name(name: str) -> str | None:
    """Return the rule name that best fits `name`, or None."""
    lower = name.lower()
    for rule_name, keywords in _KEYWORD_GROUPS:
        if any(kw in lower for kw in keywords):
            return rule_name
    return None


def _make_escaped_pattern(token: str) -> str:
    """Turn a plain keyword token into a case-insensitive regex pattern."""
    escaped = re.escape(token)
    return f"(?i){escaped}"


def update_rules(
    deleted_paths: list[Path],
    rules_file: Path | None = None,
) -> int:
    """Learn from an audit session and merge new patterns into auto_clean.yaml.

    Called at the end of `brain audit --walk` with the list of paths the
    user chose to delete. For each deleted entity:
      1. Classify its name into an existing rule bucket (or skip if novel).
      2. Extract distinctive name tokens not already covered by any pattern.
      3. Append the new patterns to that rule's `name_patterns` list.

    Returns the number of new patterns added.
    """
    if not deleted_paths:
        return 0

    path = rules_file or _rules_file()
    if not path.exists():
        return 0

    try:
        import yaml
        raw = path.read_text()
        data = yaml.safe_load(raw)
    except Exception:
        return 0

    if not isinstance(data, dict):
        return 0
    rules: list[dict] = data.get("rules", [])
    if not isinstance(rules, list):
        return 0

    # Index existing patterns so we don't add duplicates
    existing: dict[str, set[str]] = {}
    for r in rules:
        rname = r.get("name", "")
        patterns = r.get("match", {}).get("name_patterns", []) or []
        existing[rname] = set(patterns)

    added = 0
    for p in deleted_paths:
        try:
            text = p.read_text()
        except OSError:
            # File was just deleted; read from path stem as fallback
            text = ""
        name = _entity_name(text) or p.stem.replace("-", " ")
        rule_name = _classify_name(name)
        if rule_name is None:
            continue  # novel pattern; don't guess

        # Find the rule dict to mutate
        rule_dict = next((r for r in rules if r.get("name") == rule_name), None)
        if rule_dict is None:
            continue

        # Extract a 2-3 word anchor from the name that distinguishes this item
        tokens = _extract_anchor_tokens(name)
        for token in tokens:
            pattern = _make_escaped_pattern(token)
            if pattern not in existing.get(rule_name, set()):
                match = rule_dict.setdefault("match", {})
                patterns_list = match.setdefault("name_patterns", [])
                patterns_list.append(pattern)
                existing.setdefault(rule_name, set()).add(pattern)
                added += 1

    if added:
        try:
            import yaml
            new_raw = yaml.dump(data, allow_unicode=True, sort_keys=False,
                                default_flow_style=False)
            from brain.io import atomic_write_text
            atomic_write_text(path, new_raw)
        except Exception:
            return 0

    return added


def _extract_anchor_tokens(name: str) -> list[str]:
    """Return 1-2 short tokens from `name` likely to recur in similar entities.

    Heuristic: skip stop words and very short tokens; prefer tokens that
    are specific enough to be discriminating but not so specific they only
    match one entity (e.g. skip version numbers like "2026-04-11").
    """
    _STOP = {
        "a", "an", "the", "and", "or", "of", "for", "in", "on", "at",
        "to", "with", "is", "are", "was", "were", "by", "as", "–", "-",
        "from", "that", "this", "be", "been", "both", "all", "has", "have",
        "son", "brain", "project",  # too common in this vault
    }
    # Strip date prefixes like "2026-04-11"
    name = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "", name).strip()
    # Tokenise on word boundaries
    words = [w.lower() for w in re.findall(r"[a-zA-Z]{3,}", name)]
    candidates = [w for w in words if w not in _STOP]
    # Return first 2 non-stop words — enough to be specific, few enough to generalise
    return candidates[:2]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """`brain auto-clean` — apply rules and report."""
    import argparse
    p = argparse.ArgumentParser(
        description="Apply auto-clean rules to brain entities.")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be deleted without deleting.")
    p.add_argument("--rules-file", metavar="PATH",
                   help="Override rules file (default: ~/.brain/auto_clean.yaml).")
    args = p.parse_args(argv)

    rules_file = Path(args.rules_file).expanduser().resolve() \
        if args.rules_file else None

    config.ensure_dirs()
    deleted = apply_rules(dry_run=args.dry_run, rules_file=rules_file)

    if not deleted:
        print("auto-clean: nothing to delete")
        return 0

    verb = "Would delete" if args.dry_run else "Deleted"
    for fp in deleted:
        try:
            rel = fp.relative_to(BRAIN_DIR)
        except ValueError:
            rel = fp
        print(f"  {verb}: {rel}")
    suffix = " (dry run)" if args.dry_run else ""
    print(f"\nauto-clean: {len(deleted)} file(s) removed{suffix}.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
