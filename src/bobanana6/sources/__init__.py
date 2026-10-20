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
    "ResearchLedger",
    "ResearchNeedsAuthorization",
    "ResearchUnavailable",
    "SnapshotFileReader",
    "SnapshotStore",
    "SourceError",
    "SourceForm",
    "SourceFormError",
    "SourceLedger",
    "decode_body",
    "form_for",
    "forms_for",
    "rows_from_csv",
    "rows_from_json",
    "suffix_for",
    "trust_tier",
]
