"""``core/ports.py::CoveragePort`` 的实作 —— 壳里的**预扫描**。

一句话：**在派活之前，先把"材料里有几个 X"变成一份可打勾的清单，冻住。**

★ 为什么必须在壳里跑，不在块里：清单是**分母**。分母一旦由干活的那一方决定，
它就能按"我已经写了什么"反推一份短清单，于是覆盖率永远接近 1，而那不是"覆盖全了"，
是"把尺子改短了"。这和 ResearchPort 同一条：壳负责造材料，块只负责用。

★ 两道独立性，缺一道这个机制就会变成更精致的假绿：

  ① **接地**：清单上的每一项都要能在材料原文里找到（子串或归一化后的变体）。
     编出来的项不许进分母。
  ② **独立计数**：另跑一遍**不同的问法**，从头数一遍项数。清单长度低于它的 0.8
     就是 `checklist_undercount` —— 具名拒，不许拿短清单去判产物。

★★ 第二遍的读数**不许把第一遍的清单并进来算**。这是本文件最容易写错的一行：
   `expected = len(set(listed) | set(counted))` 会让 `expected >= len(listed)` 恒成立，
   欠数判定于是**永远不会响** —— 两遍都只看到 8 家时，8 项清单照样当分母，
   6/8 就能过。独立性一旦是自己跟自己比，整套反作弊就只剩一句注释。

★ 材料是**复数**：研究类来源的 `<ref>` 快照是**检索清单**（种子地址表），
  真正的材料是 `<ref>:1..N`。只喂清单，枚举器从地址表里数公司只能数出 0 家。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.checkers.item_coverage import normalize, undercount, ungrounded
from ..core.model import Snapshot
from ..core.ports import Checklist

__all__ = [
    "INDUCED_ITEM_CAP",
    "MATERIAL_CAP",
    "PrefetchCoverage",
    "checklist_problems",
    "parse_count",
    "parse_names",
    "read_materials_text",
    "read_snapshot_text",
]

#: 一项名字最长多少个字符 —— 超过这个长度的"项"通常是一句话，不是项名。
NAME_CAP = 80
#: 一份清单最多接受多少项。上限不是能力，是**防失控**：枚举器偶尔会把整段正文
#: 拆成条目，分母涨到几百项时覆盖判定就没意义了（每项都得写进产物）。
INDUCED_ITEM_CAP = 400
#: 喂给枚举器的材料最多多少字符。超了就截断，并把"被截断"记进清单与事件 ——
#: 截断是"分母为什么少"的第一解释，丢了它，覆盖率低会被误读成模型不听话。
MATERIAL_CAP = 48_000

#: ★★ 提示词的第二句是**实测出来的**，不是修辞。第一版只写"列出所有 {axis}"，同一个
#:   模型、同一份材料回 90 项：技术栈、框架、编程语言、岗位、招聘网站全进来了
#:   （真正的招聘方只有 23 家左右）。分母膨胀 4 倍 ⇒ 覆盖率永远到不了阈值 ⇒
#:   判定全红，而读数是"模型不听话"，其实是**枚举器没被告知什么不算**。
#:   加上"属于它 / 不属于它的几类 / 拿不准不列"之后，同一次调用回到 21 项，全是公司。
_SYSTEM_A = (
    "你是一个枚举器。只输出一个 JSON 数组（字符串数组），不要解释、不要 markdown 代码块。"
)
_SYSTEM_RULES = (
    "\n★ 只列**确实属于「{phrasing}」**的名称；不属于它的最相近的几类（例如属性/规格、"
    "工具与技术、职位与职级、地点、网站与平台）一律不列。"
    "\n★ 拿不准一个词算不算「{phrasing}」，就不要列。宁可少列，不要凑数。"
)
_SYSTEM_B = (
    "你负责独立核对数量。只输出一个 JSON 对象：{\"count\": <整数>, \"names\": [<字符串>]}。"
    "不要解释、不要 markdown 代码块。"
)

_TRAILING = " \t\r\n,，;；、。.:：)）]】》\"'`*#-|"
_LEADING = " \t\r\n-*•·>#0123456789.、)）]】"
_NUMBERING = re.compile(r"^\s*(?:[-*•·>]+|\d+[.、)）]|[（(]\d+[)）])\s*")


def read_snapshot_text(snapshot: Snapshot, *, cap: int = MATERIAL_CAP) -> tuple[str, bool]:  # blame: fail-closed — 缺省即规格给出的保守值；调宽松要显式声明
    """Read a frozen carrier as text. Returns ``(text, truncated)``.

    ``sources`` owns the IO: ``core`` never opens ``body_path`` (``AGENTS.md §1.3``).
    A carrier that cannot be read is a **named** condition for the caller, so this
    raises rather than returning ``""`` -- an empty material would silently produce
    an empty checklist.
    """
    raw = Path(snapshot.body_path).read_bytes()
    text = raw.decode("utf-8", errors="replace")
    if len(text) <= cap:
        return text, False
    return text[:cap], True


def read_materials_text(materials: list[Snapshot] | tuple[Snapshot, ...], *, cap: int = MATERIAL_CAP) -> tuple[str, bool]:  # blame: fail-closed — 缺省即规格给出的保守值；调宽松要显式声明
    """Concatenate several carriers into one material, stopping at ``cap``.

    ★ 分隔符带 ref：枚举器看到的是"这几份材料合起来"，不是一份来源 ——
      这样它不会把第二份的头部当成第一份的续行，也让截断位置可读。
    """
    chunks: list[str] = []
    used = 0
    truncated = False
    for snapshot in materials:
        text, cut = read_snapshot_text(snapshot, cap=cap)
        header = f"\n=== {snapshot.ref} ===\n"
        if used + len(header) + len(text) > cap:
            room = max(0, cap - used - len(header))
            chunks.append(header + text[:room])
            truncated = truncated or cut or room < len(text)
            used = cap
            break
        chunks.append(header + text)
        used += len(header) + len(text)
        truncated = truncated or cut
    return "".join(chunks), truncated


def parse_names(payload: str) -> tuple[str, ...]:
    """Pull a de-duplicated list of names out of a model reply.

    Tolerant on purpose: models wrap JSON in prose or fences, and a reply that is
    not JSON must not become "the material has no items" (that would turn a parse
    bug into a green run).

    ★ 行兜底**只认显式的列表行**（`- x` / `1. x` / `* x`）。早先的版本把任何一行
      都收下：模型回一段解释文字时，解释行会变成"清单项"，而它们天然能在材料里
      找到（是它自己写的）⇒ 分母暴涨、产物必然缺项。方向错了：宁可不解析，
      也不要凭空造分母。
    """
    text = str(payload or "").strip()
    names: list[str] = []
    found = False
    for opener, closer in (("[", "]"), ("{", "}")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start == -1 or end <= start:
            continue
        try:
            parsed = json.loads(text[start : end + 1])
        except (ValueError, TypeError):
            continue
        found = True
        if isinstance(parsed, dict):
            for key in ("names", "items", "list"):
                value = parsed.get(key)
                if isinstance(value, list):
                    names.extend(str(entry) for entry in value)
            break
        if isinstance(parsed, list):
            names.extend(str(entry) for entry in parsed)
            break
    if not found:
        for line in text.splitlines():
            if not _NUMBERING.match(line):
                continue  # 不是列表行 ⇒ 不是一项
            names.append(_NUMBERING.sub("", line))
    cleaned: list[str] = []
    for name in names:
        item = _clean_name(name)
        if item:
            cleaned.append(item)
    return _dedupe(cleaned)


def parse_count(payload: str) -> int:
    """The independent item count a reply states, or ``0`` when it states none."""
    text = str(payload or "")
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start == -1 or end <= start:
            continue
        try:
            parsed = json.loads(text[start : end + 1])
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            for key in ("count", "total", "n"):
                value = parsed.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    return value
                if isinstance(value, str) and value.strip().isdigit():
                    return int(value.strip())
    match = re.search(r"(?:count|总数|共)\D{0,6}(\d+)", text, re.IGNORECASE)
    return int(match.group(1)) if match else 0


def _clean_name(raw: str) -> str:
    item = str(raw or "")
    item = _NUMBERING.sub("", item)
    item = item.strip(_LEADING).strip(_TRAILING)
    item = re.sub(r"^\*\*(.+)\*\*$", r"\1", item).strip()
    item = " ".join(item.split())
    if not item or len(item) > NAME_CAP:
        return ""
    return item


def _dedupe(names: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    kept: list[str] = []
    for name in names:
        key = normalize(name)
        if not key or key in seen:
            continue
        seen.add(key)
        kept.append(name)
        if len(kept) >= INDUCED_ITEM_CAP:
            break
    return tuple(kept)


def checklist_problems(
    items: tuple[str, ...],
    source_text: str,
    expected: int,
) -> tuple[str, ...]:
    """Everything that makes this checklist unfit to judge a product with.

    Returns named codes (not booleans, not a score): the caller refuses the run
    with them, so the report says *why* the fifth gate did not run instead of
    quietly skipping it.

      * ``checklist_undecided`` -- nothing was enumerated;
      * ``checklist_ungrounded`` -- an item does not occur in the material;
      * ``checklist_undercount`` -- the checklist is shorter than an independent
        count says it should be.
    """
    codes: list[str] = []
    if not items:
        return ("checklist_undecided",)
    if ungrounded(items, source_text):
        codes.append("checklist_ungrounded")
    if undercount(items, expected):
        codes.append("checklist_undercount")
    return tuple(codes)


@dataclass
class PrefetchCoverage:
    """The shell-side pre-scan. Two passes, deliberately not sharing state."""

    client: Any
    #: How much of the material one pass sees. Truncation is reported, never hidden.
    material_cap: int = MATERIAL_CAP  # blame: fail-closed — 缺省即规格给出的保守值；调宽松要显式声明
    max_tokens: int = 3_000  # blame: fail-closed — 枚举回复给足 3000 输出；调小会把长清单截成短清单（正是要防的失败）

    def enumerate(
        self,
        axis: str,
        materials: list[Snapshot] | tuple[Snapshot, ...],
        alias: tuple[str, ...] | list[str] = (),  # blame: fail-closed — 缺省即规格给出的保守值；调宽松要显式声明
    ) -> Checklist:
        if isinstance(materials, Snapshot):
            materials = [materials]
        text, truncated = read_materials_text(list(materials), cap=self.material_cap)
        words = [axis, *[str(one) for one in alias]]
        phrasing = "/".join(dict.fromkeys(word for word in words if word))
        #: pass A: the checklist the blocks will actually see.
        rules = _SYSTEM_RULES.replace("{phrasing}", phrasing)
        listed = parse_names(self._ask(_SYSTEM_A + rules, self._list_request(phrasing, text, truncated)))
        #: pass B: an **independent** count, from a differently worded instruction that
        #: is never shown pass A's answer. Its whole job is to disagree.
        counted_reply = self._ask(_SYSTEM_B + rules, self._count_request(phrasing, text, truncated))
        counted_names = parse_names(counted_reply)
        stated = parse_count(counted_reply)
        # ★★ 独立计数的下界**只取第二遍**（外加作者显式声明的那个，由调用方再取 max）。
        #   把 `listed` 并进来会让 `expected >= len(listed)` 恒成立 —— 欠数永远不响。
        independent = max(stated, len(counted_names))
        #: Only items that really occur in the material may become the denominator.
        invented = ungrounded(listed, text)
        drop = set(invented)
        grounded = tuple(item for item in listed if item not in drop)
        return Checklist(
            items=grounded,
            expected=independent,
            axis=axis,
            invented=invented,
            truncated=truncated,
        )

    # -- internals --------------------------------------------------------- #

    def _ask(self, system: str, user: str) -> str:
        response = self.client.complete(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            [],
            self.max_tokens,
        )
        return str(getattr(response, "text", "") or "")

    @staticmethod
    def _list_request(phrasing: str, text: str, truncated: bool) -> str:
        note = "（材料过长，已截断；以你看到的部分为准）" if truncated else ""
        return (
            f"材料原文如下{note}。\n"
            f"请列出材料里作为 {phrasing} **出现过的所有名称**，一项一个字符串，"
            "按材料里的写法（不要翻译、不要补全、不要合并不同项）。\n"
            "只输出 JSON 数组。\n\n=== 材料开始 ===\n"
            f"{text}\n=== 材料结束 ==="
        )

    @staticmethod
    def _count_request(phrasing: str, text: str, truncated: bool) -> str:
        note = "（材料过长，已截断；以你看到的部分为准）" if truncated else ""
        return (
            f"材料原文如下{note}。\n"
            "★ 独立核对：不要参考任何已有的清单，从头数一遍。\n"
            f"材料里一共出现了多少个**不同的 {phrasing}**？逐个写出名字，再给出 count。\n"
            "★ 这个 count 会被拿去核对另一份清单的长度，所以宁可数准，不要估。\n"
            "只输出 JSON 对象 {\"count\": 整数, \"names\": [...]}。\n\n=== 材料开始 ===\n"
            f"{text}\n=== 材料结束 ==="
        )
