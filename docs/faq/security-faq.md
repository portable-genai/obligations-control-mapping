# Security FAQ

For AppSec and security architecture. Every answer names the file that is the evidence, so the
review can read the control rather than the claim.

### Who is the actor on a decision, and can a caller assert it?

A server-verified `Principal`, always. The request schemas carry no `actor` field: the audit actor
and the review maker both come from the identity adapter, and every client-supplied actor, tenant,
role, ACL and authorization header is discarded at the browser boundary
(`ui/lib/embed-policy.mjs`). Under the `gcp` profile the adapter verifies the IAP-injected
assertion against the configured audience, against IAP's own key set and against the issuer
(`adapters/gcp/identity.py`); an unset or emptied `OBLIGATIONS_IAP_AUDIENCE` REFUSES every caller,
because `audience=None` means google-auth does not verify the audience at all and would accept any
Google-signed token from any project.

### Can one tenant read another tenant's register?

No, and the refusal is loud rather than empty. `authorize_register_access` in
`domain/obligations.py` compares the verified principal's tenant against the register's owning
tenant and raises `CrossTenantError`. An empty result would be indistinguishable from a register
with no findings, which is how a probe becomes an information leak. The tenant comes from the
verified principal, never from the request body.

### What happens if the profile variable goes missing in production?

The process still binds the SDK-free adapters (the alternative is importing cloud SDKs that are
not installed), but nobody chose them, so every relaxation is withdrawn: the seeded dev personas
refuse to construct, no service-to-service scheme is selected, the dev CORS allowlist and the
`X-Dev-Persona` header are gone, the interactive docs are not registered, and the loopback
exposure guard refuses every route to any non-loopback peer. An emptied or mis-capitalised value
raises AT IMPORT, so the process fails to boot rather than serving on a posture nobody chose
(`config.py`, `tests/unit/test_profile_single_source.py`).

### Does setting the service-to-service token open anything?

No, and this is enforced rather than intended. The exposure guard's posture is derived from the
identity BINDING (the adapter declares `VERIFIED` / `CLIENT_ASSERTED` / `UNIMPLEMENTED`), never
from a credential. `OBLIGATIONS_S2S_TOKEN` authenticates a calling SERVICE and no end user.
`tests/unit/test_end_user_auth_posture.py` walks the guard's argument through the constants it
names and fails the build if a credential reappears at any depth, because it did once: setting the
token switched the guard off for the end-user routes it was protecting.

### Where does personal data go?

This service reasons over obligations, controls and evidence references rather than customer
records, so the personal-data surface is small by construction. What does appear is masked before
it crosses any boundary: before the audit write, before a review payload leaves the process, and
before a tool result can enter a model's context. The pattern set and its ORDER are this
vertical's (`domain/pii.py`, national rows first, universal rows last), drawn from the shared
`pii-kit`. The `pii_safety` eval metric holds this at `>= 0.99` and
`tests/unit/test_not_falsely_green.py` proves the metric can go red.

### Can the model exfiltrate or invent anything?

The model is reachable through exactly one port (`ports/generation.py`), it receives a system
prompt plus a facts block the engine built, and its reply is parsed and REJECTED unless it is
well-formed JSON with the requested keys and every integer in it is one the engine produced
(`domain/narration.py`: `parse_note`, `note_is_grounded`). A rejected reply is discarded and the
deterministic fallback note is used instead. The groundedness checks are module-level pure
functions rather than private methods, deliberately, so the eval measures the RAW model output
through the very same contract the service enforces: a metric that watched only the filtered
output could never go red. Prompt-injection screening through the `agent-guardrail-gateway` is
**not** wired yet.

### How is the audit trail protected?

Append-only and hash-chained, AND externally anchored. The chain catches an edit, a deletion or a
reorder; only the anchor catches a TRUNCATED TAIL, because dropping the newest rows leaves a
shorter chain that verifies perfectly. `audit_anchor_path` (`OBLIGATIONS_AUDIT_ANCHOR`) writes the
chain head to a file on another volume, and `tests/unit/test_audit_anchor.py` proves the
detection, proves the control case goes UNDETECTED without an anchor, and proves an append after
truncation refuses rather than re-anchoring. Under the managed profile the sink is a locked Cloud
Logging bucket (`infra/terraform/logging_worm.tf`), which provides non-rewritability itself.

### What about supply chain?

Both lockfiles are committed and pin every dependency exactly; the catalog commons, including
`obligation-register-kit`, are pinned to 40-character COMMIT shas rather than tags, because a
re-pushed tag changes what installs with no diff in the lockfile. The base image is digest-pinned,
Actions are SHA-pinned, dependabot covers pip, docker, github-actions and npm, and `pip-audit`
plus `npm audit --audit-level=high` are HARD CI failures. `tests/unit/test_repo_artifacts.py`
asserts each of these from inside the repo, and it asks git whether each pinned sha is a COMMIT
object rather than an annotated tag object, which a regular expression cannot tell apart.

### What is deliberately out of scope?

- **Login.** This repo authenticates nobody itself: the platform in front of it does, and the UI
  forwards the assertion without parsing or trusting a parsed copy.
- **Injection defence and output filtering.** Owned by `agent-guardrail-gateway`; not bound yet.
- **The review queue.** Owned by `human-review-console`; this repo produces escalations and routes them.
- **Durable storage of the graph.** Not bound today: offline the seed register lives in process.
  A deployment needs a store behind a port, and its access control is part of that work.
- **Network egress control.** VPC-SC governs access to Google APIs across perimeters, not
  arbitrary internet egress. The private-egress rule that lets this service reach the `human-review-console`
  and nothing else is an adopter network decision, called out in `COMPLIANCE.md` P-01.
