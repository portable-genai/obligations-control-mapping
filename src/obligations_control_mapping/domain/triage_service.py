"""The deterministic triage service: pure decision, redact-before-audit, soft escalation.

The consequential decision (the severity band and whether to escalate) is pure stdlib and
replayable; an LLM would only narrate, never produce the band. PII is redacted BEFORE anything
is written to the audit sink (R1/P-04), every result carries a citation, and a consequential
result escalates softly to a human (P-06) rather than auto-executing.
"""

from __future__ import annotations

from pii_kit import redact

from ..ports.audit import AuditSinkPort
from ..ports.observability import ObservabilityTracerPort
from .kernel import AuditEvent, Citation, Decision, Severity, utcnow
from .models import TriageInput, TriageResult
from .pii import PII_PATTERNS

# Deterministic keyword bands (the vertical IP; swap for your policy). Ordered most severe first.
_SEVERITY_KEYWORDS: tuple[tuple[Severity, frozenset[str]], ...] = (
    (Severity.CRITICAL, frozenset({"sanction", "terrorism", "fraud"})),
    (Severity.HIGH, frozenset({"breach", "leak", "urgent", "weapon"})),
    (Severity.MEDIUM, frozenset({"complaint", "dispute", "delay"})),
)

#: One span per triaged case. Structural attributes only: see :meth:`TriageService.triage`.
_TRIAGE_SPAN = "obligations.triage"


class TriageService:
    """Score a case into a severity band and record an already-redacted audit event."""

    def __init__(self, audit: AuditSinkPort, *, tracer: ObservabilityTracerPort) -> None:
        self._audit = audit
        # REQUIRED, and deliberately not an optional with a no-op default. A default would let a
        # new surface construct a service that emits nothing, and the deployment would look
        # traced because the exporter was bound. A surface that forgets the tracer fails to
        # construct instead, which is a failure somebody sees.
        self._tracer = tracer

    def triage(self, case: TriageInput, *, actor: str) -> TriageResult:
        """Triage one case and record the already-redacted audit event.

        The whole path runs inside one span, and its attributes are STRUCTURAL only: the
        action and the actor. Never the case subject, never the free-text description and
        never a finding's wording. A trace backend is not the WORM audit trail: it has no
        redaction stage, a wider read audience and no retention rule written against a
        regulator's requirement, so anything content-shaped that reaches a span has left the
        boundary the ``redact`` call below exists to hold, and it has left it silently.
        """
        with self._tracer.span(_TRIAGE_SPAN, action="triage", actor=actor):
            severity = self._severity(case.text)
            escalate = severity in (Severity.HIGH, Severity.CRITICAL)
            decision = Decision.ESCALATED if escalate else Decision.ALLOWED
            summary = f"{case.subject}: triaged {severity.value}"
            citation = Citation(
                source_id=f"case:{case.subject}",
                title="Case description",
                snippet=case.text[:80],
            )

            # Redact BEFORE the audit write: the raw identifiers never reach the WORM record.
            self._audit.record(
                AuditEvent(
                    action="triage",
                    actor=actor,
                    decision=decision,
                    severity=severity,
                    redacted_summary=redact(f"{summary} :: {case.text}", PII_PATTERNS),
                    citations=(citation,),
                    timestamp=utcnow(),
                )
            )

            return TriageResult(
                subject=case.subject,
                severity=severity,
                decision=decision,
                summary=summary,
                requires_human_review=escalate,
                citations=(citation,),
            )

    @staticmethod
    def _severity(text: str) -> Severity:
        lowered = text.lower()
        for severity, keywords in _SEVERITY_KEYWORDS:
            if any(word in lowered for word in keywords):
                return severity
        return Severity.LOW
