"""报告：SOURCES.md / CONFLICTS.md / ONTOLOGY-MAP.md / summary.json。

它们是 `core/model.py` 里 `external:report` 字段的实现读者 —— 没有这一层，
那些字段只是装饰品（`实现规格 §5.0.2`）。
"""

from __future__ import annotations

from .builders import (
    ReportBundle,
    build_reports,
    render_conflicts,
    render_ontology_map,
    render_sources,
    render_summary,
)

__all__ = [
    "ReportBundle",
    "build_reports",
    "render_conflicts",
    "render_ontology_map",
    "render_sources",
    "render_summary",
]
