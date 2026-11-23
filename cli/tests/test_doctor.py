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
        "reemission": ("ok", None),
        "webhooks": ("ok", None),
        "proposals": ("ok", None),
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
    changes |= {
        "dead_letters": None,
        "emitters": None,
        "partitions": None,
        "reemission": None,
        "webhooks": None,
    }
    result = doctor(with_health(**changes), monkeypatch)

    found = statuses(result.out)
    assert found[check] == ("anomaly", code)
    for later in ("queue", "dead_letters", "emitters", "partitions", "reemission", "webhooks"):
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


def reemission(**changes: Any) -> dict[str, Any]:
    state = {
        "queued": 0,
        "retrying": 0,
        "lower_bound": False,
        "oldest_age_seconds": None,
        "sent": 0,
        "lost": 0,
        "rejected": 0,
    }
    return state | changes


def test_a_reemission_behind_its_threshold_is_an_anomaly_with_its_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = with_health(
        reemission=reemission(
            queued=12, retrying=12, oldest_age_seconds=900, sent=3, lost=2, rejected=1
        )
    )
    result = doctor(api, monkeypatch)

    assert result.code == Exit.ANOMALY
    report = json.loads(result.out)
    assert report["anomalies"] == ["reemission_lagging"]
    (late,) = [item for item in report["checks"] if item["check"] == "reemission"]
    assert late["message"] == (
        "La réémission vers le consommateur OpenLineage externe est en retard : 12 événements à"
        " réémettre, le plus ancien depuis 15 min (seuil : 5 min) ; 2 perdus depuis la mise en"
        " service."
    )
    assert late["remedy"] == ANOMALIES["reemission_lagging"].remedy
    assert "ANSWERLINEAGE_REEMIT_URL" in late["remedy"]
    relaxed = doctor(api, monkeypatch, "--reemission-max-age", "1000")
    assert relaxed.code == Exit.OK
    (moving,) = [
        item for item in json.loads(relaxed.out)["checks"] if item["check"] == "reemission"
    ]
    assert moving["status"] == "ok"
    assert moving["message"] == (
        "La réémission avance : 12 événements à réémettre, le plus ancien depuis 15 min ;"
        " 3 remis, 2 perdus."
    )


def test_a_reemission_that_keeps_up_says_what_left_and_what_was_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = with_health(reemission=reemission(sent=1, lost=1, rejected=1))
    (found,) = [
        item
        for item in json.loads(doctor(api, monkeypatch).out)["checks"]
        if item["check"] == "reemission"
    ]
    assert (found["status"], found["message"]) == (
        "ok",
        "La réémission avance : file vide ; 1 remis, 1 perdu.",
    )


@pytest.mark.parametrize(
    "answer",
    [health(reemission=reemission()), {k: v for k, v in health().items() if k != "reemission"}],
    ids=["no-destination", "older-api"],
)
def test_a_reemission_that_cannot_be_judged_is_not_checked(
    monkeypatch: pytest.MonkeyPatch, answer: dict[str, Any]
) -> None:
    api = FakeApi.sound().answer("/api/v1/health", answer)
    result = doctor(api, monkeypatch)
    assert result.code == Exit.OK
    assert statuses(result.out)["reemission"] == ("not_checked", None)


def webhooks(**changes: Any) -> dict[str, Any]:
    state = {
        "in_service": True,
        "queued": 0,
        "retrying": 0,
        "lower_bound": False,
        "oldest_age_seconds": None,
        "sent": 0,
        "lost": 0,
        "rejected": 0,
    }
    return state | changes


def test_webhooks_behind_their_threshold_are_an_anomaly_with_its_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = with_health(
        webhooks=webhooks(queued=2, retrying=1, oldest_age_seconds=420, sent=5, lost=1, rejected=1)
    )
    result = doctor(api, monkeypatch)

    assert result.code == Exit.ANOMALY
    report = json.loads(result.out)
    assert report["anomalies"] == ["webhooks_lagging"]
    (late,) = [item for item in report["checks"] if item["check"] == "webhooks"]
    assert late["message"] == (
        "Les webhooks des incidents sont en retard : 2 messages à envoyer, le plus ancien depuis"
        " 7 min (seuil : 5 min) ; 1 perdu depuis la mise en service."
    )
    assert late["remedy"] == ANOMALIES["webhooks_lagging"].remedy
    assert "ANSWERLINEAGE_WEBHOOK_SECRET" in late["remedy"]
    relaxed = doctor(api, monkeypatch, "--webhook-max-age", "600")
    assert relaxed.code == Exit.OK
    (moving,) = [item for item in json.loads(relaxed.out)["checks"] if item["check"] == "webhooks"]
    assert (moving["status"], moving["message"]) == (
        "ok",
        "Les webhooks avancent : 2 messages à envoyer, le plus ancien depuis 7 min ;"
        " 5 remis, 1 perdu.",
    )


def test_webhooks_left_behind_by_their_last_worker_are_still_judged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = with_health(webhooks=webhooks(in_service=False, queued=1, oldest_age_seconds=3600))
    assert statuses(doctor(api, monkeypatch).out)["webhooks"] == ("anomaly", "webhooks_lagging")


def test_webhooks_that_keep_up_say_what_left_and_what_was_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = with_health(webhooks=webhooks(sent=1, lost=2, rejected=1))
    (found,) = [
        item
        for item in json.loads(doctor(api, monkeypatch).out)["checks"]
        if item["check"] == "webhooks"
    ]
    assert (found["status"], found["message"]) == (
        "ok",
        "Les webhooks avancent : file vide ; 1 remis, 2 perdus.",
    )


@pytest.mark.parametrize(
    "answer",
    [
        health(webhooks=webhooks(in_service=False, sent=7)),
        {k: v for k, v in health().items() if k != "webhooks"},
    ],
    ids=["not-in-service", "older-api"],
)
def test_webhooks_that_cannot_be_judged_are_not_checked(
    monkeypatch: pytest.MonkeyPatch, answer: dict[str, Any]
) -> None:
    api = FakeApi.sound().answer("/api/v1/health", answer)
    result = doctor(api, monkeypatch)
    assert result.code == Exit.OK
    assert statuses(result.out)["webhooks"] == ("not_checked", None)


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
        httpx.Response(200, json=health(reemission=None)),
        httpx.Response(200, json=health(reemission=reemission(queued=CANARY))),
        httpx.Response(200, json=health(reemission=reemission(oldest_age_seconds=5))),
        httpx.Response(200, json=health(reemission=reemission(queued=1, oldest_age_seconds=None))),
        httpx.Response(200, json=health(reemission=reemission(lost=1, rejected=2))),
        httpx.Response(200, json=health(webhooks=None)),
        httpx.Response(200, json=health(webhooks=webhooks(in_service=CANARY))),
        httpx.Response(200, json=health(webhooks=webhooks(queued=1, oldest_age_seconds=None))),
        httpx.Response(200, json=health(webhooks=webhooks(lost=0, rejected=1))),
        httpx.Response(200, json=health(webhooks={CANARY: 1})),
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
        "reemission-null",
        "reemission-text",
        "reemission-empty-with-age",
        "reemission-without-age",
        "reemission-more-rejected-than-lost",
        "webhooks-null",
        "webhooks-not-a-flag",
        "webhooks-without-age",
        "webhooks-more-rejected-than-lost",
        "webhooks-unknown-field",
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
    assert run(["doctor", "--reemission-max-age", "un"], api, monkeypatch).code == Exit.USAGE
    assert run(["doctor", "--webhook-max-age", "-5"], api, monkeypatch).code == Exit.USAGE
    assert run(["doctor", "--url", "ftp://127.0.0.1"], api, monkeypatch).code == Exit.USAGE
    assert api.requests == []


def proposals(**changes: Any) -> dict[str, Any]:
    return {"open": 0, "oldest_age_seconds": None, "refused": 0, "lost": 0} | changes


def test_proposals_waiting_more_than_a_day_are_an_anomaly_with_its_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = with_health(proposals=proposals(open=3, oldest_age_seconds=90_000, refused=2, lost=1))
    result = doctor(api, monkeypatch)
    report = json.loads(result.out)
    assert result.code == Exit.ANOMALY
    assert report["anomalies"] == ["proposals_waiting"]
    (waiting,) = [item for item in report["checks"] if item["check"] == "proposals"]
    assert waiting["message"] == (
        "Des incidents proposés par des tests de qualité en échec attendent une décision : "
        "3 propositions en attente, la plus ancienne depuis 1 j 1 h (seuil : 1 j 0 h) ; "
        "2 signaux refusés, 1 perdu depuis la mise en service."
    )
    assert waiting["remedy"] == ANOMALIES["proposals_waiting"].remedy
    assert "answerlineage incident proposals --state signaled" in waiting["remedy"]


def test_recent_or_no_proposals_are_no_anomaly(monkeypatch: pytest.MonkeyPatch) -> None:
    recent = doctor(with_health(proposals=proposals(open=1, oldest_age_seconds=3_600)), monkeypatch)
    (found,) = [item for item in json.loads(recent.out)["checks"] if item["check"] == "proposals"]
    assert (found["status"], found["message"]) == (
        "ok",
        "1 proposition en attente, la plus ancienne depuis 1 h 00 min ; 0 signal refusé, 0 perdu.",
    )
    none = doctor(with_health(proposals=proposals(refused=4)), monkeypatch)
    (found,) = [item for item in json.loads(none.out)["checks"] if item["check"] == "proposals"]
    assert (
        found["message"]
        == "Aucun incident proposé n'attend de décision ; 4 signaux refusés, 0 perdu."
    )


def test_an_api_that_does_not_publish_the_proposals_leaves_them_unchecked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    older = {key: value for key, value in health().items() if key != "proposals"}
    result = doctor(FakeApi.sound().answer("/api/v1/health", older), monkeypatch)
    assert statuses(result.out)["proposals"] == ("not_checked", None)


@pytest.mark.parametrize(
    "published",
    [
        None,
        proposals(open=1),
        proposals(oldest_age_seconds=5),
        proposals(open=CANARY),
        {CANARY: 1},
    ],
    ids=["null", "waiting-without-age", "age-without-waiting", "not-a-count", "unknown-field"],
)
def test_proposals_of_another_shape_make_the_response_invalid(
    published: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = doctor(with_health(proposals=published), monkeypatch)
    assert result.code == Exit.ANOMALY
    assert json.loads(result.out)["anomalies"] == ["api_invalid_response"]
    assert CANARY not in result.out
