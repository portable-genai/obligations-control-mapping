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

The request-building, parsing and groundedness checks are module-level pure functions rather than
private methods, so the eval can measure the RAW model output through the very same contract the
service enforces (a groundedness metric that watched only the already-filtered service output
could never go red). Pure stdlib: the model is reached only through the injected port.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..ports.generation import GenerationPort, GenerationRequest
from .obligations import CoverageAssessment

__all__ = [
    "NarratedNote",
    "NarrationService",
    "build_request",
    "fallback_text",
    "grounded_integers",
    "note_is_grounded",
    "parse_note",
]

_INT = re.compile(r"-?\d+")

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


def build_request(assessment: CoverageAssessment) -> GenerationRequest:
    """The exact narration request the service sends, exposed so the eval can reuse it.

    The ``facts`` block carries the engine's numbers; the prompt instructs the model to restate
    ONLY those. The same request object is scored for groundedness by the service (on the returned
    note) and by the eval (on the raw model output), so the two can never drift.
    """
    facts = _facts(assessment)
    block = "\n".join(f"{key}={value}" for key, value in facts)
    prompt = (
        f"Scope: {assessment.scope}\n"
        f"Facts (use ONLY these numbers):\n{block}\n"
        'Return JSON of the form {"note": "<one sentence>"}.'
    )
    return GenerationRequest(system=_SYSTEM, prompt=prompt, facts=facts, response_keys=("note",))


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
    """Draft a grounded gap-remediation note for a coverage assessment."""

    def __init__(self, generation: GenerationPort) -> None:
        self._generation = generation

    def narrate(self, assessment: CoverageAssessment) -> NarratedNote:
        request = build_request(assessment)
        try:
            response = self._generation.generate(request)
        except Exception:  # noqa: BLE001 - a narration failure must degrade, never crash a decision
            return NarratedNote(
                text=fallback_text(request.facts), model_authored=False, grounded=True
            )

        note = parse_note(response.text)
        if note is None or not note_is_grounded(note, request.facts):
            # Schema-invalid or ungrounded: discard the model output, never repair it.
            return NarratedNote(
                text=fallback_text(request.facts), model_authored=False, grounded=True
            )
        return NarratedNote(text=note, model_authored=True, grounded=True)
