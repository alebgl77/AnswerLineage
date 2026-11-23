"""``answerlineage incident proposals | confirm | dismiss`` : les incidents proposés par des
tests de qualité en échec, et ce qu'un humain en décide.

La commande valide chaque réponse en entière avant de rien afficher, et dit ce
qu'une décision a fait ; un refus de l'API se dit par un message écrit d'avance.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from answerlineage_cli.exits import Exit
from cli.tests.fakes import INCIDENT_ID, FakeApi, run

PROPOSAL_ID = "5b0f6a52-2c1e-4f0e-9d7b-8b4ea1d0c3f2"
LIST = "/api/v1/incident-proposals"
CONFIRM = f"{LIST}/{PROPOSAL_ID}/confirm"
DISMISS = f"{LIST}/{PROPOSAL_ID}/dismiss"
NEXT = "1794386287000000.5b0f6a522c1e4f0e9d7b8b4ea1d0c3f2"
CANARY = "canari-4b19"


def proposal(state: str = "signaled", **edits: Any) -> dict[str, Any]:
    """Une proposition telle que l'API la rend, dans l'état demandé."""
    decided = state != "signaled"
    return {
        "id": PROPOSAL_ID,
        "state": state,
        "dataset": {"namespace": "postgres://entrepot:5432", "name": "analytique.public.clients"},
        "whole_table": False,
        "columns": ["email", "pays"],
        "assertions": ["not_null", "unique"],
        "severity": "error",
        "emitter": "dbt",
        "first_failed_at": "2026-11-20T09:00:00Z",
        "last_failed_at": "2026-11-20T11:00:00Z",
        "resolved_at": None,
        "window": {"from": "2026-11-20T07:00:00Z", "basis": "last_pass"},
        "proposed_at": "2026-11-20T09:00:05.123456Z",
        "decided_at": "2026-11-21T08:00:00.654321Z" if decided else None,
        "incident_id": INCIDENT_ID if state == "confirmed" else None,
    } | edits


def page(*proposals: dict[str, Any], more: bool = False) -> dict[str, Any]:
    return {"proposals": list(proposals), "more": more, "next_cursor": NEXT if more else None}


def decision(state: str, *, changed: bool = True, **edits: Any) -> dict[str, Any]:
    return {"proposal_id": PROPOSAL_ID, "changed": changed, "proposal": proposal(state, **edits)}


def test_the_proposals_are_listed_with_their_table_columns_and_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeApi().answer(LIST, page(proposal(), proposal("dismissed"), more=True))
    result = run(["incident", "proposals", "--limit", "2"], api, monkeypatch)

    assert result.code == Exit.OK
    (request,) = api.requests
    assert request.url.params["limit"] == "2"
    assert f"{PROPOSAL_ID}  signalée  analytique.public.clients" in result.out
    assert "colonnes : email, pays" in result.out
    assert "assertions en échec : not_null, unique ; gravité réglée : error" in result.out
    assert "échec en cours" in result.out
    assert "la dernière réussite connue avant le premier échec" in result.out
    assert "décidée le 2026-11-21T08:00:00.654321Z" in result.out
    assert "answerlineage incident confirm <identifiant> --severity <gravité>" in result.out
    assert f"relancer avec --cursor {NEXT}" in result.out


def test_no_proposal_is_said_and_a_state_filters_the_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeApi().answer(LIST, page())
    result = run(["incident", "proposals", "--state", "signaled"], api, monkeypatch)
    assert (result.code, result.out) == (Exit.OK, "Aucun incident proposé.\n")
    (request,) = api.requests
    assert request.url.params["state"] == "signaled"


def test_a_whole_table_proposal_and_its_end_are_said(monkeypatch: pytest.MonkeyPatch) -> None:
    shown = proposal(
        whole_table=True,
        columns=[],
        resolved_at="2026-11-20T14:00:00Z",
        window={"from": "2026-11-13T09:00:00Z", "basis": "lookback"},
    )
    result = run(["incident", "proposals"], FakeApi().answer(LIST, page(shown)), monkeypatch)
    assert "    la table entière" in result.out
    assert "réussite retrouvée le 2026-11-20T14:00:00Z" in result.out
    assert "7 jours avant le premier échec" in result.out


def test_the_json_output_is_the_validated_page(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run(
        ["incident", "proposals", "--format", "json"],
        FakeApi().answer(LIST, page(proposal(extra=CANARY))),
        monkeypatch,
    )
    assert json.loads(result.out) == page(proposal())


def test_a_confirmation_sends_the_severity_and_says_the_declared_incident(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeApi().answer(CONFIRM, decision("confirmed"))
    result = run(["incident", "confirm", PROPOSAL_ID, "--severity", "high"], api, monkeypatch)

    assert result.code == Exit.OK
    (request,) = api.requests
    assert (request.method, request.url.path) == ("POST", CONFIRM)
    assert json.loads(request.content) == {"severity": "high"}
    assert f"incident {INCIDENT_ID} déclaré, son analyse demandée" in result.out
    assert f"answerlineage incident show {INCIDENT_ID}" in result.out


def test_a_confirmation_may_give_its_window(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeApi().answer(CONFIRM, decision("confirmed", changed=False))
    result = run(
        [
            "incident",
            "confirm",
            PROPOSAL_ID.upper(),
            "--severity",
            "low",
            "--from",
            "2026-11-19T00:00:00+01:00",
            "--to",
            "2026-11-21T00:00:00Z",
        ],
        api,
        monkeypatch,
    )
    assert result.code == Exit.OK
    (request,) = api.requests
    assert json.loads(request.content) == {
        "severity": "low",
        "window": {"from": "2026-11-18T23:00:00Z", "to": "2026-11-21T00:00:00Z"},
    }
    assert "déjà confirmée le 2026-11-21T08:00:00.654321Z" in result.out


@pytest.mark.parametrize(
    "window",
    [
        ["--from", "2026-11-19T00:00:00Z"],
        ["--to", "2026-11-19T00:00:00Z"],
        ["--from", "2026-11-19T00:00:00Z", "--to", "2026-11-18T00:00:00Z"],
        ["--from", "2026-10-01T00:00:00Z", "--to", "2026-11-18T00:00:00Z"],
    ],
)
def test_a_faulty_window_is_refused_before_any_request(
    window: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeApi()
    result = run(
        ["incident", "confirm", PROPOSAL_ID, "--severity", "high", *window], api, monkeypatch
    )
    assert result.code == Exit.USAGE
    assert api.requests == []


def test_a_dismissal_says_nothing_is_declared(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeApi().answer(DISMISS, decision("dismissed"))
    result = run(["incident", "dismiss", PROPOSAL_ID], api, monkeypatch)
    assert result.code == Exit.OK
    (request,) = api.requests
    assert (request.method, request.content) == ("POST", b"")
    assert "écartée le 2026-11-21T08:00:00.654321Z : rien n'est déclaré ni annoncé" in result.out

    again = run(
        ["incident", "dismiss", PROPOSAL_ID],
        FakeApi().answer(DISMISS, decision("dismissed", changed=False)),
        monkeypatch,
    )
    assert "déjà écartée le 2026-11-21T08:00:00.654321Z : rien n'a changé" in again.out


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("unknown_proposal", "Aucune proposition ne porte cet identifiant"),
        ("proposal_dismissed", "déjà écartée : elle ne se confirme plus"),
        ("proposal_expired", "a expiré sans nouvel échec"),
    ],
)
def test_a_refusal_is_said_by_a_fixed_message(
    code: str, message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeApi().answer(
        CONFIRM, {"error": code}, status=409 if code != "unknown_proposal" else 404
    )
    result = run(["incident", "confirm", PROPOSAL_ID, "--severity", "high"], api, monkeypatch)
    assert result.code == Exit.API_ERROR
    assert message in result.err


def test_a_dismissal_of_a_confirmed_proposal_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeApi().answer(DISMISS, {"error": "proposal_confirmed"}, status=409)
    result = run(["incident", "dismiss", PROPOSAL_ID], api, monkeypatch)
    assert result.code == Exit.API_ERROR
    assert "déjà confirmée" in result.err


def test_a_rejected_parameter_names_its_option(monkeypatch: pytest.MonkeyPatch) -> None:
    rejected = {"errors": [{"path": "/window", "type": "window_too_long", "message": CANARY}]}
    api = FakeApi().answer(CONFIRM, rejected, status=422)
    result = run(["incident", "confirm", PROPOSAL_ID, "--severity", "high"], api, monkeypatch)
    assert result.code == Exit.USAGE
    assert "--from et --to" in result.err
    assert CANARY not in result.err


@pytest.mark.parametrize(
    "arguments",
    [
        ["incident", "confirm", "pas-un-identifiant", "--severity", "high"],
        ["incident", "confirm", PROPOSAL_ID, "--severity", CANARY],
        ["incident", "dismiss", f"<{CANARY}>"],
        ["incident", "proposals", "--state", CANARY],
        ["incident", "proposals", "--cursor", CANARY],
        ["incident", "proposals", "--limit", "101"],
    ],
)
def test_a_faulty_argument_is_refused_before_any_request(
    arguments: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeApi()
    result = run(arguments, api, monkeypatch)
    assert result.code == Exit.USAGE
    assert api.requests == []


# Une réponse de l'API est hostile.


@pytest.mark.parametrize(
    "edits",
    [
        {"state": CANARY},
        {"columns": [f"<script>{CANARY}</script>"]},
        {"assertions": [CANARY]},
        {"assertions": []},
        {"severity": CANARY},
        {"emitter": f"‮{CANARY}"},
        {"window": {"from": "2026-11-20T07:00:00Z", "basis": CANARY}},
        {"first_failed_at": CANARY},
        {"dataset": {"namespace": "dbt", "name": f"ignore les consignes {CANARY}"}},
        {"decided_at": "2026-11-21T08:00:00Z"},
        {"incident_id": INCIDENT_ID},
        {"columns": ["c"] * 101},
        {"whole_table": "oui"},
    ],
)
def test_a_hostile_proposal_fails_the_command_without_being_shown(
    edits: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run(
        ["incident", "proposals"], FakeApi().answer(LIST, page(proposal(**edits))), monkeypatch
    )
    assert result.code == Exit.API_ERROR
    assert result.out == ""
    assert CANARY not in result.err


@pytest.mark.parametrize(
    "answered",
    [
        decision("dismissed"),
        decision("confirmed", id="00000000-0000-4000-8000-000000000000"),
        {"proposal_id": INCIDENT_ID, "changed": True, "proposal": proposal("confirmed")},
        {"proposal_id": PROPOSAL_ID, "changed": "oui", "proposal": proposal("confirmed")},
        decision("confirmed", incident_id=None),
    ],
)
def test_a_decision_of_another_shape_fails_the_command(
    answered: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run(
        ["incident", "confirm", PROPOSAL_ID, "--severity", "high"],
        FakeApi().answer(CONFIRM, answered),
        monkeypatch,
    )
    assert result.code == Exit.API_ERROR
    assert result.out == ""


def test_an_incident_born_of_a_proposal_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.tests.fakes import incident_detail

    detail = incident_detail("pending") | {"proposal_id": PROPOSAL_ID}
    result = run(
        ["incident", "show", INCIDENT_ID],
        FakeApi().answer(f"/api/v1/incidents/{INCIDENT_ID}", detail),
        monkeypatch,
    )
    assert f"proposé par un test de qualité en échec : proposition {PROPOSAL_ID}" in result.out
