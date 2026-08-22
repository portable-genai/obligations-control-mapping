# Compliance FAQ

For compliance, model risk and the second line. The mapping table with a file reference on every
row is [`../../COMPLIANCE.md`](../../COMPLIANCE.md); this page answers the questions that come
back after reading it.

### Is a coverage figure from this system defensible?

That is the reason the counting is pure code. `compute_coverage` in the shared
`obligation-register-kit` and the deterministic policy in `domain/obligations.py` produce every
number, and three rules make the number mean something:

- **Only accepted edges count.** A proposed mapping does not become coverage until the
  maker-checker acceptance step turns it into an accepted edge, so coverage cannot be improved by
  proposing.
- **Only non-stale edges count.** `apply_change_feed` marks a mapping stale when the regulatory
  change feed moves the obligation underneath it, so last year's mapping stops counting the day
  the obligation changes rather than the day somebody notices.
- **The severity is computed.** `worst_coverage_severity` derives the band from the gap findings,
  and the band is what sets `requires_human_review`.

The model plays no part in any of it, and the same inputs always produce the same assessment, so a
figure quoted to a regulator can be replayed from the audit record.

### Who signs off an assessment?

A human, always, for anything consequential. `requires_human_review` and the call to
`ReviewRouterPort.route` are one act, not a flag plus an intention: the API, the CLI and the agent
tool all route in the same call that produced the result, and `tests/unit/test_review_routing.py`
asserts the routing rather than the flag. Under the managed profile the router REFUSES when no
console is configured, so a deployment cannot swallow an escalation silently.

### Where does the data live, and is residency enforced or just documented?

Enforced at deploy time. The region is chosen once (`asia-southeast1`) and shared by the runtime
and Terraform: `infra/terraform/variables.tf` validates the region against the residency allowlist
at plan, `org_policy.tf` pins `gcp.resourceLocations` to that region's location group, and every
regional resource (the CMEK key ring, the WORM log bucket, the Cloud Run service) is created in
it. `infra/terraform/production_edge.tftest.hcl` is the standing proof: its
`reject_region_outside_the_residency_allowlist` and `residency_defaults_are_in_country` runs fail
if the allowlist stops refusing or a resource drifts off region, and they run against a mocked
provider so they need no project and no credentials.

### What about key management and least privilege?

One REGIONAL CMEK key with a 90-day rotation, and an explicit key binding for EACH service agent
that encrypts under it, because CMEK does not cascade (`infra/terraform/kms.tf`). One serving
identity holding only the roles a request needs, each traceable to a bound adapter, with
`logging.logWriter` write only so the process cannot read back the WORM trail it writes
(`iam.tf`). Exportable service-account keys are forbidden by org policy rather than merely
avoided, and a key creation raises an alert if one happens anyway (`org_policy.tf`,
`monitoring.tf`).

### How long is the audit trail kept, and can it be edited?

180 days by default, and the variable refuses anything below 180. The Cloud Logging bucket is
LOCKED by default, which is irreversible: once applied, retention cannot be reduced and the bucket
cannot be deleted for the full window, not even with project-owner rights. Confirm
`retention_days` before the first apply. DATA_READ audit logging is enabled too, so a read of the
register is itself recorded.

Offline the same guarantee is earned differently: the log is hash-chained AND externally anchored,
because a truncated tail leaves a shorter chain that verifies perfectly. The retention schedule
and the legal basis for the trail are adopter-owned.

### What personal data does this system process?

Very little by design: it reasons over obligations, policies, controls and evidence references
rather than customer records. Whatever does appear is masked before every boundary (the audit
write, the outbound review payload, and any tool result that could enter a model's context), with
the jurisdiction rows and their ORDER chosen in `domain/pii.py`. The `pii_safety` metric holds
this at `>= 0.99` and is proved able to go red.

### Can one business unit see another's register?

No. `authorize_register_access` compares the verified principal's tenant against the register's
owning tenant and raises rather than returning an empty result, and the tenant comes from the
verified principal rather than from the request body. Note the honest limit: offline the seed IS
the demo register, so multi-tenant isolation at the STORE level is part of binding a durable graph
store, which this repo has not done yet.

### What model-risk evidence exists?

[`../model-card.md`](../model-card.md) records the model boundary as built: the model writes one
remediation note that restates engine figures, its reply is schema-validated and groundedness-
checked and discarded on failure, and a deterministic fallback note is used instead. The offline
eval scores `decision_accuracy`, `pii_safety`, `coverage_accuracy` and `narration_groundedness` on
every change, and the groundedness metric measures raw model output rather than filtered output so
it can go red. What is NOT yet in place: the managed model is not pinned to a confirmed model id
and version, there is no token budget, rate limit or kill switch, no live-model eval run has been
registered with the Hrz4 promotion gate, and prompt-injection screening through Hrz1 is not bound.
Until those close, the managed narrator is not production-cleared and the deterministic path is
what should be relied on.

### Which regulations does this claim to satisfy?

None, on your behalf. The mapping in `COMPLIANCE.md` is to the CATALOG's own principles (P-01 to
P-13) and platform rules (R1 to R8). The crosswalk from those to MAS TRM, CPS 234, CPS 230, HKMA
or PDPA control ids, and the judgement that a control is SUFFICIENT for a regulation, is
explicitly adopter-owned. No row in that document should be quoted as regulatory assurance, and
the second-line review of the deterministic policy in `domain/` is bank-owned logic rather than a
vendor default to inherit unexamined.

### What is still open at go-live?

The `Partial` and `TODO (repo owner)` rows in `COMPLIANCE.md`, each of which names exactly what is
missing. The ones that need a risk acceptance if you go live without them: the durable graph store
and its object-level authorisation, rule R1 (the Hrz1 guardrail binding), rule R5 and P-08 (the
Hrz4 metric bundle), P-10 (timeouts, circuit breaker and a documented kill switch), and P-01's
private-egress rule, which depends on your own network rather than on this repo.
