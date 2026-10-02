"""Ports are stable-side interfaces; the fakes prove they are satisfiable.

Protocols are structural, so no fake inherits from them. The runtime-checkable
ones can be checked by ``isinstance``; the rest are checked by member presence,
which also catches a port silently losing a method.
"""

from __future__ import annotations

from fakes import (
    FakeArbiter,
    FakeOntologyPort,
    FakeRuntime,
    FakeSourcePort,
    candidate,
    mini_ontology,
)

from bobanana6.core.ports import (
    AgentIO,
    AgentRuntimePort,
    ArbiterPort,
    ConstraintSource,
    DecomposerPort,
    LLMClient,
    OntologyPort,
    SourcePort,
)


def test_fakes_structurally_satisfy_the_ports() -> None:
    assert isinstance(FakeOntologyPort(), OntologyPort)
    assert isinstance(FakeRuntime(candidate()), AgentRuntimePort)
    assert isinstance(FakeArbiter(), ArbiterPort)
    assert isinstance(FakeSourcePort(), SourcePort)


def test_ontology_port_fake_returns_a_frozen_ontology() -> None:
    obtained = FakeOntologyPort(mini_ontology()).obtain(None, None)  # type: ignore[arg-type]
    assert obtained.is_frozen


def test_arbiter_fake_returns_a_bundle_delta() -> None:
    delta = FakeArbiter().resolve(None, None, ())  # type: ignore[arg-type]
    assert delta.note


def test_source_port_fake_returns_a_snapshot() -> None:
    from bobanana6.core.model import SourceRef

    assert FakeSourcePort().fetch(SourceRef(ref="snap-1")).ref == "snap-1"


def test_every_mandated_port_member_exists() -> None:
    assert hasattr(OntologyPort, "obtain")
    assert hasattr(DecomposerPort, "split") and hasattr(DecomposerPort, "refine")
    assert hasattr(ConstraintSource, "compile")
    assert hasattr(AgentRuntimePort, "run")
    assert hasattr(LLMClient, "complete")
    assert hasattr(ArbiterPort, "resolve")
    assert hasattr(SourcePort, "fetch")
    for member in ("persist", "compress", "context_length", "note_activity"):
        assert hasattr(AgentIO, member)
