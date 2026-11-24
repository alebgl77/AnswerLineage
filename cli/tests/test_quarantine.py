"""``answerlineage incident quarantine`` : mettre un incident en quarantaine, la lever.

La commande envoie l'action explicite, valide la réponse entière, et dit si elle
a changé quelque chose ; un refus de l'API se dit par un message écrit d'avance.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from answerlineage_cli.exits import Exit
from cli.tests.fakes import (
    INCIDENT_ID,
    FakeApi,
    incident,
    incident_detail,
    quarantine,
    run,
    unreachable,
)

ONE = f"/api/v1/incidents/{INCIDENT_ID}"
ACTIVATE = f"{ONE}/quarantine"
LIFT = f"{ONE}/quarantine/lift"
CANARY = "canari-2c7d"


def change(state: str = "active", *, changed: object = True, **edits: Any) -> dict[str, Any]:
    return {"incident_id": INCIDENT_ID, "changed": changed, "quarantine": quarantine(state)} | edits


def test_a_quarantine_is_asked_in_warn_mode_by_default_and_said(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeApi().answer(ACTIVATE, change())
    result = run(["incident", "quarantine", INCIDENT_ID], api, monkeypatch)

    assert result.code == Exit.OK
    (request,) = api.requests
    assert (request.method, request.url.path) == ("POST", ACTIVATE)
    assert json.loads(request.content) == {"mode": "warn"}
    assert (
        f"Incident {INCIDENT_ID} mis en quarantaine le 2026-11-12T14:00:00.123456Z, "
        "signaler les lectures (warn)." in result.out
    )
    assert "30 s au plus" in result.out
    assert f"answerlineage incident quarantine {INCIDENT_ID} --lift" in result.out
    assert result.err == ""


def test_a_quarantine_already_active_or_lifted_says_nothing_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    again = run(
        ["incident", "quarantine", INCIDENT_ID, "--mode", "warn"],
        FakeApi().answer(ACTIVATE, change(changed=False)),
        monkeypatch,
    )
    assert again.code == Exit.OK
    assert "déjà en quarantaine depuis le 2026-11-12T14:00:00.123456Z" in again.out
    assert "rien n'a changé" in again.out

    lifted_again = run(
        ["incident", "quarantine", INCIDENT_ID, "--lift"],
        FakeApi().answer(LIFT, change("lifted", changed=False)),
        monkeypatch,
    )
    assert lifted_again.code == Exit.OK
    assert "déjà levée le 2026-11-13T09:30:00.654321Z : rien n'a changé" in lifted_again.out


def test_the_lift_is_asked_without_a_body_and_said(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeApi().answer(LIFT, change("lifted"))
    result = run(["incident", "quarantine", INCIDENT_ID, "--lift"], api, monkeypatch)

    assert result.code == Exit.OK
    (request,) = api.requests
    assert (request.method, request.url.path, request.content) == ("POST", LIFT, b"")
    assert f"Quarantaine de l'incident {INCIDENT_ID} levée le 2026-11-13T09:30:00.654321Z." in (
        result.out
    )


def test_the_json_output_is_the_change_of_the_api(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run(
        ["incident", "quarantine", INCIDENT_ID, "--format", "json"],
        FakeApi().answer(ACTIVATE, change()),
        monkeypatch,
    )
    assert result.code == Exit.OK
    assert json.loads(result.out) == change()


@pytest.mark.parametrize(
    "argv",
    [
        ["incident", "quarantine", INCIDENT_ID, "--mode", CANARY],
        ["incident", "quarantine", INCIDENT_ID, "--mode", "warn", "--lift"],
        ["incident", "quarantine", f"{CANARY}\x1b[2J"],
    ],
)
def test_an_unknown_mode_both_actions_or_a_malformed_identifier_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    api = FakeApi().answer(ACTIVATE, change())
    result = run(argv, api, monkeypatch)
    assert result.code == Exit.USAGE
    assert api.requests == []
    assert CANARY not in result.err


@pytest.mark.parametrize(
    ("path", "document", "status", "said"),
    [
        (ACTIVATE, {"error": "unknown_incident"}, 404, "Aucun incident ne porte cet identifiant"),
        (ACTIVATE, {"error": "too_many_quarantines"}, 409, "nombre maximal de quarantaines"),
        (LIFT, {"error": "not_quarantined"}, 409, "jamais été mis en quarantaine"),
        (
            ACTIVATE,
            {"errors": [{"path": "/mode", "type": "literal_error", "message": CANARY}]},
            422,
            "--mode",
        ),
        (ACTIVATE, {"error": CANARY}, 409, "HTTP 409"),
    ],
)
def test_a_refusal_of_the_api_is_said_by_a_written_message(
    monkeypatch: pytest.MonkeyPatch, path: str, document: Any, status: int, said: str
) -> None:
    argv = ["incident", "quarantine", INCIDENT_ID, *(["--lift"] if path == LIFT else [])]
    result = run(argv, FakeApi().answer(path, document, status), monkeypatch)
    assert result.code in (Exit.API_ERROR, Exit.USAGE)
    assert said in result.err
    assert CANARY not in result.err
    assert result.out == ""


@pytest.mark.parametrize(
    ("argv", "path", "document"),
    [
        ([], ACTIVATE, change("lifted")),
        (["--lift"], LIFT, change("active")),
        ([], ACTIVATE, change(incident_id="00000000-0000-4000-8000-000000000000")),
        ([], ACTIVATE, change(quarantine=None)),
        ([], ACTIVATE, change(changed=CANARY)),
        ([], ACTIVATE, change(quarantine={**quarantine(), "mode": CANARY})),
        ([], ACTIVATE, change(quarantine={**quarantine(), "lifted_at": "2026-11-13T09:30:00Z"})),
        (["--lift"], LIFT, change(quarantine={**quarantine("lifted"), "lifted_at": None})),
        ([], ACTIVATE, change(quarantine={**quarantine(), "activated_at": CANARY})),
    ],
)
def test_a_hostile_change_fails_the_command_without_being_shown(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], path: str, document: dict[str, Any]
) -> None:
    result = run(
        ["incident", "quarantine", INCIDENT_ID, *argv],
        FakeApi().answer(path, document),
        monkeypatch,
    )
    assert result.code == Exit.API_ERROR
    assert result.out == ""
    assert "Réponse de l'API invalide en /" in result.err
    assert CANARY not in result.err


def test_an_unreachable_api_has_its_own_code(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = httpx.MockTransport(unreachable)
    result = run(["incident", "quarantine", INCIDENT_ID], transport, monkeypatch)
    assert result.code == Exit.UNREACHABLE


@pytest.mark.parametrize(
    ("state", "said"),
    [
        (None, "quarantaine : aucune"),
        ("active", "quarantaine : active depuis le 2026-11-12T14:00:00.123456Z, signaler"),
        ("lifted", "quarantaine : levée le 2026-11-13T09:30:00.654321Z, active depuis le"),
    ],
)
def test_an_incident_shows_its_quarantine(
    monkeypatch: pytest.MonkeyPatch, state: str | None, said: str
) -> None:
    document = incident_detail()
    document["quarantine"] = None if state is None else quarantine(state)
    result = run(["incident", "show", INCIDENT_ID], FakeApi().answer(ONE, document), monkeypatch)
    assert said in result.out
    assert (
        json.loads(
            run(
                ["incident", "show", INCIDENT_ID, "--format", "json"],
                FakeApi().answer(ONE, document),
                monkeypatch,
            ).out
        )["quarantine"]
        == document["quarantine"]
    )


def test_an_incident_from_an_api_without_quarantine_reads_without_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = incident_detail()
    del document["quarantine"]
    result = run(["incident", "show", INCIDENT_ID], FakeApi().answer(ONE, document), monkeypatch)
    assert result.code == Exit.OK
    assert "quarantaine : aucune" in result.out
    assert incident()["quarantine"] is None


@pytest.mark.parametrize(
    "hostile",
    [
        {**quarantine(), "state": CANARY},
        {**quarantine("lifted"), "lifted_at": None},
        CANARY,
    ],
)
def test_a_hostile_quarantine_of_an_incident_is_not_shown(
    monkeypatch: pytest.MonkeyPatch, hostile: Any
) -> None:
    document = incident_detail()
    document["quarantine"] = hostile
    result = run(["incident", "show", INCIDENT_ID], FakeApi().answer(ONE, document), monkeypatch)
    assert result.code == Exit.API_ERROR
    assert result.out == ""
    assert "/quarantine" in result.err
    assert CANARY not in result.err
