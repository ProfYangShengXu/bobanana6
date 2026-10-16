"""★ A failed block's ``commit.json`` records *why* it failed.

The field reading that motivated this: a block judged ``failed`` whose
deliverable was on disk left ``{"verdict": "failed", "summary": ""}`` -- the
work was done, only the envelope was not, and the empty summary taught the next
reader nothing and made the failure unattributable (4 of 5 runs in the batch).

This is a *record* change only: the verdict, the gates and the exits are
untouched. The summary now names the constraint id and the sentence the judge
printed, and a declined block names its refusal code instead of leaving "".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from acceptance.harness import run_task
from bobanana6.core.model import ModelResponse
from tests.test_check_local import _selfcheck_task
from tests.test_convert_three import _tool_call


def test_a_failed_block_records_why_in_its_commit(tmp_path: Path) -> None:
    """★ 判失败不再留空 summary：写入判据 id + 判据自己那句话。"""
    task = _selfcheck_task(tmp_path, name="selfcheck-commit")

    class _FailingClient:
        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            turn = sum(1 for message in messages if message.get("role") == "assistant")
            if turn == 0:
                # Read the source so the answer parses, then fail the *judge's*
                # constraint -- the "envelope fine, rule broken" failure.
                return ModelResponse(tool_calls=(_tool_call("s0", "sources.snapshot", {"ref": "seed"}),))
            if turn == 1:
                return ModelResponse(
                    tool_calls=(_tool_call("w0", "files.write", {"path": "output/out.txt", "content": "short"}),)
                )
            # No `summary`: exactly the shape that used to leave commit.json empty.
            return ModelResponse(text=json.dumps({"ref": "seed", "text": "short"}))

    run = run_task(task, client=_FailingClient(), env={}, workspace=True, max_calls=4)
    assert run.verdicts["T-01"].ok is False, run.verdicts["T-01"].evidence

    manifest = json.loads(
        (task / "_run" / "artifacts" / "tree" / "T-01" / "commit.json").read_text(encoding="utf-8")
    )
    assert manifest["verdict"] == "failed"
    assert manifest["summary"].strip(), "a failed block must say why"
    assert "C-len" in manifest["summary"], manifest["summary"]


def test_a_failed_block_without_a_product_also_records_its_code(tmp_path: Path) -> None:
    """盘上没有产物（信封都没封好）时，summary 仍非空且带具名 code。"""
    task = _selfcheck_task(tmp_path, name="selfcheck-commit-no-file")

    class _ProseClient:
        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            return ModelResponse(text="I am still working on it.")  # 不写文件、信封也不合法

    run = run_task(task, client=_ProseClient(), env={}, workspace=True, max_calls=4)
    assert run.verdicts["T-01"].ok is False, run.verdicts["T-01"].evidence

    manifest = json.loads(
        (task / "_run" / "artifacts" / "tree" / "T-01" / "commit.json").read_text(encoding="utf-8")
    )
    assert manifest["verdict"] == "failed"
    assert manifest["summary"].strip(), "a failed block must say why"
    assert manifest["summary"].startswith("failed:"), manifest["summary"]
