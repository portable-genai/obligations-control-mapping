#!/usr/bin/env python3
"""Evaluation gate for Obligations to Control Mapping (Rgc7).

Two named layers via ``--mode`` (the scaffold is ``agent_eval_kit.eval_main``):

* **smoke** (default) - the offline pre-merge check CI runs on every change: it drives the real
  ``TriageService`` against a golden set with SDK-free local adapters and scores two metrics.
* **gate** - the promotion verdict from the shared Hrz4 authority (requires the ``gcp``
  profile), via ``agent_eval_kit.PromotionGateClient``.

Exit is ``0`` iff every metric meets its threshold (and, in gate mode, the authority agrees).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_eval_kit import EvalMetricResult, EvalReport, PromotionGateClient, eval_main
from obligation_register import ObligationGraph
from pii_kit import pack_leak

from obligations_control_mapping.adapters.local.audit import (
    LocalAuditAdapter,
)
from obligations_control_mapping.adapters.local.generation import (
    LocalGenerationAdapter,
)
from obligations_control_mapping.adapters.local.tracer import (
    LocalNoopTracerAdapter,
)
from obligations_control_mapping.config import (
    Settings,
)
from obligations_control_mapping.domain.models import (
    TriageInput,
)
from obligations_control_mapping.domain.narration import (
    build_request,
    note_is_grounded,
    parse_note,
)
from obligations_control_mapping.domain.obligations import (
    AssessmentService,
    accept_all_proposals,
    apply_change_feed,
    seed_graph,
)
from obligations_control_mapping.domain.pii import (
    PII_PATTERNS,
)
from obligations_control_mapping.domain.triage_service import (
    TriageService,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = _REPO_ROOT / "eval" / "datasets" / "golden_cases.jsonl"
#: The obligation-graph coverage oracle: hand-computed coverage per register scenario. Scored by
#: the engine (coverage_accuracy) and used to ground the narrator (narration_groundedness).
COVERAGE_DATASET = _REPO_ROOT / "eval" / "datasets" / "coverage_oracle.jsonl"

THRESHOLDS: dict[str, float] = {
    "decision_accuracy": 0.80,
    "pii_safety": 0.99,
    "coverage_accuracy": 0.99,
    "narration_groundedness": 0.99,
}
#: The registered Hrz4 metric bundle for this vertical (Hrz4 owns the metrics + thresholds).
_BUNDLE = "obligations-control-mapping"


def _load(path: Path) -> list[dict[str, str]]:
    cases: list[dict[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cases.append(json.loads(line))
    if not cases:
        raise SystemExit(f"{path}: golden dataset is empty")
    return cases


def _load_scenarios(path: Path) -> list[dict[str, object]]:
    """Load a jsonl oracle whose rows carry mixed types (bools, ints, lists), not just strings."""
    cases: list[dict[str, object]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cases.append(json.loads(line))
    if not cases:
        raise SystemExit(f"{path}: golden dataset is empty")
    return cases


def _mean(scores: list[float]) -> float:
    return round(sum(scores) / len(scores), 4) if scores else 0.0


def audit_texts(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """Every CONTENT-bearing field of every audit row, which is what a leak scan has to read.

    Collecting ``redacted_summary`` and nothing else would name the one field the
    redactor already masks: the metric would ask the redactor whether it had redacted, believe
    the answer, and report a green while the SAME record's citation snippet carries the
    identifier verbatim. Citations travel inside the record, and they carry raw source text in
    ``snippet`` and, routinely, in ``source_id`` (a locator built from the case subject).

    ``actor`` is excluded deliberately: it is the verified principal and an address by design, so
    a blanket scan over a whole row could never go green, and a metric nobody can make green
    gets deleted rather than fixed.
    """
    texts: list[str] = []
    for row in rows:
        texts.append(str(row.get("redacted_summary", "")))
        texts.append(json.dumps(row.get("citations", []), sort_keys=True))
    return texts


def pii_safety(records: Sequence[str], planted: Sequence[str]) -> float:
    """No identifier may survive into an audit record, by the pack rows OR by planted literal.

    Two oracles, because they fail independently: the pack scan uses the same rows the redactor
    masks with (so a redactor that skipped a field is caught), and the planted-literal check
    fires even if a pattern row is broken (so a pack that stopped matching is caught too).
    """
    pack_leaked = any(pack_leak(text, PII_PATTERNS) for text in records)
    literal_leaked = any(token in text for token in planted for text in records)
    return 0.0 if (pack_leaked or literal_leaked) else 1.0


def _scenario_graph(case: dict[str, object]) -> ObligationGraph:
    """Rebuild the register scenario a coverage-oracle row describes."""
    graph = seed_graph()
    if bool(case.get("accept_proposals")):
        graph = accept_all_proposals(graph)
    moved_raw = case.get("moved_sources")
    moved = tuple(str(s) for s in moved_raw) if isinstance(moved_raw, list) else ()
    if moved:
        graph = apply_change_feed(graph, moved)
    return graph


def score_coverage(cases: list[dict[str, object]]) -> tuple[float, float]:
    """Score the engine and the narrator over the coverage oracle.

    Returns ``(coverage_accuracy, narration_groundedness)``. ``coverage_accuracy`` compares the
    deterministic engine's counts, orphan/stale tallies and severity band to the row's INDEPENDENT
    hand-computed oracle. ``narration_groundedness`` runs the RAW local narrator (never the
    already-filtered service output, which could not go red) and scores whether every figure in its
    note is one the engine produced. A hallucinated figure would turn the metric red.
    """
    settings = Settings(profile="local", audit_path=":memory:")
    audit = LocalAuditAdapter(settings)
    service = AssessmentService(audit, tracer=LocalNoopTracerAdapter(settings))
    generation = LocalGenerationAdapter(Settings(profile="local"))

    coverage_scores: list[float] = []
    grounded_scores: list[float] = []
    for case in cases:
        assessment = service.assess(_scenario_graph(case), scope=str(case["id"]), actor="eval-bot")
        counts = dict(assessment.counts)
        matches = (
            counts.get("covered", 0) == int(str(case["expected_covered"]))
            and counts.get("partial", 0) == int(str(case["expected_partial"]))
            and counts.get("uncovered", 0) == int(str(case["expected_uncovered"]))
            and len(assessment.orphan_controls) == int(str(case["expected_orphans"]))
            and len(assessment.stale_edges) == int(str(case["expected_stale"]))
            and assessment.severity.value == str(case["expected_severity"])
        )
        coverage_scores.append(1.0 if matches else 0.0)

        request = build_request(assessment)
        raw = generation.generate(request)
        note = parse_note(raw.text)
        grounded = note is not None and note_is_grounded(note, request.facts)
        grounded_scores.append(1.0 if grounded else 0.0)

    return _mean(coverage_scores), _mean(grounded_scores)


def run_smoke(dataset: Path) -> EvalReport:
    cases = _load(dataset)
    settings = Settings(profile="local", audit_path=":memory:")
    audit = LocalAuditAdapter(settings)
    service = TriageService(audit, tracer=LocalNoopTracerAdapter(settings))

    decision_scores: list[float] = []
    for case in cases:
        result = service.triage(
            TriageInput(subject=case["subject"], text=case["text"]), actor="eval-bot"
        )
        decision_scores.append(1.0 if result.severity.value == case["expected_severity"] else 0.0)

    # pii_safety: no raw identifier may survive into any audit record. `audit_texts` decides
    # WHICH fields count as the record's content; see its docstring for why the summary alone is
    # not the record.
    records = audit_texts(audit.log.read_all())
    planted = [case["planted"] for case in cases if case.get("planted")]

    # The obligation-graph vertical: the deterministic engine against a hand-computed oracle, and
    # the narrator held to the engine's figures. Both run offline (SDK-free local adapters).
    coverage_cases = _load_scenarios(COVERAGE_DATASET)
    coverage_accuracy, narration_groundedness = score_coverage(coverage_cases)

    results = (
        EvalMetricResult.scored(
            "decision_accuracy", _mean(decision_scores), THRESHOLDS["decision_accuracy"]
        ),
        EvalMetricResult.scored(
            "pii_safety", pii_safety(records, planted), THRESHOLDS["pii_safety"]
        ),
        EvalMetricResult.scored(
            "coverage_accuracy", coverage_accuracy, THRESHOLDS["coverage_accuracy"]
        ),
        EvalMetricResult.scored(
            "narration_groundedness",
            narration_groundedness,
            THRESHOLDS["narration_groundedness"],
        ),
    )
    return EvalReport(
        dataset=str(dataset), results=results, n_examples=len(cases) + len(coverage_cases)
    )


def run_gate(dataset: Path) -> tuple[EvalReport, bool]:
    settings = Settings.load()
    if settings.profile != "gcp":
        raise SystemExit(
            "--mode gate is the promotion authority and requires "
            f"OBLIGATIONS_PROFILE=gcp (got {settings.profile!r}); "
            "run --mode smoke for the offline pre-merge check."
        )
    client = PromotionGateClient(
        os.environ.get("OBLIGATIONS_QUALITY_URL", "http://localhost:8084"),
        bundle=_BUNDLE,
        model="gemini-3.5-flash",
    )
    return client.evaluate(str(dataset)), client.gate(str(dataset))


if __name__ == "__main__":
    raise SystemExit(
        eval_main(
            smoke=run_smoke,
            gate=run_gate,
            default_dataset=DEFAULT_DATASET,
            description="Offline / Hrz4 evaluation gate for Rgc7.",
        )
    )
