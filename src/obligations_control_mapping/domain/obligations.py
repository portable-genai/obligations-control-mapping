"""The obligation-graph vertical: the deterministic engine, over obligation-register-kit.

This is Rgc7's reason to exist: the SINGLE SYSTEM OF RECORD for the obligation to policy to
control to evidence graph. The consequential math (which obligations are covered, which controls
are orphaned, which mappings are stale, and how severe the resulting picture is) is PURE CODE in
the shared ``obligation_register`` kernel and in the deterministic policy below. An LLM never
produces any of these numbers or the severity band; it only narrates them (see
``narration.py``), and only accepted, non-stale edges ever count toward coverage.

The kernel is the engine; this module supplies the vertical: a seed register of obligations and
controls, the maker-checker acceptance step, the horizon-driven staleness sweep, and the
translation of a kernel :class:`~obligation_register.coverage.CoverageReport` into a consequential
:class:`CoverageAssessment` that carries a severity, a review flag and provenance.

Pure stdlib plus the stdlib-only kernel: no web framework, no cloud SDK, no clock beyond the one
the audit envelope uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from obligation_register import (
    Citation as KitCitation,
)
from obligation_register import (
    CoverageReport,
    Edge,
    EdgeKind,
    EdgeStatus,
    GapFinding,
    Node,
    NodeKind,
    NodeRef,
    Obligation,
    ObligationGraph,
    compute_coverage,
)

from ..ports.audit import AuditSinkPort
from ..ports.observability import ObservabilityTracerPort
from .kernel import AuditEvent, Citation, Decision, Severity, utcnow

__all__ = [
    "REGISTER_TENANT",
    "AssessmentService",
    "CoverageAssessment",
    "CrossTenantError",
    "GapView",
    "accept_all_proposals",
    "apply_change_feed",
    "authorize_register_access",
    "seed_graph",
    "worst_coverage_severity",
]


#: The tenant that OWNS the offline seed register. In a deployment the graph store carries each
#: register's owning tenant on its rows; offline, the seed IS the demo bank's register, so a caller
#: whose verified identity resolves to any other tenant is refused. It is the seeded ``demo-bank``
#: partition (see the local persona set), which is why the ``other-tenant`` persona is refused.
REGISTER_TENANT = "demo-bank"


class CrossTenantError(Exception):
    """A verified principal asked for a register owned by a different tenant.

    The surface maps this to HTTP 403, never 404: the register EXISTS and the caller is simply not
    authorised for it. Answering 404 would both misdescribe the refusal and leak the same fact more
    quietly, and the authorisation is made against the VERIFIED principal, so a 403 discloses
    nothing a truthful authorisation boundary does not already imply.
    """

    def __init__(self, principal_tenant: str, register_tenant: str) -> None:
        super().__init__(
            f"tenant {principal_tenant!r} is not authorised for the register owned by "
            f"{register_tenant!r}"
        )
        self.principal_tenant = principal_tenant
        self.register_tenant = register_tenant


def authorize_register_access(
    principal_tenant: str, *, register_tenant: str = REGISTER_TENANT
) -> None:
    """Authorise a VERIFIED principal's tenant against the register's owning tenant.

    Returns ``None`` when the principal's tenant owns the register; raises :class:`CrossTenantError`
    otherwise. There is deliberately no permissive default for ``principal_tenant``: the caller must
    pass the value it resolved from the verified principal, never a fallback that would authorise an
    unauthenticated request.
    """
    if principal_tenant != register_tenant:
        raise CrossTenantError(principal_tenant, register_tenant)


# --------------------------------------------------------------------------- #
# The vertical result (envelope-compatible with the R8 review path)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class GapView:
    """A flattened, surface-ready view of one kernel gap finding."""

    kind: str
    subject: str
    detail: str


@dataclass(frozen=True, slots=True)
class CoverageAssessment:
    """The consequential coverage picture for a scope: bands, gaps, severity and provenance.

    The field set overlaps the review envelope on purpose (``subject`` / ``severity`` /
    ``decision`` / ``summary`` / ``requires_human_review`` / ``citations``), so a surface can
    route it to Hrz7 through the shared R8 path without the domain importing a surface type.
    """

    scope: str
    subject: str
    severity: Severity
    decision: Decision
    summary: str
    requires_human_review: bool
    counts: tuple[tuple[str, int], ...]
    orphan_controls: tuple[str, ...]
    stale_edges: tuple[str, ...]
    gaps: tuple[GapView, ...]
    citations: tuple[Citation, ...] = ()


#: Deterministic severity policy over the coverage picture (B4: bank-owned, here as the default).
#: An uncovered obligation is the most serious state; a partial one or any stale mapping is a
#: middle state; a fully covered graph with no gaps is low. The band is PURE code and replayable;
#: the model never produces it.
def worst_coverage_severity(report: CoverageReport) -> Severity:
    """Return the deterministic severity band for a whole-graph coverage report."""
    counts = dict(report.counts)
    if counts.get("uncovered", 0) > 0:
        return Severity.HIGH
    if counts.get("partial", 0) > 0 or report.stale_edges or report.orphan_controls:
        return Severity.MEDIUM
    return Severity.LOW


def _kernel_citation_to_envelope(citation: KitCitation) -> Citation:
    """Translate a kernel citation into the audit/review envelope citation type."""
    return Citation(
        source_id=citation.source_id,
        title=citation.title or citation.locator or citation.source_id,
        snippet=citation.snippet or citation.locator,
    )


def _gap_citations(gaps: tuple[GapFinding, ...], limit: int = 8) -> tuple[Citation, ...]:
    seen: set[str] = set()
    out: list[Citation] = []
    for gap in gaps:
        for citation in gap.citations:
            if citation.source_id in seen:
                continue
            seen.add(citation.source_id)
            out.append(_kernel_citation_to_envelope(citation))
            if len(out) >= limit:
                return tuple(out)
    return tuple(out)


#: One span per coverage assessment. Structural attributes only: see
#: :meth:`AssessmentService.assess`.
_ASSESS_SPAN = "obligations.assess_coverage"


class AssessmentService:
    """Assess a graph's coverage into a consequential, cited, audited result.

    Mirrors the reference triage service's shape: the decision is deterministic (the kernel plus
    :func:`worst_coverage_severity`), an already-redacted audit record is written before the
    result is returned, every result is cited, and a result with any gap escalates to a human
    (P-06, routed under rule R8 by the calling surface).
    """

    def __init__(self, audit: AuditSinkPort, *, tracer: ObservabilityTracerPort) -> None:
        self._audit = audit
        # REQUIRED, and deliberately not an optional with a no-op default. A default would let a
        # new surface construct a service that emits nothing, and the deployment would look
        # traced because the exporter was bound. A surface that forgets the tracer fails to
        # construct instead, which is a failure somebody sees.
        self._tracer = tracer

    def assess(self, graph: ObligationGraph, *, scope: str, actor: str) -> CoverageAssessment:
        """Assess the register's coverage and record the audit event.

        The whole path runs inside one span, and its attributes are STRUCTURAL only: the
        action and the actor. Never the caller-supplied scope string, never a gap's subject
        or detail and never an obligation's text. A trace backend is not the WORM audit
        trail: it has no redaction stage, a wider read audience and no retention rule
        written against a regulator's requirement, so anything content-shaped that reaches
        a span has left the boundary the audit envelope exists to hold, and it has left it
        silently.
        """
        with self._tracer.span(_ASSESS_SPAN, action="assess_coverage", actor=actor):
            report = compute_coverage(graph)
            severity = worst_coverage_severity(report)
            has_gaps = bool(report.gaps)
            decision = Decision.ESCALATED if has_gaps else Decision.ALLOWED
            counts = report.counts
            summary = self._summary(scope, counts, report)
            gaps = tuple(
                GapView(kind=g.kind.value, subject=g.subject, detail=g.detail) for g in report.gaps
            )
            citations = _gap_citations(report.gaps) or self._scope_citation(graph)

            # Redaction happens at the boundary, in `AuditEvent.__post_init__`, not here. This
            # comment used to claim a masking step the code never performed, and the claim was
            # wrong twice over: the summary is only PARTLY engine-derived, because it opens with
            # `scope`, which is caller-supplied free text off `CoverageRequest`. A planted
            # identifier in the scope was persisted verbatim in the WORM record. The rule is
            # unconditional, so the type now enforces it rather than each call site remembering.
            self._audit.record(
                AuditEvent(
                    action="coverage_assessment",
                    actor=actor,
                    decision=decision,
                    severity=severity,
                    redacted_summary=summary,
                    citations=citations,
                    timestamp=utcnow(),
                )
            )

            return CoverageAssessment(
                scope=scope,
                subject=scope,
                severity=severity,
                decision=decision,
                summary=summary,
                requires_human_review=has_gaps,
                counts=counts,
                orphan_controls=report.orphan_controls,
                stale_edges=report.stale_edges,
                gaps=gaps,
                citations=citations,
            )

    @staticmethod
    def _summary(scope: str, counts: tuple[tuple[str, int], ...], report: CoverageReport) -> str:
        rendered = ", ".join(f"{name}={value}" for name, value in counts)
        return (
            f"{scope}: {rendered}; orphan_controls={len(report.orphan_controls)}; "
            f"stale_edges={len(report.stale_edges)}"
        )

    @staticmethod
    def _scope_citation(graph: ObligationGraph) -> tuple[Citation, ...]:
        for obligation in graph.obligations:
            return (_kernel_citation_to_envelope(obligation.citation),)
        return ()


# --------------------------------------------------------------------------- #
# Graph operations (maker-checker acceptance, horizon staleness)
# --------------------------------------------------------------------------- #
def accept_all_proposals(graph: ObligationGraph) -> ObligationGraph:
    """Accept every proposed edge (the maker-checker acceptance, applied wholesale).

    Real acceptance is per-edge and human-driven; this is the deterministic convenience the demo
    and the offline surfaces use to move a seeded graph from proposals to an accepted register.
    Rejected edges are left untouched.
    """

    def accept(edge: Edge) -> Edge:
        return edge.accepted() if edge.status is EdgeStatus.PROPOSED else edge

    return graph.map_edges(accept)


def apply_change_feed(graph: ObligationGraph, moved_source_ids: tuple[str, ...]) -> ObligationGraph:
    """Propagate horizon-driven staleness: mark every edge from a moved source stale.

    This is the deterministic half of the Rsk1 horizon change-feed integration: a change item
    names the source ids that moved, and the kernel marks the affected obligations' mapping edges
    stale, which drops them from coverage until a reviewer re-accepts.
    """
    return graph.mark_stale_for_moved_sources(moved_source_ids)


# --------------------------------------------------------------------------- #
# The seed register (obviously fictional; the offline system-of-record content)
# --------------------------------------------------------------------------- #
def _cit(source_id: str, locator: str, title: str) -> KitCitation:
    return KitCitation(
        source_id=source_id,
        locator=locator,
        title=title,
        url=f"https://reg.example/{source_id}#{locator}",
    )


def _obl(
    obl_id: str, text: str, source_id: str, locator: str, owner: str, title: str
) -> Obligation:
    return Obligation(
        id=obl_id,
        title=title,
        text=text,
        owner=owner,
        citation=_cit(source_id, locator, title),
        effective_from=date(2026, 1, 1),
    )


def _control(slug: str, title: str) -> Node:
    return Node(
        ref=NodeRef(NodeKind.CONTROL, slug), title=title, citations=(_cit(slug, "spec", title),)
    )


def _policy(slug: str, title: str) -> Node:
    return Node(ref=NodeRef(NodeKind.POLICY, slug), title=title)


def _evidence(slug: str, title: str) -> Node:
    return Node(ref=NodeRef(NodeKind.EVIDENCE, slug), title=title)


def _edge(src: NodeRef, dst: NodeRef, kind: EdgeKind, *, status: EdgeStatus, source: str) -> Edge:
    return Edge(
        src=src,
        dst=dst,
        kind=kind,
        status=status,
        citations=(_cit(source, "map", "mapping rationale"),),
    )


def seed_graph() -> ObligationGraph:
    """The offline system-of-record content: a fictional MAS/HKMA obligation register.

    Deliberately exercises every coverage state so a surface, the eval oracle and the demo all
    have a covered obligation, a partial one, an uncovered one, an orphan control and (after a
    change feed is applied) a stale mapping to show. All parties and instruments are fictional.
    """
    obligations = (
        _obl(
            "obl-residency",
            "Customer records must be stored within the home jurisdiction.",
            "mas-trm.example",
            "s3.1",
            "cloud-controls-office",
            "Data residency",
        ),
        _obl(
            "obl-encryption",
            "Customer data must be encrypted at rest and in transit.",
            "mas-trm.example",
            "s4.2",
            "ciso-office",
            "Encryption of customer data",
        ),
        _obl(
            "obl-incident",
            "Material incidents must be reported to the regulator within one hour.",
            "hkma-or.example",
            "s6.1",
            "operational-resilience-office",
            "Incident notification",
        ),
    )
    nodes = (
        _policy("pol-cloud", "Cloud Controls Policy"),
        _control("ctrl-residency", "Resource-location organization policy"),
        _control("ctrl-cmek", "Customer-managed encryption keys"),
        _control("ctrl-kms", "Key management configuration"),
        _control("ctrl-orphan", "Legacy control with no owning obligation"),
        _evidence("ev-residency", "Security Command Center residency finding"),
        _evidence("ev-cmek", "Security Command Center CMEK finding"),
    )
    a = EdgeStatus.ACCEPTED
    p = EdgeStatus.PROPOSED
    obl = NodeKind.OBLIGATION
    pol = NodeKind.POLICY
    ctrl = NodeKind.CONTROL
    ev = NodeKind.EVIDENCE
    edges = (
        # residency: obligation -> policy -> control -> evidence, all accepted -> COVERED
        _edge(
            NodeRef(obl, "obl-residency"),
            NodeRef(pol, "pol-cloud"),
            EdgeKind.OBLIGATION_TO_POLICY,
            status=a,
            source="mas-trm.example",
        ),
        _edge(
            NodeRef(pol, "pol-cloud"),
            NodeRef(ctrl, "ctrl-residency"),
            EdgeKind.POLICY_TO_CONTROL,
            status=a,
            source="pol-cloud",
        ),
        _edge(
            NodeRef(ctrl, "ctrl-residency"),
            NodeRef(ev, "ev-residency"),
            EdgeKind.CONTROL_TO_EVIDENCE,
            status=a,
            source="ctrl-residency",
        ),
        # encryption: two controls, only one evidenced -> PARTIAL
        _edge(
            NodeRef(obl, "obl-encryption"),
            NodeRef(ctrl, "ctrl-cmek"),
            EdgeKind.OBLIGATION_TO_CONTROL,
            status=a,
            source="mas-trm.example",
        ),
        _edge(
            NodeRef(ctrl, "ctrl-cmek"),
            NodeRef(ev, "ev-cmek"),
            EdgeKind.CONTROL_TO_EVIDENCE,
            status=a,
            source="ctrl-cmek",
        ),
        _edge(
            NodeRef(obl, "obl-encryption"),
            NodeRef(ctrl, "ctrl-kms"),
            EdgeKind.OBLIGATION_TO_CONTROL,
            status=a,
            source="mas-trm.example",
        ),
        # incident: only a proposed mapping, never accepted -> UNCOVERED
        _edge(
            NodeRef(obl, "obl-incident"),
            NodeRef(ctrl, "ctrl-residency"),
            EdgeKind.OBLIGATION_TO_CONTROL,
            status=p,
            source="hkma-or.example",
        ),
        # ctrl-orphan has no incoming mapping -> ORPHAN
    )
    return ObligationGraph(obligations=obligations, nodes=nodes, edges=edges)
