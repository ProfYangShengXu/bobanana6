"""Snapshots: the frozen carrier a provenance chain points at.

A snapshot is *content addressed* -- the file name is the digest of the bytes --
so fetching the same material twice does not produce a second snapshot
(``实现规格 §13``: 快照 / 内容寻址). The digest is also what makes the frozen
carrier checkable later: the stored bytes can be re-hashed at any time.

Nothing here reads a clock: ``captured_at`` is injected by the caller, because
``freshness`` compares it against an injected ``now`` and a snapshot that stamps
itself is not replayable.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..core.model import Snapshot

__all__ = ["SnapshotStore", "SourceError"]


class SourceError(RuntimeError):
    """A fetch that produced no usable material.

    ★ Raised rather than returning an empty snapshot: downstream, "no data" and
    "the data is empty" are indistinguishable once a blank snapshot exists, and
    the difference is exactly what a provenance check is supposed to catch.
    """

    #: Declared: one source being unreachable says nothing about the block, so the
    #: answer is to read a different source rather than to re-ask the worker.
    reason = "switch_source"

    #: ★ The layer that owns this failure, declared here so attribution does not
    #: have to infer it (``AGENTS.md §7⑫``). ``switch_source`` is in the tool
    #: layer's action table (``attribution.BLAME_ACTIONS``), so the two agree.
    blame = "tool"


@dataclass(frozen=True)
class SnapshotStore:
    """A directory of digest-named files plus the ``Snapshot`` values over them."""

    root: Path

    def __post_init__(self) -> None:
        if not str(self.root):
            raise ValueError("SnapshotStore.root must not be empty")

    def path_for(self, body: bytes, *, suffix: str = ".bin") -> Path:
        digest = hashlib.sha256(body).hexdigest()
        return self.root / f"{digest[:32]}{suffix}"

    def put(
        self,
        *,
        ref: str,
        body: bytes,
        source_kind: str,
        captured_at: str,
        suffix: str = ".bin",
    ) -> Snapshot:
        if not body:
            raise SourceError(
                f"source {ref!r} returned no bytes; refusing to freeze an empty snapshot "
                "(downstream cannot tell 'no data' from 'empty data')"
            )
        try:
            datetime.fromisoformat(captured_at)
        except ValueError as exc:
            raise SourceError(f"source {ref!r}: captured_at {captured_at!r} is not an ISO-8601 timestamp") from exc
        path = self.path_for(body, suffix=suffix)
        self.root.mkdir(parents=True, exist_ok=True)
        if not path.exists():  # same bytes -> one file, however often it is fetched
            path.write_bytes(body)
        return Snapshot(ref=ref, body_path=path, captured_at=captured_at, source_kind=source_kind)

    def read(self, snapshot: Snapshot) -> bytes:
        try:
            return snapshot.body_path.read_bytes()
        except OSError as exc:
            raise SourceError(f"snapshot {snapshot.ref!r}: cannot read {snapshot.body_path}: {exc}") from exc

    def digest(self, snapshot: Snapshot) -> str:
        """这份冻结载体的 sha256（`#33` 的装配期比对用它，另一侧是工作区里那份）。"""
        return hashlib.sha256(self.read(snapshot)).hexdigest()

    def verify(self, snapshot: Snapshot) -> bool:
        """Re-hash the frozen carrier against its name. Content addressing, checked."""
        try:
            body = snapshot.body_path.read_bytes()
        except OSError:
            return False
        return snapshot.body_path.name.startswith(hashlib.sha256(body).hexdigest()[:32])
