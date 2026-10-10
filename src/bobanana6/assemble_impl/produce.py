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
import csv
import io
import json
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, NoReturn

from ..agentloop.errors import FailoverReason
from ..agentloop.events import EventLog
from ..agentloop.gate import GateVerdict
from ..agentloop.loop import LoopIO, LoopState, TurnProposal
from ..agentloop.text_tool_calls import (
    TEXT_TOOL_CALL_NOTICE,
    TextToolCall,
    detect_text_tool_call,
)
from ..agentloop.tools import ToolCall, ToolResult
from ..core.model import (
    Artifact,
    Budget,
    Candidate,
    Claim,
    Column,
    ConstraintBundle,
    ModelResponse,
    Provenance,
    Subtask,
    TaskContract,
    canonical_row,
    content_digest,
)
from ..core.ports import LLMClient, ToolSet
from ..services.checks import SHELL_COMMAND_CHECK, BlockJudge
from ..services.registry import ToolRegistry
from ..services.traces import TraceStore
from ..services.workspace import (
    WorkspaceLedger,
    WriteBudget,
    WriteBudgetExceeded,
    may_write,
    relative_parts,
)
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
from .artifact_shape import artifact_sections, clip_section, source_shapes

__all__ = [
    "CANDIDATE_SCHEMA",
    "EXPECTED_SHAPE",
    "CandidateFormatError",
    "LoopIOImpl",
    "OutputLimitUnitError",
    "ResponseFormatSupport",
    "artifact_from_disk",
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


#: ★ 抄自 Hermes `agent/prompt_builder.py::TASK_COMPLETION_GUIDANCE` —— **按规则重写，
#:   不是逐字复制**：那里治的是通用 agent 的「写完存根就停」，这里治块级工人的
#:   「形状交对了、内容空着」。这两件事同源，所以规矩同一条。
#:
#: 本仓实测复现过它治的两种毛病（13f，A 臂，DeepSeek-V4-Flash）：
#:   ① **交空壳**：T-01 三次尝试全是 `rows: []`，形状全对、内容全空；
#:   ② **不取数就作答**：`authorized_sources` 列了 7 个来源，它一次 `sources.*` 都没调，
#:      看到 `task.upstream_all` 返回 `{}` 就认定"没数据"。
#: ★ Hermes 那边的注释里记着这毛病在 **DeepSeek v4-flash** 上实测出现过 —— 同一个模型。
#:
#: 三条按重要性排：取数 → 别手写大表 → 取不到就说取不到。**与块、与题目无关**
#: （不提任何产物名/题号），所以它对每个块都成立，也才配发在系统提示里。
BLOCK_COMPLETION_GUIDANCE = (
    "\n\n# 把这件活干完\n"
    "交付物是【一份有真实数据的答案】，不是一份形状正确的空壳。"
    "空表、占位符、示例值、凭印象写的数字，都不是答案。\n"
    "★ 第一步永远是【先把数据取到手】："
    "你被授权读的来源就在 brief 的 `authorized_sources` 里，用 `sources.*` 去读它们。"
    "没有数据就先取数，不要先写答案。\n"
    "★ 大表不要手写：你有 `shell.run` / `files.write`，"
    "算得出来的东西让程序去算、去写。\n"
    "★ 取不到就说取不到 —— 但**先说，再换办法**：\n"
    "「这条来源读不出来」是合格的报告，不是终点。\n"
    "换一个来源、换一个工具、换一个说法，把能拿到的先拿到。\n"
    "★ 只有所有办法都试过、真的拿不到，才交一份说明「哪条拿不到、为什么」的答案。\n"
    "绝不拿编造的内容替代你产不出来的结果 —— 报告阻塞永远好过编一个结果。"
)


#: ★ 抄自 Hermes `agent/prompt_builder.py::TOOL_USE_ENFORCEMENT_GUIDANCE` ——
#: **按规则重写，不是逐字复制**。
#:
#: ★★ 那块在 Hermes 里是**按模型名门控**的，触发名单原文是
#: ``("gpt", "codex", "gemini", "gemma", "grok", "glm", "qwen", "deepseek")``
#: —— **deepseek 明在名单里**。本仓 A 臂用的正是 ``deepseek-v4-flash``，
#: 症状与它治的病一模一样：
#:   第 1 轮模型回「I'll start by understanding my task and examining the
#:   available sources.」—— **说了要去看，没在同一轮动手**。
#: ⇒ Hermes 的作者早就知道这个模型族需要显式引导才会用工具；本仓没抄这块。
#:
#: 与 `BLOCK_COMPLETION_GUIDANCE` 的分工：那块说**做什么**（取数、别手写、
#: 别编），这块说**怎么开始**（说了就立刻做，不许用"我接下来会…"结束一轮）。
TOOL_USE_ENFORCEMENT_GUIDANCE = (
    "\n\n# 说了就立刻做\n"
    "要动手就【在这一轮里直接发出工具调用】—— 不要先把打算说一遍、等下一轮再动。"
    "「我先看一下…」「让我检查…」这种句子后面，必须紧跟对应的工具调用。\n"
    "★ 不要用「我接下来会…」结束一轮：要么这一轮里有推进的调用，"
    "要么这一轮里交出最终答案。只有打算、没有动作的一轮是不合格的。\n"
    "★ 活儿没干完就别停：不要用一段「后续计划」收尾。"
    "手上有能干成这件事的工具，就用它们，而不是告诉别人你打算怎么用。"
)


#: ★ 抄自 Hermes `agent/system_prompt.py` 的 `# Execution discipline`（八条：
#: `tool_persistence` / `mandatory_tool_use` / `act_dont_ask` / `prerequisite_checks` /
#: `verification` / `external_state_verification` / `literal_preservation` /
#: `missing_context`）—— **按规则重写，不是逐字复制**。
#:
#: ★ 八条去掉一条、保留七条：
#:   · **去掉 `act_dont_ask`** —— 那条治的是「别问用户显而易见的问题」，
#:     而块**没有人可以问**。抄进来反而会让它幻想有个用户能回答，
#:     于是拿"我该问谁"当不出手的理由。
#:   · 其余七条全部保留，按本仓场景重写。
#:
#: ★★ 写法标准（用户 2026-10-06 定）：**普通大学生读完能说出"我该怎么做"**。
#:   ⇒ 每条 = 【一句话规矩】+【一个具体例子】，不用术语、不写"应当"。
#:   ⇒ 例子的取材一律来自 13f 的**实测症状**，不是编的。
#:
#: ★★★ 为什么这七条值得占每轮的固定预算：
#:   13f 上模型的行为**逐条命中**它们的反面 ——
#:     `tool_persistence`     拿到 `{}` 就下结论（没换办法再试）
#:     `prerequisite_checks`  跳过"读数据"直接交答案
#:     `mandatory_tool_use`   没算过就交（权重/合计全凭印象）
#:     `literal_preservation` 把来源里的值"顺手修正"
#:     `missing_context`      缺口径时不查 `semantics.*`，直接猜
#:     `verification`         交之前不调 `check.local`
#:     `external_state_verification` 写了文件不回读确认
#:   ⇒ 这不是七条泛泛的"好习惯"，是七条**对着已知症状**写的规矩。
EXECUTION_DISCIPLINE = (
    "\n\n# 干活纪律\n"
    "## 工具说没有，先换个办法再问一次\n"
    "你问「有没有数据」，答「没有」—— 这可能是真的，也可能是你问错了地方。\n"
    "★ 换一个工具、换一个说法再试一次，然后才下结论。\n"
    "例：`task.upstream_all` 返回 `{}` 只说明**没人往你这儿传数据**，"
    "不代表**这件事没有数据** —— 数据很可能就在 `authorized_sources` 里，用 `sources.*` 去读。\n"
    "## 先拿到手，再动手\n"
    "还没看到任何数据就开始写答案，等于交白卷。\n"
    "★ 顺序是：读来源 → 看懂了 → 再算、再写。别跳。\n"
    "## 数字必须真的算过\n"
    "合计、比例、计数、排序 —— 用 `shell.run` 让程序算。\n"
    "★ 「看起来应该是……」不是数据。凭印象写的数字会被判错。\n"
    "## 不确定就先查，别猜\n"
    "不知道一个词的确切口径 → `semantics.lookup`。\n"
    "不知道某个字段对应什么概念 → `semantics.map`。\n"
    "★ 查不到就在答案里说「这条读不出来」—— 这是合格的交付，不是失败。\n"
    "## 来源里的值原样抄\n"
    "代码、编号、金额、日期 —— 逐字照抄，不要顺手改。\n"
    "★ 它长得奇怪不等于它写错了。你改它的那一刻，溯源就断了。\n"
    "## 交之前，自己回答三个问题\n"
    "别问别人「我过了吗」—— 问你【自己】：\n"
    "① 正确性：题目要求的每一条，我这份产物都做到了吗？\n"
    "② 有据：我写的每个值，都能指回我读过的哪一份来源吗？\n"
    "③ 格式：形状对不对（列名、行、ref）？\n"
    "★ 三个问题的答案里有任何一个「没有」—— 那就还没干完，回去接着干。\n"
    "★ `check.local` 是辅助（它能告诉你哪条不过），不是判据：\n"
    "  它过了不等于你做对了，它只说这一块现在没被拒。\n"
    "## 写在文件里的，要回读确认\n"
    "用 `files.write` / `shell.run` 写出来的东西，回读一次，确认真的写进去了。\n"
    "★ 「我以为写好了」和「它真的在磁盘上」是两件事。"
)

#: ★ ``json_schema`` 形式，由契约的结构化形态生成（`answer_contract.py::candidate_schema`）。
#: 故意**不是** ``strict: true``：strict 要求每个属性都必填，那会逼模型在每次答案里编
#: ``hint`` / ``text`` / ``claims``。真正要紧的那一处收紧（``columns`` / ``rows`` 的形状、
#: ``ref`` 必填）都在契约里。**请求描述答案的方式必须与判定答案的方式一致**。
CANDIDATE_SCHEMA: Mapping[str, Any] = dict(candidate_schema(ANSWER_CONTRACT))


#: ★★ `#66` 交接件的**总预算**（重复 `#37` 的教训：不设上限早晚把下游的上下文撑爆）。
#: ★ 关键不是"每一项多大"，是**这一段整体多大** —— 所以按上游【个数】平分预算：
#:   上游少 ⇒ 每个额度大 ⇒ 能展开；上游多 ⇒ 每个额度小 ⇒ 只给索引。
#:   **这样 brief 的量与上游个数无关地有界，不用写"3 个以上就……"这种魔法阈值。**
#: 截断必须**可见**（写 `…[truncated]` / "还有 N 个未展开"），不许静默截。
_UPSTREAM_BRIEF_BUDGET = 2000
_UPSTREAM_BRIEF_MIN_QUOTA = 160
_HANDOFF_NOTE_FIRST = 300
_HANDOFF_FILE_CAP = 40
#: ★ 题面全文内联进每个块 brief 的字符预算。题面是**最短、最该共享**的那份规格
#: （13f 的 `input/instruction.md` 只有 5,355 字节），所以预算给得比交付输入小得多。
#: 超预算要**可见地**截断（`…[truncated]` + 省了多少字），不许静默截。
_STATEMENT_BRIEF_BUDGET = 40_000

#: ★★ 来源表的**形状画像**（行数 / 列数 / 列名 / 按列分组后的组大小）。
#: 这是**可算的事实**，不是对题目的解释：判据只用表头与行列（见 `artifact_shape.py`）。
#: ★ 预算与 `_UPSTREAM_BRIEF_BUDGET` 同一套办法：按**表的个数**平分额度 ⇒
#:   整段大小与来源个数无关地有界。装不下的部分**具名**写在 `omitted` 里（不静默）。
_SOURCE_PROFILE_BUDGET = 5000
_SOURCE_PROFILE_MIN_QUOTA = 400
#: ★★ 题面里**归属到本块产物**的那一节（逐字切片）的字符预算；超了可见地截。
_DELIVERABLE_SPEC_BUDGET = 2000


def _brief_size(value: Any) -> int:
    """一个 brief 片段占多少字符（按最终 JSON 文本量，不按 Python 长度估）。"""
    return len(json.dumps(value, ensure_ascii=False))


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

    enabled: bool = True  # blame: fail-open — 省略即假定端点支持 response_format；不支持时靠一次拒答学回来（有界，见变异 Z05）
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
    """The advertised tool surface, in the shape a model expects.

    ★ `#47`: a tool may declare **optional** arguments. They are advertised (a
    model cannot ask for what it is not told about) but not listed as required,
    so an omitted argument keeps its default instead of forcing the caller to
    invent one. For every pre-existing tool ``optional`` is empty, so the
    advertised schema is byte-identical to before.

    ★★ ``description`` 现在取 ``spec.description``（**决策信息**：何时用 /
    输入什么 / 何时不用），而不是 ``spec.returns``（返回值的类型标签）。
    这条曾经是 ``spec.returns or spec.name`` —— 模型看到的全部信息是
    ``sources.snapshot → "Snapshot"`` 八个字符，于是它每次都选 snapshot
    而不是 read，把 261 KB 的来源整份灌进上下文。
    ``returns`` 保留在 ``ToolSpec`` 里给测试与清单用，不再是发给模型的那句话。
    """
    schemas: list[dict[str, Any]] = []
    for spec in toolset.specs():
        # ★★ 默认每个参数渲染成 `{"type": "string"}`（这套工具的参数域一直是字符串）；
        #   `spec.param_schemas` 给**不是字符串**的参数覆盖真形状 —— `files.patch` 的
        #   `edits` 是一个 `[{oldText,newText}, …]` 数组，广告成 string 的话模型只能塞
        #   一段 JSON 文本，弱模型几乎必错。
        overrides = dict(spec.param_schemas)
        properties = {
            name: dict(overrides.get(name) or {"type": "string"}) for name in (*spec.params, *spec.optional)
        }
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description or spec.returns or spec.name,
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
    # ★ 空列名不是列名：core 的 `Column` 会拒收它（`__post_init__` 的 `_require_non_empty`），
    #   直接构造会让 `ValueError` 冒泡出解析边界 —— 与 `artifact_from_disk` 同一个病根。
    #   这里**具名拒收**（答案信封的形状问题），让模型拿到可照着改的反馈，而不是收到一个异常。
    def named(name: Any) -> str:
        text = str(name)
        if not text:
            # guard: parse
            reject(
                "column_name_empty",
                "'columns' names a column with an empty name; every column must be named",
            )
        return text

    if raw is None:
        names = sorted({key for row in rows for key in row})
        for name in names:
            named(name)
        return tuple(Column(name=name, dtype="str") for name in names)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        # guard: parse
        reject("columns_not_a_list", "'columns' is present but it is not an array")
    columns: list[Column] = []
    for item in raw:
        if isinstance(item, str):
            columns.append(Column(name=named(item), dtype="str"))
        elif isinstance(item, Mapping) and isinstance(item.get("name"), str):
            columns.append(
                Column(
                    name=named(item["name"]),
                    dtype=str(item.get("dtype") or "str"),
                    nullable=bool(item.get("nullable", True)),
                    # ★ A10: a block that knows the unit of what it produced says so
                    #   here; leaving it out is recorded as "not established".
                    unit=str(item.get("unit") or ""),
                )
            )
        else:
            # guard: parse
            reject("column_not_an_object", f"column entry {item!r} is neither a name nor a {{name, dtype}} object")
    return tuple(columns)


def _claims(raw: Any, *, snapshot_ref: str, locator: str, reject: Reject) -> tuple[Claim, ...]:
    if not raw:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        # guard: parse
        reject("claims_not_a_list", "'claims' is present but it is not an array")
    claims: list[Claim] = []
    for item in raw:
        if not isinstance(item, Mapping):
            # guard: parse
            reject("claim_not_an_object", f"claim entry {item!r} is not an object")
        name = item.get("field")
        source = item.get("source_id")
        if not isinstance(name, str) or not name or not isinstance(source, str) or not source:
            # guard: parse
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


def _path_tokens(blob: str, suffixes: Sequence[str]) -> tuple[str, ...]:
    """从一段命令/代码里挑出**像路径**的 token（宁缺勿滥）。"""
    found: list[str] = []
    for raw in blob.replace("'", " ").replace('"', " ").split():
        token = raw.strip("(),;|&><=")
        if not token or token.startswith("-"):
            continue
        looks_like_path = "/" in token or token.lower().endswith(tuple(suffixes))
        if not looks_like_path or len(token) < 4 or token.startswith(("http://", "https://")):
            continue
        found.append(token)
    return tuple(found)


def task_nature(contract: TaskContract, bundle: ConstraintBundle, subtask: Subtask) -> dict[str, Any]:
    """★ 块级 `goal`（`produce 'X'`）丢掉的那一层：**本任务的性质与验收要求**。

    从**契约**派生，不搬任务级 `goal` 原文 —— 面板类题（交付物不是来源、判据里没有要执行的
    命令、也没有冻结载体）返回空字典，提示一个字都不变：

      · `kind`：本块的产出**是不是改写一份已声明的来源**（交付物路径 == 某个 `locator`）
        ⇒ `"fix"`；否则 `"produce"`。
      · `acceptance`：本块判据里 `custom`+`shell_command` 的 `rationale`（任务作者写的
        验收要求，例如"全部测试必须通过"）—— 判据要的事，提示里必须有一句。
      · `frozen`：本块 `frozen_files` 点名的载体（"这几份不许改"）。

    这样"这是一次修复 / 有测试要跑 / 测试必须全过 / 测试不许改"就都到了块手上，
    而且没有把整句 goal 塞进每一个块（`#37` 那次"每块投 6 份源全文"的教训）。
    """
    locators = {source.locator for source in contract.sources}
    kind = "fix" if subtask.output in locators else "produce"
    applicable = [
        constraint
        for constraint in (*bundle.hard, *bundle.soft)
        if constraint.target in ("*", subtask.id, subtask.output)
    ]
    acceptance = [
        constraint.rationale
        for constraint in applicable
        if constraint.kind == "custom"
        and constraint.spec.get("check") == SHELL_COMMAND_CHECK
        and constraint.rationale.strip()
    ]
    frozen = sorted(
        {
            str(ref)
            for constraint in applicable
            if constraint.kind == "frozen_files"
            for ref in (constraint.spec.get("sources") or [])
            if isinstance(ref, str)
        }
    )
    if kind == "produce" and not acceptance and not frozen:
        return {}
    nature: dict[str, Any] = {"kind": kind}
    if acceptance:
        nature["acceptance"] = acceptance
    if frozen:
        nature["frozen"] = frozen
    return nature


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


#: ★★ 只有**信封**失败才值得"带 schema 重问一次"：散文 / ```json 包着 / 截断 / 交了个非对象。
#: 内容失败（`untraceable_row` 等）保持原样 —— 重问改不了内容，且会把继承守卫的观测
#: 写第二遍，而它本来就有具名反馈。判据是**确定性**的：这些码由 `parse_candidate` 逐条声明。
_ENVELOPE_FAILURE_CODES: frozenset[str] = frozenset(
    {"empty", "not_json", "truncated_answer", "malformed_json", "not_an_object"}
)


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
    #: ★ 规格 C②：**契约声明的对账关系**。空 = 逐值相同（第一道工序，行为逐字不变）。
    reconcile: Sequence[Mapping[str, Any]] = (),
    #: ★ 规格 ①(b)：**没有逐字匹配上的行**（现在不拒了，但要落进报告）。
    unverified: list[int] | None = None,
    #: ★ 规格 ①(b)：每一行的"谁 / 期望 / 实际"逐条上报（含逐键差异）。
    on_unverified: Callable[[Mapping[str, Any]], None] | None = None,
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

    from ..core.checkers.provenance import (
        aggregate_over,
        key_columns,
        normalized_reconcile,
    )

    declared = normalized_reconcile(reconcile)
    shared: set[str] = set()
    for _name, _index, input_row, _point in pool:
        shared |= set(canonical_row(input_row))
    for row in artifact.rows:
        shared |= set(canonical_row(row))
    keys = key_columns(reconcile, tuple(sorted(shared))) if declared else None

    provenance: list[Provenance] = []
    unattributed: list[int] = []
    for index, row in enumerate(artifact.rows):
        if keys is None:
            # ★ 没声明对账关系 = 逐字匹配（第一道工序的搬运语义，**行为逐字不变**）。
            #   ★ 这一行是变异锚点 `B09-any-input-row-matches` 的 `old` 串，
            #   改动它 = 偷偷放宽一条既有反证，所以声明式那条走**另一个分支**。
            matched = [entry for entry in pool if _same_row(row, entry[2])]
        else:
            # ★ 声明了变换：按**声明的主键列**认定"这是不是同一行"（派生列不参与）。
            matched = [entry for entry in pool if _same_row(row, entry[2], keys)]
        # ★ 红线：声明了变换也**不许**让"谁都没给过的那一行"混进来 ——
        #   对不上任何输入行 ⇒ 照旧拒收（换判据 ≠ 拆判据）。
        if matched and declared:
            # 派生列按声明的 op 核（聚合的是**匹配到的那些输入行**）
            for column, rule in sorted(declared.items()):
                op = str(rule.get("op") or "eq")
                if op not in ("sum", "count", "min", "max", "mean"):
                    continue
                group_by = tuple(str(name) for name in rule.get("group_by", ()) or ())
                selected = [entry[2] for entry in matched if _same_row(row, entry[2], group_by)]
                expected = aggregate_over(selected, op, str(rule.get("over") or column))
                observed = row.get(column)
                tol = float(rule.get("tol", 1e-9))
                try:
                    agrees = expected is not None and observed is not None and abs(
                        float(observed) - float(expected)
                    ) <= tol
                except (TypeError, ValueError):
                    agrees = observed == expected
                if not agrees:
                    # guard: result — 按声明的 op 从**匹配到的输入行**重算，与交付值比（带 tol）
                    reject(
                        "unreconciled_value",
                        f"row {index} of {artifact.name!r}: column {column!r} is declared as "
                        f"'{op}' over the rows it was handed, but {observed!r} != {expected!r} "
                        f"(grouped by {list(group_by)!r}, tol={tol})",
                    )
        if not matched:
            # ★ `#43`: 落观测（池子 + 交上来的行 + 逐键差异）—— 现在是**诊断**用的，
            #   不再伴随拒收（规格 ① 取缔了逐字匹配）。观测挂了照样不许影响判定。
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
            # ★ 规格 ①（取缔逐字匹配）的处置**未落地**（见交付清单 E3）：它要连带改 6 条既有
            #   断言、并补一条"CI 重算抓真编造"的红线反证；本轮只交付了**建**的那一半
            #   （判据扫描器 + 提取器 + fail-closed），不做"先拆后建"。
            #   这一行的拒收保持原样；`unattributed` 仅用于诊断计数。
            unattributed.append(index)
            # ★★ 规格 ①：**取缔**逐字匹配（它是**最后一个形状守卫** —— 靠"长得像不像我给的"
            #   推定造假；加工块结构上永远不成立：实测 8 块挂 6，全撞这一句）。
            #   ★ (a) 溯源不许一起丢：给**产物级锚点**（本块收到的哪个上游件 + 产出它的块/commit，
            #     复用既有交接字段）。
            #   ★ (b) 不许静默放行：这一行走 `unverified` 上报通道 —— **哪一列、期望多少、实际多少**
            #     都落进报告（用最近那行的逐键差异）。
            #   ★ (c) 防编造由 CI 从原始数据**重算**兜底（`services/ci.py` 的重算判据），
            #     不靠这条守卫。
            # guard: result — 上报而非拒收：对不上的行按"与最近输入行的逐键差异"落报告，
            # 编造由 CI 从原始数据重算抓住（`services/ci.py::invariant_findings`）
            anchor = _upstream_anchor(parts)
            if anchor:
                provenance.append(
                    Provenance(
                        value_ref=f"{artifact.name}:row={index}",
                        snapshot_ref=anchor[0],
                        locator=anchor[1],
                        source_row=None,
                    )
                )
            unattributed.append(index)
            if on_unverified is not None:
                with contextlib.suppress(Exception):
                    on_unverified(_unverified_detail(artifact, index, row, pool, parts, anchor))
            continue
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
    if unverified is not None:
        # ★ ①(b)：**对账结果必须落进报告** —— 哪几行没有逐字对上（谁去核：CI 的重算）。
        unverified.extend(unattributed)
    return tuple(provenance)


def reject_on_delimiter(name: str, text: str, reject: Reject) -> None:
    """★ 改动 A ①：`.csv` 的分隔符必须与名字一致 —— 不对**拒收产物**，并说清错在哪。

    `violated / expected / got` 三参数**一个都不许空**（守卫第四纪律）。
    只拒产物：它是块级拒收 ⇒ 循环会重问这一块，**绝不升格**成整跑失败。
    """
    from ..services.ci import delimiter_finding

    finding = delimiter_finding(name, text)
    if finding is None:
        return
    # guard: result — 实测第一个非空行的分隔符（名字说 .csv ⇒ 必须是逗号）
    reject(
        "artifact_delimiter",
        f"{finding['who']}: {finding['violated']}",
        expected=str(finding["expected"]),
        got=str(finding["got"]),
    )


def _unverified_detail(
    artifact: Artifact,
    index: int,
    row: Mapping[str, Any],
    pool: Sequence[tuple[str, int, Mapping[str, Any], Mapping[str, Any]]],
    parts: Mapping[str, Mapping[str, Any]],
    anchor: tuple[str, str] | None,
) -> dict[str, Any]:
    """★ ①(b)：**拒了要说清**这条纪律，在"不拒了"之后变成**上报要说清**。

    给出 `who`（哪一行）/ `expected`（离它最近的那条输入行）/ `actual`（它实际交的），
    以及**逐键差异**（哪一列、期望多少、实际多少）。
    """
    detail: dict[str, Any] = {
        "artifact": artifact.name,
        "row": index,
        "who": f"{artifact.name}:row={index}",
        "expected": "a row the block was handed, or a value the CI recomputation can reproduce",
        "actual": dict(row),
        "trace_anchor": anchor[1] if anchor else "",
        "upstream_parts": sorted(parts),
        "reconciled_by": "CI recomputation from the original data (not this guard)",
    }
    nearest: dict[str, Any] | None = None
    best = -1
    for name, input_index, input_row, point in pool:
        one, other = canonical_row(row), canonical_row(input_row)
        equal = [key for key in set(one) | set(other) if one.get(key) == other.get(key)]
        if len(equal) > best:
            best = len(equal)
            difference = {
                key: {"sent": one.get(key), "pool": other.get(key)}
                for key in sorted(set(one) | set(other))
                if one.get(key) != other.get(key)
            }
            nearest = {
                "input_name": name,
                "input_row_index": input_index,
                "input_row": dict(input_row),
                "difference": difference,
                "snapshot_ref": str(point.get("snapshot_ref") or ""),
            }
    if nearest is not None:
        detail["nearest_input_row"] = nearest
        detail["column_mismatches"] = [
            {"column": column, "expected": values["pool"], "actual": values["sent"]}
            for column, values in nearest["difference"].items()
        ]
    return detail


def _upstream_anchor(parts: Mapping[str, Mapping[str, Any]]) -> tuple[str, str] | None:
    """★ 规格 ①(a)：溯源不许一起丢 —— 给出**产物级锚点**。

    复用既有的交接字段（`handoff.producer_commit` / `handoff.from`），**不新造字段**。
    返回 `(上游件名, 人读锚点串)`；没有任何上游件时返回 `None`（宁可不编，不造假）。
    """
    for name in sorted(parts):
        payload = parts[name]
        if not isinstance(payload, Mapping):
            continue
        handoff = payload.get("handoff")
        producer = ""
        commit = ""
        if isinstance(handoff, Mapping):
            producer = str(handoff.get("from") or "")
            commit = str(handoff.get("producer_commit") or "")
        # ★ 锚点**至少是那个上游件的名字**（"这一行来自我收到的哪一件"）；有交接件时再补上
        #   产出它的块与 commit。判据是"指得到真东西"，不是"指得到最详细的那个"。
        suffix = ""
        if producer or commit:
            suffix = " (produced by " + producer + (f"; commit {commit[:12]}" if commit else "") + ")"
        return name, f"{name}{suffix}"
    return None


def _citeable_names(
    subtask: Subtask,
    authorized_sources: Sequence[str],
    parts: Mapping[str, Mapping[str, Any]] | None,
) -> tuple[str, ...]:
    """★ 规格 ②：本块**指得到**的名字 —— 存在性判据的取值域（不是白名单）。

    · 它自己读过的来源（`authorized_sources`，由 runner 从"真读过"算出）
    · 它收到的**上游产物**（`subtask.inputs`）—— 这些在 `parts` 里有产出者锚点
    """
    names = {str(name) for name in authorized_sources}
    names.update(str(name) for name in (getattr(subtask, "inputs", ()) or ()))
    for name in (parts or ()):
        names.add(str(name))
    return tuple(sorted(name for name in names if name))


def declared_reconcile(
    bundle: ConstraintBundle | None, subtask: Subtask, artifact_name: str
) -> tuple[Mapping[str, Any], ...]:
    """★ 本块**声明的**对账关系（来自 `provenance` 约束的 `spec.reconcile`）。

    判据不硬编码：哪些列是算出来的、按哪个 op 核，全部由契约声明；
    没声明 ⇒ 空元组 ⇒ 逐值相同（第一道工序行为逐字不变）。
    """
    if bundle is None:
        return ()
    for constraint in bundle.hard:
        if constraint.kind != "provenance":
            continue
        if constraint.target not in ("*", subtask.id, subtask.output, artifact_name):
            continue
        rules = constraint.spec.get("reconcile", [])
        if isinstance(rules, (list, tuple)):
            return tuple(entry for entry in rules if isinstance(entry, Mapping))
    return ()


def _same_row(
    left: Mapping[str, Any], right: Mapping[str, Any], keys: Sequence[str] | None = None
) -> bool:
    from ..core.provenance import same_value

    # ★ ②: compare the *canonical* rows. "No value" has one spelling (the key is
    #   absent), so a row that spelled it ``null`` and a row that omitted the key
    #   are the same row -- which is what the guard always meant.
    one, other = canonical_row(left), canonical_row(right)
    # ★ 规格 C①：`keys` 给定时只比**声明的主键列**（算出来的列不参与"同不同一行"）。
    if keys is not None:
        return all(same_value(one.get(key), other.get(key)) for key in keys)
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
        # guard: parse
        reject("files_not_an_object", render_files_requirement(ANSWER_CONTRACT))
    out: dict[str, str] = {}
    total = 0
    for name, content in raw.items():
        path = str(name)
        if not isinstance(content, str):
            # guard: parse
            reject("file_not_text", f"file {path!r} is {type(content).__name__}, not text")
        if not path or path.startswith(("/", "\\")) or ":" in path.split("/")[0]:
            # guard: safety
            reject("file_path_not_relative", f"file path {path!r} must be relative to the workspace")
        if ".." in Path(path).parts:
            # guard: safety
            reject("file_path_escapes", f"file path {path!r} escapes the workspace")
        if path.endswith("/"):
            # guard: safety
            reject("file_path_is_a_directory", f"file path {path!r} names a directory")
        total += len(content.encode("utf-8"))
        if total > MAX_CODE_BYTES:
            # guard: safety
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
    read_own_sources: bool = False,
    #: ★ 规格 C①：本块**声明的**对账关系（空 = 逐值相同，第一道工序语义逐字不变）。
    reconcile_declaration: Sequence[Mapping[str, Any]] = (),
    observe: Callable[[Mapping[str, Any]], None] | None = None,
    #: ★ 规格 ①(b)：未对上输入行的读数去哪儿（报告用；不参与判定）。
    on_unverified: Callable[[Mapping[str, Any]], None] | None = None,
) -> Candidate:
    """Read one answer into a ``Candidate``, or fail loudly.

    Row-level provenance is synthesized from the snapshot the answer cites:
    ``required_refs`` asks for ``<artifact>:row=<n>`` per row, so citing one
    snapshot per row is exactly enough for the chain to be walkable.

    ★ A11: two shapes of citation. A block with a source of its own cites it
    positionally (``source_row`` = its own row). A block that only merges parts
    (``parts`` handed in) read no snapshot itself, so its citations are
    *inherited* from the rows it was actually given -- and a row that matches no
    input is refused.

    ★★ ``read_own_sources``: 一个块可以【两者都是】—— 既拿到了上游的部件，又自己去读了
    来源（13f 的下游块全是这样：读 INFOTABLE 取原始数据，用上游结果定范围，算出一张新表）。
    那种块**不是**纯拼接块：它的每一行都不会是上游行的逐字拷贝，逼它逐字匹配等于让它永远
    过不了（实测 `nodone` 轮：T-02 算出 1681 行、与 verifier 逐值吻合，仍被判
    ``matches no row this block was handed``）。所以它走"自己引用"那条路 ——
    引用必须落在一份**真的读过、且行真的在**的来源上（内容级校验），比逐字匹配更强。
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
        # guard: parse
        reject("not_an_object", f"the answer is a JSON {type(document).__name__}, not an object")
    rows = document.get("rows", [])
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        # guard: parse
        reject("rows_not_a_list", "'rows' is missing or is not an array of objects")
    clean_rows: list[Mapping[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            # guard: parse
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
    # ★ 改动 A ①：每个 `.csv` 载荷的分隔符先过一遍（通道 1）。
    for _name, _payload in files.items():
        _text = _payload.get("text") if isinstance(_payload, Mapping) else None
        if isinstance(_text, str) and _text:
            reject_on_delimiter(str(_name), _text, reject)

    ref_name = contract_field("ref", ANSWER_CONTRACT).name
    # ★ ``ref`` is not optional: it is what makes the chain walkable. Filling it
    #   with the block id would look legal (a non-empty string) and then fail
    #   downstream as "unknown snapshot 'T-01'" -- a wrong place to point at.
    shape_with_refs = expected_shape(authorized_sources)
    snapshot_ref = document.get("ref")
    if not isinstance(snapshot_ref, str) or not snapshot_ref:
        # ★★ 规格 ③（按处境判，**不是删掉**）：ref 是溯源的关键 ⇒ 不删；
        #   但只有**真的读到东西的块**才被要求给 ref —— 一个"没读来源、手上也没上游件"的块
        #   没有可指的东西，逼它给 ref 就是**逼它编一个**（实测它填 `output/xxx.csv` 然后撞门）。
        if authorized_sources and (read_own_sources or parts):
            keys = sorted(str(key) for key in document)
            # guard: parse
            reject(
                "missing_ref",
                f"the answer does not say which snapshot it came from: {ref_name!r} is missing "
                f"(top-level keys it did send: {keys})",
                expected=shape_with_refs,
            )
        # No authorized source to cite (a block that only merges upstream parts):
        # there is nothing better to put here, and the gap is reported.
        snapshot_ref = source_id
    elif snapshot_ref in _citeable_names(subtask, authorized_sources, parts):
        # ★★ 规格 ②（改判据，不是取消判据）：从"必须在**被授权快照**白名单里"改成
        #   **存在性判据** —— ref 指向的东西必须【真实存在且本块拿得到】：
        #     · 它自己读过的来源（`authorized_sources`）
        #     · 或它收到的**上游产物**（`subtask.inputs`，带产出者锚点 `_upstream_anchor`）
        #   ★ 白名单取消，但"指得到真东西"不许取消：谁都产不出的名字仍会被下面那条拒。
        pass
    elif (authorized_sources or subtask.inputs) and snapshot_ref not in authorized_sources:
        # ★ A11: the guard used to be skipped whenever ``authorized_sources`` was
        #   empty -- which is exactly the merge block, so it could cite anything.
        #   A block that declares inputs may only cite a snapshot it (or the parts
        #   it consumed) actually read.
        # guard: parse
        # guard: parse — 存在性：ref 必须指得到真东西（自己读过的来源 / 收到的上游产物）
        reject(
            "unknown_ref",
            f"'ref' names {snapshot_ref!r}, which neither the block read nor received from upstream; "
            f"available: {sorted(_citeable_names(subtask, authorized_sources, parts))}",
            expected=shape_with_refs,
        )
    locator = str(document.get("body_path") or "")
    artifact_text = document.get("text")
    if isinstance(artifact_text, str) and artifact_text:
        # ★ 改动 A ①：正文形态的产物（名字是 .csv 时）同样判分隔符。
        reject_on_delimiter(subtask.output, artifact_text, reject)
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
    # ★★ 规格 C①：分流判据换变量 —— 从"**读过来源没**"改成"**有没有做变换**"。
    #   旧判据抓错了变量（原则五）：下游聚合块**被授权了 8 个来源**却是 0 次读取
    #   （算聚合表不需要重读 261KB 原始 TSV）⇒ 被判成"没读来源" ⇒ 走逐字匹配 ⇒ 必挂。
    #   ★ "有没有做变换"来自**契约声明**（`spec.reconcile`），不是猜的，也不是硬编码。
    rules = tuple(reconcile_declaration)
    transformed = bool(rules)
    if parts and (not read_own_sources or transformed):
        # ★ A11: the block read no snapshot of its own, so its citations cannot be
        #   positional. They are inherited from the rows it was handed; a row that
        #   matches no input is refused (never given a citation it cannot support).
        #   ★ 声明了变换的块：按**声明的对账关系**比（主键列逐字 + 派生列按声明的 op），
        #   而不是逐值相同 —— 逐值相同只对"从来源搬运"成立。
        unverified: list[int] = []
        provenance = list(
            _inherited_provenance(
                artifact,
                parts,
                reject,
                observe=observe,
                subtask_id=subtask.id,
                reconcile=rules,
                unverified=unverified,
                on_unverified=on_unverified,
            )
        )
        # ★ ①(b)：不拒收，但**对账结果必须落进报告** —— 逐行的"谁/期望/实际"由
        #   `on_unverified` 上报（上面循环里逐条发），这里只留汇总计数。
        if unverified:
            provenance.append(
                Provenance(
                    value_ref=f"{artifact.name}:unverified={len(unverified)}",
                    snapshot_ref=_upstream_anchor(parts)[0] if _upstream_anchor(parts) else subtask.id,
                    locator=f"{len(unverified)} rows were not matched to an input row (reconciled by CI)",
                    source_row=None,
                )
            )
    # ★★ 读过来源的块【不是】纯拼接块，不逼它逐字匹配上游的行。
    #   `_inherited_provenance` 的 docstring 自己写着这个缺口：一行对不上时，
    #   "it is either fabricated or **transformed**, and the system cannot yet tell
    #   those apart" —— 分不清就一律拒收。
    #   而 13f 的下游块**全是 transformed**：读 INFOTABLE（原始来源）+ 用上游结果定范围
    #   ⇒ 算出一张新表。它的每一行都不是上游那些行的逐字拷贝 ⇒ **结构上永远过不了**。
    #   实测（`nodone` 轮）：T-02 把 `effective_holdings.csv` 1681 行真做出来了、
    #   与 verifier 自己的算法逐值吻合，仍被判
    #   `row 0 … matches no row this block was handed`。
    #   ⇒ 判据分流：**自己取过料的，走"自己引用"那条路** —— 引用的行必须在它读过的那份
    #   来源里真的存在、值要对得上（A11 的内容级校验），比"逐字匹配上游行"更强；
    #   没取过料的（纯拼接）保持原规则不动。
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
        # ★★ 上游写给接手人的交接说明 —— 不进判定，但要传给下游（见 `Candidate.summary`）。
        summary=str(document.get("summary") or ""),
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


#: ★ 读盘 CSV 的单字段上限（字符数）。
#:
#: `csv` 模块默认是 131072（`_csv.c` 的 `128 * 1024`），本意是护栏：引号不闭合时别把
#: 整份文件当一个字段读进内存。实测（13f 炸点 ②）小模型写出一行远超 128KB 的数据
#: （`a,b\n` + `x`×500000）⇒ 默认上限直接抛 `_csv.Error`，把一个坏产物变成整跑崩溃。
#:
#: 为什么是 64 MiB：壳给每块的**工作区写预算**就是 64 MiB（`services/workspace.py` 的
#: `_WORKSPACE_WRITE_BUDGET_BYTES`）—— 本壳允许写出的任何一份产物，都不可能有一个比
#: 它整份还大的字段。所以这个上限对合法产物**永不触顶**，又远小于 `2**31-1`（≈2 GiB）：
#: 后者等于拆掉护栏，畸形/恶意输入能把解析器逼进 OOM。超限不抛 —— `_artifact_from_disk`
#: 捕获 `csv.Error` 后退回只填 `text`（判定器照常具名失败）。
MAX_CSV_FIELD_CHARS = 64 * 1024 * 1024
csv.field_size_limit(MAX_CSV_FIELD_CHARS)


def _artifact_from_disk(artifact: Artifact, path: Path, content: str) -> Artifact:
    """★ 通道 2 的**判定载荷**：把磁盘上的产物文件解析成行列。

    为什么必须解析、不能像原来那样只填 `text`：

      16 个 checker 读的是 `rows` / `column()` / `row_count`
      （`core/checkers/presence.py:18`、`range.py:29`），**只有 `provenance` 读 `text`**。
      原来那句 `replace(artifact, text=content)` 只填了 `text` ⇒ 块把表真写进了磁盘，
      判定器读的仍是答案 JSON 里那份 rows（常常是空的）—— **通道 2 看着通了，判定没通**。
      （实测：13f 里交空表也能过，症结之一在此。）

    边界：
      · 列的类型沿用答案里那份声明（模型声明的 `dtype`）；磁盘上多出来的列按 `str` 收。
        壳不替模型猜类型 —— 猜错会在 `schema` 判据上变成壳的账。
      · 空格子**不写键**（`rows` 的约定：缺键 = 缺失，不是 `""`／`null`）。
      · 不是分隔文本 / 空文件 ⇒ 退回只填 `text`，**不抛**。判定器会照常具名失败，
        不该在 loader 这里炸掉整块。
      · ★ 表头里有**空列名** ⇒ 同样退回只填 `text`，**不抛**。`.csv` 的 csv 模块会拒收
        `Column(name="")`（core 的 `__post_init__`），异常冒泡会把整跑弄死（13f 实测）。
        ★ 也**不能把空列名过滤掉**：过滤会让后面的数据列整体左移（第 3 列的值跑到第 2 列），
        那是静默错数据，比崩更糟。
      · ★ 字段超过 `MAX_CSV_FIELD_CHARS`（畸形/恶意输入）⇒ csv 抛 `csv.Error`，捕获后退回
        只填 `text`，**不抛**。

    ★ 这只是**换判定载荷的来源**，不是换判定语义 —— `core/` 一行未改。
    """
    delimiter = {".tsv": "\t", ".csv": ","}.get(path.suffix.lower())
    if delimiter is None:
        return replace(artifact, text=content)
    try:
        table = [row for row in csv.reader(io.StringIO(content), delimiter=delimiter) if row]
    except csv.Error:
        # 字段超限 / 其他 csv 结构错：与"不可解析"同一条出口 —— 退回只填 text，绝不抛。
        return replace(artifact, text=content)
    if not table:
        return replace(artifact, text=content)
    header = [cell.strip() for cell in table[0]]
    # ★ 空列名不是列名，退回 text（见 docstring 的边界）—— core 会拒收空 `Column.name`。
    if any(not name for name in header):
        return replace(artifact, text=content)
    declared = {column.name: column for column in artifact.columns}
    columns = tuple(declared.get(name, Column(name=name, dtype="str")) for name in header)
    rows = tuple(
        {name: cell for name, cell in zip(header, row) if cell != ""} for row in table[1:]
    )
    return replace(artifact, columns=columns, rows=rows, text=content)


def artifact_from_disk(name: str, path: Path) -> Artifact:
    """Read one deliverable off disk and parse it into an ``Artifact``.

    ★ 判定失败 ≠ 没干活：块可能只是**信封没封好**（散文开头、格式不对），它干出来的活
    早就在磁盘上了。壳在这里只**读盘**，不碰任何判定语义 —— `runner` 用它在失败块的
    两条出口上问一句"它的交付物在磁盘上吗"。

    无法解析成行列的分隔文本退回只填 `text`（判定器会照常具名失败，不在 loader 炸整块）。
    """
    content = path.read_text(encoding="utf-8", errors="replace")
    return _artifact_from_disk(Artifact(name=name), path, content)


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
    #: ★★ 这一块【读过来源了吗】—— 一次 `sources.*` 调用就置真。
    #:
    #: 为什么要有这个标志：13f 实测（六轮）里，块被授权读 7 份来源，
    #: 却**一次都不读**就交答案 —— 交空表、交 `"example"`、交一个字面 `"A"`。
    #: 而我们的提示词早就把该说的说了（`BLOCK_COMPLETION_GUIDANCE` 写着
    #: "第一步永远是先把数据取到手"、`how_to_read` 写着 "read first, then produce"），
    #: ★ 采访里模型自己承认："材料中没有一句让我不需要调…**我违反了这条指令**"。
    #: ⇒ 提示词这条路已经试穷（§5.3.5①：**约束改输入形态，不教模型遵守**）。
    #: 这个标志把它从"嘱托"变成"前置条件"：读都没读，答案不受理。
    #: ★ 只在【被授权读来源】的块上生效（`authorized_sources` 非空）——
    #:   纯合并块本来就读不到来源，不该被这条拦住。
    read_sources: bool = False
    #: ★★ 上一轮模型是否发了工具调用 —— 决定下一轮要不要锁成答案形状。
    #: 「还在调工具」= 还在干活；「没调」= 在答题。锁 schema 会禁掉工具调用，
    #: 所以只能在它确实在答题的那一轮锁（见 `wants_structured_output`）。
    last_had_tool_calls: bool = False
    #: ★★ "信封没封好 ⇒ 带 schema 重问一次"的上限：**每块最多一次**（有界，见
    #: `_reask_with_schema`）。散文/```json 包着/截断不是废卷，换一个时机再问一次。
    reasked: bool = False
    #: ★ `#33`: 这一跑的工作区（代码落盘到这里）。`None` = 这个部署没有工作区 ——
    #: 那就**不许**交代码类产物（具名拒绝，不静默丢）。
    workspace: Path | None = None
    #: ★ 这一块能写哪些**工作区相对路径**（由 partition 决定，写区互斥的执行点）。
    write_region: tuple[str, ...] = ()
    #: ★★ 谁都不许写的路径：**别的块的交付物 + 任务声明的来源**。
    #: 为什么需要它（实测 `handoff` 轮）：写区原来只等于"我自己那份交付物"，
    #: 于是块**连临时文件都建不了** —— T-02 要把 631KB 的中间产物先落盘，
    #: 写 `_tmp_answer.json` 被拒（`changed [...], which is outside its write region`），
    #: 最后只能塞一小撮样品进 `rows` 被判"不全"。
    #: 写区互斥本来要防的是"**别撞别人的东西**"，不是"**只许碰自己那一份**"。
    #: ★★ `None` = **没有声明"别人的东西是哪些"** ⇒ 退回旧规则（不在写区里就拒）。
    #: 默认必须是 None（fail-closed）：一个不知道别人有什么的调用方，不该因此获得"随便写"。
    frozen_paths: tuple[str, ...] | None = None
    #: ★ 本块的总写入字节闸（默认 64 MiB，见 `services/workspace.py::WriteBudget`）。
    #: `runtime` 把 `BoundWorkspace` 的那一份同时交给它 —— 写工具的、答案 `files` 的、
    #: 以及命令副作用的字节，记在同一本账上。
    write_budget: WriteBudget = field(default_factory=WriteBudget)
    #: 这一块实际落盘了哪些路径（证据；空 = 没交代码类产物）。
    written: tuple[str, ...] = ()
    #: ★ `#43`: 继承守卫拒收时把"池子 + 交上来的行"落盘到这里（`None` = 不观测，零开销）。
    observe_dir: Path | None = None
    #: ★ `#47` 通道 2：本块直接写/执行动过的工作区，块结束时在这里对账。
    #: `None` / 没被用过 ⇒ 一个字节都不多读（既有读数一字不变）。
    ledger: WorkspaceLedger | None = None
    #: ★★ 静态知识归位（规格 ①）：**全 DAG**的 subtask 清单（含本块）。
    #: 「哪些键进 system」必须是**装配期可算的客观事实**，判据是
    #: 「这个键在【全 DAG 每个块】上的值逐字节相同吗」。
    #: ★ 空元组 = 拿不到全 DAG ⇒ **一个字都不动**（不能证明恒定就不动，fail-closed）——
    #: 这正是规格警告的"跑到哪算到哪"：只有当前块看，前三个块相同、第四个不同就会被误判。
    dag_subtasks: tuple[Subtask, ...] = ()
    #: ★★ 过程级信息交换（ARTEX 机制 ②）：本块**每次工具调用**都往这里记一条。
    #:   `None` = 这个部署不记过程（既有读数一个字节都不变）。
    #:   ★ 记的落点在 `execute()` —— 每个工具结果都从那里过（与 `tool_result` 事件同一个单点）。
    traces: TraceStore | None = None  # blame: fail-closed — 省略即"这个部署不记过程"，既有读数一个字节都不变
    #: ★★ 来源表形状画像的**一次算、全块复用**（`None` = 还没算过）。
    #: 为什么必须缓存：`_constant_brief_entries` **每一轮**都会被调，而画像是 O(表的大小)。
    #: ★ 缓存也保证"同一块内每一轮的前缀逐字节相同"（缓存从第 0 个 token 命中的前提）。
    shape_cache: dict[str, Any] | None = field(  # blame: fail-closed — 省略即"还没算过"，第一次用到时才算（不是"没有画像"）
        default=None,
        compare=False,
        repr=False,
    )

    def prompt(self, state: LoopState) -> list[dict[str, Any]]:
        """Stable instructions first, this block next, the transcript last.

        Order matters: the first two parts never change within a worker, so the
        prefix stays cacheable, and everything dynamic is already at the tail of
        ``state.messages`` (``AGENTS.md §6``: no cache-hostile prompt assembly).
        """
        block = self.toolset.task.mysubtask()
        authorized = list(self.authorized_sources())
        # ★★ 静态知识归位（规格 ①）：先把"哪些键在全 DAG 上恒定"算出来（不是白名单），
        #   恒定的那些进 system 的**恒定前缀**，brief 里**不再出现**（不许两边都放）。
        constant_entries = self._constant_brief_entries()
        brief: dict[str, Any] = {"subtask": dict(block), "authorized_sources": authorized}
        # ★★ 题面里**归属到本块产物**的那一节（逐字切片）：让"这一段在描述哪个产物"
        #   在结构上可见。放在 brief 最前面 —— 它是本块最该先看到的那件事。
        #   ★ 题面全文一个字没少（仍在恒定前缀里），这里只是**再给它一次**。
        deliverable_spec = self._deliverable_spec_entry()
        if deliverable_spec:
            brief["deliverable_spec"] = deliverable_spec
        # ★★ 写区（**按块变** ⇒ 进 brief，不进恒定前缀）：本块被授权写哪儿 + 谁都不许写哪儿。
        #   以前 brief 里只有 `subtask.output` 一个相对路径，没有任何一句说"以谁为基准"。
        write_scope = self._write_region_entry()
        if write_scope:
            brief["write_region"] = write_scope
        # ★★★ 名字**和地址**一起给（见 `_source_paths` 的注记）：只给名字的后果是
        #   块分不清"来源清单"和"来源内容"，而取数入口只有一次机会。
        #   `authorized_sources` 保住溯源（判定器按 ref 查表），`source_files`
        #   补上可解性（载体就在它工作区里）。
        source_paths = self._source_paths()
        if source_paths and "source_files" not in constant_entries:
            brief["source_files"] = source_paths
        # ★★ 每张来源表的**形状**（行/列/列名/按列分组的组大小）—— 与块无关 ⇒ 通常进
        #   恒定前缀；这里只是"拿不到全 DAG 时"的兜底位置（两边**只会有一处**）。
        profiles = self._source_profile_entries()
        if profiles and "source_profiles" not in constant_entries:
            brief["source_profiles"] = profiles
        # ★★ `#66` 上游的一切直接摆上桌（见 `_upstream_brief`）：产物形状 + 上游写过哪些
        #   文件 + 上游写给接手人的说明。下游不再需要花一轮去问"我手上有什么"。
        upstream_brief = self._upstream_brief()
        if upstream_brief:
            brief["upstream"] = upstream_brief
        # ★★ 输入缺失不再是"不许开工"：缺哪几份、谁答应的，作为已知事实摆上桌，
        #   并明确禁止凭名字编（旧前置门靠"不派活"堵 E-0，现在靠"说清 + 禁编 + 溯源兜底"）。
        input_gaps = self._input_gaps()
        if input_gaps:
            brief["input_gaps"] = input_gaps
        # ★★ 题面全文（契约声明的那份来源）—— **每个块**都拿到同一份原文，
        #   而不是各自去取、各自理解（13f：八种读法，一个块偏了随产物级联）。
        statement = self._statement_entry()
        if statement and "statement" not in constant_entries:
            brief["statement"] = statement
        contract = self.toolset.task.contract()
        # ★★★ 用户 2026-10-06 定的标准：**普通大学生读完 brief 要能说出"我要做什么"**。
        #   实测前它说不出 —— 块拿到的全部任务信息是
        #   `{"goal": "produce 'output/filing_resolution.csv'", "inputs": [],
        #     "output_fields": ["text"]}`：一个动作 + 一个字段名，没有内容。
        #   而契约里**本来就有一句人话**（任务作者写的 `goal`），从没进过 brief。
        #   `#44` 当初只搬契约**派生**出来的东西（kind/acceptance/frozen），
        #   为的是不把任务级 goal 原文塞进每一块（`#37`"每块投 6 份源全文"的教训）。
        #   ★ 这次搬的是**一句话**，不是六份源全文 —— 代价几十个字符，
        #   换的是"这一块在整个任务里是干嘛的"这唯一一处上下文。
        #   缺了它，块只知道要产出一个文件名，不知道那个文件里该有什么。
        if contract.goal.strip() and "what_this_task_is" not in constant_entries:
            brief["what_this_task_is"] = contract.goal
        nature = task_nature(contract, self.judge.bundle, self.subtask)
        if nature and "task" not in constant_entries:
            # ★ 块级 goal 只说了"产出哪个文件"；本任务的性质与验收要求由契约派生（`#44`）。
            brief["task"] = nature
        head = [
            {
                "role": "system",
                # ★★ 主体对齐 Hermes 子代理（`tools/delegate_tool.py:1230`
                #   `_build_child_system_prompt`）：**任务在前、汇报在中、格式最后**。
                #   它的骨架是：你是谁 / `YOUR TASK:` / `CONTEXT:` / `WORKSPACE PATH:` /
                #   "Complete this task... When finished, provide a clear, concise summary of:
                #    What you did / What you found / Any files you created / Any issues"。
                #
                #   实测代价（13f 五轮）：bb6 原来是「你是 worker + 格式规格 + 限制」开头，
                #   任务本身排在那 900 字符之后 ⇒ 模型读到的第一件事是"我要输出一个 JSON"，
                #   而不是"我要干什么"。Hermes 第一步就是 `YOUR TASK`。
                #
                #   ★ 更要害的是那条「你做了什么」的位置：bb6 的产物是一张纯数据表
                #   （ref/columns/rows），**没有任何一格放"过程"** ⇒ 模型想汇报"我在读来源"，
                #   只能往数据格里塞。五轮原文（`"Let me read the sources."` → `"read sources"`
                #   → `"Need to read sources first."` → 「Reading authorized sources: …」）
                #   全是这么来的：**给它一个写汇报的地方，这一族症状就没了出处。**
                #
                #   ★ 同时删掉原来那句 "Use the tools you were given; you may only read what
                #   they expose." —— 它抄自 Hermes **主 agent** 的 tool-use 段，而 Hermes
                #   **子代理**没有这句。而且它是**减气**的：暗示"你能拿到的东西就这么多"
                #   （`架构设计.md §5.3.5` 的判据：harness 说的和做的不一致，
                #   都会在模型身上表现为"模型不行"）。
                # ★★ 规格 ①：[恒定前缀] + [块后缀]。恒定前缀**全 DAG、每次调用逐字节相同**
                #   ⇒ 前缀缓存从第 0 个 token 起命中。块后缀只放"这一个块"的静态内容。
                #   ⚠️ 代价如实记：`YOUR TASK` 现在排在恒定前缀之后（原来在最前）——
                #   这是"任务在前"那条实测教训与"前缀要能缓存"之间的取舍，本轮选了后者，
                #   并把块后缀紧贴在恒定前缀之后，让它仍然是模型读到的第一件**本块**的事。
                "content": (
                    self._system_prefix(constant_entries)
                    + "\n\nYOUR TASK:\n"
                    + (self.subtask.goal or self.subtask.output)
                    + "\n\nComplete this task using the tools available to you. "
                    "When finished, provide a clear, concise summary of:\n"
                    "- What you did\n"
                    "- What you found or accomplished\n"
                    "- Any files you created or modified\n"
                    "- Any issues encountered\n\n"
                    "★ 这份汇报写在答案 JSON 的 `summary` 字段里（判定器不读它）；"
                    "**产物本身照下面的形状交**。\n"
                    "★★ 它不是日记，是**写给接手这个产物的人看的交接说明** —— 后面还有块要接着干，"
                    "它只拿得到你的产物和这段话。按四格写：\n"
                    "  ① 我做了什么决定（例：RESTATEMENT 的申报 → `replace_restatement`）\n"
                    "  ② 我按什么规则做的（接手的人必须沿用同一套口径，别让他重推一遍）\n"
                    "  ③ 你接手时要注意什么\n"
                    "  ④ 我哪里不确定\n"
                    "★ brief 里若有 `upstream`，那是上游留下的：`files` 是它写过哪些文件（客观，"
                    "可以直接去读、复用它的脚本）、`handoff_note` 是**上游自己的说法、未经判定**"
                    "（以你自己核过的产物为准）。\n"
                    "★★ 交上来的东西就是你的答卷：**空表、占位符、「我打算去读来源」这类东西不算数，"
                    "会被判回来**。\n"
                    + render_prompt_contract(ANSWER_CONTRACT)
                    + BLOCK_COMPLETION_GUIDANCE
                    + TOOL_USE_ENFORCEMENT_GUIDANCE
                    + EXECUTION_DISCIPLINE
                ),
            },
            {"role": "user", "content": json.dumps(brief, ensure_ascii=False)},
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

    #: 「这个键对**另一个**块不可得」——比"值不同"更强的判断：不可得就不算恒定。
    _UNAVAILABLE = object()

    def _brief_candidates(self) -> dict[str, Any]:
        """候选键 -> 产出它在**任意一个块**上的取值的函数。

        ★ 这不是白名单：这里只列**可能恒定**的候选，真正决定进不进 system 的
        是 `_constant_brief_entries` 里那次**全 DAG 逐块比较**的结果。
        依赖本块 toolset / pieces / handoffs 的键对兄弟块**不可得** ⇒ 直接判非恒定。
        """
        contract = self.toolset.task.contract()
        unavailable = self._UNAVAILABLE
        return {
            "statement": lambda _subtask: self._statement_entry(),
            "source_files": lambda _subtask: self._source_paths(),
            # ★★ 来源表的形状：**契约声明了哪些来源就有哪些** ⇒ 与块无关 ⇒ 全 DAG 恒定。
            #   它进 system 的恒定前缀（缓存友好），而不是每块各带一份。
            "source_profiles": lambda _subtask: self._source_profile_entries(),
            "what_this_task_is": lambda _subtask: contract.goal.strip(),
            "task": lambda subtask: task_nature(contract, self.judge.bundle, subtask),
            # ★★ 工作区根：**整跑级**的值，对每个块都取得到、且同值 ⇒ 全 DAG 恒定。
            #   它只是"可能恒定"的候选之一；真正决定进不进 system 的是下面那次逐块比较
            #   （§4 三条 fail-closed 一条都不少）。空工作区返回 `""` ⇒ 空值不算恒定。
            "workspace": lambda _subtask: self._workspace_value(),
            # ↓ 三个都绑在本块的 toolset / 上游件上：对兄弟块拿不到 ⇒ 永不恒定
            "authorized_sources": lambda _subtask: unavailable,
            "upstream": lambda _subtask: unavailable,
            "input_gaps": lambda _subtask: unavailable,
        }

    def _constant_brief_entries(self) -> dict[str, Any]:
        """★ 规格 ①：在**全 DAG 每个块**上取值逐字节相同的键 ⇒ 进 system 的恒定前缀。

        算出来的，不是写死的；而且：
          · 空值不算恒定（不能拿"都没有"冒充"都相同"）
          · 对任一兄弟块**不可得** ⇒ 不算恒定（宁可不挪，也不让前缀随块变）
          · 拿不到全 DAG（`dag_subtasks` 为空 / 不含本块）⇒ **一个字都不动**
        """
        if not self.dag_subtasks or all(sub.id != self.subtask.id for sub in self.dag_subtasks):
            return {}
        constant: dict[str, Any] = {}
        for key, produce in self._brief_candidates().items():
            rendered: list[str] = []
            for subtask in self.dag_subtasks:
                value = produce(subtask)
                if value is self._UNAVAILABLE or not value:
                    rendered = []
                    break
                rendered.append(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
            if rendered and len(set(rendered)) == 1:
                constant[key] = produce(self.subtask)
        return constant

    def _system_prefix(self, constant: dict[str, Any]) -> str:
        """`[恒定前缀]`：**全 DAG 每个块、每次调用都逐字节相同**。

        ★ 为什么必须逐字节相同：前缀缓存从第 0 个 token 开始命中的前提是前面没变过。
        原来这些键在 **brief** 里，而每个块的 brief 都不同 ⇒ 前缀正好从 brief 处断开，
        恒定那 6KB 每次重新付（实测：13f 的 `statement` 5584B / `source_files` 313B /
        `what_this_task_is` 72B 对所有块相同，`subtask` 只 210B）。
        """
        contract = self.toolset.task.contract()
        # ★★ `workspace` 也走恒定判据，但**不进这段 JSON**：它的渲染是下面那段可读散文
        #   （`_workspace_line`），不是 STATIC REFERENCE 里一行转义的 JSON —— 4B 模型
        #   读散文比读转义 JSON 省力。判据一样跑，只是渲染方式不同。
        static = {
            key: value
            for key, value in sorted(constant.items())
            if key not in ("what_this_task_is", "workspace")
        }
        parts = [
            "You are a focused subagent working on a specific delegated task.",
            "",
            "TOOL CALLS:",
            "Tool calls are read **only** from the tool channel (`tool_calls`). A call written as",
            "text inside the reply body (for example `<function=...>` or `<tool_call>...`) is not",
            "parsed and is not executed.",
            "",
            "CONTEXT:",
            contract.goal if contract.goal.strip() else "(no task-level goal was declared)",
        ]
        if static:
            parts += [
                "",
                "STATIC REFERENCE (identical for every block in this task; it is deliberately NOT",
                "repeated in the per-block brief below):",
                json.dumps(static, ensure_ascii=False, sort_keys=True, default=str),
            ]
        # ★★ 工作区行**只在判据说它全 DAG 恒定时**才出现 —— 这不是"手工塞进 system"：
        #   `constant` 是 `_constant_brief_entries()` 按 §4 三条 fail-closed 算出来的。
        #   拿不到全 DAG（`dag_subtasks` 为空 / 不含本块）⇒ 判据一个字都不动 ⇒ 这里也不加
        #   （宁可不给地址，也不让前缀随块变 —— 那会从第 0 个 token 起破坏缓存）。
        if "workspace" in constant:
            parts.append(self._workspace_line())
        return "\n".join(parts)

    # -- ② 交接件：cleanliness（干净 / 污染） ------------------------------ #

    #: ★ 判据**不看工具名**：transcript 里是**线格式**的名字（`shell_run`），而内部名是
    #: `shell.run` —— 按名字匹配会在适配器边界上悄悄失配（实测踩到过：`cleanliness` 全成了
    #: `clean`）。改成认**参数形状**：参数里带 `command` 的调用就是**执行类**调用，
    #: 而执行类调用里的路径就是可读路径。这条与工具叫什么名字无关。
    _EXECUTION_ARGUMENTS = ("command",)
    #: 路径样子的 token：带 `/` 或带常见后缀，且长度合理（不许把 "a" 当成路径）。
    _PATH_SUFFIXES = (
        ".py", ".tsv", ".csv", ".json", ".txt", ".md", ".yaml", ".yml", ".jsonl", ".log", ".sql",
    )

    def cleanliness(self, messages: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """★ 规格 ②：块读了它【没被授权】的东西吗 —— **通用判据，不特化任何题**。

        本项目的本质判据不是"路径里有 test_outputs"（那是特化，换一道题就废），
        而是：**块读了 authorized_sources 的 locator 覆盖范围之外的路径**。
        契约已经声明了来源地址 ⇒ 读授权本来就该同时管【工具】与【文件在不在】。

        纪律（高精度低召回 / 不扩散）：
          · 只认**读**类调用（`shell.run` 的参数），写类交给既有写区守卫
          · 拿不准就不标：token 必须是**工作区里真实存在**的文件才算数
          · ★ 读自己在写区里的产物【不算】污染（那是它自己的东西）
          · ★ 它**只影响读数的可信度标记**，不参与判定、不让块失败
        """
        workspace = Path(self.workspace).resolve() if self.workspace is not None else None
        authorized = self._authorized_parts()
        evidence: list[str] = []
        scanned = 0
        for message in messages:
            if message.get("role") != "assistant":
                continue
            for call in message.get("tool_calls") or ():
                function = call.get("function") or {}
                raw_arguments = function.get("arguments")
                if isinstance(raw_arguments, Mapping):
                    arguments = dict(raw_arguments)
                else:
                    try:
                        arguments = json.loads(str(raw_arguments or "{}"))
                    except json.JSONDecodeError:
                        continue
                if not isinstance(arguments, Mapping):
                    continue
                blob = " ".join(
                    str(arguments.get(key) or "") for key in self._EXECUTION_ARGUMENTS
                ).strip()
                if not blob:
                    continue  # 不是执行类调用（写/取快照等）：写类交给既有写区守卫
                for token in _path_tokens(blob, self._PATH_SUFFIXES):
                    scanned += 1
                    if workspace is None:
                        continue
                    absolute = Path(token)
                    if not absolute.is_absolute():
                        absolute = workspace / token
                    if not absolute.is_file():
                        continue  # ★ 拿不准就不标（高精度低召回）
                    try:
                        relative = absolute.resolve().relative_to(workspace).as_posix()
                    except ValueError:
                        # ★ 工作区**外**的真实文件：那不在任何声明的 locator 覆盖范围之内。
                        #   （③ 之后工作区里只剩声明的来源 ⇒ 越界读的现实形态就是绝对路径。）
                        evidence.append(str(absolute))
                        continue
                    if self._in_write_region(relative) or relative in set(authorized):
                        continue
                    if self._covered_by_an_authorized_locator(relative, authorized):
                        continue
                    evidence.append(relative)
        unique = sorted(set(evidence))
        return {
            "cleanliness": "tainted" if unique else "clean",
            "cleanliness_evidence": unique,
            # ★ 缺省不是"没扫"：`scanned` 说得出"扫过、看了几个路径 token"
            "cleanliness_scanned": scanned,
        }

    def _authorized_parts(self) -> tuple[str, ...]:
        contract = self.toolset.task.contract()
        return tuple(str(source.locator).replace("\\", "/") for source in contract.sources if source.locator)

    def _in_write_region(self, relative: str) -> bool:
        """这个路径是不是本块自己写区里的东西（自己刚写的产物不算污染）。"""
        region = getattr(self, "write_region", None) or ()
        return relative in set(region) or relative == str(self.subtask.output)

    @staticmethod
    def _covered_by_an_authorized_locator(relative: str, authorized: Sequence[str]) -> bool:
        """★ 覆盖范围 = 声明的那个 locator **所在目录**（保守方向：宁可少标污染）。

        13f 的来源声明在 `input/data/...`，而官方判分器在 `input/checks/` ——
        后者不在任何 locator 的目录里 ⇒ 越界读。
        """
        for locator in authorized:
            if relative == locator:
                return True
            directory = locator.rsplit("/", 1)[0] if "/" in locator else ""
            if directory and relative.startswith(directory + "/"):
                return True
        return False

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
            # ★ 端点要求原样带回的 assistant 字段（思考模型的 reasoning_content）跟着这次工具轮
            #   一起存进循环建的 assistant 行；端点没给就是空映射，一个键都不加。
            return TurnProposal(tool_calls=calls, text=response.text, carry_back=dict(response.carry_back or {}))
        # ★★ 工具调用写成了正文文本？**当场告知，绝不执行**（实测 qwen4b6 2 次 / xing29b 1 次，
        #   每次都静默丢弃、下一轮照旧）。判据是常量表 + 只认实测过的方言；认不出的一律不动。
        #   ★ 合法答案优先（只读审计 #136）：只有"这段正文**解析不出**合法答案"才算泄漏 ——
        #     否则一个恰好引用了这段文本的合法答案会被改道、永远不被解析。
        leaked = detect_text_tool_call(response.text)
        if leaked is not None and not self._parses_as_answer(response.text, cap, response.usage):
            # ★★ 下一轮**不许锁 schema**（只读审计 #134）：`wants_structured_output` 的自记实测是
            #   "带 schema 的请求 110 次 → 工具调用 0 次"。不置这一位，告知要求的"走工具通道重发"
            #   根本发不出去 —— 那才是真的死路。这一位只说"它这一轮在干活"，不改任何判定。
            self.last_had_tool_calls = True
            self._note_text_tool_call(leaked)
            return TurnProposal(text=response.text, notice=TEXT_TOOL_CALL_NOTICE)
        if not response.text.strip():
            return TurnProposal(text="", detail="the model returned neither text nor a tool call")
        if _declined(response.text):
            # ★ The third legal exit: the worker says it cannot get this
            #   information. It is not a failure and the loop must not retry it.
            return TurnProposal(declined=True, detail=_decline_reason(response.text))
        # ★★ read-before-produce：**先查"读没读来源"，再查格式。顺序就是反馈。**
        #
        # 实测（readgate 轮，T-01）：它三次交的都是
        # `"Need to read sources first."` / `"start by reading sources"` —— **嘴上说要读，
        # 一次都没做**；而它收到的反馈是 `'ref' is missing`，于是三次全在修 `ref`。
        # ★ 门说什么，模型就修什么 —— 所以这条必须排在格式检查之前，
        #   它收到的第一句才是"你还没读过任何来源"，而不是"ref 缺了"。
        # （同族的第三种写法：`"Let me read the sources."` → `"read sources"` →
        #   `"Need to read sources first."` —— 说与做之间断了。）
        if self._must_read_sources() and not self.read_sources:
            raise CandidateFormatError(
                "this block may read its sources and has read none; an answer cannot come "
                "from sources it never opened",
                code="not_read",
                got=response.text[:400],
                expected=(
                    "call sources.snapshot / sources.read / sources.grep to get the data first, "
                    "then answer from what you received"
                ),
            )
        try:
            candidate = self._parse_answer(response.text, cap, response.usage)
        except CandidateFormatError as rejected:
            # ★★ 它想交卷，但**信封没封好**（散文 / ```json 包着 / 截断 / 交了个非对象）
            #   —— 不是废卷。今天：这一轮被格式门判死，下一轮才挂上 schema；下一轮它交出
            #   合法 JSON，但这一轮白烧、块还可能因此失败（13f 实测 5/8 个块）。
            #   改法：**立刻带 schema 重问一次**，把刚才的结论按格式交出来 —— 那一次才是
            #   答案轮。★ 这是【换一个时机再问】，不是放宽格式：答案契约与
            #   `parse_candidate` 的判定语义一字未动，散文永远不会被当成合法答案。
            #   ★ 只对**信封**失败重问：内容失败（`untraceable_row` 等）保持原样 ——
            #   重问改不了内容，且会把继承守卫的观测写第二遍，而它本来就有具名反馈。
            retry = None
            if str(rejected.feedback().get("code", "")) in _ENVELOPE_FAILURE_CODES:
                retry = self._reask_with_schema(state, cap)
            if retry is None:
                self._declare_model_failure(rejected)
                raise
            retry_calls = self.registry.calls(retry.tool_calls)
            if retry_calls:
                # 重问之后它改为调工具（严格端点少见）—— 尊重它的下一步，别把这轮算成答案。
                return TurnProposal(
                    tool_calls=retry_calls, text=retry.text, carry_back=dict(retry.carry_back or {})
                )
            response = retry
            try:
                candidate = self._parse_answer(retry.text, cap, retry.usage)
            except CandidateFormatError as second:
                # 重问也没封好 ⇒ 才走原来的失败路径（反馈的是**重问那一次**的错，更贴切）。
                self._declare_model_failure(second)
                raise
        if candidate.files:
            self._persist_files(response.text, candidate)
        candidate = self._reconcile_workspace(candidate, response.text)
        return TurnProposal(candidate=candidate, text=response.text)

    def _parses_as_answer(self, text: str, cap: int, usage: Mapping[str, Any] | None) -> bool:
        """这段正文能不能**按答案**解析出来（只读审计 #136）。

        只用来判断"方言样文本是不是恰好出现在一个合法答案里"：解析成功就绝不改道，
        解析失败（信封没封好等）才当成泄漏。`parse_candidate` 是纯的，试一次没有副作用。
        """
        try:
            self._parse_answer(text, cap, usage)
        except CandidateFormatError:
            return False
        return True

    def _note_text_tool_call(self, hit: TextToolCall) -> None:
        """记一笔：这一轮发生了文本形式调用、哪种方言、烧了多少字符（只记账，不执行）。

        ★ `ok=False`：它不是一个动作的成功。`kind` 单独一格，**绝不并进 `no_tool_call`**
          —— 否则"它没想调"与"它调了但走错通道"从此再也分不出来。
        """
        if self.log is None:
            return
        self.log.record(
            kind="text_tool_call",
            task_id=self.task_id,
            worker_id=self.subtask.id,
            at=self.epoch(),
            ok=False,
            detail={
                "dialect": hit.dialect,
                "chars": hit.chars,
                "names": list(hit.names),
                "snippet": hit.snippet,
                # ★ 发生轮次（只读审计 #138）：与归档的 block/turn 对得上。
                "turn": self.calls,
            },
        )

    def _parse_answer(self, text: str, cap: int, usage: Mapping[str, Any] | None) -> Candidate:
        """把模型的文本变成候选 —— **唯一**的解析入口（判定语义原样，只是复用它）。"""
        return parse_candidate(
            text,
            subtask=self.subtask,
            source_id=self.source_id,
            authorized_sources=self.authorized_sources(),
            output_cap=cap,
            parts=self._upstream_parts(),
            hit_output_cap=_hit_output_cap(usage, cap),
            # ★★ 它自己取过料没有 —— 决定它算不算"纯拼接块"（见 `parse_candidate`
            #   里 `if parts and not read_own_sources` 的注记）。
            read_own_sources=self.read_sources,
            # ★ 声明从这一层读（它手上才有 `judge.bundle`）
            reconcile_declaration=declared_reconcile(self.judge.bundle, self.subtask, self.subtask.output),
            # ★ ①(b)：未对上输入行的读数落进事件流（报告看得见），**不参与判定**。
            on_unverified=lambda detail: self.log.record(
                kind="gate_check",
                task_id=self.task_id,
                worker_id=self.subtask.id,
                at=self.epoch(),
                ok=True,
                detail={"content_unverified": dict(detail)},
            ),
            observe=self._guard_observer(),
        )

    def _reask_with_schema(self, state: LoopState, cap: int) -> ModelResponse | None:
        """★ 带 schema 重问一次（每块最多一次）。

        **为什么这不是"多花一轮"**：今天也是两轮（自由轮 + schema 轮），只是第一轮的
        输出被当废品判死；改完还是两轮，第一轮的语义从"废品"变成"意图信号"。
        而本来就直接交 JSON 的块，情况 a 让它**省一轮**。

        边界：① 有界（`reasked`）；② 计入模型调用预算（`budget.max_calls`，
        它就是 `LoopConfig.max_turns` 的来源，见 `runtime.py`）；③ 端点不支持 schema
        时重问没有意义，直接退回原来的失败路径。
        """
        if self.reasked or not self.response_format.enabled:
            return None
        if self.calls >= self.budget.max_calls:
            return None
        self.reasked = True
        self.calls += 1
        try:
            response = self.client.complete(
                self.prompt(state),
                tool_schemas(self.toolset),
                cap,
                response_format=candidate_response_format(),
            )
        except Exception as exc:
            if rejects_response_format(exc):
                # 端点不认 schema：重问没有意义，退回原来的失败路径。
                # ★ 与 `_complete` 同一处理（赋值顺序故意相反：让变异锚点保持唯一）。
                self.response_format.refused = True
                self.response_format.enabled = False
                return None
            raise
        # ★ 记下"这一轮它在干活还是在答题"：下一轮要不要锁 schema 由它决定。
        self.last_had_tool_calls = bool(getattr(response, "tool_calls", ()) or ())
        return response

    def _reconcile_workspace(self, candidate: Candidate, text: str) -> Candidate:
        """★ `#47` 通道 2 的对账：块**直接写/执行**出来的文件，在这里变成可追溯的产物。

        这是本块最关键的一处设计取舍（`#47` 报告 §2②）：

        * **为什么观察而不是相信每个工具**：`shell.run` 能创建壳看不到参数的
          文件（`> out.csv`、`python gen.py`）。只登记 `files.write` 会在"能执行命令"
          这一加入口上留一个洞。ledger 在**第一次**动工作区前拍了"之前"，这里 diff 出
          "新增/改动的文件"——两条路径（写工具 / 执行副作用）走同一条记录。
        * **通道 1 一字不改**：答案 JSON 的 `files` 仍然走 `_persist_files` 与行级溯源；
          这里**只追加**通道 2 的东西。没用到通道 2 的块（所有既有题）直接原样返回。
        * **写区互斥在这里是硬闸**：改到写区之外的路径 ⇒ 具名拒（模型能看见、能自纠），
          而不是"写进去了、装配关看不见"。
        * **不许当后门**：本块声明的交付物若被直接写过，那个交付物的**判定内容取自磁盘**
          （不是答案里那句话）——判定器看见的就是块真正交出来的字节。文件级溯源点
          （`<path>:file`）与交付物的行级点位一起进候选，装配关与 `SOURCES.md` 因此看得见它们。
        """
        ledger = self.ledger
        if ledger is None or not ledger.used or self.workspace is None:
            return candidate
        changed = ledger.changed()
        if not changed:
            return candidate

        # ★★ 只拒"撞到别人的东西"：别人的交付物 + 任务声明的来源。
        #   别的一律放行 —— 块要能建临时文件（见 `frozen_paths` 的注记）。
        #   ★ 判定走 `services/workspace.py::may_write`：与 `files.write` / 答案 `files`
        #     是**同一个函数**，否则换个工具就换个规矩。
        outside = [name for name in changed if not may_write(name, self.write_region, self.frozen_paths)]
        if outside:
            allowed = sorted({"/".join(relative_parts(name)) for name in self.write_region})
            raise CandidateFormatError(
                f"block {self.subtask.id!r} changed {outside!r}, which is outside its write region "
                f"{allowed!r}",
                code="write_outside_region",
                got=text[:200],
                expected=f"write only inside {allowed!r}",
            )

        root = Path(self.workspace)
        # ★ 改动 A ①（通道 2）：**直接写/执行**出来的文件在块结束时过一遍分隔符判据。
        #   实测反例正是走的这条通道：手工 `f.write` 拼出的
        #   `crowded_securities_latest.csv` 是 TAB，下游按逗号读 ⇒ column mismatch ×4。
        def _raise(code: str, problem: str, *, expected: str = "", got: str = "") -> None:
            raise CandidateFormatError(problem, code=code, got=got, expected=expected)

        for name in changed:
            target = root / name
            if not target.is_file() or not name.lower().endswith(".csv"):
                continue
            reject_on_delimiter(
                name, target.read_text(encoding="utf-8", errors="replace"), _raise
            )
        # ★ 总字节闸（**净占用**）：命令副作用（`shell.run`）落下的文件在这里
        #   记进本块账上，然后按"这些路径现在在磁盘上的字节和"判定 —— 写过的又删了 = 0，
        #   反复覆写同一个大文件不会被累计误伤（见 `services/workspace.py::WriteBudget`）。
        for name in changed:
            target = root / name
            size = target.stat().st_size if target.is_file() else 0
            self.write_budget.note_observed(name, size)
        try:
            self.write_budget.ensure(
                self.write_budget.occupancy(root), where="workspace side effects at block end"
            )
        except WriteBudgetExceeded as exceeded:
            raise CandidateFormatError(
                str(exceeded),
                code="write_budget_exceeded",
                got=text[:200],
                expected=f"keep the block's workspace occupancy under {self.write_budget.limit} bytes",
            ) from exceeded
        #: 文件级溯源要指回一个**登记在册**的快照 —— 用本块有权引用的那些（自己读的 + 继承的）。
        #: 一个都没有时只登记文件、不编一个指向不了的引用（宁可不编，不造假）。
        authorized = tuple(self.authorized_sources())
        cited = authorized[0] if authorized else ""
        provenance = list(candidate.provenance)
        files = dict(candidate.files)
        for name in changed:
            files[name] = root / name
            if cited:
                provenance.append(Provenance(value_ref=f"{name}:file", snapshot_ref=cited, locator=name))

        artifacts = list(candidate.artifacts)
        output = self.subtask.output
        if output in changed:
            # ★ 交付物被直接写过 ⇒ 判定的那一份内容以**磁盘为准**，答案里那句话不算数。
            #   ★ `errors="replace"`：与 `artifact_from_disk` 同一条解码策略。严格 utf-8 会在
            #   `shell.run` 写出非 UTF-8 交付物（GBK/二进制）时抛 `UnicodeDecodeError` ——
            #   那会让一个**本可判定**的候选被当成异常丢掉，而不是照常具名失败。
            content = (root / output).read_text(encoding="utf-8", errors="replace")
            # ★★ 光填 `text` 不够：16 个 checker 读的是 `rows` / `column()` / `row_count`，
            #   只有 `provenance` 读 `text`。所以磁盘上的 CSV 必须**解析成行列**，
            #   否则块真把表写进了磁盘，判定器照样读答案 JSON 里那份（空的）rows ——
            #   通道 2 看着通了，判定其实没通。（实测：13f 里交空表也能过，症结之一在此。）
            artifacts = [
                _artifact_from_disk(artifact, root / output, content) if artifact.name == output else artifact
                for artifact in artifacts
            ]
            if cited:
                provenance.extend(
                    Provenance(value_ref=f"{output}:line={index}", snapshot_ref=cited, locator=output)
                    for index, line in enumerate(content.splitlines())
                    if line
                )
        return replace(candidate, files=files, provenance=tuple(provenance), artifacts=tuple(artifacts))

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
        # ★ 同一套判定（`services/workspace.py::may_write`），`frozen_paths=None` 时 fail-closed。
        for name, content in payload.items():
            if not may_write(name, self.write_region, self.frozen_paths):
                allowed = sorted({"/".join(relative_parts(entry)) for entry in self.write_region})
                raise CandidateFormatError(
                    f"block {self.subtask.id!r} may not write {name!r}: "
                    f"its write region is {allowed!r}",
                    code="write_outside_region",
                    got=text[:200],
                    expected=f"write only inside {allowed!r}",
                )
            target = self.workspace / name
            # ★★ 空内容 = 「这份文件我自己已经写进工作区了」——由 `_reconcile_workspace`
            #   从磁盘取（契约里 `files` 的粒度就是这么说的）。
            #   不判空直接写，会把块**亲手写出来的交付物覆盖成空文件**：实测 13f
            #   `ansrow` 轮，T-02 写好的 `effective_holdings.csv`（189KB / 1681 行）
            #   收尾时只剩 183 字节 —— 它交的是 `files: {路径: ""}`（因为 189KB
            #   塞不进一次回答），壳照写不误。
            if content == "" and target.is_file() and target.stat().st_size > 0:
                continue
            # ★ 总字节闸与写工具共用（`runtime` 传进来的是同一个 `WriteBudget`）。
            self.write_budget.note_write(name)
            self.write_budget.note_bytes(len(content.encode("utf-8")))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        # ★ 判定以**块末净占用**为准（写过的又删了 = 0；反复覆写不累计）。
        try:
            self.write_budget.ensure(
                self.write_budget.occupancy(self.workspace), where="answer files at block end"
            )
        except WriteBudgetExceeded as exceeded:
            raise CandidateFormatError(
                str(exceeded),
                code="write_budget_exceeded",
                got=text[:200],
                expected=f"keep the block's workspace occupancy under {self.write_budget.limit} bytes",
            ) from exceeded
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

    def _source_paths(self) -> dict[str, str]:
        """★ 每个授权来源**指向哪个载体** —— 名字旁边的地址。

        为什么必须一起给：`authorized_sources` 是**句柄**（溯源主键 + 能力边界，
        两者都要求它稳定），而模型需要的是**可解**。只给名字的实测后果（13f 五轮）：
        块分不清"来源清单"和"来源内容" —— 它调 `task.contract` 拿到的是一模一样的
        那串名字，于是以为看清单就是看来源。而载体其实**就在它的工作区里**
        （`shell.run` 的 cwd 就是那儿），只是从来没人告诉过它。

        ★ `SourcePort.locator` 的 docstring 本来就写着它是 "a hint for the payload"
        —— 这句话到今天才第一次真的成立。给地址**不增加任何能力**（文件本来就在
        工作区里、它本来就能读），只补上它缺的那条信息。

        继承来的 ref（合并块）不在本块的 sources 端口里 —— `locator` 返回空串，跳过。
        """
        sources = getattr(self.toolset, "sources", None)
        if sources is None or not hasattr(sources, "locator"):
            return {}
        paths: dict[str, str] = {}
        for ref in self.authorized_sources():
            locator = sources.locator(ref) or ""
            if locator:
                paths[ref] = locator
        return paths

    def _statement_entry(self) -> dict[str, Any]:
        """★ 题面**全文**（契约声明的 source ref）—— 每个块都拿到同一份原文。

        **为什么内联、不给路径**：一次调用 = 一次往返；凡壳在派活前已知、且模型必须
        知道的，就内联进 brief（`_upstream_brief` 同一条规则）。原来题面只是一条普通来源，
        块要自己调 `sources.snapshot(ref=...)` 去取 —— 走不走过去壳不管；实测后果是
        **八个块各自去读、各自理解 ⇒ 同一份题面八种读法**，一个块理解偏了，它的偏差
        随产物级联到下游，下游无从对照。

        题面本来就是声明的来源 ⇒ 它已经在 `_fetch_sources` 的快照里（不用重新取网）。
        超预算时**可见地**截断（`…[truncated]` + 省了多少字），不许静默截。
        """
        ref = str(getattr(self.toolset.task, "statement", "") or "")
        sources = getattr(self.toolset, "sources", None)
        if not ref or sources is None:
            return {}
        text = self._statement_text(ref, sources)
        if not text:
            return {}
        budget = _STATEMENT_BRIEF_BUDGET
        if len(text) <= budget:
            return {"ref": ref, "text": text}
        return {
            "ref": ref,
            "text": text[:budget] + "…[truncated]",
            "truncated_chars": len(text) - budget,
        }

    def _source_body(self, ref: str, sources: Any) -> str:
        """一份来源的**原文**：优先工作区里那份（模型读的就是它），否则读冻结载体。

        ★ 读不到 ⇒ 空串（**fail-closed**）：调用方据此"一个字都不加"，绝不猜。
        """
        if sources is None:
            return ""
        encoding = "utf-8"
        for source in getattr(sources, "authorized", ()) or ():
            if getattr(source, "ref", "") == ref and getattr(source, "encoding", ""):
                encoding = str(source.encoding)
                break
        locator = sources.locator(ref) if hasattr(sources, "locator") else ""
        if self.workspace is not None and locator:
            path = Path(self.workspace) / locator
            if path.is_file():
                return path.read_text(encoding=encoding, errors="replace")
        try:
            snapshot = sources.snapshot(ref)
        except (AttributeError, LookupError, PermissionError):
            return ""
        try:
            return Path(snapshot.body_path).read_text(encoding=encoding, errors="replace")
        except OSError:
            return ""

    def _statement_text(self, ref: str, sources: Any) -> str:
        """题面原文：优先工作区里那份（模型读的就是它），否则读冻结载体。"""
        return self._source_body(ref, sources)

    def _deliverable_spec_entry(self) -> dict[str, Any]:
        """★ ① 题面里**归属到本块产物**的那一节（逐字切片，一个字不改写）。

        为什么要它：题面一百多行一次内联进 system，产物规格埋在中间 —— 实测（真跑六轮）
        模型读到的是**整段散文**，"哪一段在说我要产出的那个东西"要它自己去找、自己认。
        这里按**契约声明的产物名**去题面里认标题，把本块那一节原样**再给一次**
        （题面全文仍然在，见 `_statement_entry` —— 只做归属与前置，不删原文）。

        ★ 只做归属：不解释、不判分、不替模型推结论、不写"所以你应该…"。
        ★ 认不出 / 拿不到题面 ⇒ 一个字都不加（fail-closed）。
        """
        output = str(self.subtask.output or "")
        if not output:
            return {}
        statement = self._statement_entry()
        text = str(statement.get("text") or "")
        if not text:
            return {}
        section = artifact_sections([output], text).get(output)
        if not section:
            return {}
        return {
            "artifact": output,
            "verbatim_excerpt": True,
            "section": clip_section(str(section["text"]), _DELIVERABLE_SPEC_BUDGET),
        }

    def _source_profile_entries(self) -> dict[str, Any]:
        """★★ ② 每张**契约声明的**来源表的形状 —— 全 DAG 恒定 ⇒ 走 system 的恒定前缀。

        为什么这是壳该算的：表长什么样是**可算的事实**，而模型看不到 —— 它只能先花
        一轮把表读回来才知道有几行几列。这里把"行数 / 列数 / 列名 / 按两列分组后
        最大的一组有多大"摆上桌（判据只用表头与行列，见 `artifact_shape.py`）。

        ★ 只陈述形状：输出里**没有一格内容**（"最大几组"是**大小**，不是值），
          也不写"所以你应该产出几行" —— 那一步是模型自己的判断，不是壳的。
        ★ 读不到 / 不是表 ⇒ 那张不给；一张都给不出 ⇒ 空（fail-closed）。
        ★ 算一次缓存一次：这一段**每轮**都要拼，且必须每轮逐字节相同（缓存前提）。
        """
        if self.shape_cache is None:
            sources = getattr(self.toolset, "sources", None)
            texts: dict[str, str] = {}
            for source in self.toolset.task.contract().sources:
                text = self._source_body(source.ref, sources)
                if text:
                    texts[source.ref] = text
            self.shape_cache = source_shapes(texts, _SOURCE_PROFILE_BUDGET, _SOURCE_PROFILE_MIN_QUOTA)
        return dict(self.shape_cache)

    def _upstream_brief(self) -> dict[str, Any]:
        """★★ 上游的一切直接摆上桌，不让下游再花一轮去问（`#66`）。

        规则：**凡壳在派活前已经知道、而模型必须知道的，就内联进 brief。**
        一次调用 = 一次往返 —— 下游原来第一轮必花 `task.contract` / `task.upstream`
        去问"我手上有什么"，而这两样壳收块时就全有了（实测 13f：T-01 首轮固定烧在
        问任务上，而 brief 里 `subtask` 那一格与 `task.mysubtask` 的返回**完全重复**）。

        ★★ **渐进式披露**：预算按上游【个数】平分，额满即止。
        上游≤2 时每个都能展开（列名 + 文件清单 + 交接说明）；上游一多，每个只剩
        `from / 行数 / 列数 / 说明首句` 的索引形态 —— **整段大小与上游个数无关地有界**，
        要细节模型自己 `task.upstream('<名字>')` 取。这样就不用写"3 个以上就……"。

        `files` 是**客观**的（来自上游的 `Candidate.files`，壳记的）；`note` 是**自述**，
        所以键名就叫 `handoff_note`，系统提示里也明说它未经判定 —— 不许把上游的误解
        当事实传下去。

        ★ 只给"有哪些、多大"，**不给内容**：189KB 那种塞不进一次回答（实测
        `effective_holdings.csv`）。要内容它自己按路径去读。
        """
        parts = self._upstream_parts()
        ordered = [name for name in sorted(parts) if isinstance(parts[name], Mapping)]
        if not ordered:
            return {}
        quota = max(_UPSTREAM_BRIEF_MIN_QUOTA, _UPSTREAM_BRIEF_BUDGET // len(ordered))
        brief: dict[str, Any] = {}
        collapsed: list[str] = []
        for name in ordered:
            payload = parts[name]
            rows = payload.get("rows") or []
            columns = payload.get("columns") or []
            # ①② 必有：找谁 + 规模。这两格小到可以无条件给。
            entry: dict[str, Any] = {"rows": len(rows), "columns": len(columns)}
            handoff = payload.get("handoff")
            note = ""
            files: list[str] = []
            if isinstance(handoff, Mapping):
                entry["from"] = str(handoff.get("from") or "")
                note = str(handoff.get("note") or "").strip()
                files = sorted(str(item) for item in handoff.get("files") or [])
                # ★★ 这份产物是从一个**没通过判定**的块来的（它只是信封没封好，活已经干完）
                #   —— 下游必须看得见，不许静默降级。
                if handoff.get("producer_verdict"):
                    entry["producer_verdict"] = str(handoff.get("producer_verdict"))
                if handoff.get("from_disk"):
                    entry["from_disk"] = True
                # ★★ 产出它的那个 commit：下游的溯源基准（规格 §2 第1层）——
                #   "这份东西是哪块、什么时候、用什么输入产出的"有确定答案。
                if handoff.get("producer_commit"):
                    entry["producer_commit"] = str(handoff.get("producer_commit"))
                # ★ 规格 ②：交接件带 cleanliness —— 下游要能看出"这份东西是不是
                #   照着判分器做出来的"。★ 拒了要说清：不只给布尔，给出**越界读的路径**。
                if handoff.get("cleanliness"):
                    entry["cleanliness"] = str(handoff["cleanliness"])
                    entry["cleanliness_evidence"] = list(handoff.get("cleanliness_evidence") or ())

            # ③ 交接说明首句（有就给 —— 下游靠它复现口径）
            if note and quota - _brief_size(entry) > 80:
                first = note if len(note) <= _HANDOFF_NOTE_FIRST else note[:_HANDOFF_NOTE_FIRST] + "…"
                entry["handoff_note"] = first
                if len(note) > _HANDOFF_NOTE_FIRST:
                    entry["handoff_note_truncated"] = True
            # ④ 列名全表（额度够才给 —— 它是接口，值得花）
            if columns and quota - _brief_size(entry) > _brief_size(columns) + 20:
                entry["columns_detail"] = [dict(column) for column in columns]
            # ⑤ 文件清单（额度够才给）
            if files and quota - _brief_size(entry) > 60:
                keep = min(len(files), _HANDOFF_FILE_CAP)
                entry["files"] = files[:keep]
                if len(files) > keep:
                    entry["files_truncated"] = f"…还有 {len(files) - keep} 个未列出"
            if "columns_detail" not in entry or "files" not in entry:
                collapsed.append(name)
            brief[name] = entry
        if collapsed:
            # ★ 截断必须可见：明说"可以接着要"，而不是让下游以为"就这些"。
            brief["_collapsed"] = (
                f"{len(collapsed)} 个上游只给了索引（{', '.join(collapsed)}）；"
                "要细节用 `task.upstream('<名字>')`"
            )
        return brief

    def _write_region_entry(self) -> dict[str, Any]:
        """★ 本块的**写区**（相对工作目录）—— 按块变，所以进 brief（不进恒定前缀）。

        实测（`qwen4b` 第 34 轮）：模型把产物写到 `<repo>/13f-crowding/output/`，
        而判定器读的是 `<workspace>/output/` —— brief 里只有一个 `subtask.output`
        相对路径，**没有一句话说"以谁为基准"、"哪些能写、哪些碰不得"**。

        三个子键与 `services/workspace.py::may_write` **同源、同优先级**（写区优先于冻结）：
          · `you_may_write`：`write_region(contract, subtask)` 现算的路径，本块自己的产物；
          · `never_write`：`frozen_paths`（所有块的交付物 ∪ 任务声明的来源）里**不在**写区中的；
          · `rule`：口径（相对工作目录）+ 边界（产物必须落在写区里、不许写到工作目录之外）。

        ★ 这是**告知**，不是判据：拦截仍然只在 `may_write` / `_reconcile_workspace` 那一处。
        ★★ 文案必须照**执行语义**写，不能说教一条比 `may_write` 更严的规矩：
          `may_write` 的真实规则是「写区永远可写 + `frozen_paths` 之外也可以写（草稿纸）」，
          只有 `frozen_paths is None` 时才退回「不在写区就拒」。写成"写区之外一律不许写"
          会与执行层相反 —— 那正是 `AGENTS.md` 说的"harness 说的和做的不一致"；
          所以下面按 `frozen_paths` 的两种取值分开渲染，`you_may_write` 为空时也据实说明。
        """
        if self.workspace is None:
            return {}
        # ★ 空串不是路径：`partition="deliverable:"`（冒号后为空）会让 `write_region` 返回
        #   `("",)`。那是一个**已经坏掉的声明**，但"告知"里不该把它当成一条"可以写的路径"
        #   发出去 —— 过滤掉，不给模型制造一个幻觉地址（这条只影响文案，不动 `may_write`）。
        allowed = [str(name) for name in self.write_region if str(name)]
        allowed_set = set(allowed)
        # ★ 与 `may_write` 的优先级一致：写区优先 ⇒ 写区里的路径不再列进"碰不得"。
        #   否则同一句话里"能写 X"和"不许写 X"并存（自己改自己那份交付物是合法的）。
        frozen = [str(name) for name in (self.frozen_paths or ()) if str(name) not in allowed_set]
        if self.frozen_paths is None:
            # `may_write(..., frozen_paths=None)`：不在写区就拒 —— 只有写区放行。
            outside = "`you_may_write` 之外一律不许写（调用方没有声明别人的东西是哪些）。"
            empty = "（`you_may_write` 为空 = 这一块不许写任何文件：产物写在答案 JSON 里。）"
        else:
            # `may_write(..., frozen_paths=…)`：写区永远可写 + frozen 之外也可以写（草稿纸）。
            outside = (
                "`never_write`（别人的交付物、任务声明的来源）一个字节都不许改；"
                "工作目录里 `never_write` 之外的其它路径可以写（临时文件）。"
            )
            empty = (
                "（`you_may_write` 为空 = 这一块没有文件形式的交付区："
                "把产物写在答案 JSON 里，别去猜一个路径。）"
            )
        return {
            "you_may_write": allowed,
            "never_write": frozen,
            "rule": (
                "以上路径都**相对工作目录**（见 system 里的 `WORKSPACE PATH`，那里给的是绝对路径）。"
                "`you_may_write` 里的路径**永远可以写**；"
                + outside
                + "★ 你的**产物必须落在 `you_may_write` 里** —— 不要写到工作目录【之外】："
                "判定器只读工作目录，写到别处等于没交。"
                "`shell.run` 能写到任何地方：用它时一律用相对路径，"
                "或用 system 里那个前缀拼出来的绝对路径。"
                + (empty if not allowed else "")
            ),
        }

    def _workspace_value(self) -> str:
        """工作区根的**绝对路径** —— `_brief_candidates` 里 `workspace` 那个值。

        它是整跑级的（每个块同一个值），所以能过 §4 的全 DAG 比较；没有工作区 ⇒ **空串**
        （空值不算恒定，见 `_constant_brief_entries` 的第一条 fail-closed）。
        """
        if self.workspace is None:
            return ""
        return str(Path(self.workspace).resolve())

    def _workspace_line(self) -> str:
        """抄 Hermes 子代理骨架里那句 `WORKSPACE PATH:`（bb6 一直没实现）。

        没有它，块手上有 `shell.run` 却不知道 cwd 在哪、素材就在脚下。
        有了它，两条路各干各的：**地址用来导航与取数，名字用来溯源**。

        ★★ 实测追加（`qwen4b` 第 34 轮：43 次调用、零产物）：**只丢一个路径字符串不够。**
          那一轮 system 里本来就有这一行，模型仍然照题面里写死的 `/app/data/` 走，
          又自己 `find` 出题目原目录的绝对路径，最后 `mkdir -p <repo>/…/output`
          把产物写到了**工作区之外** —— 判定器只读工作区，于是无限重试。
          ⇒ 补齐三件事（都是【告知】，不是新判据）：
            ① 路径本身（**绝对**，由 `_workspace_value()` 现算）；
            ② **口径**：brief 里的相对路径以它为基准，不是"相对某个不知道的地方"；
            ③ **边界**：不许自己推断/猜测绝对路径；题面里的其它绝对路径属于原环境。

        ★ 它是**全 DAG 恒定**的（工作区根是整跑级的），所以放在 system 的恒定前缀里
          （`_system_prefix`），不占每块 brief 的预算，也不破坏前缀缓存。
          **出现与否由 §4 的恒定判据决定**（`"workspace" in constant`），不是无条件直插。
        """
        root = self._workspace_value()
        if not root:
            return ""
        return (
            "\n\nWORKSPACE PATH:\n"
            + root
            + "\n★ 这就是你的工作目录。**brief 里所有相对路径都以它为基准** —— "
            "`output/xxx.csv` 指的就是 `" + root + "/output/xxx.csv`。\n"
            "★ 不许自己推断或猜测绝对路径：需要绝对路径时，一律拿上面这个前缀去拼。"
            "题面或说明里出现的其它绝对路径（例如 `/app/...`）属于原环境，在这里不成立。\n"
            "Your tools run inside this directory. The task's own material is already "
            "here -- look under `input/` (use `shell.run` to list and read it); "
            "`output/` is where deliverables land.\n"
            "Look around before concluding that anything is missing.\n"
            "★ 引值时仍要写它出自哪个来源（`ref`）：地址是用来【看】的，名字是用来【溯源】的。\n"
        )

    def _input_gaps(self) -> dict[str, Any]:
        """本块按计划要、但没到手的输入 —— 拆门之后堵 E-0 的那一半。

        旧前置门用"缺输入就不派活"堵"凭名字编一张表"；门拆掉之后，改成**把它已经知道的
        事实说出来**（缺哪几份、谁答应的），并明确禁止编 —— 剩下的由溯源层兜底
        （编出来的行追不到真来源：引用的行不存在 / 值对不上）。
        """
        parts = self._upstream_parts()
        missing = [
            {"input": name, "producer": str(parts[name].get("producer") or "")}
            for name in self.subtask.inputs
            if isinstance(parts.get(name), Mapping) and parts[name].get("missing") is True
        ]
        if not missing:
            return {}
        return {
            # ① 你按计划要这几份输入
            "you_were_expected_to_receive": list(self.subtask.inputs),
            # ② ★ 其中这几份没有到（答应的块是 <块 id>，它失败了）
            "these_did_not_arrive": missing,
            # ③ ★ 不许凭名字猜它们的内容 —— 编出来的数据会被溯源层抓住
            "do_not_invent": (
                "上面这几份【没有到】：产出它们的块失败了。不许凭名字猜它们的内容 —— "
                "编出来的行会被溯源层抓住（引用的行不存在 / 值与来源对不上）。"
            ),
            # ④ 两条正当出路
            "legitimate_ways_out": [
                "从你自己的 authorized_sources 里取原始来源、自己算（地址在 source_files 里）",
                "交一份降级的产物，并在 `summary` 里写清你缺什么、你用什么替代的",
            ],
        }

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
        exactly what stops it.

        ★★ 也**不能在"它还没取到数"的时候**挂：`response_format` 一旦挂上，输出就被
        约束成答案对象，模型**再也发不出工具调用**。而 read-before-produce 的反馈
        （"你还没读来源"）恰恰是发在这一轮里的 ⇒ **那道门成了一道永远满足不了的门**。
        实测（全档 7 个 tag）：可调工具的请求 67 次 → 工具调用 166 次；
        带 schema 的请求 **110 次 → 工具调用 0 次**，一次例外都没有。
        块的一生只有第一次调用能调工具 —— 它把那一次拿去问了 `task.contract`。

        ⇒ 判据从"第 2 回合起"改成"**它已经能答题了**才锁"。取数路没走完，
        这一轮问的是"下一步"，不是"答案"。
        """
        if not self.response_format.enabled or self.calls <= 1:
            return False
        # ★★ 上一轮它还在调工具 ⇒ **它还在干活**，这一轮别锁 —— 锁了它就再也调不动，
        #   于是"读了一份规格"就被当成"读完了"。实测（`addrgate`）：它 turn1 读了
        #   `instruction`（5KB 规格），schema 立刻锁上，**再也读不到 `infotable`
        #   （261KB 数据）**。⇒ 判据必须是"它在干活还是在答题"，不是"它读过没有"。
        if self.last_had_tool_calls:
            return False
        return not (self._must_read_sources() and not self.read_sources)

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
            response = self.client.complete(prompt, schemas, max_tokens)
        else:
            try:
                response = self.client.complete(
                    prompt, schemas, max_tokens, response_format=candidate_response_format()
                )
            except Exception as exc:
                if not rejects_response_format(exc):
                    raise
                self.response_format.enabled = False
                self.response_format.refused = True
                response = self.client.complete(prompt, schemas, max_tokens)
        # ★★ 记下"这一轮它在干活还是在答题"：下一轮要不要锁 schema 由它决定。
        self.last_had_tool_calls = bool(getattr(response, "tool_calls", ()) or ())
        return response

    def execute(self, calls: tuple[ToolCall, ...]) -> tuple[ToolResult, ...]:
        results = self.registry.execute(calls)
        now = self.epoch()
        # ★★ 过程级信息交换的第一步：**每个工具结果都从这一个点过** ⇒ 就在这里记。
        #   与下面的 `tool_result` 事件同一个单点，不另开一条旁路。
        self._record_traces(calls, results, now)
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
            # ★★ 一次成功的 `sources.*` 调用就足够 —— 判据是"读没读过"，
            #   不是"读全了没有"（读全不全由产物自己交代）。读失败不算数：
            #   失败的那次没拿到任何数据，不该用来开门。
            if result.ok and name.startswith("sources."):
                self.read_sources = True
        return results

    def _record_traces(
        self, calls: Sequence[ToolCall], results: Sequence[ToolResult], now: float
    ) -> None:
        """★ 把这一批工具调用记进【过程】存储（ARTEX 机制 ② 的第一步）。

        记什么：哪一块 / 第几次 / 什么工具 / args 的可检索文本 / result 的可检索文本 /
        时间戳 / 成功与否 / 失败类型。**不记判定** —— 过程只回答"当时发生了什么"。

        ★★ 硬边界：这一步读了**工作区之外真实存在的文件**（判分器那种）时，结果内容
          不进索引。存储层收到 `result_indexed=False` 时**根本不存正文**（不是存了再遮），
          所以那条内容从检索路径上取不回来。
        """
        if self.traces is None:
            return
        by_call = {call.call_id: call for call in calls}
        for result in results:
            call = by_call.get(result.call_id)
            arguments = (
                dict(call.arguments)
                if call is not None and isinstance(call.arguments, Mapping)
                else {}
            )
            name = str(getattr(call, "name", "") or "")
            self.traces.add(
                block_id=self.subtask.id,
                # `self.calls` 是这一块的**模型调用**计数 ⇒ 它就是"第几轮"（`seq` 是第几次工具调用）。
                turn=self.calls,
                tool=name or "<unknown>",
                args_text=self._trace_args_text(name, arguments),
                result_text=str(getattr(result, "content", "") or ""),
                at=now,
                ok=bool(getattr(result, "ok", False)),
                error_type=str(getattr(result, "error_type", "") or ""),
                result_indexed=not self._trace_reads_outside(name, arguments),
            )

    @staticmethod
    def _trace_args_text(name: str, arguments: Mapping[str, Any]) -> str:
        """args 的可检索文本 —— 工具名 + 参数（一行 JSON）。不读盘、不展开附件。"""
        return f"{name} {json.dumps(dict(arguments), ensure_ascii=False, default=str)}"

    def _trace_reads_outside(self, name: str, arguments: Mapping[str, Any]) -> bool:
        """这一步有没有**可能**读到工作区之外的东西（保守方向：拿不准也遮）。

        ★★ 这是那条硬边界（"过程检索不许成为读判分器的通道"）**唯一**的执行点，
          所以它不能只做"路径是不是一个真实存在的文件"那一种判断：
          静态扫描做不到精确，就必须往**保守**那边倒 —— 判分器内容进索引是不可接受的，
          而多遮一条无害的过程观察只是少一点信息。三条都遮：

        ```
        ① 绝对路径（含 `/abs/checks/*` 这种**通配**形式 —— token 本身不是文件）
        ② 向上逃逸（token 里带 `..`）
        ③ 相对路径 + 通配/命令替换（`sub/*.py` / `$(pwd)/x`）—— 解析不出它最终指哪
        ```
        ★ 只扫执行类调用（args 里带 `command`）：`sources.*` 读的是**声明的来源**，
          它已经在工作区里，不需要遮。
        """
        workspace = Path(self.workspace).resolve() if self.workspace is not None else None
        if workspace is None:
            return False
        blob = " ".join(str(arguments.get(key) or "") for key in self._EXECUTION_ARGUMENTS).strip()
        if not blob:
            return False
        for token in _path_tokens(blob, self._PATH_SUFFIXES):
            if token.startswith(("/", "\\")) or ".." in Path(token).parts:
                return True
            if "/" in token and any(char in token for char in "*?[$`"):
                return True
            absolute = Path(token)
            if not absolute.is_absolute():
                absolute = workspace / token
            if not absolute.is_file():
                continue
            try:
                absolute.resolve().relative_to(workspace)
            except ValueError:
                return True
        return False

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
        # ★ read-before-produce 不在这里判 —— 它在 `produce` 里、**排在格式检查之前**
        #   （见那里的注记：门说什么模型就修什么，位置的先后就是反馈的先后）。
        #   这里只保留 `_must_read_sources` 供 `produce` 使用，避免同一条规则两处实现漂移。
        return self.judge.verify(candidate)

    def _must_read_sources(self) -> bool:
        """这一块【只有来源一条取数路】吗（见 `produce` 里 read-before-produce 的注记）。

        块有**两条**取数路 —— `task.upstream*`（上游产物）与 `sources.*`（授权来源）：
        · 有上游 ⇒ 数据可以从上游来，**不逼它读来源**（合并块就是这种；实测用
          `authorized_sources` 非空去判，会把 pipeline 的合并块 T-03 一起拦下）；
        · 没上游 + 有来源 ⇒ **只能读来源**，一次没读就是没取数。
        """
        sources = list(self.authorized_sources())
        if not sources:
            return False
        return not tuple(getattr(self.subtask, "inputs", ()) or ())
