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
    UNIT_SYMBOLS,
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

#: Port members the spec mandates on the stable side even though they contain
#: forbidden substrings (实现规格 §3).
_MANDATED_PORT_IDENTIFIERS = ("llmclient", "max_tokens", "approx_tokens")

#: Ruler literals are exempt *only* because they appear in the controlled
#: UNIT_SYMBOLS table (spec §5.0.1 ④). A quoted string is not automatically
#: safe: any literal outside the table still trips the vocabulary scan.
UNIT_SYMBOL_LITERALS = tuple(f'"{symbol}"' for symbol in sorted(UNIT_SYMBOLS))

MANDATED_IDENTIFIERS = _MANDATED_PORT_IDENTIFIERS + UNIT_SYMBOL_LITERALS

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
    for token in MANDATED_IDENTIFIERS:
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
    # Only literals in UNIT_SYMBOLS are exempt, not "anything quoted".
    assert vocabulary_hits('max_output_unit: str = "tokens"') == []
    assert vocabulary_hits('max_output_unit: str = "token_bucket"') == ["token"]
    assert vocabulary_hits("tokens = 5") == ["token"]


def test_unit_symbol_exemptions_come_from_the_whitelist() -> None:
    assert set(UNIT_SYMBOL_LITERALS) == {f'"{symbol}"' for symbol in UNIT_SYMBOLS}
    assert UNIT_SYMBOLS == frozenset({"tokens", "bytes"})


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


# --------------------------------------------------------------------------- #
# Field reader rule (实现规格 §5.0)
# --------------------------------------------------------------------------- #

#: The three reader classes from 实现规格 §5.0. ``passthrough:`` is kept as a
#: general mechanism for a *known* external reader that is not a named module
#: (spec §5.0.2 forbids using it as a catch-all for "nobody reads this yet").
#: No field currently uses it; its reason text is still required.
READER_MARKERS = ("core:", "external:", "passthrough:")


def _is_dataclass_decorator(decorators: list[ast.expr]) -> bool:
    for decorator in decorators:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Name) and target.id == "dataclass":
            return True
        if isinstance(target, ast.Attribute) and target.attr == "dataclass":
            return True
    return False


def field_reader_markers(source: str) -> dict[tuple[str, str], str | None]:
    """Map every dataclass field in ``source`` to its reader marker prefix.

    ``None`` means the field declares no reader. The scan covers the whole
    declaration (annotations may wrap over several lines), so a marker cannot be
    hidden in a place the scanner does not look.
    """
    lines = source.splitlines()
    found: dict[tuple[str, str], str | None] = {}
    for node in ast.parse(source).body:
        if not isinstance(node, ast.ClassDef) or not _is_dataclass_decorator(node.decorator_list):
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                segment = "\n".join(lines[stmt.lineno - 1 : stmt.end_lineno])
                marker = next((m for m in READER_MARKERS if m in segment), None)
                found[(node.name, stmt.target.id)] = marker
    return found


def passthrough_without_reason(source: str) -> tuple[str, ...]:
    """``passthrough:`` with nothing after the colon is an empty declaration."""
    offenders: list[str] = []
    for node in ast.parse(source).body:
        if not isinstance(node, ast.ClassDef) or not _is_dataclass_decorator(node.decorator_list):
            continue
        lines = source.splitlines()
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                segment = "\n".join(lines[stmt.lineno - 1 : stmt.end_lineno])
                if "passthrough:" in segment:
                    reason = segment.split("passthrough:", 1)[1].split("\n")[0].strip()
                    if reason == "":
                        offenders.append(f"{node.name}.{stmt.target.id}")
    return tuple(offenders)


def test_every_field_declares_a_reader() -> None:
    source = (CORE / "model.py").read_text(encoding="utf-8")
    markers = field_reader_markers(source)

    assert markers, "no dataclass fields found -- the scanner itself is broken"
    assert len(markers) >= 120, f"scanner only saw {len(markers)} fields; it is not covering the model"

    missing = sorted(f"{cls}.{name}" for (cls, name), marker in markers.items() if marker is None)
    assert missing == [], f"fields with no declared reader: {missing}"

    ambiguous = sorted(
        f"{cls}.{name}"
        for (cls, name) in markers
        if sum(1 for m in READER_MARKERS if m in _field_segment(source, cls, name)) > 1
    )
    assert ambiguous == [], f"fields with more than one reader marker: {ambiguous}"

    empty_reasons = passthrough_without_reason(source)
    assert empty_reasons == (), f"passthrough fields must state a reason: {empty_reasons}"


def _field_segment(source: str, cls: str, field_name: str) -> str:
    lines = source.splitlines()
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.target.id == field_name:
                    return "\n".join(lines[stmt.lineno - 1 : stmt.end_lineno])
    return ""


def test_guard_detects_a_field_without_a_reader() -> None:
    unmarked = "from dataclasses import dataclass\n\n@dataclass\nclass X:\n    a: int = 0\n"
    marked = "from dataclasses import dataclass\n\n@dataclass\nclass X:\n    a: int = 0  # core:kernel\n"
    assert field_reader_markers(unmarked) == {("X", "a"): None}
    assert field_reader_markers(marked) == {("X", "a"): "core:"}


def test_guard_rejects_empty_passthrough_reason() -> None:
    empty = "from dataclasses import dataclass\n\n@dataclass\nclass X:\n    a: int = 0  # passthrough:\n"
    filled = "from dataclasses import dataclass\n\n@dataclass\nclass X:\n    a: int = 0  # passthrough: carried for the report\n"
    assert passthrough_without_reason(empty) == ("X.a",)
    assert passthrough_without_reason(filled) == ()


# --------------------------------------------------------------------------- #
# kind set and ruler whitelist: one source of truth each
# --------------------------------------------------------------------------- #


def test_known_kinds_and_dispatch_table_agree() -> None:
    """架构设计 §9: KNOWN_KINDS and checkers.CHECKS must be the same set.

    They drifted silently before: a kind present only in CHECKS would be
    dispatchable while KNOWN_KINDS still denied it, and vice versa.
    """
    from bobanana6.core.checkers import CHECKS as dispatched
    from bobanana6.core.model import KNOWN_KINDS

    #: ★ 这个数是**棘轮**，不是装饰：只许往上加，不许悄悄减。
    #: 16 -> 17 是 2026-10-20 显式加了第五道门 `item_coverage`（清单逐项覆盖），
    #: 不是某条 checker 掉了之后顺手改的数字。
    assert len(dispatched) == 17, "the dispatch table must not silently shrink"
    assert set(KNOWN_KINDS) == set(dispatched)


def unit_field_defaults(source: str) -> dict[str, str]:
    """Literal defaults of every ``*_unit`` field in a dataclass source."""
    found: dict[str, str] = {}
    for node in ast.parse(source).body:
        if not isinstance(node, ast.ClassDef) or not _is_dataclass_decorator(node.decorator_list):
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.target.id.endswith("_unit"):
                assert isinstance(stmt.value, ast.Constant), f"{node.name}.{stmt.target.id} needs a literal default"
                found[f"{node.name}.{stmt.target.id}"] = stmt.value.value
    return found


def test_every_unit_field_uses_a_whitelisted_symbol() -> None:
    defaults = unit_field_defaults((CORE / "model.py").read_text(encoding="utf-8"))
    assert defaults, "no *_unit field found -- the whitelist guard would be vacuous"
    bad = {name: value for name, value in defaults.items() if value not in UNIT_SYMBOLS}
    assert bad == {}, f"*_unit fields outside UNIT_SYMBOLS: {bad}"


def test_unit_whitelist_guard_detects_a_bad_symbol() -> None:
    source = "from dataclasses import dataclass\n\n@dataclass\nclass X:\n    max_unit: str = 'widgets'\n"
    defaults = unit_field_defaults(source)
    assert defaults == {"X.max_unit": "widgets"}
    assert set(defaults.values()) - UNIT_SYMBOLS == {"widgets"}


# --------------------------------------------------------------------------- #
# agentloop/ boundary (实现规格 §9.1): it implements core ports, and it must
# not reach for the volatile side or for core's concrete judgment modules.
# --------------------------------------------------------------------------- #

AGENTLOOP = REPO / "src" / "bobanana6" / "agentloop"

AGENTLOOP_STEP_MODULES = (
    "attribution.py",
    "compress.py",
    "concurrency.py",
    "errors.py",
    "events.py",
    "gate.py",
    "loop.py",
    "metrics.py",
    "recovery.py",
    "sandbox.py",
    "state.py",
    "tools.py",
)

FORBIDDEN_AGENTLOOP_DEPS = (
    "bobanana6.adapters",
    "bobanana6.sources",
    "bobanana6.cli",
    "bobanana6.core.kernel",
    "bobanana6.core.checkers",
    "bobanana6.core.split",
    "bobanana6.core.assemble",
    "bobanana6.core.consist",
    "bobanana6.core.ontology",
    "bobanana6.core.capabilities",
    "bobanana6.core.provenance",
)


def agentloop_files() -> list[Path]:
    return sorted(path for path in AGENTLOOP.rglob("*.py") if path.is_file())


def resolved_imports(module_name: str, source: str, *, is_package: bool = False) -> list[str]:
    """Absolute module names for absolute imports and resolved relative ones."""
    package = module_name.split(".") if is_package else module_name.split(".")[:-1]
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    found.append(node.module)
                continue
            remaining = len(package) - (node.level - 1)
            if remaining > 0:
                base = ".".join(package[:remaining])
                found.append(f"{base}.{node.module}" if node.module else base)
    return found


def test_agentloop_step_modules_are_present() -> None:
    names = {path.name for path in agentloop_files()}
    assert set(AGENTLOOP_STEP_MODULES) <= names, f"missing agentloop modules: {set(AGENTLOOP_STEP_MODULES) - names}"


def test_agentloop_has_no_forbidden_dependencies() -> None:
    offenders: list[str] = []
    for path in agentloop_files():
        source = path.read_text(encoding="utf-8")
        for module in resolved_imports(module_name_for(path), source, is_package=path.name == "__init__.py"):
            if module.startswith(FORBIDDEN_AGENTLOOP_DEPS):
                offenders.append(f"{path.relative_to(REPO)} imports {module}")
    assert offenders == [], f"agentloop reached outside its allowed dependencies: {offenders}"


def test_agentloop_dependency_guard_detects_a_violation() -> None:
    absolute = resolved_imports("bobanana6.agentloop.errors", "from bobanana6.adapters import glm")
    assert absolute == ["bobanana6.adapters"]
    assert any(module.startswith(FORBIDDEN_AGENTLOOP_DEPS) for module in absolute)

    relative = resolved_imports("bobanana6.agentloop.errors", "from .events import Event")
    assert relative == ["bobanana6.agentloop.events"]


# --------------------------------------------------------------------------- #
# Steps 8-10: ontology/ contract/ decompose/
#
# These sit below the volatile side (adapters/sources/cli) and above the frozen
# kernel.  Two arrows must hold, both direction-bearing:
#   * nothing in core/ may import them       (core is the terminal, zero-model)
#   * they may not import adapters/sources/cli (only the assembly point may)
# --------------------------------------------------------------------------- #

MID_LAYER_PACKAGES = ("ontology", "contract", "decompose")

MID_LAYER_STEP_MODULES = (
    "ontology/loader.py",
    "ontology/__init__.py",
    "ontology/schemas/ontology.schema.json",
    "contract/parser.py",
    "contract/__init__.py",
    "decompose/strategies.py",
    "decompose/budget.py",
    "decompose/__init__.py",
)

FORBIDDEN_MID_LAYER_DEPS = (
    "bobanana6.adapters",
    "bobanana6.sources",
    "bobanana6.cli",
)


def mid_layer_files() -> list[Path]:
    root = REPO / "src" / "bobanana6"
    return sorted(path for package in MID_LAYER_PACKAGES for path in (root / package).rglob("*.py"))


def mid_layer_offenders(module_name: str, source: str, *, is_package: bool = False) -> list[str]:
    """Imports that would drag the volatile side into ontology/contract/decompose.

    Only resolvable names are reported; a relative import that climbs above
    ``bobanana6`` is not representable (Python raises at import time), so there
    is nothing to resolve it to.
    """
    return [
        module
        for module in resolved_imports(module_name, source, is_package=is_package)
        if module.startswith(FORBIDDEN_MID_LAYER_DEPS)
    ]


def test_mid_layer_step_modules_are_present() -> None:
    root = REPO / "src" / "bobanana6"
    missing = [name for name in MID_LAYER_STEP_MODULES if not (root / name).is_file()]
    assert missing == [], f"missing step 8-10 modules: {missing}"


def test_mid_layer_packages_do_not_reach_the_volatile_side() -> None:
    offenders: list[str] = []
    for path in mid_layer_files():
        source = path.read_text(encoding="utf-8")
        for module in mid_layer_offenders(
            module_name_for(path), source, is_package=path.name == "__init__.py"
        ):
            offenders.append(f"{path.relative_to(REPO)} imports {module}")
    assert offenders == [], f"mid-layer reached the volatile side: {offenders}"


def test_core_does_not_import_the_mid_layer() -> None:
    """§1.2: the arrow points at core; core never points back."""
    forbidden = tuple(f"bobanana6.{package}" for package in MID_LAYER_PACKAGES)
    offenders: list[str] = []
    for path in core_files():
        source = path.read_text(encoding="utf-8")
        for module in resolved_imports(
            module_name_for(path), source, is_package=path.name == "__init__.py"
        ):
            if module.startswith(forbidden):
                offenders.append(f"{path.relative_to(REPO)} imports {module}")
    assert offenders == [], f"core depends on the mid layer: {offenders}"


def test_mid_layer_dependency_guard_detects_a_violation() -> None:
    module = "bobanana6.decompose.strategies"
    assert mid_layer_offenders(module, "from bobanana6.adapters import glm") == [
        "bobanana6.adapters"
    ]
    assert mid_layer_offenders(module, "from ..adapters import glm") == ["bobanana6.adapters"]
    assert mid_layer_offenders(module, "from ..core import split") == []
    assert mid_layer_offenders(module, "import os") == []

    core_guard = tuple(f"bobanana6.{package}" for package in MID_LAYER_PACKAGES)
    assert [
        found
        for found in resolved_imports("bobanana6.core.kernel", "from bobanana6.ontology import loader")
        if found.startswith(core_guard)
    ] == ["bobanana6.ontology"]


# --------------------------------------------------------------------------- #
# Steps 11-14: sources/ adapters/ services/ assemble_impl/ report/ cli.py
#
# Three arrows are checked here:
#   * core/ still points at nothing volatile (extended to the new packages);
#   * adapters/ talks to the kernel through the ports, never through the
#     judgment modules -- that is what keeps 判定 out of the adapter layer;
#   * only cli.py constructs boundary implementations (AGENTS §1.4).
# --------------------------------------------------------------------------- #

WIRING_PACKAGES = ("sources", "adapters", "services", "assemble_impl", "report")

JUDGMENT_MODULES = (
    "bobanana6.core.kernel",
    "bobanana6.core.checkers",
    "bobanana6.core.split",
    "bobanana6.core.assemble",
    "bobanana6.core.consist",
    "bobanana6.core.ontology",
    "bobanana6.core.capabilities",
    "bobanana6.core.provenance",
)

STEP_11_14_MODULES = (
    "sources/__init__.py",
    "sources/store.py",
    "sources/fetchers.py",
    "adapters/__init__.py",
    "adapters/openai_compat.py",
    "adapters/offline.py",
    "adapters/ontology.py",
    "services/__init__.py",
    "services/tools.py",
    "services/registry.py",
    "services/checks.py",
    "assemble_impl/__init__.py",
    "assemble_impl/produce.py",
    "assemble_impl/runtime.py",
    "assemble_impl/runner.py",
    "report/__init__.py",
    "report/builders.py",
    "cli.py",
    "__main__.py",
)

#: Implementations that only the assembly point may construct. The runtime
#: internals (``WorkerLoop`` / ``RuntimeGate`` ...) are deliberately absent:
#: ``assemble_impl/`` *is* the assembly implementation (AGENTS §1.5) and it is
#: the layer that adapts those ports. What must not leak is the environment
#: boundary -- model clients, fetchers, ontology sources, the store.
BOUNDARY_IMPLEMENTATIONS = (
    "OpenAICompatibleClient",
    "EchoToolResultClient",
    "ScriptedClient",
    "FileOntology",
    "DraftingOntology",
    "HttpFetcher",
    "FileFetcher",
    "LocatorFetcher",
    "Decomposer",
)


def wiring_files(package: str) -> list[Path]:
    root = REPO / "src" / "bobanana6" / package
    return sorted(path for path in root.rglob("*.py") if path.is_file())


def imported_modules(path: Path) -> list[str]:
    return resolved_imports(
        module_name_for(path), path.read_text(encoding="utf-8"), is_package=path.name == "__init__.py"
    )


def test_step_11_14_modules_are_present() -> None:
    root = REPO / "src" / "bobanana6"
    missing = [name for name in STEP_11_14_MODULES if not (root / name).is_file()]
    assert missing == [], f"missing step 11-14 modules: {missing}"


def test_core_does_not_import_the_volatile_side() -> None:
    """§1.2, extended to every package added after the kernel."""
    forbidden = tuple(f"bobanana6.{package}" for package in (*MID_LAYER_PACKAGES, *WIRING_PACKAGES, "cli"))
    offenders: list[str] = []
    for path in core_files():
        for module in imported_modules(path):
            if module.startswith(forbidden):
                offenders.append(f"{path.relative_to(REPO)} imports {module}")
    assert offenders == [], f"core depends on the volatile side: {offenders}"


def test_adapters_reach_the_kernel_only_through_ports_and_data() -> None:
    """★ The adapter layer does protocol translation, never judgment."""
    offenders: list[str] = []
    for path in wiring_files("adapters"):
        for module in imported_modules(path):
            if module.startswith(JUDGMENT_MODULES):
                offenders.append(f"{path.relative_to(REPO)} imports {module}")
    assert offenders == [], f"adapters imported a judgment module: {offenders}"


def test_sources_and_adapters_are_leaf_layers() -> None:
    """Nothing in the environment boundary may depend on the wiring above it."""
    forbidden = ("bobanana6.services", "bobanana6.assemble_impl", "bobanana6.report", "bobanana6.cli")
    offenders: list[str] = []
    for package in ("sources", "adapters"):
        for path in wiring_files(package):
            for module in imported_modules(path):
                if module.startswith(forbidden):
                    offenders.append(f"{path.relative_to(REPO)} imports {module}")
    assert offenders == [], f"a leaf layer reached the wiring above it: {offenders}"


def constructed_names(source: str) -> tuple[str, ...]:
    """Every bare name or attribute called as a constructor."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            found.append(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            found.append(node.func.attr)
    return tuple(found)


#: Packages whose classes are *only* the assembly point's to construct. Read off
#: the tree, so a new client or fetcher cannot escape the guard by being absent
#: from a hand-written list.
BOUNDARY_PACKAGES = ("sources", "adapters")


def boundary_defining_packages() -> dict[str, str]:
    """Every class the assembly point owns, derived from where it is defined.

    Exceptions are skipped: raising one is not choosing an implementation.
    """
    root = REPO / "src" / "bobanana6"
    defined: dict[str, str] = {}
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(root)
        package = relative.parts[0] if len(relative.parts) > 1 else ""
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.startswith("class "):
                continue
            name = line.split()[1].split("(")[0].split(":")[0]
            bases = line[line.find("(") + 1 : line.rfind(")")] if "(" in line else ""
            if "Error" in bases or "Exception" in bases:
                continue
            if package in BOUNDARY_PACKAGES or name in BOUNDARY_IMPLEMENTATIONS:
                defined[name] = package
    return defined


def boundary_construction_offenders() -> tuple[str, ...]:
    """Constructions of a boundary implementation **from another package**.

    Composing a sibling inside its own module (the fetcher that dispatches by
    locator scheme, say) is that module's job. What this rule forbids is one
    layer reaching over and choosing a concrete implementation for the system --
    which is precisely AGENTS §1.4.
    """
    root = REPO / "src" / "bobanana6"
    defined = boundary_defining_packages()
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "cli.py" or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(root)
        package = relative.parts[0] if len(relative.parts) > 1 else ""
        for name in constructed_names(path.read_text(encoding="utf-8")):
            if name in defined and defined[name] != package:
                offenders.append(f"{path.relative_to(REPO)} constructs {name}() defined in {defined[name]!r}")
    return tuple(offenders)


def test_only_the_cli_constructs_boundary_implementations() -> None:
    """★ AGENTS §1.4: ``cli.py::build_app()`` is the only ``new`` site."""
    assert boundary_construction_offenders() == ()


def test_assembly_point_guard_derives_its_names_from_the_tree() -> None:
    """★ The rule is not a hand-written list: a new client in adapters/ is covered.

    ``SnapshotStore`` is deliberately absent from ``BOUNDARY_IMPLEMENTATIONS``;
    if it still shows up, the guard is reading the tree rather than the list.
    """
    derived = boundary_defining_packages()
    assert "SnapshotStore" not in BOUNDARY_IMPLEMENTATIONS
    assert derived["SnapshotStore"] == "sources"
    assert derived["OpenAICompatibleClient"] == "adapters"
    assert "SourceError" not in derived  # raising an exception is not assembling


def test_assembly_point_guard_detects_a_violation() -> None:
    assert "OpenAICompatibleClient" in constructed_names("c = OpenAICompatibleClient(s)")
    assert "HttpFetcher" in constructed_names("fetcher = sources.HttpFetcher(store=store)")
    assert constructed_names("x = Port()") == ("Port",)
    # a *reference* is not a construction: passing a class around stays legal
    assert "OpenAICompatibleClient" not in constructed_names("from a import OpenAICompatibleClient")


# --------------------------------------------------------------------------- #
# ★ Declared failure reasons: the vocabulary is checked, so a typo cannot ship
#
# ``agentloop/errors.py`` reads ``<Exception>.reason`` before it infers anything
# from a class name or from wording. An unreadable label is ignored at runtime
# (a typo must not decide the recovery action), which means a typo would be
# *silent* -- this guard is what makes it loud.
# --------------------------------------------------------------------------- #


def declared_reasons(source: str) -> dict[str, str]:
    """Class-body ``reason = "<literal>"`` declarations, keyed by class name."""
    found: dict[str, str] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ClassDef):
            continue
        for statement in node.body:
            if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
                continue
            target = statement.targets[0]
            value = statement.value
            if (
                isinstance(target, ast.Name)
                and target.id == "reason"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                found[node.name] = value.value
        for statement in node.body:
            # A typed declaration must not escape the guard either.
            if not isinstance(statement, ast.AnnAssign) or not isinstance(statement.target, ast.Name):
                continue
            if statement.target.id != "reason" or statement.value is None:
                continue
            value = statement.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                found[node.name] = value.value
    return found


def test_every_declared_reason_is_in_the_vocabulary() -> None:
    from bobanana6.agentloop.errors import BobananaAction, FailoverReason

    # Either vocabulary is legal: a label may name a reason, or an action when the
    # 22 reasons have no entry for that failure. Both are checked here so neither
    # can hold a typo.
    allowed = {reason.value for reason in FailoverReason} | {action.value for action in BobananaAction}
    root = REPO / "src" / "bobanana6"
    declared: dict[str, str] = {}
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for name, value in declared_reasons(path.read_text(encoding="utf-8")).items():
            declared[name] = value
    assert len(declared) >= 6, f"the guard found almost nothing to check: {declared}"
    offenders = sorted(f"{name} = {value!r}" for name, value in declared.items() if value not in allowed)
    assert offenders == [], f"declared reason values outside the vocabulary: {offenders}"
    # The two the measured failure chain depends on.
    assert declared["CandidateFormatError"] == "format_error"
    assert declared["SourceError"] == "switch_source"
    assert declared["ToolCallError"] == "format_error"


def test_the_declared_reason_guard_detects_a_typo() -> None:
    assert declared_reasons('class E(Exception):\n    reason = "fmt_error"') == {"E": "fmt_error"}
    assert declared_reasons('class E(Exception):\n    reason = LOCALLY_DEFINED') == {}
    assert declared_reasons("class E(Exception):\n    reason: str = 'format_error'") == {"E": "format_error"}


# --------------------------------------------------------------------------- #
# ★ 补充规格 §四：CI/快照层的架构纯洁性门
# --------------------------------------------------------------------------- #

#: ★★ 为什么 CI 的调度层能用一条架构规则守住：
#: 本项目 CI 便宜的全部理由是 `core/` 是「纯 stdlib · 确定性 · 不碰 I/O」。
#: 一个伸手去够 `agentloop/` / `adapters/` / `sources/` 的调度器，
#: 会把**环境接触**和**易变侧**一起拖进那条本该稳定、可重放、可逐字复现的判定路径。
#: 于是：这个模块**只许** import 标准库与 `bobanana6.core`。
CI_LAYER_MODULE = "bobanana6.services.ci"
CI_LAYER_ALLOWED = ("bobanana6.core",)


def ci_layer_module() -> Path:
    return REPO / "src" / "bobanana6" / "services" / "ci.py"


def ci_layer_offenders(module_name: str, source: str, *, is_package: bool = False) -> list[str]:
    """`bobanana6.*` imports of the CI layer that are **not** the kernel.

    Stdlib imports are not reported: "pure stdlib" is the *allowed* side here.
    """
    return [
        module
        for module in resolved_imports(module_name, source, is_package=is_package)
        if module.startswith("bobanana6.") and not module.startswith(CI_LAYER_ALLOWED)
    ]


def test_the_ci_layer_reaches_only_the_kernel() -> None:
    """★ 补充规格 §四①：新增的 CI/快照层仍然满足「箭头只朝 core」。"""
    path = ci_layer_module()
    assert path.is_file(), f"the CI layer is missing: {path}"
    source = path.read_text(encoding="utf-8")
    offenders = ci_layer_offenders(CI_LAYER_MODULE, source)
    assert offenders == [], (
        f"the CI layer left the kernel side: {offenders} -- it may import only stdlib and "
        f"{CI_LAYER_ALLOWED[0]}"
    )
    # 这条守卫不能是空转的：它必须真的看得见这批 import
    assert [m for m in resolved_imports(CI_LAYER_MODULE, source) if m.startswith("bobanana6.")], (
        "the scanner found no bobanana6 imports at all -- the guard would pass vacuously"
    )


def test_the_ci_layer_guard_detects_a_violation() -> None:
    """反向：一个伸手去够易变侧的调度器必须被抓住（能永远通过的守卫比没有守卫更坏）。"""
    assert ci_layer_offenders(
        CI_LAYER_MODULE, "from ..adapters import glm\n"
    ) == ["bobanana6.adapters"]
    assert ci_layer_offenders(
        CI_LAYER_MODULE, "import bobanana6.agentloop.loop\n"
    ) == ["bobanana6.agentloop.loop"]
    assert ci_layer_offenders(
        CI_LAYER_MODULE, "from .ci_runner import x\n"
    ) == ["bobanana6.services.ci_runner"]
    # 允许的那一侧：标准库与 core 都不报
    assert ci_layer_offenders(CI_LAYER_MODULE, "import json\nfrom ..core import model\n") == []
    assert ci_layer_offenders(CI_LAYER_MODULE, "from ..core.checkers import CHECKS\n") == []


def test_the_layered_gate_schedules_every_registered_checker() -> None:
    """★ 补充规格 §四③：三层门只许【调度】core 已有的 checker —— 一个都不许漏。

    判据：注册表（`core/checkers/CHECKS`）与调度表（`services/ci.py::KIND_LAYER`）**完全相等**。
    加了一个 checker 却没给它分层 ⇒ 这里立刻红，而不是静默漏判某一层。
    """
    from bobanana6.core.checkers import CHECKS
    from bobanana6.services.ci import (
        KIND_LAYER,
        LAYER_ASSEMBLY,
        LAYER_BLOCK,
        LAYER_DELIVERY,
        _require_layered,
    )

    _require_layered()  # 不抛就说明没有漏层的 kind
    assert set(CHECKS) == set(KIND_LAYER)
    assert set(KIND_LAYER.values()) == {LAYER_BLOCK, LAYER_ASSEMBLY}
    assert LAYER_DELIVERY not in set(KIND_LAYER.values()), (
        "the delivery layer must not be selected by checker kind: it is the five whole-run "
        "judgments in core/, not a per-kind dispatch"
    )


def test_the_default_scanner_is_pure_ast() -> None:
    """★ 补充规格 §五⑤ + §一①：默认值扫描器**不许 import 被测模块**。

    理由不是洁癖：有些默认值恰恰是**在 import 期**生效的（模块级常量、字段默认值），
    "扫源码"一旦变成"执行源码"，扫出来的东西就不再是源码的事实。
    """
    path = REPO / "tools" / "default_audit.py"
    assert path.is_file(), f"the default scanner is missing: {path}"
    source = path.read_text(encoding="utf-8")
    offenders = [
        module
        for module in resolved_imports("tools.default_audit", source)
        if module.startswith("bobanana6.")
    ]
    assert offenders == [], f"the scanner imports the modules it scans: {offenders}"
    # 反向：它确实 import 了 ast（不然"纯 ast"是空话）
    assert "ast" in resolved_imports("tools.default_audit", source)


# --------------------------------------------------------------------------- #
# 工具返回换页：单点收口（AGENTS §7③）
# --------------------------------------------------------------------------- #

SRC_ROOT = REPO / "src" / "bobanana6"


def source_files_containing(needle: str) -> dict[str, int]:
    """Every source file under ``src/`` that contains ``needle``, with its count."""
    found: dict[str, int] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        count = path.read_text(encoding="utf-8").count(needle)
        if count:
            found[path.relative_to(REPO).as_posix()] = count
    return found


def test_the_tool_output_page_rule_has_exactly_one_gate() -> None:
    """★ 一条规则、一个调用点：换页只在 ``loop._tool_round`` 发生。

    每条 ``tool`` 行（执行出来的、guardrail 拦下的、参数不可解析而合成的）都在那
    一处建成，所以放那里就是"覆盖全部工具的一份实现"。多一个调用点就是多一份实现，
    而两份实现必然漂移 —— 这座仓库已经有 9 组这种病。
    """
    call_sites = source_files_containing("page_tool_output(")
    call_sites.pop("src/bobanana6/agentloop/tool_output.py", None)  # the definition
    assert call_sites == {"src/bobanana6/agentloop/loop.py": 1}, call_sites


def test_the_tool_output_policy_is_read_from_env_at_one_place() -> None:
    """阈值只有一个来源：装配点从 env 读一次，loop 不读 env（单测才可确定性）。"""
    hits = source_files_containing("ToolOutputPolicy.from_env(")
    assert hits == {"src/bobanana6/assemble_impl/runtime.py": 1}, hits
