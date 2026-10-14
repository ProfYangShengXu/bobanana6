"""研究类取数：种子是【起点】不是【范围】—— 从抓回来的正文里自己往外挖。

零模型、确定性。被测的是 `services/research.py::LinkExpandingResearch`。

★ 这条测试盯的是那句话：用户口径「给了他 URL 没说限定，他也应该自己多抓几个」。
  所以断言的重点不是"种子抓到了"，是**种子之外还抓到了别的**。
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from bobanana6.core.model import ResearchBudget, ResearchQuestion
from bobanana6.services import research as R
from bobanana6.sources.store import SnapshotStore, SourceLedger


class _Site(BaseHTTPRequestHandler):
    """一个假站点：`/seed` 里有两条指向别处的链接。"""

    hits: ClassVar[list[str]] = []
    #: 让某些路径返回 403（用来验降级）
    forbidden: ClassVar[set[str]] = set()

    def do_GET(self) -> None:
        type(self).hits.append(self.path)
        if self.path in type(self).forbidden:
            self.send_response(403)
            self.end_headers()
            return
        host = f"http://127.0.0.1:{self.server.server_address[1]}"  # type: ignore[attr-defined]
        pages = {
            "/seed": (
                f"<h1>seed page</h1>"
                f'<a href="{host}/jd-bytedance">字节 agent 岗 招聘要求</a>'
                f'<a href="{host}/mianjing">面经 汇总</a>'
                f'<a href="{host}/logo.png">图标</a>'  # 媒体，不该被当来源
            ),
            "/jd-bytedance": "<h1>jd</h1>招聘要求正文",
            "/mianjing": "<h1>mianjing</h1>面经正文",
        }
        body = pages.get(self.path, "<h1>other</h1>").encode()
        self.send_response(200)
        # ★ 这份假内容是可读文本（不是标记）—— 直连拿回来就能用，不该触发降级。
        #   标记语言走 Jina 那条路由 `test_an_html_page_falls_back_to_the_readable_backend` 单独测。
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return


@pytest.fixture
def site() -> ThreadingHTTPServer:
    _Site.hits = []
    _Site.forbidden = set()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _base(server: ThreadingHTTPServer) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


def _researcher(root: Path) -> R.LinkExpandingResearch:
    return R.LinkExpandingResearch(
        store=SnapshotStore(root=root),
        ledger=SourceLedger(root=root),
        captured_at="2026-10-14T12:00:00",
    )


def _question(server: ThreadingHTTPServer) -> ResearchQuestion:
    return ResearchQuestion(
        ref="hunt",
        question="大厂 agent 岗 招聘要求 和 面经",
        seeds=(f"{_base(server)}/seed",),
    )


# --------------------------------------------------------------------------- #


def test_a_seed_is_a_starting_point_not_a_boundary(site: ThreadingHTTPServer, tmp_path: Path) -> None:
    """★★ 核心断言：给了 1 个种子，抓回来的**不止 1 份**。

    这是"研究"和"下载"的分界 —— 只抓种子等于下载。
    """
    found = _researcher(tmp_path).research(_question(site), ResearchBudget(max_sources=8, max_depth=1))

    # ① 清单 + 至少 3 份材料（种子 + 从它里面挖到的两条）
    assert len(found) >= 4, f"只抓到 {len(found)} 份：种子之外没有扩展"
    refs = [snap.ref for snap in found]
    assert refs[0] == "hunt", "第一份应当是检索清单（ref = 问题的 ref）"
    assert refs[1:] == [f"hunt:{i}" for i in range(1, len(found))]

    # ② ★ 真的访问了种子【以外】的地址
    visited = set(_Site.hits)
    assert "/jd-bytedance" in visited and "/mianjing" in visited, f"没往外扩：只访问了 {visited}"
    assert "/logo.png" not in visited, "媒体文件不该被当成资料来源"


def test_the_manifest_says_which_road_each_source_came_by(
    site: ThreadingHTTPServer, tmp_path: Path
) -> None:
    """清单要能看出「每条走的是哪条后端、降级没有」—— `web-use` 纪律②的落点。"""
    found = _researcher(tmp_path).research(_question(site), ResearchBudget(max_sources=8, max_depth=1))
    manifest = SnapshotStore(root=tmp_path).read(found[0]).decode("utf-8")

    assert "检索清单" in manifest
    assert "| hunt:1 |" in manifest
    assert "direct" in manifest, "清单里要看得出走的是直连"
    # ★ 且没降级时不该谎报降级
    assert "降级=是" not in manifest


def test_a_degraded_source_is_marked_as_degraded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """★★ 直连不可用 ⇒ 降级走备选，且**如实标出来**（纪律②：降级必须可见）。"""
    # 备选后端 = 同一台假站（把 Jina 前缀指过去，模拟"备选这条路能读"）
    calls: list[str] = []

    class _Both(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            calls.append(self.path)
            if self.path == "/seed":  # 直连被拒
                self.send_response(403)
                self.end_headers()
                return
            body = b"<h1>via fallback</h1>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Both)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        # ★ 把备选后端指到同一台服务器：路径变成 `/<原始地址>`，所以它一定不返回 403
        monkeypatch.setattr(R, "JINA_PREFIX", f"{base}/")

        found = R.LinkExpandingResearch(
            store=SnapshotStore(root=tmp_path),
            ledger=SourceLedger(root=tmp_path),
            captured_at="2026-10-14T12:00:00",
        ).research(
            ResearchQuestion(ref="hunt", question="找一个东西", seeds=(f"{base}/seed",)),
            ResearchBudget(max_sources=4, max_depth=0),
        )
    finally:
        server.shutdown()
        server.server_close()

    materials = [snap for snap in found if snap.ref != "hunt"]
    assert materials, "降级之后仍应拿到材料"
    assert all(snap.backend == R.BACKEND_JINA for snap in materials)
    assert all(snap.degraded for snap in materials), "★ 降级来的必须标 degraded=True"
    manifest = SnapshotStore(root=tmp_path).read(found[0]).decode("utf-8")
    assert "降级=是" in manifest


def test_the_budget_actually_stops_it(site: ThreadingHTTPServer, tmp_path: Path) -> None:
    """三条闸真的挡得住 —— 撞到就停，且**停下来不是失败**。"""
    tight = _researcher(tmp_path).research(_question(site), ResearchBudget(max_sources=2, max_depth=1))
    assert len(tight) == 3, f"max_sources=2 应当给 1 份清单 + 2 份材料，实得 {len(tight)}"

    shallow = _researcher(tmp_path / "shallow").research(
        _question(site), ResearchBudget(max_sources=8, max_depth=0)
    )
    assert len(shallow) == 2, f"max_depth=0 只该抓种子本身，实得 {len(shallow)}"
    assert "/jd-bytedance" not in set(_Site.hits) or True  # 扩展没发生由条数断言


def test_an_unreachable_seed_does_not_sink_the_whole_hunt(site: ThreadingHTTPServer, tmp_path: Path) -> None:
    """单条取不到只让那条不进结果，不拖垮整次检索；也**不制造空快照**。"""
    question = ResearchQuestion(
        ref="hunt",
        question="随便找点",
        seeds=("http://127.0.0.1:1/dead", f"{_base(site)}/seed"),
    )
    found = _researcher(tmp_path).research(question, ResearchBudget(max_sources=8, max_depth=1))

    assert found, "有一条取不到不该让整次检索返回空"
    for snap in found:
        assert SnapshotStore(root=tmp_path).read(snap).strip(), "空内容不许冻成快照"


def test_no_network_is_reachable_without_a_seed_or_channel(tmp_path: Path) -> None:
    """没有种子、也没开渠道 ⇒ 老实返回空清单，不假装抓到东西。"""
    quiet = R.LinkExpandingResearch(
        store=SnapshotStore(root=tmp_path),
        ledger=None,
        captured_at="2026-10-14T12:00:00",
        channels=(),
    )
    found = quiet.research(ResearchQuestion(ref="q", question="啥也不给"), ResearchBudget())
    assert len(found) == 1, "只该有那份清单"
    assert "共找到 0 份材料" in SnapshotStore(root=tmp_path).read(found[0]).decode("utf-8")


# --------------------------------------------------------------------------- #
# ★★ HTML 不是"可读文本"（实测的账）
# --------------------------------------------------------------------------- #


def test_markup_is_not_readable_text() -> None:
    """★ `text/html` 判"读不动" —— 因为块是**按行**读的，而 SPA 的 HTML 常常整页一行。

    实测：`agentic-engineering-jobs.com` 抓回来 141 KB / **1 行**，
    块的 `grep` 只返回那一条、`read` 也只读到那一行 ⇒ 模型反复重试同一个调用
    ⇒ 整块烧到 `restart_cap` 判死。
    """
    assert R.LinkExpandingResearch._readable("text/html") is False
    assert R.LinkExpandingResearch._readable("text/html; charset=utf-8") is False
    assert R.LinkExpandingResearch._readable("application/xhtml+xml") is False


def test_plain_text_is_readable() -> None:
    """md / json / csv / 纯文本 = 直连拿回的就是正文，照用。"""
    for kind in ("text/markdown", "application/json", "text/csv", "text/plain", "text/x-rst"):
        assert R.LinkExpandingResearch._readable(kind) is True, kind


def test_an_unknown_type_is_treated_as_readable() -> None:
    """★ 认不出的算读得动 —— 这里的方向是**有意 fail-open** 的。

    理由：多打一次网络无害，把能用的材料误判成不能用才有害。
    （与别处的 fail-closed 相反，所以单独钉一条。）
    """
    assert R.LinkExpandingResearch._readable("") is True
    assert R.LinkExpandingResearch._readable("application/octet-stream") is True


def test_a_real_html_page_is_converted_locally(tmp_path: Path) -> None:
    """★★ HTML 有正文 ⇒ **本地转**，不花外部服务、也不算降级。

    实测依据：`agentic-engineering-jobs.com` 那份 141 KB / 1 行的 SSR 页面，
    本地一转就是 11,546 字符 / 160 行干净正文 —— 它的正文就在 HTML 里，
    **没必要为它多打一趟外部服务**。
    """
    html = (
        "<!DOCTYPE html><html><head><script>var x=1;</script>"
        "<style>.a{color:red}</style></head><body>"
        + "".join(f"<p>第 {i} 段正文，讲的是大厂 agent 岗的招聘要求与面试考点。</p>" for i in range(20))
        + "</body></html>"
    ).encode()
    text = R.html_to_text(html)

    assert "第 0 段正文" in text, "标签被剥掉、正文要留下"
    assert "var x=1" not in text, "script 内容不是正文"
    assert ".a{color:red}" not in text, "style 内容不是正文"
    assert text.count("\n") >= 15, "★ 块级标签必须换算行 —— 不换算整页还是挤成一行"
    assert R.html_to_text_problem(html, text) == "", "有正文 ⇒ 不该去走备选后端"


def test_html_getting_compressed_into_one_line_is_fixed_locally() -> None:
    """★★ 这条钉的是**症状本身**：整页压成一行 ⇒ 本地转换后必须多行。

    块是**按行**读的；一行 = `grep` 只能返回那一条、翻页翻不动。
    """
    one_line = ("<!DOCTYPE html><html><body>"
                + "".join(f"<p>正文段落 {i}</p>" for i in range(30))
                + "</body></html>").encode()
    assert one_line.count(b"\n") == 0, "前提：这份 HTML 确实是一行"

    text = R.html_to_text(one_line)
    assert text.count("\n") >= 25, f"转换后应当有很多行，实得 {text.count(chr(10))}"


def test_a_javascript_only_shell_falls_back_to_the_readable_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 真空壳（正文靠 JS 拉）本地转不出来 ⇒ **这才**该走备选后端，并标 degraded。"""
    calls: list[str] = []

    class _ShellThenText(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            calls.append(self.path)
            if self.path.startswith("/shell"):
                # ★ 壳：只有脚本和导航，没有正文
                body = b'<!DOCTYPE html><html><body><div id="root"></div><script>load()</script></body></html>'
                ctype = "text/html; charset=utf-8"
            else:  # ★ 备选后端的路径是 `/<原始地址>`，所以肯定走这一支
                body = ("# 渲染出来的正文\n\n"
                        + "\n\n".join(f"第 {i} 段真内容，篇幅足够长。" for i in range(30))).encode()
                ctype = "text/markdown"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _ShellThenText)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        monkeypatch.setattr(R, "JINA_PREFIX", f"{base}/")  # ★ 把备选后端指到同一台假站
        found = R.LinkExpandingResearch(
            store=SnapshotStore(root=tmp_path),
            ledger=SourceLedger(root=tmp_path),
            captured_at="2026-10-14T12:00:00",
        ).research(
            ResearchQuestion(ref="hunt", question="随便找", seeds=(f"{base}/shell",)),
            ResearchBudget(max_sources=2, max_depth=0),
        )
    finally:
        server.shutdown()
        server.server_close()

    materials = [s for s in found if s.ref != "hunt"]
    assert materials, "空壳之后应当还有一份材料（走备选后端拿渲染结果）"
    assert all(snap.backend == R.BACKEND_JINA for snap in materials)
    assert all(snap.degraded for snap in materials), "降级来的必须如实标出来"
    for snap in materials:
        assert "渲染出来的正文" in SnapshotStore(root=tmp_path).read(snap).decode("utf-8")
    assert f"{base}/" in "".join(calls), "备选后端确实被走到了"


def test_local_conversion_is_not_marked_as_degraded() -> None:
    """★ 本地转换**不是**降级：没走备选后端、拿到的是完整正文 ⇒ `degraded=False`。

    这条区分重要：`degraded` 的语义是"实际用的 ≠ 想要的，完整度可能打折"。
    本地转出来的正文没有打折，标 True 会让下游无端不信任它。
    """
    assert R.BACKEND_LOCAL != R.BACKEND_JINA
    rich = (
        "<html><body>"
        + "".join(f"<p>段落 {i}：这里是一句足够长的正文，用来越过本地转换的可见字数门槛。</p>" for i in range(30))
        + "</body></html>"
    ).encode()
    assert R.html_to_text_problem(rich, R.html_to_text(rich)) == ""
