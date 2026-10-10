"""Ports: the boundary between the stable kernel and everything environment
specific.

Every protocol here is *defined* on the stable side and *implemented* outside
it. Nothing in this file imports an implementation, and no implementation
module is named.

Port inventory note: the spec's repo-structure comment lists six ports
(including a ``CandidateSource`` that is never given a signature) while its
port section defines the eight protocols found here. This unit implements the
eight with signatures; ``CandidateSource`` is deliberately not invented. See
the delivery report -- the two documents disagree and must be synced.

Vocabulary note: the environment guard forbids model/network vocabulary inside
``core/``. Two protocol members mandated by the spec itself -- ``LLMClient``
and the ``max_tokens`` / ``approx_tokens`` parameters that belong to it --
contain those substrings. The guard exempts exactly those three identifiers
and nothing else, so any *other* leak still fails.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .model import (
    AssemblyOutcome,
    Budget,
    BundleDelta,
    Candidate,
    ConstraintBundle,
    InputSurvey,
    ModelResponse,
    Ontology,
    Recon,
    Snapshot,
    SnapshotBody,
    SourceRef,
    SplitPlan,
    Subtask,
    TaskContract,
)

__all__ = [
    "AgentIO",
    "AgentRuntimePort",
    "ArbiterPort",
    "CheckTools",
    "ConstraintSource",
    "DecomposerPort",
    "OntologyPort",
    "SemanticsTools",
    "SnapshotReader",
    "SourcePort",
    "SourceTools",
    "TaskTools",
    "ToolSet",
    "ToolSpec",
    "TraceTools",
    "WorkspaceTools",
]


@runtime_checkable
class OntologyPort(Protocol):
    """Whoever supplies the task's shared semantics."""

    def obtain(self, contract: TaskContract, survey: InputSurvey) -> Ontology: ...


@runtime_checkable
class DecomposerPort(Protocol):
    """Whoever cuts the task into subtasks."""

    def split(self, contract: TaskContract, ontology: Ontology, recon: Recon) -> SplitPlan: ...

    def refine(
        self,
        contract: TaskContract,
        ontology: Ontology,
        plan: SplitPlan,
        outcome: AssemblyOutcome,
    ) -> SplitPlan: ...


@runtime_checkable
class ConstraintSource(Protocol):
    """Whoever compiles the constraint bundle for a plan."""

    def compile(self, contract: TaskContract, plan: SplitPlan, ontology: Ontology) -> ConstraintBundle: ...


@runtime_checkable
class AgentRuntimePort(Protocol):
    """Whoever drives one worker through one subtask."""

    def run(
        self,
        subtask: Subtask,
        bundle: ConstraintBundle,
        ontology: Ontology,
        tools: ToolSet,
        budget: Budget,
    ) -> Candidate: ...


@runtime_checkable
class LLMClient(Protocol):
    """One model call.

    Named by the spec even though the name trips a naive vocabulary scan; the
    kernel only ever sees this interface, never an implementation.
    """

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        **kw: Any,
    ) -> ModelResponse: ...


@runtime_checkable
class AgentIO(Protocol):
    """Host capabilities the worker loop needs, kept deliberately narrow."""

    def persist(self, messages: list[dict[str, Any]]) -> None: ...

    def compress(self, messages: list[dict[str, Any]], trigger: str, approx_tokens: int) -> list[dict[str, Any]]: ...

    def context_length(self) -> int: ...

    def note_activity(self, kind: str, detail: str = "") -> None: ...

    @property
    def max_tokens(self) -> int: ...


@runtime_checkable
class ArbiterPort(Protocol):
    """Whoever adds constraints, records conflicts, or rules on a definition."""

    def resolve(
        self,
        bundle: ConstraintBundle,
        ontology: Ontology,
        items: tuple[Any, ...],
    ) -> BundleDelta: ...


@runtime_checkable
class SourcePort(Protocol):
    """Whoever fetches raw material and freezes it into a snapshot."""

    def fetch(self, ref: SourceRef) -> Snapshot: ...


@runtime_checkable
class SnapshotReader(Protocol):
    """Whoever turns a frozen carrier into the rows core may judge (A11).

    Defined on the stable side and implemented outside: the content-level
    provenance check is judgment and belongs in ``core/``, but opening the
    carrier is not something ``core/`` may do (``AGENTS.md §1.1``). The reader
    hands the body over as data.
    """

    def body(self, snapshot: Snapshot) -> SnapshotBody: ...


# --------------------------------------------------------------------------- #
# Tool surface
# --------------------------------------------------------------------------- #


class SemanticsTools(Protocol):
    def lookup(self, term: str) -> Mapping[str, Any]: ...

    def map(self, field_name: str) -> str: ...

    def check(self, usage: Any) -> tuple[bool, str]: ...


class TaskTools(Protocol):
    def mysubtask(self) -> Mapping[str, Any]: ...

    def upstream(self, name: str) -> Mapping[str, Any]: ...

    def contract(self) -> TaskContract: ...


class CheckTools(Protocol):
    def local(self) -> Any: ...

    def provenance(self) -> Any: ...

    def assemble_dryrun(self) -> Any: ...


class SourceTools(Protocol):
    def snapshot(self, ref: str) -> Snapshot: ...

    #: The carrier's declared locator, for the payload's human-readable hint.
    #: Never used for permission (``snapshot``/``resolve`` own that).
    def locator(self, ref: str) -> str: ...


class WorkspaceTools(Protocol):
    """The block's own working area: files it may write, commands it may run.

    ★ Deliberately narrow and *bound*: the paths are workspace-relative and the
    process cwd stays inside the workspace, so a block cannot reach the host.
    The permission rule (which paths this block may write) is decided outside
    and passed in -- a tool never widens its own write region by asking.
    """

    def write(self, path: str, content: str) -> Mapping[str, Any]: ...

    def patch(self, path: str, old_string: str, new_string: str, replace_all: bool = False) -> Mapping[str, Any]: ...

    #: ★★ 定点修改（多条）：每条对**原文件**匹配、唯一、互不重叠，整体原子。
    #:   `edits` = `[{oldText, newText}, …]`；写区/账本与 `patch` 同一条路。
    def patch_edits(self, path: str, edits: Any) -> Mapping[str, Any]: ...

    def run(self, command: str, *, background: bool = False, timeout: float | None = None, workdir: str = "") -> Mapping[str, Any]: ...


class TraceTools(Protocol):
    """★ 上游块的【过程】—— 按需检索，不是全量灌进上下文。

    与交接件的分工：交接件是**结果**（推给下游），这里是**过程**（下游主动拉）。
    可见范围由装配方限死在本块的**上游闭包**上：`search` 只能看那些块，
    `read` 只能取那些块里的步骤。过程里常有半成品与错路，扩散出去只会污染。
    """

    def search(self, query: str) -> Mapping[str, Any]: ...

    def read(self, block_id: str, seq: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class ToolSpec:
    name: str
    params: tuple[str, ...]
    returns: str
    bound_to: str | None = None
    #: Arguments the tool accepts but does not require. They are advertised (a
    #: model cannot ask for what it is not told about) but not listed as
    #: required, so a default stays a default.
    optional: tuple[str, ...] = ()
    #: ★ 按域分组（`task` / `sources` / `files` / `exec` / `semantics` / `check`）。
    #: 名字已经带前缀，这个字段是给"要不要按组渐进披露"留的接口：工具多到
    #: 注意力被稀释时，可以只暴露某个组。现在只用于核对清单，不改变发送内容。
    group: str = ""
    #: ★★ **模型选工具的唯一依据**（`agentloop` 把它们原样发给端点）。
    #:
    #: 这一段曾经是空的 —— `tool_schemas()` 拿 `spec.returns` 当描述，
    #: 于是模型看到的全部信息是 `sources.snapshot → "Snapshot"` 八个字符。
    #: 实测代价：13f 上模型在 `snapshot`（8 字符）和 `read`（59 字符）之间，
    #: 每次都选 `snapshot`，于是 261 KB 的来源整份灌进上下文（298,587 字符，
    #: 约八万个词元），装不下也算不动，退回交"描述"当答案。
    #:
    #: 判据（三条缺一不可，顺序即重要性）：
    #:   ① **何时用** —— 决策信息。模型靠这句决定"调不调、调哪个"。
    #:   ② **输入什么** —— 每个参数填什么，尤其"哪个名字是从哪抄来的"。
    #:   ③ **何时不用** —— 边界。★ 这一条最常被省，也最贵：
    #:      工具选错 90% 是描述问题，而"选错"多半是【不知道它的边界】。
    #: 不写"怎么实现"（执行细节模型不需要），也不写"返回什么类型"
    #:   （`returns` 仍在，但它是给测试与清单看的，不再是给模型看的描述）。
    description: str = ""
    #: ★★ 参数级的 JSON schema 覆盖。默认每个参数都被渲染成 `{"type": "string"}`，
    #: 因为这套工具的参数域一直是字符串。**不是字符串**的参数必须在这里给出真形状，
    #: 否则模型会把它当字符串填：实测触发条件是 `files.patch` 的 `edits` —— 它是一个
    #: `[{oldText, newText}, …]` 数组，若广告成 string，模型只能塞一段 JSON 文本。
    #: 不参与相等/哈希（判定与清单看的是 `params`/`optional`/`description`，
    #: 模型看的是 `assemble_impl/produce.py::tool_schemas()` 渲染出来的那份）。
    # blame: fail-closed — 省略即所有参数按字符串广告（旧形状），方向保守；只有显式声明了才给数组等真形状
    param_schemas: Mapping[str, Mapping[str, Any]] = field(default_factory=dict, compare=False, hash=False)


#: ★ 全部工具的完整描述。写法见 `ToolSpec.description` 的注记。
#: 18 条合计约 3 KB —— 每轮常驻，所以每条都按"决策信息"写，
#: 不重复 `returns` 里的类型说明。
_TOOL_DESCRIPTIONS: dict[str, str] = {
    # -- task：我是谁 / 我的输入 / 我的契约 ---------------------------------- #
    "task.mysubtask": (
        "何时用：开工前确认【你自己这一块】的任务 —— 产出哪个文件、什么模式、"
        "有哪些字段。它与别的块不同，别拿 task.contract 里的全局清单当自己的任务。"
        "输入：无。"
        "何时不用：已经清楚自己这一块要干什么时，不必重复问。"
    ),
    "task.upstream": (
        "何时用：你要【某一件】声明输入的原文，按名字精确取。"
        "输入：name = 输入名（就是 task.mysubtask 里 inputs 列出的那几个之一）。"
        "★ 何时不用：壳已经把你的输入预取到 brief 的 delivered_inputs 里了 —— "
        "先看那里；只有它没给（超出预算退化成索引）时才用本工具。"
        "要一次取回全部输入用 task.upstream_all。"
        "取不到不等于没有 —— 它可能在上游失败，返回值里会点名。"
    ),
    "task.upstream_all": (
        "何时用：一次把你【声明的全部输入】取回来，省得逐个取。"
        "输入：无。"
        "★ 何时不用：你只要其中一件时用 task.upstream。"
        "★ 返回空 {} 只说明【你没有声明输入】—— 不等于这件事没有数据："
        "你的数据可能在 authorized_sources 里，用 sources.* 去读。"
    ),
    "task.contract": (
        "何时用：你要知道这份任务的全貌 —— 全局目标、全部产物的名字、"
        "以及你被授权读的来源清单。"
        "输入：无。"
        "何时不用：只要知道自己那一块时用 task.mysubtask；"
        "要读来源内容时用 sources.*（本工具只给名字，不给内容）。"
    ),
    # -- sources：读授权来源 ------------------------------------------------ #
    "sources.snapshot": (
        "何时用：你要一份来源的【整份内容】，而且它不大（几行到几百行）。"
        "输入：ref = 来源名，原样抄 brief 的 authorized_sources 里那一个。"
        "★ 何时不用：来源超过约两万字符时【不要】用它 —— 整份装不下，"
        "返回值会被截成样本 + 一段提示，而样本不是数据。"
        "那时改用 sources.read（按行分段取）或 sources.grep（按内容定位）。"
    ),
    "sources.read": (
        "何时用：★ 大来源的默认读法。你要看某一段、或按页浏览一份长文本。"
        "输入：ref = 来源名；offset = 从第几行开始（默认 0）；limit = 读几行。"
        "何时不用：你已经知道要找什么字符串时，用 sources.grep 更快更省；"
        "来源很短、你要整份时用 sources.snapshot。"
    ),
    "sources.grep": (
        "何时用：★ 你知道要找【什么】（一个 CUSIP、一个 accession number、"
        "一个表头、一个词），但不知道它在哪一行。"
        "输入：pattern = 正则；ref = 在哪个来源里找；"
        "context = 命中行上下各带几行；limit / offset 分页。"
        "何时不用：不知道找什么、要通读时，用 sources.read 按行看。"
    ),
    # -- files：把产物写下来 ------------------------------------------------ #
    "files.write": (
        "何时用：★ 产物是【一份文件】，你要把它的内容落到工作区。短文件直接写。"
        "输入：path = 工作区相对路径（必须落在你的写区内，越界会被拒）；"
        "content = 文件全文；append = 追加而非覆盖。"
        "★ 何时不用：文件很大时不要试图一次写完 —— 用 append 分次写，"
        "或干脆用 shell.run 让程序生成它。"
        "改已有文件的几行用 files.patch。"
    ),
    "files.patch": (
        "何时用：★ 你已经诊断出【某份文件里哪几行错了】—— 用 path + edits[] 一次改完这几处。"
        "别为了改几行就把整份文件重写一遍：整段重写会连已经写对的部分一起换掉，错越改越多。"
        "输入：path = 工作区相对路径（必须落在你的写区内）；"
        "edits = 数组，每条 {oldText, newText} —— oldText 是【要被替换的精确原文】，"
        "newText 是替换成什么（空串 = 删掉这一段）。"
        "★ 每条 oldText 必须在原文件里只出现一次；多条各自对【原文件】匹配，彼此不许重叠。"
        "何时不用：整份新写 / 整份重写用 files.write；产物要交给程序算（pandas 等）用 shell.run。"
        "★ 原文匹配不上会被具名拒，并告诉你文件里那一段附近实际是什么 —— "
        "把 oldText 往前/往后多带一行（相邻的 import、缩进、注释）就能唯一。"
    ),
    # -- exec：算出来、跑出来 ----------------------------------------------- #
    "shell.run": (
        "何时用：你要跑一条 shell 命令 —— 看目录、查文件、跑一个已有的脚本"
        "（数据要【算】就写成 `*.py` 再 `python x.py`，产物大到写不完时尤其如此）。"
        "输入：command = 命令；workdir = 工作目录；"
        "timeout = 秒（★ 默认 60、上限 600，不够要显式给）；background = 后台跑。"
        "★ 输出太长时正文只留【头 + 尾】两段，中间省掉多少会说清；被截时完整输出落盘，"
        "路径在返回的 `full_output_path` 里。"
        "何时不用：产物是一份短文件时用 files.write。"
        "★ 产出的文件与 files.write 同等记账。"
    ),
    # -- traces：上游的【过程】（不是结果） ---------------------------------- #
    "traces.search": (
        "何时用：★ 你想知道【上游某个块当时到底试过什么、被什么拒了、卡在哪一步】—— "
        "产物与交接件里没写、而你又不想自己重建一遍来找原因时。"
        "输入：query = 关键字（工具名、报错词、文件名、字段名都行，大小写不敏感）。"
        "返回：命中项列表（哪一块 / 第几步 / 用的什么工具 / 命中片段）。"
        "★ 标记是【过程】：那是上游当时的行为，不是经过判定的结论，要用先自己核。"
        "何时不用：brief 的 upstream 已经够用时不用；它只覆盖你【上游闭包】里已跑完的块，"
        "看不到下游、也看不到平行的别块；每块每次跑测最多调 12 次（search + read 合计），超了会被拒。"
    ),
    "traces.read": (
        "何时用：`traces.search` 给了你某一块某几步的线索，你要看那几步的【完整内容】时。"
        "输入：block_id = search 结果里给的那个；seq = 第几步，可写单个 `3`，也可写一串 `3,5,7`。"
        "何时不用：还想先按关键字找用 `traces.search`；只想拿上游的【产物】用 `task.upstream`"
        "（那是结果，这是过程）；读上游写给接手人的说明，在 brief 的 upstream 里。"
        "★ 一个块一次跑测最多 12 次 traces.* 调用，超了会被具名拒。"
    ),
    # -- semantics：查口径 -------------------------------------------------- #
    "semantics.lookup": (
        "何时用：你不确定某个业务词的【确切口径】（含哪些、不含哪些、什么单位）时。"
        "★ 口径有分歧时先查，不要凭常识猜 —— 猜错会静默地污染整份产物。"
        "输入：term = 那个词。"
        "何时不用：词义没有歧义时不必查。"
    ),
    "semantics.map": (
        "何时用：你要知道某个字段名对应本任务本体论里的哪个概念；"
        "或者想确认它【没有】对应概念（返回 unmapped）。"
        "输入：field = 字段名。"
        "何时不用：你只需查词义时用 semantics.lookup。"
    ),
    "semantics.check": (
        "何时用：你打算按某个口径解释一个词，想先确认这样用是对的。"
        "输入：usage = 你的用法。"
        "何时不用：只有在口径会被下游引用、或你不确定时才查。"
    ),
    # -- check：自己验一遍 -------------------------------------------------- #
    "check.local": (
        "何时用：★ 你准备交答案之前，用它看这一块当前被判成什么。"
        "比交上去被拒、再重来一遍便宜得多。"
        "输入：无。"
        "何时不用：答案还没成形时不必调（还没东西可判）。"
    ),
    "check.provenance": (
        "何时用：你想确认自己的溯源链是否成立 —— 每一行都指得回一份来源。"
        "★ 交答案前调一次，可以避免「值对但对不上来源」这一类被拒。"
        "输入：无。"
        "何时不用：本块没有任何来源可引（纯合并块）时不适用。"
    ),
    "check.assemble_dryrun": (
        "何时用：你想知道【这一块拼进整体】会不会出问题（接口对不上、"
        "合并代价过高）—— 只跑与本块相关的判定，不需要别的块的产物。"
        "输入：无。"
        "何时不用：只想知道本块过不过时用 check.local。"
    ),
}


_BASE_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("semantics.lookup", ("term",), "concept+unit+definition+aliases",
             group="semantics", description=_TOOL_DESCRIPTIONS["semantics.lookup"]),
    ToolSpec("semantics.map", ("field",), "concept_id | unmapped",
             group="semantics", description=_TOOL_DESCRIPTIONS["semantics.map"]),
    ToolSpec("semantics.check", ("usage",), "ok/reason",
             group="semantics", description=_TOOL_DESCRIPTIONS["semantics.check"]),
    ToolSpec("task.mysubtask", (), "goal/inputs/output/axis/mode", "subtask",
             group="task", description=_TOOL_DESCRIPTIONS["task.mysubtask"]),
    ToolSpec("task.upstream", ("name",), "schema+path", "inputs",
             group="task", description=_TOOL_DESCRIPTIONS["task.upstream"]),
    #: ★ ①(b) 兜底：一次把【全部】声明输入取回来，"忘了取"的代价降到一次调用。
    ToolSpec("task.upstream_all", (), "every declared input, by name", "inputs",
             group="task", description=_TOOL_DESCRIPTIONS["task.upstream_all"]),
    ToolSpec("task.contract", (), "interface contract",
             group="task", description=_TOOL_DESCRIPTIONS["task.contract"]),
    ToolSpec("check.local", (), "Verdict for this block", "subtask",
             group="check", description=_TOOL_DESCRIPTIONS["check.local"]),
    ToolSpec("check.provenance", (), "provenance report", "subtask",
             group="check", description=_TOOL_DESCRIPTIONS["check.provenance"]),
    ToolSpec("check.assemble_dryrun", (), "assembly verdict", "subtask",
             group="check", description=_TOOL_DESCRIPTIONS["check.assemble_dryrun"]),
    ToolSpec("sources.snapshot", ("ref",), "Snapshot", "authorized sources",
             group="sources", description=_TOOL_DESCRIPTIONS["sources.snapshot"]),
    #: ★ 读：整份取回来会撑爆上下文，所以给它【带行号的分页】与【正则检索】。
    #: 路径参数不是任意路径，而是**授权来源的 ref** —— 换的是"能指到哪"，不是语义。
    ToolSpec(
        "sources.read",
        ("ref",),
        "the source's text as LINE_NUM|CONTENT, one window at a time",
        "authorized sources",
        optional=("offset", "limit"),
        group="sources",
        description=_TOOL_DESCRIPTIONS["sources.read"],
    ),
    ToolSpec(
        "sources.grep",
        ("pattern",),
        "regex matches (with context lines) inside an authorized source, or among its names",
        "authorized sources",
        optional=("ref", "target", "limit", "offset", "context"),
        group="sources",
        description=_TOOL_DESCRIPTIONS["sources.grep"],
    ),
    #: ★ 写：产物直接落工作区（父目录自动创建），不再要求"整个文件装进一个字符串"。
    #: `append` 是对"整份覆盖写"的一处**有意扩展**：产物大于单次调用上限时，
    #: 覆盖写没有诚实的办法分次写完（实作与理由见 `services/workspace.py`）。
    ToolSpec(
        "files.write",
        ("path", "content"),
        "the file was written (bytes + digest)",
        "write region",
        optional=("append",),
        group="files",
        description=_TOOL_DESCRIPTIONS["files.write"],
    ),
    #: ★★ 定点修改（`#edit`）：**参数形状改成 `path` + `edits[]`**（flash `edit` 的语义）。
    #: 原来它是 `path + old_string + new_string`（单条），描述却写着"改几处文字" ——
    #: 说得到、做不到。实测（4B）：模型宁可整段重写脚本 33~41 次，也一次没调它。
    #: 旧参数在 **handler 里**仍兼容（既有调用方不被砍），但**不再广告**给模型：
    #: 一个工具只留一种形状，模型才不会在两种写法之间选错。
    ToolSpec(
        "files.patch",
        ("path",),
        "exact text replacement(s); a non-unique or overlapping match is refused, nothing is written",
        "write region",
        optional=("edits",),
        group="files",
        description=_TOOL_DESCRIPTIONS["files.patch"],
        param_schemas={
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "oldText": {"type": "string", "description": "要被替换的精确原文"},
                        "newText": {"type": "string", "description": "替换成什么（空串 = 删掉）"},
                    },
                    "required": ["oldText", "newText"],
                },
            }
        },
    ),
    #: ★ 执行：cwd 落在工作区内；产生的文件与 `files.write` 同等处理。
    ToolSpec(
        "shell.run",
        ("command",),
        "exit code + stdout/stderr of a shell command run inside the workspace",
        "workspace cwd",
        optional=("background", "timeout", "workdir"),
        group="exec",
        description=_TOOL_DESCRIPTIONS["shell.run"],
    ),
    #: ★ 过程检索：**两段式**（先 search 再 read），照 ARTEX 的机制 ②。
    #:   两段不是风格，是预算：一次把上游过程整份灌进上下文，正是 B 臂 88K 上下文卡死的那条路。
    ToolSpec(
        "traces.search",
        ("query",),
        "hits (block_id / seq / tool / snippet) from this block's upstream traces",
        group="traces",
        description=_TOOL_DESCRIPTIONS["traces.search"],
    ),
    ToolSpec(
        "traces.read",
        ("block_id", "seq"),
        "the full recorded steps named by block_id + seq",
        group="traces",
        description=_TOOL_DESCRIPTIONS["traces.read"],
    ),
)



@dataclass(frozen=True)
class ToolSet:
    """The only channel that connects the semantic, task and check layers.

    Tools are *bound* to a subtask: the set a worker holds can only see that
    block. ``upstream`` is restricted to the inputs the subtask declared, and
    the restriction is enforced here rather than requested in a message.

    ``allowed_inputs`` is an inference: the spec binds tools to a subtask id but
    does not say where the declared input names are carried, and the permission
    rule has to be owned by the stable side to be auditable.
    """

    subtask: str
    semantics: SemanticsTools
    task: TaskTools
    check: CheckTools
    sources: SourceTools | None = None
    #: ★ `#47`: the block's own working area. ``None`` = this deployment has no
    #: workspace, and then the write/execute tools are **not advertised at all**
    #: (the same rule `sources` follows) -- a block cannot ask for a capability
    #: the deployment never granted.
    workspace: WorkspaceTools | None = None
    #: ★★ 上游块的**过程**（ARTEX 机制 ②）。``None`` = 本块没有可检索的上游
    #: （入口块 / 部署没给）⇒ `traces.*` **根本不广告** —— 与 `sources`/`workspace`
    #: 同一条规则：拿不到的能力不该被广告出来（否则模型只会撞一堵墙）。
    traces: TraceTools | None = None  # blame: fail-closed — 省略即"本块没有可检索的上游"⇒ traces.* 根本不广告，方向保守
    allowed_inputs: tuple[str, ...] = ()
    #: ★ A11: the snapshots this block did not read itself but inherited from the
    #: parts it consumes. A merge block may cite these and nothing else.
    inherited_sources: tuple[str, ...] = ()
    #: ★★ 工具面收束（核心层 + 按需层）：契约里**真有语义声明**吗 —— 由装配方从契约现算
    #: （交付物字段声明了类型以外的东西，见 `DeliverableField.declared`）。
    #: 拿不到/没声明 ⇒ **不给** `semantics.*`（fail-closed：不知道就不广告）。
    semantics_declared: bool = False  # blame: fail-closed — 省略即"契约没声明语义"⇒ 不广告 semantics.*
    #: ★★ 本块**有交付物**吗 —— 由装配方从 `subtask.output × contract.deliverables` 现算。
    #: 没有 ⇒ 不广告 `check.*`（一个不产出交付物的块，让它自查"这一块过没过"是噪声）。
    has_deliverable: bool = False  # blame: fail-closed — 省略即"本块不产出交付物"⇒ 不广告 check.*
    def __post_init__(self) -> None:
        if self.subtask == "":
            raise ValueError("ToolSet.subtask must not be empty")

    def allows_upstream(self, name: str) -> bool:
        return name in self.allowed_inputs

    def require_upstream(self, name: str) -> None:
        if not self.allows_upstream(name):
            raise PermissionError(f"input {name!r} is not declared by subtask {self.subtask!r}")

    def _wrote_a_file(self) -> bool:
        """★ 按需层：本块**已经写过至少一个文件**了吗 —— 从运行时状态现算。

        判据是 `WriteBudget.paths`（`files.write` / `files.patch` / 答案 `files` 都会
        `note_write` 进来）。★ **拿不到就 False**（fail-closed）：没有 workspace、
        或者它还没写过 —— 两种都不该让 `files.patch` 出现在菜单上。

        ★ 代价是 O(1)（读一个集合），因为 `specs()` **每一轮都会被调**（`tool_schemas`）。
        ⚠️ 已知边界：`shell.run("> x")` 造出来的文件**不算**（执行副作用不 `note_write`）——
        那种块看不到 `files.patch`。fail-closed 方向，如实记在报告里。
        """
        if self.workspace is None:
            return False
        budget = getattr(self.workspace, "write_budget", None)
        paths = getattr(budget, "paths", None)
        return bool(paths)

    def _advertises(self, name: str, *, dynamic: bool) -> bool:
        """这一条工具在当前状态下该不该出现。

        ``dynamic=False`` 时**忽略会随轮次变化的门**（`files.patch` 的"写过没有"）——
        注册表要按那个超集注册，否则菜单后出现的工具派发不到（见 `servable_specs`）。
        """
        # -- 能力门（既有规则，一条不动） ---------------------------------- #
        if name.startswith("sources.") and self.sources is None:
            return False
        if name.startswith(("files.", "shell.")) and self.workspace is None:
            return False
        if name.startswith("traces.") and self.traces is None:
            return False
        # -- 按需层：满足条件才暴露（三条都从状态现算，拿不到就不给） -------- #
        if name.startswith("semantics.") and not self.semantics_declared:
            return False
        if name.startswith("check.") and not self.has_deliverable:
            return False
        # ★ 唯一的**轮内会变**的门：本块写过文件之后 `files.patch` 才亮出来。
        #   `dynamic=False`（注册表那一遍）忽略它 —— 否则菜单后出现的工具派发不到。
        return not (dynamic and name == "files.patch" and not self._wrote_a_file())

    def specs(self) -> tuple[ToolSpec, ...]:
        """**这一轮**广告给模型的工具面（核心层 + 满足条件的按需层）。

        A capability the block does not hold is not advertised: no ``sources``
        means no read tools, no ``workspace`` means no write/execute tools.
        """
        return tuple(spec for spec in _BASE_TOOL_SPECS if self._advertises(spec.name, dynamic=True))

    def servable_specs(self) -> tuple[ToolSpec, ...]:
        """★ 注册表必须能**派发**的全部工具 —— `specs()` 的**超集**。

        为什么必须有这一层：`files.patch` 是**轮内会变**的（本块写过文件才出现），
        而注册表在块开始时只建一次（`services/registry.py::build_registry`）。
        只按 `specs()` 注册 ⇒ 菜单后来出现了 `files.patch`，派发却答 "unknown tool"
        —— 模型照着菜单调用却吃闭门羹，比不给它更糟。
        """
        return tuple(spec for spec in _BASE_TOOL_SPECS if self._advertises(spec.name, dynamic=False))
