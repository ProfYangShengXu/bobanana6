#!/usr/bin/env bash
# 两臂对照跑测器：在指定的题目录上连跑 N 次，结果写进独立的 jsonl。
#
# 用法（WSL 里）：bash l1_ab.sh <题目录名> <臂标签> <次数>
#   例: bash l1_ab.sh text-volume-l1-armA armA 5
#
# ★ 代码两臂相同（仓库 HEAD），只有题面 rationale 不同 ⇒ 这样才分得清"改措辞值多少"。
set -u

TASK_DIR_NAME=${1:?题目录名}
LABEL=${2:?臂标签}
N=${3:-5}

REPO="/mnt/c/Users/45140/OneDrive/Desktop/项目/bobanana6"
TASK="$REPO/acceptance/ab/$TASK_DIR_NAME"
SECRETS="/mnt/c/Users/45140/AppData/Local/hermes/secrets/siliconflow.env"
PY312="/mnt/c/Users/45140/AppData/Local/Programs/Python/Python312/python.exe"
OUT="$TASK/l1_results-$LABEL.jsonl"

cd "$REPO" || exit 1

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

echo "### 臂 $LABEL  题=$TASK_DIR_NAME  次数=$N  head=$(git -C "$REPO" rev-parse --short HEAD)  $(date '+%H:%M:%S')"

for i in $(seq 1 "$N"); do
  HEAD=$(git -C "$REPO" rev-parse --short HEAD)
  echo "=== [$LABEL] 第 $i 次 / 共 $N 次  head=$HEAD  $(date '+%H:%M:%S') ==="

  rm -f /tmp/l1_run.log
  timeout "${BB6_ARM_TIMEOUT:-2400}" .venv/bin/python "acceptance/ab/$TASK_DIR_NAME/run_l1.py" > /tmp/l1_run.log 2>&1
  rc=$?

  "$PY312" - "$i" "$HEAD" "$rc" "$TASK_DIR_NAME" "$LABEL" <<'PY'
import json, pathlib, re, subprocess, sys
from collections import Counter

i, head, rc, dirname, label = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
task = pathlib.Path("C:/Users/45140/OneDrive/Desktop/项目/bobanana6/acceptance/ab") / dirname
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

print("  " + json.dumps(rec, ensure_ascii=False))
with open(task / f"l1_results-{label}.jsonl", "a", encoding="utf-8") as fh:
    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
PY
done
echo "### 臂 $LABEL 跑完 $(date '+%H:%M:%S')"
