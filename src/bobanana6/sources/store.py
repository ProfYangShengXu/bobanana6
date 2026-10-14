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
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..core.model import Snapshot

__all__ = ["SnapshotStore", "SourceError", "SourceLedger"]

#: 台账文件名（与冻结件同目录 —— 冻结件是内容寻址的，台账是来源寻址的，两者并存）
LEDGER_NAME = "sources.index.json"


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


@dataclass(frozen=True)
class SourceLedger:
    """哪个来源已经抓过了 —— 「同一个来源不重复抓」的台账。

    ★ 为什么不并进 `SnapshotStore`：它们回答的是**两个问题**。
      store 答"同样的字节存了几份"（内容寻址，纯的，跟时间无关）；
      ledger 答"这个来源抓过了吗"（一份**状态**，随时间变）。
      并在一起 = 一个对象两副职责；而且**内容寻址会把它盖住**：
      字节相同时磁盘上只有一份文件，看目录完全看不出"抓过两次"。

    ★ 只记**冻结点在哪**，不记内容 —— 内容仍旧只有 store 一份（不复制数据）。
    ★★ fail-closed 四条，全部倒向"当作没抓过 ⇒ 去抓"：
         台账文件缺失 / 读不出 / 不是字典 / 那条记的冻结件不在了
       ⇒ 一律 `None`（当作没抓过）。
       方向安全：宁可多抓一次拿到真东西，也不要复用一个指不到的空记录。

    ★ 复用的是【那份冻结件】本身，连同它的 `captured_at` ——
      所以第二次跑看到的是第一次那份**材料**，这正是可复现要的。
    """

    root: Path

    @property
    def path(self) -> Path:
        return self.root / LEDGER_NAME

    def _read(self) -> dict[str, dict[str, str]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict[str, str]] = {}
        for ref, entry in raw.items():
            if isinstance(entry, dict) and isinstance(entry.get("body"), str):
                out[str(ref)] = {k: str(v) for k, v in entry.items() if isinstance(v, (str, int, float))}
        return out

    def recall(self, ref: str) -> Snapshot | None:
        """这个来源抓过了吗 —— 抓过就把那份冻结件给回来，没抓过给 `None`。"""
        entry = self._read().get(ref)
        if not entry:
            return None
        body = self.root / entry["body"]
        if not body.is_file():
            # ★ 台账指向的冻结件没了 ⇒ 当作没抓过，而不是回一份读不出内容的记录
            return None
        return Snapshot(
            ref=ref,
            body_path=body,
            captured_at=entry.get("captured_at", ""),  # blame: fail-closed — 台账自己写的，缺了也只是没时间戳，不影响"抓过了"这个事实
            source_kind=entry.get("source_kind", ""),  # blame: fail-closed — 同上，分层信息缺失不改变复用决定
        )

    def remember(self, snapshot: Snapshot) -> None:
        """把"这个来源冻在这儿了"记下来 —— 下一次跑据此复用，不再打网络。"""
        entries = self._read()
        entries[snapshot.ref] = {
            "body": snapshot.body_path.name,
            "captured_at": snapshot.captured_at,
            "source_kind": snapshot.source_kind,
        }
        self.root.mkdir(parents=True, exist_ok=True)
        staging = self.path.with_name(self.path.name + ".tmp")
        staging.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        # ★ 原子替换：中途挂掉留下的只会是 .tmp，台账本身要么是旧的要么是新的，
        #   绝不会出现"读到半条" ⇒ 不会把某个来源误判成没抓过或抓过。
        staging.replace(self.path)

    def entries(self) -> dict[str, dict[str, str]]:
        """整本台账（观测用：哪个来源、什么时候抓的、算哪一类）。"""
        return self._read()
