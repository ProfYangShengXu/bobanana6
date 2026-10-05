"""答案契约：模型【必须交什么形状】的**单一来源**（`#42`）。

壳里有三样"本体论"，前两样一直有单一来源 + 校验，第三样到今天还是散着的手写字符串：

```
① 领域概念   `ontology/` + `schemas/ontology.schema.json`    单一来源 ✓ 校验 ✓
② 任务声明   `contract/`                                     单一来源 ✓ 校验 ✓
③ 答案契约   ← 本模块。每个字段三件事：取值域 / 粒度 / 例子；外加结构化形态（供 JSON
             schema 派生）。`assemble_impl/produce.py` 的六处渲染点全部从它派生。
```

**为什么值得单独成模块**：这一族病已经复发四次，每次都是"③ 缺席"的不同侧面 ——

```
#34  `files` 交付通道没人说          → 契约缺一个【字段】
#37  判据参照物没送到块手上           → 契约缺一个【角色】
#41  `reference_note` 缩水           → 第 N 处【渲染】漏同步
现在 `ref` 的【粒度】没人说           → 契约缺一个【字段属性】
```

字段名与结构化形态在别处再手写一遍，就是第七处渲染点；所以守卫测试
（`tests/test_runner.py::test_every_rendering_of_the_answer_contract_agrees_with_it` 与
`::test_no_second_hand_written_source_of_the_answer_shape`）会把它挡下来。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ANSWER_CONTRACT",
    "CANDIDATE_OPTIONAL_PROPERTIES",
    "AnswerField",
    "answer_contract_renderings",
    "candidate_schema",
    "contract_field",
    "render_files_requirement",
    "render_prompt_contract",
    "render_ref_domain",
    "render_reference_note",
    "render_shape",
]


@dataclass(frozen=True)
class AnswerField:
    """契约里的一个字段：**取值域 / 粒度 / 例子**三件都要有。

    ``granularity`` 是今天最缺的那一件：取值域说了"能给哪些值"，粒度说"一个值代表多大一块"
    （例：`ref` 是整个快照，不带 `:row=0` 这种定位后缀）。没有它，模型只能猜。
    """

    name: str
    #: 取值域 —— 能给哪些值。
    domain: str
    #: 粒度 —— 一个值代表多大一块。
    granularity: str
    #: 逐字的一小段（要能直接抄）。
    example: str
    #: 形状骨架里那一格怎么写。
    placeholder: str
    #: 结构化形态 —— `CANDIDATE_SCHEMA` 由它生成（唯一的结构来源）。
    json_schema: Mapping[str, Any] = field(default_factory=dict)
    #: 必填？
    required: bool = False
    #: 条件必填的触发条件（空 = 无条件）。
    when: str = ""


ANSWER_CONTRACT: tuple[AnswerField, ...] = (
    AnswerField(
        name="ref",
        domain="one of the authorized source ids the brief lists (never a deliverable name, never a path)",
        granularity=(
            "the whole snapshot: the id alone as listed, with no locator suffix "
            "(`:row=0` / `/json/schema:...` are not part of a source id)"
        ),
        example='"interface"',
        placeholder='"<source id>"',
        json_schema={"type": "string"},
        required=True,
    ),
    AnswerField(
        name="columns",
        domain="an array of {name, dtype} objects",
        granularity="one entry per column; `name` is the exact key `rows` uses (dtype: str/int/float/bool)",
        example='[{"name": "function", "dtype": "str"}]',
        placeholder='[{"name": "<column>", "dtype": "str"}]',
        json_schema={
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name"],
                "properties": {
                    "name": {"type": "string"},
                    "dtype": {"type": "string"},
                    "nullable": {"type": "boolean"},
                },
            },
        },
        required=True,
    ),
    AnswerField(
        name="rows",
        domain="an array of objects keyed by the declared column names",
        granularity='one object per row; an absent cell is a missing key (never `null` / `""`)',
        example='[{"function": "total"}]',
        placeholder='[{"<column>": <value>}]',
        json_schema={"type": "array", "items": {"type": "object", "additionalProperties": True}},
        required=True,
    ),
    AnswerField(
        name="files",
        domain="a map of workspace-relative path -> the file's full text",
        granularity="one path per file, the whole file in one string (relative paths only)",
        example='{"input/repo/shared.py": "def total(items):\\n    return sum(items)\\n"}',
        placeholder='{"<workspace-relative path>": "<file content>"}',
        json_schema={"type": "object", "additionalProperties": {"type": "string"}},
        required=False,
        when="when this block produces files",
    ),
)

#: **不是**契约字段，但请求里允许出现的可选字段（`hint` / `text` / `definition_versions` /
#: `claims`）。它们不承担"必须填"的语义，所以不进 `ANSWER_CONTRACT` —— 分开摆，别混。
CANDIDATE_OPTIONAL_PROPERTIES: Mapping[str, Any] = {
    "hint": {"type": "string"},
    "text": {"type": "string"},
    "definition_versions": {"type": "object", "additionalProperties": {"type": "string"}},
    "claims": {
        "type": "array",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["field", "source_id"],
            "properties": {"field": {"type": "string"}, "value": {}, "source_id": {"type": "string"}},
        },
    },
}


def contract_field(name: str, contract: Sequence[AnswerField] = ANSWER_CONTRACT) -> AnswerField:
    """按名字取契约里的字段（找不到就是编程错误，不静默）。"""
    for answer_field in contract:
        if answer_field.name == name:
            return answer_field
    raise KeyError(f"the answer contract has no field {name!r}")


def render_shape(contract: Sequence[AnswerField] = ANSWER_CONTRACT) -> str:
    """形状骨架（拒绝时喂回去的 `expected`）—— 每个字段的粒度也逐字带上。"""
    required = [answer_field for answer_field in contract if answer_field.required]
    optional = [answer_field for answer_field in contract if not answer_field.required]
    skeleton = "{" + ", ".join(f'"{answer_field.name}": {answer_field.placeholder}' for answer_field in required) + "}"
    for answer_field in optional:
        skeleton += f' -- and, {answer_field.when}, "{answer_field.name}": {answer_field.placeholder}'
    lines = [skeleton, "each field:"]
    for answer_field in contract:
        condition = f" ({answer_field.when})" if answer_field.when else ""
        lines.append(f'  -- "{answer_field.name}"{condition} = {answer_field.granularity}')
    return "\n".join(lines)


def candidate_schema(contract: Sequence[AnswerField] = ANSWER_CONTRACT) -> Mapping[str, Any]:
    """``json_schema`` 那一份（服务端 schema）—— 必填/属性/类型全部由契约生成。"""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [answer_field.name for answer_field in contract if answer_field.required],
        "properties": {
            **{answer_field.name: dict(answer_field.json_schema) for answer_field in contract},
            **{name: dict(schema) for name, schema in CANDIDATE_OPTIONAL_PROPERTIES.items()},
        },
    }


def render_ref_domain(authorized: Sequence[str], contract: Sequence[AnswerField] = ANSWER_CONTRACT) -> str:
    """``ref`` 的取值域，带上这一块**逐字**的授权名单。"""
    ref = contract_field("ref", contract)
    listed = ", ".join(f'"{name}"' for name in authorized)
    return f' -- "{ref.name}" must be {ref.domain}: {listed}'


def render_prompt_contract(contract: Sequence[AnswerField] = ANSWER_CONTRACT) -> str:
    """系统提示里那一段 —— **形状只渲染一次**（`render_shape`），别在这里再复述一遍。

    `#39` 的教训：提示变长对小子模型不是中性的；所以这里只加"形状没说到的那些话"
    （`ref` 非可选、`files` 不写就没有下游），字段的取值域/粒度全在形状里说清了。
    """
    files = next((answer_field for answer_field in contract if not answer_field.required), None)
    tail = ""
    if files is not None:
        tail = (
            f' If this block produces files, they only land on disk through "{files.name}" '
            f'({files.domain}) -- without it the downstream compile/test cannot run.'
        )
    return (
        f"Answer with a single JSON object shaped {render_shape(contract)}. "
        'The "ref" is not optional: it is how every value is traced back to a snapshot.' + tail
    )


def render_reference_note(contract: Sequence[AnswerField] = ANSWER_CONTRACT) -> str:
    """参照物注记：说清"这些是判据的参照物、原文要自己去取、答案形状没变"。"""
    names = ", ".join(f'"{answer_field.name}"' for answer_field in contract if answer_field.required)
    ref = contract_field("ref", contract)
    return (
        "★ 判据会拿你的产出与上面这些冻结载体对照（它们是**判据的参照物**）。"
        "原文没有塞进来：需要哪一件就用 `sources.snapshot` 取（逐件、原文）。"
        f"答案仍然必须是 {{{names}}} —— 其中 {ref.name} 的粒度是：{ref.granularity}。"
    )


def render_files_requirement(contract: Sequence[AnswerField] = ANSWER_CONTRACT) -> str:
    """``files`` 这一格的具名要求（`_file_payloads` 的拒收文案用它）。"""
    files = contract_field("files", contract)
    return f'"{files.name}" must be {files.domain}: {files.granularity}'


def answer_contract_renderings(
    authorized: Sequence[str] = (),
    contract: Sequence[AnswerField] = ANSWER_CONTRACT,
) -> dict[str, str]:
    """**六处渲染点**一次拿全（守卫测试与代码路径共用同一组）。

    键就是报告里那张表的名字；任何一处与契约不一致，守卫测试红。
    """
    return {
        "shape": render_shape(contract),
        "schema": json.dumps(candidate_schema(contract), ensure_ascii=False, sort_keys=True),
        "ref_domain": render_ref_domain(authorized, contract),
        "prompt": render_prompt_contract(contract),
        "reference_note": render_reference_note(contract),
        "files_requirement": render_files_requirement(contract),
    }
