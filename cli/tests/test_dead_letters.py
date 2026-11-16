"""``answerlineage dead-letters`` : consulter la file de rebut, la rejouer.

La liste valide la réponse entière avant de l'afficher et dit, par son code de
sortie, si des événements attendent. Le rejeu demande un filtre, ou ``--all``
pour tout le rebut, et dit s'il reste à rejouer.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from answerlineage_cli.exits import Exit
from cli.tests.fakes import FakeApi, json_response, run, unreachable

LIST = "/api/v1/ingestion/dead-letters"
REPLAY = f"{LIST}/replay"
CANARY = "canari-6d0f"
EVENT_ID = "3f2a" + "0" * 60
OTHER_ID = "9c1e" + "f" * 60
RUN_ID = "0193a9c5-7a10-7c3e-9d5e-2f1b6c8a0002"
CURSOR = f"1794386287000000.{EVENT_ID}"
REASONS = [
    "invalid_identifier",
    "invalid_event",
    "constraint_violation",
    "invalid_value",
    "database_error",
    "projection_error",
]


def dead_letter(event_id: str = EVENT_ID, **changes: Any) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "run_id": RUN_ID,
        "emitter": "mcp://entrepot",
        "reason": "constraint_violation",
        "attempts": 1,
        "received_at": "2026-11-16T09:00:00.120000Z",
        "first_failed_at": "2026-11-16T09:00:01.500000Z",
        "last_failed_at": "2026-11-16T09:00:01.500000Z",
    } | changes


def page(
    items: list[dict[str, Any]] | None = None,
    *,
    counted: dict[str, int] | None = None,
    truncated: bool = False,
    more: bool = False,
) -> dict[str, Any]:
    shown = [dead_letter(), dead_letter(OTHER_ID, reason="database_error", attempts=10)]
    found = {"constraint_violation": 1, "database_error": 1} if counted is None else counted
    return {
        "dead_letters": shown if items is None else items,
        "counts": [{"reason": reason, "count": found.get(reason, 0)} for reason in REASONS],
        "truncated": truncated,
        "more": more,
        "next_cursor": CURSOR if more else None,
    }


def listing(document: Any, status: int = 200) -> FakeApi:
    return FakeApi().answer(LIST, document, status)


def replaying(document: Any, status: int = 202) -> FakeApi:
    return FakeApi().answer(REPLAY, document, status)


def test_the_list_shows_reason_emitter_attempts_and_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    api = listing(page())
    result = run(["dead-letters", "list"], api, monkeypatch)

    assert result.code == Exit.WAITING
    out = result.out
    assert "Rebut : 2 événements" in out
    assert "constraint_violation" in out and "l'écriture du graphe viole une contrainte" in out
    assert "database_error" in out and "la base a échoué à chaque essai" in out
    assert "2026-11-16T09:00:01.500000Z  constraint_violation  mcp://entrepot" in out
    assert f"événement {EVENT_ID}" in out
    assert f"run {RUN_ID}, reçu le 2026-11-16T09:00:00.120000Z" in out
    assert "10 essais en échec, le premier le" in out
    assert "les rejouer : answerlineage dead-letters replay --all" in out
    assert result.err == ""
    (request,) = api.requests
    assert dict(request.url.params) == {}


def test_the_list_sends_its_filters_and_page_and_suggests_the_matching_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = listing(page(more=True))
    argv = [
        "dead-letters",
        "list",
        "--emitter",
        "mcp://société",
        "--reason",
        "database_error",
        "--limit",
        "2",
        "--cursor",
        CURSOR,
    ]
    result = run(argv, api, monkeypatch)

    assert result.code == Exit.WAITING
    (request,) = api.requests
    assert dict(request.url.params) == {
        "emitter": "mcp://société",
        "reason": "database_error",
        "limit": "2",
        "cursor": CURSOR,
    }
    assert f"Page suivante : relancer avec --cursor {CURSOR}" in result.out
    assert (
        "answerlineage dead-letters replay --emitter 'mcp://société' --reason database_error"
        in result.out
    )


def test_an_empty_dead_letter_queue_has_its_own_code(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run(["dead-letters", "list"], listing(page([], counted={})), monkeypatch)
    assert result.code == Exit.OK
    assert result.out == "Rebut vide : aucun événement n'attend.\n"

    # Sous un filtre de motif, seul ce motif compte.
    narrowed = run(
        ["dead-letters", "list", "--reason", "invalid_value"],
        listing(page([], counted={"constraint_violation": 4})),
        monkeypatch,
    )
    assert narrowed.code == Exit.OK


def test_counts_beyond_their_bound_are_said_to_be_minimums(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = page(counted={"invalid_event": 100_001}, truncated=True)
    result = run(["dead-letters", "list"], listing(document), monkeypatch)
    assert "Rebut : au moins 100001 événements" in result.out


def test_the_json_output_of_the_list_is_the_response_of_the_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = page(more=True)
    result = run(["dead-letters", "list", "--format", "json"], listing(document), monkeypatch)
    assert result.code == Exit.WAITING
    assert json.loads(result.out) == document


def put(document: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    *parents, last = path.split("/")
    target: Any = document
    for key in parents:
        target = target[int(key)] if isinstance(target, list) else target[key]
    if isinstance(target, list):
        target[int(last)] = value
    else:
        target[last] = value
    return document


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("dead_letters/0/emitter", f"mcp://entrepot\x1b[2J{CANARY}"),
        ("dead_letters/0/emitter", f"mcp://entrepot\nRebut vide {CANARY}"),
        ("dead_letters/0/event_id", CANARY),
        ("dead_letters/0/run_id", CANARY),
        ("dead_letters/0/reason", CANARY),
        ("dead_letters/0/attempts", -1),
        ("dead_letters/0/last_failed_at", f"hier {CANARY}"),
        ("counts/0/reason", CANARY),
        ("counts/1/reason", "invalid_identifier"),
        ("counts/0/count", True),
        ("next_cursor", CANARY),
        ("more", True),
        ("truncated", None),
    ],
)
def test_a_hostile_list_fails_the_command_without_being_shown(
    monkeypatch: pytest.MonkeyPatch, path: str, value: Any
) -> None:
    result = run(["dead-letters", "list"], listing(put(page(), path, value)), monkeypatch)

    assert result.code == Exit.API_ERROR
    assert result.out == ""
    assert "Réponse de l'API invalide en /" in result.err
    assert CANARY not in result.err


@pytest.mark.parametrize(
    ("argv", "said"),
    [
        (["dead-letters", "list", "--emitter", f"mcp://x {CANARY}"], "la forme d'un nom"),
        (["dead-letters", "list", "--reason", CANARY], ""),
        (["dead-letters", "list", "--limit", "501"], ""),
        (["dead-letters", "list", "--cursor", CANARY], ""),
        (["dead-letters", "replay"], "--all"),
        (["dead-letters", "replay", "--all", "--reason", "invalid_event"], "--all"),
        (["dead-letters", "replay", "--event-id", CANARY], ""),
        (["dead-letters", "replay", "--all", "--limit", "10001"], ""),
        (
            [
                "dead-letters",
                "replay",
                *[part for rank in range(1001) for part in ("--event-id", f"{rank:064x}")],
            ],
            "1000 fois",
        ),
    ],
    ids=[
        "emetteur-phrase",
        "motif-inconnu",
        "page-trop-grande",
        "curseur-mal-forme",
        "rejeu-sans-filtre",
        "tout-et-un-filtre",
        "identifiant-mal-forme",
        "rejeu-trop-grand",
        "trop-d-identifiants",
    ],
)
def test_a_faulty_command_is_refused_before_any_request_without_its_value(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], said: str
) -> None:
    api = FakeApi().answer(LIST, page()).answer(REPLAY, {"replayed": 0, "more": False}, 202)
    result = run(argv, api, monkeypatch)

    assert result.code == Exit.USAGE
    assert api.requests == []
    assert said in result.err
    assert CANARY not in result.err
    assert result.out == ""


@pytest.mark.parametrize(
    ("argv", "document"),
    [
        (["--all"], {}),
        (["--reason", "constraint_violation"], {"reason": "constraint_violation"}),
        (
            ["--emitter", "mcp://crm", "--limit", "50"],
            {"emitter": "mcp://crm", "limit": 50},
        ),
        (
            ["--event-id", EVENT_ID.upper(), "--event-id", OTHER_ID, "--event-id", EVENT_ID],
            {"event_ids": [EVENT_ID, OTHER_ID]},
        ),
    ],
    ids=["tout", "motif", "emetteur-et-borne", "identifiants"],
)
def test_a_replay_sends_exactly_its_filters(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], document: dict[str, Any]
) -> None:
    api = replaying({"replayed": 2, "more": False})
    result = run(["dead-letters", "replay", *argv], api, monkeypatch)

    assert result.code == Exit.OK
    (request,) = api.requests
    assert request.method == "POST"
    assert json.loads(request.content) == document
    assert "2 événements remis en file" in result.out


@pytest.mark.parametrize(
    ("answered", "code", "said"),
    [
        ({"replayed": 1, "more": False}, Exit.OK, "1 événement remis en file"),
        ({"replayed": 1000, "more": True}, Exit.REMAINING, "relancer la même commande"),
        ({"replayed": 0, "more": True}, Exit.REMAINING, "relancer la même commande"),
        ({"replayed": 0, "more": False}, Exit.NOTHING_TO_REPLAY, "Aucun événement n'attend"),
    ],
    ids=["tout-rejoue", "a-poursuivre", "tenu-ailleurs", "rien-a-rejouer"],
)
def test_the_exit_code_of_a_replay_says_whether_more_remains(
    monkeypatch: pytest.MonkeyPatch, answered: dict[str, Any], code: Exit, said: str
) -> None:
    result = run(["dead-letters", "replay", "--all"], replaying(answered), monkeypatch)
    assert result.code == code
    assert said in result.out

    shown = run(
        ["dead-letters", "replay", "--all", "--format", "json"], replaying(answered), monkeypatch
    )
    assert json.loads(shown.out) == answered


@pytest.mark.parametrize(
    "answered",
    [{"replayed": -1, "more": False}, {"replayed": 1}, {"replayed": 1, "more": CANARY}, [CANARY]],
)
def test_a_hostile_replay_result_is_not_shown(
    monkeypatch: pytest.MonkeyPatch, answered: Any
) -> None:
    result = run(["dead-letters", "replay", "--all"], replaying(answered), monkeypatch)
    assert result.code == Exit.API_ERROR
    assert result.out == ""
    assert CANARY not in result.err


@pytest.mark.parametrize(
    ("argv", "path", "option"),
    [
        (["list", "--emitter", "mcp://x"], "/query/emitter", "--emitter"),
        (["replay", "--event-id", EVENT_ID], "/event_ids/0", "--event-id"),
        (["replay", "--all", "--limit", "5"], "/limit", "--limit"),
    ],
)
def test_a_parameter_refused_by_the_api_names_the_option(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], path: str, option: str
) -> None:
    refused = {"errors": [{"path": path, "type": "x", "message": CANARY}]}
    api = FakeApi().answer(LIST, refused, 422).answer(REPLAY, refused, 422)
    result = run(["dead-letters", *argv], api, monkeypatch)
    assert result.code == Exit.USAGE
    assert f"L'API a refusé un paramètre : {option}." in result.err
    assert CANARY not in result.err

    unnamed = {"errors": [{"path": "", "type": "unknown_field", "message": CANARY}]}
    other = run(
        ["dead-letters", *argv],
        FakeApi().answer(LIST, unnamed, 422).answer(REPLAY, unnamed, 422),
        monkeypatch,
    )
    assert other.code == Exit.USAGE
    assert "L'API a refusé un paramètre de la requête." in other.err


@pytest.mark.parametrize("argv", [["list"], ["replay", "--all"]])
def test_a_refusal_of_the_api_is_said_by_a_written_message(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    timed_out = FakeApi().answer(LIST, {"error": "query_timeout"}, 503)
    timed_out.answer(REPLAY, {"error": "query_timeout"}, 503)
    result = run(["dead-letters", *argv], timed_out, monkeypatch)
    assert result.code == Exit.API_ERROR
    assert "délai maximal" in result.err

    odd = FakeApi().answer(LIST, {"error": CANARY}, 409).answer(REPLAY, {"error": CANARY}, 409)
    other = run(["dead-letters", *argv], odd, monkeypatch)
    assert other.code == Exit.API_ERROR
    assert "HTTP 409" in other.err
    assert CANARY not in other.err

    garbled = FakeApi().route(LIST, lambda _: httpx.Response(500, text=CANARY))
    garbled.route(REPLAY, lambda _: httpx.Response(500, text=CANARY))
    assert "HTTP 500" in run(["dead-letters", *argv], garbled, monkeypatch).err
    listed = FakeApi().answer(LIST, [CANARY], 500).answer(REPLAY, [CANARY], 500)
    assert "HTTP 500" in run(["dead-letters", *argv], listed, monkeypatch).err


def test_an_unreachable_api_has_its_own_code(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = httpx.MockTransport(unreachable)
    assert run(["dead-letters", "list"], transport, monkeypatch).code == Exit.UNREACHABLE
    assert run(["dead-letters", "replay", "--all"], transport, monkeypatch).code == (
        Exit.UNREACHABLE
    )


def test_an_api_that_answers_with_another_status_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeApi().route(LIST, lambda _: json_response(page(), 201))
    assert run(["dead-letters", "list"], api, monkeypatch).code == Exit.API_ERROR
