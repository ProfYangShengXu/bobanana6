"""``SourcePort`` implementations.

Two rules from ``AGENTS.md §3`` are load-bearing here:

  * **The proxy configuration is read from the environment, never cleared.**
    Some environments require a proxy to reach anything at all, so a fetcher
    that "helpfully" strips ``HTTP_PROXY`` is a fetcher that cannot run there.
    ``urllib.request.build_opener()`` picks the proxy up by itself; we only
    surface what it read so the choice can be observed and tested.
  * **A failed fetch raises; it never returns an empty snapshot.** A blank
    snapshot is indistinguishable from "the source says nothing", which turns a
    network failure into an apparent data problem (§7⑤ of AGENTS is the same
    lesson in the token dimension).
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.model import Snapshot, SourceRef
from .store import SnapshotStore, SourceError, SourceLedger

__all__ = ["PROXY_ENV_VARS", "FileFetcher", "HttpFetcher", "suffix_for"]

#: Every spelling the standard proxy machinery honours, in both cases.
PROXY_ENV_VARS: tuple[str, ...] = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)

_SUFFIXES = (
    ("text/csv", ".csv"),
    ("application/json", ".json"),
    ("text/markdown", ".md"),
    ("text/html", ".html"),
    ("text/plain", ".txt"),
)


def suffix_for(content_type: str) -> str:
    for prefix, suffix in _SUFFIXES:
        if content_type.startswith(prefix):
            return suffix
    return ".bin"


@dataclass(frozen=True)
class FileFetcher:
    """Offline source: ``locator`` is a path on disk. No network involved."""

    store: SnapshotStore
    captured_at: str
    source_kind: str = "file"
    #: ★ 「这个来源抓过了」的台账。给了它，第二次跑就复用那份冻结件，不再读一遍。
    #: `None`（缺省）⇒ 每次读源 —— 与加台账之前**逐字相同**，既有读数不受影响。
    # blame: fail-closed — 没有台账 ⇒ 一律重读（多读一次总比复用错东西安全）
    ledger: SourceLedger | None = None

    def fetch(self, ref: SourceRef) -> Snapshot:
        if self.ledger is not None:
            frozen = self.ledger.recall(ref.ref)
            if frozen is not None:
                return frozen
        locator = ref.locator or ref.ref
        path = Path(locator)
        try:
            body = path.read_bytes()
        except OSError as exc:
            raise SourceError(f"source {ref.ref!r}: cannot read {path}: {exc}") from exc
        snapshot = self.store.put(
            ref=ref.ref,
            body=body,
            source_kind=self.source_kind,
            captured_at=self.captured_at,
            suffix=path.suffix or ".bin",
        )
        if self.ledger is not None:
            self.ledger.remember(snapshot)
        return snapshot


@dataclass(frozen=True)
class HttpFetcher:
    """Network source. The proxy comes from ``env``; this class never edits it."""

    store: SnapshotStore
    captured_at: str
    timeout_s: float = 30.0
    source_kind: str = "http"
    #: Injected for tests; ``None`` means "let urllib build the real opener",
    #: which is also what makes the environment proxy take effect.
    opener: Any = None
    headers: dict[str, str] = field(default_factory=lambda: {"User-Agent": "bobanana6/0.1"})
    #: ★ 同 `FileFetcher.ledger` —— 来源抓过了就不打第二次网络。
    #: 对网页来源这一条尤其要紧：内容会变，重复抓会拿到**不一样的材料** ⇒ 产物不可复现。
    # blame: fail-closed — 没有台账 ⇒ 一律打网络（多打一次，好过复用错东西）
    ledger: SourceLedger | None = None

    def proxies(self) -> dict[str, str]:
        """What the environment says, without interpretation (observability hook)."""
        return dict(urllib.request.getproxies())

    def fetch(self, ref: SourceRef) -> Snapshot:
        if self.ledger is not None:
            frozen = self.ledger.recall(ref.ref)
            if frozen is not None:
                return frozen
        locator = ref.locator or ref.ref
        request = urllib.request.Request(locator, headers=dict(self.headers))
        opener = self.opener if self.opener is not None else urllib.request.build_opener()
        try:
            response = opener.open(request, timeout=self.timeout_s)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # ★ Explicit failure. The caller must decide what to do about it.
            raise SourceError(f"source {ref.ref!r}: fetch of {locator!r} failed: {exc}") from exc
        with response:
            status = int(getattr(response, "status", 200))
            content_type = str(response.headers.get("Content-Type", ""))
            body = response.read()
        if status != 200:
            raise SourceError(f"source {ref.ref!r}: fetch of {locator!r} returned HTTP {status}")
        snapshot = self.store.put(
            ref=ref.ref,
            body=body,
            source_kind=self.source_kind,
            captured_at=self.captured_at,
            suffix=suffix_for(content_type),
        )
        if self.ledger is not None:
            self.ledger.remember(snapshot)
        return snapshot


@dataclass(frozen=True)
class LocatorFetcher:
    """Pick the transport from the locator's scheme.

    This is the assembly-point-independent default: a locator that names a path
    is read from disk, an ``http(s)`` locator goes to the network. Choosing a
    transport is wiring, not judgment, so it lives with the adapters rather than
    inside the CLI.
    """

    store: SnapshotStore
    captured_at: str
    timeout_s: float = 30.0
    opener: Any = None
    #: ★ 一路透传给下面两个取数器 —— 台账是**取数层**的账，
    #: 在哪儿选传输（locator 的 scheme）跟"这个来源抓过没有"无关，所以由它带过去。
    # blame: fail-closed — 缺省 None ⇒ 两个子取数器各自回到"每次都抓"的老行为
    ledger: SourceLedger | None = None

    def fetch(self, ref: SourceRef) -> Snapshot:
        locator = ref.locator or ref.ref
        if locator.startswith(("http://", "https://")):
            return HttpFetcher(
                store=self.store,
                captured_at=self.captured_at,
                timeout_s=self.timeout_s,
                opener=self.opener,
                ledger=self.ledger,
            ).fetch(ref)
        if locator.startswith("file://"):
            return FileFetcher(store=self.store, captured_at=self.captured_at, ledger=self.ledger).fetch(
                SourceRef(ref=ref.ref, locator=locator[len("file://") :])
            )
        return FileFetcher(store=self.store, captured_at=self.captured_at, ledger=self.ledger).fetch(ref)
