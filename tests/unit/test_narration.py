"""The narration service: the model narrates, and a note it cannot ground is discarded.

The engine owns every number; the model only restates them. These tests prove the service accepts
a grounded model note, discards a schema-invalid one, discards an ungrounded one (a hallucinated
figure), degrades to a grounded fallback when the model raises, and that the fallback is grounded
by construction. A hallucinated figure therefore never reaches a surface.
"""

from __future__ import annotations

import json

from obligations_control_mapping.config import Container
from obligations_control_mapping.domain.narration import (
    NarrationService,
    build_request,
    fallback_text,
    grounded_integers,
    note_is_grounded,
    parse_note,
)
from obligations_control_mapping.domain.obligations import (
    AssessmentService,
    CoverageAssessment,
    accept_all_proposals,
    seed_graph,
)
from obligations_control_mapping.ports.generation import GenerationRequest, GenerationResponse


class _StubGeneration:
    """A generation port that returns a fixed raw text, so the service's checks are exercised."""

    def __init__(self, text: str) -> None:
        self._text = text

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        return GenerationResponse(text=self._text, model="stub")


class _RaisingGeneration:
    """A generation port that fails to reach the model."""

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        raise RuntimeError("model endpoint unreachable")


def _assessment(container: Container) -> CoverageAssessment:
    return AssessmentService(container.audit, tracer=container.tracer).assess(
        accept_all_proposals(seed_graph()), scope="register", actor="analyst@bank.example"
    )


def test_grounded_model_note_is_accepted(container: Container) -> None:
    assessment = _assessment(
        container
    )  # facts: covered=2, partial=1, uncovered=0, orphan=1, stale=0
    grounded = json.dumps({"note": "2 obligations covered, 1 partial, 0 uncovered; 1 orphan."})
    note = NarrationService(_StubGeneration(grounded)).narrate(assessment)
    assert note.model_authored is True
    assert note.grounded is True
    assert "2 obligations covered" in note.text


def test_non_json_output_is_discarded_for_the_fallback(container: Container) -> None:
    assessment = _assessment(container)
    note = NarrationService(_StubGeneration("not json at all")).narrate(assessment)
    assert note.model_authored is False
    assert note.grounded is True
    assert note.text == fallback_text(build_request(assessment).facts)


def test_json_missing_the_note_key_is_discarded(container: Container) -> None:
    assessment = _assessment(container)
    note = NarrationService(_StubGeneration(json.dumps({"summary": "wrong key"}))).narrate(
        assessment
    )
    assert note.model_authored is False
    assert note.text == fallback_text(build_request(assessment).facts)


def test_ungrounded_note_with_a_hallucinated_figure_is_discarded(container: Container) -> None:
    assessment = _assessment(container)
    liar = json.dumps({"note": "coverage is 999 covered and 888 partial"})
    note = NarrationService(_StubGeneration(liar)).narrate(assessment)
    # 999 / 888 are not figures the engine produced, so the note is discarded, never repaired.
    assert note.model_authored is False
    assert "999" not in note.text and "888" not in note.text


def test_model_failure_degrades_to_a_grounded_fallback(container: Container) -> None:
    assessment = _assessment(container)
    note = NarrationService(_RaisingGeneration()).narrate(assessment)
    assert note.model_authored is False
    assert note.grounded is True
    assert note.text == fallback_text(build_request(assessment).facts)


def test_fallback_is_grounded_by_construction(container: Container) -> None:
    facts = build_request(_assessment(container)).facts
    assert note_is_grounded(fallback_text(facts), facts)


def test_groundedness_helpers() -> None:
    facts = (("covered", "2"), ("partial", "1"), ("uncovered", "0"))
    assert grounded_integers(facts) == {"0", "1", "2"}
    assert note_is_grounded("2 covered, 1 partial, 0 uncovered", facts) is True
    assert note_is_grounded("42 covered", facts) is False


def test_parse_note_rejects_blank_and_non_dict() -> None:
    assert parse_note('{"note": "   "}') is None
    assert parse_note("[1, 2, 3]") is None
    assert parse_note('{"note": "ok"}') == "ok"
