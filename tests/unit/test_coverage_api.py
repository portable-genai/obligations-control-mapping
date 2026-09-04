"""The /v1/coverage surface: engine numbers, R8 routing, cross-tenant 403, and determinism.

The endpoint runs the deterministic engine, narrates the result through the bound model, routes any
gap to human-review-console (rule R8) in the same request, and authorises the read against the
VERIFIED principal's tenant. The determinism proof is the load-bearing one: with the generation
adapter replaced by a hallucinating stub, every consequential number in the response is
byte-identical, and the invented figures never appear.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from obligations_control_mapping.adapters.local.generation import LocalGenerationAdapter
from obligations_control_mapping.ports.generation import GenerationRequest, GenerationResponse

_AUDITOR = {"X-Dev-Persona": "auditor"}  # tenant demo-bank: the register owner
_OTHER_TENANT = {"X-Dev-Persona": "other-tenant"}  # tenant other-bank

_CONSEQUENTIAL = (
    "counts",
    "severity",
    "decision",
    "orphan_controls",
    "stale_edges",
    "gaps",
    "requires_human_review",
)


def test_coverage_returns_engine_numbers_and_routes(api_client: TestClient) -> None:
    resp = api_client.post("/v1/coverage", json={}, headers=_AUDITOR)
    assert resp.status_code == 200
    body = resp.json()
    assert body["counts"] == {"covered": 1, "partial": 1, "uncovered": 1}
    assert body["severity"] == "high"
    assert body["requires_human_review"] is True
    # Rule R8: the escalation was ROUTED, not merely flagged.
    assert body["review_ref"]
    # Every result carries provenance and a grounded note.
    assert body["citations"]
    assert body["note"]


def test_coverage_accept_proposals_changes_the_counts(api_client: TestClient) -> None:
    resp = api_client.post("/v1/coverage", json={"accept_proposals": True}, headers=_AUDITOR)
    body = resp.json()
    assert body["counts"] == {"covered": 2, "partial": 1, "uncovered": 0}
    assert body["severity"] == "medium"


def test_coverage_denies_a_cross_tenant_principal_with_403(api_client: TestClient) -> None:
    resp = api_client.post("/v1/coverage", json={}, headers=_OTHER_TENANT)
    # 403, not 404: the register exists and the caller is simply not authorised for it.
    assert resp.status_code == 403


def test_coverage_allows_the_home_tenant_principal(api_client: TestClient) -> None:
    resp = api_client.post("/v1/coverage", json={}, headers=_AUDITOR)
    assert resp.status_code == 200


def _hallucinate(self: LocalGenerationAdapter, request: GenerationRequest) -> GenerationResponse:
    """Stand in for a model that invents figures the engine never produced."""
    return GenerationResponse(
        text='{"note": "coverage is 999 covered, 888 partial and 777 uncovered"}',
        model="hallucinating-stub",
    )


def test_coverage_numbers_are_identical_when_generation_is_stubbed(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = {"accept_proposals": True}
    honest = api_client.post("/v1/coverage", json=body, headers=_AUDITOR).json()

    # Replace the narrator with one that hallucinates wild numbers.
    monkeypatch.setattr(LocalGenerationAdapter, "generate", _hallucinate)
    stubbed = api_client.post("/v1/coverage", json=body, headers=_AUDITOR).json()

    # The consequential output is the engine's, so it does not move when the model is swapped.
    for key in _CONSEQUENTIAL:
        assert honest[key] == stubbed[key], f"{key} changed with the generation adapter"

    # The honest local narrator is grounded and accepted; the hallucination is discarded, and its
    # invented figures never reach the response.
    assert honest["note_model_authored"] is True
    assert stubbed["note_model_authored"] is False
    assert "999" not in stubbed["note"]
    assert "888" not in stubbed["note"]
