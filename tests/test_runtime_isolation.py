"""Architectural test: brain.runtime must not depend on the vault layer.

If brain.runtime.* imports from brain.entities, brain.graph, or
brain.semantic, transport and curated knowledge are coupled — which
is exactly the design we ruled out in the spec.
"""
from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path

FORBIDDEN_PREFIXES = (
    "brain.entities",
    "brain.graph",
    "brain.semantic",
    "brain.consolidation",
    "brain.dedupe",
    "brain.dedupe_judge",
    "brain.dedupe_ledger",
    "brain.note_extract",
    "brain.auto_extract",
    "brain.apply_extraction",
    "brain.reconcile",
    "brain.ontology_guard",
    "brain.predicate_registry",
    "brain.subject_reject",
    "brain.triple_audit",
    "brain.triple_rules",
)


def _runtime_modules():
    runtime = importlib.import_module("brain.runtime")
    runtime_path = Path(runtime.__file__).parent
    for info in pkgutil.iter_modules([str(runtime_path)]):
        if info.ispkg:
            continue
        yield f"brain.runtime.{info.name}", runtime_path / f"{info.name}.py"


def _imports(file_path: Path) -> list[str]:
    tree = ast.parse(file_path.read_text())
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.append(node.module)
    return out


def test_runtime_modules_dont_import_vault():
    violations: list[tuple[str, str]] = []
    for mod_name, file_path in _runtime_modules():
        for imp in _imports(file_path):
            for forbidden in FORBIDDEN_PREFIXES:
                if imp == forbidden or imp.startswith(forbidden + "."):
                    violations.append((mod_name, imp))
    assert not violations, (
        "brain.runtime modules must not import from vault layer:\n"
        + "\n".join(f"  {mod} imports {imp}" for mod, imp in violations)
    )
