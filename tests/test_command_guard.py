"""`shell.run` refuses an inline interpreter script -- and only that.

Background (measured, not a hunch): a block worker rewrote the whole
multi-thousand-character program every turn instead of editing the previous one
-- 14 of 32 `shell.run` calls in one measured run were `python3 << 'EOF'`
heredocs. A program that never lands on disk can never be patched, so
`files.patch` was called zero times.

This file pins the two halves of the guard:

```
① 拒：heredoc 喂解释器 / 管道喂裸解释器 / 长或多行的 `-c` `-e` 代码体
② 不拒（反向，不许误伤）：ls / cat / python3 script.py / 短 -c / 写文件的 heredoc
③ 一拒一次调用：拒的是一条 ToolResult，同一个块继续跑、最终仍交出候选
④ files.patch 的门：本块真的用 files.write 写过文件后，它出现在真实 tool_schemas 里
```

Threshold: :data:`INLINE_CODE_MAX_CHARS` = 200 chars, plus "any newline body".
Rationale in the report; the measured problem scripts were 3k-11k chars.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from acceptance.harness import run_task
from bobanana6.assemble_impl.produce import tool_schemas
from bobanana6.core.model import ModelResponse
from bobanana6.services.command_guard import (
    INLINE_CODE_MAX_CHARS,
    inline_script_problem,
)
from tests.test_convert_three import _block_of, _task, _tool_call
from tests.test_services import _full_toolset

# --------------------------------------------------------------------------- #
# ① 必须拒的形态
# --------------------------------------------------------------------------- #

REJECTED = {
    "python-heredoc": "python3 << 'EOF'\nimport pandas as pd\ndf = pd.read_csv('a.csv')\nprint(df)\nEOF",
    "python-heredoc-unquoted": "python3 <<EOF\nprint(1)\nEOF",
    "python-heredoc-dash": "python3 - <<'PY'\nprint(1)\nPY",
    "node-heredoc": "node << EOF\nconsole.log(1)\nEOF",
    "python-c-multiline": "python3 -c \"import pandas as pd\ndf = pd.read_csv('a.csv')\nprint(df)\"",
    "python-c-long": "python3 -c \"print(" + "1+" * 200 + "1)\"",
    "node-e-long": "node -e \"" + "a" * (INLINE_CODE_MAX_CHARS + 1) + "\"",
    "bash-c-long": "bash -c \"" + "echo hi; " * 40 + "\"",
    "php-r-long": "php -r '" + "echo 1; " * 40 + "'",
    "piped-into-interpreter": "cat <<EOF | python3\nprint(1)\nEOF",
    "echo-piped-into-interpreter": "echo 'print(1)' | python3",
    "wrapped-python-heredoc": "python3 -u - <<'PY'\nprint(1)\nPY",
    # ★ wrapper 后仍要拒（审计实测的绕过：timeout / sudo / nice / env + 选项）
    "timeout-wrapped-heredoc": "timeout 120 python3 << 'EOF'\nprint(1)\nEOF",
    "sudo-wrapped-heredoc": "sudo -u root python3 <<EOF\nprint(1)\nEOF",
    "env-wrapped-heredoc": "env -i python3 <<EOF\nprint(1)\nEOF",
    "nice-wrapped-inline-body": 'nice -n 10 python3 -c "' + "x" * (INLINE_CODE_MAX_CHARS + 1) + '"',
    # ★ 脚本被 heredoc 直接写进文件：仍然是"内联的脚本"，且不留可 patch 的文件
    "heredoc-written-to-script": "cat > gen.py << 'EOF'\nprint(1)\nEOF",
    "heredoc-appended-to-script": "cat <<EOF >> gen.sh\necho hi\nEOF",
    "tee-heredoc-to-script": "tee calc.py <<EOF\nprint(1)\nEOF",
}


@pytest.mark.parametrize("name", sorted(REJECTED))
def test_an_inline_script_is_refused(name: str) -> None:
    problem = inline_script_problem(REJECTED[name])
    assert problem, f"{name} should have been refused"


@pytest.mark.parametrize("name", sorted(REJECTED))
def test_the_refusal_names_violated_expected_got_and_the_way_out(name: str) -> None:
    problem = inline_script_problem(REJECTED[name])
    assert "violated" in problem and "expected" in problem and "got" in problem
    assert "files.write" in problem  # 下一步：先把脚本写成文件
    assert "files.patch" in problem  # 改已有脚本用它


# --------------------------------------------------------------------------- #
# ② 反向：正常命令必须原样放行（高精度低召回 —— 宁可漏，不可错杀）
# --------------------------------------------------------------------------- #

ALLOWED = (
    "ls -la input/",
    "cat input/a.csv",
    "head -5 a.csv",
    "wc -l a.csv",
    "grep -n x a.csv | head",
    "mkdir -p out",
    "python3 script.py",
    "python3 calc.py > out.csv",
    "python3 -m pytest x -q",
    'python3 -c "print(1+1)"',  # 短的一行：放行
    'node -e "console.log(1)"',
    'bash -c "ls -la"',
    "python3 script.py <<EOF\n1,2\nEOF",  # heredoc 是喂给脚本的【数据】，不是代码
    "cat <<EOF > data.txt\nhello\nEOF",  # heredoc 写【数据文件】：本守卫不拒
    "cat > data.txt << 'EOF'\nhello\nEOF",
    "cat <<EOF > notes.md\nx\nEOF",
    "timeout 120 cat a.csv",  # 有 wrapper、没有解释器 ⇒ 不拒
    "sudo -u root cat a.csv",
    "echo 'print(1)' > gen.py",  # 重定向写文件（非 heredoc）：不拒（见报告边界）
    "exec sleep 1",
    "seq -f '%0500g' 1 10",
    'echo python3 -c "not the command"',  # python3 不是这条命令的【命令词】
    'python3 -c "unterminated',  # 解析不了 ⇒ 放过（fail open），不误伤
)


@pytest.mark.parametrize("command", ALLOWED)
def test_ordinary_commands_are_not_refused(command: str) -> None:
    assert inline_script_problem(command) == "", command


def test_an_empty_or_non_string_command_is_not_our_business() -> None:
    assert inline_script_problem("") == ""
    assert inline_script_problem("   ") == ""
    assert inline_script_problem(None) == ""  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# ③ 一拒一次调用：注册表把它折成一条失败结果，块继续跑
# --------------------------------------------------------------------------- #


def _call(registry, name, arguments):  # type: ignore[no-untyped-def]
    raw = {
        "id": f"c-{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }
    return registry.execute((registry.calls([raw])[0],))[0]


def test_a_refused_shell_run_is_a_failed_result_not_an_exception(tmp_path: Path) -> None:
    _tools, registry = _full_toolset(tmp_path)
    result = _call(registry, "shell.run", {"command": REJECTED["python-heredoc"]})
    assert result.ok is False
    assert result.error_type == "WorkspaceError"
    assert "violated" in result.content and "files.patch" in result.content


def test_a_sibling_call_in_the_same_batch_still_runs(tmp_path: Path) -> None:
    """同一个块里，被拒的那一次不影响别的工具（拒的是调用，不是块）。"""
    _tools, registry = _full_toolset(tmp_path)
    blocked = _call(registry, "shell.run", {"command": REJECTED["python-heredoc"]})
    fine = _call(registry, "shell.run", {"command": "printf 'small'"})
    assert blocked.ok is False
    assert fine.ok is True and "small" in fine.content


def test_the_block_keeps_going_after_a_refused_inline_script(tmp_path: Path) -> None:
    """走真链路：T-01 第 2 轮发 heredoc ⇒ 被拒，但第 3 轮仍在、块仍交出候选。"""
    task = _task(tmp_path, name="guard-survives")
    calls: dict[str, int] = {}

    class _Client:
        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            block = _block_of(messages) or "?"
            turn = calls.get(block, 0)
            calls[block] = turn + 1
            if block == "T-01" and turn == 0:
                return ModelResponse(tool_calls=(_tool_call("r1", "sources.snapshot", {"ref": "seed"}),))
            if block == "T-01" and turn == 1:
                return ModelResponse(
                    tool_calls=(
                        _tool_call(
                            "s1",
                            "shell.run",
                            {"command": "python3 << 'PY'\nimport csv\nprint(len(list(csv.reader(['a'])))\nPY"},
                        ),
                    )
                )
            return ModelResponse(
                text=json.dumps(
                    {
                        "ref": "seed",
                        "columns": [{"name": "obligor", "dtype": "str"}],
                        "rows": [{"obligor": "A-1"}],
                        "done": True,
                    }
                )
            )

    run = run_task(task, client=_Client(), env={}, workspace=True)
    # ★ 被拒之后模型仍被问了一次（第 3 轮）—— 拒的是那【一次调用】，不是这个块。
    assert calls.get("T-01", 0) >= 3, calls
    assert "T-01" in run.candidates, sorted(run.candidates)


# --------------------------------------------------------------------------- #
# ④ files.patch 的门：真写过文件之后，它出现在真实的 tool_schemas 里
# --------------------------------------------------------------------------- #


def _names(tools) -> set[str]:  # type: ignore[no-untyped-def]
    return {entry["function"]["name"] for entry in tool_schemas(tools)}


def test_files_patch_is_on_the_menu_after_a_real_files_write(tmp_path: Path) -> None:
    tools, registry = _full_toolset(tmp_path)
    assert "files.patch" not in _names(tools)
    result = _call(registry, "files.write", {"path": "out.csv", "content": "obligor\nA-1\n"})
    assert result.ok is True, result.content
    assert "files.patch" in _names(tools)


def test_a_file_created_by_shell_redirection_does_not_open_the_patch_gate(tmp_path: Path) -> None:
    """★ 如实记下的边界（不改门，不放宽）：shell 重定向造的文件不 `note_write`。

    `core/ports.py::_wrote_a_file` 只看 `WriteBudget.paths`；`shell.run("> out.csv")`
    是执行副作用，不算"写过文件"，所以 `files.patch` **仍不在菜单上**。本守卫已拒
    「heredoc 直接写脚本文件」（`cat > gen.py <<EOF`），但 `printf … > gen.py` 这类
    非 heredoc 的重定向仍在放行 —— 这条路径今天依然开着，报在报告里。
    """
    tools, registry = _full_toolset(tmp_path)
    result = _call(registry, "shell.run", {"command": "printf 'x\\n' > out.csv"})
    assert result.ok is True, result.content
    assert "files.patch" not in _names(tools)


# --------------------------------------------------------------------------- #
# ⑤ 提示词：真实的 system prompt 里带上了新规矩，旧的"鼓励内联"措辞不在
# --------------------------------------------------------------------------- #


def test_the_real_system_prompt_carries_the_script_rule(tmp_path: Path) -> None:
    task = _task(tmp_path, name="prompt-rule")
    systems: list[str] = []
    menus: list[list[dict]] = []

    class _Client:
        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            if not systems:
                systems.append(str(messages[0].get("content", "")))
                menus.append(list(tools))
            return ModelResponse(
                text=json.dumps(
                    {
                        "ref": "seed",
                        "columns": [{"name": "obligor", "dtype": "str"}],
                        "rows": [{"obligor": "A-1"}],
                        "done": True,
                    }
                )
            )

    run_task(task, client=_Client(), env={}, workspace=True)
    system = systems[0]
    # 新规矩在里面
    assert "脚本要落成文件，改动用 `files.patch`" in system
    assert "files.patch" in system
    assert "不要在命令里内联一大段代码" in system
    # 三处旧措辞（把 shell.run 摆在推荐位、零处提 files.patch）都不在了
    assert "你有 `shell.run` / `files.write`" not in system
    assert "用 `shell.run` 让程序算" not in system
    # shell.run 的工具描述也不再"鼓励内联"
    tool = next(entry for entry in menus[0] if entry["function"]["name"] == "shell.run")
    description = tool["function"]["description"]
    assert "files.patch" in description
    assert "不要" in description
