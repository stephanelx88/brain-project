"""Architectural test: brain.claims must not depend on entities/semantic/graph layers.

Claims are knowledge layer. Importing entities or semantic from
claims would couple the knowledge layer to the projection or
indexing layer — violates 3-layer separation.
"""
from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path

FORBIDDEN_PREFIXES = (
    "brain.entities",
    "brain.semantic",
    "brain.graph",
    "brain.consolidation",
    "brain.dedupe",
    "brain.dedupe_judge",
    "brain.note_extract",
    "brain.auto_extract",
    "brain.apply_extraction",
    "brain.reconcile",
)


def _claims_modules():
    pkg = importlib.import_module("brain.claims")
    pkg_path = Path(pkg.__file__).parent
    for info in pkgutil.iter_modules([str(pkg_path)]):
        if info.ispkg:
            continue
        yield f"brain.claims.{info.name}", pkg_path / f"{info.name}.py"


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


def test_claims_modules_dont_import_other_layers():
    violations: list[tuple[str, str]] = []
    for mod_name, file_path in _claims_modules():
        for imp in _imports(file_path):
            for forbidden in FORBIDDEN_PREFIXES:
                if imp == forbidden or imp.startswith(forbidden + "."):
                    violations.append((mod_name, imp))
    assert not violations, (
        "brain.claims modules must not import from entities/semantic/graph/etc:\n"
        + "\n".join(f"  {mod} imports {imp}" for mod, imp in violations)
    )
