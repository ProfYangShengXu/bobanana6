#!/usr/bin/env bash
# L1 批量跑测：连跑 N 次，每次记 HEAD + 判定 + 运行期读数，追加进 l1_results.jsonl。
#
# 用法（在 WSL 里）：bash l1_batch.sh [次数]
# ★ 环境从 Windows 侧的 secrets 里读（这个脚本跑在 WSL 里，路径是 /mnt/c/...）。
set -u

N=${1:-5}
REPO="/mnt/c/Users/45140/OneDrive/Desktop/项目/bobanana6"
TASK="$REPO/acceptance/ab/text-volume-l1"
SECRETS="/mnt/c/Users/45140/AppData/Local/hermes/secrets/siliconflow.env"
PY312="/mnt/c/Users/45140/AppData/Local/Programs/Python/Python312/python.exe"

cd "$REPO" || exit 1

# --- 环境：从 secrets 里取，补上 /v1 ---
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

echo "base=$BASE  key 长度=${#KEY}"

for i in $(seq 1 "$N"); do
  HEAD=$(git -C "$REPO" rev-parse --short HEAD)
  DIRTY=$(git -C "$REPO" status --porcelain | grep -v "^??" | wc -l)
  echo "=== 第 $i 次 / 共 $N 次  head=$HEAD dirty=$DIRTY  $(date '+%H:%M:%S') ==="

  rm -f /tmp/l1_run.log
  timeout 2700 .venv/bin/python acceptance/ab/text-volume-l1/run_l1.py > /tmp/l1_run.log 2>&1
  rc=$?

  "$PY312" - "$i" "$HEAD" "$rc" <<'PY'
import json, pathlib, re, subprocess, sys
from collections import Counter

i, head, rc = sys.argv[1], sys.argv[2], sys.argv[3]
task = pathlib.Path("C:/Users/45140/OneDrive/Desktop/项目/bobanana6/acceptance/ab/text-volume-l1")
run = task / "_run"
brief = run / "workspace" / "output" / "brief.md"

rec = {"run": int(i), "head": head, "rc": int(rc)}

log = pathlib.Path("/tmp/l1_run.log")
lt = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
for key, pat in (("plan_shape", r"^plan_shape = (\S+)"), ("seconds", r"^耗时 = ([\d.]+)s"),
                 ("block_pass_rate", r"^block_pass_rate = (\S+)")):
    m = re.search(pat, lt, re.M)
    rec[key] = m.group(1) if m else ""

rec["chars_effective"] = 0
rec["cites"] = 0
rec["path_ok"] = brief.is_file()
if brief.is_file():
    t = brief.read_text(encoding="utf-8")
    rec["bytes"] = len(t.encode("utf-8"))
    rec["chars_effective"] = len(re.sub(r"[\s#*->|`\[\]()]", "", t))
    rec["cites"] = len(re.findall(r"\bL(\d{1,5})\b", t))
    rec["head_text"] = t.strip().splitlines()[0][:60] if t.strip() else ""

tree = run / "artifacts" / "tree"
rec["tools"] = {}
try:
    h = subprocess.run(["git", "-C", str(tree), "log", "--format=%h", "-n", "1"],
                       capture_output=True, text=True, timeout=30).stdout.strip()
    if h:
        tr = subprocess.run(["git", "-C", str(tree), "show", f"{h}:T-01/traces.jsonl"],
                            capture_output=True, text=True, timeout=60).stdout
        rows = [json.loads(l) for l in tr.splitlines() if l.strip()]
        rec["tools"] = dict(Counter(str(r.get("tool")) for r in rows))
        rec["calls"] = len(rows)
        cj = subprocess.run(["git", "-C", str(tree), "show", f"{h}:T-01/commit.json"],
                            capture_output=True, text=True, timeout=30).stdout
        m = re.search(r'"summary":\s*"([^"]*)"', cj)
        rec["block_summary"] = (m.group(1) if m else "")[:160]
except Exception as exc:  # noqa: BLE001
    rec["tools_error"] = f"{type(exc).__name__}: {exc}"

# ★ 判定从痕迹树里读（日志里的字段名会变，树的 commit subject 是权威）
try:
    subj = subprocess.run(["git", "-C", str(tree), "log", "--format=%s", "-n", "1"],
                          capture_output=True, text=True, timeout=30).stdout.strip()
    rec["verdict"] = "ok" if "verdict=ok" in subj else ("failed" if "verdict=failed" in subj else subj)
except Exception as exc:  # noqa: BLE001
    rec["verdict"] = f"err:{type(exc).__name__}"

line = json.dumps(rec, ensure_ascii=False)
print("  " + line)
with open(task / "l1_results.jsonl", "a", encoding="utf-8") as fh:
    fh.write(line + "\n")
PY
done

echo
echo "=== 汇总 ==="
"$PY312" - <<'PY'
import json, pathlib
p = pathlib.Path("C:/Users/45140/OneDrive/Desktop/项目/bobanana6/acceptance/ab/text-volume-l1/l1_results.jsonl")
rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
ok = [r for r in rows if r.get("verdict") == "ok"]
print(f"  总轮次 {len(rows)}   块通过 {len(ok)}   通过率 {len(ok)/max(1,len(rows)):.0%}")
for r in rows:
    print("  #%s head=%-8s verdict=%-7s chars=%-6s cites=%-4s path_ok=%-5s calls=%-4s  %s"
          % (r.get("run"), r.get("head"), r.get("verdict"), r.get("chars_effective"),
             r.get("cites"), r.get("path_ok"), r.get("calls"), str(r.get("block_summary"))[:60]))
PY
