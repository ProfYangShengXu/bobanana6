"""sources/: content-addressed snapshots, env-proxy respect, explicit fetch failure."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Self

import pytest

from bobanana6.core.model import SourceRef
from bobanana6.sources import FileFetcher, HttpFetcher, SnapshotStore, SourceError

NOW = "2026-10-03T00:00:00+08:00"


class _FakeResponse:
    def __init__(self, body: bytes = b"ok", *, status: int = 200, content_type: str = "text/csv") -> None:
        self.body = body
        self.status = status
        self.headers = {"Content-Type": content_type}

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeOpener:
    """Stands in for ``OpenerDirector`` so no socket is opened."""

    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.requests: list[str] = []

    def open(self, request: object, timeout: float = 0.0) -> _FakeResponse:
        self.requests.append(str(getattr(request, "full_url", request)))
        return self.response



@pytest.fixture
def store(tmp_path: Path) -> SnapshotStore:
    return SnapshotStore(root=tmp_path / "snapshots")


# --------------------------------------------------------------------------- #
# store: content addressing
# --------------------------------------------------------------------------- #


def test_same_bytes_twice_produce_one_file(store: SnapshotStore) -> None:
    first = store.put(ref="a", body=b"x,y\n1,2\n", source_kind="file", captured_at=NOW, suffix=".csv")
    second = store.put(ref="b", body=b"x,y\n1,2\n", source_kind="file", captured_at=NOW, suffix=".csv")
    assert first.body_path == second.body_path
    assert len(list(store.root.iterdir())) == 1
    assert first.ref != second.ref  # the ref names the source, not the bytes


def test_different_bytes_get_different_files(store: SnapshotStore) -> None:
    first = store.put(ref="a", body=b"one", source_kind="file", captured_at=NOW)
    second = store.put(ref="a", body=b"two", source_kind="file", captured_at=NOW)
    assert first.body_path != second.body_path


def test_empty_body_is_refused_not_frozen(store: SnapshotStore) -> None:
    """★ A blank snapshot makes 'fetch failed' look like 'the source is empty'."""
    with pytest.raises(SourceError, match="empty snapshot"):
        store.put(ref="a", body=b"", source_kind="file", captured_at=NOW)
    assert not store.root.exists()


def test_non_iso_captured_at_is_refused(store: SnapshotStore) -> None:
    with pytest.raises(SourceError, match="ISO-8601"):
        store.put(ref="a", body=b"x", source_kind="file", captured_at="yesterday")


def test_verify_rehashes_the_frozen_carrier(store: SnapshotStore) -> None:
    snapshot = store.put(ref="a", body=b"payload", source_kind="file", captured_at=NOW)
    assert store.verify(snapshot) is True
    snapshot.body_path.write_bytes(b"tampered")
    assert store.verify(snapshot) is False


# --------------------------------------------------------------------------- #
# FileFetcher (offline)
# --------------------------------------------------------------------------- #


def test_file_fetcher_freezes_the_file(tmp_path: Path, store: SnapshotStore) -> None:
    body = tmp_path / "input.csv"
    body.write_text("id,amount\n1,10\n", encoding="utf-8")
    snapshot = FileFetcher(store=store, captured_at=NOW).fetch(SourceRef(ref="src-1", locator=str(body)))
    assert store.read(snapshot) == b"id,amount\n1,10\n"
    assert snapshot.source_kind == "file"
    assert snapshot.captured_at == NOW
    assert snapshot.body_path.suffix == ".csv"


def test_file_fetcher_missing_file_is_an_explicit_error(tmp_path: Path, store: SnapshotStore) -> None:
    with pytest.raises(SourceError, match="cannot read"):
        FileFetcher(store=store, captured_at=NOW).fetch(SourceRef(ref="src-1", locator=str(tmp_path / "nope.csv")))


def test_file_fetcher_falls_back_to_the_ref_as_locator(tmp_path: Path, store: SnapshotStore) -> None:
    body = tmp_path / "src-1"
    body.write_text("v\n", encoding="utf-8")
    snapshot = FileFetcher(store=store, captured_at=NOW).fetch(SourceRef(ref=str(body)))
    assert store.read(snapshot) == b"v\n"


# --------------------------------------------------------------------------- #
# HttpFetcher: proxy from env, failure is explicit
# --------------------------------------------------------------------------- #


def test_http_fetcher_reads_the_environment_proxy(monkeypatch: pytest.MonkeyPatch, store: SnapshotStore) -> None:
    """★ Some environments cannot reach anything without a proxy."""
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    fetcher = HttpFetcher(store=store, captured_at=NOW)
    proxies = fetcher.proxies()
    assert proxies.get("http") == "http://proxy.invalid:3128"
    assert proxies.get("https") == "http://proxy.invalid:3128"


def test_fetch_does_not_clear_the_proxy_environment(monkeypatch: pytest.MonkeyPatch, store: SnapshotStore) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")

    snapshot = HttpFetcher(store=store, captured_at=NOW, opener=_FakeOpener(_FakeResponse())).fetch(
        SourceRef(ref="src-1", locator="http://example.invalid/data.csv")
    )
    assert store.read(snapshot) == b"ok"
    assert snapshot.body_path.suffix == ".csv"  # from Content-Type
    assert os.environ["HTTP_PROXY"] == "http://proxy.invalid:3128"  # ★ never cleared


def test_http_failure_raises_instead_of_returning_an_empty_snapshot(store: SnapshotStore) -> None:
    """★ A closed port: no server, no snapshot, a named error."""
    fetcher = HttpFetcher(store=store, captured_at=NOW, timeout_s=2.0)
    with pytest.raises(SourceError, match="failed"):
        fetcher.fetch(SourceRef(ref="src-1", locator="http://127.0.0.1:1/nothing.csv"))
    assert not store.root.exists()


def test_http_non_200_is_an_error(store: SnapshotStore) -> None:
    opener = _FakeOpener(_FakeResponse(b"upstream down", status=503, content_type="text/plain"))
    with pytest.raises(SourceError, match="HTTP 503"):
        HttpFetcher(store=store, captured_at=NOW, opener=opener).fetch(
            SourceRef(ref="src-1", locator="http://example.invalid/x")
        )
