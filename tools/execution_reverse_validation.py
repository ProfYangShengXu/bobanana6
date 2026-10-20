#!/usr/bin/env python
"""Reverse validation for `kind: execution` -- prove the new criterion is loud.

Three readings, each a whole contract parse + real command run (no model):

```
bad      today's artifact  -> MUST be red   (it never answers a query)
good     an implementation that really replies -> MUST be green
patched  bad + the one-line "sendto is reachable" fix -> MUST STILL be red
```

The third reading is the important one: "I added the missing line" is not the
same as "the check passes" -- the artifact is still missing the rest. A criterion
that turns green after any single edit is not measuring the task.

The bad/good artifacts and the test script are task files, passed on the CLI; the
judge itself is structural (`kind: execution` + `expect_stdout`) and knows nothing
about DNS.

Run:
    python tools/execution_reverse_validation.py \
        --bad  ../../ECE4016-Assignment1-bb6/_run/workspace/dns_server.py \
        --good ../../ECE4016-Assignment1-bb6/dns_server.py \
        --test-script ../../ECE4016-Assignment1-bb6/test_dns.py

Exit: 0 only when bad=red, good=green, patched=red.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from bobanana6.contract import parse_contract  # noqa: E402
from bobanana6.core.checkers import check_kind  # noqa: E402
from bobanana6.core.checkers.base import CheckContext  # noqa: E402
from bobanana6.core.model import Candidate  # noqa: E402
from bobanana6.services.checks import execution_check  # noqa: E402

BAD = REPO.parent / "ECE4016-Assignment1-bb6" / "_run" / "workspace" / "dns_server.py"
GOOD = REPO.parent / "ECE4016-Assignment1-bb6" / "dns_server.py"
TEST_SCRIPT = REPO.parent / "ECE4016-Assignment1-bb6" / "test_dns.py"
EXPECT = "5/5 通过"

#: The given bad artifact *has* a `sendto`, but the gate above it is inverted
#: (`if not (flags & 0x8000): continue` skips every query), so the line is never
#: reached for a real query. "Add the sendto line" therefore means "make it
#: reachable". The response it then sends is still a malformed packet, so the test
#: still says no -- exactly the third reading we need.
PATCH_OLD = "if not (flags & 0x8000):  # Not a response"
PATCH_NEW = "if (flags & 0x8000):  # response packet, not a query to answer"


def _contract(target: str, script: str, expect: str, timeout_s: int) -> str:
    return f"""
task_id: execution-reverse-validation
goal: prove that kind execution is red on the broken artifact and green on a working one
version: 1
deliverables:
  - {{name: {target}, schema: {{content: str}}}}
sources:
  - {{ref: spec, locator: {script}}}
constraints:
  hard:
    - id: C-behavior
      kind: execution
      target: {target}
      spec:
        command: "python3 {script} {target}"
        expect_stdout: "{expect}"
        timeout_s: {timeout_s}
        fail_to_pass: ["baseline: server not implemented"]
        pass_to_pass: ["py_compile"]
      rationale: "the server must answer a real query; looking complete is not passing"
"""


def _run_case(name: str, artifact: Path, *, target: str, script: str, expect: str, timeout_s: int) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"bb6-exec-{name}-") as raw:
        workspace = Path(raw)
        shutil.copyfile(TEST_SCRIPT, workspace / script)
        shutil.copyfile(artifact, workspace / target)
        document = parse_contract(_contract(target, script, expect, timeout_s))
        constraint = document.hard[0]
        outcome = check_kind(
            constraint,
            Candidate(subtask_id="T-01", source_id="T-01"),
            CheckContext(custom_checks={"execution": execution_check(cwd=workspace)}),
        )
        return {
            "case": name,
            "ok": outcome.ok,
            "reading": "green" if outcome.ok else "red",
            "facts": list(outcome.facts),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bad", type=Path, default=BAD)
    parser.add_argument("--good", type=Path, default=GOOD)
    parser.add_argument("--test-script", type=Path, default=TEST_SCRIPT)
    parser.add_argument("--target", default="dns_server.py")
    parser.add_argument("--script", default="test_dns.py")
    parser.add_argument("--expect", default=EXPECT)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--patch-old", default=PATCH_OLD)
    parser.add_argument("--patch-new", default=PATCH_NEW)
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args(argv)

    for path in (args.bad, args.good, args.test_script):
        if not path.is_file():
            raise SystemExit(f"missing task file: {path}")

    # The patched copy is derived, never written back: the bad artifact stays as-is.
    with tempfile.TemporaryDirectory(prefix="bb6-exec-patched-src-") as raw:
        patched = Path(raw) / args.target
        original = args.bad.read_text(encoding="utf-8")
        if args.patch_old not in original:
            raise SystemExit(f"the bad artifact no longer contains the documented patch anchor: {args.patch_old!r}")
        patched.write_text(original.replace(args.patch_old, args.patch_new, 1), encoding="utf-8")
        readings = [
            _run_case("bad", args.bad, target=args.target, script=args.script, expect=args.expect, timeout_s=args.timeout),
            _run_case("good", args.good, target=args.target, script=args.script, expect=args.expect, timeout_s=args.timeout),
            _run_case("patched", patched, target=args.target, script=args.script, expect=args.expect, timeout_s=args.timeout),
        ]

    expected = {"bad": False, "good": True, "patched": False}
    passed = all(row["ok"] is expected[row["case"]] for row in readings)
    if args.json:
        print(json.dumps({"readings": readings, "as_expected": passed}, ensure_ascii=False, indent=2))
    else:
        for row in readings:
            print(f"[{row['reading'].upper():5}] {row['case']}")
            for fact in row["facts"]:
                print(f"         {fact}")
            print()
        print(f"as expected (bad=red good=green patched=red): {passed}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
