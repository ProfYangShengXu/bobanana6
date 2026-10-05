"""Turn model output into core values -- the only place that does.

The worker loop never sees an ``Artifact`` or a ``Candidate`` from outside: it
sees JSON text and tool calls. This module is the boundary that parses that text
**strictly**. A malformed answer must fail visibly (the loop classifies it and
retries) rather than degrade into a half-built candidate, because a candidate
that quietly loses its provenance would be admitted by every check that trusts
it.

Only the parsing boundary lives here; no constraint is judged here.

Structured output is built in three layers here (lec9:237 -- *never assume the
first answer is right*):

  * **before** -- :func:`candidate_response_format` asks the service for the shape;
  * **during** -- the schema itself (``CANDIDATE_SCHEMA``) is the tightening;
  * **after** -- a rejected answer raises :class:`CandidateFormatError` carrying
    *how to fix it*, which the loop feeds back to the model.

The three are complementary, not alternatives: layer 3 is the only one that can
say anything at all when a service ignores layers 1-2.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from ..agentloop.errors import FailoverReason
from ..agentloop.events import EventLog
from ..agentloop.gate import GateVerdict
from ..agentloop.loop import LoopIO, LoopState, TurnProposal
from ..agentloop.tools import ToolCall, ToolResult
from ..core.model import (
    Artifact,
    Budget,
    Candidate,
    Claim,
    Column,
    ModelResponse,
    Provenance,
    Subtask,
    TaskContract,
    canonical_row,
    content_digest,
)
from ..core.ports import LLMClient, ToolSet
from ..services.checks import BlockJudge
from ..services.registry import ToolRegistry
from .answer_contract import (
    ANSWER_CONTRACT,
    candidate_schema,
    contract_field,
    render_files_requirement,
    render_prompt_contract,
    render_ref_domain,
    render_reference_note,
    render_shape,
)

__all__ = [
    "CANDIDATE_SCHEMA",
    "EXPECTED_SHAPE",
    "CandidateFormatError",
    "LoopIOImpl",
    "OutputLimitUnitError",
    "ResponseFormatSupport",
    "candidate_response_format",
    "expected_shape",
    "model_output_cap",
    "parse_candidate",
    "rejects_response_format",
    "tool_schemas",
]


def _declined(text: str) -> bool:
    try:
        document = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(document, Mapping) and bool(document.get("declined"))


def _decline_reason(text: str) -> str:
    try:
        document = json.loads(text)
    except (json.JSONDecodeError, TypeError):  # pragma: no cover - guarded by _declined
        return ""
    if isinstance(document, Mapping):
        return str(document.get("reason") or "")
    return ""


def _name_of(calls: Sequence[ToolCall], call_id: str) -> str:
    for call in calls:
        if call.call_id == call_id:
            return call.name
    return ""


#: ★ `#42`: 答案形状**不再是手写字符串** —— 它是 `answer_contract.py::render_shape()` 的
#: 渲染结果（取值域 / 粒度 / 例子都在那个模块里，六处渲染点共用同一份）。下面两个常量与
#: `expected_shape()` / 系统提示 / `reference_note` / `files` 要求，全部由它派生。
EXPECTED_SHAPE = render_shape(ANSWER_CONTRACT)

#: ★ ``json_schema`` 形式，由契约的结构化形态生成（`answer_contract.py::candidate_schema`）。
#: 故意**不是** ``strict: true``：strict 要求每个属性都必填，那会逼模型在每次答案里编
#: ``hint`` / ``text`` / ``claims``。真正要紧的那一处收紧（``columns`` / ``rows`` 的形状、
#: ``ref`` 必填）都在契约里。**请求描述答案的方式必须与判定答案的方式一致**。
CANDIDATE_SCHEMA: Mapping[str, Any] = dict(candidate_schema(ANSWER_CONTRACT))


def candidate_response_format() -> Mapping[str, Any]:
    """The ``response_format`` payload for one answer (the *before* layer)."""
    return {"type": "json_schema", "json_schema": {"name": "candidate", "schema": dict(CANDIDATE_SCHEMA)}}


#: A schema/structured-output request the service does not understand.
_SCHEMA_MARKERS = ("response_format", "json_schema", "json schema", "structured output")
_REJECTION_MARKERS = ("not supported", "unsupported", "unrecognized", "unknown", "unexpected", "invalid")


def rejects_response_format(error: BaseException) -> bool:
    """Whether a failure says "I do not understand structured output".

    Only client-shaped statuses count: a 500 is not a capability answer, and a
    timeout says nothing about the parameter. Read off the exception rather than
    from an adapter type so this module keeps talking to the port only.
    """
    status = getattr(error, "status_code", None)
    if not isinstance(status, int) or isinstance(status, bool) or status not in (400, 404, 415, 422):
        return False
    text = str(error).lower()
    return any(marker in text for marker in _SCHEMA_MARKERS) and any(marker in text for marker in _REJECTION_MARKERS)


@dataclass
class ResponseFormatSupport:
    """What this service has accepted so far, learned once per run.

    Capability negotiation must not become a hard failure, and it must not become a
    per-call probe either: the first refusal turns the layer off for the rest of the
    run, so a service that ignores ``response_format`` costs exactly one extra call.
    """

    enabled: bool = True
    refused: bool = False


#: The one unit a model call's output cap can be counted in.
TOKEN_UNIT = "tokens"


class OutputLimitUnitError(ValueError):
    """The declared output allowance cannot cap a model call.

    ``Budget.max_output`` is a declaration and a declaration has to have
    consequences (``架构设计 §6.9``): declaring 8192 and spending 512 means nobody
    honours it. ``max_tokens`` is counted in tokens and in nothing else -- a
    byte-denominated allowance has no defined conversion -- so a non-token unit is
    refused instead of being silently reinterpreted.

    The request's shape is what has to change, so it declares ``format_error``
    (which the loop routes to ``fix_constraint``) and carries no answer-shape
    feedback: there is nothing to tell the model, the operator has to fix the
    budget.
    """

    reason = "format_error"


def model_output_cap(budget: Budget) -> int:
    """The token cap this model call may use, read off the declared allowance."""
    if budget.max_output_unit != TOKEN_UNIT:
        raise OutputLimitUnitError(
            f"the output allowance is declared in {budget.max_output_unit!r}, and a model call "
            f"can only be capped in {TOKEN_UNIT!r}; declare the unit or the cap it produces"
        )
    if budget.max_output <= 0:
        raise OutputLimitUnitError(f"Budget.max_output is {budget.max_output}; a model call needs a positive cap")
    return budget.max_output


class CandidateFormatError(ValueError):
    """The model's answer could not be read as a candidate.

    Carries two *different* representations on purpose:

      * the message -- prose for logs, reports and ``state.errors``;
      * :meth:`feedback` -- the structured fix the model is told, rendered by
        ``recovery.reformat_message`` (code + problem + what was sent + the shape).
    """

    #: ★ Declared: retrying the identical request cannot fix a malformed answer.
    #: Without this the message was matched against a word list, and
    #: "the answer is not JSON" matched nothing -> ``unknown`` -> ``retry_local``,
    #: which burned the budget re-sending the same request.
    reason = "format_error"

    def __init__(
        self,
        problem: str,
        *,
        code: str = "invalid_answer",
        got: str = "",
        expected: str = EXPECTED_SHAPE,
        reason: str = "",
    ) -> None:
        super().__init__(problem)
        # ★ `#41`: 「答案读不出来」是一类，但**为什么**读不出来决定了该走哪个动作：
        #   撞上限的截断（`truncated` → reshard）与"模型在转圈"（`repetition` → reshard）
        #   都和形状错误（`format_error` → fix_constraint）不是一回事。生产者知道是哪一种，
        #   就由它逐条声明（实例属性覆盖类属性 `format_error`；缺省仍然沿用类属性）。
        if reason:
            self.reason = reason
        self._feedback: dict[str, Any] = {"code": code, "problem": problem, "got": got, "expected": expected}

    def feedback(self) -> Mapping[str, Any]:
        """How to fix it -- read by ``agentloop.recovery.declared_feedback``."""
        return dict(self._feedback)


def tool_schemas(toolset: ToolSet) -> list[dict[str, Any]]:
    """The advertised tool surface, in the shape a model expects."""
    schemas: list[dict[str, Any]] = []
    for spec in toolset.specs():
        properties = {name: {"type": "string"} for name in spec.params}
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.returns or spec.name,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": list(spec.params),
                        "additionalProperties": False,
                    },
                },
            }
        )
    return schemas


#: ``reject(code, problem)`` raises with the fix attached.
Reject = Callable[[str, str], "NoReturn"]


def _columns(raw: Any, rows: Sequence[Mapping[str, Any]], reject: Reject) -> tuple[Column, ...]:
    if raw is None:
        names = sorted({key for row in rows for key in row})
        return tuple(Column(name=name, dtype="str") for name in names)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        reject("columns_not_a_list", "'columns' is present but it is not an array")
    columns: list[Column] = []
    for item in raw:
        if isinstance(item, str):
            columns.append(Column(name=item, dtype="str"))
        elif isinstance(item, Mapping) and isinstance(item.get("name"), str):
            columns.append(
                Column(
                    name=str(item["name"]),
                    dtype=str(item.get("dtype") or "str"),
                    nullable=bool(item.get("nullable", True)),
                    # ★ A10: a block that knows the unit of what it produced says so
                    #   here; leaving it out is recorded as "not established".
                    unit=str(item.get("unit") or ""),
                )
            )
        else:
            reject("column_not_an_object", f"column entry {item!r} is neither a name nor a {{name, dtype}} object")
    return tuple(columns)


def _claims(raw: Any, *, snapshot_ref: str, locator: str, reject: Reject) -> tuple[Claim, ...]:
    if not raw:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        reject("claims_not_a_list", "'claims' is present but it is not an array")
    claims: list[Claim] = []
    for item in raw:
        if not isinstance(item, Mapping):
            reject("claim_not_an_object", f"claim entry {item!r} is not an object")
        name = item.get("field")
        source = item.get("source_id")
        if not isinstance(name, str) or not name or not isinstance(source, str) or not source:
            reject("claim_missing_keys", f"claim entry {item!r} needs a non-empty 'field' and 'source_id'")
        claims.append(
            Claim(
                field=name,
                value=item.get("value"),
                source_id=source,
                provenance=Provenance(value_ref=f"{name}:{source}", snapshot_ref=snapshot_ref, locator=locator),
            )
        )
    return tuple(claims)


#: ★ `#41`: 「重复主导」的判据常量（阈值由 253 条归档输出实测定，见 `.bobanana6/b41/报告.md`）。
#: 判据 = **最长的"周期 P 连续重复段"占被检段的比例**（对齐无关：窗口法会在重复单元长度
#: 与窗口不整除时漏判 —— 实测一条周期 37 的循环窗口占比只有 0.059，这一条抓得住）。
REPETITION_MIN_CHARS = 1000  # 短输出不参与："把整个预算花光"这件事本身就意味着长
REPETITION_TAIL_CHARS = 2048  # 只看尾部：退化填充/循环堆在尾部（前面通常还有真内容）
REPETITION_MAX_PERIOD = 128  # 循环单元周期上限（代价 2048×128 ≈ 26 万次比较，~25ms）
#: 实测：正常输出最大 0.144，退化样本 0.953 / 0.996 / 1.000 ⇒ 取几何中点 0.40（两侧各 ~2.5×）。
REPETITION_RUN_SHARE = 0.40
#: 只作**读数**（用户量过的那个口径）：60 字符非重叠窗口里最常出现的那个占多少。
REPETITION_WINDOW = 60


def repetition_readings(text: str) -> dict[str, float]:
    """把"重复"量成两个数：周期重复段占比（判据）+ 窗口重复率（读数，可对账）。"""
    segment = text[-REPETITION_TAIL_CHARS:]
    best = 0
    period = 1
    if len(segment) >= 2:
        for candidate in range(1, min(REPETITION_MAX_PERIOD, len(segment) - 1) + 1):
            run = 1
            for index in range(candidate, len(segment)):
                run = run + 1 if segment[index] == segment[index - candidate] else 1
                if run > best:
                    best, period = run, candidate
    windows = [
        segment[index : index + REPETITION_WINDOW]
        for index in range(0, max(len(segment) - REPETITION_WINDOW + 1, 0), REPETITION_WINDOW)
    ]
    return {
        "chars": len(text),
        "checked_chars": len(segment),
        "window_share": (max(Counter(windows).values()) / len(windows)) if windows else 0.0,
        "period": period,
        "periodic_run_share": (best / len(segment)) if segment else 0.0,
        "whitespace_share": (sum(1 for char in text if char.isspace()) / len(text)) if text else 0.0,
    }


def repetition_dominated(text: str) -> bool:
    """★ `#41`: 这一段输出是不是"模型在转圈"（而不是"额度真的不够、内容还有"）。"""
    if len(text) < REPETITION_MIN_CHARS:
        return False
    return repetition_readings(text)["periodic_run_share"] >= REPETITION_RUN_SHARE


def _truncation_reason(text: str, *, hit_output_cap: bool) -> str:
    """把"截断"归到真相那一类：``repetition`` / ``truncated`` / 空串（沿用 ``format_error``）。

    · 重复主导 ⇒ ``repetition``：接着续还是重复，该回切分器（`REASON_ACTION` 里本来就是
      `RESHARD_TASK`），不该再发一次同样形状的请求；
    · 没重复、但端点的用量说到上限了 ⇒ ``truncated``：是我们自己的输出额度不够；
    · 没重复、也没撞上限 ⇒ 模型自己停在半途（`#38` 的 completion_tokens=107 那种）——
      那仍然是"形状没写对"，沿用 `format_error`（fix_constraint 会把形状喂回去）。
    """
    if repetition_dominated(text):
        # ★ 用词表的成员而不是字符串字面量：写错名字 Python 当场报错，
        #   不会像字符串那样悄悄退回兜底分类（那正是词表守卫要防的事）。
        return FailoverReason.REPETITION
    return FailoverReason.TRUNCATED if hit_output_cap else ""


def _hit_output_cap(usage: Mapping[str, Any] | None, cap: int) -> bool:
    """端点报的用量说没说到上限。**没报用量就不声称撞线**（宁可少判一类，不编事实）。"""
    if not cap:
        return False
    return int((usage or {}).get("completion_tokens") or 0) >= cap


def _describe_json_failure(text: str, exc: Exception, *, output_cap: int | None) -> tuple[str, str]:
    """Say which of the three breakages happened: empty, not JSON, or cut off."""
    head = text.lstrip()
    if not head:
        return "empty", "the answer is empty"
    if head[0] != "{":
        return "not_json", f"the first character is {head[0]!r}, not '{{' (JSON parse failed: {exc})"
    if not head.rstrip().endswith("}"):
        cap = f"; the output allowance is {output_cap} tokens" if output_cap else ""
        problem = (
            "the answer was cut off before the JSON object closed "
            f"(it starts with '{{' but does not end with '}}'){cap} (JSON parse failed: {exc})"
        )
        return "truncated_answer", problem
    return "malformed_json", f"the answer starts and ends like JSON but is not valid: {exc}"


def expected_shape(authorized_sources: Sequence[str]) -> str:
    """The required shape, plus *which* snapshots may be cited when we know them.

    ★ `#42`: 后半句也是渲染 —— `ref` 的**取值域**由契约给（`render_ref_domain`），
    所以"名单变了、这句话忘了改"这种漂移不会再有。
    """
    if not authorized_sources:
        return EXPECTED_SHAPE
    return f"{EXPECTED_SHAPE}{render_ref_domain(authorized_sources, ANSWER_CONTRACT)}"


def _row_agreement(sent: Mapping[str, Any], pooled: Mapping[str, Any]) -> tuple[int, int, dict[str, Any]]:
    """守卫自己怎么比的，观测就怎么报：两边先 `canonical_row`，再逐键 `same_value`。

    ★ 观测不改判定 —— 它只是把 `_same_row` 的那次比较**摊开**给人看。
    """
    from ..core.provenance import same_value

    one, other = canonical_row(sent), canonical_row(pooled)
    keys = sorted(set(one) | set(other))
    equal = 0
    difference: dict[str, Any] = {}
    for key in keys:
        left, right = one.get(key), other.get(key)
        if same_value(left, right):
            equal += 1
        else:
            difference[key] = {"sent": left, "pool": right}
    return equal, len(keys), difference


def _pool_diagnosis(parts: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """池子为空时**必须能回答"为什么是 0"** —— 三种修法不同的原因分开报。

    (a) 输入的 parts 里压根没有 rows        -> ``no_rows_at_all``
    (b) 有 rows 但没有 provenance            -> ``rows_without_provenance``
    (c) 有 provenance 但 value_ref 对不上    -> ``value_ref_mismatch``
    池子非空（行对不上是真对不上）           -> ``pool_non_empty``
    """
    entries: list[dict[str, Any]] = []
    for name in sorted(parts):
        payload = parts[name]
        if not isinstance(payload, Mapping):
            entries.append({"name": name, "verdict": "not_a_payload"})
            continue
        rows = payload.get("rows") or []
        points = [point for point in (payload.get("provenance") or []) if isinstance(point, Mapping)]
        expected = [f"{name}:row={index}" for index in range(len(rows))]
        seen = [str(point.get("value_ref")) for point in points]
        matched_rows = [ref for ref in expected if ref in set(seen)]
        if not rows:
            verdict = "no_rows"
        elif not points:
            verdict = "rows_without_provenance"
        elif not matched_rows:
            verdict = "value_ref_mismatch"
        else:
            verdict = "ok"
        entries.append(
            {
                "name": name,
                "verdict": verdict,
                "rows": len(rows),
                "provenance_points": len(points),
                "matched_rows": len(matched_rows),
                "expected_value_refs": expected[:5],
                "seen_value_refs": seen[:5],
            }
        )
    verdicts = {entry["verdict"] for entry in entries}
    if not entries:
        overall = "no_parts"
    elif verdicts == {"no_rows"}:
        overall = "no_rows_at_all"
    elif "rows_without_provenance" in verdicts:
        overall = "rows_without_provenance"
    elif "value_ref_mismatch" in verdicts:
        overall = "value_ref_mismatch"
    elif verdicts == {"ok"}:
        overall = "pool_non_empty"
    else:
        # 多个部件诊断不同：别挑一个代表（那会把"混合"说成"单一原因"）
        overall = "mixed:" + "+".join(sorted(verdicts))
    return {"overall": overall, "parts": entries}


def _guard_observation(
    *,
    subtask_id: str,
    artifact_name: str,
    row_index: int,
    sent_row: Mapping[str, Any],
    pool: Sequence[tuple[str, int, Mapping[str, Any], Mapping[str, Any]]],
    matched: Sequence[Any],
    parts: Mapping[str, Mapping[str, Any]],
    epoch: Callable[[], float],
) -> dict[str, Any]:
    """把"池子 + 交上来的行 + 逐键差异"攒成一条可机读记录（**只给观察用**）。"""
    nearest: dict[str, Any] | None = None
    for name, input_index, pooled_row, point in pool:
        equal, total, difference = _row_agreement(sent_row, pooled_row)
        if nearest is None or equal > nearest["keys_equal"]:
            nearest = {
                "input_name": name,
                "input_row_index": input_index,
                "value_ref": str(point.get("value_ref") or ""),
                "keys_equal": equal,
                "keys_total": total,
                "difference": difference,
            }
    return {
        "block": subtask_id,
        "output": artifact_name,
        "row_index": row_index,
        "pool_size": len(pool),
        "pool": [dict(row) for _name, _index, row, _point in pool],
        "pool_refs": [
            {
                "input_name": name,
                "input_row_index": input_index,
                "value_ref": str(point.get("value_ref") or ""),
                "snapshot_ref": str(point.get("snapshot_ref") or ""),
                "source_row": point.get("source_row"),
            }
            for name, input_index, _row, point in pool
        ],
        "pool_diagnosis": _pool_diagnosis(parts),
        "sent_row": dict(sent_row),
        "nearest": nearest,
        # 走到这条分支就是"一行都没匹配上"；留着它是为了将来守卫有别的分支时口径一致
        "all_pool_differs": not matched,
        "at": epoch(),
    }


def _inherited_provenance(
    artifact: Artifact,
    parts: Mapping[str, Mapping[str, Any]],
    reject: Reject,
    *,
    observe: Callable[[Mapping[str, Any]], None] | None = None,
    subtask_id: str = "",
    epoch: Callable[[], float] = time.monotonic,
) -> tuple[Provenance, ...]:
    """Cite a merge block's rows back to the rows it was handed (A11).

    The block read no snapshot itself, so "row n came from source X" is not its
    claim to make. Every output row is matched against the input rows it
    received, and the matched input's citation is inherited. A row that matches
    nothing is refused loudly: it is either fabricated or transformed, and the
    system cannot yet tell those apart -- pretending would make the chain lie.
    """

    pool: list[tuple[str, int, Mapping[str, Any], Mapping[str, Any]]] = []
    for name in sorted(parts):
        payload = parts[name]
        if not isinstance(payload, Mapping):
            continue
        raw_points = payload.get("provenance")
        by_ref = {
            str(point.get("value_ref")): point
            for point in raw_points or []
            if isinstance(point, Mapping)
        }
        rows = payload.get("rows")
        for index, row in enumerate(rows or []):
            point = by_ref.get(f"{name}:row={index}")
            if isinstance(row, Mapping) and isinstance(point, Mapping):
                # ★ `#43`: 池子元组带上**输入名**（观测要按名字报，判定不用它）
                pool.append((name, index, row, point))

    provenance: list[Provenance] = []
    for index, row in enumerate(artifact.rows):
        matched = [entry for entry in pool if _same_row(row, entry[2])]
        if not matched:
            # ★ `#43`: 先落观测（池子 + 交上来的行 + 逐键差异），**再照原样拒收**。
            #   观测挂了不许影响判定 —— 调用点包了 try/except。
            if observe is not None:
                # ★ 观测挂了不许影响判定：把异常吞在这里，判定照原样走（`#43` 的硬要求）。
                with contextlib.suppress(Exception):  # 观测不是判定的一部分
                    observe(
                        _guard_observation(
                            subtask_id=subtask_id,
                            artifact_name=artifact.name,
                            row_index=index,
                            sent_row=row,
                            pool=pool,
                            matched=matched,
                            parts=parts,
                            epoch=epoch,
                        )
                    )
            reject(
                "untraceable_row",
                f"row {index} of {artifact.name!r} matches no row this block was handed; "
                "a merge may only carry values it received",
            )
        seen: set[tuple[str, int]] = set()
        for _name, input_index, _input_row, point in matched:
            snapshot_ref = str(point.get("snapshot_ref") or "")
            raw_source_row = point.get("source_row")
            source_row = raw_source_row if isinstance(raw_source_row, int) else input_index
            if not snapshot_ref or (snapshot_ref, source_row) in seen:
                continue
            seen.add((snapshot_ref, source_row))
            provenance.append(
                Provenance(
                    value_ref=f"{artifact.name}:row={index}",
                    snapshot_ref=snapshot_ref,
                    locator=str(point.get("locator") or ""),
                    source_row=source_row,
                )
            )
    return tuple(provenance)


def _same_row(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    from ..core.provenance import same_value

    # ★ ②: compare the *canonical* rows. "No value" has one spelling (the key is
    #   absent), so a row that spelled it ``null`` and a row that omitted the key
    #   are the same row -- which is what the guard always meant.
    one, other = canonical_row(left), canonical_row(right)
    return all(same_value(one.get(key), other.get(key)) for key in set(one) | set(other))


#: ★ `#33`: 代码类产物能写多大（整个答案的上限由 ``model_output_cap`` 兜着）。
MAX_CODE_BYTES = 200_000


def write_region(contract: TaskContract, subtask: Subtask) -> tuple[str, ...]:
    """一块能写哪些**工作区相对路径** —— 由 `partition` 决定（`#33` 的写区互斥）。

    * `module:<名字>` -> 那个模块声明的来源路径（每块只改自己模块内的文件）
    * `deliverable:<路径>` -> 那份交付物自己
    别的 partition = 空区（不许写文件）—— ★ 空区不是"随便写"，是"没有工作区权限"。
    """
    partition = subtask.partition or ""
    if partition.startswith("module:"):
        name = partition.split(":", 1)[1]
        return tuple(sorted(source.locator for source in contract.sources if source.module == name and source.locator))
    if partition.startswith("deliverable:"):
        return (partition.split(":", 1)[1],)
    return ()


def _file_payloads(
    document: Mapping[str, Any],
    reject: Callable[..., NoReturn],
) -> Mapping[str, str]:
    """`{"files": {相对路径: 原文}}` -> 校验过的 {path: text}（空映射 = 不是代码类答案）。

    校验在这里做完：路径必须**相对**、不许 `..` 逃逸、不许指向目录、总量有上限。
    落盘（写工作区、写区互斥）由外侧做 —— `core/` 不读盘，这里只判形状。
    """
    raw = document.get("files")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping) or not raw:
        reject("files_not_an_object", render_files_requirement(ANSWER_CONTRACT))
    out: dict[str, str] = {}
    total = 0
    for name, content in raw.items():
        path = str(name)
        if not isinstance(content, str):
            reject("file_not_text", f"file {path!r} is {type(content).__name__}, not text")
        if not path or path.startswith(("/", "\\")) or ":" in path.split("/")[0]:
            reject("file_path_not_relative", f"file path {path!r} must be relative to the workspace")
        if ".." in Path(path).parts:
            reject("file_path_escapes", f"file path {path!r} escapes the workspace")
        if path.endswith("/"):
            reject("file_path_is_a_directory", f"file path {path!r} names a directory")
        total += len(content.encode("utf-8"))
        if total > MAX_CODE_BYTES:
            reject("files_too_large", f"the files payload exceeds {MAX_CODE_BYTES} bytes")
        out[path] = content
    return out


def _reject_plain(code: str, problem: str, *, expected: str = "") -> NoReturn:
    """落盘阶段复用的拒绝（形状已经在 `parse_candidate` 里拒过一次；这里是第二道）。"""
    raise CandidateFormatError(problem, code=code, got="", expected=expected or EXPECTED_SHAPE)


def parse_candidate(
    text: str,
    *,
    subtask: Subtask,
    source_id: str,
    authorized_sources: Sequence[str] = (),
    output_cap: int | None = None,
    parts: Mapping[str, Mapping[str, Any]] | None = None,
    hit_output_cap: bool = False,
    observe: Callable[[Mapping[str, Any]], None] | None = None,
) -> Candidate:
    """Read one answer into a ``Candidate``, or fail loudly.

    Row-level provenance is synthesized from the snapshot the answer cites:
    ``required_refs`` asks for ``<artifact>:row=<n>`` per row, so citing one
    snapshot per row is exactly enough for the chain to be walkable.

    ★ A11: two shapes of citation. A block with a source of its own cites it
    positionally (``source_row`` = its own row). A block that only merges parts
    (``parts`` handed in) read no snapshot itself, so its citations are
    *inherited* from the rows it was actually given -- and a row that matches no
    input row is refused instead of being given a citation it cannot support.
    """
    # ★ One place builds the fix, and every rejection carries it: what the model
    #   sent, which rule it broke, and the shape that would be accepted.
    def reject(code: str, problem: str, *, expected: str = EXPECTED_SHAPE, reason: str = "") -> NoReturn:
        raise CandidateFormatError(problem, code=code, got=text, expected=expected, reason=reason)

    try:
        document = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        # ★ Three different breakages, three different fixes. Saying "the first
        #   character is not '{'" when it *is* '{' sends the reader to the wrong
        #   place: a truncated answer is our own output cap, not the model's shape.
        code, problem = _describe_json_failure(text, exc, output_cap=output_cap)
        # ★ `#41`: 只有"被截断"这一支才需要再分一次 —— 是额度不够，还是模型在转圈。
        reason = _truncation_reason(text, hit_output_cap=hit_output_cap) if code == "truncated_answer" else ""
        reject(code, problem, reason=reason)
    if not isinstance(document, Mapping):
        reject("not_an_object", f"the answer is a JSON {type(document).__name__}, not an object")
    rows = document.get("rows", [])
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        reject("rows_not_a_list", "'rows' is missing or is not an array of objects")
    clean_rows: list[Mapping[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            reject("row_not_an_object", f"a row is {type(row).__name__}, not an object of column: value")
        # ★ A04 + ②: an absent cell is recorded as *absent*, never as an empty
        #   string or a zero. The canonical spelling is "the key is not there"
        #   (``canonical_row``), so every later reader -- the merge guard, the
        #   payload the consumer sees, the truth comparison -- has exactly one
        #   shape to handle instead of guessing at ``null`` / ``""`` / a sentinel.
        #   ``0`` stays a value: a filled zero must remain distinguishable.
        clean_rows.append(canonical_row(row))

    # ★ `#33`: 代码类产物走 `files`（工作区相对路径 -> 文件原文）。校验在【这里】做完：
    #   路径必须是相对的、不许 `..` 逃逸、不许重复 —— 落盘的动作由外侧做（core 不读盘）。
    files = _file_payloads(document, reject)

    ref_name = contract_field("ref", ANSWER_CONTRACT).name
    # ★ ``ref`` is not optional: it is what makes the chain walkable. Filling it
    #   with the block id would look legal (a non-empty string) and then fail
    #   downstream as "unknown snapshot 'T-01'" -- a wrong place to point at.
    shape_with_refs = expected_shape(authorized_sources)
    snapshot_ref = document.get("ref")
    if not isinstance(snapshot_ref, str) or not snapshot_ref:
        if authorized_sources:
            keys = sorted(str(key) for key in document)
            reject(
                "missing_ref",
                f"the answer does not say which snapshot it came from: {ref_name!r} is missing "
                f"(top-level keys it did send: {keys})",
                expected=shape_with_refs,
            )
        # No authorized source to cite (a block that only merges upstream parts):
        # there is nothing better to put here, and the gap is reported.
        snapshot_ref = source_id
    elif (authorized_sources or subtask.inputs) and snapshot_ref not in authorized_sources:
        # ★ A11: the guard used to be skipped whenever ``authorized_sources`` was
        #   empty -- which is exactly the merge block, so it could cite anything.
        #   A block that declares inputs may only cite a snapshot it (or the parts
        #   it consumed) actually read.
        reject(
            "unknown_ref",
            f"'ref' names {snapshot_ref!r}, which is not a snapshot this block was given",
            expected=shape_with_refs,
        )
    locator = str(document.get("body_path") or "")
    artifact_text = document.get("text")
    artifact = Artifact(
        name=subtask.output,
        columns=_columns(document.get("columns"), clean_rows, reject),
        rows=tuple(clean_rows),
        text=artifact_text if isinstance(artifact_text, str) else None,
    )
    provenance = [
        Provenance(
            value_ref=f"{subtask.output}:row={index}",
            snapshot_ref=snapshot_ref,
            locator=locator,
            # ★ A11: say explicitly which snapshot row this output row was taken
            #   from, so the content check has something to replay.
            source_row=index,
        )
        for index in range(len(clean_rows))
    ]
    if isinstance(artifact_text, str) and artifact_text:
        provenance.extend(
            Provenance(value_ref=f"{subtask.output}:line={index}", snapshot_ref=snapshot_ref, locator=locator)
            for index, line in enumerate(artifact_text.splitlines())
            if line
        )
    if parts:
        # ★ A11: the block read no snapshot of its own, so its citations cannot be
        #   positional. They are inherited from the rows it was handed; a row that
        #   matches no input is refused (never given a citation it cannot support).
        provenance = list(
            _inherited_provenance(artifact, parts, reject, observe=observe, subtask_id=subtask.id)
        )
    versions = document.get("definition_versions")
    return Candidate(
        subtask_id=subtask.id,
        # ★ The source that asserted this, when the answer names one. Two blocks
        #   reading the same source are not independent corroboration, so this
        #   field has to be the source and not the block.
        source_id=str(document.get("source_id") or document.get("ref") or source_id),
        hint=str(document.get("hint") or content_digest(text)),
        # ★ `#33`: 代码类产物 —— 值是【工作区相对路径】（落盘由外侧做，core 不碰盘）。
        files={str(path): Path(str(path)) for path in files},
        provenance=tuple(provenance),
        artifacts=(artifact,),
        claims=_claims(document.get("claims"), snapshot_ref=snapshot_ref, locator=locator, reject=reject),
        definition_versions=dict(versions) if isinstance(versions, Mapping) else {},
    )


#: ★ ①: 前置预取一次最多塞进多少字符。超了就退化成「索引 + 可逐件取」——
#: 默认给全是对的（E-5：3/3 交出面板），但 240 个输入不能靠"一律全塞"。
DELIVERED_INPUTS_BUDGET = 120_000


def named_source_refs(value: Any, authorized: Sequence[str]) -> tuple[str, ...]:
    """★ `#37`: 判据 spec 里【点名了的源 ref】—— 递归扫，只认授权过的那些。

    判据会在自己的 `spec` 里点名要拿来对照的东西（`frozen_files.spec.sources` 是最典型的）。
    那些 ref 就是"这一块要照着的参照物"：**壳知道，就该在派活前送到块手上**，别让模型自己去发现
    （`#36` 的真实读数：3 跑 21 次调用里 `sources.snapshot` **0 次**）。
    只收 `authorized` 里的名字：没授权的名字即便出现在 spec 里也不送（不越权）。
    """
    allowed = {str(ref) for ref in authorized}
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, str):
            if node in allowed:
                found.add(node)
        elif isinstance(node, Mapping):
            for item in node.values():
                walk(item)
        elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
            for item in node:
                walk(item)

    walk(value)
    return tuple(sorted(found))


def _input_index(parts: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """够用来挑件的最小索引：名字 / schema / 列 / 行数 / 第一行。"""
    index = {}
    for name, payload in sorted(parts.items()):
        rows = payload.get("rows") or []
        index[name] = {
            "schema": payload.get("schema"),
            "columns": payload.get("columns"),
            "rows": len(rows),
            "missing": bool(payload.get("missing")),
            "first_row": dict(rows[0]) if rows and isinstance(rows[0], Mapping) else None,
        }
    return index


@dataclass
class LoopIOImpl(LoopIO):
    """The loop's three collaborators: produce, execute, verify.

    ``produce`` is where the model is called, ``execute`` is the registry, and
    ``verify`` is the block judge -- three different layers, so a failure in one
    cannot be mistaken for a failure in another.
    """

    client: LLMClient
    toolset: ToolSet
    registry: ToolRegistry
    judge: BlockJudge
    subtask: Subtask
    #: ★ The declared allowance, not a private constant: the cap of every model call
    #: is read off it (``model_output_cap``), so declaring 8192 really spends 8192.
    budget: Budget
    source_id: str = "runtime"
    #: Structured-output support, shared with the rest of the run so a refusal is
    #: learned once instead of once per block.
    response_format: ResponseFormatSupport = field(default_factory=ResponseFormatSupport)
    #: The event stream the tool round writes to. ``execute`` is the single point
    #: every tool result passes through, so it is where they are recorded.
    log: EventLog | None = None
    task_id: str = ""
    epoch: Callable[[], float] = time.monotonic
    calls: int = 0
    failed_tools: set[str] = field(default_factory=set)
    #: ★ `#33`: 这一跑的工作区（代码落盘到这里）。`None` = 这个部署没有工作区 ——
    #: 那就**不许**交代码类产物（具名拒绝，不静默丢）。
    workspace: Path | None = None
    #: ★ 这一块能写哪些**工作区相对路径**（由 partition 决定，写区互斥的执行点）。
    write_region: tuple[str, ...] = ()
    #: 这一块实际落盘了哪些路径（证据；空 = 没交代码类产物）。
    written: tuple[str, ...] = ()
    #: ★ `#43`: 继承守卫拒收时把"池子 + 交上来的行"落盘到这里（`None` = 不观测，零开销）。
    observe_dir: Path | None = None

    def prompt(self, state: LoopState) -> list[dict[str, Any]]:
        """Stable instructions first, this block next, the transcript last.

        Order matters: the first two parts never change within a worker, so the
        prefix stays cacheable, and everything dynamic is already at the tail of
        ``state.messages`` (``AGENTS.md §6``: no cache-hostile prompt assembly).
        """
        block = self.toolset.task.mysubtask()
        authorized = list(self.authorized_sources())
        head = [
            {
                "role": "system",
                # ★ `#42`: 这一句不再手写形状 —— 字段、取值域、粒度全部从答案契约渲染
                #   （`answer_contract.py`）。这里只剩"这不是一次自由发挥"与工具授权两句。
                "content": (
                    "You are one worker in a split plan. "
                    + render_prompt_contract(ANSWER_CONTRACT)
                    + " Use the tools you were given; you may only read what they expose."
                ),
            },
            {"role": "user", "content": json.dumps({"subtask": dict(block), "authorized_sources": authorized}, ensure_ascii=False)},
        ]
        delivered = self._delivered_inputs()
        if delivered:
            # ★ 不能被当成 briefing：它前面有一句人话，所以它不是一条以 "{" 开头的
            #   JSON user 消息（读归档 / 读 brief 的地方都在找那一条，别让它们认错）。
            head.append(
                {
                    "role": "user",
                    "content": "★ 以下是壳替你取好的输入（原文，未改动）：\n" + json.dumps(delivered, ensure_ascii=False),
                }
            )
        return [*head, *(dict(message) for message in state.messages)]

    def _delivered_inputs(self) -> dict[str, Any]:
        """★ ① 运行时前置预取：把本块声明的输入【取好、放进 context】。

        计划知道 inputs 是谁，所以"取回输入"不该再是模型的工作（E-2 证明提示有效但只有
        2/3；E-5 证明直接把部件放进 context 是 3/3）。超出预算时退化成索引 ——
        模型仍可用 ``task.upstream`` / ``task.upstream_all`` 逐件或整批取。
        """
        delivered: dict[str, Any] = {}
        if self.subtask.inputs:
            parts = {name: dict(self.toolset.task.upstream(name)) for name in self.subtask.inputs}
            if len(json.dumps(parts, ensure_ascii=False)) <= DELIVERED_INPUTS_BUDGET:
                delivered["delivered_inputs"] = parts
                delivered["note"] = (
                    "★ 你的输入已经由壳取好、就在这里：不必再调 task.upstream。"
                    "答案里的每一行都必须来自这些行（缺件在 missing 里点名）。"
                )
            else:
                delivered["delivered_inputs_index"] = _input_index(parts)
                delivered["note"] = (
                    "★ 输入太多，先给索引：用 `task.upstream_all` 一次取全部，"
                    "或用 `task.upstream` 逐件取（每件都是完整内容 + 逐行引用）。"
                )

        # ★ `#37`: 判据参照的冻结载体也【前置送到】—— 与 #25① 同一个理由：
        #   壳知道的东西，别让模型自己猜（`#36`：真模型 0 次去读）。
        references = self._reference_refs()
        if references:
            # ★ `#39`：**只给索引 + 指路，不塞原文** —— 这是真模型四臂对照里读数最好的一版
            #   （见 `.bobanana6/b39/报告.md`）。两条实测理由：
            #   ① "给名字 + 指路"才是承重的：同一窗口里，不给指路的臂 `sources.snapshot`
            #      取回 **0** 次，给指路的臂取回 **29** 次（三跑），T-01 也是 3/3；
            #   ② 塞全文（且与候选同形 `{columns, rows}`）会被模型当答案抄：那一臂 7/7 次
            #      把参照物原文塞进 `text` 列，还试过把六个载体逐个写进工作区。
            #   注记里【不写】"不必再调 `sources.snapshot`"：只给索引时那句话等于把唯一的取法
            #   也否掉（带那句话的那一臂取回 4 次，只给索引的这一臂取回 29 次；n=3，只作旁证）。
            delivered["reference_index"] = list(references)
            delivered["reference_note"] = render_reference_note(ANSWER_CONTRACT)
        return delivered

    def _reference_refs(self) -> tuple[str, ...]:
        """本块判据 `spec` 里点名、且本块有权读的**冻结载体的 ref**（只给名字，不给内容）。

        `judge.bundle` 已经是**本块那一份**（`runtime.py::block_bundle` 切过片），这里再按
        `target` 过一遍是防御性的：万一拿到的是未切片的整包，也不能把邻居的参照物塞进提示
        （与 `block_bundle` 的 `keep` 是同一条规则；两边都依赖 `Constraint.target` 的语义）。
        """
        refs: set[str] = set()
        authorized = tuple(self.authorized_sources())
        for constraint in (*self.judge.bundle.hard, *self.judge.bundle.soft):
            if constraint.target not in ("*", self.subtask.id, self.subtask.output):
                continue
            refs.update(named_source_refs(constraint.spec, authorized))
        return tuple(sorted(refs))

    def produce(self, state: LoopState) -> TurnProposal:
        self.calls += 1
        cap = model_output_cap(self.budget)
        response = self._complete(state, cap)
        calls = self.registry.calls(response.tool_calls)
        if calls:
            return TurnProposal(tool_calls=calls, text=response.text)
        if not response.text.strip():
            return TurnProposal(text="", detail="the model returned neither text nor a tool call")
        if _declined(response.text):
            # ★ The third legal exit: the worker says it cannot get this
            #   information. It is not a failure and the loop must not retry it.
            return TurnProposal(declined=True, detail=_decline_reason(response.text))
        try:
            candidate = parse_candidate(
                response.text,
                subtask=self.subtask,
                source_id=self.source_id,
                authorized_sources=self.authorized_sources(),
                output_cap=cap,
                parts=self._upstream_parts(),
                hit_output_cap=_hit_output_cap(response.usage, cap),
                observe=self._guard_observer(),
            )
        except CandidateFormatError as rejected:
            # ★ `#65`: 这一类的生产者【只有模型自己】——答案的结构性错误（不是 JSON /
            #   缺必需字段 / 行对不上）在进程内是可判的；而"语义答错"在进程内与
            #   "上下文不够"分不开（`#17` 已论证），所以这里【只声明结构性错误】，
            #   不加启发式硬判。声明走事件流（`detail["cause"]`），归因层照 `#16` 的
            #   规矩先读声明，不再把这一类记成 `None`。
            self._declare_model_failure(rejected)
            raise
        if candidate.files:
            self._persist_files(response.text, candidate)
        return TurnProposal(candidate=candidate, text=response.text)

    def _guard_observer(self) -> Callable[[Mapping[str, Any]], None] | None:
        """★ `#43`: 继承守卫的观测点 —— 只观测，不改判定。

        写盘失败**静默跳过**（观测挂了不许影响判定）；`observe_dir` 为 `None` 时返回 `None`，
        调用点连函数调用都不发生（零开销）。
        """
        if self.observe_dir is None:
            return None
        directory = self.observe_dir
        block = self.subtask.id
        seq = self.calls

        def observe(record: Mapping[str, Any]) -> None:
            try:
                directory.mkdir(parents=True, exist_ok=True)
                target = directory / f"guard-pool-{block}-{seq}.json"
                target.write_text(
                    json.dumps(record, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
                )
            except Exception:  # noqa: BLE001 — 观测不是判定的一部分
                return

        return observe

    def _persist_files(self, text: str, candidate: Candidate) -> None:
        """把代码类产物落到工作区 —— ★ 位置就是"派活前"约定好的工作区，写区互斥在这里执行。"""
        if self.workspace is None:
            raise CandidateFormatError(
                "this deployment has no workspace: a code deliverable cannot be persisted",
                code="no_workspace",
                got=text[:200],
                expected="run with --workspace <dir> so produced files have somewhere to land",
            )
        payload = _file_payloads(json.loads(text), _reject_plain)
        allowed = set(self.write_region)
        for name, content in payload.items():
            if allowed and name not in allowed:
                raise CandidateFormatError(
                    f"block {self.subtask.id!r} may not write {name!r}: "
                    f"its write region is {sorted(allowed)!r}",
                    code="write_outside_region",
                    got=text[:200],
                    expected=f"write only inside {sorted(allowed)!r}",
                )
            target = self.workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        self.written = tuple(sorted(payload))

    def _declare_model_failure(self, rejected: CandidateFormatError) -> None:
        """把「模型给了结构性坏答案」这件事作为【声明】写进事件流。"""
        if self.log is None:
            return
        code = str(getattr(rejected, "code", "") or "")
        detail: dict[str, Any] = {"cause": "model", "code": code, "reason": rejected.reason}
        if str(getattr(rejected, "reason", "")) == "repetition":
            # ★ `#41`: 判它重复就把读数一起落下（归因要能回答"凭什么这么说"）。
            detail["repetition"] = repetition_readings(str(rejected.feedback().get("got") or ""))
        self.log.record(
            kind="produce_failure",
            task_id=self.task_id or self.subtask.id,
            worker_id=self.subtask.id,
            at=self.epoch(),
            ok=False,
            detail=detail,
        )

    def authorized_sources(self) -> tuple[str, ...]:
        """The snapshots this block may cite -- the same list the prompt carries.

        ★ A11: for a merge block this is not empty either. It read no source
        itself, but the parts it consumed did, and a citation has to point at one
        of those instead of at whatever the answer names.
        """
        sources = getattr(self.toolset, "sources", None)
        own = tuple(sources.refs()) if sources is not None and hasattr(sources, "refs") else ()
        inherited = tuple(getattr(self.toolset, "inherited_sources", ()) or ())
        return own + inherited

    def _upstream_parts(self) -> dict[str, Any]:
        """The parts this block was handed, for a block that consumes inputs.

        They carry the producing blocks' per-row citations, which is what a
        consumer inherits its provenance from. A block with no declared inputs
        read its material itself and cites it positionally.
        """
        if not self.subtask.inputs:
            return {}
        parts: dict[str, Any] = {}
        for name in self.subtask.inputs:
            try:
                parts[name] = dict(self.toolset.task.upstream(name))
            except PermissionError:  # not a declared input: it is not this block's to read
                continue
        return parts

    def wants_structured_output(self) -> bool:
        """Whether this call should ask for the answer shape.

        ★ Not on the very first call of a block: until the model has had one turn
        it may legitimately need to call a tool, and a strict answer schema is
        exactly what stops it. From the second turn on, either it has data (and
        should answer) or its first answer was unusable (and structure is what it
        needs) -- both want the schema.
        """
        return self.response_format.enabled and self.calls > 1

    def _complete(self, state: LoopState, max_tokens: int) -> ModelResponse:
        """One model call, with capability negotiation around the answer schema.

        ★ A service that does not understand ``response_format`` must not turn into
        a hard failure: it is asked once, refused once, and then the layer is off for
        the rest of the run. That is negotiation, not a retry policy -- the call is
        only repeated when the failure *is* the capability answer.
        """
        prompt = self.prompt(state)
        schemas = tool_schemas(self.toolset)
        if not self.wants_structured_output():
            return self.client.complete(prompt, schemas, max_tokens)
        try:
            return self.client.complete(prompt, schemas, max_tokens, response_format=candidate_response_format())
        except Exception as exc:
            if not rejects_response_format(exc):
                raise
            self.response_format.enabled = False
            self.response_format.refused = True
            return self.client.complete(prompt, schemas, max_tokens)

    def execute(self, calls: tuple[ToolCall, ...]) -> tuple[ToolResult, ...]:
        results = self.registry.execute(calls)
        now = self.epoch()
        for result in results:
            name = _name_of(calls, result.call_id)
            # ★ "recovered" means this tool failed earlier in this block and has
            #   now succeeded -- counting a retried win as a first-try win is
            #   exactly the pollution tool_metrics splits out.
            recovered = result.ok and name in self.failed_tools
            if self.log is not None:
                # ★ §7⑫: the event carries the layer that owns the failure, so
                #   attribution reads a declaration instead of guessing from
                #   "some tool returned ok=False".
                detail: dict[str, Any] = {
                    "args_ok": result.error_type != "invalid_tool_arguments",
                    "recovered": recovered,
                    "tool": name,
                }
                cause = self._declared_cause(name, result, calls)
                if cause:
                    detail["cause"] = cause
                self.log.record(
                    kind="tool_result",
                    task_id=self.task_id or self.subtask.id,
                    worker_id=self.subtask.id,
                    at=now,
                    ok=result.ok,
                    detail=detail,
                )
            if name and not result.ok:
                self.failed_tools.add(name)
        return results

    def _declared_cause(self, name: str, result: Any, calls: Sequence[Any]) -> str:
        """Which layer owns this tool outcome, as declared (``AGENTS.md §7⑫``).

        The tool result may carry its own declaration (a broken carrier says
        ``tool``, unusable arguments say ``model``). One case has no result-level
        owner: ``task.upstream`` succeeded but its declared input has no payload --
        the delegation layer promised the block something it never produced, which
        is a *context* failure, and the toolset is where that fact is known.
        """
        declared = str(getattr(result, "cause", "") or "")
        if declared or name != "task.upstream":
            return declared
        # ★ This call *succeeded* -- the payload is what says the input is absent,
        #   so `result.ok` is not the test here.
        arguments = next((call.arguments for call in calls if call.call_id == result.call_id), {})
        requested = str(arguments.get("name", "")) if isinstance(arguments, Mapping) else ""
        if not requested or requested not in self.subtask.inputs:
            return ""
        payload = self.toolset.task.upstream(requested)
        return "context" if isinstance(payload, Mapping) and payload.get("missing") is True else ""

    def verify(self, candidate: Candidate) -> GateVerdict:
        return self.judge.verify(candidate)
