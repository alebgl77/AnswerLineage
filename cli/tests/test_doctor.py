"""``answerlineage doctor`` : chaque composant vérifié, chaque anomalie nommée avec son remède.

Les messages et les remèdes sont écrits d'avance : aucun texte reçu de l'API
n'y est recopié. Ce qui ne peut pas être vérifié est dit « non vérifié ».
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from answerlineage_cli.doctor import ANOMALIES, CHECKS
from answerlineage_cli.exits import Exit
from cli.tests.fakes import FakeApi, health, json_response, run, unreachable

CANARY = "canari-5c1d"


def statuses(out: str) -> dict[str, tuple[str, str | None]]:
    report = json.loads(out)
    return {item["check"]: (item["status"], item["code"]) for item in report["checks"]}


def doctor(api: FakeApi | httpx.BaseTransport, monkeypatch: pytest.MonkeyPatch, *extra: str) -> Any:
    return run(["doctor", "--format", "json", *extra], api, monkeypatch)


def with_health(**changes: Any) -> FakeApi:
    return FakeApi.sound().answer("/api/v1/health", health(**changes))


def test_a_sound_installation_has_no_anomaly(monkeypatch: pytest.MonkeyPatch) -> None:
    result = doctor(FakeApi.sound(), monkeypatch)

    assert result.code == Exit.OK
    report = json.loads(result.out)
    assert report["anomalies"] == []
    assert statuses(result.out) == {
        "api": ("ok", None),
        "address": ("ok", None),
        "database": ("ok", None),
        "migrations": ("ok", None),
        "queue": ("ok", None),
        "dead_letters": ("ok", None),
        "emitters": ("ok", None),
        "partitions": ("ok", None),
        "explorer": ("ok", None),
        "workers": ("not_checked", None),
    }
    assert [item["check"] for item in report["checks"]] == list(CHECKS)
    (buried,) = [item for item in report["checks"] if item["check"] == "dead_letters"]
    assert buried["message"] == "La file de rebut est vide."


def test_the_readable_report_says_each_check_and_the_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run(
        ["doctor"],
        with_health(queue={"pending": 4, "lower_bound": False, "oldest_age_seconds": 7200}),
        monkeypatch,
    )

    assert result.code == Exit.ANOMALY
    assert "API 1.0.0" in result.out
    assert "anomalie" in result.out
    assert "queue_stalled" in result.out
    assert "Remède : " in result.out
    assert "non vérifié" in result.out
    assert "1 anomalie." in result.out


def test_a_sound_readable_report_ends_without_anomaly(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run(["doctor"], FakeApi.sound(), monkeypatch)
    assert result.code == Exit.OK
    assert "Aucune anomalie." in result.out


def test_an_unreachable_api_leaves_everything_else_unchecked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = doctor(httpx.MockTransport(unreachable), monkeypatch)

    assert result.code == Exit.ANOMALY
    found = statuses(result.out)
    assert found.pop("api") == ("anomaly", "api_unreachable")
    assert set(found.values()) == {("not_checked", None)}


@pytest.mark.parametrize(
    ("refusal", "code"),
    [("host_not_allowed", "host_not_allowed"), ("cross_site_not_allowed", "address_refused")],
)
def test_an_address_refused_by_the_api_is_an_anomaly(
    monkeypatch: pytest.MonkeyPatch, refusal: str, code: str
) -> None:
    api = FakeApi.sound().answer("/api/v1/health", {"error": refusal}, 403)
    result = doctor(api, monkeypatch)

    found = statuses(result.out)
    assert found["api"] == ("ok", None)
    assert found["address"] == ("anomaly", code)
    assert found["database"] == ("not_checked", None)
    assert found["explorer"] == ("not_checked", None)


def test_an_origin_refused_by_the_api_is_named_and_the_rest_is_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def guarded(request: httpx.Request) -> httpx.Response:
        if "origin" in request.headers:
            return json_response({"error": "origin_not_allowed"}, 403)
        return json_response(health())

    api = FakeApi.sound().route("/api/v1/health", guarded)
    result = doctor(api, monkeypatch)

    found = statuses(result.out)
    assert found["address"] == ("anomaly", "origin_not_allowed")
    assert found["migrations"] == ("ok", None)
    assert found["explorer"] == ("ok", None)
    # L'origine envoyée est celle de l'adresse utilisée.
    assert {request.headers.get("origin") for request in api.requests} >= {"http://127.0.0.1:8000"}


@pytest.mark.parametrize(
    ("changes", "check", "code"),
    [
        (
            {"database": "unavailable", "migrations": None, "queue": None},
            "database",
            "database_unavailable",
        ),
        ({"migrations": "not_migrated", "queue": None}, "migrations", "schema_not_migrated"),
        ({"migrations": "behind", "queue": None}, "migrations", "schema_behind"),
        (
            {"migrations": "unknown_revision", "queue": None},
            "migrations",
            "schema_unknown_revision",
        ),
    ],
)
def test_a_database_or_schema_anomaly_leaves_what_depends_on_it_unchecked(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any], check: str, code: str
) -> None:
    changes |= {"dead_letters": None, "emitters": None, "partitions": None}
    result = doctor(with_health(**changes), monkeypatch)

    found = statuses(result.out)
    assert found[check] == ("anomaly", code)
    for later in ("queue", "dead_letters", "emitters", "partitions"):
        assert found[later] == ("not_checked", None)
    assert found["explorer"] == ("ok", None)
    assert json.loads(result.out)["anomalies"] == [code]


def test_a_slow_database_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeApi.sound().answer("/api/v1/health", {"error": "query_timeout"}, 503)
    found = statuses(doctor(api, monkeypatch).out)
    assert found["database"] == ("anomaly", "database_unavailable")
    assert found["migrations"] == ("not_checked", None)


def test_a_queue_that_does_not_move_is_an_anomaly_beyond_the_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = with_health(queue={"pending": 12, "lower_bound": False, "oldest_age_seconds": 600})
    assert statuses(doctor(api, monkeypatch).out)["queue"] == ("anomaly", "queue_stalled")
    relaxed = doctor(api, monkeypatch, "--queue-max-age", "900")
    assert relaxed.code == Exit.OK
    assert statuses(relaxed.out)["queue"] == ("ok", None)


def test_events_waiting_in_the_dead_letters_are_an_anomaly_with_its_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = with_health(dead_letters={"events": 3, "lower_bound": False, "oldest_age_seconds": 5400})
    result = doctor(api, monkeypatch)

    assert result.code == Exit.ANOMALY
    report = json.loads(result.out)
    assert report["anomalies"] == ["dead_letters_waiting"]
    (buried,) = [item for item in report["checks"] if item["check"] == "dead_letters"]
    assert buried["status"] == "anomaly"
    assert buried["message"] == (
        "3 événements au rebut, que les workers n'ont pas pu intégrer au graphe ;"
        " le plus ancien y est depuis 1 h 30 min."
    )
    # Le remède écrit d'avance : lire les motifs, puis rejouer une fois la cause corrigée.
    assert buried["remedy"] == ANOMALIES["dead_letters_waiting"].remedy
    assert "answerlineage dead-letters list" in buried["remedy"]
    assert "answerlineage dead-letters replay" in buried["remedy"]
    readable = run(["doctor"], api, monkeypatch)
    assert "dead_letters_waiting : 3 événements au rebut" in readable.out
    assert f"Remède : {ANOMALIES['dead_letters_waiting'].remedy}" in readable.out


def test_a_dead_letter_count_beyond_its_bound_says_it_is_a_minimum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = with_health(dead_letters={"events": 10000, "lower_bound": True, "oldest_age_seconds": 30})
    (buried,) = [
        item
        for item in json.loads(doctor(api, monkeypatch).out)["checks"]
        if item["check"] == "dead_letters"
    ]
    assert buried["message"].startswith("Plus de 10000 événements au rebut")


def test_muted_emitters_and_missing_partitions_are_anomalies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = with_health(
        emitters={"known": 3, "muted": 2, "lower_bound": False},
        partitions={"missing": 1, "lower_bound": False},
    )
    result = doctor(api, monkeypatch)
    report = json.loads(result.out)
    assert report["anomalies"] == ["emitters_muted", "partitions_missing"]
    (muted,) = [item for item in report["checks"] if item["check"] == "emitters"]
    assert muted["message"].startswith("2 émetteurs muets")
    assert muted["remedy"] == ANOMALIES["emitters_muted"].remedy


def test_without_any_emitter_their_silence_is_not_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    api = with_health(emitters={"known": 0, "muted": 0, "lower_bound": False})
    result = doctor(api, monkeypatch)
    assert result.code == Exit.OK
    assert statuses(result.out)["emitters"] == ("not_checked", None)


@pytest.mark.parametrize(
    "explorer",
    [
        httpx.Response(404, json={"detail": "Not Found"}),
        httpx.Response(200, json={"page": CANARY}),
        httpx.Response(200, text=f"<html>{CANARY}</html>", headers={"content-type": "text/html"}),
    ],
    ids=["absent", "json", "another-page"],
)
def test_an_address_that_does_not_serve_the_explorer_is_an_anomaly(
    monkeypatch: pytest.MonkeyPatch, explorer: httpx.Response
) -> None:
    api = FakeApi.sound().route("/", lambda _: explorer)
    result = doctor(api, monkeypatch)
    assert statuses(result.out)["explorer"] == ("anomaly", "explorer_missing")
    assert CANARY not in result.out


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, text=CANARY),
        httpx.Response(200, json=health(migrations=CANARY)),
        httpx.Response(200, json=health(api_version=CANARY)),
        httpx.Response(200, json=health(queue={"pending": "1", "lower_bound": False})),
        httpx.Response(
            200,
            json=health(
                dead_letters={"events": CANARY, "lower_bound": False, "oldest_age_seconds": 1}
            ),
        ),
        httpx.Response(
            200,
            json=health(dead_letters={"events": -1, "lower_bound": False, "oldest_age_seconds": 1}),
        ),
        httpx.Response(
            200,
            json=health(
                dead_letters={"events": 2, "lower_bound": False, "oldest_age_seconds": None}
            ),
        ),
        httpx.Response(
            200,
            json=health(dead_letters={"events": 0, "lower_bound": False, "oldest_age_seconds": 9}),
        ),
        httpx.Response(200, json=health(dead_letters=None)),
        httpx.Response(
            200, json={k: v for k, v in health().items() if k != "dead_letters"} | {CANARY: 1}
        ),
        httpx.Response(500, json={"detail": CANARY}),
        httpx.Response(404, json={"detail": "Not Found"}),
        httpx.Response(403, json={"error": CANARY}),
    ],
    ids=[
        "not-json",
        "unknown-state",
        "version",
        "wrong-type",
        "dead-letters-text",
        "dead-letters-negative",
        "dead-letters-without-age",
        "dead-letters-empty-with-age",
        "dead-letters-null",
        "dead-letters-missing",
        "server-error",
        "no-route",
        "403",
    ],
)
def test_an_unexpected_answer_of_the_api_is_an_anomaly_that_cites_nothing(
    monkeypatch: pytest.MonkeyPatch, answer: httpx.Response
) -> None:
    api = FakeApi.sound().route("/api/v1/health", lambda _: answer)
    for output in ("json", "text"):
        result = run(["doctor", "--format", output], api, monkeypatch)
        assert result.code == Exit.ANOMALY
        assert CANARY not in result.out + result.err
    assert statuses(doctor(api, monkeypatch).out)["api"][1] in {
        "api_invalid_response",
        "api_version_unsupported",
    }


def test_another_major_version_of_the_api_is_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    result = doctor(with_health(api_version="2.0.0"), monkeypatch)
    found = statuses(result.out)
    assert found["api"] == ("anomaly", "api_version_unsupported")
    assert found["database"] == ("not_checked", None)


def test_every_anomaly_has_a_message_and_a_remedy_written_in_advance() -> None:
    assert ANOMALIES
    for code, anomaly in ANOMALIES.items():
        assert code.isidentifier() and code.islower()
        assert anomaly.check in CHECKS
        assert anomaly.message.endswith(".")
        assert anomaly.remedy.endswith(".")


def test_a_usage_error_of_doctor_has_its_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeApi.sound()
    assert run(["doctor", "--queue-max-age", "-1"], api, monkeypatch).code == Exit.USAGE
    assert run(["doctor", "--url", "ftp://127.0.0.1"], api, monkeypatch).code == Exit.USAGE
    assert api.requests == []
