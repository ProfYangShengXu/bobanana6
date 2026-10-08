"""`provenance` -- can every point be traced back to a snapshot?

Missing rate above the limit is a failure. A block with no points at all is
also a failure: "nothing to trace" is not the same as "everything traced".

★ A11: when the outside reader has handed over the frozen bodies, a citation is
also checked for *content* -- the cited row must exist and the value must match
it. The name being in the registry proves nothing about whether the reference is
real, and the fabricated answer this gap was found by (30 rows citing a 3-row
snapshot) passes the name check with coverage 1.0.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..model import Artifact, Candidate, Constraint, Ontology, SnapshotBody, is_missing
from ..ontology import base_unit, convert_value, map_field, split_quantity
from ..provenance import row_of, same_value, validate_points
from .base import CheckContext, CheckOutcome, artifact_for, fail, ok

KIND = "provenance"


def required_refs(artifact: Artifact) -> tuple[str, ...]:
    """The set of traceable points a deliverable is expected to expose."""
    refs = [f"{artifact.name}:row={index}" for index in range(artifact.row_count)]
    if artifact.text is not None:
        refs.extend(f"{artifact.name}:line={index}" for index, line in enumerate(artifact.text.splitlines()) if line)
    return tuple(refs)


def same_quantity(left: object, right: object, concept: str, ontology: Ontology | None) -> bool:
    """Whether two values are the same *quantity*, allowing a declared conversion.

    ★ ①: a worker that understood its source may hand back the same quantity in
    the field's own unit (``"250 千元"`` read, ``250000.0`` 元 answered). The
    conversion is not a guess -- the factor and the base come from the ontology's
    declared ``Unit`` table (``实现规格 §5.0.1`` 判据②③), and a value that is not
    the same quantity is still a mismatch: dropping the unit without converting
    (250 instead of 250000) stays caught.
    """
    if same_value(left, right):
        return True
    if ontology is None:
        return False
    target = base_unit(concept, ontology)
    if target is None:
        return False
    left_quantity = split_quantity(left) if isinstance(left, str) else None
    right_quantity = split_quantity(right) if isinstance(right, str) else None

    def in_base(value: object, quantity: tuple[float, str] | None) -> float | None:
        if quantity is not None:
            number, symbol = quantity
        else:
            try:
                number, symbol = float(str(value)), target.symbol
            except (TypeError, ValueError):
                return None
        try:
            return convert_value(number, symbol, target.symbol, ontology)
        except (KeyError, ValueError):
            return None

    left_base = in_base(left, left_quantity)
    right_base = in_base(right, right_quantity)
    return left_base is not None and left_base == right_base


#: 一列与它所指的来源行之间**声明的对账关系**（`约束 spec.reconcile`）。
#:   eq     逐值相同（缺省，收紧）
#:   none   ★ 这一列是**算出来的**（第二道工序）—— 明说"这里不做逐值比对"
#:   sum / count / min / max / mean   与**同组来源行**的聚合值比
RECONCILE_OPS = ("eq", "none", "sum", "count", "min", "max", "mean")

#: 聚合类 op —— 要按 `group_by` 选出一组来源行再比。
_AGGREGATES = ("sum", "count", "min", "max", "mean")


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _aggregate(rows: tuple[Mapping[str, Any], ...], op: str, column: str) -> float | int | None:
    """把选中的来源行聚成一个值（`count` 数行，其余按列）。"""
    if op == "count":
        return len(rows)
    numbers = [_number(row.get(column)) for row in rows]
    present = [number for number in numbers if number is not None]
    if not present:
        return None
    if op == "sum":
        return sum(present)
    if op == "min":
        return min(present)
    if op == "max":
        return max(present)
    return sum(present) / len(present)


#: 缺省对账 = 逐值相同（第一道工序的搬运语义）。
DEFAULT_RECONCILE_OP = "eq"


def normalized_reconcile(
    reconcile: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """★ 声明式对账的**唯一读取口**（`spec.reconcile` → `{列名: 规则}`）。

    生产侧（继承引用）与判定侧（`value_mismatches`）都走这里 ——
    两份读取口就是"判定逻辑的第二个副本"，会漂。
    """
    declared: dict[str, Mapping[str, Any]] = {}
    for entry in reconcile:
        if not isinstance(entry, Mapping):
            continue
        column = entry.get("column")
        if not isinstance(column, str) or not column:
            continue
        declared[column] = entry
    return declared


def key_columns(
    reconcile: Sequence[Mapping[str, Any]], shared: Sequence[str] = ()
) -> tuple[str, ...]:
    """★ 「哪些列必须逐字相同」——**从声明算出来**，不是硬编码。

    · 显式声明 `op: eq` 的列
    · 没被声明为算出来的 / 聚合的列（`shared` 是两边都有的列）
    """
    declared = normalized_reconcile(reconcile)
    if declared:
        explicit = tuple(sorted(c for c, rule in declared.items() if rule.get("op") in (None, "eq")))
        if explicit:
            return explicit
        # 只声明了聚合/派生列 ⇒ 其余共有列按逐字比（保守方向）
        derived = {c for c, rule in declared.items() if rule.get("op") in ("none", *_AGGREGATES)}
        return tuple(sorted(c for c in shared if c not in derived))
    return tuple(sorted(shared))


def aggregate_over(rows: Sequence[Mapping[str, Any]], op: str, column: str) -> float | int | None:
    """把一组行聚成一个值（`count` 数行，其余按列）—— 判定侧与生产侧**共用**这一个。"""
    return _aggregate(tuple(rows), op, column)


def reconcile_holes(reconcile: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """★ 被声明为**算出来的**列（`op: none`）—— 这里**没有**内容检查它们。

    这些列必须由别处（内容层的 invariants / 外部 checker / 生成断言）去验；
    报告要**说得出有几个洞**，不许让"内部一致"冒充"内容正确"。
    """
    return tuple(
        sorted(str(entry.get("column")) for entry in reconcile if entry.get("op") == "none")
    )


def value_mismatches(
    artifact: Artifact,
    candidate: Candidate,
    bodies: dict[str, SnapshotBody] | None = None,
    ontology: Ontology | None = None,
    reconcile: Sequence[Mapping[str, Any]] = (),
) -> tuple[str, ...]:
    """Values that do not match the row they claim to come from.

    ★★ 规格 ⑤：**按声明的对账关系比对，而不是逐值相同**。

    逐值相同是**第一道工序的搬运语义**（读块照抄来源）。第二道工序（聚合 / 再加工）
    交的是**算出来的新行**，逐值相同必然挂 —— 所以"该对得上"从硬编码变成契约可声明：

        spec.reconcile: [{column: notional, op: sum, group_by: [manager], tol: 1e-6},
                         {column: manager,  op: eq},
                         {column: hhi,      op: none}]      # 算出来的，这里不逐值比

    没有声明 ⇒ 逐值相同（与改前逐字一致，第一道工序的读数不动）。
    """
    declared = {str(entry.get("column")): entry for entry in reconcile}
    facts: list[str] = []
    for provenance in sorted(candidate.provenance, key=lambda p: (p.value_ref, p.snapshot_ref)):
        body = (bodies or {}).get(provenance.snapshot_ref)
        index = row_of(provenance.value_ref)
        if body is None or index is None or index >= artifact.row_count:
            continue
        source_row = provenance.source_row if provenance.source_row is not None else index
        snapshot_row = body.row(source_row)
        if snapshot_row is None:
            continue  # a missing row is the chain's business, not a value mismatch
        row = artifact.rows[index]
        for column in sorted(set(row) & set(snapshot_row)):
            value = row[column]
            if is_missing(value):
                continue
            rule = declared.get(column)
            op = str(rule.get("op", "eq")) if rule is not None else "eq"  # blame: fail-closed — 缺 op 即逐值相同（第一道工序的搬运语义），方向收紧
            if op == "none":
                continue  # ★ 声明为算出来的列：这里不判（洞由内容层补，见 reconcile_holes）
            if op in _AGGREGATES:
                keys = tuple(str(name) for name in (rule or {}).get("group_by", ()) or ())  # blame: fail-closed — 缺 group_by 即整份快照当一组；选得更宽只会更容易抓到不一致，方向保守
                over = str((rule or {}).get("over") or column)
                selected = tuple(
                    body_row
                    for body_row in body.rows
                    if all(body_row.get(name) == row.get(name) for name in keys)
                )
                expected = _aggregate(selected, op, over)
                tol = float((rule or {}).get("tol", 1e-9))  # blame: fail-closed — 缺 tol 即 1e-9（几乎不放宽），方向收紧
                observed = _number(value)
                agrees = (
                    expected is not None
                    and observed is not None
                    and abs(observed - float(expected)) <= tol
                )
                if not agrees:
                    facts.append(
                        f"row={index} col={column} value {value!r} does not match the declared "
                        f"'{op}' over {len(selected)} source rows of {provenance.snapshot_ref!r} "
                        f"grouped by {keys!r}: {expected!r}"
                    )
                continue
            if not same_quantity(value, snapshot_row[column], map_field(column, ontology) if ontology else column, ontology):
                facts.append(
                    f"row={index} col={column} value {value!r} does not match snapshot "
                    f"{provenance.snapshot_ref!r} row {source_row}: {snapshot_row[column]!r}"
                )
    return tuple(facts)


def check(constraint: Constraint, candidate: Candidate, context: CheckContext) -> CheckOutcome:
    artifact = artifact_for(constraint, candidate)
    if artifact is None:
        return fail(constraint, f"artifact for target {constraint.target!r} not found")
    minimum = constraint.spec.get("min_coverage", context.limits.provenance_min_coverage)
    if not isinstance(minimum, (int, float)) or not 0.0 <= minimum <= 1.0:
        return fail(constraint, f"min_coverage must be in [0, 1], got {minimum!r}")

    points = required_refs(artifact)
    if not points:
        return fail(constraint, f"{artifact.name!r} exposes no traceable points")

    bodies = dict(context.snapshot_bodies)
    report = validate_points(points, candidate.provenance, context.snapshots, bodies)
    coverage = report.coverage

    content = tuple(break_.detail for break_ in report.breaks if break_.detail)
    # ★ ⑤：对账关系从**契约**读（`spec.reconcile`），不再硬编码"逐值相同"。
    rules = constraint.spec.get("reconcile", [])  # blame: fail-closed — 缺 reconcile 即不做任何逐值放宽（与改前逐字一致）
    if not isinstance(rules, (list, tuple)):
        return fail(constraint, "spec.reconcile must be a list of {column, op, ...}")
    mismatches = value_mismatches(artifact, candidate, bodies, context.ontology, rules)
    # ★ 声明为"算出来的"列 = 这里**没有**内容检查它。说得出有几个洞，不许让内部一致冒充内容正确。
    holes = reconcile_holes(rules)

    if coverage < minimum or content or mismatches:
        detail = f"coverage {coverage:.4f} < required {minimum}"
        if coverage >= minimum:
            detail = f"coverage {coverage:.4f} reaches {minimum}, but the citations do not replay"
        unknown = tuple(
            sorted(
                {
                    provenance.snapshot_ref
                    for provenance in candidate.provenance
                    if provenance.snapshot_ref not in context.snapshots
                }
            )
        )
        if unknown:
            detail += f"; unknown snapshots {unknown!r}"
        if content:
            detail += f"; {content[0]}"
        if mismatches:
            detail += f"; {mismatches[0]}"
        if holes:
            detail += f"; declared-derived columns were NOT content-checked here: {list(holes)!r}"
        return fail(constraint, detail)
    checked = f"coverage {coverage:.4f} >= required {minimum}"
    if holes:
        # ★ 过了也不许含糊：这一格只证明"引用能重放"，没证明"算得对"。
        checked += f"; declared-derived columns were NOT content-checked here: {list(holes)!r}"
    return ok(constraint, checked)
