"""The obligation-graph engine: coverage bands, staleness, escalation, and cross-tenant denial.

The consequential numbers (which obligations are covered, which controls are orphaned, which
mappings are stale, and the severity band) are PURE code over the shared kernel. These tests pin
the seed register's coverage in every state, prove the assessment is replay-identical and audited,
and prove a verified principal from another tenant is refused rather than served.
"""

from __future__ import annotations

from datetime import date

import pytest
from obligation_register import (
    Citation as KitCitation,
)
from obligation_register import (
    Edge,
    EdgeKind,
    EdgeStatus,
    Node,
    NodeKind,
    NodeRef,
    Obligation,
    ObligationGraph,
)

from obligations_control_mapping.config import Container
from obligations_control_mapping.domain.kernel import Decision, Severity
from obligations_control_mapping.domain.obligations import (
    REGISTER_TENANT,
    AssessmentService,
    CoverageAssessment,
    CrossTenantError,
    accept_all_proposals,
    apply_change_feed,
    authorize_register_access,
    seed_graph,
)

_ACTOR = "analyst@bank.example"


def _assess(
    container: Container, graph: ObligationGraph, scope: str = "register"
) -> CoverageAssessment:
    return AssessmentService(container.audit, tracer=container.tracer).assess(
        graph, scope=scope, actor=_ACTOR
    )


def test_seed_register_exercises_every_coverage_state(container: Container) -> None:
    assessment = _assess(container, seed_graph())
    assert dict(assessment.counts) == {"covered": 1, "partial": 1, "uncovered": 1}
    assert assessment.orphan_controls == ("ctrl-orphan",)
    assert assessment.stale_edges == ()
    # An uncovered obligation is the worst band, so the seed graph is HIGH and escalates.
    assert assessment.severity is Severity.HIGH
    assert assessment.decision is Decision.ESCALATED
    assert assessment.requires_human_review is True
    assert assessment.citations, "every result carries provenance"


def test_accepting_proposals_covers_the_incident_obligation(container: Container) -> None:
    assessment = _assess(container, accept_all_proposals(seed_graph()))
    assert dict(assessment.counts) == {"covered": 2, "partial": 1, "uncovered": 0}
    # No uncovered obligation remains, but a partial one and an orphan control keep it MEDIUM.
    assert assessment.severity is Severity.MEDIUM


def test_change_feed_marks_edges_stale_and_reopens_coverage(container: Container) -> None:
    stale = apply_change_feed(accept_all_proposals(seed_graph()), ("mas-trm.example",))
    assessment = _assess(container, stale)
    assert dict(assessment.counts) == {"covered": 1, "partial": 0, "uncovered": 2}
    assert len(assessment.stale_edges) == 3
    assert assessment.severity is Severity.HIGH


def test_assessment_is_replay_identical(container: Container) -> None:
    graph = accept_all_proposals(seed_graph())
    first = _assess(container, graph)
    second = _assess(container, graph)
    assert first.counts == second.counts
    assert first.severity == second.severity
    assert first.orphan_controls == second.orphan_controls
    assert first.stale_edges == second.stale_edges
    assert [(g.kind, g.subject) for g in first.gaps] == [(g.kind, g.subject) for g in second.gaps]


def test_assessment_writes_an_audit_record(container: Container) -> None:
    _assess(container, seed_graph())
    records = container.audit.log.read_all()
    assert records, "the coverage assessment must leave an audit record"
    assert any(r.get("action") == "coverage_assessment" for r in records)


def _covered_graph() -> ObligationGraph:
    """A minimal register with one fully covered obligation and no orphan control: the LOW band."""
    cite = KitCitation(
        source_id="reg.example", locator="s1", title="rule", url="https://reg.example/x"
    )
    obligation = Obligation(
        id="obl-x",
        title="Covered obligation",
        text="A fully covered obligation.",
        owner="office",
        citation=cite,
        effective_from=date(2026, 1, 1),
    )
    control = Node(ref=NodeRef(NodeKind.CONTROL, "ctrl-x"), title="Control", citations=(cite,))
    evidence = Node(ref=NodeRef(NodeKind.EVIDENCE, "ev-x"), title="Evidence")
    edges = (
        Edge(
            src=NodeRef(NodeKind.OBLIGATION, "obl-x"),
            dst=NodeRef(NodeKind.CONTROL, "ctrl-x"),
            kind=EdgeKind.OBLIGATION_TO_CONTROL,
            status=EdgeStatus.ACCEPTED,
            citations=(cite,),
        ),
        Edge(
            src=NodeRef(NodeKind.CONTROL, "ctrl-x"),
            dst=NodeRef(NodeKind.EVIDENCE, "ev-x"),
            kind=EdgeKind.CONTROL_TO_EVIDENCE,
            status=EdgeStatus.ACCEPTED,
            citations=(cite,),
        ),
    )
    return ObligationGraph(obligations=(obligation,), nodes=(control, evidence), edges=edges)


def test_fully_covered_graph_is_low_and_not_escalated(container: Container) -> None:
    assessment = _assess(container, _covered_graph())
    assert dict(assessment.counts) == {"covered": 1, "partial": 0, "uncovered": 0}
    assert assessment.orphan_controls == ()
    assert assessment.severity is Severity.LOW
    assert assessment.decision is Decision.ALLOWED
    # A router that manufactured a review here would be lying: no gap, no escalation.
    assert assessment.requires_human_review is False


def test_authorize_register_access_allows_the_owning_tenant() -> None:
    authorize_register_access(REGISTER_TENANT)  # the demo-bank register owner: no raise


def test_authorize_register_access_denies_another_tenant() -> None:
    with pytest.raises(CrossTenantError) as excinfo:
        authorize_register_access("other-bank")
    # The error carries both tenants, so the surface can log the denial without re-deriving them.
    assert excinfo.value.principal_tenant == "other-bank"
    assert excinfo.value.register_tenant == REGISTER_TENANT


def test_authorize_register_access_has_no_permissive_default() -> None:
    # An empty principal tenant (nobody verified) is NOT the owner and is refused, never allowed.
    with pytest.raises(CrossTenantError):
        authorize_register_access("")
