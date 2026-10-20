"""研究类取数的实作 —— `core/ports.py::ResearchPort` 的唯一实现（易变侧）。

    research(question, budget) -> tuple[Snapshot, ...]

## 它在流水线里的位置

    契约（含研究问题）→ ★ 检索阶段 → 快照 → 切分 → 派块 → 判定 → 汇总
                          ^^^^^^^^ 只有这一段是新的

★ 产出物与 `SourcePort` 同型（都是 `Snapshot`），所以快照 / 溯源 / 写区 / 判定
  四套机制一个字都不用改。

## 三条来自 `web-use` 的纪律，逐条落到代码

```
① 直连优先      已知结构化地址（官方 API）直接取；退而求其次才走备选后端
② 降级必须可见  降级来的快照标 backend=<备选> + degraded=True —— 下游要能知道"这条打折了"
③ 不撞同一面墙  403/429 不当"重试信号"，当"换后端信号"；换完还不行就认输
```

## ★★ 种子是【起点】，不是【范围】

用户口径原话：*"给了 URL 没说限定，他也应该自己多抓几个"*。
所以种子抓完之后**必须继续往外扩** —— 从抓回来的正文里挖出更多相关来源，
按 `budget.max_depth` 递归。这是"研究"和"下载"的分界。

★ 相关性的判据是**通用的**：拿 `question` 的词去匹配候选地址本身。
  **不写死任何领域词** —— 写死领域词就是把这套壳过拟合到一道题上。

## 熔断状态机（五条出口）

`BackendFailure.kind` 就是这五个身份（`FAIL_*` 常量）：

```
失败
  ├─ 403 / 429      → 换下一个后端（不等 —— 免费额度是别人的），并把 (后端, 主机) 熔断
  ├─ 连接超时        → 同一后端重试 1 次 → 再超时熔断换后端
  ├─ 返回空          → 不是错，可能真没结果 → 换措辞 1 次（有 `rewrite` 才换）→ 再空换后端
  ├─ 需要登录态      → 不是失败也不是"不许" → 记进清单，全部拿不到时具名交回上层
  └─ 全部不可用      → ★ 抛 `ResearchUnavailable`（具名弃权），不制造一份"共找到 0 份"的清单
```

★ 最后一条是红线：**拿不到依据 ⇒ 具名弃权，不产出**。一份"共找到 0 份材料"
的清单会让块在零依据下照写 —— 那是幻觉的温床。runner 把这条异常变成
具名的 `RunRefused`（`research_no_evidence` / `research_needs_authorization`）。

## ★ 可复现：同一份契约跑两次，材料逐字节相同

`ResearchLedger`（`research.index.json`，与冻结件同目录）按
`(ref, question, seeds, budget, channels)` 记下**这一次检索的完整结果**
（含 `captured_at` / `backend` / `degraded` / 信任层）。第二次跑**直接重放**，
一个网络请求都不打。台账缺失/损坏 ⇒ 当作没检索过（fail-closed：宁可重抓）。

## ★ 信任分层：落到 `Snapshot.source_kind`

```
T1 官方文档 / API 响应     → 可当事实
T2 论文 / 官方博客         → 可当事实（带版本）
T3 社区（HN / Reddit / X） → 当参考
T4 不知名博客 / 聚合站     → 只当线索
```

规则表定 T1/T2/T3 的**已知两端**；规则表没命中的地址缺省落 T4（fail-closed）。
`trust=` 钩子交给外层（模型 / 上层 agent）判中间层，**只允许判成 T2/T3** ——
T1 必须由规则表自证，不能由模型一句话升上去。

## ★ 产出的 ref 怎么排（块按 ref 读来源，所以这一层必须想清楚）

    第 0 份   ref = question.ref        检索清单：这次找到哪些来源、各走哪条后端、降级没有
    第 i 份   ref = f"{question.ref}:{i}"  第 i 条材料本身

清单放第 0 位是有用的：块先看清单就知道**手里有哪些材料**，
再按需 `sources.grep` / `sources.read` 去读具体的 —— 不用把 N 份材料全塞进 brief。
"""

from __future__ import annotations

import hashlib
import html as html_module
import ipaddress
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.model import ResearchBudget, ResearchQuestion, Snapshot
from .fetchers import suffix_for

__all__ = [
    "API_ROUTES",
    "BACKEND_DIRECT",
    "BACKEND_JINA",
    "BACKEND_LOCAL",
    "BACKEND_MANIFEST",
    "DEFAULT_CHANNELS",
    "FAIL_EMPTY",
    "FAIL_FORBIDDEN",
    "FAIL_NEEDS_LOGIN",
    "FAIL_RATE_LIMITED",
    "FAIL_TIMEOUT",
    "FAIL_UNAVAILABLE",
    "MIN_VISIBLE_CHARS",
    "RESEARCH_LEDGER_NAME",
    "TRUST_T1",
    "TRUST_T2",
    "TRUST_T3",
    "TRUST_T4",
    "BackendFailure",
    "LinkExpandingResearch",
    "ResearchLedger",
    "ResearchNeedsAuthorization",
    "ResearchUnavailable",
    "api_backend",
    "classify_http_status",
    "extract_links",
    "html_to_text",
    "html_to_text_problem",
    "question_terms",
    "trust_tier",
]

# --------------------------------------------------------------------------- #
# 后端名字 —— 报告里 `Snapshot.backend` 取这些值（`web-use` 纪律②的落点）
# --------------------------------------------------------------------------- #

BACKEND_DIRECT = "direct"
#: ★ 直连抓到 HTML 之后**在本地转成文本** —— 不花外部服务的那条路
BACKEND_LOCAL = "direct+html2text"
BACKEND_JINA = "jina"
#: 检索清单自己的后端名（它不是从网上取回来的，是本层生成的）
BACKEND_MANIFEST = "manifest"

#: 默认可用后端（**有序**：先直连，直连读不动才走备选）。
#: ★ 真跑时由入口用 `agent-reach doctor` 的读数覆盖。
#: 这台机器实测：「直连」通、「任意网页（Jina Reader）」通、「全网语义搜索」没配。
#: blame: fail-closed — 默认只开实测最稳的两条；要更宽的渠道得显式给
DEFAULT_CHANNELS: tuple[str, ...] = (BACKEND_DIRECT, BACKEND_JINA)

#: Jina Reader 前缀 —— `agent-reach doctor` 报"任意网页"这条就是它。
JINA_PREFIX = "https://r.jina.ai/"

#: 官方 API（直连优先的落点）：`(主机, 路径前缀, 后端名)`。
#: ★ 命中只在**标注**上多一个名字（`api:pypi`），不多打一次请求：
#:   "先搜 vs 先问有没有 API"的差别是**整条路径**，不是同一路径多一次握手。
API_ROUTES: tuple[tuple[str, str, str], ...] = (
    ("pypi.org", "/pypi/", "api:pypi"),
    ("huggingface.co", "/api/", "api:huggingface"),
    ("api.github.com", "/", "api:github"),
    ("export.arxiv.org", "/api/", "api:arxiv"),
    ("arxiv.org", "/api/", "api:arxiv"),
)

# --------------------------------------------------------------------------- #
# 信任分层
# --------------------------------------------------------------------------- #

TRUST_T1 = "T1"
TRUST_T2 = "T2"
TRUST_T3 = "T3"
TRUST_T4 = "T4"

#: 规则表的**已知两端**。命中的按表走；一个都不命中的落 `TRUST_T4`（fail-closed）。
#: ★ 顺序有意义：T1 先判，所以 `api.github.com` 不会被 `github.com` 抢走。
TRUST_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        TRUST_T1,
        (
            "pypi.org",
            "huggingface.co",
            "api.github.com",
            "docs.python.org",
            "developer.mozilla.org",
            "docs.rs",
            "kubernetes.io",
            "nodejs.org",
            "python.org",
        ),
    ),
    (
        TRUST_T2,
        (
            "arxiv.org",
            "export.arxiv.org",
            "openreview.net",
            "aclanthology.org",
            "aclweb.org",
            "neurips.cc",
            "icml.cc",
            "openai.com",
            "anthropic.com",
            "deepmind.google",
            "research.google",
            "ai.googleblog.com",
        ),
    ),
    (
        TRUST_T3,
        (
            "news.ycombinator.com",
            "reddit.com",
            "x.com",
            "twitter.com",
            "zhihu.com",
            "v2ex.com",
            "weibo.com",
            "bilibili.com",
            "juejin.cn",
            "github.com",
            "raw.githubusercontent.com",
        ),
    ),
)

# --------------------------------------------------------------------------- #
# 熔断状态机的五个身份
# --------------------------------------------------------------------------- #

FAIL_FORBIDDEN = "forbidden"
FAIL_RATE_LIMITED = "rate_limited"
FAIL_TIMEOUT = "timeout"
FAIL_EMPTY = "empty"
FAIL_NEEDS_LOGIN = "needs_login"
FAIL_UNAVAILABLE = "unavailable"

#: 会让 (后端, 主机) 熔断的身份。★ `empty` **不在**里面：
#: 200-空是"确实没有"，不是"这条路断了" —— 把空结果当熔断信号会误杀好后端。
_CIRCUIT_BREAKING = frozenset(
    {FAIL_FORBIDDEN, FAIL_RATE_LIMITED, FAIL_NEEDS_LOGIN, FAIL_TIMEOUT, FAIL_UNAVAILABLE}
)

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

#: HTML 转换为正文时的**输入上限**与"去整块"的次数上限。
#: ★ 实测（审计读数）：`_DROP_BLOCK_RE` 在大量未闭合 `<script` 上是 O(n²)，
#:   128KB 8.1s / 256KB 32.4s；socket 超时管不到 CPU。这两个闸把最坏情况钉在常数级。
MAX_HTML_CONVERT_BYTES = 1_000_000  # blame: fail-open — 超上限只截断转换，不判整页失败
MAX_DROP_BLOCKS = 200  # blame: fail-open — 块数超上限就跳过"去整块"，只做线性的剥标签

#: 检索台账文件名（与冻结件同目录）
RESEARCH_LEDGER_NAME = "research.index.json"


# --------------------------------------------------------------------------- #
# 纯函数：地址 → 后端名 / 信任层
# --------------------------------------------------------------------------- #


def api_backend(locator: str) -> str:
    """这个地址是不是**已知的官方 API**？是就返回后端名，否则空串。

    ★ 只标注、不改请求：直连拿回来的就是那个字段，不该再为它多打一次搜索。
    """
    parsed = urllib.parse.urlparse(locator)
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"
    for api_host, path_prefix, name in API_ROUTES:
        if (host == api_host or host.endswith("." + api_host)) and path.startswith(path_prefix):
            return name
    return ""


def trust_tier(locator: str, middle: Callable[[str], str] | None = None) -> str:  # blame: fail-closed — 没有中间层判断者就落 T4
    """这份来源属于哪一层（T1–T4）。

    ★ 规则表定**已知两端**：T1/T2/T3 命中就按表；一个都不命中 ⇒ T4（fail-closed）。
    ★ `middle` 是**中间层**的判断者（模型 / 上层 agent）：它只允许判 T2/T3。
      T1 必须由规则表自证（"官方"不能由模型一句话升上去）；T4 是缺省，也不需要它降。
    """
    host = (urllib.parse.urlparse(locator).hostname or "").lower()
    for tier, hosts in TRUST_RULES:
        for known in hosts:
            if host == known or host.endswith("." + known):
                return tier
    if middle is not None:
        try:
            judged = str(middle(locator) or "")
        except Exception:  # noqa: BLE001 — 中间层判不了 ≠ 取数失败；落回缺省层
            judged = ""
        if judged in (TRUST_T2, TRUST_T3):
            return judged
    return TRUST_T4


def classify_http_status(status: int) -> str:
    """HTTP 状态码 → 熔断状态机的身份。"""
    if status in (401, 407):
        return FAIL_NEEDS_LOGIN
    if status in (402, 403, 451):
        return FAIL_FORBIDDEN
    if status == 429:
        return FAIL_RATE_LIMITED
    return FAIL_UNAVAILABLE


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


def _is_public_locator(locator: str) -> bool:
    """从**别人页面里挖出来**的地址能不能抓 —— 只挡最明确的几类内网目标。

    ★ 种子不经过这道检查（它们是契约/调用方给的）；这道检查只作用于**扩展链接**：
      别人页面里写一个 `http://169.254.169.254/...` 或 `http://127.0.0.1:...`，
      取数层就去抓，等于把壳变成 SSRF 跳板。
    ★ 残余风险：域名解析到内网（DNS rebinding）这里挡不住 —— 要挡得住得在
      连接层固定 IP，代价与收益不匹配；这里只做字面量 + 本机名的检查。
    """
    parsed = urllib.parse.urlparse(locator)
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    # ★ 字面量 IP 之外，还有"公共域名指向内网"的固定写法（不需要 DNS rebinding）：
    #   localtest.me / <ip>.nip.io / <ip>.sslip.io / metadata.google.internal 都是。
    internal_names = ("localhost", "localtest.me", "metadata.google.internal")
    internal_suffixes = (".localhost", ".local", ".internal", ".nip.io", ".sslip.io", ".xip.io")
    if host in internal_names or any(host.endswith(suffix) for suffix in internal_suffixes):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True  # 普通域名：交给网络层
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _same_host(one: str, other: str) -> bool:
    """两个地址是不是同一台主机（大小写无关；端口不参与比较）。"""
    left = (urllib.parse.urlparse(one).hostname or "").lower()
    right = (urllib.parse.urlparse(other).hostname or "").lower()
    return bool(left) and left == right


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
    text = markup[:MAX_HTML_CONVERT_BYTES].decode("utf-8", "replace")
    if text.count("<script") + text.count("<style") <= MAX_DROP_BLOCKS:
        text = _DROP_BLOCK_RE.sub(" ", text)
    # else: 病态页面（大量未闭合标签）—— 跳过"去整块"那一步，它正是 O(n²) 的来源；
    #       下面两步是线性的。（脚本内容可能漏进正文，代价远小于卡死整次检索。）
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


# --------------------------------------------------------------------------- #
# 异常与失败身份
# --------------------------------------------------------------------------- #


class ResearchUnavailable(RuntimeError):
    """这次取数拿不到东西 —— ★ 具名，不制造空快照、也不退回一份"共找到 0 份"的清单。

    ★ 这条红线是 bb6 特有的：块不能像人一样"停下问一句"，所以壳必须在派块之前
      把"这个研究问题没拿到依据"变成一个具名出口 —— 错答会得分，幻觉没有依据更糟。
    """

    #: 与 `SourceError` 同族：一个来源取不到，说的是这条来源，不是这个块。
    blame = "tool"


class ResearchNeedsAuthorization(ResearchUnavailable):
    """某个后端要登录态 —— 不是失败，也不是"不许"，是把决定权交回上层。"""

    blame = "tool"


@dataclass(frozen=True)
class BackendFailure:
    """一条后端失败的**身份**（不是一句日志）。"""

    backend: str
    kind: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - 只为人读
        return f"{self.backend}: {self.kind} ({self.detail})"


@dataclass(frozen=True)
class _BackendUnavailable(RuntimeError):
    """某个后端这条路不通（403/429/超时/要登录）—— 是"换后端"的信号，不是"再等等"。"""

    backend: str
    kind: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - 只为人读
        return f"{self.backend} unavailable ({self.kind}): {self.detail}"


# --------------------------------------------------------------------------- #
# 检索台账：同一份契约第二次跑，直接重放
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ResearchLedger:
    """一次检索的**完整结果**（有序快照 + 元数据），按检索键重放。

    ★ 为什么不是 `SourceLedger`：那个台账答"**这个 ref** 抓过没有"，键是 ref；
      研究展开的 ref（`hunt:1`…）是运行时按**抓到顺序**编的，第二次跑一旦某条
      取不到，后面的 ref 会整体前移 ⇒ 按 ref 复用会张冠李戴。
      所以这里按**整个检索**（问题 + 种子 + 预算 + 渠道）复用，键是那组输入的哈希。

    ★ fail-closed 四条：文件缺失 / 读不出 / 不是列表 / 指向的冻结件不在了
      ⇒ 一律当作没检索过（重抓，不复用一份指不到的内容）。
    """

    root: Path

    @property
    def path(self) -> Path:
        return self.root / RESEARCH_LEDGER_NAME

    def _read(self) -> dict[str, list[dict[str, Any]]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, list[dict[str, Any]]] = {}
        for key, entries in raw.items():
            if isinstance(entries, list) and all(isinstance(entry, dict) for entry in entries):
                out[str(key)] = entries
        return out

    def recall(self, key: str) -> tuple[Snapshot, ...] | None:
        entries = self._read().get(key)
        if not entries:
            return None
        snapshots: list[Snapshot] = []
        for entry in entries:
            body = entry.get("body")
            ref = entry.get("ref")
            if not isinstance(body, str) or not body or not isinstance(ref, str) or not ref:
                return None
            path = self.root / body
            if not path.is_file():
                return None
            # ★ 复核冻结件：文件名 = 内容摘要。截断/被替换的文件不能当"抓过了"复用
            #   （只 is_file 会把上一次崩溃留下的半份文件永久留在链上）。
            try:
                if not path.name.startswith(hashlib.sha256(path.read_bytes()).hexdigest()[:32]):
                    return None
            except OSError:
                return None
            snapshots.append(
                Snapshot(
                    ref=ref,
                    body_path=path,
                    captured_at=str(entry.get("captured_at", "")),  # blame: fail-closed — 缺时间戳不改变"复用"这个事实
                    source_kind=str(entry.get("source_kind", "")),  # blame: fail-closed — 缺分层不改变"复用"这个事实
                    backend=str(entry.get("backend", "")),  # blame: fail-closed — 缺后端名就当作"没走网络那条路"
                    degraded=bool(entry.get("degraded", False)),  # blame: fail-closed — 缺省 False = 当作没降级（不虚报）
                )
            )
        return tuple(snapshots)

    def remember(self, key: str, snapshots: tuple[Snapshot, ...]) -> None:
        entries = self._read()
        entries[key] = [
            {
                "ref": snapshot.ref,
                "body": snapshot.body_path.name,
                "captured_at": snapshot.captured_at,
                "source_kind": snapshot.source_kind,
                "backend": snapshot.backend,
                "degraded": snapshot.degraded,
            }
            for snapshot in snapshots
        ]
        self.root.mkdir(parents=True, exist_ok=True)
        # ★ 唯一临时名：多进程共享同一个快照目录时不互相踩
        staging = self.path.with_name(f"{self.path.name}.tmp.{os.getpid()}")
        staging.write_text(json.dumps(entries, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        # ★ 原子替换：中途挂掉留下的只会是 .tmp，台账本身要么是旧的要么是新的。
        staging.replace(self.path)

    def entries(self) -> dict[str, list[dict[str, Any]]]:
        """整本台账（观测用）。"""
        return self._read()


def _cache_key(question: ResearchQuestion, budget: ResearchBudget, channels: tuple[str, ...]) -> str:
    """检索键 = 这组输入（问题 / 种子 / 预算 / 渠道）的哈希。时间**不**进键。"""
    payload = {
        "ref": question.ref,
        "question": question.question,
        "seeds": list(question.seeds),
        "channels": list(channels),
        "budget": [budget.max_sources, budget.max_depth, budget.max_bytes],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# --------------------------------------------------------------------------- #
# 主实作
# --------------------------------------------------------------------------- #


@dataclass
class LinkExpandingResearch:
    """抓种子 → 从正文挖更多来源 → 递归 → 冻住。"""

    store: Any  # SnapshotStore（在易变侧，core 只认识 Snapshot）
    #: 「这个来源抓过了」的台账 —— 供 FileFetcher/HttpFetcher 用；本层另用 `ResearchLedger` 重放
    ledger: Any = None  # blame: fail-closed — 没台账 ⇒ 每次都抓（多抓一次好过复用错东西）
    #: ★ 由入口注入（壳不读时钟）：所有快照共用同一个捕获时刻 = "这一次检索"
    captured_at: str = ""  # blame: fail-closed — 空 = 没给捕获时刻（壳不读时钟，由入口注入）
    #: 检索清单的 source_kind；材料按 `trust_tier()` 落 T1–T4
    source_kind: str = "research"  # blame: whitelist — 固定值，不是"缺省省略"；改它等于改清单的溯源分层
    timeout_s: float = 30.0  # blame: fail-open — 30s 是超时上限而非行为开关；够短到不会挂住，够长到能拿回大页
    user_agent: str = "bobanana6/0.1"  # blame: whitelist — 自报家门，不给它伪装成浏览器
    #: 当前实际可用的后端 —— **有序**（首选在前）。入口可用 `agent-reach doctor` 的读数覆盖它
    channels: tuple[str, ...] = DEFAULT_CHANNELS  # blame: fail-closed — 只开实测最稳的两条；更宽的渠道要显式给
    #: 注入给测试的 opener；None = 用 urllib 的真 opener（环境代理随之生效）
    opener: Any = None  # blame: whitelist — None = 用 urllib 的真 opener（环境代理随之生效），只在测试里注入
    headers: dict[str, str] = field(default_factory=dict)  # blame: fail-closed — 空 = 不加任何自定义头（不塞凭据、不伪装）
    #: "返回空 → 换措辞 1 次"的实现。None = 没有措辞可换（URL 取数）⇒ 直接换后端。
    rewrite: Callable[[str], str] | None = None  # blame: fail-closed — 没有可换的措辞 ⇒ 直接换后端，不硬编一个假措辞
    #: 中间层（T2/T3）的判断者。None = 规则表未命中就落 T4（fail-closed）。
    trust: Callable[[str], str] | None = None  # blame: fail-closed — 没有中间层判断者 ⇒ 规则表没命中就落 T4
    #: 单份材料读取上限 —— 防止一个种子把内存吃光（`max_bytes` 是整次检索的落盘上限）
    max_body_bytes: int = 8_000_000  # blame: fail-closed — 单份材料封顶 8MB，超了就当这条取不到
    #: 关掉重放（诊断用；正常部署必须开着 —— 它是"跑两次逐字节相同"的那条线）
    cache: bool = True  # blame: whitelist — 重放是设计决定（§8 待验③倾向复用）；关掉它只用于诊断
    _opener: Any = field(default=None, init=False, repr=False)  # blame: fail-closed — 一次检索复用同一个 opener（无连接池至少别重造）
    _order: tuple[str, ...] = field(default_factory=tuple, init=False, repr=False)  # blame: fail-closed — 空 = 用实例的 channels（本次检索的有效顺序）
    _failures: list[BackendFailure] = field(default_factory=list, init=False, repr=False)
    _needs_login: list[tuple[str, str]] = field(default_factory=list, init=False, repr=False)  # blame: fail-closed — 空 = 这次检索没人要登录态
    _circuits: set[tuple[str, str]] = field(default_factory=set, init=False, repr=False)  # blame: fail-closed — 空 = 没有后端被熔断
    _errors: dict[str, str] = field(default_factory=dict, init=False, repr=False)  # blame: fail-closed — 空 = 没有取不到的来源

    # ---------------------------------------------------------------- 后端 --

    def _backend_order(self, override: tuple[str, ...] = ()) -> tuple[str, ...]:
        """有序、去重的后端列表 —— 首选在前；覆盖配置不得隐藏可用后端。

        ★ `question.channels` 也是覆盖（契约说"限定走哪些后端"）：空 = 用实例的默认列表。
        """
        ordered: list[str] = []
        for name in (override or self.channels):
            if name and name not in ordered:
                ordered.append(name)
        return tuple(ordered)

    def _cache(self) -> ResearchLedger:
        root = self.ledger.root if self.ledger is not None else self.store.root
        return ResearchLedger(root=Path(root))

    def _read(self, locator: str, *, backend: str) -> tuple[bytes, str]:
        """取回正文 → `(字节, Content-Type)`。失败一律抛 `_BackendUnavailable`（带身份）。"""
        target = JINA_PREFIX + locator if backend == BACKEND_JINA else locator
        request = urllib.request.Request(
            target, headers={"User-Agent": self.user_agent, **dict(self.headers)}
        )
        if self.opener is not None:
            opener = self.opener
        else:
            # ★ 一次检索复用同一个 opener：urllib 没有连接池，但至少别每个 URL 重建一遍
            if self._opener is None:
                self._opener = urllib.request.build_opener()
            opener = self._opener
        try:
            with opener.open(request, timeout=self.timeout_s) as response:
                status = int(getattr(response, "status", 200))
                content_type = str(response.headers.get("Content-Type", ""))  # blame: fail-open — 认不出类型算读得动（与 _readable 同向）
                # ★ 有上限地读：读到上限 +1 就知道"超了"，不把整份大文件塞进内存
                body = response.read(self.max_body_bytes + 1)
                if len(body) > self.max_body_bytes:
                    raise _BackendUnavailable(
                        backend, FAIL_UNAVAILABLE, f"body exceeds {self.max_body_bytes} bytes"
                    )
        except urllib.error.HTTPError as exc:
            # ★ 403/429 不是"等等再来"，是"换后端"（web-use 纪律③）
            raise _BackendUnavailable(backend, classify_http_status(int(exc.code)), f"HTTP {exc.code}") from exc
        except TimeoutError as exc:
            raise _BackendUnavailable(backend, FAIL_TIMEOUT, "timed out") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise _BackendUnavailable(backend, FAIL_TIMEOUT, "timed out") from exc
            raise _BackendUnavailable(backend, FAIL_UNAVAILABLE, str(reason or exc)) from exc
        except (OSError, ValueError) as exc:
            raise _BackendUnavailable(backend, FAIL_UNAVAILABLE, f"{type(exc).__name__}: {exc}") from exc
        if status != 200:
            raise _BackendUnavailable(backend, classify_http_status(status), f"HTTP {status}")
        return body, content_type

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

    @staticmethod
    def _links_of(payload: bytes, locator: str) -> tuple[str, ...]:
        """从**原始载荷**里挖候选链接（HTML 的 href / Markdown / 裸地址）。"""
        return extract_links(payload[:400_000].decode("utf-8", "replace"), base=locator)

    def _trip(self, backend: str, locator: str, kind: str) -> None:
        """把 (后端, 主机) 熔断 —— 本次检索的后续来源不再走这条路。"""
        if kind not in _CIRCUIT_BREAKING:
            return
        # ★ 键用 netloc（含端口）：同一台主机的不同端口是不同服务，
        #   一个端口的连接被拒不该把另一个端口也熔断。
        host = (urllib.parse.urlparse(locator).netloc or "").lower()
        self._circuits.add((backend, host))

    def _try_backend(self, backend: str, locator: str) -> tuple[bytes, str, bool, tuple[str, ...], str] | None:
        """一个后端的一次完整尝试（含超时重试 1 次、空结果换措辞 1 次）。

        ★ 第 4 项是**从原始载荷里挖到的链接**，不是从转换后的文本里挖的 ——
          HTML 本地转文本会把 `<a href>` 剥成锚文本，转完再挖链接永远是 0 条。
        """
        attempts = 0
        reworded = False
        while True:
            try:
                body, content_type = self._read(locator, backend=backend)
            except _BackendUnavailable as exc:
                if exc.kind == FAIL_TIMEOUT and attempts == 0:
                    # ★ 连接超时先重试 1 次（网络抖动），再超时才熔断换后端
                    attempts = 1
                    continue
                self._failures.append(BackendFailure(backend, exc.kind, exc.detail))
                if exc.kind == FAIL_NEEDS_LOGIN:
                    self._needs_login.append((locator, backend))
                self._trip(backend, locator, exc.kind)
                return None

            if not body:
                # ★ 返回空不是错，可能真没结果 —— 换措辞 1 次再判
                if self.rewrite is not None and not reworded:
                    candidate = self.rewrite(locator)
                    if candidate and candidate != locator:
                        locator = candidate
                        reworded = True
                        continue
                # 200-空**不**熔断：后端是活的，只是这个地址没内容
                self._failures.append(BackendFailure(backend, FAIL_EMPTY, "the backend returned 0 bytes"))
                return None

            if backend == BACKEND_DIRECT:
                api = api_backend(locator)
                if api:
                    return body, api, False, self._links_of(body, locator), content_type
                if self._readable(content_type):
                    return body, BACKEND_DIRECT, False, self._links_of(body, locator), content_type
                # ★ HTML：先在本地转，转得出正文就不花外部服务
                text = html_to_text(body)
                problem = html_to_text_problem(body, text)
                if not problem:
                    # ★ 链接从【原始 markup】里挖：转换后的文本没有 href 了
                    return text.encode("utf-8"), BACKEND_LOCAL, False, self._links_of(body, locator), content_type
                self._failures.append(BackendFailure(backend, FAIL_EMPTY, problem))
                return None

            if backend == BACKEND_JINA:
                return body, BACKEND_JINA, True, self._links_of(body, locator), content_type  # ★ 降级 = True，如实标出来

            # 其它显式声明的后端：按它自己的名字记账（"能读到"就是能读到）
            return body, backend, False, self._links_of(body, locator), content_type

    def _fetch(self, locator: str) -> tuple[bytes, str, bool, tuple[str, ...], str]:
        """取一份材料 → `(正文, 后端名, 是否降级, 载荷里的链接)`。

        ## 直连优先，但"能拿到字节"不等于"拿到了可用的材料"

        ```
        官方 API 地址            → 直连，后端名 api:<host>（不额外搜索）
        md / raw / json / csv   → 直连拿回的就是正文        ⇒ 用它（backend=direct）
        text/html，本地转得出正文 → ★ 本地转成文本再用        （backend=direct+html2text）
        text/html，本地转不出正文 → 内容靠 JS 渲染 ⇒ 才走 Jina（backend=jina, degraded）
        ```

        ★ 全都不可用 ⇒ 抛具名异常，**不返回空内容** ——
          "取不到"和"内容是空的"必须分得开。
        """
        before = len(self._failures)
        for backend in (self._order or self._backend_order()):
            host = (urllib.parse.urlparse(locator).netloc or "").lower()
            if (
                backend == BACKEND_JINA
                and not _is_public_locator(locator)
                and _is_public_locator(JINA_PREFIX)
            ):
                # ★ 内网 / 本机地址不交给外部备选后端：它那边根本够不着，
                #   而且等于把内部地址发给第三方（SSRF / 泄露两侧都不该做）。
                self._failures.append(
                    BackendFailure(backend, FAIL_UNAVAILABLE, "non-public locator is not sent to an external fallback")
                )
                continue
            if (backend, host) in self._circuits:
                self._failures.append(
                    BackendFailure(backend, FAIL_UNAVAILABLE, "circuit open for this host in this hunt")
                )
                continue
            result = self._try_backend(backend, locator)
            if result is not None:
                return result
        detail = "; ".join(str(failure) for failure in self._failures[before:]) or "no channels enabled"
        raise ResearchUnavailable(f"no backend could read {locator!r} -- {detail}")

    # ---------------------------------------------------------------- 落盘 --

    def _freeze(
        self,
        ref: str,
        body: bytes,
        *,
        backend: str,
        degraded: bool,
        locator: str,
        source_kind: str | None = None,  # blame: fail-closed — 不给就按地址判层（不是固定成'可信'）
        content_type: str = "",
    ) -> Snapshot:
        # ★ 后端的**产出形态**决定后缀，不是来源地址的后缀：
        #   Jina / 本地转换产出的都是**文本**，用来源的 `.html` 命名会误导下游
        #   （它按后缀猜内容形态）。
        if backend in (BACKEND_JINA, BACKEND_LOCAL):
            suffix = ".txt"
        elif content_type:
            # ★ 后端给的 Content-Type 比地址后缀准：官方 API 常常没有扩展名
            #   （`pypi.org/pypi/x/json`），按地址猜会冻成 .txt，下游就把它当纯文本读。
            suffix = suffix_for(content_type)
        else:
            suffix = _suffix_of(locator)
        kind = source_kind if source_kind is not None else trust_tier(locator, middle=self.trust)
        frozen = self.store.put(
            ref=ref,
            body=body,
            source_kind=kind,
            captured_at=self.captured_at,
            suffix=suffix,
        )
        if self.ledger is not None:
            self.ledger.remember(frozen)
        # ★ 后端 / 降级 / 信任层**只在这里**定下来 —— `put` 是内容寻址的，跟"怎么取回来的"无关
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
        ★ 一份材料都没拿到 ⇒ **具名弃权**（`ResearchUnavailable`），不返回空清单。
        """
        self._failures = []
        self._needs_login = []
        self._circuits = set()
        self._errors = {}
        self._opener = None

        order = self._backend_order(question.channels)
        self._order = order
        key = _cache_key(question, budget, order)
        if self.cache:
            replayed = self._cache().recall(key)
            if replayed is not None:
                return replayed

        terms = question_terms(question.question)
        frontier: list[tuple[str, int]] = [(seed, 0) for seed in question.seeds]
        seen: set[str] = set()
        kept: list[tuple[str, Snapshot]] = []
        total_bytes = 0

        # ★ 失败尝试也要有界：N 个死种子 × 每条最多 2 后端 × 2 次超时 = 4 分钟 × N。
        #   预算按"成功保留几条"算，所以这里另给一个尝试上限（成功数的小倍数）。
        max_attempts = max(4, budget.max_sources * 6)  # blame: whitelist — 尝试上限 = 保留上限的 6 倍；再多就是病态种子清单
        attempts = 0
        while frontier and len(kept) < budget.max_sources and attempts < max_attempts:
            attempts += 1
            locator, depth = frontier.pop(0)
            if locator in seen:
                continue
            seen.add(locator)
            try:
                body, backend, degraded, links, content_type = self._fetch(locator)
            except ResearchUnavailable as exc:
                # ★ 单条取不到不拖垮整次检索；它只是**不进结果**，原因记进清单
                self._errors[locator] = str(exc)
                continue
            if not body:
                continue
            if total_bytes + len(body) > budget.max_bytes:
                break
            total_bytes += len(body)
            ref = f"{question.ref}:{len(kept) + 1}"
            kept.append(
                (
                    locator,
                    self._freeze(
                        ref, body, backend=backend, degraded=degraded, locator=locator, content_type=content_type
                    ),
                )
            )

            # ★★ 扩展：种子只是起点。深度没用完就继续往外挖。
            if depth >= budget.max_depth:
                continue
            candidates = [
                link
                for link in links
                if link not in seen
                and (_is_public_locator(link) or _same_host(link, locator))
            ]
            ranked = sorted(
                candidates,
                key=lambda link: (-relevance(link, terms), link),
                # blame: whitelist — 全 0 也要按地址序抓：种子页里挖到的链接本身就是"相关"的
                #   （它出现在这个页面上），不因为"地址里没命中问题词"就一条都不扩
            )
            room = max(0, budget.max_sources - len(kept) - len(frontier))
            for link in ranked[:room]:
                frontier.append((link, depth + 1))

        if not kept:
            # ★★ 红线：拿不到依据 ⇒ 具名弃权，不产出。
            attempted = sorted(self._errors) or ["<no seed and no backend to try>"]
            detail = "; ".join(  # blame: fail-closed — 缺理由就写明 not attempted，不假装成功
                f"{locator}: {self._errors.get(locator, 'not attempted')}" for locator in attempted
            )
            if self._needs_login:
                pairs = ", ".join(f"{locator} ({backend})" for locator, backend in self._needs_login)
                raise ResearchNeedsAuthorization(
                    f"research question {question.question!r} produced no evidence; "
                    f"login required for: {pairs}; attempted: {detail}"
                )
            raise ResearchUnavailable(
                f"research question {question.question!r} produced no evidence -- {detail}"
            )

        snapshots = (self._manifest(question, kept),) + tuple(snap for _loc, snap in kept)
        if self.cache:
            self._cache().remember(key, snapshots)
        return snapshots

    @property
    def last_errors(self) -> dict[str, str]:
        """上一次 `research()` 里**没取到的**地址 → 原因（runner 把它写进 SOURCES.md）。"""
        return dict(self._errors)

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
        lines.append("| ref | 信任层 | 走的后端 | 降级 | 地址 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for locator, snap in kept:
            lines.append(
                f"| {snap.ref} | {snap.source_kind or '?'} | {snap.backend} | "
                f"{'是' if snap.degraded else '否'} | {locator} |"
            )
        lines.append("")
        lines.append("信任层：T1 官方文档/API · T2 论文/官方博客 · T3 社区 · T4 不知名博客/聚合站（只当线索）。")
        lines.append("")
        if any(snap.degraded for _loc, snap in kept):
            lines.append("★ 标了「降级=是」的材料是走备选后端取回来的，**完整度可能打折**，引用时留个心眼。")
            lines.append("")
        if self._needs_login:
            lines.append("## 需要登录态（没有自己去抓）")
            lines.append("")
            for locator, backend in self._needs_login:
                lines.append(f"- `{locator}`（{backend}）")
            lines.append("")
        if self._errors:
            lines.append("## 没取到的（具名，不是空内容）")
            lines.append("")
            for locator in sorted(self._errors):
                lines.append(f"- `{locator}`: {self._errors[locator][:240]}")
            lines.append("")
        body = ("\n".join(lines) + "\n").encode("utf-8")
        return self._freeze(
            question.ref,
            body,
            backend=BACKEND_MANIFEST,
            degraded=False,
            locator=question.ref,
            source_kind=self.source_kind,
        )
