"""快照冻结：同一个来源不许重复抓 —— 研究类取数设计（§6①）的那条"红线"。

纯壳的账，不含模型：这里只跑 `HttpFetcher`，不跑 `run`。

被测的东西（`sources/store.py` 的文档把它当特性写着）：

    if not path.exists():   # same bytes -> one file, however often it is fetched
        path.write_bytes(body)

    ⇒ 同样的【字节】只存一份                              ✓ 真的
    ⇒ 但它【不阻止重复抓取】—— fetch 照样再打一次网络      ✗

★ 这两件事差了整整一层，而且差在最要命的地方：

    研究类任务的来源【会变】（网页、价格、release notes）。
    变了之后内容寻址【不会拒绝】—— 它愉快地存下【第二个】快照。
    于是同一份契约跑两次，拿到两份不同的材料 ⇒ 产物不同
    ⇒ 可复现性没了。

    设计 §6① 要的是「**来源**不重复抓」，不是「**字节**不重复存」。

★★ 更麻烦的是它【测不出来】：内容寻址让"抓了两次"这个事实
   在磁盘上**看不见** —— 字节相同时两份快照长得一模一样（同一个 digest 名）。
    所以下面第三条用【假服务器自己的计数器】当权威读数，不看磁盘。
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from bobanana6.core.model import SourceRef
from bobanana6.sources.fetchers import HttpFetcher
from bobanana6.sources.store import SnapshotStore, SourceLedger

CAPTURED_AT = "2026-10-14T12:00:00"


class _ChangingSource(BaseHTTPRequestHandler):
    """每次 GET 返回【不一样】的正文，并记下被访问过几次。

    ★ 计数器就是权威读数：磁盘上的快照数会被内容寻址掩盖，这个不会。
    """

    hits: ClassVar[int] = 0
    #: 内容是否逐次变化 —— 关掉它就能模拟"两次抓到同样的字节"
    mutating: ClassVar[bool] = True

    def do_GET(self) -> None:
        type(self).hits += 1
        seq = type(self).hits if type(self).mutating else 1
        body = f"value,as_of\n{seq},2026-10-14\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/csv")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # 测试里不要刷屏
        return


@pytest.fixture
def source() -> ThreadingHTTPServer:
    _ChangingSource.hits = 0
    _ChangingSource.mutating = True
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ChangingSource)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _ref(server: ThreadingHTTPServer) -> SourceRef:
    host, port = server.server_address[:2]
    return SourceRef(ref="panel", locator=f"http://{host}:{port}/panel.csv")


def _fetch(
    server: ThreadingHTTPServer, root: Path, *, with_ledger: bool = True
) -> tuple[bytes, object]:
    fetcher = HttpFetcher(
        store=SnapshotStore(root=root),
        captured_at=CAPTURED_AT,
        ledger=SourceLedger(root=root) if with_ledger else None,
    )
    # 每次都【新建 fetcher】—— 这正是"两次跑"的形状：进程重来，台账留在磁盘上
    snapshot = fetcher.fetch(_ref(server))
    return SnapshotStore(root=root).read(snapshot), snapshot


# --------------------------------------------------------------------------- #


def test_content_addressing_alone_cannot_give_freezing(
    source: ThreadingHTTPServer, tmp_path: Path
) -> None:
    """★ 对照臂：**只**有内容寻址时（不接台账），"冻住了"是个假象。

    ★★ 这条是这条账的证据核心：
       字节相同时，抓两次磁盘上也只有【一份】快照 ⇒ 看磁盘以为"冻住了"；
       实际网络被打了两次，只是**没人记**。
       ⇒ 判定器看不见的失败，正是这个壳最危险的形态。
       ⇒ 所以台账必须存在（把"抓过几次"变成**看得见的事实**），而不是靠看目录。
    """
    _ChangingSource.mutating = False  # 两次抓到同样的字节

    _fetch(source, tmp_path, with_ledger=False)
    _fetch(source, tmp_path, with_ledger=False)

    files = [p for p in tmp_path.glob("*") if p.suffix != ".json"]
    assert len(files) == 1, "同样的字节应当只落一份（这条是 store 的真特性）"
    assert _ChangingSource.hits == 2, "★ 而网络确实被打了两次 —— 光看磁盘完全看不出来"

    # ★ 对照组：接上台账，同一个场景网络只被打一次
    _ChangingSource.hits = 0
    _fetch(source, tmp_path)
    _fetch(source, tmp_path)
    assert _ChangingSource.hits == 1, "接上台账之后，同一个来源不该再打第二次网络"


# ★★ 下面两条曾是 `xfail(strict=True)` 的**缺口活着的证据**（b50 报告）。
#    补上 `SourceLedger`（ref → 冻结件）之后它们转绿，按仓里 `test_attacks.py`
#    立下的口径**升级成正式断言** —— 不留一条恒真的 xfail。


def test_the_same_source_is_fetched_only_once(source: ThreadingHTTPServer, tmp_path: Path) -> None:
    """★ 核心断言：同一个来源，跑第二次【不该再打网络】，该复用磁盘上那份冻结材料。

    这是设计 §6① 的原话：「检索只发生一次，结果入库存档，第二次直接读快照」。
    """
    first_body, _ = _fetch(source, tmp_path)
    second_body, _ = _fetch(source, tmp_path)

    assert _ChangingSource.hits == 1, (
        f"同一个来源被抓了 {_ChangingSource.hits} 次；"
        "要的是「来源不重复抓」，磁盘上只有内容寻址（「字节不重复存」）"
    )
    assert second_body == first_body, "第二次跑拿到的材料跟第一次不一样 ⇒ 产物不可复现"


def test_a_changed_source_still_yields_the_first_material(
    source: ThreadingHTTPServer, tmp_path: Path
) -> None:
    """★ 最锋利的一条：来源变了之后，跑的仍然是**第一次那份材料**。

    来源（假网页）第二次被访问会给出不同的字节 —— 但因为有台账，
    它第二次根本不被访问，拿到的是第一次冻住的那份。
    ★★ 判据落在**内容**上（两次读到同一个 digest），不落在"抓了几次"上。
    """
    first_body, first = _fetch(source, tmp_path)
    second_body, second = _fetch(source, tmp_path)

    store = SnapshotStore(root=tmp_path)
    assert store.digest(first) == store.digest(second), (
        "同一个来源跑两次产生了两个不同的冻结材料；"
        f"第一次 {first_body!r} / 第二次 {second_body!r}"
    )
    assert second_body == first_body, "第二次跑拿到的材料跟第一次不一样 ⇒ 产物不可复现"
    assert _ChangingSource.hits == 1, "来源变了也不该再抓 —— 第二次跑该复用第一份"
    frozen = [p for p in tmp_path.glob("*") if p.suffix != ".json"]
    assert len(frozen) == 1, "磁盘上不该出现第二份冻结件"
