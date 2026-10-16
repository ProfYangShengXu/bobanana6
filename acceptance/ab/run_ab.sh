#!/usr/bin/env bash
# 两臂对照跑测器（可续跑）：在指定的题目录上连跑 N 次，结果【只追加】进独立的 jsonl。
#
# 用法（WSL 里）：bash acceptance/ab/run_ab.sh <题目录名> <臂标签> <次数>
#   例: bash acceptance/ab/run_ab.sh text-volume-l1-armA armA10 10
#
# ★ 代码两臂相同（仓库 HEAD），只有题面 rationale 不同 ⇒ 这样才分得清"改措辞值多少"。
# ★ 两臂串行、单轮跑法/环境变量/结果字段都【照旧】，本脚本只加"哪些轮次不用重跑"这一层。
#
# ── 续跑的三条规则（缺一不可）─────────────────────────────────────────────
#   ① 已完成 且 head 相同  → 跳过，不重跑（打印"第 N 轮已完成，跳过"）
#   ② 已完成 但 head 不同  → ★ 不跳过，并打警告：这些轮次是【别的代码】跑的，
#                            不能和本次混着比
#   ③ 超时轮（rc=124）     → 算完成，不重跑
#      ★ 理由：中断是读数的一部分。能被续掉的"超时率"等于没有超时率；
#        重跑超时轮会让报出来的通过率比真实的高。
#
# ── 别把已经攒下的东西弄丢 ───────────────────────────────────────────────
#   · 本脚本【不】在启动时删/归档题目录的 _run（痕迹树是累积的，是别的读数的来源）
#   · 结果文件只以 "a" 模式追加，从不重写；本脚本也不在启动时创建它
#   · 跳过的轮次【不碰工作区】：不 rm、不跑 run_l1.py、不写结果
#
# ── ★★ 一个必须写下来的坑：$PY312 是【Windows 的 python】，脚本跑在【WSL】里 ──
#   $PY312=C:/.../Python312/python.exe 是 Win32 程序，而 bash 这边路径是 /mnt/c/...
#   形态。把 /mnt/c/... 直接交给它 → FileNotFoundError: No such file or directory
#   （文件明明在，但那个路径对 Win32 不存在）。
#   实测（本机，c9b9d2d）：
#     "…/python.exe" -c 'open(sys.argv[1])'  /mnt/c/…/l1_results-armA10.jsonl → FileNotFoundError
#     "…/python.exe" -c 'open(sys.argv[1])'  C:/…/l1_results-armA10.jsonl        → ok
#   正解：脚本开头把 REPO 同时存成两个形态 ——
#     REPO_WSL  给 bash 用（/mnt/c/…）
#     REPO_WIN  给 python 用（C:/…）
#   凡是要交给 python 的路径（题目录、**转出来的结果文件路径**）都从 REPO_WIN 拼出来。
#   （等价做法：wslpath -w。这里按任务书硬存两形态，不额外依赖 wslpath。）
#
# 测试开关（默认关，只影响"要不要真的起跑"，不改跑测语义）：
#   BB6_RESUME_DRY_RUN=1  → 本该起跑的轮次只打印命令、不启动进程、不写结果。
set -u

TASK_DIR_NAME=${1:?题目录名}
LABEL=${2:?臂标签}
N=${3:-5}

case "$N" in
  ''|*[!0-9]*) echo "★ 次数必须是整数：$N" >&2; exit 2 ;;
esac

# ★★ 坑见文件头：REPO 存两个形态，bash 用 WSL，python 用 WIN。
REPO_WSL="/mnt/c/Users/45140/OneDrive/Desktop/项目/bobanana6"
REPO_WIN="C:/Users/45140/OneDrive/Desktop/项目/bobanana6"
SECRETS="/mnt/c/Users/45140/AppData/Local/hermes/secrets/siliconflow.env"
PY312="/mnt/c/Users/45140/AppData/Local/Programs/Python/Python312/python.exe"
TASK_WSL="$REPO_WSL/acceptance/ab/$TASK_DIR_NAME"
TASK_WIN="$REPO_WIN/acceptance/ab/$TASK_DIR_NAME"
OUT_WSL="$TASK_WSL/l1_results-$LABEL.jsonl"
OUT_WIN="$TASK_WIN/l1_results-$LABEL.jsonl"

cd "$REPO_WSL" || exit 1

# --- 环境：从 secrets 里取，补上 /v1（与旧跑测器一字不差）---
read_env() { grep -E "^$1=" "$SECRETS" | head -1 | cut -d= -f2- | tr -d '\r' | tr -d '"' | tr -d "'"; }
KEY=$(read_env SILICONFLOW_API_KEY)
BASE=$(read_env SILICONFLOW_BASE_URL)
BASE=${BASE:-https://api.siliconflow.cn}
case "$BASE" in */v1) ;; *) BASE="$BASE/v1" ;; esac

export BOBANANA6_BASE_URL="$BASE"
export BOBANANA6_MODEL="Qwen/Qwen3.5-4B"
export BOBANANA6_API_KEY="$KEY"
export BOBANANA6_EXTRA_BODY='{"enable_thinking": false}'
export BOBANANA6_TPM_LIMIT="120000"
export BOBANANA6_RPM_LIMIT="600"
export BOBANANA6_TIMEOUT_S="300"

CUR_HEAD=$(git -C "$REPO_WSL" rev-parse --short HEAD)
if [ -z "$CUR_HEAD" ]; then
  # head 是续跑的键：读不到就既不能判"跳过"也不能判"别的代码跑的"。
  # 宁可停，也不要把 head 空的记录写进结果文件（那会污染后续所有续跑判定）。
  echo "★ 读不到仓库 HEAD（git 失败：dubious ownership / 不在 git 仓里？），停。" >&2
  echo "  提示：git config --global --add safe.directory '$REPO_WSL'" >&2
  exit 1
fi

echo "### 臂 $LABEL  题=$TASK_DIR_NAME  次数=$N  head=$CUR_HEAD  $(date '+%H:%M:%S')"
echo "### 结果文件（只追加）=$OUT_WSL"

# ── 续跑判定 ────────────────────────────────────────────────────────────
# 把"已完成的轮次"从只追加的结果文件里读回来。交给 Windows python 的是 OUT_WIN
# （C:/ 形态）—— 这正是上面那个坑的落点。
#
# 重复行策略（显式）：同一个 run 号可能有多行（head 不同被重跑过）。本脚本取该 run
# 号【所有】行的 head 集合 —— 只有"全都等于当前 head"才跳过；只要有一行不同，
# 就按规则②警告并重跑。宁可重跑一次，也不把别的代码跑的轮次算进来。
#
# 解析失败/截断的半行【不静默吞掉】：打警告后忽略（文件是 /mnt/c 上的追加日志，
# 强杀/断电可能留下半行）。
decisions=$("$PY312" - "$N" "$CUR_HEAD" "$OUT_WIN" <<'PY'
import json
import pathlib
import sys

n = int(sys.argv[1])
cur = sys.argv[2]
out = sys.argv[3]

by_run = {}
p = pathlib.Path(out)
if p.is_file():
    for lineno, raw in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        s = raw.strip().lstrip("\ufeff")  # 容忍 Windows 编辑器留下的 BOM
        if not s:
            continue
        try:
            rec = json.loads(s)
        except Exception as exc:  # noqa: BLE001
            print(f"[续跑] ★ 结果文件第 {lineno} 行不是合法 JSON（{type(exc).__name__}），"
                  f"已忽略 —— 可能是上次被中断写坏的半行（不静默吞掉）", file=sys.stderr)
            continue
        if not isinstance(rec, dict) or "run" not in rec:
            print(f"[续跑] ★ 结果文件第 {lineno} 行缺 run 字段，已忽略", file=sys.stderr)
            continue
        try:
            run = int(rec["run"])
        except (TypeError, ValueError):
            print(f"[续跑] ★ 结果文件第 {lineno} 行 run 不是整数，已忽略", file=sys.stderr)
            continue
        by_run.setdefault(run, []).append(rec)

for i in range(1, n + 1):
    recs = by_run.get(i)
    if not recs:
        print(f"R {i}")
        continue
    heads = {str(r.get("head")) for r in recs}
    if heads == {cur}:
        # 规则①（含规则③：rc=124 也是"已完成"）
        timed_out = any(str(r.get("rc")) == "124" for r in recs)
        print(f"S {i} {'rc=124(超时，算完成，不重跑)' if timed_out else ''}".rstrip())
    else:
        # 规则②：head 不同 ⇒ 不跳过 + 警告
        got = "、".join(sorted("<无head>" if h == "None" else h for h in heads))
        print(f"[续跑] ★ 警告：第 {i} 轮已完成，但 head={got} ≠ 当前 head={cur}；"
              f"这些轮次是【别的代码】跑的，【不跳过】—— 不能和本次混着比", file=sys.stderr)
        print(f"R {i}")
PY
)
scan_rc=$?
if [ "$scan_rc" != "0" ]; then
  echo "★ 续跑判定失败（$PY312 退出码 $scan_rc），不猜着跑，停。" >&2
  exit 1
fi

# ── 逐轮：跳过或跑 ──────────────────────────────────────────────────────
while IFS=' ' read -r tag idx rest; do
  [ -n "${tag:-}" ] || continue

  if [ "$tag" = "S" ]; then
    # 规则①/③：跳过。★ 不碰工作区。
    if [ -n "${rest:-}" ]; then
      echo "  第 $idx 轮已完成，跳过（head=$CUR_HEAD，$rest）"
    else
      echo "  第 $idx 轮已完成，跳过（head=$CUR_HEAD）"
    fi
    continue
  fi

  # tag = R：这一轮要跑
  HEAD=$(git -C "$REPO_WSL" rev-parse --short HEAD)
  echo "=== [$LABEL] 第 $idx 次 / 共 $N 次  head=$HEAD  $(date '+%H:%M:%S') ==="

  if [ "${BB6_RESUME_DRY_RUN:-0}" = "1" ]; then
    # 测试用：只打印"准备起跑"这一步，不起真模型、不写结果。
    echo "  [dry-run] 本应起跑：timeout ${BB6_ARM_TIMEOUT:-2400} .venv/bin/python acceptance/ab/$TASK_DIR_NAME/run_l1.py"
    echo "  [dry-run] 本应记录：$PY312 - <轮次> <head> <rc> $TASK_WIN $OUT_WIN $LABEL"
    continue
  fi

  rm -f /tmp/l1_run.log
  timeout "${BB6_ARM_TIMEOUT:-2400}" .venv/bin/python "acceptance/ab/$TASK_DIR_NAME/run_l1.py" > /tmp/l1_run.log 2>&1
  rc=$?

  "$PY312" - "$idx" "$HEAD" "$rc" "$TASK_WIN" "$OUT_WIN" "$LABEL" <<'PY'
import json, pathlib, re, subprocess, sys
from collections import Counter

i, head, rc, task_win, out_win, label = (
    sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6])
task = pathlib.Path(task_win)
out = pathlib.Path(out_win)
run = task / "_run"
brief = run / "workspace" / "output" / "brief.md"
tree = run / "artifacts" / "tree"

rec = {"arm": label, "run": int(i), "head": head, "rc": int(rc)}

rec["verdict"] = "?"
rec["calls"] = 0
rec["tools"] = {}
try:
    subj = subprocess.run(["git", "-C", str(tree), "log", "--format=%s", "-n", "1"],
                          capture_output=True, text=True, timeout=30).stdout.strip()
    rec["verdict"] = "ok" if "verdict=ok" in subj else ("failed" if "verdict=failed" in subj else subj)
    h = subprocess.run(["git", "-C", str(tree), "log", "--format=%h", "-n", "1"],
                       capture_output=True, text=True, timeout=30).stdout.strip()
    if h:
        tr = subprocess.run(["git", "-C", str(tree), "show", f"{h}:T-01/traces.jsonl"],
                            capture_output=True, text=True, timeout=60).stdout
        rows = [json.loads(l) for l in tr.splitlines() if l.strip()]
        rec["tools"] = dict(Counter(str(r.get("tool")) for r in rows))
        rec["calls"] = len(rows)
except Exception as exc:  # noqa: BLE001
    rec["tree_error"] = f"{type(exc).__name__}: {exc}"

rec["path_ok"] = brief.is_file()
rec["bytes"] = rec["chars_effective"] = rec["cites"] = 0
if brief.is_file():
    t = brief.read_text(encoding="utf-8")
    rec["bytes"] = len(t.encode("utf-8"))
    rec["chars_effective"] = len(re.sub(r"[\s#*->|`\[\]()]", "", t))
    rec["cites"] = len(re.findall(r"\bL(\d{1,5})\b", t))
    rec["distinct_cites"] = len(set(re.findall(r"\bL(\d{1,5})\b", t)))
    rec["head_text"] = t.strip().splitlines()[0][:60] if t.strip() else ""

def ends_with_newline(path):
    # /mnt/c 上的追加日志可能被强杀在写 "\n" 之前；若上一行没有换行符，
    # 直接追加会把【上一行】和新行粘成一段坏 JSON（两轮一起丢）。
    if not path.is_file() or path.stat().st_size == 0:
        return True
    with open(path, "rb") as fh:
        fh.seek(-1, 2)
        return fh.read(1) == b"\n"


print("  " + json.dumps(rec, ensure_ascii=False))
line = json.dumps(rec, ensure_ascii=False) + "\n"
with open(out, "a", encoding="utf-8") as fh:
    if not ends_with_newline(out):
        fh.write("\n")
    fh.write(line)
PY
  rec_rc=$?
  if [ "$rec_rc" != "0" ]; then
    echo "  ★ 警告：第 $idx 轮的记录写入失败（退出码 $rec_rc），结果文件里【没有】这一轮 —— 重跑时会重跑它"
  fi
done <<< "$decisions"

echo "### 臂 $LABEL 结束 $(date '+%H:%M:%S')"
