"""sources/: content-addressed snapshots, env-proxy respect, explicit fetch failure."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Self

import pytest

from bobanana6.core.model import SourceRef
from bobanana6.sources import (
    FileFetcher,
    HttpFetcher,
    SnapshotFileReader,
    SnapshotStore,
    SourceError,
    SourceForm,
    SourceFormError,
    decode_body,
)

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


def test_the_reader_hands_the_frozen_rows_over_as_data(tmp_path: Path) -> None:
    """★ A11：端口定义在稳定侧、实作在外侧 —— 这里验外侧真的把行读成了 core 的数据。"""
    store = SnapshotStore(root=tmp_path / "snaps")
    snapshot = store.put(
        ref="panel-a",
        body=b"obligor,amount\nA-001,1200000.0\nA-002,800000.0\n",
        source_kind="csv",
        captured_at=NOW,
        suffix=".csv",
    )
    body = SnapshotFileReader(store=store).body(snapshot)
    assert body.ref == "panel-a"
    assert [column.name for column in body.columns] == ["obligor", "amount"]
    assert body.rows[1]["obligor"] == "A-002"
    assert body.row(2) is None


def test_the_reader_refuses_to_pretend_a_missing_file_has_rows(tmp_path: Path) -> None:
    """读不回来就是错，不是「退化成一个空快照」—— 空快照会让内容级校验说谎。"""
    store = SnapshotStore(root=tmp_path / "snaps")
    snapshot = store.put(ref="panel-a", body=b"a\n1\n", source_kind="csv", captured_at=NOW, suffix=".csv")
    snapshot.body_path.unlink()
    with pytest.raises(SourceError):
        SnapshotFileReader(store=store).body(snapshot)


# --------------------------------------------------------------------------- #
# ★ #32：源载体的【形式】—— 派活前按任务声明规整（机制层，不许出现领域词）
# --------------------------------------------------------------------------- #


def test_a_declared_encoding_is_used_instead_of_guessed() -> None:
    """★ 声明了 gbk 就用 gbk：不再出现静默替换出来的 U+FFFD。"""
    body = "def f():\r\n    # 归一\r\n".encode("gbk")
    decoded = decode_body(body, SourceForm(encoding="gbk"))

    assert decoded.text.startswith("def f():")
    assert "归一" in decoded.text
    assert "\ufffd" not in decoded.text
    assert decoded.note["encoding"] == "gbk"
    assert decoded.note["assumed"] is False


def test_an_undeclared_carrier_is_guessed_but_says_so() -> None:
    """★ 没声明就按 utf-8 猜，但**把假设说出来**（#18 的「不许沉默」）。"""
    decoded = decode_body("café".encode("latin-1"), SourceForm())

    assert decoded.text == "caf\ufffd"
    assert decoded.note["assumed"] is True
    assert decoded.note["replacement_chars"] == 1


def test_a_clean_undeclared_carrier_is_delivered_verbatim() -> None:
    """★★ 反向的单元版：没有声明、字节干净 ⇒ 逐字一样，而且**不带任何说明**。"""
    decoded = decode_body(b"a\nb\n", SourceForm())

    assert decoded.text == "a\nb\n"
    assert decoded.note == {}


def test_a_declared_encoding_that_does_not_match_is_a_named_error() -> None:
    """声明错了要报具名错（重试无用），不是静默替换。"""
    with pytest.raises(SourceFormError, match="declared as 'gbk'"):
        decode_body("中文".encode(), SourceForm(encoding="gbk"))

    assert SourceFormError.reason == "fix_constraint"


def test_a_declared_eol_is_normalized_both_ways() -> None:
    assert decode_body(b"a\r\nb\r\n", SourceForm(eol="lf")).text == "a\nb\n"
    assert decode_body(b"a\nb\n", SourceForm(eol="crlf")).text == "a\r\nb\r\n"
    assert decode_body(b"a\r\nb\r\n", SourceForm(eol="preserve")).text == "a\r\nb\r\n"


def test_an_unknown_form_is_refused_at_declaration_time() -> None:
    with pytest.raises(ValueError, match="unknown encoding"):
        SourceForm(encoding="not-a-codec")
    with pytest.raises(ValueError, match="unknown eol"):
        SourceForm(eol="cr")


def test_the_reader_normalizes_the_carrier_before_it_becomes_rows(tmp_path: Path) -> None:
    """A11 的读者走同一条规整（两处解码点，一套动作）。"""
    store = SnapshotStore(root=tmp_path / "s")
    snapshot = store.put(
        ref="src-1",
        body="a,b\n归一,2\n".encode("gbk"),
        source_kind="file",
        captured_at="2026-01-01T00:00:00",
        suffix=".csv",
    )
    reader = SnapshotFileReader(store=store, forms={"src-1": SourceForm(encoding="gbk")})

    body = reader.body(snapshot)
    assert body.rows == ({"a": "归一", "b": "2"},)
