"""★ 全项目唯一的装配点（``AGENTS.md §1.4``）。

**本文件是全项目唯一 `new` 具体实现的地方。** 其他任何文件出现
`OpenAICompatibleClient()` / `HttpFetcher()` / `WorkerRuntime()` 这类构造都是违规。

装配点只回答「把谁接到谁」：

  * 契约从文件读（`contract/`），本体从文件读或由模型起草（`adapters/`），
    两者都由 `core/` 校验；
  * 取网/取文件由 `sources/` 负责，取不回来是显式失败；
  * 判定由 `core/kernel.py` 做，运行由 `agentloop/` 的 `WorkerLoop` 做，
    两者之间的缝隙由 `assemble_impl/` 填；
  * 报告由 `report/` 写。

**这里没有一条判定。** 计划合不合法、块收不收、整体拼不拼得上，全部问 `core/`。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .adapters import (
    DraftingOntology,
    EchoToolResultClient,
    FileOntology,
    LLMConfigError,
    LLMSettings,
    OpenAICompatibleClient,
    ScriptedClient,
    TokenRateLimiter,
)
from .agentloop.compress import CompressionPolicy, block_char_band
from .agentloop.concurrency import GateConfig, RuntimeGate
from .agentloop.events import EventLog
from .assemble_impl import (
    BlockRuntime,
    ContractConstraintSource,
    Runner,
    RunResult,
    llm_summarizer,
)
from .contract import parse_contract_file
from .core.capabilities import Capability, WorkerCapabilities, WorkerLimits
from .core.model import DEFAULT_LIMITS, Budget
from .core.ports import LLMClient, OntologyPort, SourcePort
from .decompose import BlockBudget, Decomposer, RoundBudget
from .ontology import load_ontology_file
from .services.research import LinkExpandingResearch
from .sources import LocatorFetcher, SnapshotFileReader, SnapshotStore, SourceLedger
from .sources.form import forms_for

__all__ = ["App", "build_app", "main"]

DEFAULT_SNAPSHOTS = ".bobanana6/snapshots"
DEFAULT_REPORTS = ".bobanana6/reports"


@dataclass(frozen=True)
class App:
    """The wired pipeline. ``run`` is the only thing the entry point calls."""

    runner: Runner

    def run(self) -> RunResult:
        return self.runner.run()


def _now_iso() -> str:
    return datetime.now(tz=UTC).astimezone().isoformat()


def build_client(args: argparse.Namespace, env: Mapping[str, str]) -> LLMClient:
    """The model side: an offline stand-in, a script, or a real endpoint."""
    if args.offline_script:
        return ScriptedClient.from_file(args.offline_script)
    if args.offline:
        return EchoToolResultClient()
    settings = LLMSettings.from_env(env)
    # ★ 限速/退避只挂给**真端点**：离线替身没有额度，也不该被拖慢。
    #   限额从 settings（= env 的 `BOBANANA6_TPM_LIMIT` / `_RPM_LIMIT`）来，不在这里写死。
    limiter = TokenRateLimiter(tpm_limit=settings.tpm_limit, rpm_limit=settings.rpm_limit)
    return OpenAICompatibleClient(settings, limiter=limiter)


def build_ontology_port(args: argparse.Namespace, client: LLMClient) -> OntologyPort:
    if args.ontology:
        # Read it once here so a bad file fails before any worker exists.
        load_ontology_file(args.ontology)
        return FileOntology(Path(args.ontology))
    # ★ 任务目录里的 `ontology.yaml` 就是它的本体 —— 摆在旁边却不被使用是个坑：
    #   用户以为自己把本体给了，实际却去走了"模型起草"，而离线模型起草不出合法本体，
    #   报的是 `Additional properties are not allowed ('columns', 'rows' ...)` ——
    #   一个跟"你没给本体"完全对不上的错。
    #   约定和发现顺序保持一致：显式 `--ontology` > 同目录同名文件 > 模型起草。
    sibling = Path(args.task).resolve().parent / "ontology.yaml"
    if sibling.is_file():
        load_ontology_file(sibling)
        return FileOntology(sibling)
    return DraftingOntology(client=client)


def build_app(args: argparse.Namespace, *, env: Mapping[str, str] | None = None) -> App:
    """Wire every piece. The only function in the project that constructs implementations."""
    source_env = os.environ if env is None else env
    document = parse_contract_file(args.task)
    contract = document.contract

    client = build_client(args, source_env)
    ontology_port = build_ontology_port(args, client)

    store = SnapshotStore(root=Path(args.snapshots))
    # ★ 来源台账与冻结仓同目录：冻结件按【内容】寻址（同样的字节只有一份），
    #   台账按【来源】寻址（同一个 ref 抓过了就不再抓）。两者并存，各答各的问题。
    #   ⇒ 网页来源第二次跑拿到的是**第一次那份材料**，产物才可复现。
    ledger = SourceLedger(root=Path(args.snapshots))
    fetcher: SourcePort = LocatorFetcher(store=store, captured_at=_now_iso(), ledger=ledger)
    # ★ A11: the content-level provenance check needs the frozen rows as data,
    #   and core never opens a path -- so the reader is built here, at the one
    #   assembly point, and injected into the runner.
    # ★ `#32`: 载体的形式由任务声明（sources[].encoding / .eol）；壳在派人之前按它规整。
    reader = SnapshotFileReader(store=store, forms=forms_for(contract.sources))

    budget = Budget(max_calls=args.max_calls, max_wall_s=args.max_wall_s, max_output=args.max_output)
    # ★★ 两级块预算 = 压缩阈值 × 两个系数（同一把尺子，见 `block_char_band`）：
    #   读块输入 ∈ [0.6, 0.8]×阈值；写块输入（全部 part 的总和）∈ [0.1, 0.3]×阈值。
    #   切分器据此把一个超预算的源拆成多个读块、把几份小源合成一个读块；
    #   读块的产出上限由写层上界 ÷ 读块数倒推。
    #   ★ 阈值塌成 0（`--max-output >= --context-length`）时**不传预算** ——
    #     "没有可用预算"是"不按大小切"，不是"把每份源拆碎"。
    compression = CompressionPolicy(
        context_length=max(args.context_length, budget.max_output + 1),
        max_tokens=budget.max_output,
    )
    band = block_char_band(compression)
    block_budget = (
        BlockBudget(
            max_chars=band.read_max,
            min_chars=band.read_min,
            write_min_chars=band.write_min,
            write_max_chars=band.write_max,
        )
        if band.read_max > 0
        else None
    )
    decomposer = Decomposer(
        strategy=args.strategy,
        budget=RoundBudget(document.limits.max_rounds),
        block_budget=block_budget,
    )
    event_log = EventLog()
    # ★ One limiter, shared by the block's model call and the summarizer callback.
    gate = RuntimeGate(GateConfig(max_concurrent=1, default_timeout=0.0))
    workspace = Path(args.workspace) if getattr(args, "workspace", "") else None
    # ★ `#43`: 继承守卫的观测目录（只落盘、不改判定）；不给 = 不观测。
    observe_dir = Path(args.observe_dir) if getattr(args, "observe_dir", "") else None
    content_checks = document.content_checks.invariants
    comparisons = getattr(document.content_checks, "comparisons", ())

    def _make_runtime(log: EventLog, own_gate: RuntimeGate) -> BlockRuntime:
        return BlockRuntime(
            client=client,
            store=store,
            contract=contract,
            # ★★ 契约声明的判据随块交下去（按 `applies_to` 归属到产出那份产物的块）——
            #   判定器拿什么尺子量，模型就得看到那把尺子；不然只能按常识写、被判据拒，
            #   而报告上看起来像"运行期出错"。
            content_checks=content_checks,
            comparisons=comparisons,
            workspace=workspace,
            observe_dir=observe_dir,
            budget=budget,
            event_log=log,
            compression=compression,
            # ★ The compression callback is a model call, and it is injected here --
            #   the loop never reaches a model on its own.
            summarize=llm_summarizer(client, gate=own_gate),
            gate=own_gate,
        )

    runtime = _make_runtime(event_log, gate)

    def runtime_factory() -> BlockRuntime:
        """One isolated runtime per block (only used when ``--parallel >= 2``).

        ★ ``traces`` / ``last_judge`` / ``last_cleanliness`` / ``gate`` 都是单实例状态，
          并发必须每块一份；``event_log`` 也是（``EventLog`` 是单写者，最后按 plan 顺序并回）。
          自己的限流器让块内调用串行；跨块并发由 ``--parallel`` 决定。
        """
        own_gate = RuntimeGate(GateConfig(max_concurrent=1, default_timeout=0.0))
        return _make_runtime(EventLog(), own_gate)

    parallel = max(1, int(getattr(args, "parallel", 1)))
    runner = Runner(
        document=document,
        ontology_port=ontology_port,
        decomposer=decomposer,
        constraint_source=ContractConstraintSource(document=document),
        runtime=runtime,
        # ★ 默认 1 ⇒ factory 为 None ⇒ `run()` 走**逐字不变**的串行路径。
        parallel=parallel,
        runtime_factory=runtime_factory if parallel > 1 else None,
        fetcher=fetcher,
        store=store,
        reader=reader,
        # ★★ 研究类取数：只有契约里出现 `kind: research` 的来源才会被用到。
        #   在这里一并注入（装配点是唯一构造点），`None` 会让那类来源**具名拒**。
        #   ★ `channels` 暂时用实作里的保守默认（直连 + Jina，本机实测通的那两条）；
        #     接 `agent-reach doctor` 的读数是下一步（跨侧调用，见设计文档 §3.4）。
        researcher=LinkExpandingResearch(
            store=store,
            ledger=ledger,
            captured_at=_now_iso(),
        ),
        now=_now_iso(),
        epoch=time.monotonic,
        limits=DEFAULT_LIMITS,
        budget=budget,
        event_log=event_log,
        workspace=workspace,
        report_dir=Path(args.out),
        # ★ §6.8 的能力匹配：两个档位，各自只声明它真正提供的能力。
        #   多给一分能力就是 capability_over —— 那是成本信号，会拒绝装配。
        worker_pool=(
            WorkerCapabilities(
                worker_id="worker:reader",
                provides=frozenset({Capability.READ, Capability.COMPUTE}),
                limits=WorkerLimits(max_output=budget.max_output, concurrency=args.concurrency),
            ),
            WorkerCapabilities(
                worker_id="worker:writer",
                provides=frozenset({Capability.COMPUTE, Capability.WRITE_DELIVERABLE}),
                limits=WorkerLimits(max_output=budget.max_output, concurrency=args.concurrency),
            ),
        ),
    )
    return App(runner=runner)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bobanana6", description="把任务切块分工砸完，并保证每块合格、整体拼得上。")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="跑一份任务契约")
    run.add_argument("task", help="task.yaml 的路径")
    run.add_argument("--ontology", default="", help="本体文件；不给则由模型起草")
    run.add_argument("--out", default=DEFAULT_REPORTS, help="四份报告写到哪")
    run.add_argument("--snapshots", default=DEFAULT_SNAPSHOTS, help="快照落到哪")
    run.add_argument(
        "--workspace",
        default="",
        help="代码类产物的落盘目录（`#33`）；不给 = 这个部署没有工作区，交代码类产物会被具名拒绝",
    )
    run.add_argument(
        "--observe-dir",
        default="",
        help="继承守卫拒收时把「池子 + 交上来的行」落盘到这里（`#43`，只观测、不改判定）",
    )
    run.add_argument("--strategy", default="source", choices=("source", "field", "module", "single"), help="切分策略")
    run.add_argument("--offline", action="store_true", help="不接网络：用确定性离线模型与本地源")
    run.add_argument("--offline-script", default="", help="离线脚本（JSON），用固定响应驱动")
    run.add_argument("--max-calls", type=int, default=64)
    run.add_argument("--max-wall-s", type=int, default=3600)
    run.add_argument("--max-output", type=int, default=8192)
    run.add_argument("--context-length", type=int, default=32768)
    # ★ 单块输入预算不再由命令行给：两级预算的四个系数声明在
    #   `agentloop/compress.py`（`READ_BUDGET_LOW/HIGH`、`WRITE_BUDGET_LOW/HIGH`），
    #   与压缩阈值共用一把尺子。原来的 `--block-headroom` 只影响读侧上界，
    #   留着会让"命令行的 0.6 / argparse 的 0.7"分叉，所以删掉。
    #: 充裕并发是这个项目的前提；声明得比需求小，装配就会以 capability_short 拒绝。
    run.add_argument("--concurrency", type=int, default=8)
    # ★★ 块级并发旋钮（与上一条**正交**）：`--concurrency` 是**能力申报**，`--parallel`
    #   是**执行计划**。默认 1 = 串行（生产端点扛不住并发）；>= 2 才真并发跑块。
    run.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="同时跑几个块（默认 1 = 串行）；这是执行旋钮，不改 --concurrency 的能力申报",
    )
    return parser


def _print_result(result: RunResult, stream: object | None = None) -> None:
    # ★ Resolved at call time: a default bound to sys.stdout at import time
    #   keeps writing to the pre-redirect stream and silently bypasses capture.
    write = (stream or sys.stdout).write
    write(f"task      : {result.task_id}\n")
    write(f"ontology  : {result.ontology.domain}@{result.ontology.version} frozen={result.ontology.is_frozen}\n")
    write(f"sources   : {', '.join(sorted(result.snapshots)) or '<none>'}\n")
    write(f"plan      : shape={result.plan.shape} round={result.plan.round} blocks={list(result.plan.subtask_ids)}\n")
    for block_id in result.plan.subtask_ids:
        verdict = result.verdicts.get(block_id)
        state = "passed" if verdict is not None and verdict.ok else "failed"
        failed = "" if verdict is None or not verdict.failed else f" failed={list(verdict.failed)}"
        write(f"  {block_id:6} {state}{failed}\n")
    write(f"converge  : {result.convergence.decision}\n")
    write(f"assembly  : passed={result.assembly.passed} codes={list(result.assembly.codes())}\n")
    if result.attribution is not None:
        write(f"blame     : {result.attribution.blame} ({result.attribution.confidence})\n")
        write(f"actions   : {', '.join(result.attribution_actions) or '<none>'}\n")
    write(f"exit      : {result.exit_reason}\n")
    if result.reports is not None:
        write("reports   : " + ", ".join(sorted(result.reports.files())) + "\n")


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command != "run":  # pragma: no cover - argparse rejects anything else
        return 2
    try:
        app = build_app(args, env=env)
    except LLMConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"cannot start: {exc}", file=sys.stderr)
        return 2
    try:
        result = app.run()
    except Exception as exc:  # noqa: BLE001 - the refusal *is* the product here
        from .assemble_impl import RunRefused

        gate = exc.gate if isinstance(exc, RunRefused) else ""
        codes = exc.codes if isinstance(exc, RunRefused) else ()
        label = f"refused ({gate})" if gate else "failed"
        print(f"{label}: {exc}", file=sys.stderr)
        if codes:
            print(f"  codes: {', '.join(codes)}", file=sys.stderr)
        return 1
    _print_result(result)
    return 0 if result.ok else 1


# ★ 让 `python -m bobanana6.cli ...` 真的跑起来。
#   没有这个块时，`-m` 只 import 模块就退出、**exit 0** —— 一个"静默成功"：
#   调用方拿退出码当判据，就会把"什么都没干"读成"干成了"。
#   实测踩过一次：桥入口脚本用 `-m` 调起子进程，子进程秒退 0，
#   入口据此回了 `ok: true, exit_code: 0, detail: "passed"`。
if __name__ == "__main__":
    raise SystemExit(main())
