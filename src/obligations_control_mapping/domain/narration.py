"""The narration service: the model narrates the engine's numbers, and never produces them.

Given a :class:`~.obligations.CoverageAssessment` (already computed by the deterministic engine),
this asks the generation port for a short gap-remediation note, then holds that note to two hard
rules before it is allowed out:

* **Schema validation, discard on failure.** The model must return JSON with the requested keys.
  Malformed output, or output missing a key, is discarded, not repaired.
* **Groundedness, discard on failure.** Every integer in the note must be one the engine actually
  produced (the request's ``facts``). A note that invents a figure is discarded.

When a model note is discarded, a deterministic note built purely from the engine facts is used
instead, so a surface always has a grounded sentence and never a hallucinated one. The service
reports which path produced the note, so the eval and the demo can tell them apart.

Rule R1: the guardrail screens BOTH directions of the generation call. INPUT, before the model
is called: the caller-supplied ``scope`` on its own (it is the one caller-controlled field the
prompt carries), then the PROMPT as sent, built from the screened scope. OUTPUT: the model's raw
text, before it is parsed, grounded or returned. The text each screen hands back is the text used
from then on, exactly as given. A block, and a guardrail that raised instead of deciding (its
backend errored or timed out: fail closed), is audited ``Decision.BLOCKED`` and the model's
note is never used, not even in part. Narration is optional by design, so the surface then
falls back to the fixed, engine-built note rather than refusing the already-computed and
already-audited assessment; the BLOCKED record is what says a refusal happened.

The request-building, parsing and groundedness checks are module-level pure functions rather than
private methods, so the eval can measure the RAW model output through the very same contract the
service enforces (a groundedness metric that watched only the already-filtered service output
could never go red). Pure stdlib: the model is reached only through the injected port.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace

from ..ports.audit import AuditSinkPort
from ..ports.generation import GenerationPort, GenerationRequest
from ..ports.guardrail import GuardrailPort
from .kernel import AuditEvent, Decision, Direction, GuardrailVerdict, utcnow
from .obligations import CoverageAssessment

__all__ = [
    "NARRATION_ACTION",
    "NarratedNote",
    "NarrationService",
    "build_request",
    "fallback_text",
    "grounded_integers",
    "note_is_grounded",
    "parse_note",
]

_INT = re.compile(r"-?\d+")

#: The audit action a guardrail refusal of the narration is recorded under.
NARRATION_ACTION = "coverage_narration"

_SYSTEM = (
    "You are a compliance analyst assistant. You restate the coverage figures you are given as a "
    "short remediation note. You never invent a number: use only the figures in the facts block."
)


@dataclass(frozen=True, slots=True)
class NarratedNote:
    """A gap-remediation note plus how it was produced."""

    text: str
    model_authored: bool
    grounded: bool
    #: True when the guardrail refused either direction of the call (or could not decide), so
    #: the fixed note is shown and a BLOCKED audit record says why.
    guardrail_blocked: bool = False


class _Blocked(Exception):
    """Internal: a guardrail screen refused (already audited). Never leaves this module."""


def grounded_integers(facts: tuple[tuple[str, str], ...]) -> set[str]:
    """Every integer token that appears in the engine-owned facts (the grounded number set)."""
    allowed: set[str] = set()
    for _key, value in facts:
        allowed.update(_INT.findall(value))
    return allowed


def note_is_grounded(text: str, facts: tuple[tuple[str, str], ...]) -> bool:
    """True when every integer in ``text`` is one the engine facts contain."""
    allowed = grounded_integers(facts)
    return all(token in allowed for token in _INT.findall(text))


def _facts(assessment: CoverageAssessment) -> tuple[tuple[str, str], ...]:
    """The engine-owned figures the note may cite, and nothing else grounds it."""
    counts = {name: value for name, value in assessment.counts}
    return (
        ("covered", str(counts.get("covered", 0))),
        ("partial", str(counts.get("partial", 0))),
        ("uncovered", str(counts.get("uncovered", 0))),
        ("orphan_controls", str(len(assessment.orphan_controls))),
        ("stale_edges", str(len(assessment.stale_edges))),
        ("severity", assessment.severity.value),
    )


def build_request(assessment: CoverageAssessment, *, scope: str | None = None) -> GenerationRequest:
    """The exact narration request the service sends, exposed so the eval can reuse it.

    The ``facts`` block carries the engine's numbers; the prompt instructs the model to restate
    ONLY those. The same request object is scored for groundedness by the service (on the returned
    note) and by the eval (on the raw model output), so the two can never drift. ``scope`` is the
    scope as the guardrail's INPUT screen handed it back; it defaults to the assessment's own.
    """
    facts = _facts(assessment)
    block = "\n".join(f"{key}={value}" for key, value in facts)
    prompt = (
        f"Scope: {assessment.scope if scope is None else scope}\n"
        f"Facts (use ONLY these numbers):\n{block}\n"
        'Return JSON of the form {"note": "<one sentence>"}.'
    )
    # Narration is drafting: it samples freely (no temperature sent). Every number in the note is
    # still checked against the engine's facts, and an ungrounded note is discarded.
    return GenerationRequest(
        system=_SYSTEM, prompt=prompt, facts=facts, response_keys=("note",), temperature=None
    )


def parse_note(text: str) -> str | None:
    """Parse the model's raw text into the ``note`` string, or ``None`` if it is not valid."""
    try:
        parsed = json.loads(text.strip())
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    note = parsed.get("note")
    if not isinstance(note, str) or not note.strip():
        return None
    return note.strip()


def fallback_text(facts: tuple[tuple[str, str], ...]) -> str:
    """A deterministic, grounded-by-construction note built purely from the engine facts."""
    values = dict(facts)
    return (
        f"Coverage stands at {values.get('covered', '0')} covered, "
        f"{values.get('partial', '0')} partial and {values.get('uncovered', '0')} uncovered "
        f"obligations, with {values.get('orphan_controls', '0')} orphan control(s) and "
        f"{values.get('stale_edges', '0')} stale mapping(s) to review."
    )


class NarrationService:
    """Draft a grounded, guardrail-screened gap-remediation note for a coverage assessment."""

    def __init__(
        self, generation: GenerationPort, *, guardrail: GuardrailPort, audit: AuditSinkPort
    ) -> None:
        self._generation = generation
        # Both REQUIRED, with no permissive default: a surface that forgot the guardrail would
        # otherwise send caller text to the model unscreened and look finished (rule R1).
        self._guardrail = guardrail
        self._audit = audit

    def narrate(self, assessment: CoverageAssessment, *, actor: str) -> NarratedNote:
        facts = _facts(assessment)
        try:
            # 1) INPUT, before the model is called: the caller-supplied field, then the prompt
            # built from it AS SENT, because the prompt is the string the model actually reads.
            scope = self._screen(assessment.scope, Direction.INPUT, assessment, actor, None)
            request = build_request(assessment, scope=scope)
            prompt = self._screen(request.prompt, Direction.INPUT, assessment, actor, scope)
            request = replace(request, prompt=prompt)
        except _Blocked:
            return _fallback(facts, blocked=True)

        try:
            response = self._generation.generate(request)
        except Exception:  # noqa: BLE001 - a narration failure must degrade, never crash a decision
            return _fallback(facts)

        try:
            # 2) OUTPUT, before the model's text is parsed, grounded, used or returned.
            text = self._screen(response.text, Direction.OUTPUT, assessment, actor, scope)
        except _Blocked:
            return _fallback(facts, blocked=True)

        note = parse_note(text)
        if note is None or not note_is_grounded(note, request.facts):
            # Schema-invalid or ungrounded: discard the model output, never repair it.
            return _fallback(facts)
        return NarratedNote(text=note, model_authored=True, grounded=True)

    def _screen(
        self,
        text: str,
        direction: Direction,
        assessment: CoverageAssessment,
        actor: str,
        scope: str | None,
    ) -> str:
        """Screen one text in one direction; return the text to use from here on, or refuse.

        The returned text is the verdict's ``sanitized_text`` exactly as given, including an
        empty string. A block, and a guardrail that raised instead of deciding, both fail closed:
        a BLOCKED record is written and :class:`_Blocked` is raised. ``scope`` is what the record
        may name, and it is ``None`` until the scope has itself passed the INPUT screen.
        """
        try:
            verdict: GuardrailVerdict = self._guardrail.screen(text, direction)
        except Exception as exc:  # noqa: BLE001 - an undecided screen is a refusal (fail closed)
            reason = f"guardrail unavailable ({type(exc).__name__})"
            self._audit_blocked(assessment, actor, direction, reason, scope)
            raise _Blocked(reason) from exc
        if not verdict.allowed or verdict.sanitized_text is None:
            reason = verdict.reason or f"narration {direction.value} blocked by guardrail"
            self._audit_blocked(assessment, actor, direction, reason, scope)
            raise _Blocked(reason)
        return verdict.sanitized_text

    def _audit_blocked(
        self,
        assessment: CoverageAssessment,
        actor: str,
        direction: Direction,
        reason: str,
        scope: str | None,
    ) -> None:
        """Audit a guardrail refusal of the narration (rule R1/R2).

        Never carries the refused text: only that a refusal happened, in which direction, and
        why, plus the scope once it has itself passed the INPUT screen. A write failure here
        propagates: a refusal the WORM trail cannot hold is not one to absorb silently.
        """
        what = f"{scope}: narration blocked" if scope is not None else "narration blocked"
        self._audit.record(
            AuditEvent(
                action=NARRATION_ACTION,
                actor=actor,
                decision=Decision.BLOCKED,
                severity=assessment.severity,
                redacted_summary=f"{what} ({direction.value}): {reason}",
                citations=(),
                timestamp=utcnow(),
            )
        )


def _fallback(facts: tuple[tuple[str, str], ...], *, blocked: bool = False) -> NarratedNote:
    """The fixed, engine-built note, used whenever the model's note is not."""
    return NarratedNote(
        text=fallback_text(facts), model_authored=False, grounded=True, guardrail_blocked=blocked
    )
