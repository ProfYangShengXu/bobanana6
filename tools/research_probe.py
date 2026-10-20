#!/usr/bin/env python
"""ResearchPort 落地探针 —— 一条命令复现全部读数（零模型、零真网络）。

    PYTHONPATH=src:. python tools/research_probe.py
    PYTHONPATH=src:. python tools/research_probe.py --json

五组读数，对应任务书 §6 的四条反证 + 信任分层/元数据接线：

```
[1] 可复现性（红线）  同一份契约跑两次 ⇒ 材料逐字节相同、第二次 0 请求
[2] 检索真的接上了    种子之外真的抓到了材料；关掉 ResearchPort ⇒ 稳定失败（具名拒）
[3] 降级可见          首选后端 403 ⇒ backend=jina / degraded=True，SOURCES.md 里看得见
[4] 直连优先在省      API 臂 1 次请求且数字新；先搜再抓 2 次请求且数字旧
[5] 信任分层           T1/T4 两端由规则表自证；报告按层分节，前端不可自升 T1
```

★ 每一组都带一个**反证**（把关键机制关掉/篡改，读数必须变红）。
  反证不成立时这一组算失败 —— 否则"全绿"可能只是断言写空了。

★ 全程只连 127.0.0.1 的假站；真搜索后端（agent-reach）本机未安装，探针如实报出来，
  不伪造一个后端。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from bobanana6.core.model import ResearchBudget, ResearchQuestion, Snapshot
from bobanana6.report.builders import render_sources
from bobanana6.sources import research as R
from bobanana6.sources.store import SnapshotStore, SourceLedger

#: 固定时刻：探针不读时钟（否则"跑两次逐字节相同"永远不成立）
CAPTURED_AT = "2026-10-20T12:00:00"


# --------------------------------------------------------------------------- #
# 假站
# --------------------------------------------------------------------------- #

FORBIDDEN = "/forbidden"
LOGIN = "/login"
EMPTY = "/empty"
EMPTY_FIXED = "/empty-fixed"
SEARCH = "/search"
STALE = "/stale"
API = "/api/item"
RENDER = "/render/"


class _Site(BaseHTTPRequestHandler):
    hits: ClassVar[list[str]] = []
    revision: ClassVar[str] = "v1"

    def do_GET(self) -> None:
        type(self).hits.append(self.path)
        host = f"http://127.0.0.1:{self.server.server_address[1]}"  # type: ignore[attr-defined]
        if self.path.startswith("/seed"):
            filler = "".join(f"<p>种子页正文第 {i} 段，用来越过本地转换门槛。</p>" for i in range(12))
            body = (
                f"<html><body><h1>seed</h1>"
                f'<a href="{host}/jd">大厂 agent 岗 招聘要求</a>'
                f'<a href="{host}/interview">面经 真题</a>'
                f'<a href="{host}/logo.png">图标</a>'
                f"{filler}</body></html>"
            ).encode()
            self._write(body, "text/html; charset=utf-8")
            return
        if self.path == "/jd":
            self._write(f"JD: agent 岗要求 Python / RAG / 评测。({type(self).revision})\n".encode(), "text/markdown")
            return
        if self.path == "/interview":
            self._write(f"面经: 手写 attention / RAG 召回。({type(self).revision})\n".encode(), "text/markdown")
            return
        if self.path.startswith(FORBIDDEN):
            self.send_response(403)
            self.end_headers()
            return
        if self.path.startswith(LOGIN):
            self.send_response(401)
            self.end_headers()
            return
        if self.path == EMPTY:
            self._write(b"", "text/markdown")
            return
        if self.path == EMPTY_FIXED:
            self._write(f"换措辞之后的正文 ({type(self).revision})".encode(), "text/markdown")
            return
        if self.path.startswith(RENDER):
            self._write(b"rendered via the fallback backend", "text/markdown")
            return
        if self.path == SEARCH:
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
        self._write(f"ok body ({type(self).revision})".encode(), "text/markdown")

    def _write(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return


def _agent_reach_reading() -> dict[str, Any]:
    """本机 Agent Reach 的真实读数（WSL 侧没有时，走宿主 exe —— 设计 §4 ④ 已验证的路子）。

    ★ 探针**不伪造**一个后端：没装就写没装；装了就把 doctor 的渠道数照抄。
    """
    reading: dict[str, Any] = {"path": shutil.which("agent-reach") or "", "version": "", "channels": "", "search": ""}
    base_cmd: list[str] = []
    if reading["path"]:
        base_cmd = ["agent-reach"]
    elif Path("/mnt/c/Windows/System32/cmd.exe").is_file():
        base_cmd = ["cmd.exe", "/c", "agent-reach"]
    if not base_cmd:
        return reading
    try:
        version = subprocess.run([*base_cmd, "--version"], capture_output=True, text=True, timeout=20, check=False)
        reading["version"] = (version.stdout or version.stderr).strip().splitlines()[-1] if version.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return reading
    try:
        doctor = subprocess.run([*base_cmd, "doctor"], capture_output=True, text=True, timeout=90, check=False)
        text = (doctor.stdout or "") + (doctor.stderr or "")
        match = re.search(r"(\d+)/(\d+)\s*个渠道可用", text)
        if match:
            reading["channels"] = f"{match.group(1)}/{match.group(2)}"
        for line in text.splitlines():
            if "全网语义搜索" in line:
                reading["search"] = line.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return reading


@dataclass
class _SiteHandle:
    server: ThreadingHTTPServer
    base: str

    @property
    def hits(self) -> list[str]:
        return _Site.hits


def _start_site() -> _SiteHandle:
    _Site.hits = []
    _Site.revision = "v1"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return _SiteHandle(server=server, base=f"http://127.0.0.1:{server.server_address[1]}")


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def _researcher(root: Path, *, channels: tuple[str, ...] = (R.BACKEND_DIRECT,), **kwargs: Any) -> Any:
    return R.LinkExpandingResearch(
        store=SnapshotStore(root=root),
        ledger=SourceLedger(root=root),
        captured_at=CAPTURED_AT,
        channels=channels,
        **kwargs,
    )


def _question(base: str, *, ref: str = "hunt", seed: str = "/seed") -> ResearchQuestion:
    return ResearchQuestion(ref=ref, question="大厂 agent 岗 招聘要求 和 面经", seeds=(base + seed,))


def _digests(root: Path, found: tuple[Snapshot, ...]) -> list[tuple[str, str]]:
    store = SnapshotStore(root=root)
    return [(snap.ref, hashlib.sha256(store.read(snap)).hexdigest()) for snap in found]


def _read_all(root: Path, found: tuple[Snapshot, ...]) -> tuple[tuple[Any, ...], ...]:
    store = SnapshotStore(root=root)
    return tuple(
        (snap.ref, store.read(snap), snap.captured_at, snap.source_kind, snap.backend, snap.degraded)
        for snap in found
    )


@dataclass
class Check:
    name: str
    passed: bool
    reading: dict[str, Any] = field(default_factory=dict)
    reverse: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# [1] 可复现性
# --------------------------------------------------------------------------- #


def check_reproducible(root: Path, site: _SiteHandle) -> Check:
    question = _question(site.base)
    budget = ResearchBudget(max_sources=8, max_depth=1)

    first = _researcher(root).research(question, budget)
    hits_after_first = len(site.hits)
    second = _researcher(root).research(question, budget)
    requests_second = len(site.hits) - hits_after_first

    identical = _read_all(root, first) == _read_all(root, second)
    zero = requests_second == 0

    # ★ 反证：删台账 + 改站点内容 ⇒ 必须真的重抓、且拿到不一样的材料
    (root / R.RESEARCH_LEDGER_NAME).unlink()
    _Site.revision = "v2"
    third = _researcher(root).research(question, budget)
    requests_third = len(site.hits) - hits_after_first - requests_second
    reverse_ok = requests_third > 0 and _read_all(root, third) != _read_all(root, second)

    return Check(
        name="可复现性（红线）",
        passed=bool(identical and zero and reverse_ok),
        reading={
            "materials": len(first),
            "run1_digests": [digest[:12] for _ref, digest in _digests(root, first)],
            "run2_digests": [digest[:12] for _ref, digest in _digests(root, second)],
            "byte_identical": identical,
            "requests_run1": hits_after_first,
            "requests_run2": requests_second,
        },
        reverse={
            "action": "delete research.index.json + change site content",
            "requests": requests_third,
            "materials_changed": _read_all(root, third) != _read_all(root, second),
        },
    )


# --------------------------------------------------------------------------- #
# [2] 检索真的接上了（含关掉它的反向）
# --------------------------------------------------------------------------- #


def check_retrieval(root: Path, site: _SiteHandle) -> Check:
    found = _researcher(root / "connected").research(_question(site.base), ResearchBudget(max_sources=8, max_depth=1))
    manifest = SnapshotStore(root=root / "connected").read(found[0]).decode("utf-8")
    materials = [snap for snap in found if snap.ref != "hunt"]
    citations_ok = all(snap.ref in manifest for snap in materials) and all(
        store_ok(root / "connected", snap) for snap in materials
    )
    visited = set(site.hits)
    expanded = "/jd" in visited and "/interview" in visited

    # ★ 反向 1：没有接线研究器 ⇒ 具名拒（不是静默跳过）
    runner = _probe_runner(researcher=None)
    disabled = _refusal_codes(lambda: runner._research(runner.document.contract.sources[0]))

    # ★ 反向 2：研究器拿不到依据 ⇒ runner 拒跑（research_no_evidence），不是让块照写
    runner = _probe_runner(researcher=_NoEvidenceResearch())
    no_evidence = _refusal_codes(lambda: runner._fetch_sources(runner.document.contract))

    return Check(
        name="检索真的接上了",
        passed=bool(materials and citations_ok and expanded and "research_unavailable" in disabled and "research_no_evidence" in no_evidence),
        reading={
            "materials": len(materials),
            "refs": [snap.ref for snap in found],
            "each_material_has_citation": citations_ok,
            "visited": sorted(visited),
        },
        reverse={
            "research_off_refusal": disabled,
            "no_evidence_refusal": no_evidence,
        },
    )


def store_ok(root: Path, snap: Snapshot) -> bool:
    return bool(SnapshotStore(root=root).read(snap).strip())


class _NoEvidenceResearch:
    def research(self, question: Any, budget: Any) -> tuple[Snapshot, ...]:
        raise R.ResearchUnavailable(f"no evidence for {question.ref!r}")


def _probe_runner(*, researcher: Any) -> Any:
    from bobanana6.assemble_impl.runner import Runner
    from bobanana6.contract import parse_contract

    spec = """
task_id: probe
goal: 探针
deliverables:
  - {name: out.md, schema: {text: str}}
sources:
  - {ref: hunt, kind: research, question: "找东西", seeds: ["https://a.example"]}
content_checks: {none: true, visibility: open}
"""
    document = parse_contract(spec, source="<probe>")
    return Runner(
        document=document,
        ontology_port=None,  # type: ignore[arg-type]
        decomposer=None,  # type: ignore[arg-type]
        constraint_source=None,  # type: ignore[arg-type]
        runtime=None,  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        store=None,  # type: ignore[arg-type]
        now=CAPTURED_AT,
        epoch=lambda: 0.0,
        researcher=researcher,
    )


def _refusal_codes(call: Any) -> list[str]:
    from bobanana6.assemble_impl.runner import RunRefused

    try:
        call()
    except RunRefused as refusal:
        return list(refusal.codes)
    return []


# --------------------------------------------------------------------------- #
# [3] 降级可见
# --------------------------------------------------------------------------- #


def check_degraded_visible(root: Path, site: _SiteHandle) -> Check:
    found = _researcher(root, channels=(R.BACKEND_DIRECT, R.BACKEND_JINA)).research(
        _question(site.base, ref="degraded", seed=FORBIDDEN),
        ResearchBudget(max_sources=1, max_depth=0),
    )
    material = next(snap for snap in found if snap.ref != "degraded")
    manifest = SnapshotStore(root=root).read(found[0]).decode("utf-8")
    sources_md = render_sources(found, [])

    visible = (
        material.degraded is True
        and material.backend == R.BACKEND_JINA
        and "降级=是" in manifest
        and "`jina`" in sources_md
        and "| 是 |" in sources_md
    )
    # ★ 反证：把 degraded 抹掉 ⇒ 报告必须**看不出**"是"
    tampered = Snapshot(
        ref=material.ref,
        body_path=material.body_path,
        captured_at=material.captured_at,
        source_kind=material.source_kind,
        backend=material.backend,
        degraded=False,
    )
    tampered_md = render_sources([found[0], tampered], [])
    reverse_ok = "| 是 |" not in tampered_md

    return Check(
        name="降级可见",
        passed=bool(visible and reverse_ok),
        reading={
            "backend": material.backend,
            "degraded": material.degraded,
            "manifest_says_degraded": "降级=是" in manifest,
            "sources_md_has_backend_and_degraded": "`jina`" in sources_md and "| 是 |" in sources_md,
        },
        reverse={"remove degraded flag": "| 是 |" in tampered_md},
    )


# --------------------------------------------------------------------------- #
# [4] 直连优先在省
# --------------------------------------------------------------------------- #


class _ApiOpener:
    """把 pypi 官方 API 地址映射到本地内容：走 `api_backend()` 分支，1 次请求。"""

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


def check_direct_first(root: Path, site: _SiteHandle) -> Check:
    opener = _ApiOpener()
    started = time.perf_counter()
    api_found = _researcher(root / "api", opener=opener).research(
        ResearchQuestion(ref="api", question="pyyaml 最新版本", seeds=("https://pypi.org/pypi/pyyaml/json",)),
        ResearchBudget(max_sources=1, max_depth=0),
    )
    api_ms = (time.perf_counter() - started) * 1000
    api_material = next(snap for snap in api_found if snap.ref != "api")
    api_body = SnapshotStore(root=root / "api").read(api_material)

    hits_before = len(site.hits)
    started = time.perf_counter()
    search_found = _researcher(root / "search").research(
        _question(site.base, ref="search", seed=SEARCH),
        ResearchBudget(max_sources=2, max_depth=1),
    )
    search_ms = (time.perf_counter() - started) * 1000
    search_material = [snap for snap in search_found if snap.ref != "search"][-1]
    search_body = SnapshotStore(root=root / "search").read(search_material)
    search_requests = len(site.hits) - hits_before

    return Check(
        name="直连优先在省",
        passed=bool(
            len(opener.urls) == 1
            and api_material.backend == "api:pypi"
            and b"2.0.0" in api_body
            and search_requests == 2
            and b"1.0.0" in search_body
            and len(opener.urls) < search_requests
        ),
        reading={
            "api": {
                "requests": len(opener.urls),
                "backend": api_material.backend,
                "value": "2.0.0",
                "latency_ms": round(api_ms, 1),
            },
            "search_then_fetch": {
                "requests": search_requests,
                "backend": search_material.backend,
                "value": "1.0.0",
                "latency_ms": round(search_ms, 1),
            },
            "saves_requests": search_requests - len(opener.urls),
        },
        reverse={
            "force the api question through the search page": "2 requests vs 1",
            "numbers_agree": "api=2.0.0 (fresh) / search=1.0.0 (stale)",
        },
    )


# --------------------------------------------------------------------------- #
# [5] 信任分层 + 报告里的层
# --------------------------------------------------------------------------- #


def check_trust_tiers() -> Check:
    cases = {
        "https://huggingface.co/api/models/Qwen/Qwen3": R.TRUST_T1,
        "https://pypi.org/pypi/pyyaml/json": R.TRUST_T1,
        "https://arxiv.org/abs/2401.00001": R.TRUST_T2,
        "https://news.ycombinator.com/item?id=1": R.TRUST_T3,
        "https://some-random-blog.example/post/1": R.TRUST_T4,
    }
    got = {url: R.trust_tier(url) for url in cases}
    table_ok = got == cases

    base = {"captured_at": CAPTURED_AT, "body_path": Path("/tmp/probe.txt")}
    t1 = Snapshot(ref="hunt:1", source_kind=R.TRUST_T1, backend=R.BACKEND_DIRECT, degraded=False, **base)
    t4 = Snapshot(ref="hunt:2", source_kind=R.TRUST_T4, backend=R.BACKEND_JINA, degraded=True, **base)
    sources_md = render_sources([t1, t4], [])
    report_ok = "## T1" in sources_md and "## T4" in sources_md and "信任层：T1" in sources_md

    # ★ 反证：中间层不能把官方"升"成 T1（T1 只能由规则表自证）
    promoted = R.trust_tier("https://some-random-blog.example/post/1", middle=lambda _u: R.TRUST_T1)
    reverse_ok = promoted == R.TRUST_T4

    return Check(
        name="信任分层",
        passed=bool(table_ok and report_ok and reverse_ok),
        reading={"tiers": got, "report_has_T1_and_T4": report_ok},
        reverse={"middle_hook_tries_to_promote_to_T1": promoted, "stayed_T4": reverse_ok},
    )


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def run_probe() -> list[Check]:
    import tempfile

    site = _start_site()
    original_prefix = R.JINA_PREFIX
    # ★ 备选后端指到同一台假站（不碰真网）；这也让"内网不送外部备选"的守卫放行
    R.JINA_PREFIX = site.base + RENDER
    checks: list[Check] = []
    try:
        with tempfile.TemporaryDirectory(prefix="research-probe-") as workdir:
            root = Path(workdir)
            checks.append(_run("1/5", check_reproducible, root / "repro", site))
            checks.append(_run("2/5", check_retrieval, root / "retrieval", site))
            checks.append(_run("3/5", check_degraded_visible, root / "degraded", site))
            checks.append(_run("4/5", check_direct_first, root / "direct", site))
            checks.append(_run("5/5", check_trust_tiers))
    finally:
        R.JINA_PREFIX = original_prefix
        site.server.shutdown()
        site.server.server_close()
    return checks


def _run(label: str, function: Any, *args: Any) -> Check:
    try:
        check = function(*args)
    except Exception as exc:  # noqa: BLE001 — 探针自己也要有失败读数，不是崩掉
        check = Check(name=getattr(function, "__name__", label), passed=False, reading={"error": f"{type(exc).__name__}: {exc}"})
    print(f"[{label}] {check.name:16s} {'PASS' if check.passed else 'FAIL'}")
    for key, value in check.reading.items():
        print(f"        {key} = {value}")
    for key, value in check.reverse.items():
        print(f"        反证 · {key} = {value}")
    print()
    return check


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ResearchPort 落地探针（零模型、零真网络）")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args(argv)

    agent_reach = _agent_reach_reading()
    header = (
        "=== ResearchPort 探针（确定性 · 零模型 · 只连 127.0.0.1 假站）===\n\n"
        f"环境：agent-reach = {agent_reach['version'] or '<不可用>'}"
        f"；渠道 = {agent_reach['channels'] or '<未知>'}；"
        f"语义搜索 = {agent_reach['search'] or '<未配置>'}\n"
        "      （搜索后端不可用 ⇒ 只量直连/备选两条路；不伪造一个搜索后端）\n\n"
    )
    # ★ 读数只跑一次：把过程输出收走，--json 时只吐结构化结果
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        checks = run_probe()

    payload = {
        "agent_reach": agent_reach,
        "checks": [
            {"name": check.name, "passed": check.passed, "reading": check.reading, "reverse": check.reverse}
            for check in checks
        ],
        "passed": sum(1 for check in checks if check.passed),
        "total": len(checks),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(header, end="")
        print(buffer.getvalue(), end="")
        print(f"RESULT: {payload['passed']}/{payload['total']} PASS")
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if payload["passed"] == payload["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
