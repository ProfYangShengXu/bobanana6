"""adapters/: protocol translation only -- LLM settings, parsing, and the offline stand-ins."""

from __future__ import annotations

import json
import re
import urllib.error
from pathlib import Path
from typing import Any, Self

import pytest

from bobanana6.adapters import (
    DEFAULT_TIMEOUT_S,
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
        self.timeouts: list[float] = []

    def open(self, request: Any, timeout: float = 0.0) -> _FakeResponse:
        self.requests.append(request)
        self.timeouts.append(timeout)
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


# --------------------------------------------------------------------------- #
# extra_body: the vendor-extension escape hatch
# --------------------------------------------------------------------------- #


def _settings(**kw: object) -> LLMSettings:
    base: dict[str, object] = {"base_url": "http://x.invalid/v1", "model": "m"}
    base.update(kw)
    return LLMSettings(**base)  # type: ignore[arg-type]


def test_extra_body_lands_in_the_payload() -> None:
    """★ Without this, a vendor-only switch (``enable_thinking``) is dropped silently."""
    opener = _RecordingOpener(_FakeResponse(_reply()))
    client = OpenAICompatibleClient(_settings(extra_body={"enable_thinking": False, "top_k": 20}), opener=opener)
    client.complete([{"role": "user", "content": "hi"}], [], 128)
    payload = json.loads(opener.requests[0].data.decode())
    assert payload["enable_thinking"] is False
    assert payload["top_k"] == 20
    assert payload["model"] == "m" and payload["messages"] == [{"role": "user", "content": "hi"}]


def test_extra_body_is_merged_last_so_it_can_override_max_tokens() -> None:
    opener = _RecordingOpener(_FakeResponse(_reply()))
    client = OpenAICompatibleClient(_settings(extra_body={"max_tokens": 7}), opener=opener)
    client.complete([], [], 128)
    payload = json.loads(opener.requests[0].data.decode())
    assert payload["max_tokens"] == 7


def test_extra_body_may_not_override_model() -> None:
    with pytest.raises(LLMConfigError, match=r"must not override \['model'\]"):
        _settings(extra_body={"model": "someone-elses-model"})


def test_extra_body_may_not_override_messages() -> None:
    with pytest.raises(LLMConfigError, match="call contract"):
        _settings(extra_body={"messages": []})


def test_extra_body_reads_a_json_object_from_the_environment() -> None:
    settings = LLMSettings.from_env(
        {"BOBANANA6_BASE_URL": "http://x.invalid/v1", "BOBANANA6_EXTRA_BODY": '{"enable_thinking": false}'}
    )
    assert settings.extra_body == {"enable_thinking": False}


def test_extra_body_defaults_to_empty() -> None:
    assert LLMSettings.from_env({"BOBANANA6_BASE_URL": "http://x.invalid/v1"}).extra_body == {}


def test_extra_body_refuses_malformed_json() -> None:
    with pytest.raises(LLMConfigError, match="must be a JSON object"):
        LLMSettings.from_env({"BOBANANA6_BASE_URL": "http://x.invalid/v1", "BOBANANA6_EXTRA_BODY": "{not json"})


def test_extra_body_refuses_a_non_object() -> None:
    with pytest.raises(LLMConfigError, match="got list"):
        LLMSettings.from_env({"BOBANANA6_BASE_URL": "http://x.invalid/v1", "BOBANANA6_EXTRA_BODY": "[1, 2]"})


# --------------------------------------------------------------------------- #
# timeout: configurable, default unchanged
# --------------------------------------------------------------------------- #


def test_timeout_default_is_unchanged() -> None:
    """★ 60s stays the default; a slow service must be fixed by configuration."""
    assert DEFAULT_TIMEOUT_S == 60.0
    assert LLMSettings.from_env({"BOBANANA6_BASE_URL": "http://x.invalid/v1"}).timeout_s == 60.0


def test_timeout_reaches_the_transport() -> None:
    """★ A cold start measured at 90.8s needs a timeout the caller can raise."""
    settings = LLMSettings.from_env({"BOBANANA6_BASE_URL": "http://x.invalid/v1", "BOBANANA6_TIMEOUT_S": "120"})
    assert settings.timeout_s == 120.0
    opener = _RecordingOpener(_FakeResponse(_reply()))
    OpenAICompatibleClient(settings, opener=opener).complete([], [], 1)
    assert opener.timeouts == [120.0]


def test_timeout_refuses_a_non_number() -> None:
    with pytest.raises(LLMConfigError, match="BOBANANA6_TIMEOUT_S"):
        LLMSettings.from_env({"BOBANANA6_BASE_URL": "http://x.invalid/v1", "BOBANANA6_TIMEOUT_S": "soon"})


def test_timeout_must_be_positive() -> None:
    with pytest.raises(LLMConfigError, match="timeout_s must be > 0"):
        _settings(timeout_s=0.0)


# --------------------------------------------------------------------------- #
# ★ 线格式工具名：严格端点只接受 [a-zA-Z0-9_-]+，而壳内部的工具名含点号
#   （这是**适配器层**的账：工具叫什么没错，错在发出去前没做合规转换）
# --------------------------------------------------------------------------- #


def _real_menu() -> list[dict[str, Any]]:
    """真实工具面：内部名字含点号，正是严格端点会 400 的那一份。"""
    from bobanana6.core.ports import _BASE_TOOL_SPECS

    return [
        {"type": "function", "function": {"name": spec.name, "description": spec.returns, "parameters": {}}}
        for spec in _BASE_TOOL_SPECS
    ]


def _strict_endpoint(client: OpenAICompatibleClient, tools: list[dict[str, Any]]) -> dict[str, Any]:
    """像一个严格端点那样校验 `function.name`（DeepSeek 官方的逐字约束）。"""
    opener = _RecordingOpener(_FakeResponse(_reply("ok")))
    client.opener = opener
    client.complete([{"role": "user", "content": "hi"}], tools, 64)
    payload = json.loads(opener.requests[0].data.decode())
    for tool in payload.get("tools", []):
        name = tool.get("function", {}).get("name", "")
        assert re.match(r"^[a-zA-Z0-9_-]+$", name), f"non-compliant function.name on the wire: {name!r}"
        assert len(name) <= 64, name
    return payload


def test_the_internal_menu_is_not_wire_compliant(tmp_path: Path) -> None:
    """★ 病根的读数：壳内部的工具名**本来就**过不了严格端点的 pattern。"""
    internal = [tool["function"]["name"] for tool in _real_menu()]
    assert any("." in name for name in internal), internal
    assert [name for name in internal if not re.match(r"^[a-zA-Z0-9_-]+$", name)]


def test_the_wire_menu_satisfies_the_strict_pattern() -> None:
    """★ 改后：送出去的每一个 `function.name` 都合规（`semantics.lookup` → `semantics_lookup`）。"""
    client = OpenAICompatibleClient(LLMSettings(base_url="http://x.invalid/v1", model="m"))
    payload = _strict_endpoint(client, _real_menu())
    names = [tool["function"]["name"] for tool in payload["tools"]]
    assert "semantics_lookup" in names and "semantics.lookup" not in names
    assert len(names) == len(set(names)), "线格式名字必须一一对应"


def test_a_compliant_menu_is_sent_byte_identically() -> None:
    """★ 反向：名字本来就合规 ⇒ 出站一个字节都不改（旧端点读数不受影响）。"""
    menu = [
        {"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "files_patch", "parameters": {}}},
    ]
    client = OpenAICompatibleClient(LLMSettings(base_url="http://x.invalid/v1", model="m"))
    opener = _RecordingOpener(_FakeResponse(_reply("ok")))
    client.opener = opener
    client.complete([], menu, 8)
    assert json.loads(opener.requests[0].data.decode())["tools"] == menu


def test_an_entry_without_a_function_object_is_left_alone() -> None:
    """菜单里没有 `function` 的条目原样放行（别为了翻译把形状改了）。"""
    client = OpenAICompatibleClient(LLMSettings(base_url="http://x.invalid/v1", model="m"))
    opener = _RecordingOpener(_FakeResponse(_reply("ok")))
    client.opener = opener
    client.complete([], [{"type": "function"}], 8)
    assert json.loads(opener.requests[0].data.decode())["tools"] == [{"type": "function"}]


def test_the_wire_name_comes_back_as_the_internal_name() -> None:
    """★ 入站：模型回 `semantics_lookup` ⇒ 壳拿到的必须是 `semantics.lookup`。

    否则注册表会把它当"未知工具"，而真正的原因在适配器层。
    """
    client = OpenAICompatibleClient(LLMSettings(base_url="http://x.invalid/v1", model="m"))
    reply = _reply(
        "",
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "semantics_lookup", "arguments": '{"term": "x"}'},
            }
        ],
    )
    opener = _RecordingOpener(_FakeResponse(reply))
    client.opener = opener
    response = client.complete([], _real_menu(), 64)
    call = response.tool_calls[0]
    assert call["function"]["name"] == "semantics.lookup"
    assert call["function"]["arguments"] == '{"term": "x"}' and call["id"] == "c1"


def test_a_name_the_shell_never_renamed_is_passed_through() -> None:
    """模型编的名字不做"猜"还原 —— 原样交给既有的 unknown tool 路径。"""
    client = OpenAICompatibleClient(LLMSettings(base_url="http://x.invalid/v1", model="m"))
    call = {"id": "c1", "type": "function", "function": {"name": "totally_made_up", "arguments": "{}"}}
    client.opener = _RecordingOpener(_FakeResponse(_reply("", tool_calls=[call])))
    response = client.complete([], _real_menu(), 64)
    assert response.tool_calls[0]["function"]["name"] == "totally_made_up"


def test_every_internal_tool_name_round_trips() -> None:
    """★ 一一对应且可逆：没有冲突、每个名字都还原得回来。"""
    from bobanana6.adapters.tool_names import ToolNameMap, is_wire_name

    internal = [spec.name for spec in __import__(
        "bobanana6.core.ports", fromlist=["_BASE_TOOL_SPECS"]
    )._BASE_TOOL_SPECS]
    names = ToolNameMap.of(internal)
    assert len(set(names.forward.values())) == len(internal)
    for name in internal:
        wire = names.wire(name)
        assert is_wire_name(wire), wire
        assert names.internal(wire) == name


def test_two_names_that_would_collide_are_refused_not_merged() -> None:
    """★ 冲突检测：`a.b` 与 `a_b` 清洗后撞车 ⇒ 显式报错，不许静默合并。"""
    from bobanana6.adapters.tool_names import ToolNameCollision, ToolNameMap

    with pytest.raises(ToolNameCollision, match="both become"):
        ToolNameMap.of(("a.b", "a_b"))
    # 同名的重复声明不是冲突（同一张表里同名只出现一次）
    assert ToolNameMap.of(("a.b", "a.b")).wire("a.b") == "a_b"


def test_the_forced_tool_choice_is_translated_too() -> None:
    """`tool_choice` 里也能点名一个函数 —— 同一个 pattern，同一张表。"""
    client = OpenAICompatibleClient(LLMSettings(base_url="http://x.invalid/v1", model="m"))
    opener = _RecordingOpener(_FakeResponse(_reply("ok")))
    client.opener = opener
    client.complete(
        [], _real_menu(), 8, tool_choice={"type": "function", "function": {"name": "semantics.lookup"}}
    )
    payload = json.loads(opener.requests[0].data.decode())
    assert payload["tool_choice"]["function"]["name"] == "semantics_lookup"


def test_the_response_schema_label_is_made_wire_compliant() -> None:
    """`response_format.json_schema.name` 受**同一个** pattern 管 —— 顺手在边界上收口。"""
    client = OpenAICompatibleClient(LLMSettings(base_url="http://x.invalid/v1", model="m"))
    opener = _RecordingOpener(_FakeResponse(_reply("ok")))
    client.opener = opener
    client.complete([], _real_menu(), 8, response_format={"type": "json_schema", "json_schema": {"name": "candidate"}})
    assert json.loads(opener.requests[0].data.decode())["response_format"]["json_schema"]["name"] == "candidate"

    from bobanana6.adapters.tool_names import ToolNameMap

    dotted = ToolNameMap.of(("semantics.lookup",)).wire_response_format(
        {"type": "json_schema", "json_schema": {"name": "candidate.v2", "schema": {}}}
    )
    assert dotted["json_schema"]["name"] == "candidate_v2"


# --------------------------------------------------------------------------- #
# ★ 出站消息也过边界：assistant 行的工具名换成线格式、壳的记账键不许上线
# --------------------------------------------------------------------------- #


def _transcript() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "protocol"},
        {"role": "user", "content": "brief"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call-1", "type": "function", "function": {"name": "semantics.lookup", "arguments": "{}"}},
                {"id": "call-2", "type": "function", "function": {"name": "sources.read", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "r1"},
        {"role": "tool", "tool_call_id": "call-2", "content": "r2"},
        {"role": "assistant", "content": "digest", "candidate": True},
    ]


def test_the_transcript_is_translated_and_internal_bookkeeping_is_dropped() -> None:
    """★ assistant 行的 `function.name` 与 `tools[]` 受**同一条** pattern 管。

    `candidate` 是壳自己的"这一行交付了答案"标记（`close_transcript_tail`），
    不是 chat schema 的字段 —— 边界上丢掉，壳内部照旧保留。
    """
    client = OpenAICompatibleClient(LLMSettings(base_url="http://x.invalid/v1", model="m"))
    opener = _RecordingOpener(_FakeResponse(_reply("ok")))
    client.opener = opener
    client.complete(_transcript(), _real_menu(), 8)

    sent = json.loads(opener.requests[0].data.decode())["messages"]
    calls = sent[2]["tool_calls"]
    assert [call["function"]["name"] for call in calls] == ["semantics_lookup", "sources_read"]
    for call in calls:
        assert re.match(r"^[a-zA-Z0-9_-]+$", call["function"]["name"])
    assert "candidate" not in sent[-1]
    assert sent[-1] == {"role": "assistant", "content": "digest"}
    # 壳内部不变：调用方手里的 transcript 一个字符没动（翻译只发生在出站）
    assert _transcript()[2]["tool_calls"][0]["function"]["name"] == "semantics.lookup"


def test_the_wire_messages_satisfy_the_sequence_the_endpoints_enforce() -> None:
    """★ 请求里的消息序列本身就是合规的（不只是名字）。"""
    from bobanana6.agentloop.compress import message_sequence_problem

    client = OpenAICompatibleClient(LLMSettings(base_url="http://x.invalid/v1", model="m"))
    opener = _RecordingOpener(_FakeResponse(_reply("ok")))
    client.opener = opener
    client.complete(_transcript(), _real_menu(), 8)

    sent = json.loads(opener.requests[0].data.decode())["messages"]
    assert message_sequence_problem(sent) == ""
    # 反向：把带 tool_calls 的那条 assistant 行抽掉 ⇒ 同一个检查报出那条 400 的原因
    without = [row for row in sent if not (row.get("role") == "assistant" and row.get("tool_calls"))]
    assert "no preceding assistant row" in message_sequence_problem(without)


def test_a_transcript_message_without_tool_calls_keeps_its_shape() -> None:
    """普通消息原样过 —— 只有**壳自己的记账键**在边界上掉下去。

    `pinned` 是压缩器的"把这一行钉在末尾"标记（`compress._pin_instruction`），
    不是 chat schema 的字段；严格端点有权拒收未知字段，所以与 `candidate` 一样丢掉。
    壳内部照旧看得见它（压缩行为不变）。
    """
    client = OpenAICompatibleClient(LLMSettings(base_url="http://x.invalid/v1", model="m"))
    opener = _RecordingOpener(_FakeResponse(_reply("ok")))
    client.opener = opener
    client.complete([{"role": "user", "content": "hi", "pinned": True}], [], 8)
    assert json.loads(opener.requests[0].data.decode())["messages"] == [{"role": "user", "content": "hi"}]


# --------------------------------------------------------------------------- #
# ★ 思考模型：端点给的 reasoning 必须原样带回；没给的端点不许凭空造一个
# --------------------------------------------------------------------------- #


def _reasoning_reply(text: str = "", *, reasoning: str = "let me think") -> bytes:
    message: dict[str, Any] = {"role": "assistant", "content": text, "reasoning_content": reasoning}
    return json.dumps({"choices": [{"message": message}], "usage": {"total_tokens": 7}}).encode()


def test_the_endpoints_reasoning_field_is_parsed_into_the_response() -> None:
    """★ 响应里给了 ⇒ 取出来（壳不解释它，只是原样拿着）。"""
    client = OpenAICompatibleClient(LLMSettings(base_url="http://x.invalid/v1", model="m"))
    client.opener = _RecordingOpener(_FakeResponse(_reasoning_reply("hi")))
    response = client.complete([{"role": "user", "content": "x"}], [], 8)
    assert response.carry_back == {"reasoning_content": "let me think"}


def test_an_endpoint_that_gives_no_reasoning_yields_an_empty_carry_back() -> None:
    """★ 反向：没给就是空 —— 不许凭空造一个空值（宽松端点的报文必须一字不变）。"""
    client = OpenAICompatibleClient(LLMSettings(base_url="http://x.invalid/v1", model="m"))
    client.opener = _RecordingOpener(_FakeResponse(_reply("hi")))
    response = client.complete([{"role": "user", "content": "x"}], [], 8)
    assert dict(response.carry_back) == {}


def test_the_assistant_row_carries_the_reasoning_back_byte_for_byte() -> None:
    """★ 往返：att+tool_calls+reasoning 再发出去，`reasoning_content` 一字不差。"""
    from bobanana6.agentloop.loop import TurnProposal, assistant_tool_row
    from bobanana6.agentloop.tools import ToolCall

    proposal = TurnProposal(
        tool_calls=(ToolCall(call_id="call-1", name="semantics.lookup", arguments={}),),
        text="",
        carry_back={"reasoning_content": "let me think"},
    )
    row = assistant_tool_row(proposal)
    assert row["reasoning_content"] == "let me think"

    client = OpenAICompatibleClient(LLMSettings(base_url="http://x.invalid/v1", model="m"))
    opener = _RecordingOpener(_FakeResponse(_reply("ok")))
    client.opener = opener
    client.complete(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}, row,
         {"role": "tool", "tool_call_id": "call-1", "content": "r"}],
        _real_menu(),
        8,
    )
    sent = json.loads(opener.requests[0].data.decode())["messages"]
    assert sent[2]["reasoning_content"] == "let me think"
    assert sent[2]["tool_calls"][0]["function"]["name"] == "semantics_lookup"


def test_a_row_without_carry_back_gets_no_reasoning_key_at_all() -> None:
    """★ 反向的第二半：没有内容就**不出这个键**（`""` / `null` 都不行）。"""
    from bobanana6.agentloop.loop import TurnProposal, assistant_tool_row
    from bobanana6.agentloop.tools import ToolCall

    row = assistant_tool_row(TurnProposal(tool_calls=(ToolCall(call_id="c1", name="sources.read", arguments={}),)))
    assert "reasoning_content" not in row
    assert set(row) == {"role", "content", "tool_calls"}
