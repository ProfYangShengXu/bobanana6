"""接模型：``core/ports.py::LLMClient`` / ``OntologyPort`` 的实作（易变侧）。

★ 这里只做协议转换，不做判定。判定永远在 ``core/`` —— 所以本包
**不 import** `core.kernel` / `core.checkers` / `core.split` / `core.assemble` /
`core.consist` / `core.ontology` / `core.capabilities` / `core.provenance`，
由 `tests/test_arch_guard.py` 的 `test_adapters_*` 守着。起草出来的本体是【候选】，
语义校验由装配点调用 `core/ontology.py` 完成。
"""

from __future__ import annotations

from .offline import EchoToolResultClient, ScriptedClient
from .ontology import DraftingOntology, FileOntology, OntologyDraftError
from .openai_compat import (
    DEFAULT_TIMEOUT_S,
    LLMConfigError,
    LLMConnectionError,
    LLMError,
    LLMSettings,
    OpenAICompatibleClient,
)

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "PROTECTED_PAYLOAD_KEYS",
    "DraftingOntology",
    "EchoToolResultClient",
    "FileOntology",
    "LLMConfigError",
    "LLMConnectionError",
    "LLMError",
    "LLMSettings",
    "OntologyDraftError",
    "OpenAICompatibleClient",
    "ScriptedClient",
]
