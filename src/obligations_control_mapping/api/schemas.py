"""API request/response schemas (Pydantic) mapped to/from the pure-domain models."""

from __future__ import annotations

from pydantic import BaseModel

from ..domain.models import TriageResult
from ..domain.narration import NarratedNote
from ..domain.obligations import CoverageAssessment


class TriageRequest(BaseModel):
    subject: str
    text: str


class CitationModel(BaseModel):
    source_id: str
    title: str
    snippet: str = ""


class TriageResponse(BaseModel):
    subject: str
    severity: str
    decision: str
    summary: str
    requires_human_review: bool
    #: Where the escalation WENT (rule R8): the Hrz7 review id, or the local queue reference.
    #: Empty only when the result did not escalate. A caller can tell a routed escalation from
    #: a flag that stopped here, which is the whole point of the rule.
    review_ref: str = ""
    citations: list[CitationModel] = []

    @classmethod
    def from_domain(cls, result: TriageResult, *, review_ref: str = "") -> TriageResponse:
        return cls(
            subject=result.subject,
            severity=result.severity.value,
            decision=result.decision.value,
            summary=result.summary,
            requires_human_review=result.requires_human_review,
            review_ref=review_ref,
            citations=[
                CitationModel(source_id=c.source_id, title=c.title, snippet=c.snippet)
                for c in result.citations
            ],
        )


class CoverageRequest(BaseModel):
    """Assess the obligation register's coverage for a scope.

    ``accept_proposals`` applies the maker-checker acceptance to every pending proposal before
    assessing (the "accept the mappings" action). ``moved_sources`` names source ids that a
    horizon change feed reports as moved, so their mappings are marked stale before assessment.
    """

    scope: str = "obligation register"
    accept_proposals: bool = False
    moved_sources: list[str] = []


class GapModel(BaseModel):
    kind: str
    subject: str
    detail: str


class CoverageResponse(BaseModel):
    scope: str
    severity: str
    decision: str
    summary: str
    requires_human_review: bool
    #: The engine-owned coverage counts by band, as (name, value) pairs.
    counts: dict[str, int] = {}
    orphan_controls: list[str] = []
    stale_edges: list[str] = []
    gaps: list[GapModel] = []
    #: The model-narrated (or deterministically fallen-back) remediation note. Grounded: every
    #: figure in it comes from the engine, never the model.
    note: str = ""
    note_model_authored: bool = False
    #: Where the escalation WENT (rule R8): the Hrz7 review id or the local queue reference.
    review_ref: str = ""
    citations: list[CitationModel] = []

    @classmethod
    def from_domain(
        cls,
        assessment: CoverageAssessment,
        *,
        note: NarratedNote,
        review_ref: str = "",
    ) -> CoverageResponse:
        return cls(
            scope=assessment.scope,
            severity=assessment.severity.value,
            decision=assessment.decision.value,
            summary=assessment.summary,
            requires_human_review=assessment.requires_human_review,
            counts=dict(assessment.counts),
            orphan_controls=list(assessment.orphan_controls),
            stale_edges=list(assessment.stale_edges),
            gaps=[
                GapModel(kind=g.kind, subject=g.subject, detail=g.detail) for g in assessment.gaps
            ],
            note=note.text,
            note_model_authored=note.model_authored,
            review_ref=review_ref,
            citations=[
                CitationModel(source_id=c.source_id, title=c.title, snippet=c.snippet)
                for c in assessment.citations
            ],
        )


class HealthResponse(BaseModel):
    status: str
    profile: str
    region: str
    #: Provenance the UI banner states on every page: where the runtime sits and which model
    #: answers. Both are read off the service because the browser cannot know either.
    runtime: str = "local"  # "gcp" | "local"
    generator_model: str = "deterministic-offline-stub"
