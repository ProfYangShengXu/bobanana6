"""探针本身也要有守卫：一条命令能复现全部读数。

★ 这条测试的存在意义：`tools/research_probe.py` 是对外交付物（验收方亲手跑的
  就是它）。如果哪天取数层退回"空清单照写 / 不复用台账 / 降级不标"，探针必须红，
  而不是等人肉去跑。
"""

from __future__ import annotations

from tools.research_probe import run_probe


def test_the_probe_reproduces_every_reading() -> None:
    """五组读数全绿；每组都带一个反证（把机制关掉，读数必须变红）。"""
    checks = run_probe()

    assert [check.name for check in checks] == [
        "可复现性（红线）",
        "检索真的接上了",
        "降级可见",
        "直连优先在省",
        "信任分层",
    ], "探针的读数分组变了 —— 验收方按这五组对账"
    failed = [f"{check.name}: {check.reading} / {check.reverse}" for check in checks if not check.passed]
    assert not failed, "探针读数为红：\n" + "\n".join(failed)

    by_name = {check.name: check for check in checks}
    # ★ 红线读数是具体的，不是"跑通了"
    assert by_name["可复现性（红线）"].reading["byte_identical"] is True
    assert by_name["可复现性（红线）"].reading["requests_run2"] == 0
    assert by_name["可复现性（红线）"].reverse["materials_changed"] is True
    assert by_name["降级可见"].reading["backend"] == "jina"
    assert by_name["降级可见"].reading["degraded"] is True
    assert by_name["降级可见"].reverse["remove degraded flag"] is False
    assert by_name["直连优先在省"].reading["api"]["requests"] == 1
    assert by_name["直连优先在省"].reading["search_then_fetch"]["requests"] == 2
    assert by_name["检索真的接上了"].reverse["research_off_refusal"] == ["research_unavailable"]
    assert by_name["检索真的接上了"].reverse["no_evidence_refusal"] == ["research_no_evidence"]
