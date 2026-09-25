"""The service half of the model pills: which model ANSWERED, and whether it searched.

The console shows two pills at the top right: the model that answered the last request, and
``Search`` when that answer used an online search tool. Both come from response headers the kit
emits (``install_answer_provenance`` in ``api/app.py``) for whatever the model adapters NOTED as
they called. Before a request is answered the pill shows ``generator_model`` from ``/healthz``,
so that value must be the model the bound adapter calls, never one a configuration flag names
while the adapter calls another.

The Gemini narrator is driven here through a FAKE ``google.genai`` module, so what is proved is
this repository's half: the model id the call notes, and the sampling it sends (the coverage
note is narration, so no temperature at all; a pinned request still reaches the config).
"""

from __future__ import annotations

import dataclasses
import sys
import types
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hex_service_kit import provenance

from obligations_control_mapping import config
from obligations_control_mapping.adapters.gcp.generation import CloudGenerationAdapter
from obligations_control_mapping.adapters.local.generation import (
    STUB_MODEL,
    LocalGenerationAdapter,
)
from obligations_control_mapping.api import app as app_module
from obligations_control_mapping.ports.generation import GenerationRequest, GenerationResponse

from tests import REPO_ROOT
from tests.conftest import local_settings

ANSWERED_BY = "x-answered-by"
SEARCH_USED = "x-search-used"
_AUDITOR = {"X-Dev-Persona": "auditor"}


@pytest.fixture()
def local_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The API under ``local`` whatever the shell exported: CI runs with no profile set."""
    monkeypatch.setenv(config._PROFILE_ENV, "local")
    app_module._container.cache_clear()
    with TestClient(app_module.app, client=("127.0.0.1", 50000)) as client:
        yield client
    app_module._container.cache_clear()


def _coverage(client: TestClient) -> dict[str, str]:
    response = client.post("/v1/coverage", json={}, headers=_AUDITOR)
    assert response.status_code == 200, response.text
    return dict(response.headers)


def test_the_local_narrator_answers_as_the_stub_the_pill_first_names(
    local_client: TestClient,
) -> None:
    headers = _coverage(local_client)
    assert headers[ANSWERED_BY] == STUB_MODEL
    assert SEARCH_USED not in headers
    assert local_settings().generator_model == STUB_MODEL


def test_a_call_that_searched_says_so_and_the_next_request_starts_fresh(
    local_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = LocalGenerationAdapter.generate

    def searching(self: LocalGenerationAdapter, request: GenerationRequest) -> GenerationResponse:
        provenance.note_model("fake-searching-model")
        provenance.note_search()
        return original(self, request)

    monkeypatch.setattr(LocalGenerationAdapter, "generate", searching)
    headers = _coverage(local_client)
    assert headers[ANSWERED_BY] == f"fake-searching-model, {STUB_MODEL}"
    assert headers[SEARCH_USED] == "true"
    monkeypatch.setattr(LocalGenerationAdapter, "generate", original)
    headers = _coverage(local_client)
    assert headers[ANSWERED_BY] == STUB_MODEL
    assert SEARCH_USED not in headers


def test_the_coverage_note_is_drafted_with_no_temperature(
    local_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[GenerationRequest] = []
    original = LocalGenerationAdapter.generate

    def recording(self: LocalGenerationAdapter, request: GenerationRequest) -> GenerationResponse:
        seen.append(request)
        return original(self, request)

    monkeypatch.setattr(LocalGenerationAdapter, "generate", recording)
    _coverage(local_client)
    assert seen and all(request.temperature is None for request in seen)


# --------------------------------------------------------------------------------------- #
# The Gemini narrator, through a fake SDK.
# --------------------------------------------------------------------------------------- #
class _FakeModels:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(text='{"note": "Coverage stands."}')


@pytest.fixture()
def fake_genai(monkeypatch: pytest.MonkeyPatch) -> _FakeModels:
    models = _FakeModels()
    genai = types.ModuleType("google.genai")
    genai_types = types.ModuleType("google.genai.types")
    genai_types.GenerateContentConfig = lambda **kw: SimpleNamespace(**kw)  # type: ignore[attr-defined]
    genai.types = genai_types  # type: ignore[attr-defined]
    genai.Client = lambda **_: SimpleNamespace(models=models)  # type: ignore[attr-defined]
    google = sys.modules.get("google") or types.ModuleType("google")
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setattr(google, "genai", genai, raising=False)
    monkeypatch.setitem(sys.modules, "google.genai", genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", genai_types)
    return models


def _gcp_adapter() -> CloudGenerationAdapter:
    return CloudGenerationAdapter(dataclasses.replace(local_settings(), profile="gcp"))


def test_the_gemini_narrator_notes_its_model_and_drafts_with_no_temperature(
    fake_genai: _FakeModels,
) -> None:
    with provenance.scope() as record:
        _gcp_adapter().generate(GenerationRequest(system="s", prompt="p"))
    assert record.models == [CloudGenerationAdapter._MODEL]
    assert record.search_used is False
    (call,) = fake_genai.calls
    assert call["model"] == CloudGenerationAdapter._MODEL
    assert call["config"].temperature is None, "free sampling: the SDK sends no temperature"


def test_a_pinned_request_reaches_the_gemini_config(fake_genai: _FakeModels) -> None:
    _gcp_adapter().generate(GenerationRequest(system="s", prompt="p", temperature=0.0))
    assert fake_genai.calls[0]["config"].temperature == 0.0


def test_generator_model_is_the_model_the_gemini_narrator_calls() -> None:
    settings = dataclasses.replace(local_settings(), profile="gcp")
    assert settings.generator_model == CloudGenerationAdapter._MODEL


def test_no_flag_swaps_in_a_model_the_adapter_never_calls() -> None:
    """The latent false banner: a flag that moved the pill but not the model that answered."""
    models = SimpleNamespace(
        reasoning="the-model-the-adapter-calls",
        hard_reasoning="a-model-nobody-calls",
        use_hard_reasoning=True,
    )
    named = config._model_from_settings(SimpleNamespace(models=models), "models.reasoning")
    assert named == "the-model-the-adapter-calls"


def test_the_hard_reasoning_flag_does_not_exist() -> None:
    settings_file = (REPO_ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")
    assert "use_hard_reasoning" not in settings_file
    for source in sorted((REPO_ROOT / "src").rglob("*.py")):
        assert "use_hard_reasoning" not in source.read_text(encoding="utf-8"), source
