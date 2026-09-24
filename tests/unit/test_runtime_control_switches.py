"""Review routing has a switch, default on, and every caller says what happened to a hand-off.

The fleet's runtime-control contract (2026-09-24). Review routing is the one cheap runtime control
this service has: ``OBLIGATIONS_REVIEW_ROUTING`` is read in three states; off binds a disabled
router and says so at startup; on under the managed profile refuses to boot without a console; and
the API (triage and coverage), the agent tool and the CLI report ``review_routing`` rather than
failing an already-audited result when the console is unreachable.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from obligations_control_mapping.adapters.controls import (
    DisabledReviewRouter,
    RecordingReviewRouter,
    ReviewRouting,
)
from obligations_control_mapping.agent import tools
from obligations_control_mapping.api import app as api_module
from obligations_control_mapping.api.app import app
from obligations_control_mapping.cli.main import main as cli_main
from obligations_control_mapping.config import (
    REVIEW_ROUTING_ENV,
    Container,
    ControlSwitches,
    ProfileChoice,
    Settings,
    build_container,
    warn_switched_off,
)
from obligations_control_mapping.domain.models import TriageInput, TriageResult
from obligations_control_mapping.domain.triage_service import TriageService
from obligations_control_mapping.envread import ConfiguredEmptyError

_LOOPBACK = ("127.0.0.1", 50000)
_ESCALATING = {"subject": "Acme Holdings (FICTIONAL)", "text": "urgent data breach"}
_ROUTINE = {"subject": "Acme Holdings (FICTIONAL)", "text": "routine note"}
_AUDITOR = {"X-Dev-Persona": "auditor"}
_LOCAL_ROUTE = "obligations_control_mapping.adapters.local.review_router.LocalReviewRouter.route"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(REVIEW_ROUTING_ENV, raising=False)
    monkeypatch.delenv("HUMAN_REVIEW_URL", raising=False)
    # The API caches its container for the process; each test here states its own posture.
    api_module._container.cache_clear()
    yield
    api_module._container.cache_clear()


def _result(text: str = "urgent data breach") -> TriageResult:
    container = build_container(Settings(profile="local", audit_path=":memory:"))
    service = TriageService(container.audit, tracer=container.tracer)
    return service.triage(
        TriageInput(subject="Acme Holdings (FICTIONAL)", text=text), actor="auditor@bank.example"
    )


def _gcp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "obligations_control_mapping.config.resolve_profile",
        lambda environ=None: ProfileChoice("gcp", True),
    )


# --------------------------------------------------------------------------- #
# Three states
# --------------------------------------------------------------------------- #
def test_routing_is_on_when_nothing_is_said() -> None:
    assert Settings.load().controls == ControlSwitches(review_routing=True)


def test_routing_switched_off_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REVIEW_ROUTING_ENV, "off")
    assert Settings.load().controls.switched_off() == (REVIEW_ROUTING_ENV,)


def test_an_emptied_switch_refuses_at_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REVIEW_ROUTING_ENV, "")
    with pytest.raises(ConfiguredEmptyError, match=REVIEW_ROUTING_ENV):
        Settings.load()


def test_an_unrecognised_switch_refuses_at_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REVIEW_ROUTING_ENV, "sometimes")
    with pytest.raises(ValueError, match=REVIEW_ROUTING_ENV):
        Settings.load()


# --------------------------------------------------------------------------- #
# Off binds the disabled router, and says so once
# --------------------------------------------------------------------------- #
def test_off_binds_the_disabled_router() -> None:
    settings = Settings(profile="local", controls=ControlSwitches(review_routing=False))
    assert isinstance(Container(settings).review_router, DisabledReviewRouter)


def test_on_binds_the_profile_router() -> None:
    settings = Settings(profile="local")
    assert not isinstance(Container(settings).review_router, DisabledReviewRouter)


def test_the_off_posture_is_logged_once_however_many_containers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    warn_switched_off.cache_clear()
    settings = Settings(profile="local", controls=ControlSwitches(review_routing=False))
    with caplog.at_level(logging.WARNING, logger="obligations_control_mapping.config"):
        for _ in range(3):
            build_container(settings)
    assert caplog.text.count(REVIEW_ROUTING_ENV) == 1


# --------------------------------------------------------------------------- #
# On has to work: checked at boot under the managed profile
# --------------------------------------------------------------------------- #
def test_routing_on_under_gcp_without_a_console_refuses_at_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _gcp(monkeypatch)
    with pytest.raises(ConfiguredEmptyError, match="HUMAN_REVIEW_URL"):
        Settings.load()


def test_routing_stated_off_under_gcp_needs_no_console(monkeypatch: pytest.MonkeyPatch) -> None:
    _gcp(monkeypatch)
    monkeypatch.setenv(REVIEW_ROUTING_ENV, "false")
    assert Settings.load().controls.review_routing is False


def test_routing_on_under_gcp_with_a_console_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    _gcp(monkeypatch)
    monkeypatch.setenv("HUMAN_REVIEW_URL", "https://review.example.test")
    assert Settings.load().review_url == "https://review.example.test"


# --------------------------------------------------------------------------- #
# The four routing outcomes
# --------------------------------------------------------------------------- #
class _Accepting:
    def route(self, result: TriageResult, *, maker: str, tenant: str = "") -> str:
        return "review-1"


class _Refusing:
    def route(self, result: TriageResult, *, maker: str, tenant: str = "") -> str:
        raise ConnectionError("console unreachable")


def test_routing_outcomes_take_each_of_their_four_values() -> None:
    result = _result()
    assert result.requires_human_review

    not_required = RecordingReviewRouter(_Accepting())
    assert not_required.route(_result("routine note"), maker="m") == ""
    assert not_required.outcome is ReviewRouting.NOT_REQUIRED

    routed = RecordingReviewRouter(_Accepting())
    assert routed.route(result, maker="m") == "review-1"
    assert routed.outcome is ReviewRouting.ROUTED

    off = RecordingReviewRouter(DisabledReviewRouter(Settings()))
    assert off.route(result, maker="m") == ""
    assert off.outcome is ReviewRouting.OFF


def test_a_failed_hand_off_is_reported_and_logged_never_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failed = RecordingReviewRouter(_Refusing())
    with caplog.at_level(logging.WARNING, logger="obligations_control_mapping.adapters.controls"):
        assert failed.route(_result(), maker="m") == ""
    assert failed.outcome is ReviewRouting.FAILED
    assert "ConnectionError" in caplog.text


# --------------------------------------------------------------------------- #
# Every caller reports it
# --------------------------------------------------------------------------- #
def _post(path: str, body: dict[str, object]) -> dict[str, object]:
    response = TestClient(app, client=_LOOPBACK).post(path, json=body, headers=_AUDITOR)
    assert response.status_code == 200, response.text
    return response.json()


def test_the_api_reports_routed_and_not_required_triage() -> None:
    routed = _post("/v1/triage", _ESCALATING)
    assert routed["review_routing"] == "routed"
    assert routed["review_ref"]
    routine = _post("/v1/triage", _ROUTINE)
    assert routine["review_routing"] == "not_required"
    assert routine["review_ref"] == ""


def test_the_api_reports_routing_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REVIEW_ROUTING_ENV, "off")
    reply = _post("/v1/triage", _ESCALATING)
    assert reply["review_routing"] == "off"
    assert reply["review_ref"] == ""


def test_the_api_reports_a_failed_hand_off_instead_of_failing_the_triage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_LOCAL_ROUTE, _Refusing.route)
    reply = _post("/v1/triage", _ESCALATING)
    assert reply["review_routing"] == "failed"
    assert reply["review_ref"] == ""


def test_the_agent_tool_reports_the_hand_off(monkeypatch: pytest.MonkeyPatch) -> None:
    assert tools.triage_case(**_ESCALATING)["review_routing"] == "routed"
    monkeypatch.setattr(_LOCAL_ROUTE, _Refusing.route)
    failed = tools.triage_case(**_ESCALATING)
    assert failed["review_routing"] == "failed"
    assert failed["review_ref"] == ""


def test_the_cli_reports_the_hand_off(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(REVIEW_ROUTING_ENV, "off")
    assert cli_main(["triage", "Acme Holdings (FICTIONAL)", "urgent data breach"]) == 0
    assert "human review hand-off : off" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# The coverage assessment hands off too
# --------------------------------------------------------------------------- #
def test_the_coverage_route_reports_routed_off_and_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routed = _post("/v1/coverage", {})
    assert routed["requires_human_review"] is True
    assert routed["review_routing"] == "routed"

    monkeypatch.setattr(_LOCAL_ROUTE, _Refusing.route)
    failed = _post("/v1/coverage", {})
    assert failed["review_routing"] == "failed"
    assert failed["review_ref"] == ""

    monkeypatch.setenv(REVIEW_ROUTING_ENV, "off")
    api_module._container.cache_clear()
    off = _post("/v1/coverage", {})
    assert off["review_routing"] == "off"
    assert off["review_ref"] == ""
