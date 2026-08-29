# Adoption FAQ

For an engineering lead forking this repo as their institution's obligation register. The
step-by-step is [`../ADOPTING.md`](../ADOPTING.md); this answers the "will it hurt later?"
questions.

### How do I rebrand it for my organisation?

`scripts/rename_fork.py` rewrites the package name (`obligations_control_mapping`, which is also
the console script), the `OBLIGATIONS_` env prefix (including the bare token that
`infra/terraform/render.tf.json` carries, so Terraform sets the same variable names on the
service), the Terraform `name_prefix` resource stem (`rgc7-svc`) and the distribution / git id in
one pass. Preview with `--dry-run`, apply with `--yes`, then recreate the venv, `make install`,
and run `make gate`. The catalog id `Rgc7` is left alone unless you pass `--catalog-id`, so a fork
stays traceable to the entry it descends from. The script does the mechanical rename; the human
decisions (region, IdP, the register content, the coverage policy, the eval golden set) are the
checklist in `ADOPTING.md`.

### If several institutions fork this, how does each take upstream fixes?

Track upstream via **git tags**. The repo declares a core-vs-adopter-owned boundary
(`ADOPTING.md` section 2): upstream owns `domain/kernel.py`, `ports/`, `tests/contract/`, the eval
harness mechanics, CI and the Terraform stack; you own `config/settings.yaml` values, the seed
register, the coverage policy, `adapters/onprem/*`, UI theming and `terraform.tfvars`. The graph
arithmetic is a separate upstream package (`obligation-register-kit`) pinned by commit, so you
take its fixes by bumping the pin rather than by merging code. Rebase your adopter-owned changes
onto each release rather than merging `main` continuously.

### What do we have to supply that is not in this repo?

Three things, and only one of them is code here:

1. **The register.** `seed_graph` builds an obviously fictional demo register. Yours replaces it:
   your obligations, your policies, your controls, your evidence, and the edges between them.
2. **A durable graph store.** Offline the seed lives in process. A deployment needs a store bound
   behind a port of its own, carrying each register's owning tenant on its rows. This is the
   largest single piece of adoption work and it is not started.
3. **The review console.** An Hrz7 deployment reachable at `HUMAN_REVIEW_URL`. The managed
   router REFUSES to swallow an escalation when this is empty, so a fork cannot ship rule R8
   unwired and green.

### How do I add a new outbound dependency (a new port)?

There is a fixed touch list and a contract test that enforces it. A port must be registered in
FIVE places or it runs with no enforcement at all: `ports/__init__.py` (`PORT_PROTOCOLS`),
`config.DEFAULT_BINDINGS`, a `Container` accessor, `config/settings.yaml`, and a `PortCase` in
`tests/contract/canonical.py`. Then bind it in all three families.
`tests/contract/test_port_parity.py` asserts set equality across the five. The graph store is
exactly this job, and it is the port a real deployment adds first. See
[`../../CONTRIBUTING.md`](../../CONTRIBUTING.md).

### Can I retune the coverage policy without touching code?

Not yet, and this is stated honestly. `worst_coverage_severity`, the `CoverageAssessment` review
flag, the staleness rule in `apply_change_feed` and the eval thresholds are module-level constants
and functions rather than a `policy:` block in `config/settings.yaml` with a `from_policy(...)`
constructor. That is the open B4 item in [`../practices-audit.md`](../practices-audit.md). If your
compliance function must own these numbers as configuration, plan that addition as part of
adoption.

### Why are there two verticals in here?

Because the render started from the template's generic triage service and the Rgc7 coverage engine
was built alongside it. `domain/triage_service.py` (with `/v1/triage`, the CLI `triage` command
and the `triage_case` agent tool) is scaffolding; `domain/obligations.py` and
`domain/narration.py` (with `/v1/coverage`) are the reason this system exists. A fork that only
wants the register can delete the triage path, its tests and its routes; the hexagon does not
depend on it.

### Does the gate run for my fork out of the box?

Yes. `make gate` is offline, credential-free and network-free (ruff, ruff format, mypy strict, the
whole suite except integration, and the eval), and the CI workflow references no `secrets.`, so a
fork's build is green immediately. You add secrets only when you wire the `gcp` profile. Note the
eval measures the REFERENCE seed register until you rebuild the golden set for your own; that is
an explicit adoption step, not a silent pass.

### The eval reports high scores. Should we believe them?

Only because each metric is proved able to report something else.
`tests/unit/test_not_falsely_green.py` hands the metrics planted mutants and fails the build if
they still pass. The groundedness metric in particular measures the RAW model output through the
same pure functions the service enforces, so it can actually go red; a metric that watched only
the already-filtered service output would be green by construction.

### Will the demo rot after I diverge?

It is guarded, and the guard is inside the gate. A demo step lives in `demo.STEPS` and in
`walkthrough.CHECKS`, and `tests/unit/test_demo_surface.py` holds the two equal, so a claim the
demo makes but nobody verifies cannot exist. `make demo-selftest` runs the whole arc headless over
the real loopback server and exits non-zero when a claim stops being true. If you diverge, keep
the step keys and the `facts` dict the checks read.

### What is still open?

[`../practices-audit.md`](../practices-audit.md) carries the per-check verdict and the work list.
The three that matter most before production: the durable graph store, binding the Hrz1 guardrail
gateway before untrusted free text reaches the narrator, and registering this repo's metric bundle
with Hrz4 so `eval/run_eval.py --mode gate` has an authority to ask. The Terraform stack is
written, validated and tested against a mocked provider; it has never been applied.
