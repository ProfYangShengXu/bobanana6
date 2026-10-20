"""取网 + 快照：``core/ports.py::SourcePort`` / ``ResearchPort`` 的实作（易变侧）。"""

from __future__ import annotations

from .fetchers import (
    PROXY_ENV_VARS,
    FileFetcher,
    HttpFetcher,
    LocatorFetcher,
    suffix_for,
)
from .form import SourceForm, SourceFormError, decode_body, form_for, forms_for
from .prefetch import (
    PrefetchCoverage,
    checklist_problems,
    parse_count,
    parse_names,
    read_materials_text,
)
from .reader import SnapshotFileReader, rows_from_csv, rows_from_json
from .research import (
    LinkExpandingResearch,
    ResearchLedger,
    ResearchNeedsAuthorization,
    ResearchUnavailable,
    trust_tier,
)
from .store import SnapshotStore, SourceError, SourceLedger

__all__ = [
    "PROXY_ENV_VARS",
    "FileFetcher",
    "HttpFetcher",
    "LinkExpandingResearch",
    "LocatorFetcher",
    "PrefetchCoverage",
    "ResearchLedger",
    "ResearchNeedsAuthorization",
    "ResearchUnavailable",
    "SnapshotFileReader",
    "SnapshotStore",
    "SourceError",
    "SourceForm",
    "SourceFormError",
    "SourceLedger",
    "checklist_problems",
    "decode_body",
    "form_for",
    "forms_for",
    "parse_count",
    "parse_names",
    "read_materials_text",
    "rows_from_csv",
    "rows_from_json",
    "suffix_for",
    "trust_tier",
]
