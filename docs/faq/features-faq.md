# Features FAQ

For a product owner, a risk lead or a delivery manager deciding what this system does, what it
refuses to do, and where its responsibility ends.

### What does it actually do?

It is the single system of record for the obligation to policy to control to evidence graph, and
the engine that reads a verdict out of that graph. Given a register, `domain/obligations.py`
computes coverage over the shared `obligation-register-kit` kernel (`compute_coverage`) and turns
the kernel's `CoverageReport` into a consequential `CoverageAssessment`: which obligations are
covered, which controls are orphaned, which mappings have gone stale, the worst severity across
the findings, whether a human must look, and the provenance for each claim. `domain/narration.py`
then asks the model for a short gap-remediation note that restates those figures.

### What makes a coverage number defensible?

Three rules in the engine, all pure code:

- **Only accepted, non-stale edges count.** A proposed mapping does not become coverage until the
  maker-checker acceptance step (`accept_all_proposals`) turns it into an accepted edge, and
  `apply_change_feed` marks an edge stale when the regulatory change feed moves the obligation
  underneath it. An institution cannot improve its coverage figure by proposing mappings.
- **The severity is computed, not asserted.** `worst_coverage_severity` maps the gap findings to a
  band, and the band is what sets `requires_human_review`.
- **The graph kernel is shared and stdlib-only.** The counting lives in `obligation-register-kit`,
  so the same arithmetic is used by every consumer rather than reimplemented per repo.

The model plays no part in any of it.

### What is the model allowed to say?

Only a short remediation note that restates figures the engine produced, and it is held to two
hard rules before the note is allowed out: the reply must parse as JSON with the requested keys
(malformed output is discarded, never repaired), and every integer in the note must be one the
engine actually produced (a note that invents a figure is discarded). When a note is discarded, a
deterministic note built from the engine facts is used instead, and the service reports which
path produced it, so the eval and the demo can tell them apart. See
[`../model-card.md`](../model-card.md).

### What will it refuse to do?

- **It will not serve another tenant's register.** `authorize_register_access` raises
  `CrossTenantError` rather than returning an empty result, so a caller cannot probe for another
  register by asking politely.
- **It will not count an unaccepted or stale mapping.**
- **It will not auto-execute a consequential result.** A consequential assessment sets
  `requires_human_review` and is ROUTED to the Hrz7 console in the same call that produced it
  (rule R8).
- **It will not answer without provenance.** Every claim carries a `Citation`.

### Which surfaces expose it?

The FastAPI app (`POST /v1/coverage` for the register assessment, `POST /v1/triage` for the
single-case decision), the argparse CLI, the agent tools (`triage_case`, `verify_audit_trail`,
advertised on the A2A card at `/.well-known/agent-card.json`), the embeddable `ui/`
micro-frontend, and the eval harness. Each routes escalations in the same call, so rule R8 does
not hold on some surfaces and not others.

Note that the repo carries two verticals side by side today: the Rgc7 coverage engine
(`domain/obligations.py`, `domain/narration.py`, `/v1/coverage`) and the template's generic triage
service (`domain/triage_service.py`, `/v1/triage`, the CLI and the agent tool). The triage path is
scaffolding the render started from, not the reason this system exists.

### What does this repo own, and what does it integrate?

| Concern | Owner | How this repo touches it |
|---|---|---|
| The obligation to policy to control to evidence graph | **this repo (Rgc7)** | it IS the system of record. Consumers read from here rather than keeping a second register. |
| The graph arithmetic | the shared `obligation-register-kit` | imported, not reimplemented. |
| The regulatory corpus and change horizon | **Rsk1** compliance assistant | this repo consumes change records in `apply_change_feed`; it does not track the corpus. |
| Agent discovery and entitlements | **Hrz3** agent registry | this agent publishes a card; the registry owns discovery. |
| Model and agent promotion | **Hrz4** AI quality and model risk | `eval/run_eval.py --mode gate` asks Hrz4; the offline smoke mode never promotes. |
| Traces and the immutable audit sink | **Hrz5** agent observability | `AuditSinkPort` and `ObservabilityTracerPort`. |
| Human review and maker-checker | **Hrz7** human review console | `ReviewRouterPort` over the shared `review-kit`. This repo produces escalations; it does not render a queue. |
| Prompt-injection defence and output filtering | **Hrz1** agent guardrail gateway | **not wired today.** It becomes mandatory the moment untrusted free text reaches the narrator (rule R1). |
| Grounded retrieval over an enterprise corpus | **Hrz2** enterprise knowledge base | not wired; this service reasons over its own graph rather than over documents. |

### Can I demo it without a cloud project?

Yes, and the demo is code rather than a deck. `make demo` runs a presenter-paced walkthrough over
eight steps (opened, routine, escalation, redaction, review queue, audit, tamper, portability) on
its own loopback server; `make demo-selftest` runs the same arc headless and asserts every
narrated claim, so a claim that stops being true fails a build rather than a meeting;
`make demo-static` renders the same audit-first panels to static HTML for screenshots.

### What is not built yet?

The honest list is [`../practices-audit.md`](../practices-audit.md) and the `TODO (repo owner)`
rows in [`../../COMPLIANCE.md`](../../COMPLIANCE.md). The three that matter most for a production
decision: a durable graph store behind a port (offline the seed register lives in process), the
Hrz1 guardrail binding, and registering this repo's metric bundle with Hrz4 so `--mode gate` has
an authority to ask.
