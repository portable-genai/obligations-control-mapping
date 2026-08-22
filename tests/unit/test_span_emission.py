"""Both consequential paths open ONE span each, and no span carries content.

A trace backend is not the WORM audit trail. It has no redaction stage, no retention policy
written against a regulator's requirement, and a far wider read audience than the audit store.
So the value of tracing these paths depends entirely on the spans carrying structural
attributes only: which action, whose. A case subject, an obligation's text, a gap's subject or
detail, the caller-supplied scope string or a planted identifier reaching a span has left the
boundary redaction exists to hold, and it has left it silently.

Two orchestrators are pinned because BOTH are real request paths: ``/v1/triage`` drives
``TriageService.triage`` and ``/v1/coverage`` drives ``AssessmentService.assess`` over the
obligation register, the graph this system is the record for. The triage content case drives
the case with the planted NRIC; the coverage content case asserts no gap subject or detail
from the seeded register reaches a span.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from obligations_control_mapping.config import build_container
from obligations_control_mapping.domain.models import TriageInput
from obligations_control_mapping.domain.obligations import (
    AssessmentService,
    CoverageAssessment,
    seed_graph,
)
from obligations_control_mapping.domain.triage_service import TriageService

from tests.conftest import local_settings
from tests.fixtures import sample_cases

#: The complete attribute key set each span may carry. Adding to one of these is a decision
#: about what leaves the trust boundary, so it is made here rather than at the call site.
_TRIAGE_KEYS = {"action", "actor"}
_ASSESS_KEYS = {"action", "actor"}

#: A scope string with the planted identifier, so the "scope stays off the span" claim is
#: proven against input that would actually leak rather than against a bland literal.
_PII_SCOPE = f"register for NRIC {sample_cases.PLANTED_NRIC}"


class _RecordingTracer:
    """Captures every span name and attribute so the test can inspect what was emitted."""

    def __init__(self) -> None:
        self.spans: list[tuple[str, dict[str, str]]] = []

    @contextmanager
    def span(self, name: str, **attributes: str) -> Iterator[None]:
        self.spans.append((name, dict(attributes)))
        yield

    def record_token_usage(self, usage: object, model: str) -> None:
        return None


def _triage(case: TriageInput) -> _RecordingTracer:
    container = build_container(local_settings())
    tracer = _RecordingTracer()
    service = TriageService(container.audit, tracer=tracer)  # type: ignore[arg-type]
    service.triage(case, actor=sample_cases.ACTOR)
    return tracer


def _assess(scope: str = "register") -> tuple[_RecordingTracer, CoverageAssessment]:
    """The seeded register, unaccepted, so the assessment has real gaps to keep quiet about."""
    container = build_container(local_settings())
    tracer = _RecordingTracer()
    service = AssessmentService(container.audit, tracer=tracer)  # type: ignore[arg-type]
    assessment = service.assess(seed_graph(), scope=scope, actor=sample_cases.ACTOR)
    return tracer, assessment


def _emitted(tracer: _RecordingTracer) -> str:
    """Every attribute KEY and VALUE that was emitted, as one searchable blob."""
    parts: list[str] = []
    for name, attributes in tracer.spans:
        parts.append(name)
        parts.extend(attributes)
        parts.extend(attributes.values())
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# The spans exist at all
# --------------------------------------------------------------------------- #
def test_triaging_a_case_opens_exactly_one_named_span() -> None:
    tracer = _triage(sample_cases.ROUTINE_CASE)
    assert [name for name, _ in tracer.spans] == ["obligations.triage"]


def test_assessing_coverage_opens_exactly_one_named_span() -> None:
    tracer, _ = _assess()
    assert [name for name, _ in tracer.spans] == ["obligations.assess_coverage"]


# --------------------------------------------------------------------------- #
# What the spans carry
# --------------------------------------------------------------------------- #
def test_the_triage_span_carries_the_structural_attributes_an_operator_needs() -> None:
    _, attributes = _triage(sample_cases.ROUTINE_CASE).spans[0]
    assert attributes["action"] == "triage"
    assert attributes["actor"] == sample_cases.ACTOR


def test_the_assess_span_carries_the_structural_attributes_an_operator_needs() -> None:
    tracer, _ = _assess()
    _, attributes = tracer.spans[0]
    assert attributes["action"] == "assess_coverage"
    assert attributes["actor"] == sample_cases.ACTOR


@pytest.mark.parametrize(
    "case",
    [sample_cases.ROUTINE_CASE, sample_cases.ESCALATING_CASE, sample_cases.PII_CASE],
    ids=["routine", "escalating", "pii"],
)
def test_the_triage_attribute_set_is_a_fixed_allowlist_whatever_the_verdict(
    case: TriageInput,
) -> None:
    """An escalating case must not start attaching its findings to the span to explain itself."""
    for _, attributes in _triage(case).spans:
        assert set(attributes) == _TRIAGE_KEYS


def test_the_assess_attribute_set_is_a_fixed_allowlist() -> None:
    """A gapped register must not start attaching its gaps to the span to explain itself."""
    tracer, assessment = _assess()
    assert assessment.requires_human_review, (
        "the seeded register stopped producing gaps, so this test no longer proves an "
        "escalating assessment keeps its findings off the span"
    )
    for _, attributes in tracer.spans:
        assert set(attributes) == _ASSESS_KEYS, (
            "a new span attribute appeared; confirm it is structural, then widen "
            "the allowlist here deliberately"
        )


# --------------------------------------------------------------------------- #
# What the spans must never carry
# --------------------------------------------------------------------------- #
def test_no_triage_span_attribute_carries_case_content_or_the_planted_identifier() -> None:
    """The case used here has an NRIC planted in its description, so a leak would show."""
    emitted = _emitted(_triage(sample_cases.PII_CASE))
    forbidden = [
        sample_cases.PLANTED_NRIC,
        sample_cases.PII_CASE.subject,
        sample_cases.PII_CASE.text,
        "ops@gamma.example",
    ]
    for literal in forbidden:
        assert literal, "an empty needle would pass this test for the wrong reason"
        assert literal.lower() not in emitted.lower(), f"a span attribute carried {literal!r}"


def test_no_assess_span_attribute_carries_register_content_or_the_scope_string() -> None:
    """Every gap the seeded register produces, so a leak nobody wrote a case for still fails."""
    tracer, assessment = _assess(scope=_PII_SCOPE)
    emitted = _emitted(tracer).lower()

    assert sample_cases.PLANTED_NRIC.lower() not in emitted
    assert _PII_SCOPE.lower() not in emitted, "the caller-supplied scope reached a span"
    assert assessment.gaps, "an empty gap list would pass this test for the wrong reason"
    for gap in assessment.gaps:
        for literal in (gap.subject, gap.detail):
            assert literal, "an empty needle would pass this test for the wrong reason"
            assert literal.lower() not in emitted, f"a span attribute carried {literal!r}"


def test_every_emitted_attribute_value_is_a_string_the_port_declares() -> None:
    """``span(name, **attributes: str)``: a non-string would serialise however the SDK felt."""
    tracer, _ = _assess()
    values: list[Any] = [
        v
        for t in (tracer, _triage(sample_cases.ESCALATING_CASE))
        for _, attributes in t.spans
        for v in attributes.values()
    ]
    assert values
    assert all(isinstance(value, str) for value in values)
