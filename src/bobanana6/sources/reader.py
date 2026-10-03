"""读快照载体：``core/ports.py::SnapshotReader`` 的实作（易变侧）。

★ A11：判定「引用是不是真的」是判定语义，属于 `core/`；但**打开载体**不是
`core/` 能做的事（`AGENTS.md §1.1` 零 I/O）。所以这里把冻结的载体读成 core 的
数据形状（:class:`~bobanana6.core.model.SnapshotBody`）再交进去 ——
端口定义在稳定侧、实作在外侧（`AGENTS.md §1.3`）。
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..core.model import Column, Snapshot, SnapshotBody
from .store import SnapshotStore

__all__ = ["SnapshotFileReader", "rows_from_csv", "rows_from_json"]


def rows_from_csv(text: str) -> tuple[tuple[Column, ...], tuple[Mapping[str, Any], ...]]:
    reader = csv.DictReader(io.StringIO(text))
    names = list(reader.fieldnames or [])
    rows = tuple(dict(row) for row in reader)
    return tuple(Column(name=name, dtype="str") for name in names), rows


def rows_from_json(text: str) -> tuple[tuple[Column, ...], tuple[Mapping[str, Any], ...]]:
    document = json.loads(text)
    if isinstance(document, Mapping):
        document = [document]
    if not isinstance(document, list):
        raise TypeError("a JSON snapshot must be an object or a list of objects")
    rows = tuple(dict(row) for row in document if isinstance(row, Mapping))
    names = sorted({key for row in rows for key in row})
    return tuple(Column(name=name, dtype="str") for name in names), rows


@dataclass(frozen=True)
class SnapshotFileReader:
    """Read a frozen snapshot body into the rows the kernel may judge."""

    store: SnapshotStore

    def body(self, snapshot: Snapshot) -> SnapshotBody:
        text = self.store.read(snapshot).decode("utf-8", errors="replace")
        suffix = snapshot.body_path.suffix.lower()
        if suffix == ".csv":
            columns, rows = rows_from_csv(text)
        elif suffix == ".json":
            columns, rows = rows_from_json(text)
        else:
            columns, rows = (Column(name="text", dtype="str"),), ({"text": text},)
        return SnapshotBody(ref=snapshot.ref, columns=columns, rows=rows)
