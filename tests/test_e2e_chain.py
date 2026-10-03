"""The whole chain over real HTTP, against a local stand-in for an OpenAI-compatible service.

Why a local server instead of a stub client: the thing under test is the *chain*
-- CLI -> adapter -> urllib -> service -> producer -> classifier -> feedback ->
loop -> judge -> assembly -- and a stub client would step over the transport, the
error body, and the capability negotiation that only exist on the wire.

The stand-in is **feedback-driven**, which is the whole point: it answers prose
*unless* the transcript contains the fed-back fix. That is the measured 4B
behaviour -- the model can produce the object once it is told what was wrong -- and
it is the only version of the story in which layer 3 is load-bearing.

  * offers ``response_format`` -> 400, like a service that does not implement it;
  * transcript carries the fix ("required shape") -> valid JSON;
  * otherwise -> prose.

Nothing here is a model: it is a scripted service. What it proves is that the three
layers exist, that the fix actually reaches the model, and that a refusal degrades
instead of failing.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from bobanana6 import cli

ONTOLOGY = """
domain: credit-risk
version: "2026.1"
concepts:
  obligor: {label: Obligor, kind: entity}
  pd: {label: PD, kind: measure, unit: ratio}
  amount: {label: Amount, kind: measure, unit: CNY}
units:
  pd: {symbol: ratio, scale: 1.0, base: ratio}
  amount: {symbol: CNY, scale: 1.0, base: CNY}
"""

TASK = """
task_id: t-chain
goal: build the panel
deliverables:
  - {name: out.csv, schema: {obligor: str, pd: float, amount: float}}
sources:
  - {ref: panel-2026q1, locator: CSV_PATH}
constraints:
  hard:
    - {id: C-rows, kind: presence, target: "*", spec: {min_rows: 1}, rationale: "no empty panels"}
    - {id: C-prov, kind: provenance, target: out.csv, spec: {min_coverage: 0.95}, rationale: "traceable values"}
"""


class _Service(BaseHTTPRequestHandler):
    """Scripted answers, plus a record of what was asked."""

    requests: ClassVar[list[dict]] = []
    answers: ClassVar[int] = 0

    def log_message(self, *args: object) -> None:  # silence the test output
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).requests.append(body)

        if "response_format" in body:
            payload = {"error": {"message": "response_format is not supported by this model", "type": "invalid_request_error"}}
            self._send(400, payload)
            return

        type(self).answers += 1
        if "required shape" not in _transcript(body["messages"]):
            # The measured failure: the model answers prose on its own.
            self._send(200, _reply("I think the panel is probably fine, but I am not sure."))
            return
        ref = _authorized_source(body["messages"]) or "panel-2026q1"
        self._send(
            200,
            _reply(
                json.dumps(
                    {
                        "ref": ref,
                        "columns": [
                            {"name": "obligor", "dtype": "str"},
                            {"name": "pd", "dtype": "str"},
                            {"name": "amount", "dtype": "str"},
                        ],
                        "rows": [{"obligor": "A-1", "pd": "0.01", "amount": "100"}],
                    }
                )
            ),
        )

    def _send(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _reply(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}], "usage": {"total_tokens": 9}}


def _transcript(messages: list[dict]) -> str:
    return "\n".join(str(m.get("content")) for m in messages)


def _authorized_source(messages: list[dict]) -> str:
    for message in messages:
        content = message.get("content")
        if not isinstance(content, str) or not content.startswith("{"):
            continue
        try:
            document = json.loads(content)
        except json.JSONDecodeError:
            continue
        sources = document.get("authorized_sources") if isinstance(document, dict) else None
        if isinstance(sources, list) and sources:
            return str(sources[0])
    return ""


@pytest.fixture
def service() -> ThreadingHTTPServer:
    _Service.requests = []
    _Service.answers = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    csv = tmp_path / "panel.csv"
    csv.write_text("obligor,pd,amount\nA-1,0.01,100\n", encoding="utf-8")
    ontology = tmp_path / "ontology.yaml"
    ontology.write_text(ONTOLOGY, encoding="utf-8")
    task = tmp_path / "task.yaml"
    task.write_text(TASK.replace("CSV_PATH", str(csv)), encoding="utf-8")
    return task, ontology


def test_the_chain_recovers_from_prose_and_from_a_refused_schema(
    tmp_path: Path, service: ThreadingHTTPServer, capsys: pytest.CaptureFixture[str]
) -> None:
    """★ The measured failure, then the two later layers, in one run."""
    task, ontology = _fixture(tmp_path)
    host, port = service.server_address[:2]
    code = cli.main(
        [
            "run",
            str(task),
            "--ontology",
            str(ontology),
            "--out",
            str(tmp_path / "reports"),
            "--snapshots",
            str(tmp_path / "snapshots"),
        ],
        env={"BOBANANA6_BASE_URL": f"http://{host}:{port}/v1", "BOBANANA6_MODEL": "stub", "BOBANANA6_TIMEOUT_S": "10"},
    )
    printed = capsys.readouterr()
    assert code == 0, printed.out + printed.err
    assert "exit      : passed" in printed.out

    sent = _Service.requests
    # turn 1 free-form (it may need a tool), turn 2 asks for the shape, turn 3 does
    # not because the service refused it -- and the run still reaches a candidate.
    assert [("response_format" in body) for body in sent] == [False, True, False]
    # ① the last call carries the structured fix, and the transcript stays clean
    last = sent[-1]["messages"]
    feedback = [m for m in last if isinstance(m.get("content"), str) and "required shape" in m["content"]]
    assert feedback, last
    text = feedback[0]["content"]
    assert "code: not_json" in text
    assert "what you sent: I think the panel is probably fine" in text
    assert "Traceback" not in text

    summary = json.loads((tmp_path / "reports" / "summary.json").read_text(encoding="utf-8"))
    assert summary["exit_reason"] == "passed"
    assert summary["blocks_report"][0]["ok"] is True
    assert summary["blocks_report"][0]["source_id"] == "panel-2026q1"


def test_a_service_without_structured_output_is_asked_only_once(
    tmp_path: Path, service: ThreadingHTTPServer
) -> None:
    """★ Capability negotiation is not a per-call probe: one refusal per run."""
    task, ontology = _fixture(tmp_path)
    host, port = service.server_address[:2]
    per_run: list[list[bool]] = []
    for index in range(2):
        start = len(_Service.requests)
        code = cli.main(
            [
                "run",
                str(task),
                "--ontology",
                str(ontology),
                "--out",
                str(tmp_path / f"reports-{index}"),
                "--snapshots",
                str(tmp_path / f"snapshots-{index}"),
            ],
            env={"BOBANANA6_BASE_URL": f"http://{host}:{port}/v1", "BOBANANA6_MODEL": "stub", "BOBANANA6_TIMEOUT_S": "10"},
        )
        assert code == 0
        per_run.append([("response_format" in body) for body in _Service.requests[start:]])

    # ★ Within one run it is offered once and then remembered; a fresh run is a
    #   fresh negotiation (the flag lives in the run's assembly, not in a module global).
    assert per_run == [[False, True, False], [False, True, False]]
