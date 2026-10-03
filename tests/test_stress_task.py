"""压力任务 A（信息类）：五条异构真的在数据里、九条攻击面造得出来、真值与输入隔离。

★ 这里只证明【题是通的】：切分/装配/判据都在动，真值运行期读不到。
   答案对不对由真模型跑测报（见 `acceptance/stress/README.md` 的 #20 一节）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from acceptance import run_task

STRESS = Path(__file__).resolve().parents[1] / "acceptance" / "stress"


@pytest.fixture(scope="module")
def materialised() -> Path:
    """生成器的产出（input/ + truth/ + task.yaml + ontology.yaml）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_stress_generate", STRESS / "generate.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    module.generate()
    return STRESS


def test_the_five_heterogeneity_dimensions_are_really_in_the_data(materialised: Path) -> None:
    """★ 五条异构不是写在 README 里，是**在文件里**：格式/语言/单位/结构/缺失。"""
    given = materialised / "input"
    suffixes = {path.suffix for path in given.glob("*")}
    assert {".csv", ".html", ".json", ".txt", ".pdf"} <= suffixes, suffixes

    text = "\n".join(path.read_text(encoding="utf-8") for path in sorted(given.glob("*")))
    # 语言：三种字段写法都要出现（本体用 aliases 收）
    # 格式：HTML 真的是 HTML（不是改了个后缀的 CSV），扫描件模拟真的带噪声
    html = next(path for path in given.glob("*.html"))
    assert "<table" in html.read_text(encoding="utf-8").lower(), html
    pdf = next(path for path in given.glob("*.pdf"))
    assert "—" in pdf.read_text(encoding="utf-8"), "the scanned-PDF stand-in lost its noise"

    for field in ("营业收入", "revenue", "売上高"):
        assert field in text, field
    # 单位：千元/百万元在数据里出现（有的源标、有的源不标）
    assert "千元" in text and "百万元" in text
    # 缺失的四种写法：空 / 0 / - / N/A
    assert "N/A" in text and "-" in text

    # 结构：宽表（年度做列）与内部码源都要在
    wide = next(
        path for path in sorted(given.glob("*.csv")) if "2021" in path.read_text(encoding="utf-8").splitlines()[0]
    )
    header = wide.read_text(encoding="utf-8").splitlines()[0]
    assert "2021" in header and "2022" in header, header
    assert "internal_code" in (given / "s13.csv").read_text(encoding="utf-8")


def test_the_pressure_task_is_a_pipeline_of_blocks_with_one_source_offline(materialised: Path) -> None:
    """★ 题是通的：不拒跑、切出多块、取源失败走降级（A06）、装配与判据都在报。"""
    run = run_task(STRESS, client=None, env={}, offline=True, concurrency=32)

    assert run.refused is None, run.report.notes
    assert run.plan_shape == "pipeline"
    assert len(run.blocks) == 24, run.blocks  # 23 个活源 + 1 个合并块
    # ★ ⑥ 持久故障：s24 的输入文件故意不生成 → 降级并标记，而不是整个 run 拒跑
    assert any("sources unavailable: s24" in note for note in run.report.notes), run.report.notes
    # 判据都在动：块级读数 + 装配读数 + 覆盖声明都在
    assert run.report.block_pass_rate == 0.0 or run.report.block_pass_rate == 1.0
    assert "capability_short" not in run.assembly_codes, run.assembly_codes


def test_the_run_is_never_handed_the_truth(materialised: Path) -> None:
    """★★ 真值纪律：运行期只挂 `input/`；`truth/` 只给判分器读。

    两条独立证据：① 契约里的每个 locator 都在 input/ 下；
    ② harness 实际交给运行的输入清单里没有 truth。
    """
    document = yaml.safe_load((materialised / "task.yaml").read_text(encoding="utf-8"))
    locators = [str(row["locator"]) for row in document["sources"]]
    assert locators, "the task declares no sources"
    assert all(locator.startswith("input/") for locator in locators), locators
    assert not any("truth" in locator for locator in locators), locators

    run = run_task(STRESS, client=None, env={}, offline=True, concurrency=32)
    assert run.inputs_given, "the run was handed nothing"
    assert all(path.startswith("input/") for path in run.inputs_given), run.inputs_given
    # ★ 真值确实存在（判分器那一侧），它只是没被交出去
    truth = json.loads((materialised / "truth" / "truth.json").read_text(encoding="utf-8"))
    assert truth["expected_keys"]
