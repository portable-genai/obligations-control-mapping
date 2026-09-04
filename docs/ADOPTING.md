# Adopting this repo as your base

This repository (`obligations-control-mapping`, Obligations to Control Mapping) is a **common base** that a bank or other
regulated institution forks to build its own **system of record for the obligation to policy to
control to evidence graph**: the service that answers which obligations are covered, which
controls are orphaned, which mappings have gone stale, and how severe the resulting picture is.
It ships a reusable hexagonal core (a pure-stdlib domain, typed ports, three swappable adapter
profiles, a green offline gate) plus a fully worked coverage vertical over the shared
`obligation-register-kit` that you can keep, reseed, or retune.

This guide is the step-by-step for making it yours. It has two halves: a **mechanical rebrand**
(one script) and the **human decisions** the script cannot make for you.

> Related reading: [`ARCHITECTURE.md`](../ARCHITECTURE.md) (the port table and topology),
> [`CONTRIBUTING.md`](../CONTRIBUTING.md) (adding an adapter, adding a port), the
> [`faq/`](faq/) directory, [`model-card.md`](model-card.md) (the model boundary),
> [`practices-audit.md`](practices-audit.md) (the per-check verdict).

---

## 1. What you keep vs what you rewrite

The core is hexagonal, and the boundary between reusable machinery and this vertical is a
physical module split with an enforced dependency direction (practices-audit check A7).
`domain/kernel.py` owns the vertical-neutral contracts and imports nothing from the vertical;
`domain/models.py` holds this service's own request and result types.

| Layer | Where | For your own register |
|---|---|---|
| **Vertical-neutral machinery** | `domain/kernel.py` (`Citation`, `AuditEvent`, `Severity`, `Decision`, `utcnow`), every Protocol in `ports/`, the container wiring in `config.py` | keep untouched |
| **Graph kernel** | the shared `obligation-register-kit` (`Node`, `Edge`, `EdgeKind`, `EdgeStatus`, `ObligationGraph`, `compute_coverage`, `GapFinding`) | keep untouched, and take upstream releases |
| **Policy (your numbers and rules)** | the severity mapping in `domain/obligations.py` (`worst_coverage_severity`, the `CoverageAssessment` review flag), the staleness rule in `apply_change_feed`, the jurisdiction list in `domain/pii.py`, the metric thresholds in `eval/run_eval.py` | change deliberately (see section 4) |
| **Vertical (the register content)** | the seed register in `domain/obligations.py` (`seed_graph`), the vertical models in `domain/models.py`, the narration prompt in `domain/narration.py`, the local fixtures and the eval golden set | reseed and rewrite for your control library |

If your product is another *graph coverage* service, the hexagon, the three profiles, the
deterministic-verdict pattern, the eval gate and the `human-review-console` review routing transfer directly; you
replace the register content and retune the severity policy.

## 2. Core-vs-adopter-owned files (so upstream merges stay mechanical)

Upstream keeps evolving these; avoid diverging from them so you can pull fixes cleanly:

- **Upstream-owned** (take our changes): `domain/kernel.py`, `ports/`, `tests/contract/`, the
  eval harness mechanics (`eval/run_eval.py`), the CI workflows, the hexagon wiring (`config.py`
  `Container`) and the deploy stack in `infra/terraform/`.
- **Adopter-owned** (yours; expect to edit): `config/settings.yaml` *values*, the seed register
  and every fixture, the severity policy in `domain/obligations.py`, `adapters/onprem/*`, UI
  theming and branding, the golden eval dataset, `infra/terraform/terraform.tfvars`, and the
  regulator crosswalk section of `COMPLIANCE.md`.

Track upstream via git tags; rebase your adopter-owned changes onto each release rather than
merging `main` continuously.

## 3. The mechanical rebrand (one script)

`scripts/rename_fork.py` rewrites the package name (`obligations_control_mapping`, which is also
the console script), the `OBLIGATIONS_` env prefix (including the bare token that
`infra/terraform/render.tf.json` carries so Terraform sets the same variable names on the
service), the cloud resource stem (`rgc7-svc`, the Terraform `name_prefix`) and the distribution
/ git id in one pass. Preview first, then apply:

```bash
# Preview (writes nothing):
python scripts/rename_fork.py --package acme_obligation_graph --env-prefix ACME \
    --resource acme-obligations --dry-run

# Apply:
python scripts/rename_fork.py --package acme_obligation_graph --env-prefix ACME \
    --resource acme-obligations --yes

# Then recreate the environment (the distribution name changed) and prove it is green:
python3.12 -m venv .venv && source .venv/bin/activate
make install
make gate
```

`--dist` defaults to the `--resource` value; pass it explicitly when your git id differs from
your resource stem. `--resource` is validated against the same regex the Terraform `name_prefix`
variable enforces, so a stem the stack would refuse fails here instead of at plan time. Add
`--include-docs` to sweep Markdown prose too. The catalog id `obligations-control-mapping` is left alone unless you pass
`--catalog-id`, so a fork stays traceable to the entry it descends from. The script deliberately
does NOT touch the human decisions below.

## 4. The human decisions (the script can't make these)

1. **Region / residency.** The build defaults to `asia-southeast1` (MAS / Singapore), chosen once
   and shared: `config/settings.yaml:region`, `infra/terraform/render.tf.json:render_region` and
   the Terraform `region` / `allowed_regions` pair. Set all of them to your in-country region and
   re-run `infra/terraform/production_edge.tftest.hcl`, which refuses a region outside the
   allowlist at plan time. See [`runbook.md`](runbook.md).
2. **Identity / IdP.** This repo owns no login flow: the `gcp` profile verifies the IAP-injected
   assertion at the edge, `local` uses seeded dev personas, and `onprem` is a client IdP
   placeholder. Wire your issuer on the deployed service (auth is configured ON the service, not
   in this code) and set `OBLIGATIONS_IAP_AUDIENCE`. An unset or emptied audience refuses every
   caller rather than verifying without one.
3. **The register itself.** `seed_graph` in `domain/obligations.py` builds an obviously fictional
   demo register: obligations, policies, controls, evidence and the edges between them. That seed
   is a shape, not your control library. Replace it with your own, and decide where the graph
   lives in a deployment: the offline profile holds the seed in process, so a deployment needs a
   durable graph store bound behind a port of its own.
4. **Coverage policy your compliance function owns.** `worst_coverage_severity` and the review
   flag on `CoverageAssessment` decide how bad a gap has to be before a human is pulled in; the
   staleness rule in `apply_change_feed` decides when a mapping stops counting; the acceptance
   step in `accept_all_proposals` decides who may turn a proposed edge into a counted one. Only
   accepted, non-stale edges count toward coverage, which is the property that makes the number
   defensible. These are module-level today rather than a `policy:` settings section
   (practices-audit check B4); change them deliberately and add a test that pins your values.
5. **Tenancy.** `REGISTER_TENANT` and `authorize_register_access` enforce that a caller may only
   read its own register, and a cross-tenant read raises rather than returning an empty result.
   Offline the seed IS the demo bank's register. Decide how your deployment carries the owning
   tenant on graph rows before you serve a second one.
6. **Reference data is fictional.** Every fixture and the seed register use obviously fake parties
   and `.example` domains. Replace them with your own synthetic data. **Do not run against a real
   control library without your own security and model-risk sign-off.**
7. **Eval golden set.** Rebuild the golden dataset for your register: a fork inherits a green gate
   that measures the WRONG graph until you do. The four metrics (`decision_accuracy`,
   `pii_safety`, `coverage_accuracy`, `narration_groundedness`) and their thresholds are generic;
   the golden cases are yours.
8. **Deployment posture.** Review the Dockerfile (digest-pinned base, non-root uid 10001),
   `infra/terraform/` (Org Policy, CMEK, a dry-run-first VPC-SC perimeter, the locked WORM log
   bucket) and the loopback-by-default binding before you expose anything. The WORM lock is
   irreversible: confirm `retention_days` before the first apply.

## 5. Do not duplicate the platform

This repo is one system in a catalog of composable GRC systems. It is deliberately the OWNER of
the obligation graph, so other systems read from it rather than keeping their own copy. What it
integrates rather than rebuilds (see [`faq/features-faq.md`](faq/features-faq.md) for the full
map):

- `human-review-console` human-review / maker-checker console: every `requires_human_review` escalation is
  routed to it over the shared `review-kit` (rule R8); you wire your endpoint
  (`HUMAN_REVIEW_URL`), you do not re-implement the console.
- `agent-observability` plus immutable WORM audit: audit events and trace spans go to it through
  `AuditSinkPort` and `ObservabilityTracerPort`.
- `model-quality-gate` AI-quality / model-risk gate: owns promotion. `eval/run_eval.py --mode gate` is the
  client half and refuses to run off the managed profile.
- `agent-registry`: this agent publishes its A2A card at
  `/.well-known/agent-card.json`; register it rather than inventing a discovery mechanism.
- `compliance-advisory`: the regulatory corpus and the change horizon that drive
  `apply_change_feed`. This repo consumes change records; it does not track the corpus.
- **Consumers of this register** (`ai-act-conformity-pack`'s applicability engine among them) read the graph from
  here. Adding a second register in a consuming repo is the failure this system exists to
  prevent.

The guardrail gateway (`agent-guardrail-gateway`) is **not** integrated today, and the enterprise knowledge base
(`enterprise-knowledge-base`) is not either. `agent-guardrail-gateway` becomes mandatory the moment untrusted free text reaches the narrator:
see rule R1 in [`../COMPLIANCE.md`](../COMPLIANCE.md).

## 6. Adoption checklist

- [ ] Ran `scripts/rename_fork.py`, recreated the venv, `make gate` green.
- [ ] Set the region in all three places (settings, `render.tf.json`, tfvars) and re-ran the
      Terraform residency tests.
- [ ] Wired your IdP audience on the deployed service (this repo owns no login flow).
- [ ] Replaced the seed register with your own obligations, policies, controls and evidence, and
      bound a durable graph store.
- [ ] Owned the coverage policy (severity mapping, staleness rule, acceptance step) with your
      compliance function.
- [ ] Decided how the owning tenant is carried on graph rows before serving a second tenant.
- [ ] Replaced every synthetic fixture.
- [ ] Rebuilt the eval golden set for your register.
- [ ] Reviewed the deploy posture (Dockerfile, Terraform, `retention_days`, bind address).
- [ ] Wired your `human-review-console` review endpoint and decided which sibling services you integrate vs stub.
- [ ] Read [`model-card.md`](model-card.md) and closed its remaining controls before enabling the
      managed narrator.
- [ ] Recorded your baseline upstream tag so you can take future fixes.
