#!/usr/bin/env python
"""第五道门（块内覆盖）的探针 —— 每条读数都必须是**真实输出**，不是"通过"。

规格 §6 / 任务书锁 3 要六条：

    ① 主线有效      同一道题加 coverage 前后，覆盖率 0.30 → ≥0.65
    ② 拆预扫描      关掉预扫描 ⇒ 覆盖率掉回 ~0.35
    ③ 拆覆盖义务    保留清单但不给「每项必须出现」⇒ 掉回 ~0.35
    ④ 拆冻住        允许事后改清单 ⇒ 出现假绿（清单短而全中）⇒ 必须能被抓到
    ⑤ 漏项真会响    给一份只写 5 家的产物 ⇒ FAIL 且列出缺的 18 家名字
    ⑥ 短清单要拒    让预扫描只产 8 项（材料实际 23 项）⇒ checklist_undercount 具名拒

★★ ②③④ 是关键：要证明「三条缺一不可」，不能只证明「加了有用」。

用法：

    # 确定性三条（④⑤⑥）—— 零模型调用，随时可跑
    PYTHONPATH=src python tools/coverage_probe.py --section deterministic

    # 真实模型三条（①②③）—— 要端点凭据
    set -a; . ../.v2-creds.env; set +a
    export BOBANANA6_BASE_URL=$SILICONFLOW_BASE_URL BOBANANA6_MODEL=$SILICONFLOW_MODEL \
           BOBANANA6_API_KEY=$SILICONFLOW_API_KEY BOBANANA6_TIMEOUT_S=300
    PYTHONPATH=src python tools/coverage_probe.py --section model --runs 3

★ 分母（gold 清单）是**从材料枚举一次、冻成文件**的：`tools/coverage_probe_gold.json`。
  它进 git，所以每一次读数用的是同一把尺子；`--refresh-gold` 才会重算它。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "src")]

MATERIAL = REPO / "acceptance" / "ab" / "agent-jd-research" / "input" / "refs" / "jd-requirements.md"
GOLD = REPO / "tools" / "coverage_probe_gold.json"
OUTPUT_DIR = REPO / ".bobanana6" / "coverage-probe"

#: 判定时认哪些列名 —— 与契约里的 `coverage.alias` 同义。
ALIAS = ("公司", "厂商", "企业", "公司名称", "企业名称")

TASK_HEAD = (
    "你是一名资料整理员。下面是一份 2026 年 AI Agent / LLM 应用岗的招聘调研材料。\n"
    "请把它整理成一张 markdown 表格，一行一家公司，列为：公司 | 岗位 | 硬性要求 | 薪资量级。\n"
)

TASK_TAIL = "\n只输出表格本身（可以带一段很短的说明），不要写别的。\n"


# --------------------------------------------------------------------------- #
# 公共件
# --------------------------------------------------------------------------- #


def _material_text() -> str:
    return MATERIAL.read_text(encoding="utf-8")


def _client():
    """Build the real client, or return ``None`` with the reason printed."""
    from bobanana6.adapters import LLMSettings, OpenAICompatibleClient

    env = dict(os.environ)
    if not (env.get("BOBANANA6_BASE_URL") or env.get("OPENAI_BASE_URL")):
        for source, target in (
            ("SILICONFLOW_BASE_URL", "BOBANANA6_BASE_URL"),
            ("SILICONFLOW_MODEL", "BOBANANA6_MODEL"),
            ("SILICONFLOW_API_KEY", "BOBANANA6_API_KEY"),
        ):
            if env.get(source) and not env.get(target):
                env[target] = env[source]
    try:
        settings = LLMSettings.from_env(env)
    except Exception as exc:  # noqa: BLE001 - 探针要能"报出没配"，不是崩掉
        print(f"  !! 没有可用的端点：{type(exc).__name__}: {exc}")
        return None, ""
    print(f"  端点 = {settings.base_url}   模型 = {settings.model}")
    return OpenAICompatibleClient(settings), settings.model


def _gold() -> tuple[tuple[str, ...], tuple[str, ...], int]:
    """(oracle, checklist, independent count).

    ``oracle`` is the denominator used to **score** a product; ``checklist`` is what
    the real pre-scan produced and what the +G arm is handed. They are deliberately
    not the same list -- see the fixture's ``_why``.
    """
    payload = json.loads(GOLD.read_text(encoding="utf-8"))
    oracle = tuple(payload["oracle"])
    checklist = tuple(payload.get("checklist") or oracle)
    return oracle, checklist, int(payload.get("checklist_expected") or len(checklist))


def _refresh_gold() -> int:
    """Enumerate the material once with the real pre-scan and freeze the result."""
    from bobanana6.core.model import Snapshot
    from bobanana6.sources.prefetch import PrefetchCoverage, read_materials_text

    client, model = _client()
    if client is None:
        return 2
    snapshot = Snapshot(ref="hunt", body_path=MATERIAL, captured_at="2026-10-20T00:00:00")
    print("  预扫描中（两遍枚举）……", flush=True)
    checklist = PrefetchCoverage(client=client).enumerate("公司", [snapshot], ALIAS)
    text, truncated = read_materials_text([snapshot])
    payload = {
        "_why": "探针的分母：从材料枚举一次、冻成文件。每次读数用同一把尺子；--refresh-gold 才会重算。",
        "material": str(MATERIAL.relative_to(REPO)),
        "material_chars": len(text),
        "material_truncated": truncated,
        "model": model,
        "axis": "公司",
        "items": list(checklist.items),
        "expected": checklist.expected or len(checklist.items),
        "invented": list(checklist.invented),
        "truncated": checklist.truncated,
    }
    GOLD.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  冻结 {len(checklist.items)} 项 → {GOLD.relative_to(REPO)}（expected={payload['expected']}）")
    return 0


def _table(names) -> str:
    lines = ["| 公司 | 岗位 | 硬性要求 | 薪资量级 |", "|---|---|---|---|"]
    lines += [f"| {name} | 工程师 | 看懂 Agent | 40-60K |" for name in names]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# ④⑤⑥ 确定性（零模型调用）
# --------------------------------------------------------------------------- #


def _bar(ratio: float, width: int = 20) -> str:
    filled = round(ratio * width)
    return "█" * filled + "·" * (width - filled)


def section_deterministic() -> int:
    from bobanana6.core.checkers.item_coverage import (
        check_coverage,
        item_present,
        ungrounded,
    )
    from bobanana6.sources.prefetch import checklist_problems

    gold, checklist, expected = _gold()
    material = _material_text()
    print(f"\n分母（人工核出的 oracle）：{len(gold)} 项")
    print(f"  {json.dumps(list(gold), ensure_ascii=False)}")
    if ungrounded(gold, material):
        print(f"  !! oracle 里有原文查不到的项：{ungrounded(gold, material)}")
        return 1
    print(f"预扫描实际产出的清单：{len(checklist)} 项（独立计数 {expected}）")
    print(f"  {json.dumps(list(checklist), ensure_ascii=False)}")

    print("\n" + "=" * 78)
    print("⑤ 漏项真会响 —— 只写 5 家的产物必须 FAIL 且**列出**缺的每一家")
    print("=" * 78)
    five = gold[:5]
    artifact = _table(five)
    result = check_coverage(artifact, gold, ALIAS, 0.65, axis="公司")
    print(f"  产物写了 {len(five)} 家：{json.dumps(list(five), ensure_ascii=False)}")
    print(f"  total={result.total} covered={result.covered} ratio={result.ratio:.4f} "
          f"passed={result.passed} mode={result.mode}")
    print(f"  missing({len(result.missing)}) = {json.dumps(list(result.missing), ensure_ascii=False)}")
    print(f"  fact = {result.fact()}")
    ok_five = result.passed is False and len(result.missing) == len(gold) - 5 and all(
        name in result.fact() for name in result.missing
    )
    print(f"  ⇒ {'PASS' if ok_five else 'FAIL'}: 漏项具名（{len(result.missing)} 家，要求 {len(gold) - 5} 家）")
    if not ok_five:
        return 1

    print("\n" + "=" * 78)
    print("④ 拆冻住 —— 允许事后按产物反推清单 ⇒ 出现假绿；再看 0.8 下界能不能抓到")
    print("=" * 78)
    post_hoc = tuple(name for name in gold if item_present(name, artifact))
    fake = check_coverage(artifact, post_hoc, ALIAS, 0.65, axis="公司")
    print(f"  事后清单 = 产物里出现过的那些项：{len(post_hoc)} 项 {json.dumps(list(post_hoc), ensure_ascii=False)}")
    print(f"  拿它去判同一份产物：covered={fake.covered}/{fake.total} ratio={fake.ratio:.4f} passed={fake.passed}")
    print("  ★ 这就是假绿：清单短而全中。分母跟着产物走，覆盖率永远是 1.0。")
    codes = checklist_problems(post_hoc, material, len(gold))
    print(f"  短清单交给壳的可信性判定：len={len(post_hoc)} expected={len(gold)} "
          f"floor={len(post_hoc) / len(gold):.2f} → codes={list(codes)}")
    caught = "checklist_undercount" in codes
    print(f"  ⇒ {'PASS' if caught else 'FAIL'}: 假绿被 checklist_undercount 抓到")
    if not caught:
        return 1

    print("\n" + "=" * 78)
    print("⑥ 短清单要拒 —— 预扫描只产 8 项（材料实际更多）⇒ 具名拒")
    print("=" * 78)
    short = gold[:8]
    codes = checklist_problems(short, material, len(gold))
    grounded = not ungrounded(short, material)
    print(f"  清单 = {len(short)} 项 {json.dumps(list(short), ensure_ascii=False)}")
    print(f"  每一项都在材料里：{grounded}；len={len(short)} < 0.8×{len(gold)} = {0.8 * len(gold):.1f}")
    print(f"  codes = {list(codes)}")
    ok_six = "checklist_undercount" in codes
    print(f"  ⇒ {'PASS' if ok_six else 'FAIL'}: 壳拒绝拿这份清单去判产物（checklist_undercount）")
    if not ok_six:
        return 1

    print("\n" + "=" * 78)
    print("附：两条负向（漏项码不许错位）")
    print("=" * 78)
    invented = ("字节跳动", "根本不在材料里的公司")
    codes = checklist_problems(invented, "字节跳动 腾讯", 2)
    print(f"  ① 清单里有编造的项 ⇒ codes={list(codes)}（要 checklist_ungrounded，不要误报少项）")
    ok_a = "checklist_ungrounded" in codes and "checklist_undercount" not in codes
    empty = check_coverage(artifact, (), ALIAS, 0.65, axis="公司")
    print(f"  ② 空清单 ⇒ total={empty.total} ratio={empty.ratio:.4f} passed={empty.passed}"
          f"（无依据 ≠ 全覆盖）")
    ok_b = empty.passed is False and empty.total == 0
    print(f"  ⇒ {'PASS' if ok_a and ok_b else 'FAIL'}")
    return 0 if (ok_a and ok_b) else 1


# --------------------------------------------------------------------------- #
# ①②③ 真模型（单次调用，复现实验的"冷启动"形态）
# --------------------------------------------------------------------------- #


def _ask(client, prompt: str, *, max_tokens: int = 3_000) -> str:
    response = client.complete(
        [{"role": "system", "content": "你是一名资料整理员。只输出要求的产物，不要解释。"},
         {"role": "user", "content": prompt}],
        [],
        max_tokens,
    )
    return str(getattr(response, "text", "") or "")


def _arms(checklist: tuple[str, ...]) -> dict[str, str]:
    from bobanana6.agentloop.brief import render_coverage_obligation

    material = _material_text()
    base = TASK_HEAD + "\n=== 材料开始 ===\n" + material + "\n=== 材料结束 ===\n" + TASK_TAIL
    listing = "以下是公司清单（共 {} 项）：\n{}\n\n".format(
        len(checklist), "\n".join(f"   {name}" for name in checklist)
    )
    return {
        # 基线：只给材料与任务（实测 0.30 那一臂）
        "A_基线_直接写": base,
        # +F：给信息，不给义务（实测 0.35）
        "B_只给清单": base + "\n" + listing,
        # ② 拆预扫描：不给壳冻住的清单，让模型自己先数一遍再覆盖
        "C_只有义务_无预扫描": base
        + "\n先自己数一遍材料里出现了哪些公司，再逐项覆盖：\n"
        + "- 材料里的每一家公司都必须出现在表格里\n"
        + "- 某一项信息不足时，那一格写「材料未提及」，但这一行要保留\n"
        + "- 写完自己数一遍：材料里有几家公司，表格交代了几家\n",
        # +G：清单 + 覆盖义务（实测 0.75）
        "D_清单加义务": base + "\n" + listing + render_coverage_obligation(checklist) + "\n",
    }


def _ceiling(oracle: tuple[str, ...], checklist: tuple[str, ...]) -> tuple[float, tuple[str, ...]]:
    """这份清单**最多**能拿到多少分。

    ★ 这是本探针最重要的一行诊断：`+G` 的措辞只说"清单上每一项都要出现"，
      它**没有说"清单之外的不必写"** —— 但模型就是这么理解的（实测）。于是一份
      召回不全的清单会给产物**封顶**：清单里没有的公司，产物那一版也就不写。

      所以"加了 coverage 覆盖率没动"有两种完全不同的原因，必须分开：
        ① 模型不听话（清单里有，产物不写）→ 机制没生效；
        ② **清单本身就漏了几家**（产物写到清单上限为止）→ 瓶颈在预扫描召回。
      把每个臂的读数除以这个上限，两种原因立刻分开。
    """
    from bobanana6.core.checkers.item_coverage import item_present

    reachable = tuple(
        name for name in oracle if any(item_present(name, other) for other in checklist)
    )
    return (len(reachable) / len(oracle) if oracle else 0.0), reachable


def section_model(runs: int, *, replay: bool = False) -> int:
    from bobanana6.core.checkers.item_coverage import check_coverage

    gold, checklist, _expected = _gold()
    client, model = (None, "(replay)") if replay else _client()
    if client is None and not replay:
        return 2
    arms = _arms(checklist)
    print(f"\n同一道题、同一份材料（{len(_material_text())} 字符）、同一把 oracle 尺子（{len(gold)} 项）")
    print(f"+G 臂拿到的清单 = 真实预扫描输出（{len(checklist)} 项）")
    print(f"{'臂':<22}{'run':>4}  {'covered':>7}  {'ratio':>6}  {'pass':>5}  形态")
    print("-" * 78)
    readings: dict[str, list[float]] = {}
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, prompt in arms.items():
        ratios = []
        for index in range(1, runs + 1):
            saved = OUTPUT_DIR / f"{name}-run{index}.md"
            if replay:
                if not saved.is_file():
                    print(f"  !! 缺 {saved.relative_to(REPO)}，无法 replay")
                    return 2
                answer = saved.read_text(encoding="utf-8")
            else:
                answer = _ask(client, prompt)
                saved.write_text(answer, encoding="utf-8")
            result = check_coverage(answer, gold, ALIAS, 0.65, axis="公司")
            ratios.append(result.ratio)
            sample = result.missing[:3]
            print(f"{name:<22}{index:>4}  {result.covered:>3}/{result.total:<3}  "
                  f"{result.ratio:>6.3f}  {result.passed!s:>5}  {_bar(result.ratio)} "
                  f"列={result.mode} 缺{len(result.missing)}项{(' 例:'+ '、'.join(sample)) if sample else ''}")
        readings[name] = ratios
        print(f"{'':<22}{'均值':>4}  {'':>7}  {statistics.mean(ratios):>6.3f}")
        print("-" * 78)

    means = {name: statistics.mean(values) for name, values in readings.items()}
    ceiling, reachable = _ceiling(gold, checklist)
    print(f"\n清单召回上限：oracle {len(gold)} 项里，这份清单**够得着** {len(reachable)} 项 "
          f"⇒ ceiling = {ceiling:.3f}")
    unreachable = [name for name in gold if name not in reachable]
    print(f"  够不着的：{json.dumps(unreachable, ensure_ascii=False)}"
          "   ← 清单里没有它们（或只有别名变体），产物那一版也就不写")
    print(f"\n汇总（同窗、同材料、同尺子；模型 {model}）")
    for name, mean in means.items():
        print(f"  {name:<22} 平均覆盖 {mean:.3f}   {_bar(mean)}")
    base = means["A_基线_直接写"]
    full = means["D_清单加义务"]
    no_prefetch = means["C_只有义务_无预扫描"]
    no_obligation = means["B_只给清单"]
    print("\n判据：")
    checks = [
        ("① 主线有效（+G ≥ 0.65 且比基线高）", full >= 0.65 and full > base, f"{base:.3f} → {full:.3f}"),
        ("② 拆预扫描（义务在、清单不在 ⇒ 应该掉）", no_prefetch < full - 0.10,
         f"{no_prefetch:.3f} vs 满配 {full:.3f}"),
        ("③ 拆覆盖义务（清单在、义务不在 ⇒ 应该掉）", no_obligation < full - 0.10,
         f"{no_obligation:.3f} vs 满配 {full:.3f}"),
    ]
    failed = False
    for label, ok, detail in checks:
        print(f"  {'PASS' if ok else '未复现'}  {label}   读数 {detail}")
        failed = failed or not ok
    #: ★ ②③ 不成立时，先问"是不是清单本身封了顶"，而不是先怪模型。
    if not (checks[1][1] and checks[2][1]):
        print("\n  ②③ 未复现 —— 归因（按顺序看，不要把三种原因混成一句「机制没用」）：")
        ratio_to_ceiling = (full / ceiling) if ceiling else 0.0
        print(f"    满配 / 清单上限 = {full:.3f} / {ceiling:.3f} = {ratio_to_ceiling:.2f}"
              f"  ⇒ {'★ 产物已经写满清单的召回上限：瓶颈在**预扫描召回**，不在模型听话程度' if ratio_to_ceiling >= 0.9 else '模型没写满清单给它的项：瓶颈在**服从度**'}")
        if no_prefetch > full:
            print(f"    ② 反而更高（{no_prefetch:.3f} > {full:.3f}）：不给冻结清单时模型**自己**枚举得更全 ——"
                  " 说明这份清单的召回还不如模型自述，加清单是在**封顶**。")
        if abs(no_obligation - full) < 0.02:
            print(f"    ③ 与满配相同（{no_obligation:.3f} ≈ {full:.3f}）：这个模型拿到清单后**本来就会**逐项写，"
                  " 覆盖义务这句话在它身上没有增量。")
    print(f"\n逐字产物落在 {OUTPUT_DIR.relative_to(REPO)}/")
    return 1 if failed else 0


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(description="块内覆盖（第五道门）探针")
    parser.add_argument("--section", choices=("all", "deterministic", "model"), default="all")
    parser.add_argument("--runs", type=int, default=3, help="每个臂跑几次（默认 3）")
    parser.add_argument("--refresh-gold", action="store_true", help="重新枚举并冻结分母")
    parser.add_argument("--replay", action="store_true", help="复读已落盘的产物重新判分（零模型调用）")
    arguments = parser.parse_args()

    print("块内覆盖探针 · 材料 =", MATERIAL.relative_to(REPO))
    if not GOLD.is_file():
        print(f"  !! 缺少冻结的分母 {GOLD.relative_to(REPO)} —— 先 --refresh-gold 生成再用")
        return 2
    status = 0
    if arguments.section in ("all", "deterministic"):
        status |= section_deterministic()
    if arguments.section in ("all", "model"):
        status |= section_model(arguments.runs, replay=arguments.replay)
    print("\n总判据：", "PASS" if status == 0 else "FAIL")
    return 0 if status == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
