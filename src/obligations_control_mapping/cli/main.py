"""Minimal stdlib CLI: triage a case, or verify the audit chain (argparse, no extra deps)."""

from __future__ import annotations

import argparse
import sys

from hex_service_kit.logging import configure_logging

from ..config import build_container
from ..domain.models import TriageInput
from ..domain.triage_service import TriageService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obligations_control_mapping")
    sub = parser.add_subparsers(dest="command", required=True)

    triage_cmd = sub.add_parser("triage", help="Triage a single case.")
    triage_cmd.add_argument("subject")
    triage_cmd.add_argument("text")
    triage_cmd.add_argument("--actor", default="cli-user@bank.example")
    triage_cmd.add_argument("--tenant", default="", help="Tenant partition asserted to Hrz7.")

    args = parser.parse_args(argv)
    container = build_container()
    # Idempotent: a process that is both an API app and a CLI configures once.
    configure_logging(container.settings.profile, service="obligations-control-mapping")

    if args.command == "triage":
        service = TriageService(container.audit, tracer=container.tracer)
        result = service.triage(TriageInput(subject=args.subject, text=args.text), actor=args.actor)
        print(f"{result.subject}: {result.severity.value} ({result.decision.value})")
        print(f"  requires_human_review: {result.requires_human_review}")
        if result.requires_human_review:
            # Rule R8 on the CLI path too: the same escalation, the same router. A surface that
            # only printed the flag would be a second place for an escalation to stop.
            ref = container.review_router.route(result, maker=args.actor, tenant=args.tenant)
            print(f"  routed to human review: {ref}")
        return 0

    return 2  # pragma: no cover - argparse requires a subcommand


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
