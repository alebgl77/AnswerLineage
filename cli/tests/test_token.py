"""Le jeton porteur de la commande : lu d'une variable ou d'un fichier, jamais cité.

L'API demande un jeton OIDC sur ses routes. La commande l'envoie sur chaque
requête, en https ou vers la boucle locale seulement, et dit chaque refus de
l'API pour le jeton par un message écrit d'avance.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from answerlineage_cli.client import ENV_TOKEN, ENV_TOKEN_FILE, MAX_TOKEN_LENGTH
from answerlineage_cli.exits import Exit
from cli.tests.fakes import INCIDENT, FakeApi, health, impact_result, json_response, run

# Un jeton inventé, cherché ensuite dans tout ce que la commande écrit.
TOKEN = "canari.jeton-cli_7e2a~+/=="  # noqa: S105 — canari de test, pas un secret.
IMPACT = ["impact", *INCIDENT, "--format", "json"]


def impact_api() -> FakeApi:
    return FakeApi().answer("/api/v1/impact", impact_result())


def test_without_token_no_authorization_header_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    api = impact_api()
    assert run(IMPACT, api, monkeypatch).code == Exit.OK
    (request,) = api.requests
    assert "authorization" not in request.headers


def test_the_token_of_the_environment_goes_on_every_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_TOKEN, TOKEN)
    api = impact_api()
    result = run(IMPACT, api, monkeypatch)
    assert result.code == Exit.OK
    (request,) = api.requests
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in result.out + result.err


@pytest.mark.parametrize("by_option", [True, False], ids=["--token-file", "variable"])
def test_the_token_may_come_from_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, by_option: bool
) -> None:
    held = tmp_path / "jeton"
    held.write_text(TOKEN + "\n", encoding="ascii")
    options = ["--token-file", str(held)] if by_option else []
    if not by_option:
        monkeypatch.setenv(ENV_TOKEN_FILE, str(held))
    api = impact_api()
    assert run([*IMPACT, *options], api, monkeypatch).code == Exit.OK
    assert api.requests[0].headers["authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize(
    "value",
    [
        f"Bearer {TOKEN}",
        "jeton avec espace",
        "jeton\nsur deux lignes",
        "x" * (MAX_TOKEN_LENGTH + 1),
        "é" + TOKEN,
    ],
    ids=["préfixe", "espace", "saut de ligne", "démesuré", "hors ASCII"],
)
def test_an_unreadable_token_is_refused_without_being_cited(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(ENV_TOKEN, value)
    api = impact_api()
    result = run(IMPACT, api, monkeypatch)
    assert result.code == Exit.USAGE
    assert api.requests == []
    assert value not in result.err
    assert TOKEN not in result.err


def test_a_token_file_and_a_token_variable_together_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = tmp_path / "jeton"
    held.write_text(TOKEN, encoding="ascii")
    monkeypatch.setenv(ENV_TOKEN, TOKEN)
    result = run([*IMPACT, "--token-file", str(held)], impact_api(), monkeypatch)
    assert result.code == Exit.USAGE
    assert TOKEN not in result.err


def test_an_unreadable_token_file_is_refused_without_its_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "canari-chemin-absent"
    result = run([*IMPACT, "--token-file", str(missing)], impact_api(), monkeypatch)
    assert result.code == Exit.USAGE
    assert "canari-chemin-absent" not in result.err


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://lignage.exemple.fr", Exit.USAGE),
        ("https://lignage.exemple.fr", Exit.OK),
        ("http://127.0.0.1:8000", Exit.OK),
        ("http://localhost:8000", Exit.OK),
    ],
)
def test_the_token_leaves_only_over_https_or_to_the_loopback(
    monkeypatch: pytest.MonkeyPatch, url: str, code: Exit
) -> None:
    monkeypatch.setenv(ENV_TOKEN, TOKEN)
    api = impact_api()
    result = run([*IMPACT, "--url", url], api, monkeypatch)
    assert result.code == code
    assert len(api.requests) == (0 if code == Exit.USAGE else 1)
    assert TOKEN not in result.err


@pytest.mark.parametrize(
    ("status", "refusal", "said"),
    [
        (401, "missing_token", "L'API demande un jeton"),
        (401, "invalid_token", "L'API refuse le jeton"),
        (401, "expired_token", "Le jeton a expiré"),
        (403, "insufficient_role", "n'a pas le rôle"),
        (503, "authentication_unavailable", "son émetteur OIDC ne répond pas"),
    ],
)
@pytest.mark.parametrize(
    "argv",
    [
        IMPACT,
        ["exposure", "--format", "json"],
        ["incident", "show", "3f0c6a52-9d1e-4b7a-8c2f-5e6d7a8b9c0d"],
        ["dead-letters", "list"],
    ],
    ids=["impact", "exposure", "incident", "dead-letters"],
)
def test_a_refusal_for_the_token_is_said_by_a_fixed_message(
    monkeypatch: pytest.MonkeyPatch, status: int, refusal: str, said: str, argv: list[str]
) -> None:
    monkeypatch.setenv(ENV_TOKEN, TOKEN)

    def refuse(_: httpx.Request) -> httpx.Response:
        return json_response({"error": refusal}, status)

    result = run(argv, httpx.MockTransport(refuse), monkeypatch)
    assert result.code == Exit.API_ERROR
    assert said in result.err
    assert TOKEN not in result.out + result.err


def test_a_refusal_code_with_another_status_is_not_taken_for_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un 403 qui n'est pas un refus de rôle reste un refus de l'adresse, dit comme tel."""
    api = FakeApi().answer("/api/v1/impact", {"error": "missing_token"}, 403)
    result = run(IMPACT, api, monkeypatch)
    assert result.code == Exit.API_ERROR
    assert "jeton" not in result.err


def doctor(api: FakeApi, monkeypatch: pytest.MonkeyPatch) -> dict[str, tuple[str, str | None]]:
    result = run(["doctor", "--format", "json"], api, monkeypatch)
    assert TOKEN not in result.out + result.err
    report = json.loads(result.out)
    return {item["check"]: (item["status"], item["code"]) for item in report["checks"]}


@pytest.mark.parametrize(
    ("status", "refusal", "anomaly"),
    [
        (401, "missing_token", "token_missing"),
        (401, "invalid_token", "token_refused"),
        (401, "expired_token", "token_refused"),
        (403, "insufficient_role", "role_insufficient"),
        (503, "authentication_unavailable", "authentication_unavailable"),
    ],
)
def test_doctor_names_a_refusal_for_the_token_and_still_checks_the_explorer(
    monkeypatch: pytest.MonkeyPatch, status: int, refusal: str, anomaly: str
) -> None:
    monkeypatch.setenv(ENV_TOKEN, TOKEN)
    api = FakeApi.sound().answer("/api/v1/health", {"error": refusal}, status)
    found = doctor(api, monkeypatch)
    assert found["api"] == ("ok", None)
    assert found["address"] == ("ok", None)
    assert found["access"] == ("anomaly", anomaly)
    assert found["database"] == ("not_checked", None)
    assert found["explorer"] == ("ok", None)


def test_doctor_says_whether_the_api_asked_for_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    without = run(["doctor"], FakeApi.sound(), monkeypatch)
    assert "authentification est désactivée" in without.out
    monkeypatch.setenv(ENV_TOKEN, TOKEN)
    api = FakeApi.sound().answer("/api/v1/health", health())
    with_token = run(["doctor"], api, monkeypatch)
    assert "L'API accepte le jeton" in with_token.out
    assert TOKEN not in with_token.out
    assert {request.headers["authorization"] for request in api.requests} == {f"Bearer {TOKEN}"}
