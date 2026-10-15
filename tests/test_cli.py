"""cli/: the single assembly point -- it must wire, not judge."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from bobanana6 import cli
from bobanana6.adapters import (
    EchoToolResultClient,
    FileOntology,
    OpenAICompatibleClient,
    ScriptedClient,
)
from bobanana6.assemble_impl import BlockRuntime, ContractConstraintSource, Runner
from bobanana6.decompose import Decomposer

ONTOLOGY = """
domain: credit-risk
version: "2026.1"
concepts:
  obligor: {label: Obligor, kind: entity}
  amount: {label: Amount, kind: measure, unit: CNY}
units:
  amount: {symbol: CNY, scale: 1.0, base: CNY}
"""
TASK = """
task_id: t-cli
goal: build the panel
deliverables:
  - {name: out.csv, schema: {obligor: str, amount: float}}
sources:
  - {ref: src-1, locator: CSV_PATH}
constraints:
  hard:
    - {id: C-1, kind: presence, target: "*", spec: {min_rows: 1}, rationale: "no empty panels"}
notes: cli note
"""


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    csv = tmp_path / "panel.csv"
    csv.write_text("obligor,amount\nA-1,100\nA-2,200\n", encoding="utf-8")
    ontology = tmp_path / "ontology.yaml"
    ontology.write_text(ONTOLOGY, encoding="utf-8")
    task = tmp_path / "task.yaml"
    task.write_text(TASK.replace("CSV_PATH", str(csv)), encoding="utf-8")
    return task, ontology


def _args(tmp_path: Path, task: Path, ontology: Path | None = None, **overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "command": "run",
        "task": str(task),
        "ontology": "" if ontology is None else str(ontology),
        "out": str(tmp_path / "reports"),
        "snapshots": str(tmp_path / "snapshots"),
        "strategy": "source",
        "offline": True,
        "offline_script": "",
        "max_calls": 32,
        "max_wall_s": 60,
        "max_output": 2048,
        "context_length": 32768,
        "concurrency": 8,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# --------------------------------------------------------------------------- #
# build_app: the only place that constructs implementations
# --------------------------------------------------------------------------- #


def test_build_app_wires_every_collaborator(tmp_path: Path) -> None:
    task, ontology = _fixture(tmp_path)
    app = cli.build_app(_args(tmp_path, task, ontology))

    assert isinstance(app.runner, Runner)
    assert isinstance(app.runner.runtime, BlockRuntime)
    assert isinstance(app.runner.constraint_source, ContractConstraintSource)
    assert isinstance(app.runner.decomposer, Decomposer)
    assert isinstance(app.runner.ontology_port, FileOntology)
    assert isinstance(app.runner.runtime.client, EchoToolResultClient)
    assert app.runner.report_dir == tmp_path / "reports"
    # ★ capability matching can only pass when the declared pool matches the plan
    assert {worker.worker_id for worker in app.runner.worker_pool or ()} == {"worker:reader", "worker:writer"}


def test_build_app_uses_a_real_client_when_not_offline(tmp_path: Path) -> None:
    from bobanana6.adapters import LLMConfigError

    task, ontology = _fixture(tmp_path)
    args = _args(tmp_path, task, ontology, offline=False)
    with pytest.raises(LLMConfigError):
        cli.build_app(args, env={})  # no endpoint configured -> refuse rather than guess
    app = cli.build_app(args, env={"BOBANANA6_BASE_URL": "http://x.invalid/v1", "BOBANANA6_MODEL": "m"})
    assert isinstance(app.runner.runtime.client, OpenAICompatibleClient)


def test_build_app_can_load_a_scripted_offline_model(tmp_path: Path) -> None:
    task, ontology = _fixture(tmp_path)
    script = tmp_path / "script.json"
    script.write_text(json.dumps([{"text": '{"rows": []}'}]), encoding="utf-8")
    app = cli.build_app(_args(tmp_path, task, ontology, offline=False, offline_script=str(script)))
    assert isinstance(app.runner.runtime.client, ScriptedClient)


def test_build_app_drafts_the_ontology_only_when_there_is_none_to_find(tmp_path: Path) -> None:
    """★ 起草是**最后一档**，不是"没给 `--ontology`"这一档。

    改动说明（2026-10-15）：这条原来断言的是"不给 `--ontology` ⇒ 走起草"。
    加了同目录自动发现之后，那个断言不再成立 —— 而它原来能过，只是因为
    `_fixture` 恰好也在同目录写了一份 `ontology.yaml`，等于**测了一件假事**。
    现在它测的才是真约定：显式给了用给的，没给就看同目录，两处都没有才起草。
    """
    from bobanana6.adapters import DraftingOntology

    task, ontology = _fixture(tmp_path)
    ontology.unlink()  # ★ 同目录那份也拿掉 —— 这才叫"没有本体可找"
    app = cli.build_app(_args(tmp_path, task))
    assert isinstance(app.runner.ontology_port, DraftingOntology)


def test_build_app_reads_the_ontology_once_so_a_bad_file_fails_early(tmp_path: Path) -> None:
    task, _ = _fixture(tmp_path)
    broken = tmp_path / "broken.yaml"
    broken.write_text("domain: d\n", encoding="utf-8")
    with pytest.raises(Exception, match="required property"):
        cli.build_app(_args(tmp_path, task, broken))


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def test_main_runs_end_to_end_and_writes_the_reports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    task, ontology = _fixture(tmp_path)
    code = cli.main(
        [
            "run",
            str(task),
            "--ontology",
            str(ontology),
            "--offline",
            "--out",
            str(tmp_path / "reports"),
            "--snapshots",
            str(tmp_path / "snapshots"),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "exit      : passed" in out
    assert "T-01   passed" in out
    assert "assembly  : passed=True" in out
    summary = json.loads((tmp_path / "reports" / "summary.json").read_text(encoding="utf-8"))
    assert summary["task"]["notes"] == "cli note"
    assert {path.name for path in (tmp_path / "reports").iterdir()} == {
        "SOURCES.md",
        "CONFLICTS.md",
        "ONTOLOGY-MAP.md",
        "summary.json",
    }


def test_main_reports_a_configuration_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    task, ontology = _fixture(tmp_path)
    code = cli.main(["run", str(task), "--ontology", str(ontology), "--out", str(tmp_path / "r")], env={})
    assert code == 2
    assert "configuration error" in capsys.readouterr().err


def test_main_reports_a_refusal_with_its_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _, ontology = _fixture(tmp_path)
    # point the source at a file that does not exist -> the fetch gate refuses
    broken = tmp_path / "broken-task.yaml"
    broken.write_text(TASK.replace("CSV_PATH", str(tmp_path / "nope.csv")), encoding="utf-8")
    code = cli.main(
        [
            "run",
            str(broken),
            "--ontology",
            str(ontology),
            "--offline",
            "--out",
            str(tmp_path / "r"),
            "--snapshots",
            str(tmp_path / "s"),
        ]
    )
    err = capsys.readouterr().err
    assert code == 1
    assert "refused (sources)" in err
    assert "fetch_failed" in err


def test_main_reports_a_missing_task_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["run", str(tmp_path / "nope.yaml"), "--offline"])
    assert code == 2
    assert "cannot start" in capsys.readouterr().err


def test_run_command_requires_a_task_argument() -> None:
    with pytest.raises(SystemExit):
        cli.main(["run"])


# --------------------------------------------------------------------------- #
# 本体的发现顺序：显式 --ontology > 同目录 ontology.yaml > 模型起草
# --------------------------------------------------------------------------- #


def test_a_sibling_ontology_is_used_without_being_asked(tmp_path: Path) -> None:
    """★ 任务旁边的 `ontology.yaml` 就该被用上。

    摆在旁边却不被使用是个**安静**的坑：用户以为本体已经给了，实际却去走
    "模型起草"，而离线模型起草不出合法本体 —— 报出来的是一句跟"你没给本体"
    完全对不上的话（`Additional properties are not allowed ('columns', 'rows' ...)`）。
    """
    task, _ = _fixture(tmp_path)  # 同目录已经放了 ontology.yaml
    app = cli.build_app(_args(tmp_path, task, ontology=None))
    assert isinstance(app.runner.ontology_port, FileOntology)


def test_an_explicit_ontology_wins_over_the_sibling(tmp_path: Path) -> None:
    """显式给的那份优先级最高 —— 同目录那份只是缺省，不许压过用户的选择。"""
    task, _ = _fixture(tmp_path)
    other = tmp_path / "elsewhere.yaml"
    other.write_text(ONTOLOGY.replace("2026.1", "2027.2"), encoding="utf-8")
    app = cli.build_app(_args(tmp_path, task, other))
    assert isinstance(app.runner.ontology_port, FileOntology)
    assert app.runner.ontology_port.path == other
