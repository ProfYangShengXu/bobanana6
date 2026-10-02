"""adapters/: protocol translation only -- LLM settings, parsing, and the offline stand-ins."""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from typing import Any, Self

import pytest

from bobanana6.adapters import (
    DraftingOntology,
    EchoToolResultClient,
    FileOntology,
    LLMConfigError,
    LLMConnectionError,
    LLMError,
    LLMSettings,
    OntologyDraftError,
    OpenAICompatibleClient,
    ScriptedClient,
)
from bobanana6.agentloop.errors import BobananaAction, classify_error
from bobanana6.core.model import InputSurvey, SourceRef, TaskContract

DRAFT = json.dumps(
    {
        "domain": "credit-risk",
        "version": "2026.1",
        "concepts": {"pd": {"label": "PD", "kind": "measure"}},
    }
)


class _FakeResponse:
    def __init__(self, payload: bytes, *, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def read(self) -> bytes:
        return self.payload

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _RecordingOpener:
    def __init__(self, response: _FakeResponse | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.requests: list[Any] = []

    def open(self, request: Any, timeout: float = 0.0) -> _FakeResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def _reply(text: str = "hello", *, tool_calls: list[dict[str, Any]] | None = None) -> bytes:
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return json.dumps({"choices": [{"message": message}], "usage": {"total_tokens": 7}}).encode()


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #


def test_settings_prefer_the_project_spellings() -> None:
    settings = LLMSettings.from_env(
        {"BOBANANA6_BASE_URL": "http://a.invalid/v1", "BOBANANA6_MODEL": "m", "OPENAI_BASE_URL": "http://b.invalid"}
    )
    assert settings.base_url == "http://a.invalid/v1"
    assert settings.model == "m"
    assert settings.endpoint == "http://a.invalid/v1/chat/completions"


def test_settings_fall_back_to_openai_spellings() -> None:
    settings = LLMSettings.from_env({"OPENAI_BASE_URL": "http://b.invalid/v1", "OPENAI_API_KEY": "k"})
    assert settings.base_url == "http://b.invalid/v1"
    assert settings.api_key == "k"


def test_settings_without_an_endpoint_refuse_to_guess() -> None:
    with pytest.raises(LLMConfigError, match="no endpoint configured"):
        LLMSettings.from_env({})


# --------------------------------------------------------------------------- #
# protocol translation
# --------------------------------------------------------------------------- #


def test_complete_sends_the_openai_shape_and_parses_the_reply() -> None:
    opener = _RecordingOpener(_FakeResponse(_reply("answer", tool_calls=[{"id": "c1", "type": "function"}])))
    client = OpenAICompatibleClient(LLMSettings(base_url="http://x.invalid/v1", model="m", api_key="k"), opener=opener)
    response = client.complete([{"role": "user", "content": "hi"}], [{"type": "function"}], 128, temperature=0.0)

    assert response.text == "answer"
    assert response.tool_calls == ({"id": "c1", "type": "function"},)
    assert response.usage == {"total_tokens": 7}

    request = opener.requests[0]
    payload = json.loads(request.data.decode())
    assert payload["model"] == "m"
    assert payload["max_tokens"] == 128
    assert payload["tools"] == [{"type": "function"}]
    assert payload["temperature"] == 0.0
    assert request.get_header("Authorization") == "Bearer k"


def test_unknown_kw_is_not_forwarded() -> None:
    opener = _RecordingOpener(_FakeResponse(_reply()))
    client = OpenAICompatibleClient(LLMSettings(base_url="http://x.invalid/v1", model="m"), opener=opener)
    client.complete([], [], 1, nonsense="drop me")
    assert "nonsense" not in json.loads(opener.requests[0].data.decode())


def test_http_error_keeps_its_status_for_the_classifier() -> None:
    opener = _RecordingOpener(error=urllib.error.HTTPError("u", 429, "too many requests", {}, None))
    client = OpenAICompatibleClient(LLMSettings(base_url="http://x.invalid/v1", model="m"), opener=opener)
    with pytest.raises(LLMError) as error:
        client.complete([], [], 1)
    assert error.value.status_code == 429
    # ★ the status survives into agentloop's decision layer
    assert classify_error(error.value).reason == "rate_limit"
    assert classify_error(error.value).action is BobananaAction.RETRY_LOCAL


def test_transport_failure_names_the_connection() -> None:
    opener = _RecordingOpener(error=urllib.error.URLError("no route to host"))
    client = OpenAICompatibleClient(LLMSettings(base_url="http://x.invalid/v1", model="m"), opener=opener)
    with pytest.raises(LLMConnectionError):
        client.complete([], [], 1)
    assert classify_error(LLMConnectionError("boom")).retryable is True


def test_non_200_without_an_exception_is_still_an_error() -> None:
    opener = _RecordingOpener(_FakeResponse(_reply(), status=503))
    client = OpenAICompatibleClient(LLMSettings(base_url="http://x.invalid/v1", model="m"), opener=opener)
    with pytest.raises(LLMError, match="HTTP 503"):
        client.complete([], [], 1)


def test_a_reply_without_choices_is_refused() -> None:
    opener = _RecordingOpener(_FakeResponse(b'{"choices": []}'))
    client = OpenAICompatibleClient(LLMSettings(base_url="http://x.invalid/v1", model="m"), opener=opener)
    with pytest.raises(LLMError, match="no choices"):
        client.complete([], [], 1)


# --------------------------------------------------------------------------- #
# offline stand-ins
# --------------------------------------------------------------------------- #


def test_scripted_client_repeats_the_last_response() -> None:
    client = ScriptedClient([{"text": "one"}, {"text": "two"}])
    assert [client.complete([], [], 1).text for _ in range(3)] == ["one", "two", "two"]


def test_scripted_client_from_file(tmp_path: Path) -> None:
    path = tmp_path / "script.json"
    path.write_text(json.dumps([{"text": "from file"}]), encoding="utf-8")
    assert ScriptedClient.from_file(path).complete([], [], 1).text == "from file"


def test_echo_client_asks_for_the_snapshot_then_answers_with_it() -> None:
    client = EchoToolResultClient()
    tools = [{"type": "function", "function": {"name": "sources.snapshot"}}]
    first = client.complete([], tools, 10)
    assert first.tool_calls[0]["function"]["name"] == "sources.snapshot"
    assert first.tool_calls[0]["function"]["arguments"] == "{}"

    messages = [{"role": "tool", "tool_call_id": "offline-1", "content": '{"rows": [{"a": 1}]}'}]
    second = client.complete(messages, tools, 10)
    assert json.loads(second.text) == {"rows": [{"a": 1}]}


# --------------------------------------------------------------------------- #
# OntologyPort
# --------------------------------------------------------------------------- #


def _contract() -> TaskContract:
    return TaskContract(task_id="t", goal="build the panel")


def test_file_ontology_reads_the_file(tmp_path: Path) -> None:
    path = tmp_path / "credit.yaml"
    path.write_text("domain: d\nversion: '1'\nconcepts:\n  a: {label: A, kind: measure}\n", encoding="utf-8")
    ontology = FileOntology(path).obtain(_contract(), InputSurvey())
    assert ontology.domain == "d"
    assert ontology.is_frozen is False  # freezing is the caller's explicit step


def test_drafting_ontology_returns_a_validated_candidate() -> None:
    ontology = DraftingOntology(client=ScriptedClient([{"text": DRAFT}])).obtain(_contract(), InputSurvey())
    assert ontology.concepts["pd"].label == "PD"
    assert ontology.is_frozen is False


def test_drafting_ontology_refuses_an_unreadable_draft() -> None:
    client = ScriptedClient([{"text": "I think the concepts should probably be about credit."}])
    with pytest.raises(OntologyDraftError, match="not a valid ontology document"):
        DraftingOntology(client=client).obtain(_contract(), InputSurvey())


def test_drafting_ontology_refuses_an_empty_draft() -> None:
    with pytest.raises(OntologyDraftError, match="empty"):
        DraftingOntology(client=ScriptedClient([{"text": "  "}])).obtain(_contract(), InputSurvey())


def test_drafting_ontology_sends_the_contract_and_the_survey() -> None:
    sent: list[list[dict[str, Any]]] = []

    class _Capture(ScriptedClient):
        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            sent.append(messages)
            return super().complete(messages, tools, max_tokens, **kw)

    client = _Capture([{"text": DRAFT}])
    contract = TaskContract(task_id="t", goal="g", sources=(SourceRef(ref="src-1"),))
    DraftingOntology(client=client).obtain(contract, InputSurvey(source_refs=("src-1",)))
    user = json.loads(sent[0][1]["content"])
    assert user["goal"] == "g"
    assert user["sources"] == ["src-1"]
    assert user["input_sources"] == ["src-1"]
