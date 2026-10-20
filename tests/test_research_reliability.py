"""研究类取数的**失败模式与元数据** —— 五条出口、信任分层、重放、直连优先。

零模型、确定性、不碰真网络（假站都在 127.0.0.1）。

★ 这里每一条都对应任务书锁 2 的「值到达哪里算完成」：
  不是"实现了熔断"，而是"把首选后端打成 403 ⇒ backend=<备选> 且 degraded=True，
  且报告里真的出现这两样"。
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest

from bobanana6.core.model import ResearchBudget, ResearchQuestion, Snapshot
from bobanana6.report.builders import render_sources
from bobanana6.sources import research as R
from bobanana6.sources.store import SnapshotStore, SourceLedger

# --------------------------------------------------------------------------- #
# 假站：每条出口一个路径
# --------------------------------------------------------------------------- #

FORBIDDEN = "/forbidden"
LOGIN = "/login"
SLOW = "/slow"
EMPTY = "/empty"
EMPTY_FIXED = "/empty-fixed"
OK = "/ok"
SEARCH = "/search"
STALE = "/stale"
API = "/api/item"


class _Site(BaseHTTPRequestHandler):
    hits: ClassVar[list[str]] = []
    #: 可变的正文版本 —— 用来证明"删掉台账之后真的会拿到不一样的内容"
    revision: ClassVar[str] = "v1"

    def do_GET(self) -> None:
        type(self).hits.append(self.path)
        host = f"http://127.0.0.1:{self.server.server_address[1]}"  # type: ignore[attr-defined]
        if self.path.startswith(FORBIDDEN):
            self.send_response(403)
            self.end_headers()
            return
        if self.path.startswith(LOGIN):
            self.send_response(401)
            self.end_headers()
            return
        if self.path.startswith(SLOW):
            time.sleep(1.0)  # 客户端 timeout_s 设得比它短
            self._write(b"slow body", "text/markdown")
            return
        if self.path == EMPTY:
            self._write(b"", "text/markdown")
            return
        if self.path.startswith("/render/"):
            self._write(b"rendered via the fallback backend", "text/markdown")
            return
        if self.path == SEARCH:
            # ★ 正文要够长：本地转换的"这页真有内容"门槛是 200 可见字符
            filler = "".join(f"<p>检索结果说明第 {i} 段，用来越过本地转换门槛。</p>" for i in range(12))
            body = (
                f"<html><body><h1>search results</h1>"
                f'<a href="{host}{STALE}">the only result</a>{filler}</body></html>'
            ).encode()
            self._write(body, "text/html; charset=utf-8")
            return
        if self.path == STALE:
            self._write(b'{"version": "1.0.0", "from": "search"}', "application/json")
            return
        if self.path == API:
            self._write(b'{"version": "2.0.0", "from": "api"}', "application/json")
            return
        if self.path == "/huge":
            self._write(b"x" * 5000, "text/markdown")
            return
        if self.path == "/html-seed":
            filler = "".join(f"<p>种子页正文第 {i} 段，用来越过本地转换门槛。</p>" for i in range(12))
            body = (
                f"<html><body><h1>seed</h1>"
                f'<a href="{host}/ok">one</a>'
                f'<a href="{host}/empty-fixed">two</a>'
                f'<a href="{host}/logo.png">media</a>'
                f"{filler}</body></html>"
            ).encode()
            self._write(body, "text/html; charset=utf-8")
            return
        if self.path == EMPTY_FIXED:
            self._write(f"the page after one paraphrase ({type(self).revision})".encode(), "text/markdown")
            return
        self._write(f"ok body ({type(self).revision})".encode(), "text/markdown")

    def _write(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return


@pytest.fixture
def site() -> ThreadingHTTPServer:
    _Site.hits = []
    _Site.revision = "v1"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _base(server: ThreadingHTTPServer) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


def _researcher(
    root: Path, *, channels: tuple[str, ...] = (R.BACKEND_DIRECT,), **kwargs: Any
) -> R.LinkExpandingResearch:
    return R.LinkExpandingResearch(
        store=SnapshotStore(root=root),
        ledger=SourceLedger(root=root),
        captured_at="2026-10-20T12:00:00",
        channels=channels,
        **kwargs,
    )


def _question(seed: str, *, ref: str = "hunt", question: str = "找一个东西") -> ResearchQuestion:
    return ResearchQuestion(ref=ref, question=question, seeds=(seed,))


# --------------------------------------------------------------------------- #
# 出口①②：403/429 换后端；超时重试 1 次
# --------------------------------------------------------------------------- #


def test_a_forbidden_backend_falls_through_to_the_next_one_and_says_so(
    site: ThreadingHTTPServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★★ 纪律②：403 ⇒ 换后端，**不重试**；结果标 backend=<备选> + degraded=True。"""
    base = _base(site)
    monkeypatch.setattr(R, "JINA_PREFIX", f"{base}/render/")
    found = _researcher(tmp_path, channels=(R.BACKEND_DIRECT, R.BACKEND_JINA)).research(
        _question(f"{base}{FORBIDDEN}"), ResearchBudget(max_sources=1, max_depth=0)
    )

    materials = [snap for snap in found if snap.ref != "hunt"]
    assert len(materials) == 1
    assert materials[0].backend == R.BACKEND_JINA
    assert materials[0].degraded is True
    # ★ 403 只打了一次（不是"重试到超时"），备选只打了一次
    assert _Site.hits.count(FORBIDDEN) == 1, f"403 被重试了：{_Site.hits}"
    assert any(hit.startswith("/render/") for hit in _Site.hits)


def test_a_timeout_is_retried_once_then_the_backend_is_cut_off(
    site: ThreadingHTTPServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 连接超时 ⇒ 同一后端重试 1 次；再超时才熔断换后端。

    读数：`/slow` 被打了 **2 次**（1 次 + 1 次重试），备选后端拿到结果。
    """
    base = _base(site)
    monkeypatch.setattr(R, "JINA_PREFIX", f"{base}/render/")
    found = _researcher(tmp_path, channels=(R.BACKEND_DIRECT, R.BACKEND_JINA), timeout_s=0.15).research(
        _question(f"{base}{SLOW}"), ResearchBudget(max_sources=1, max_depth=0)
    )

    assert _Site.hits.count(SLOW) == 2, f"超时没有恰好重试一次：{_Site.hits}"
    materials = [snap for snap in found if snap.ref != "hunt"]
    assert materials and materials[0].backend == R.BACKEND_JINA and materials[0].degraded


# --------------------------------------------------------------------------- #
# 出口③：空结果换措辞 1 次（不是熔断）
# --------------------------------------------------------------------------- #


def test_an_empty_answer_gets_one_paraphrase_before_switching_backend(
    site: ThreadingHTTPServer, tmp_path: Path
) -> None:
    """★ 200-空不是错：换措辞 1 次；换完拿到内容 ⇒ 仍是直连、**不**标降级。"""
    base = _base(site)
    calls: list[str] = []

    def reword(locator: str) -> str:
        calls.append(locator)
        return locator.replace(EMPTY, EMPTY_FIXED)

    found = _researcher(tmp_path, rewrite=reword).research(
        _question(f"{base}{EMPTY}"), ResearchBudget(max_sources=1, max_depth=0)
    )

    assert calls == [f"{base}{EMPTY}"], "换措辞应当只发生一次"
    materials = [snap for snap in found if snap.ref != "hunt"]
    assert materials and materials[0].backend == R.BACKEND_DIRECT
    assert materials[0].degraded is False, "换了措辞但仍是直连拿到完整正文 ⇒ 不该标降级"
    body = SnapshotStore(root=tmp_path).read(materials[0])
    assert b"paraphrase" in body


def test_an_empty_answer_without_a_rephrase_goes_to_the_next_backend(
    site: ThreadingHTTPServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有 `rewrite` 可换 ⇒ 直接换后端；**不**把空结果当熔断。"""
    base = _base(site)
    monkeypatch.setattr(R, "JINA_PREFIX", f"{base}/render/")
    found = _researcher(tmp_path, channels=(R.BACKEND_DIRECT, R.BACKEND_JINA)).research(
        _question(f"{base}{EMPTY}"), ResearchBudget(max_sources=1, max_depth=0)
    )
    materials = [snap for snap in found if snap.ref != "hunt"]
    assert materials and materials[0].backend == R.BACKEND_JINA


# --------------------------------------------------------------------------- #
# 出口④：需要登录态 → 具名交回上层（不是失败、不是"不许"）
# --------------------------------------------------------------------------- #


def test_a_login_required_backend_is_an_authorization_exit_not_a_crash(
    site: ThreadingHTTPServer, tmp_path: Path
) -> None:
    """★ 401 ⇒ `ResearchNeedsAuthorization`，消息里点名"login required"。

    这是"交回上层"的出口：块不自己持有别人的会话，也不把它当普通失败吞掉。
    """
    base = _base(site)
    with pytest.raises(R.ResearchNeedsAuthorization) as excinfo:
        _researcher(tmp_path).research(
            _question(f"{base}{LOGIN}"), ResearchBudget(max_sources=1, max_depth=0)
        )
    assert "login required" in str(excinfo.value)
    assert LOGIN in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 出口⑤：全部不可用 ⇒ 具名弃权，绝不退回空清单
# --------------------------------------------------------------------------- #


def test_all_backends_down_is_a_named_abstention_with_no_manifest(
    site: ThreadingHTTPServer, tmp_path: Path
) -> None:
    """★★ 红线：拿不到依据 ⇒ 抛具名异常；**一份快照都不冻**（尤其不冻空清单）。"""
    base = _base(site)
    with pytest.raises(R.ResearchUnavailable) as excinfo:
        _researcher(tmp_path).research(
            _question(f"{base}{FORBIDDEN}"), ResearchBudget(max_sources=1, max_depth=0)
        )
    assert "produced no evidence" in str(excinfo.value)
    assert not list(tmp_path.glob("*.md")) and not list(tmp_path.glob("*.txt"))


# --------------------------------------------------------------------------- #
# 熔断真的挡住了后续来源（不是一次性重试）
# --------------------------------------------------------------------------- #


def test_a_tripped_circuit_is_not_retried_for_later_sources(
    site: ThreadingHTTPServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 熔断的读数：同一 (后端, 主机) 在本次检索里**不再被碰第二次**。

    第一条来源 403 ⇒ 直连熔断；第二条来源即使本身是好的，也不再走直连。
    """
    base = _base(site)
    monkeypatch.setattr(R, "JINA_PREFIX", f"{base}/render/")
    question = ResearchQuestion(
        ref="hunt",
        question="找两份",
        seeds=(f"{base}{FORBIDDEN}", f"{base}{OK}"),
    )
    found = _researcher(tmp_path, channels=(R.BACKEND_DIRECT, R.BACKEND_JINA)).research(
        question, ResearchBudget(max_sources=4, max_depth=0)
    )

    assert _Site.hits.count(FORBIDDEN) == 1, "熔断后不该再试直连"
    assert _Site.hits.count(OK) == 0, "直连已熔断，第二条来源不该再走直连"
    materials = [snap for snap in found if snap.ref != "hunt"]
    assert materials and all(snap.backend == R.BACKEND_JINA for snap in materials)


# --------------------------------------------------------------------------- #
# 可复现：同一份契约跑两次 ⇒ 逐字节相同、第二次零请求
# --------------------------------------------------------------------------- #


def test_the_same_hunt_twice_is_byte_identical_and_hits_the_network_once(
    site: ThreadingHTTPServer, tmp_path: Path
) -> None:
    """★★ 红线①：第二次**直接重放**第一次的冻结件（0 请求，逐字节相同）。

    ★ 反证（同一个测试里做）：删掉检索台账 ⇒ 第二次真的重抓（请求数 > 0、内容变）。
      这说明"逐字节相同"不是碰巧，而是台账在起作用。
    """
    base = _base(site)
    question = _question(f"{base}/html-seed")
    store = SnapshotStore(root=tmp_path)

    def read_all(found: tuple[Snapshot, ...]) -> tuple[tuple[str, bytes, str, str, bool], ...]:
        return tuple(
            (snap.ref, store.read(snap), snap.captured_at, snap.backend, snap.degraded)
            for snap in found
        )

    first = _researcher(tmp_path).research(question, ResearchBudget(max_sources=4, max_depth=1))
    hits_after_first = len(_Site.hits)
    second = _researcher(tmp_path).research(question, ResearchBudget(max_sources=4, max_depth=1))

    assert read_all(first) == read_all(second), "同一份契约两次跑，材料逐字节不同"
    assert len(_Site.hits) == hits_after_first, f"第二次重放却打了网络：{_Site.hits[hits_after_first:]}"

    # ★ 反证：台账在，才有"零请求 + 逐字节相同"
    (tmp_path / R.RESEARCH_LEDGER_NAME).unlink()
    _Site.revision = "v2"
    third = _researcher(tmp_path).research(question, ResearchBudget(max_sources=4, max_depth=1))
    assert len(_Site.hits) > hits_after_first, "删掉台账之后应当重新抓取"
    assert read_all(third) != read_all(second), "重抓之后内容变了 ⇒ 说明台账才是可复现的来源"


# --------------------------------------------------------------------------- #
# 信任分层（T1–T4）
# --------------------------------------------------------------------------- #


def test_trust_tiers_put_official_apis_at_t1_and_random_blogs_at_t4() -> None:
    """★★ 两端由规则表自证：HF 的 API 响应落 T1，随便的博客落 T4。"""
    assert R.trust_tier("https://huggingface.co/api/models/Qwen/Qwen3") == R.TRUST_T1
    assert R.trust_tier("https://pypi.org/pypi/pyyaml/json") == R.TRUST_T1
    assert R.trust_tier("https://arxiv.org/abs/2401.00001") == R.TRUST_T2
    assert R.trust_tier("https://news.ycombinator.com/item?id=1") == R.TRUST_T3
    assert R.trust_tier("https://some-random-blog.example/post/1") == R.TRUST_T4
    # ★ 中间层可以判 T2/T3，但不能把"官方"降成模型一句话
    assert R.trust_tier("https://some-random-blog.example/post/1", middle=lambda _u: R.TRUST_T2) == R.TRUST_T2
    assert R.trust_tier("https://some-random-blog.example/post/1", middle=lambda _u: R.TRUST_T1) == R.TRUST_T4


def test_a_frozen_research_source_carries_its_tier_and_a_local_one_is_t4(
    site: ThreadingHTTPServer, tmp_path: Path
) -> None:
    """信任层落到 `Snapshot.source_kind`；本地假站的地址不在规则表里 ⇒ T4。"""
    base = _base(site)
    found = _researcher(tmp_path).research(
        _question(f"{base}{OK}"), ResearchBudget(max_sources=1, max_depth=0)
    )
    materials = [snap for snap in found if snap.ref != "hunt"]
    assert materials and materials[0].source_kind == R.TRUST_T4
    assert found[0].source_kind == "research"  # 清单自己不是网上的一条材料


def test_the_tier_is_visible_in_the_sources_report() -> None:
    """★★ 报告里能看出"这句话来自哪一层"：T1 与 T4 分节，后端/降级两列都在。"""
    base = {"captured_at": "T", "body_path": Path("/tmp/x.txt")}
    t1 = Snapshot(ref="hunt:1", source_kind=R.TRUST_T1, backend=R.BACKEND_DIRECT, degraded=False, **base)
    t4 = Snapshot(ref="hunt:2", source_kind=R.TRUST_T4, backend=R.BACKEND_JINA, degraded=True, **base)
    text = render_sources([t1, t4], [])

    assert "## T1" in text and "## T4" in text, text
    assert "`direct`" in text and "`jina`" in text, text
    assert "| 是 |" in text and "| 否 |" in text, "降级列没有如实落表"

    # ★ 反证：把 degraded 拿掉，报告就不该再出现「是」—— 可见性确实来自那个字段
    tampered = Snapshot(ref="hunt:2", source_kind=R.TRUST_T4, backend=R.BACKEND_JINA, degraded=False, **base)
    assert "| 是 |" not in render_sources([t1, tampered], [])


# --------------------------------------------------------------------------- #
# 直连优先：同一组问题，先问 API 比先搜再抓少一次往返
# --------------------------------------------------------------------------- #


class _ApiOpener:
    """把 pypi 的官方 API 地址直接映射到本地内容（不碰网络）。

    ★ 用它才能真实走到 `api_backend()` 那条分支：后端名 = `api:pypi`。
    """

    class _Response:
        def __init__(self, body: bytes) -> None:
            self._body = body
            self.status = 200
            self.headers = {"Content-Type": "application/json"}

        def read(self, amount: int = -1) -> bytes:
            return self._body if amount < 0 else self._body[:amount]

        def __enter__(self) -> _ApiOpener._Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def __init__(self) -> None:
        self.urls: list[str] = []

    def open(self, request: Any, timeout: float = 0) -> _ApiOpener._Response:
        self.urls.append(getattr(request, "full_url", str(request)))
        return self._Response(b'{"info": {"version": "2.0.0"}}')


def test_direct_api_first_costs_one_request_while_search_first_costs_two(
    site: ThreadingHTTPServer, tmp_path: Path
) -> None:
    """★★ 判据④：要一个已知结构化字段时，直连是 1 次请求；从检索页再抓是 2 次。

    ★ "先搜"这一臂用假站的检索页模拟（本机没有搜索后端 —— agent-reach 未安装），
      量的是**路径成本**，不是某个搜索引擎的延迟。
    """
    base = _base(site)

    api_opener = _ApiOpener()
    api_found = _researcher(tmp_path / "api", opener=api_opener).research(
        ResearchQuestion(
            ref="api", question="pyyaml 最新版本", seeds=("https://pypi.org/pypi/pyyaml/json",)
        ),
        ResearchBudget(max_sources=1, max_depth=0),
    )
    api_material = next(snap for snap in api_found if snap.ref != "api")

    hits_before = len(_Site.hits)
    search_found = _researcher(tmp_path / "search").research(
        _question(f"{base}{SEARCH}"), ResearchBudget(max_sources=2, max_depth=1)
    )
    search_material = [snap for snap in search_found if snap.ref != "hunt"][-1]

    assert len(api_opener.urls) == 1, f"直连 API 不该多打：{api_opener.urls}"
    assert api_material.backend == "api:pypi"
    assert b"2.0.0" in SnapshotStore(root=tmp_path / "api").read(api_material)

    search_requests = len(_Site.hits) - hits_before
    assert search_requests == 2, f"先搜再抓应当是 2 次请求，实得 {search_requests}"
    assert b"1.0.0" in SnapshotStore(root=tmp_path / "search").read(search_material)
    assert search_requests > len(api_opener.urls), "直连优先没有省下任何请求"


# --------------------------------------------------------------------------- #
# HTML 种子的链接扩展：转换后的文本里没有 href 了
# --------------------------------------------------------------------------- #


def test_links_are_mined_from_the_raw_markup_not_the_converted_text(
    site: ThreadingHTTPServer, tmp_path: Path
) -> None:
    """★★ HTML 种子也要能往外扩 —— 链接从**原始 markup** 里挖。

    实测坑：`html_to_text()` 把 `<a href>` 剥成锚文本，转完再 `extract_links`
    永远是 0 条 ⇒ "种子是起点"对最常见的网页来源失效（测试曾用 text/markdown 掩盖）。
    """
    base = _base(site)
    found = _researcher(tmp_path).research(
        _question(f"{base}/html-seed"), ResearchBudget(max_sources=4, max_depth=1)
    )

    visited = set(_Site.hits)
    assert "/ok" in visited, f"HTML 种子里的链接没被扩展：{visited}"
    assert EMPTY_FIXED in visited
    assert "/logo.png" not in visited, "媒体文件不该被当来源"
    assert len(found) >= 3


# --------------------------------------------------------------------------- #
# runner 侧：无依据 ⇒ 具名拒跑（不是让块照写）
# --------------------------------------------------------------------------- #


def _runner_with(researcher: Any) -> Any:
    from tests.test_research_contract import _runner

    return _runner(
        "  - {ref: hunt, kind: research, question: \"找东西\", seeds: [\"https://a.example\"]}",
        researcher,
    )


def test_the_runner_refuses_the_run_when_research_produced_no_evidence() -> None:
    """★★ 红线落在主流程：research 抛 `ResearchUnavailable` ⇒ `RunRefused`，

    code 是 `research_no_evidence`（具名，不是"退化成瞎编"）。
    """
    from bobanana6.assemble_impl.runner import RunRefused

    class _NoEvidence:
        def research(self, question: Any, budget: Any) -> tuple[Snapshot, ...]:
            raise R.ResearchUnavailable("no backend could read 'https://a.example'")

    runner = _runner_with(_NoEvidence())
    with pytest.raises(RunRefused) as excinfo:
        runner._fetch_sources(runner.document.contract)
    assert "research_no_evidence" in excinfo.value.codes  # type: ignore[attr-defined]


def test_the_runner_refuses_the_run_when_research_needs_authorization() -> None:
    """登录态 ⇒ `research_needs_authorization`（交回有权限的一方，不是普通失败）。"""
    from bobanana6.assemble_impl.runner import RunRefused

    class _NeedsLogin:
        def research(self, question: Any, budget: Any) -> tuple[Snapshot, ...]:
            raise R.ResearchNeedsAuthorization("login required for: https://a.example")

    runner = _runner_with(_NeedsLogin())
    with pytest.raises(RunRefused) as excinfo:
        runner._fetch_sources(runner.document.contract)
    assert "research_needs_authorization" in excinfo.value.codes  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# 边界：只回清单不算依据；部分失败进报告；渠道覆盖；读取上限；病态 HTML；SSRF
# --------------------------------------------------------------------------- #


def test_a_researcher_that_only_returns_a_manifest_is_refused() -> None:
    """★★ 审计堵口：清单的 ref 就是声明 ref，**不能自己证明自己**找到了东西。

    只回清单（0 份材料）⇒ `research_no_evidence`，任何空壳实现都不能靠"回个清单"过关。
    """
    from bobanana6.assemble_impl.runner import RunRefused
    from tests.test_research_contract import _FakeResearcher

    runner = _runner_with(_FakeResearcher(count=0))
    with pytest.raises(RunRefused) as excinfo:
        runner._fetch_sources(runner.document.contract)
    assert "research_no_evidence" in excinfo.value.codes  # type: ignore[attr-defined]


def test_a_partial_seed_failure_lands_in_the_report(
    site: ThreadingHTTPServer, tmp_path: Path
) -> None:
    """★ "4 个种子死 3 个"不能只藏在清单正文里：失败地址要进 `fetch_failures`（→ SOURCES.md）。"""
    base = _base(site)
    researcher = _researcher(tmp_path)
    question = ResearchQuestion(
        ref="hunt",
        question="找两份",
        seeds=("http://127.0.0.1:1/dead", f"{base}{OK}"),
    )
    found = researcher.research(question, ResearchBudget(max_sources=4, max_depth=0))
    assert [snap for snap in found if snap.ref != "hunt"]

    from tests.test_research_contract import _runner

    spec = (
        "  - {ref: hunt, kind: research, question: \"找两份\", "
        "seeds: [\"http://127.0.0.1:1/dead\", \"" + base + OK + "\"]}"
    )
    runner = _runner(spec, researcher)
    runner.fetch_failures = {}
    snapshots = runner._fetch_sources(runner.document.contract)
    assert snapshots
    assert any(key.startswith("hunt:http://127.0.0.1:1") for key in runner.fetch_failures), runner.fetch_failures
    assert researcher.last_errors, "研究者自己也要留下失败读数"


def test_question_channels_override_the_instance_default(
    site: ThreadingHTTPServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """契约里 `限定走哪些后端` 要有读者：只给 jina ⇒ 直连一次都不打。"""
    base = _base(site)
    monkeypatch.setattr(R, "JINA_PREFIX", f"{base}/render/")
    question = ResearchQuestion(
        ref="hunt", question="找个东西", seeds=(f"{base}{OK}",), channels=(R.BACKEND_JINA,)
    )
    found = _researcher(tmp_path).research(question, ResearchBudget(max_sources=1, max_depth=0))

    assert OK not in _Site.hits, f"指定只走 jina，却打了直连：{_Site.hits}"
    materials = [snap for snap in found if snap.ref != "hunt"]
    assert materials and materials[0].backend == R.BACKEND_JINA and materials[0].degraded


def test_a_body_over_the_read_cap_is_treated_as_unavailable(
    site: ThreadingHTTPServer, tmp_path: Path
) -> None:
    """★ 单份材料读取有上限：超过 `max_body_bytes` 就不把整份文件塞进内存。"""
    base = _base(site)
    with pytest.raises(R.ResearchUnavailable):
        _researcher(tmp_path, max_body_bytes=100).research(
            _question(f"{base}/huge"), ResearchBudget(max_sources=1, max_depth=0)
        )


def test_a_json_api_response_is_frozen_as_json(site: ThreadingHTTPServer, tmp_path: Path) -> None:
    """★ 官方 API 常常没有扩展名；后缀必须按 Content-Type 定，否则下游把 JSON 当纯文本读。"""
    opener = _ApiOpener()
    found = _researcher(tmp_path, opener=opener).research(
        ResearchQuestion(ref="api", question="pyyaml 最新版本", seeds=("https://pypi.org/pypi/pyyaml/json",)),
        ResearchBudget(max_sources=1, max_depth=0),
    )
    material = next(snap for snap in found if snap.ref != "api")
    assert material.body_path.suffix == ".json", material.body_path
    assert material.backend == "api:pypi"


def test_pathological_html_cannot_stall_the_hunt() -> None:
    """★ 实测坑：未闭合 `<script` 让"去整块"正则退化成 O(n²)（256KB 32s）。

    转换层必须有常数上限；否则 socket 超时管不到 CPU，整次检索卡死在这里。
    """
    payload = (b"<script " * 20_000) + b"body text"
    started = time.perf_counter()
    text = R.html_to_text(payload)
    elapsed = time.perf_counter() - started

    assert elapsed < 2.0, f"病态 HTML 转换用了 {elapsed:.2f}s —— 上限没生效"
    assert "body text" in text


def test_the_ssrf_guard_rejects_public_names_that_point_inward() -> None:
    """★ 只查字面量 IP 不够：localtest.me / nip.io 这类公共域名静态就指向内网。"""
    for target in (
        "http://localtest.me:8080/admin",
        "http://127.0.0.1.nip.io/x",
        "http://169.254.169.254.sslip.io/latest/meta-data",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:1/dead",
    ):
        assert R._is_public_locator(target) is False, target
    assert R._is_public_locator("https://huggingface.co/api/models/x") is True
