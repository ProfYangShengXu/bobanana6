"""`kind: execution` + `shell.run`'s `expect` assertion (2026-10-17, changes 1 / 2).

The two reverse readings are locked here as deterministic assertions:

```
1. command exits 0 but its output has no marker  => failure (today's `cat test.py`)
2. output has the marker but the command exits nonzero => pass (marker only; exit code is not evidence)
3. the marker sits in the paged-out middle        => still pass (assertion reads the full output)
4. no runner / empty marker                       => named failure, never a silent pass
```

★ The three real DNS readings (bad / good / patched) live in
   `tools/execution_reverse_validation.py`; this file only pins the semantics, so
   the unit suite needs no network and no fixed port.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from bobanana6.contract import parse_contract
from bobanana6.core.checkers import check_kind
from bobanana6.core.checkers.base import CheckContext
from bobanana6.core.model import Candidate, Constraint
from bobanana6.services.checks import execution_check
from tests.test_services import _full_toolset


def _constraint(spec: dict, *, kind: str = "execution", target: str = "dns_server.py") -> Constraint:
    return Constraint(
        id="C-behavior", kind=kind, target=target, spec=spec, rationale="the artifact must really run"
    )


def _candidate() -> Candidate:
    return Candidate(subtask_id="T-01", source_id="T-01")


def _run_execution(spec: dict, *, cwd: Path, with_runner: bool = True):
    context = (
        CheckContext(custom_checks={"execution": execution_check(cwd=cwd)})
        if with_runner
        else CheckContext()
    )
    return check_kind(_constraint(spec), _candidate(), context)


def _call(registry, name: str, arguments: dict):
    raw = {
        "id": f"c-{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }
    return registry.execute((registry.calls([raw])[0],))[0]


# --------------------------------------------------------------------------- #
# 1. `execution`: the marker is the only evidence
# --------------------------------------------------------------------------- #


def test_execution_passes_on_the_marker_even_when_the_command_exits_nonzero(tmp_path: Path) -> None:
    command = f"{sys.executable} -c \"print('5/5 passed'); raise SystemExit(3)\""
    outcome = _run_execution({"command": command, "expect_stdout": "5/5 passed"}, cwd=tmp_path)
    assert outcome.ok is True
    assert any("exited 3" in fact for fact in outcome.facts)
    assert any("not acceptance evidence" in fact for fact in outcome.facts)


def test_execution_fails_when_the_command_exits_zero_without_the_marker(tmp_path: Path) -> None:
    """Today's `cat test_dns.py`: exit 0, but the output carries no acceptance result."""
    command = f"{sys.executable} -c \"print('the file contents, not a test run')\""
    outcome = _run_execution({"command": command, "expect_stdout": "5/5 passed"}, cwd=tmp_path)
    assert outcome.ok is False
    assert any("does not contain the required marker" in fact for fact in outcome.facts)
    assert any("exited 0" in fact for fact in outcome.facts)


def test_execution_reads_stdout_only_never_stderr(tmp_path: Path) -> None:
    command = f"{sys.executable} -c \"import sys; sys.stderr.write('5/5 passed\\n')\""
    outcome = _run_execution({"command": command, "expect_stdout": "5/5 passed"}, cwd=tmp_path)
    assert outcome.ok is False


def test_execution_without_a_registered_runner_fails_closed(tmp_path: Path) -> None:
    outcome = _run_execution(
        {"command": "true", "expect_stdout": "passed"}, cwd=tmp_path, with_runner=False
    )
    assert outcome.ok is False
    assert "no execution runner is registered" in outcome.facts[0]
    assert outcome.cause == "tool"


def test_execution_spec_is_refused_by_name_when_fields_are_missing_or_empty(tmp_path: Path) -> None:
    for spec, needle in (
        ({}, "spec.command"),
        ({"command": ""}, "spec.command"),
        ({"command": "true"}, "spec.expect_stdout"),
        ({"command": "true", "expect_stdout": ""}, "spec.expect_stdout"),
        ({"command": "true", "expect_stdout": "x", "timeout_s": -1}, "timeout_s"),
        ({"command": "true", "expect_stdout": "x", "timeout_s": 9999}, "timeout_s"),
        ({"command": "true", "expect_stdout": "x", "fail_to_pass": "not-a-list"}, "fail_to_pass"),
        ({"command": "true", "expect_stdout": "x", "pass_to_pass": [""]}, "pass_to_pass"),
    ):
        outcome = _run_execution(spec, cwd=tmp_path)
        assert outcome.ok is False, spec
        assert any(needle in fact for fact in outcome.facts), (spec, outcome.facts)


def test_execution_accepts_the_specs_fail_to_pass_and_pass_to_pass_declarations(tmp_path: Path) -> None:
    """They are **authoring-side** skeleton-red / regression-green declarations; this judge only shape-checks them."""
    outcome = _run_execution(
        {
            "command": f"{sys.executable} -c \"print('5/5 passed')\"",
            "expect_stdout": "5/5 passed",
            "fail_to_pass": ["baseline: server not implemented"],
            "pass_to_pass": ["py_compile"],
        },
        cwd=tmp_path,
    )
    assert outcome.ok is True


def test_execution_timeout_is_named_and_does_not_leave_the_process_tree(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    command = (
        f"{sys.executable} -c \""
        "import subprocess, sys, time; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        f"open({str(pid_file)!r}, 'w').write(str(p.pid)); "
        "time.sleep(30)\""
    )
    started = time.monotonic()
    outcome = _run_execution(
        {"command": command, "expect_stdout": "never", "timeout_s": 0.5}, cwd=tmp_path
    )
    elapsed = time.monotonic() - started
    assert outcome.ok is False
    assert any("timed out" in fact for fact in outcome.facts)
    assert any("timed_out=true" in fact for fact in outcome.facts)
    assert elapsed < 15, elapsed
    # The child was in the shell's process group; killing only the shell would
    # leave it asleep for 30s (and, for a test server, holding the port).
    pid = int(pid_file.read_text(encoding="utf-8"))
    stat = Path(f"/proc/{pid}/stat")
    alive = stat.exists() and stat.read_text(encoding="utf-8").split(") ", 1)[1].split(" ", 1)[0] != "Z"
    assert alive is False


# --------------------------------------------------------------------------- #
# ② contract parser accepts kind: execution
# --------------------------------------------------------------------------- #


EXECUTION_CONTRACT = """
task_id: ece4016-dns
goal: write a local DNS server that passes the real test script
version: 2
deliverables:
  - {name: dns_server.py, schema: {content: str}}
sources:
  - {ref: spec, locator: assignment.md}
constraints:
  hard:
    - id: C-behavior
      kind: execution
      target: dns_server.py
      spec:
        command: "python3 test_dns.py dns_server.py"
        expect_stdout: "5/5 通过"
        timeout_s: 120
        fail_to_pass:
          - "baseline: server not implemented"
        pass_to_pass:
          - "py_compile"
      rationale: "服务器必须真的回包；结构完整不算通过"
"""


def test_the_contract_parser_accepts_kind_execution() -> None:
    document = parse_contract(EXECUTION_CONTRACT)
    constraint = document.hard[0]
    assert constraint.kind == "execution"
    assert constraint.spec["expect_stdout"] == "5/5 通过"
    assert constraint.spec["timeout_s"] == 120


# --------------------------------------------------------------------------- #
# 3. `shell.run`'s expect assertion
# --------------------------------------------------------------------------- #


def test_shell_run_expect_mismatch_is_a_failed_result_with_the_structured_payload(tmp_path: Path) -> None:
    _tools, registry = _full_toolset(tmp_path)
    result = _call(registry, "shell.run", {"command": "printf '2/5 passed'", "expect": "5/5 passed"})
    assert result.ok is False
    assert result.error_type == "expectation_not_met"
    payload = json.loads(result.content)
    assert payload["verified"] is False
    assert payload["exit_code"] == 1  # forced: the command's own 0 is not a pass
    assert "5/5 passed" in payload["reason"]
    assert payload["stdout"] == "2/5 passed"


def test_shell_run_expect_match_keeps_the_commands_own_exit_code(tmp_path: Path) -> None:
    _tools, registry = _full_toolset(tmp_path)
    result = _call(
        registry,
        "shell.run",
        {"command": "sh -c \"printf '5/5 passed'; exit 7\"", "expect": "5/5 passed"},
    )
    assert result.ok is True
    payload = json.loads(result.content)
    assert payload["verified"] is True
    assert payload["exit_code"] == 7  # the marker is the verdict; the exit code is reported


def test_shell_run_expect_checks_the_full_output_before_paging(tmp_path: Path) -> None:
    """★ A marker in the omitted middle must still count -- the assertion reads the pre-paging output."""
    _tools, registry = _full_toolset(tmp_path)
    command = (
        f"{sys.executable} -c \"print('\\n'.join("
        "('5/5 passed' if i == 500 else 'filler %d' % i) for i in range(1000)))\""
    )
    result = _call(registry, "shell.run", {"command": command, "expect": "5/5 passed"})
    assert result.ok is True
    payload = json.loads(result.content)
    assert payload["verified"] is True
    assert payload["truncated"] is True
    assert "5/5 passed" not in payload["stdout"]  # it really was in the omitted middle
    assert "5/5 passed" in (tmp_path / "ws" / payload["full_output_path"]).read_text(
        encoding="utf-8"
    )


def test_shell_run_without_expect_keeps_the_pre_change_shape(tmp_path: Path) -> None:
    _tools, registry = _full_toolset(tmp_path)
    result = _call(registry, "shell.run", {"command": "printf 'hello'"})
    assert result.ok is True
    payload = json.loads(result.content)
    assert set(payload) == {"background", "exit_code", "cwd", "stdout", "stderr", "truncated"}
    assert "verified" not in payload


def test_shell_run_refuses_an_empty_expect_and_expect_with_background(tmp_path: Path) -> None:
    _tools, registry = _full_toolset(tmp_path)
    empty = _call(registry, "shell.run", {"command": "true", "expect": "   "})
    assert empty.ok is False
    assert "expect must be a non-empty string" in empty.content
    background = _call(
        registry, "shell.run", {"command": "true", "background": "true", "expect": "x"}
    )
    assert background.ok is False
    assert "background" in background.content


def test_shell_run_expect_timeout_is_a_structured_failure(tmp_path: Path) -> None:
    _tools, registry = _full_toolset(tmp_path)
    result = _call(
        registry,
        "shell.run",
        {"command": "sleep 30", "timeout": 0.5, "expect": "done"},
    )
    assert result.ok is False
    assert result.error_type == "expectation_not_met"
    payload = json.loads(result.content)
    assert payload["verified"] is False
    assert payload["timed_out"] is True
    assert payload["exit_code"] == 124
