"""``OntologyPort`` implementations: the four sources the kernel cannot tell apart.

  * :class:`FileOntology`  -- source ④ (hand written) and ② (reuse the frozen file).
  * :class:`DraftingOntology` -- source ① (auto draft): one model call produces
    an ontology *candidate*. The candidate goes through the same structural
    loader as a hand-written file, and the **assembly point** then runs
    ``core/ontology.py::validate_ontology`` on it. A draft is therefore never
    trusted just because a model produced it, and this module stays free of any
    judgment import.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..core.model import InputSurvey, Ontology, TaskContract
from ..core.ports import LLMClient
from ..ontology import OntologyLoadError, load_ontology, load_ontology_file

__all__ = ["DraftingOntology", "FileOntology", "OntologyDraftError"]

_DRAFT_INSTRUCTIONS = """\
Draft the shared semantics for one task. Reply with a single JSON object and no prose.

Required keys: domain, version, concepts.
Optional keys: units, relations, definitions, code_systems, calendar.

Shapes:
  concepts:  {"<concept id>": {"label": str, "kind": "entity|attribute|event|measure",
                               "aliases": [str], "unit": str, "code_system": str}}
  units:     {"<symbol>": {"symbol": str, "scale": number, "base": str}}
  relations: [{"src": str, "dst": str, "kind": "belongs_to|subtype_of|measured_in|derived_from",
               "cardinality": "1:1|1:N|N:1|N:M"}]
  definitions: {"<concept id>": {"version": str, "text": str, "formula": str, "edge_cases": [str]}}
  code_systems: {"<name>": [str]}
"""


class OntologyDraftError(RuntimeError):
    """The draft could not be read as an ontology document."""

    #: The answer came back in the wrong shape, so the next attempt must be told
    #: what was wrong instead of being sent the identical request again.
    reason = "format_error"


@dataclass(frozen=True)
class FileOntology:
    """Read the ontology from a file. No model involved."""

    path: Path

    def obtain(self, contract: TaskContract, survey: InputSurvey) -> Ontology:
        return load_ontology_file(self.path)


@dataclass(frozen=True)
class DraftingOntology:
    """Ask a model for a draft, then hand it to the shared structural loader."""

    client: LLMClient
    max_tokens: int = 2000
    domain_hint: str = ""

    def obtain(self, contract: TaskContract, survey: InputSurvey) -> Ontology:
        messages = [
            {"role": "system", "content": _DRAFT_INSTRUCTIONS},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "goal": contract.goal,
                        "deliverables": [
                            {"name": d.name, "schema": dict(d.schema_payload)} for d in contract.deliverables
                        ],
                        "sources": [s.ref for s in contract.sources],
                        "input_sources": list(survey.source_refs),
                        "domain_hint": self.domain_hint,
                        "notes": contract.notes,
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        response = self.client.complete(messages, [], self.max_tokens)
        text = response.text.strip()
        if not text:
            raise OntologyDraftError("the draft is empty; there is nothing to validate")
        try:
            # JSON is a subset of YAML, so the draft rides the same loader as a
            # hand-written file -- one structural rule, not two.
            return load_ontology(text, source="<draft>")
        except OntologyLoadError as exc:
            raise OntologyDraftError(f"the draft is not a valid ontology document: {exc}") from exc
