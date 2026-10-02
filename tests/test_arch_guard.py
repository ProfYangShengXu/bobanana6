"""Architecture guards -- the hard gate for this unit.

These are not style checks. The kernel is only trustworthy if it is
environment-free and if no dependency arrow leaves it, so both properties are
asserted mechanically over the real source tree.

The guard helpers are themselves self-tested with synthetic sources: a scanner
that can never fail is worse than no scanner, because it manufactures
confidence. Each helper therefore has a positive case (real tree is clean) and
a negative case (a deliberately bad source is caught).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from bobanana6.core.model import (
    BlindSpot,
    Candidate,
    Concept,
    Conflict,
    DefinitionMismatch,
    DisagreementTypes,
    Ontology,
    content_digest,
)

pytestmark = pytest.mark.arch_guard

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "src" / "bobanana6" / "core"
MODEL_MODULE = "bobanana6.core.model"

FORBIDDEN_IMPORTS = (
    "openai",
    "anthropic",
    "httpx",
    "requests",
    "urllib",
    "aiohttp",
    "selenium",
    "playwright",
    "glm",
    "zhipu",
)

FORBIDDEN_VOCABULARY = ("prompt", "token", "temperature", "llm", "model_id", "http", "url", "html", "cookie")

#: The spec mandates these three identifiers on the stable side while also
#: forbidding the substrings they contain. They are the only exemption; any
#: other occurrence of the vocabulary still fails.
MANDATED_PORT_IDENTIFIERS = ("llmclient", "max_tokens", "approx_tokens")

RUNTIME_CLASS_NAMES = (
    "Attribution",
    "ClassifiedError",
    "Checkpoint",
    "GateVerdict",
    "BobananaAction",
    "EventSink",
    "ResourceGate",
)


def core_files() -> list[Path]:
    return sorted(path for path in CORE.rglob("*.py") if path.is_file())


def import_roots(source: str) -> list[str]:
    """Top-level module names of every absolute import, including lazy ones."""
    roots: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.append(node.module.split(".")[0])
    return roots


def outbound_edges(module_name: str, source: str, *, is_package: bool = False) -> list[str]:
    """Absolute imports that leave ``bobanana6.core`` plus relative escapes.

    Relative imports are resolved against the module's package (the module
    itself when it is a package ``__init__``), so ``from ...x import y`` written
    below ``core/`` is reported, not ignored.
    """
    package = module_name.split(".") if is_package else module_name.split(".")[:-1]
    outbound: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in sys.stdlib_module_names:
                    outbound.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if not node.module:
                    continue
                root = node.module.split(".")[0]
                if root in sys.stdlib_module_names:
                    continue
                if node.module == "bobanana6.core" or node.module.startswith("bobanana6.core."):
                    continue
                outbound.append(node.module)
            else:
                remaining = len(package) - (node.level - 1)
                if remaining <= 0:
                    outbound.append("." * node.level + (node.module or ""))
                    continue
                resolved = ".".join(package[:remaining])
                if not (resolved == "bobanana6.core" or resolved.startswith("bobanana6.core.")):
                    outbound.append("." * node.level + (node.module or ""))
    return outbound


def vocabulary_hits(source: str) -> list[str]:
    text = source.lower()
    for token in MANDATED_PORT_IDENTIFIERS:
        text = text.replace(token, "")
    return [word for word in FORBIDDEN_VOCABULARY if word in text]


def module_name_for(path: Path) -> str:
    relative = path.relative_to(REPO / "src").with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


# --------------------------------------------------------------------------- #
# Environment freedom
# --------------------------------------------------------------------------- #


def test_core_source_tree_is_present_and_non_empty() -> None:
    files = core_files()
    assert len(files) >= 12, f"expected the full kernel, found {[f.name for f in files]}"


def test_core_imports_only_stdlib() -> None:
    """AST scan, not a text scan: catches lazy imports and ignores prose."""
    offenders: list[str] = []
    for path in core_files():
        for root in import_roots(path.read_text(encoding="utf-8")):
            if root not in sys.stdlib_module_names:
                offenders.append(f"{path.relative_to(REPO)}: {root}")
    assert offenders == [], f"non-stdlib imports in core: {offenders}"


def test_core_has_no_forbidden_imports() -> None:
    offenders: list[str] = []
    for path in core_files():
        for root in import_roots(path.read_text(encoding="utf-8")):
            if root.startswith(FORBIDDEN_IMPORTS):
                offenders.append(f"{path.relative_to(REPO)}: {root}")
    assert offenders == [], f"forbidden imports in core: {offenders}"


def test_core_is_environment_free() -> None:
    """No model SDK, no network client, no environment vocabulary anywhere in
    core -- comments and string literals included."""
    offenders: list[str] = []
    for path in core_files():
        source = path.read_text(encoding="utf-8")
        for word in vocabulary_hits(source):
            offenders.append(f"{path.relative_to(REPO)}: '{word}'")
    assert offenders == [], f"environment vocabulary leaked into core: {offenders}"


def test_no_outbound_edges_from_core() -> None:
    offenders: list[str] = []
    for path in core_files():
        source = path.read_text(encoding="utf-8")
        for name in outbound_edges(module_name_for(path), source, is_package=path.name == "__init__.py"):
            offenders.append(f"{path.relative_to(REPO)} imports {name}")
    assert offenders == [], f"outbound dependency arrows from core: {offenders}"


def test_guard_detects_non_stdlib_import() -> None:
    # The spec's sketch only checked the first alias of an `import` statement;
    # the AST scan must see every alias, including a sneaky second one.
    assert import_roots("import os, numpy") == ["os", "numpy"]
    assert "numpy" not in sys.stdlib_module_names


def test_guard_detects_outbound_edge() -> None:
    assert outbound_edges(MODEL_MODULE, "from bobanana6.adapters import glm") == ["bobanana6.adapters"]
    assert outbound_edges(MODEL_MODULE, "from ...adapters import glm") == ["...adapters"]
    assert outbound_edges(MODEL_MODULE, "from .other import thing") == []
    assert outbound_edges(MODEL_MODULE, "from bobanana6.core import model") == []
    # A package __init__ may import from itself with a single dot.
    assert outbound_edges("bobanana6.core", "from . import model", is_package=True) == []
    assert outbound_edges("bobanana6.core.checkers", "from ..model import x", is_package=True) == []


def test_guard_detects_vocabulary_leak() -> None:
    assert vocabulary_hits("x = temperature + 1") == ["temperature"]
    assert vocabulary_hits("class LLMClient(Protocol): ...") == []
    assert vocabulary_hits("def complete(self, max_tokens: int): ...") == []
    assert vocabulary_hits("a_tokenizer = 1") == ["token"]


def test_core_knows_no_runtime() -> None:
    combined = "\n".join(path.read_text(encoding="utf-8").lower() for path in core_files())
    assert "hermes" not in combined
    assert "claude" not in combined


def test_runtime_layer_concerns_do_not_leak_into_core() -> None:
    defined: set[str] = set()
    for path in core_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef):
                defined.add(node.name)
    assert defined.isdisjoint(RUNTIME_CLASS_NAMES), f"runtime-layer classes in core: {defined & set(RUNTIME_CLASS_NAMES)}"


# --------------------------------------------------------------------------- #
# Semantics: three distinct divergence types, no per-agent semantics
# --------------------------------------------------------------------------- #


def test_three_divergence_types_are_distinct() -> None:
    from dataclasses import fields

    assert len(DisagreementTypes) == 3
    assert len(set(DisagreementTypes)) == 3

    shapes = {frozenset(f.name for f in fields(t)) for t in DisagreementTypes}
    assert len(shapes) == 3, f"divergence types share their shape: {shapes}"
    assert not issubclass(BlindSpot, Conflict)
    assert not issubclass(Conflict, DefinitionMismatch)
    assert not issubclass(DefinitionMismatch, BlindSpot)


def test_no_per_agent_semantics() -> None:
    """Only the shared ontology may carry concepts; a candidate never does."""
    from dataclasses import fields

    names = {f.name for f in fields(Candidate)}
    assert not (names & {"concepts", "definitions", "ontology", "units"})

    kernel_source = (CORE / "kernel.py").read_text(encoding="utf-8")
    constructed = [
        node.func.id
        for node in ast.walk(ast.parse(kernel_source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert "Ontology" not in constructed, "kernel must not build its own ontology"


def test_ontology_draft_is_frozen_before_workers() -> None:
    from dataclasses import replace

    mutable = Ontology(
        domain="d",
        version="1",
        concepts={"a": Concept(id="a", label="A", kind="measure", domain="d")},
        definitions={},
        units={},
    )
    frozen = mutable.freeze()

    assert frozen.is_frozen and not mutable.is_frozen
    assert frozen.content_hash() == mutable.freeze().content_hash()
    with pytest.raises(TypeError):
        frozen.concepts["b"] = None  # type: ignore[index]

    # Freeze must be content-sensitive: a different ontology hashes differently.
    other = replace(frozen, concepts={"a": replace(frozen.concepts["a"], label="B")})
    assert other.content_hash() != frozen.content_hash()


def test_content_digest_is_order_stable() -> None:
    assert content_digest({"b": 1, "a": 2}) == content_digest({"a": 2, "b": 1})
    assert content_digest({"a": 1}) != content_digest({"a": 2})
