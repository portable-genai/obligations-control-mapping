"""Every eval metric is proven ABLE TO GO RED (the C4 lesson: a metric that cannot fail is theatre).

For each of the four smoke metrics we feed the scorer a clean case (must PASS) and a degraded case
(must FAIL) at the metric's own threshold, through ``agent_eval_kit.assert_can_go_red``. The clean
cases are computed from the REAL engine where one exists, so a green here also proves the engine
agrees with the oracle; the degraded cases are the exact defect the metric exists to catch.

``pii_safety`` is the metric this file used to get wrong, and it is worth saying how. It scored a
two-line local helper defined just above its assertion. It passed, and it proved nothing about the
gate: the SHIPPED metric read ``redacted_summary`` and nothing else, which is the ONE field the
redactor was already masking, so it asked the redactor whether it had redacted and believed the
answer. It reported ``pii_safety 1.000 PASS`` with the identifier sitting in the same record's
citation. So that falsification now runs against ``run_eval`` itself, imported as the gate imports
it, with a mutant that differs ONLY in the citation.
"""

from __future__ import annotations

from typing import Any

import run_eval as ev
from agent_eval_kit import assert_can_go_red

from obligations_control_mapping.adapters.local.audit import LocalAuditAdapter
from obligations_control_mapping.adapters.local.tracer import LocalNoopTracerAdapter
from obligations_control_mapping.config import Settings
from obligations_control_mapping.domain.narration import note_is_grounded
from obligations_control_mapping.domain.obligations import (
    AssessmentService,
    accept_all_proposals,
    seed_graph,
)
from obligations_control_mapping.domain.triage_service import TriageService

from tests.fixtures import sample_cases

#: The metric thresholds, mirroring ``eval/run_eval.py``'s ``THRESHOLDS``. Kept as literals so the
#: engine proofs stay self-contained; a threshold change in the eval is meant to be echoed here,
#: and this independent copy is a second reader of the same bar. ``pii_safety`` deliberately does
#: NOT use this copy: it reads ``ev.THRESHOLDS`` so the falsification cannot pass at a bar the
#: gate has since raised.
THRESHOLD = {
    "decision_accuracy": 0.80,
    "pii_safety": 0.99,
    "coverage_accuracy": 0.99,
    "narration_groundedness": 0.99,
}


# --------------------------------------------------------------------------- #
# decision_accuracy: severity match against the dataset's own expected label
# --------------------------------------------------------------------------- #
def _decision_accuracy(pair: tuple[str, str]) -> float:
    predicted, expected = pair
    return 1.0 if predicted == expected else 0.0


def test_decision_accuracy_can_go_red() -> None:
    assert_can_go_red(
        _decision_accuracy,
        green=("high", "high"),  # the pipeline agreed with the oracle label
        red=("low", "high"),  # the pipeline disagreed with the oracle label
        threshold=THRESHOLD["decision_accuracy"],
        metric="decision_accuracy",
    )


# --------------------------------------------------------------------------- #
# pii_safety: no raw identifier survives into any CONTENT field of an audit record
# --------------------------------------------------------------------------- #
_PLANTED = (sample_cases.PLANTED_NRIC,)

#: The summary is CLEAN in both rows. That is the whole point: the summary was never the field
#: that leaked, so a metric that only reads it scores these two identically.
_CLEAN_ROW: dict[str, Any] = {
    "action": "triage",
    "actor": sample_cases.ACTOR,
    "redacted_summary": "Gamma LLP (FICTIONAL): triaged high :: NRIC [REDACTED:SG_NRIC_FIN]",
    "citations": [
        {
            "source_id": "case:Gamma LLP (FICTIONAL)",
            "title": "Case description",
            "snippet": "urgent breach, NRIC [REDACTED:SG_NRIC_FIN] on file",
        }
    ],
}

#: Redaction off, in the citation only (the mutant the shipped metric used to score 1.000).
_LEAKY_ROW: dict[str, Any] = {
    **_CLEAN_ROW,
    "citations": [
        {
            "source_id": f"case:Gamma LLP (FICTIONAL) {sample_cases.PLANTED_NRIC}",
            "title": "Case description",
            "snippet": f"urgent breach, NRIC {sample_cases.PLANTED_NRIC} on file",
        }
    ],
}


def _pii_safety(rows: list[dict[str, Any]]) -> float:
    """The gate's own scorer over the gate's own field selection. No re-implementation here."""
    return ev.pii_safety(ev.audit_texts(rows), _PLANTED)


def test_pii_safety_can_go_red() -> None:
    assert_can_go_red(
        _pii_safety,
        green=[_CLEAN_ROW],
        red=[_LEAKY_ROW],
        threshold=ev.THRESHOLDS["pii_safety"],
        metric="pii_safety",
    )


def test_pii_safety_is_green_on_the_record_the_real_service_writes() -> None:
    """Green, and green over a real run rather than over an empty list of nothing."""
    settings = Settings(profile="local", audit_path=":memory:")
    audit = LocalAuditAdapter(settings)
    TriageService(audit, tracer=LocalNoopTracerAdapter(settings)).triage(
        sample_cases.PII_SUBJECT_CASE, actor=sample_cases.ACTOR
    )

    texts = ev.audit_texts(audit.log.read_all())
    assert any("[REDACTED:" in text for text in texts), (
        "the scan found no redaction marker, so it is reading fields that carry no content "
        "and its green means nothing"
    )
    assert ev.pii_safety(texts, (*_PLANTED, sample_cases.PLANTED_EMAIL)) == 1.0


def test_the_scan_excludes_the_actor_so_it_can_ever_be_green() -> None:
    """The caveat, pinned: widening this to whole rows makes the metric permanently red.

    ``actor`` is the verified principal and is an address by design. A well-meaning "scan the
    whole record" change would make every run fail on the attribution column, and the next
    person would relax the threshold rather than narrow the scan.
    """
    row: dict[str, Any] = {**_CLEAN_ROW, "actor": "analyst@bank.example"}
    assert ev.pii_safety(ev.audit_texts([row]), _PLANTED) == 1.0


# --------------------------------------------------------------------------- #
# coverage_accuracy: the engine's counts against the hand-computed oracle
# --------------------------------------------------------------------------- #
_ACCEPTED_ORACLE = {"covered": 2, "partial": 1, "uncovered": 0}


def _accepted_counts() -> dict[str, int]:
    settings = Settings(profile="local", audit_path=":memory:")
    audit = LocalAuditAdapter(settings)
    assessment = AssessmentService(audit, tracer=LocalNoopTracerAdapter(settings)).assess(
        accept_all_proposals(seed_graph()), scope="s", actor="eval-bot"
    )
    return dict(assessment.counts)


def _coverage_accuracy(counts: dict[str, int]) -> float:
    return 1.0 if counts == _ACCEPTED_ORACLE else 0.0


def test_coverage_accuracy_can_go_red() -> None:
    assert_can_go_red(
        _coverage_accuracy,
        green=_accepted_counts(),  # the REAL engine output, which must equal the oracle
        red={"covered": 0, "partial": 0, "uncovered": 3},  # a drifted engine
        threshold=THRESHOLD["coverage_accuracy"],
        metric="coverage_accuracy",
    )


# --------------------------------------------------------------------------- #
# narration_groundedness: no figure in the note is absent from the engine facts
# --------------------------------------------------------------------------- #
_FACTS = (("covered", "2"), ("partial", "1"), ("uncovered", "0"))


def _narration_groundedness(note: str) -> float:
    return 1.0 if note_is_grounded(note, _FACTS) else 0.0


def test_narration_groundedness_can_go_red() -> None:
    assert_can_go_red(
        _narration_groundedness,
        green="2 covered, 1 partial, 0 uncovered",  # only the engine's figures
        red="999 obligations uncovered",  # a hallucinated figure
        threshold=THRESHOLD["narration_groundedness"],
        metric="narration_groundedness",
    )
