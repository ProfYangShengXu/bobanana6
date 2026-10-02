"""取网 + 快照：``core/ports.py::SourcePort`` 的实作（易变侧）。"""

from __future__ import annotations

from .fetchers import (
    PROXY_ENV_VARS,
    FileFetcher,
    HttpFetcher,
    LocatorFetcher,
    suffix_for,
)
from .store import SnapshotStore, SourceError

__all__ = [
    "PROXY_ENV_VARS",
    "FileFetcher",
    "HttpFetcher",
    "LocatorFetcher",
    "SnapshotStore",
    "SourceError",
    "suffix_for",
]
