"""研究类取数的实作 —— `core/ports.py::ResearchPort` 的唯一实现（易变侧）。

    research(question, budget) -> tuple[Snapshot, ...]

## 它在流水线里的位置

    契约（含研究问题）→ ★ 检索阶段 → 快照 → 切分 → 派块 → 判定 → 汇总
                          ^^^^^^^^ 只有这一段是新的

★ 产出物与 `SourcePort` 同型（都是 `Snapshot`），所以快照 / 溯源 / 写区 / 判定
  四套机制一个字都不用改。

## 三条来自 `web-use` 的纪律，逐条落到代码

```
① 直连优先      先按地址直抓（最便宜、最准）；抓不成才降级走 Jina Reader
② 降级必须可见  降级来的快照标 backend=<备选> + degraded=True —— 下游要能知道"这条打折了"
③ 不撞同一面墙  403/429 不当"重试信号"，当"换后端信号"；换完还不行就认输
```

## ★★ 种子是【起点】，不是【范围】

用户口径原话：*"给了 URL 没说限定，他也应该自己多抓几个"*。
所以种子抓完之后**必须继续往外扩** —— 从抓回来的正文里挖出更多相关来源，
按 `budget.max_depth` 递归。这是"研究"和"下载"的分界。

★ 相关性的判据是**通用的**：拿 `question` 的词去匹配候选地址本身。
  **不写死任何领域词** —— 写死领域词就是把这套壳过拟合到一道题上。

## ★ 产出的 ref 怎么排（块按 ref 读来源，所以这一层必须想清楚）

    第 0 份   ref = question.ref        检索清单：这次找到哪些来源、各走哪条后端、降级没有
    第 i 份   ref = f"{question.ref}:{i}"  第 i 条材料本身

清单放第 0 位是有用的：块先看清单就知道**手里有哪些材料**，
再按需 `sources.grep` / `sources.read` 去读具体的 —— 不用把 N 份材料全塞进 brief。
"""

from __future__ import annotations

import html as html_module
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from ..core.model import ResearchBudget, ResearchQuestion, Snapshot

__all__ = [
    "BACKEND_DIRECT",
    "BACKEND_JINA",
    "BACKEND_LOCAL",
    "DEFAULT_CHANNELS",
    "MIN_VISIBLE_CHARS",
    "LinkExpandingResearch",
    "ResearchUnavailable",
    "extract_links",
    "html_to_text",
    "html_to_text_problem",
    "question_terms",
]

#: 后端名字 —— 报告里 `Snapshot.backend` 取这些值（`web-use` 纪律②的落点）
BACKEND_DIRECT = "direct"
#: ★ 直连抓到 HTML 之后**在本地转成文本** —— 不花外部服务的那条路
BACKEND_LOCAL = "direct+html2text"
BACKEND_JINA = "jina"

#: 默认可用渠道。★ 真跑时由入口用 `agent-reach doctor` 的读数覆盖。
#: 这台机器实测：「任意网页（Jina Reader）」通、「全网语义搜索」没配（见 [待验①]）。
#: blame: fail-closed — 默认只开实测最稳的两条；要更宽的渠道得显式给
DEFAULT_CHANNELS: tuple[str, ...] = (BACKEND_DIRECT, BACKEND_JINA)

#: Jina Reader 前缀 —— `agent-reach doctor` 报"任意网页"这条就是它。
JINA_PREFIX = "https://r.jina.ai/"

#: 不抓的扩展名：二进制 / 媒体 / 压缩包 —— 那是"下载"，不是"读资料"
_SKIP_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
    ".zip", ".tar", ".gz", ".7z", ".rar", ".exe", ".dmg",
    ".mp4", ".mp3", ".wav", ".avi", ".mov", ".woff", ".woff2", ".ttf",
)

#: 停用词 —— 中英各一小张，只为"拿 question 的词去匹配候选地址"，不做任何领域假设
_STOP = frozenset(
    ["the", "a", "an", "and", "or", "of", "to", "in", "for", "on", "with", "by", "from", "at", "is", "are", "was", "were", "be", "been", "this", "that", "these", "those", "it", "its", "as", "into", "about", "over", "after", "before", "more", "most", "的", "了", "和", "与", "及", "或", "在", "是", "对", "从", "为", "以", "把", "被", "中", "上", "下", "里", "这", "那", "有", "无", "不", "也", "就", "都"]
)

_LINK_RE = re.compile(r'href\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
_MD_LINK_RE = re.compile(r"\[[^\]]{0,120}\]\((https?://[^)\s]+)\)")
_BARE_RE = re.compile(r"https?://[^\s<>\"'\)\]]+")

#: HTML 转换里要去掉的整块（内容为代码/样式，不是正文）
_DROP_BLOCK_RE = re.compile(r"(?is)<(script|style|noscript|template|svg)\b.*?</\1>")
#: 块级标签换算行 —— 不这么做，整页会挤成一行（正是"HTML 读不动"的成因）
_BLOCK_TAG_RE = re.compile(r"(?is)<(?:br|hr)\s*/?>|</(?:p|div|li|tr|h[1-6]|section|article|blockquote|pre)\s*>")
_TAG_RE = re.compile(r"(?s)<[^>]+>")
_BLANK_RE = re.compile(r"[ \t\u00a0]+")

#: ★ 本地转换后，正文至少要有这么多字符才算"这页真有内容"。
#: 依据（实测）：SSR 页面转出来 11,546 字符；纯 CSR 的壳只剩导航/版权，通常 < 200。
#: blame: fail-open — 阈值偏低（宁可本地转出少内容交出去，也不轻易判"没内容"去花外网那趟）
MIN_VISIBLE_CHARS = 200


class ResearchUnavailable(RuntimeError):
    """这次取数拿不到东西 —— ★ 具名，不制造空快照。"""

    #: 与 `SourceError` 同族：一个来源取不到，说的是这条来源，不是这个块。
    blame = "tool"


@dataclass(frozen=True)
class _BackendUnavailable(RuntimeError):
    """某个后端这条路不通（403/429/超时）—— 是"换后端"的信号，不是"再等等"。"""

    backend: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - 只为人读
        return f"{self.backend} unavailable: {self.detail}"


def question_terms(question: str) -> tuple[str, ...]:
    """把研究问题切成词 —— **通用**的，不含任何领域词表。"""
    raw = re.split(r"[^0-9A-Za-z\u4e00-\u9fff]+", question.lower())
    terms: list[str] = []
    for piece in raw:
        if not piece or piece in _STOP:
            continue
        # ★ 汉字没有空格，长串按 2 字滑窗切 —— 中文检索的通用做法，不假设词表
        if re.fullmatch(r"[\u4e00-\u9fff]+", piece) and len(piece) > 2:
            terms.extend(piece[i : i + 2] for i in range(len(piece) - 1))
        else:
            terms.append(piece)
    ordered: list[str] = []
    for term in terms:
        if len(term) >= 2 and term not in ordered:
            ordered.append(term)
    return tuple(ordered)


def extract_links(text: str, *, base: str = "") -> tuple[str, ...]:  # blame: whitelist — base 空 = 只认绝对地址（相对地址无从补全时宁可丢）
    """从一段正文里挖出候选来源地址 —— **纯函数**，不碰网络。

    三种写法都认：HTML 的 `href=`、Markdown 的 `[文字](地址)`、裸地址。
    """
    found: list[str] = []
    for pattern in (_LINK_RE, _MD_LINK_RE, _BARE_RE):
        for raw in pattern.findall(text):
            candidate = raw.strip()
            if not candidate or candidate.startswith(("#", "javascript:", "mailto:", "data:")):
                continue
            absolute = urllib.parse.urljoin(base, candidate) if base else candidate
            parsed = urllib.parse.urlparse(absolute)
            if parsed.scheme not in ("http", "https"):
                continue
            if parsed.path.lower().endswith(_SKIP_SUFFIXES):
                continue
            # ★ 去掉 fragment（同一页面的锚点不是新来源），保留 query（常是分页/检索参数）
            clean = urllib.parse.urlunparse(parsed._replace(fragment=""))
            if clean not in found:
                found.append(clean)
    return tuple(found)


def html_to_text(markup: bytes) -> str:
    """把 HTML 变成**块读得动**的文本 —— 纯 stdlib，不碰网络、不依赖第三方。

    ## 为什么必须做这一步（实测的账）

    现代站点（Next.js 等）把整页 HTML 压成**一行**：`agentic-engineering-jobs.com`
    抓回来是 141,125 字节 / **1 行**。而块是**按行**读的（`grep` / `read`）——
    一行 = 只能返回那一条、翻页翻不动 ⇒ 模型反复重试同一个调用 ⇒ 烧到 `restart_cap` 判死。

    ## 两步：先去掉"不是正文的整块"，再把块级标签换算行

    ```
    ① 去掉 <script> / <style> / <noscript> / <template> / <svg> 及其内容
       （一份 SPA 首页有 45 个 <script>，它们不是正文）
    ② 块级标签（</p> </div> </li> </h1-6> <br> …）换成换行
       ★ 不做这一步，整页还是会挤成一行 —— 这正是本函数存在的理由
    ③ 剥标签 → 反转义实体 → 压空白 → 丢空行
    ```

    ★ 实测效果：那份 141 KB / 1 行的页面 → **11,546 字符 / 160 行**干净正文，
      段落、标题、表格数据全在。
    """
    text = markup.decode("utf-8", "replace")
    text = _DROP_BLOCK_RE.sub(" ", text)
    text = _BLOCK_TAG_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html_module.unescape(text)
    lines = [_BLANK_RE.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def html_to_text_problem(_markup: bytes, text: str) -> str:
    """本地转出来的正文够不够 —— 空串 = 够用，非空 = 该换后端。

    ★ 这条判据把"要不要花外网那趟"变成一个**可测的量**，而不是拍脑袋：
      转出的正文太少 ⇒ 这页的内容多半靠 JS 渲染，本地怎么转都出不来 ⇒ 才走 Jina。
    """
    if len(text) >= MIN_VISIBLE_CHARS:
        return ""
    return f"local conversion yielded only {len(text)} visible chars (< {MIN_VISIBLE_CHARS})"


def relevance(link: str, terms: tuple[str, ...]) -> int:
    """候选来源跟研究问题的相关度 —— 命中的词数。

    ★ 只看**地址本身**（含路径与 query，常带标题 slug）。不为了判相关性去把每个候选读一遍 ——
      那会让"找来源"变成"把候选全读一遍"，成本失控。
    """
    haystack = urllib.parse.unquote(link).lower()
    return sum(1 for term in terms if term in haystack)


def _suffix_of(locator: str) -> str:
    path = urllib.parse.urlparse(locator).path
    dot = path.rfind(".")
    if dot == -1:
        return ".txt"
    suffix = path[dot:].lower()
    return suffix if 1 < len(suffix) <= 6 and suffix.isascii() else ".txt"


@dataclass
class LinkExpandingResearch:
    """抓种子 → 从正文挖更多来源 → 递归 → 冻住。"""

    store: Any  # SnapshotStore（在易变侧，core 只认识 Snapshot）
    #: 「这个来源抓过了」的台账 —— 有它，第二次跑不再重复抓（b51 的保证）
    ledger: Any = None  # blame: fail-closed — 没台账 ⇒ 每次都抓（多抓一次好过复用错东西）
    #: ★ 由入口注入（壳不读时钟）：所有快照共用同一个捕获时刻 = "这一次检索"
    captured_at: str = ""  # blame: fail-closed — 空 = 没给捕获时刻（壳不读时钟，由入口注入）
    source_kind: str = "research"  # blame: whitelist — 固定值，不是"缺省省略"；改它等于改溯源分层
    timeout_s: float = 30.0  # blame: fail-open — 30s 是超时上限而非行为开关；够短到不会挂住，够长到能拿回大页
    user_agent: str = "bobanana6/0.1"  # blame: whitelist — 自报家门，不给它伪装成浏览器
    #: 当前实际可用的后端 —— 入口可用 `agent-reach doctor` 的读数覆盖它
    channels: tuple[str, ...] = DEFAULT_CHANNELS  # blame: fail-closed — 只开实测最稳的两条；更宽的渠道要显式给
    #: 注入给测试的 opener；None = 用 urllib 的真 opener（环境代理随之生效）
    opener: Any = None  # blame: whitelist — None = 用 urllib 的真 opener（环境代理随之生效），只在测试里注入
    headers: dict[str, str] = field(default_factory=dict)  # blame: fail-closed — 空 = 不加任何自定义头（不塞凭据、不伪装）

    # ---------------------------------------------------------------- 后端 --

    def _open(self, locator: str, *, backend: str) -> tuple[bytes, str]:
        """取回正文 → `(字节, Content-Type)`。

        ★ 要 Content-Type 是有原因的（实测的账）：**HTML 和"可读文本"不是一回事**。
          现代 SPA（Next.js 等）会把整页 HTML 压成**一行** —— 141 KB 一行，
          `grep` 只能返回那一条、`read` 也只能读到那一行 ⇒ **材料冻住了但读不动**。
        """
        request = urllib.request.Request(
            locator, headers={"User-Agent": self.user_agent, **dict(self.headers)}
        )
        opener = self.opener if self.opener is not None else urllib.request.build_opener()
        with opener.open(request, timeout=self.timeout_s) as response:
            status = int(getattr(response, "status", 200))
            if status != 200:
                # ★ 403/429 不是"等等再来"，是"换后端"（web-use 纪律③）
                raise _BackendUnavailable(backend, f"HTTP {status}")
            content_type = str(response.headers.get("Content-Type", ""))
            return response.read(), content_type

    @staticmethod
    def _readable(content_type: str) -> bool:
        """这份直连连回来的东西，块**读得动**吗？

        ★ 判据只认一条：**是标记语言就算读不动**。
          `text/html` 交给块 = 它得自己从标签里刨文字，而 SPA 那种单行页面连刨都刨不了
          （`grep`/`read` 是按行工作的）。

        ★ 不认识的类型算读得动（fail-open 的那一侧）——
          因为"认不出"不等于"不能读"，而误判成不能读会白白多打一次网络。
          这里的方向与别处相反是**有意的**：多打一次网络无害，把能用的材料当成不能用才有害。
        """
        kind = content_type.split(";", 1)[0].strip().lower()
        return kind not in ("text/html", "application/xhtml+xml")

    def _fetch(self, locator: str) -> tuple[bytes, str, bool]:
        """取一份材料 → `(正文, 后端名, 是否降级)`。

        ## 直连优先，但"能拿到字节"不等于"拿到了可用的材料"

        ```
        md / raw / json / csv   → 直连拿回的就是正文        ⇒ 用它（backend=direct）
        text/html，本地转得出正文 → ★ 本地转成文本再用        （backend=direct+html2text）
                                  纯 stdlib，不花外部服务、不依赖网络、确定性
        text/html，本地转不出正文 → 内容靠 JS 渲染 ⇒ 才走 Jina（backend=jina, degraded）
        ```

        ★★ 这两条都是**实测逼出来的**：`agentic-engineering-jobs.com` 抓回来
          141 KB **一行**，块的 `grep`/`read` 对它失效 ⇒ 模型反复重试同一个调用
          ⇒ 整块烧到 `restart_cap` 判死。取数是成功的，**材料是不可用的**。

        ★ 先本地转、转不出来才走外援 —— 顺序不能反：
          那份页面本地一转就是 11,546 字符 / 160 行干净正文（SSR 的正文就在 HTML 里），
          **白白多打一趟外部服务**是这一步要避免的。
          只有真·CSR（正文靠 JS 拉）才值得花那一趟。

        ★ 全都不可用 ⇒ 抛具名异常，**不返回空内容** ——
          "取不到"和"内容是空的"必须分得开。
        """
        failures: list[str] = []
        if BACKEND_DIRECT in self.channels:
            try:
                body, content_type = self._open(locator, backend=BACKEND_DIRECT)
                if self._readable(content_type):
                    return body, BACKEND_DIRECT, False
                # ★ HTML：先在本地转，转得出正文就不花外部服务
                text = html_to_text(body)
                problem = html_to_text_problem(body, text)
                if not problem:
                    return text.encode("utf-8"), BACKEND_LOCAL, False
                failures.append(f"{BACKEND_DIRECT}: {problem}")
            except (urllib.error.URLError, OSError, ValueError, _BackendUnavailable) as exc:
                failures.append(f"{BACKEND_DIRECT}: {exc}")
        if BACKEND_JINA in self.channels:
            try:
                body, _content_type = self._open(JINA_PREFIX + locator, backend=BACKEND_JINA)
                return body, BACKEND_JINA, True  # ★ 降级 = True，如实标出来
            except (urllib.error.URLError, OSError, ValueError, _BackendUnavailable) as exc:
                failures.append(f"{BACKEND_JINA}: {exc}")
        raise ResearchUnavailable(
            f"no backend could read {locator!r} -- " + "; ".join(failures or ["no channels enabled"])
        )

    # ---------------------------------------------------------------- 落盘 --

    def _freeze(self, ref: str, body: bytes, *, backend: str, degraded: bool, locator: str) -> Snapshot:
        # ★ 后端的**产出形态**决定后缀，不是来源地址的后缀：
        #   Jina / 本地转换产出的都是**文本**，用来源的 `.html` 命名会误导下游
        #   （它按后缀猜内容形态）。
        if backend in (BACKEND_JINA, BACKEND_LOCAL):
            suffix = ".txt"
        else:
            suffix = _suffix_of(locator)
        frozen = self.store.put(
            ref=ref,
            body=body,
            source_kind=self.source_kind,
            captured_at=self.captured_at,
            suffix=suffix,
        )
        if self.ledger is not None:
            self.ledger.remember(frozen)
        # ★ 后端与降级**只在这里**加上去 —— `put` 是内容寻址的，跟"怎么取回来的"无关
        return Snapshot(
            ref=frozen.ref,
            body_path=frozen.body_path,
            captured_at=frozen.captured_at,
            source_kind=frozen.source_kind,
            backend=backend,
            degraded=degraded,
        )

    # -------------------------------------------------------------- 主流程 --

    def research(self, question: ResearchQuestion, budget: ResearchBudget) -> tuple[Snapshot, ...]:
        """把研究问题变成一批冻结材料。

        ★ 三条闸：`max_sources`（抓几份）、`max_depth`（扩几层）、`max_bytes`（落多少字节）。
          撞到任意一条就停 —— **停下来是正常出口，不是失败**。
        """
        terms = question_terms(question.question)
        frontier: list[tuple[str, int]] = [(seed, 0) for seed in question.seeds]
        seen: set[str] = set()
        kept: list[tuple[str, Snapshot]] = []
        total_bytes = 0

        while frontier and len(kept) < budget.max_sources:
            locator, depth = frontier.pop(0)
            if locator in seen:
                continue
            seen.add(locator)
            try:
                body, backend, degraded = self._fetch(locator)
            except ResearchUnavailable:
                continue  # ★ 单条取不到不拖垮整次检索，它只是不进结果
            if not body:
                continue
            if total_bytes + len(body) > budget.max_bytes:
                break
            total_bytes += len(body)
            ref = f"{question.ref}:{len(kept) + 1}"
            kept.append((locator, self._freeze(ref, body, backend=backend, degraded=degraded, locator=locator)))

            # ★★ 扩展：种子只是起点。深度没用完就继续往外挖。
            if depth >= budget.max_depth:
                continue
            text = body[:400_000].decode("utf-8", "replace")
            candidates = [link for link in extract_links(text, base=locator) if link not in seen]
            ranked = sorted(
                candidates,
                key=lambda link: (-relevance(link, terms), link),
                # blame: whitelist — 全 0 也要按地址序抓：种子页里挖到的链接本身就是"相关"的
                #   （它出现在这个页面上），不因为"地址里没命中问题词"就一条都不扩
            )
            room = max(0, budget.max_sources - len(kept) - len(frontier))
            for link in ranked[:room]:
                frontier.append((link, depth + 1))

        return (self._manifest(question, kept),) + tuple(snap for _loc, snap in kept)

    def _manifest(self, question: ResearchQuestion, kept: list[tuple[str, Snapshot]]) -> Snapshot:
        """检索清单 —— 让块先知道**手里有哪些材料**，再按需去读具体的。

        ★ 它也是一份快照（同型），所以下游照旧只认识 `Snapshot`。
        """
        lines = [f"# 检索清单：{question.question}", ""]
        lines.append(f"问题：{question.question}")
        if question.seeds:
            lines.append(f"种子（起点，不构成范围）：{', '.join(question.seeds)}")
        lines.append("")
        lines.append(f"共找到 {len(kept)} 份材料：")
        lines.append("")
        lines.append("| ref | 走的后端 | 降级 | 地址 |")
        lines.append("| --- | --- | --- | --- |")
        for _locator, snap in kept:
            lines.append(
                f"| {snap.ref} | {snap.backend} | {'是' if snap.degraded else '否'} | {_locator} |"
            )
        lines.append("")
        if any(snap.degraded for _loc, snap in kept):
            lines.append("★ 标了「降级=是」的材料是走备选后端取回来的，**完整度可能打折**，引用时留个心眼。")
        body = ("\n".join(lines) + "\n").encode("utf-8")
        return self._freeze(question.ref, body, backend="manifest", degraded=False, locator=question.ref)
