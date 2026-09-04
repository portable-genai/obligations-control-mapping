# Model card: Obligations to Control Mapping (`obligations-control-mapping`)

This is a STARTER model card. It records the model boundary as built and the controls that must be
completed before a managed deployment. The deterministic coverage engine is the system of record;
the model is a bounded, replaceable component that writes one paragraph.

## What the model does, and does not do

- **Does**: write a short gap-remediation note that restates a `CoverageAssessment` the engine has
  ALREADY computed. It receives a system instruction plus a facts block of engine-owned figures
  (`domain/narration.py:build_request`) and returns JSON.
- **Does NOT**: produce any coverage count, gap finding, severity band or escalation decision.
  Coverage comes from `compute_coverage` in the shared stdlib-only `obligation-register-kit`; the
  severity comes from `worst_coverage_severity` and the review flag from `CoverageAssessment`, all
  in `domain/obligations.py`. With the local stub generation adapter bound, every consequential
  field is identical, so a model change cannot move a figure.

## Boundary and validation

- The model is reachable through exactly one port, `ports/generation.py`. There is no second model
  seam in the repo.
- The reply is held to two hard rules before it is allowed out (`domain/narration.py`):
  **schema validation**, so output that is not JSON with the requested keys is discarded rather
  than repaired; and **groundedness**, so every integer in the note must be one the engine
  produced (`grounded_integers`, `note_is_grounded`). A note that invents a figure is discarded.
- When a note is discarded, `fallback_text` builds a deterministic note purely from the engine
  facts, so a surface always has a grounded sentence and never a hallucinated one. The service
  reports which path produced the note, so the eval and the demo can tell them apart.
- The parsing and groundedness checks are module-level pure functions rather than private methods,
  deliberately: the `narration_groundedness` eval metric measures the RAW model output through the
  very same contract the service enforces. A metric that watched only the already-filtered service
  output could never go red.
- Personal data is masked before the audit write, before an outbound review payload and before a
  tool result can enter a model's context (`domain/pii.py`).
- Every consequential result sets `requires_human_review` and is routed to `human-review-console` (rule R8) in the
  same call; nothing auto-executes.

## Adapters and profiles

| Profile | Generation adapter | Behaviour |
|---|---|---|
| `local` | `adapters/local/generation.py` | Deterministic stub: restates the request's engine facts as a JSON note. Grounded by construction, SDK-free, no network. A silent empty return would let a producer ship the narration seam unwired, so it emits a real, inspectable note. |
| `gcp` | `adapters/gcp/generation.py` | Gemini via `google.generativeai`, imported lazily inside the method. Model id pinned in the adapter as `_MODEL`, currently `gemini-3.5-flash`, with `response_mime_type=application/json`, `temperature=0.2` and a caller-supplied `max_output_tokens`. |
| `onprem` | `adapters/onprem/generation.py` | Fail-fast placeholder: refuses at call time rather than pretending to narrate, so a placeholder never becomes a silent no-op on the one path where an empty answer would look like a working narrator. |

## Remaining controls (TODO, repo owner)

- **Model id, version and region** (P-07): `gemini-3.5-flash` is a pinned default in the adapter,
  not a confirmed deployment decision. Gemini model ids are regional and an unavailable one fails
  at call time rather than at boot, so confirm the id is served in your region, pin the exact
  version, and record it here.
- **Budget, rate limit and a kill switch** (P-10, P-11): `max_output_tokens` is per request and
  there is no per-tenant token budget, no request rate limit, and no switch that forces
  deterministic-only operation. The fallback path already exists, since a discarded note yields
  the deterministic text, but nothing yet lets an operator disable the model deliberately.
- **Evaluation of the live model**: the offline eval scores the deterministic pipeline with the
  stub adapter against the golden set. Add a managed-profile run, registered with the `model-quality-gate`
  promotion gate (P-08, rule R5), that scores `narration_groundedness` with the real model bound.
- **Prompt-injection screening** (rule R1): the `agent-guardrail-gateway` is not bound. Screen any
  untrusted free text that reaches the facts block, and fail closed to deterministic-only when the
  screen is unavailable. The exposure is small today, because the facts block carries engine
  integers rather than free text, and it grows the moment obligation titles or control
  descriptions from an external register are passed through.
- **Reasoning trace**: the audit record carries the validated note and its provenance, not the
  prompt and reply pair. `COMPLIANCE.md` P-07 records that as owed.

Until these are complete the system is safe to run offline (deterministic engine plus the stub
adapter) and the managed model path is not production-cleared.
