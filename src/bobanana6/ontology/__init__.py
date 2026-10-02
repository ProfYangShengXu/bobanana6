"""Ontology data + loader (external input), not judgment rules.

``core/ontology.py`` holds the *rules* that check whether an ontology is
self-consistent; this package reads and structurally validates the *file*
(AGENTS.md §1.5 keeps the two apart on purpose).
"""

from .loader import SCHEMA_PATH, OntologyLoadError, load_ontology, load_ontology_file

__all__ = ["SCHEMA_PATH", "OntologyLoadError", "load_ontology", "load_ontology_file"]
