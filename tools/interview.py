"""bb6 采访执行器 —— 问小模型，找出壳的调优点。

设计见 `项目/bobanana6/小模型采访设计-找壳的调优点-20261004.md`。
本脚本只负责【问 + 收 + 落盘】，判读由人做。

三条防污染原则（写死在问题模板里，别绕过）：
  要【引用】不要【表态】 · 要【选择】不要【自述】 · 要【重做】不要【解释】

用法：
    python interview.py --archive <归档.jsonl> --block T-24 --out <采访记录.jsonl>
    python interview.py --archive <归档.jsonl> --list      # 先看看有哪些调用可采
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

# ---------------------------------------------------------------- 问句模板
# ★ 每个问题都必须是"要引用/要选择/要重做"，不许出现"你觉得/是不是/为什么错"

QUESTIONS = {
    # ---- A 类：回溯型（问它当时看到什么）证据强度：弱 ----
    "A1": (
        "下面是你之前收到的一段完整输入。请你【逐字】找出其中规定"
        "「你要怎么把那 23 个输入取回来」的那一段（比如用哪个工具、怎么取），"
        "原样抄出来，不要总结、不要改写、不要补全。\n"
        "如果找不到这样的一段，就直接回答「找不到」。"
    ),
    "A2": (
        "下面是你之前收到的一段完整输入。请你【逐字】找出其中规定"
        "「revenue 该用哪个口径」（哪一版定义）的那一段，原样抄出来。\n"
        "如果找不到这样的一段，就直接回答「找不到」。"
    ),
    # ---- B 类：复述型（问它理解到什么）证据强度：中 ----
    "B1": (
        "下面是你之前收到的一段完整输入。请用你自己的话说："
        "这次要交付的东西【长什么样】——有几列、每列叫什么、一行代表什么。"
    ),
    "B2": (
        "下面是你之前收到的一段完整输入。请回答：你怎么判断每一列该填什么？"
        "如果没写，直接回答「没写」。"
    ),
    # ---- C 类：卡点型（问它卡在哪）证据强度：中 ----
    # ★ 必须放在 A/B 之后问，否则它会编
    "C1": (
        "下面是你之前收到的输入。做这一步的时候，哪一部分你最不确定？"
        "★ 请先引用原文里相关的那句话，再说你不确定的点。"
        "如果引用不出原文，直接回答「引用不出」。"
    ),
    "C2": (
        "下面是你之前收到的输入。如果只允许你改【一条】指令让这件事变容易，"
        "你会改哪条？★ 先引用那条指令的原文，再说改成什么。"
    ),
    # ★ C3 也是【要选择不要自述】：把三种情况写成选项，它只能挑一个，
    #   而不是自己编一段"我当时怎么想的"。三种答案对应三种账（a/b/d）。
    "C3": (
        "关于【任务级规则】（主体号怎么写、revenue 用哪个口径、单位怎么换算），"
        "下面三种情况哪一种最接近你当时做的？只回选项字母，再补一句原文依据"
        "（引用不出原文就写「引用不出」）。\n"
        "A. 我从没想过去问它们 —— 原文里也没有任何东西提示我要问。\n"
        "B. 我想过要去问，但不知道该问什么。\n"
        "C. 我确实去问了（或试着问了），但拿到的信息没有影响我的答案。"
    ),
}

# ---- D 类：对照型（给它两版让它做）证据强度：强 ----
# ★ 不是问句，是【任务重放】——见 run_ab()
D_ARM_NOTE = "D 类不用问句：两版指令各跑 N 次，比【能不能交出面板】。"

# ---- E 类：复现型（同上下文 + 允许分步）证据强度：强 ----
E_STEPS = [
    "第 1 步：先只回答「你要交哪几列」。别的都不做。",
    "第 2 步：现在只做【第一行】。",
    "第 3 步：现在做完剩下的行。",
]


# ---------------------------------------------------------------- 归档读取
def load_archive(path: str) -> list[dict]:
    """读归档 JSONL，一条一行。字段约定见采访设计第五节。"""
    rows = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                rows.append({"__parse_error__": True, "line": i, "raw": line[:400]})
    return rows


def list_calls(rows: list[dict]) -> None:
    print(f"共 {len(rows)} 条归档记录")
    seen: dict[str, int] = {}
    for r in rows:
        blk = str(r.get("block") or "?")
        seen[blk] = seen.get(blk, 0) + 1
    for blk, n in sorted(seen.items()):
        print(f"  {blk:12s} {n} 次调用")
    print()
    print("★ 挑选建议：优先采访【失败那一块】的最后一次调用（它最接近出结果的那一刻）")


# ---------------------------------------------------------------- 模型调用
def make_client(cfg: dict):
    """和真跑同一套参数（★ 不许换模型 —— 换了结论不可比）。"""
    from bobanana6.adapters import LLMSettings, OpenAICompatibleClient

    env = {
        "BOBANANA6_BASE_URL": os.environ["BOBANANA6_BASE_URL"],
        "BOBANANA6_API_KEY": os.environ["BOBANANA6_API_KEY"],
        "BOBANANA6_MODEL": cfg.get("model") or os.environ.get("BOBANANA6_MODEL", ""),
        "BOBANANA6_EXTRA_BODY": os.environ.get("BOBANANA6_EXTRA_BODY", "{}"),
        "BOBANANA6_TIMEOUT_S": os.environ.get("BOBANANA6_TIMEOUT_S", "180"),
    }
    return OpenAICompatibleClient(LLMSettings.from_env(env))


def ask(client, messages: list[dict], max_tokens: int = 1500) -> dict:
    t0 = time.time()
    try:
        resp = client.complete(messages, tools=[], max_tokens=max_tokens)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}", "seconds": round(time.time() - t0, 1)}
    text = getattr(resp, "text", None) or getattr(resp, "content", None) or str(resp)
    usage = getattr(resp, "usage", None) or {}
    return {"text": text, "usage": usage, "seconds": round(time.time() - t0, 1)}


# ---------------------------------------------------------------- 主流程
def build_context_message(record: dict) -> dict:
    """把归档里的原始 messages 【原样】拼成一条 —— 不许摘要、不许截断。

    ★ 这是 A1「逐字找出」的前提：原文缺了，问题就废了。
    """
    raw = record.get("messages") or []
    parts = []
    for m in raw:
        role = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, list):
            content = json.dumps(content, ensure_ascii=False)
        parts.append(f"### [{role}]\n{content}")
    body = "\n\n".join(parts)
    return {
        "role": "user",
        "content": (
            "===== 以下是你之前收到的完整输入（原文，未改动）=====\n\n"
            f"{body}\n\n"
            "===== 原文结束 =====\n\n"
            "★ 回答时只依据上面的原文；引用时逐字抄，不许改写或总结。"
        ),
    }


def run_questions(client, record: dict, out_fh, rounds: int, tags: list[str]) -> None:
    base = build_context_message(record)
    for tag in tags:
        q = QUESTIONS.get(tag)
        if q is None:
            continue
        for k in range(rounds):
            msgs = [base, {"role": "user", "content": q}]
            res = ask(client, msgs)
            out_fh.write(json.dumps({
                "kind": "question",
                "tag": tag,
                "round": k,
                "block": record.get("block"),
                "turn": record.get("turn"),
                "question": q,
                "answer": res,
            }, ensure_ascii=False) + "\n")
            out_fh.flush()
            print(f"  {tag} round {k}: {str(res.get('text'))[:110]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", required=True)
    ap.add_argument("--block", default=None, help="只采访这个块")
    ap.add_argument("--out", default="interview-out.jsonl")
    ap.add_argument("--rounds", type=int, default=3, help="★ 每个问题至少 3 次")
    ap.add_argument("--tags", default="A1,A2,B1,C1,C2,C3")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    rows = load_archive(args.archive)
    if args.list:
        list_calls(rows)
        return 0

    # ★ 只要【真的发出去过的调用】：归档里还有 record_verdicts 追加的拒绝理由行，
    #   它们没有 messages —— 挑中它，问句就没有原文可引。
    picked = [r for r in rows if args.block in (None, str(r.get("block"))) and r.get("messages")]
    if not picked:
        print("★ 没挑到记录 —— 检查 --block（要挑带 messages 的调用记录）")
        return 2
    target = picked[-1]          # ★ 取最后一次调用：最接近出结果的那一刻
    print(f"采访对象：block={target.get('block')} turn={target.get('turn')}")
    print("★ 顺序：A → B → C（先确认'看到什么'，再问'理解什么'）")
    print(D_ARM_NOTE)

    client = make_client({})
    with open(args.out, "a", encoding="utf-8") as fh:
        run_questions(client, target, fh, args.rounds, args.tags.split(","))
    print(f"\n落盘：{args.out}")
    print("★ 判读由人做：对着四类 (a)没给 / (b)给了没说清 / (c)做不到 / (d)被干扰")
    return 0


if __name__ == "__main__":
    sys.exit(main())
