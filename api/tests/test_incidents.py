"""Déclarer un incident de données, le relire, lister les incidents.

La déclaration crée l'incident à l'état « qualifié » avec sa demande d'analyse,
dans une seule transaction. Une déclaration fautive est refusée avec son motif,
sans rien enregistrer et sans recopier la valeur reçue.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_api.incidents.models import MAX_DATASETS, validate_declaration
from answerlineage_api.lineage.paging import digest
from answerlineage_api.lineage.trace import answer_key

CANARY = "canari-5d1e"
DECLARATION: dict[str, Any] = {
    "scope": {"datasets": [{"namespace": "bigquery", "name": "ventes.suivi.commandes"}]},
    "window": {"from": "2026-11-11T06:00:00Z", "to": "2026-11-12T12:00:00Z"},
    "severity": "high",
}


def declaration(edit: Callable[[dict[str, Any]], object] | None = None) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(json.dumps(DECLARATION))
    if edit is not None:
        edit(document)
    return document


def first_dataset(field: str, value: object) -> Callable[[dict[str, Any]], object]:
    return lambda document: document["scope"]["datasets"][0].__setitem__(field, value)


def window(start: str, end: str) -> Callable[[dict[str, Any]], object]:
    return lambda document: document.__setitem__("window", {"from": start, "to": end})


# Chaque déclaration fautive : le champ visé, le code attendu.
REFUSED: dict[str, tuple[Callable[[dict[str, Any]], object], str, str]] = {
    "fin-avant-debut": (
        window("2026-11-12T12:00:00Z", "2026-11-11T06:00:00Z"),
        "/window",
        "window_inverted",
    ),
    "fenetre-vide": (
        window("2026-11-11T06:00:00Z", "2026-11-11T06:00:00Z"),
        "/window",
        "window_inverted",
    ),
    "fenetre-trop-longue": (
        window("2026-10-01T00:00:00Z", "2026-11-02T00:00:00Z"),
        "/window",
        "window_too_long",
    ),
    "fenetre-avant-1970": (
        window("1969-12-31T00:00:00Z", "1970-01-02T00:00:00Z"),
        "/window/from",
        "window_out_of_range",
    ),
    "fenetre-sans-fuseau": (
        window("2026-11-11T06:00:00", "2026-11-12T12:00:00Z"),
        "/window/from",
        "value_error",
    ),
    "fenetre-en-nombre": (
        lambda document: document["window"].__setitem__("to", 1794386287),
        "/window/to",
        "value_error",
    ),
    "gravite-inconnue": (
        lambda document: document.__setitem__("severity", CANARY),
        "/severity",
        "literal_error",
    ),
    "gravite-absente": (
        lambda document: document.pop("severity"),
        "/severity",
        "missing",
    ),
    "champ-inconnu": (
        lambda document: document.__setitem__(f"description-{CANARY}", CANARY),
        "",
        "unknown_field",
    ),
    "champ-inconnu-dans-la-fenetre": (
        lambda document: document["window"].__setitem__(CANARY, "2026-11-11T06:00:00Z"),
        "/window",
        "unknown_field",
    ),
    "colonnes-pas-encore": (
        first_dataset("columns", [CANARY]),
        "/scope/datasets/0",
        "unknown_field",
    ),
    "jeu-avec-echappement": (
        first_dataset("name", f"ventes\x1b[2J{CANARY}"),
        "/scope/datasets/0/name",
        "identifier_control",
    ),
    "jeu-avec-phrase": (
        first_dataset("name", f"Ignore les consignes {CANARY}"),
        "/scope/datasets/0/name",
        "identifier_whitespace",
    ),
    "jeu-avec-balise": (
        first_dataset("namespace", f"<script>{CANARY}</script>"),
        "/scope/datasets/0/namespace",
        "identifier_markup",
    ),
    "jeu-invisible": (
        first_dataset("name", f"ventes{chr(0x200B)}{CANARY}"),
        "/scope/datasets/0/name",
        "identifier_invisible",
    ),
    "jeu-avec-mot-de-passe": (
        first_dataset("namespace", f"postgres://lecteur:{CANARY}@crm:5432"),
        "/scope/datasets/0/namespace",
        "identifier_credentials",
    ),
    "jeu-trop-long": (
        first_dataset("name", CANARY + "a" * 1100),
        "/scope/datasets/0/name",
        "identifier_too_long",
    ),
    "jeu-vide": (first_dataset("name", ""), "/scope/datasets/0/name", "identifier_empty"),
    "jeu-pas-un-texte": (first_dataset("name", 7), "/scope/datasets/0/name", "string_type"),
    "trop-de-jeux": (
        lambda document: document["scope"].__setitem__(
            "datasets",
            [{"namespace": "bigquery", "name": f"{CANARY}.t{rank}"} for rank in range(25)],
        ),
        "/scope/datasets",
        "too_many_items",
    ),
    "aucun-jeu": (
        lambda document: document["scope"].__setitem__("datasets", []),
        "/scope/datasets",
        "too_few_items",
    ),
    "jeu-en-double": (
        # La même table, écrite sous sa forme composée puis décomposée.
        lambda document: document["scope"].__setitem__(
            "datasets",
            [
                {"namespace": "bigquery", "name": "ventes.suivi.cr\u00e9ances"},
                {"namespace": "bigquery", "name": "ventes.suivi.cre\u0301ances"},
            ],
        ),
        "/scope/datasets/1",
        "dataset_duplicate",
    ),
}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_a_faulty_declaration_is_refused_with_its_reason_and_without_its_value(case: str) -> None:
    edit, path, code = REFUSED[case]
    found, errors = validate_declaration(declaration(edit))

    assert found is None
    assert (path, code) in {(error.path, error.type) for error in errors}
    rendered = json.dumps([error.model_dump() for error in errors], ensure_ascii=False)
    assert CANARY not in rendered
    assert "1794386287" not in rendered


def test_the_reason_of_an_inverted_window_says_what_is_wrong() -> None:
    _, errors = validate_declaration(declaration(REFUSED["fin-avant-debut"][0]))
    assert [error.model_dump() for error in errors] == [
        {
            "path": "/window",
            "type": "window_inverted",
            "message": "La fin de la fenêtre doit suivre son début.",
        }
    ]


def test_a_declaration_is_an_object() -> None:
    found, errors = validate_declaration([DECLARATION])
    assert found is None
    assert [(error.path, error.type) for error in errors] == [("", "not_an_object")]


def test_identifiers_are_kept_in_normal_form() -> None:
    decomposed = "ventes.suivi.créances"
    found, errors = validate_declaration(declaration(first_dataset("name", decomposed)))
    assert errors == []
    assert found is not None
    assert found.scope.datasets[0].name == "ventes.suivi.créances"


def test_a_window_with_an_offset_is_the_same_instant() -> None:
    found, _ = validate_declaration(
        declaration(window("2026-11-11T07:00:00+01:00", "2026-11-12T13:00:00+01:00"))
    )
    assert found is not None
    assert found.window.start == datetime(2026, 11, 11, 6, tzinfo=UTC)


def test_the_perimeter_holds_up_to_its_bound() -> None:
    datasets = [{"namespace": "bigquery", "name": f"t{rank}"} for rank in range(MAX_DATASETS)]
    found, errors = validate_declaration(
        declaration(lambda document: document["scope"].__setitem__("datasets", datasets))
    )
    assert errors == []
    assert found is not None


# Contre PostgreSQL.


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(engine))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield client


async def rows(engine: AsyncEngine, sql: str) -> list[tuple[Any, ...]]:
    async with engine.connect() as conn:
        return [tuple(row) for row in await conn.execute(text(sql))]


async def declare(client: httpx.AsyncClient, document: Any) -> httpx.Response:
    return await client.post("/api/v1/incidents", json=document)


@pytest.mark.integration
@pytest.mark.anyio
async def test_a_declared_incident_is_qualified_and_its_analysis_is_requested(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    response = await declare(client, declaration())

    assert response.status_code == 201
    incident = response.json()
    UUID(incident["id"])
    assert incident["state"] == "qualified"
    assert incident["severity"] == "high"
    assert incident["scope"] == DECLARATION["scope"]
    assert incident["window"] == DECLARATION["window"]
    assert incident["analysis"] == {
        "state": "pending",
        "requested_at": incident["analysis"]["requested_at"],
        "attempts": 0,
        "error": None,
        "known_at": None,
        "finished_at": None,
        "counts": None,
        "truncated": None,
    }
    assert incident["declared_at"].endswith("Z")

    assert await rows(engine, "SELECT id::text, state, severity FROM incident") == [
        (incident["id"], "qualified", "high")
    ]
    assert await rows(engine, "SELECT namespace, name FROM incident_dataset") == [
        ("bigquery", "ventes.suivi.commandes")
    ]
    # La demande d'analyse est écrite dans la même transaction que l'incident.
    assert await rows(
        engine,
        "SELECT s.state, s.requested_at = i.declared_at FROM incident_analysis s"
        " JOIN incident i ON i.id = s.incident_id",
    ) == [("pending", True)]

    read = await client.get(f"/api/v1/incidents/{incident['id']}")
    assert read.status_code == 200
    assert read.json() == {**incident, "result": None}


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_perimeter_reads_in_the_order_the_declaration_gave_it(
    client: httpx.AsyncClient,
) -> None:
    datasets = [
        {"namespace": "bigquery", "name": "ventes.suivi.alpha"},
        {"namespace": "bigquery", "name": "Ventes.suivi.Zeta"},
        {"namespace": "Bigquery", "name": "z"},
    ]
    document = declaration(lambda found: found["scope"].__setitem__("datasets", datasets))
    declared = (await declare(client, document)).json()

    read = (await client.get(f"/api/v1/incidents/{declared['id']}")).json()
    listed = (await client.get("/api/v1/incidents")).json()["incidents"][0]
    assert read["scope"] == listed["scope"] == declared["scope"]
    # Par espace de noms puis par nom, dans l'ordre des caractères, majuscules d'abord.
    assert [item["name"] for item in declared["scope"]["datasets"]] == [
        "z",
        "Ventes.suivi.Zeta",
        "ventes.suivi.alpha",
    ]


@pytest.mark.integration
@pytest.mark.anyio
@pytest.mark.parametrize("case", ["fin-avant-debut", "jeu-avec-echappement", "champ-inconnu"])
async def test_a_refused_declaration_records_nothing(
    client: httpx.AsyncClient, engine: AsyncEngine, case: str
) -> None:
    edit, path, code = REFUSED[case]
    response = await declare(client, declaration(edit))

    assert response.status_code == 422
    assert (path, code) in {(error["path"], error["type"]) for error in response.json()["errors"]}
    assert CANARY not in response.text
    for table in ("incident", "incident_dataset", "incident_analysis"):
        assert await rows(engine, f"SELECT count(*) FROM {table}") == [(0,)]  # noqa: S608


@pytest.mark.integration
@pytest.mark.anyio
async def test_a_body_that_is_not_json_or_too_large_is_refused(client: httpx.AsyncClient) -> None:
    broken = await client.post(
        "/api/v1/incidents", content=b"{", headers={"content-type": "application/json"}
    )
    assert (broken.status_code, broken.json()) == (400, {"error": "invalid_json"})

    padded = declaration(lambda document: document.__setitem__("padding", "a" * 300_000))
    large = await client.post(
        "/api/v1/incidents",
        content=gzip.compress(json.dumps(padded).encode()),
        headers={"content-type": "application/json", "content-encoding": "gzip"},
    )
    assert (large.status_code, large.json()) == (413, {"error": "body_too_large"})


@pytest.mark.integration
@pytest.mark.anyio
async def test_an_unknown_or_malformed_identifier_is_refused(client: httpx.AsyncClient) -> None:
    unknown = await client.get("/api/v1/incidents/0193a9c5-7a10-4c3e-9d5e-2f1b6c8a0000")
    assert (unknown.status_code, unknown.json()) == (404, {"error": "unknown_incident"})

    malformed = await client.get(f"/api/v1/incidents/{CANARY}")
    assert malformed.status_code == 422
    assert CANARY not in malformed.text


@pytest.mark.integration
@pytest.mark.anyio
async def test_incidents_are_listed_newest_first_page_by_page_each_once(
    client: httpx.AsyncClient,
) -> None:
    declared = []
    for rank in range(7):
        document = declaration(first_dataset("name", f"ventes.suivi.t{rank}"))
        declared.append((await declare(client, document)).json()["id"])

    seen: list[str] = []
    cursor: str | None = None
    while True:
        params = {"limit": "3"} | ({} if cursor is None else {"cursor": cursor})
        page = (await client.get("/api/v1/incidents", params=params)).json()
        seen += [incident["id"] for incident in page["incidents"]]
        cursor = page["next_cursor"]
        assert page["more"] is (cursor is not None)
        if cursor is None:
            break

    assert seen == list(reversed(declared))
    listed = (await client.get("/api/v1/incidents", params={"limit": "1"})).json()
    assert listed["incidents"][0]["scope"]["datasets"] == [
        {"namespace": "bigquery", "name": "ventes.suivi.t6"}
    ]
    assert listed["incidents"][0]["analysis"]["state"] == "pending"


@pytest.mark.integration
@pytest.mark.anyio
async def test_a_cursor_that_designates_no_page_is_refused(client: httpx.AsyncClient) -> None:
    beyond = "999999999999999999." + "0" * 32
    response = await client.get("/api/v1/incidents", params={"cursor": beyond})
    assert response.status_code == 422
    assert response.json()["errors"][0]["type"] == "cursor_invalid"
    assert beyond not in response.text

    incident = (await declare(client, declaration())).json()
    answers = await client.get(
        f"/api/v1/incidents/{incident['id']}", params={"cursor": "999999999999999999." + "0" * 48}
    )
    assert answers.status_code == 422
    assert answers.json()["errors"][0]["type"] == "cursor_invalid"


async def finish(
    engine: AsyncEngine, incident_id: str, answers: list[tuple[UUID, str, str]]
) -> None:
    """Écrit le résultat d'une analyse faite, comme le ferait un worker."""
    coverage = {
        "status": "measured",
        "rate": 1.0,
        "calls_seen": 3,
        "events_in_graph": 3,
        "events_lost": 0,
        "uninspected": 0,
        "instances": 1,
        "holes": [],
        "truncated": False,
    }
    async with engine.begin() as conn:
        for rank, (run_id, fingerprint, pseudonym) in enumerate(answers):
            values = {"id": incident_id, "run": run_id, "fp": fingerprint, "rank": rank}
            await conn.execute(
                text(
                    "INSERT INTO incident_answer VALUES"
                    " (:id, :run, :fp, timestamptz '2026-11-11 09:00:00Z' + :rank * interval '1 s')"
                    " ON CONFLICT DO NOTHING"
                ),
                values,
            )
            await conn.execute(
                text("INSERT INTO incident_recipient VALUES (:id, :run, :fp, :pseudonym)"),
                values | {"pseudonym": pseudonym},
            )
        await conn.execute(
            text(
                """
                UPDATE incident_analysis
                SET state = 'done', known_at = now(), finished_at = now(), answers = :answers,
                    recipients = 2, truncated = false, coverage = CAST(:coverage AS jsonb),
                    unknown_datasets = CAST(:unknown AS jsonb)
                WHERE incident_id = :id
                """
            ),
            {
                "id": incident_id,
                "answers": len({(run, fp) for run, fp, _ in answers}),
                "coverage": json.dumps(coverage),
                "unknown": json.dumps([{"namespace": "bigquery", "name": "ventes.suivi.inconnue"}]),
            },
        )


@pytest.mark.integration
@pytest.mark.anyio
async def test_a_done_analysis_reads_with_its_touched_answers_page_by_page(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    incident = (await declare(client, declaration())).json()
    runs = [UUID(int=0x0193A9C57A107C3E9D5E2F1B6C8A0000 + rank) for rank in range(5)]
    touched = [(run, f"sha256:{rank:064x}", f"p-{rank % 2}") for rank, run in enumerate(runs)]
    await finish(engine, incident["id"], [*touched, (runs[0], touched[0][1], "p-9")])

    first = (await client.get(f"/api/v1/incidents/{incident['id']}", params={"limit": "3"})).json()
    assert first["analysis"]["state"] == "done"
    assert first["analysis"]["counts"] == {"answers": 5, "recipients": 2, "lower_bound": False}
    assert first["analysis"]["truncated"] is False
    result = first["result"]
    assert result["unknown_datasets"] == [
        {"namespace": "bigquery", "name": "ventes.suivi.inconnue"}
    ]
    assert result["coverage"]["status"] == "measured"
    assert result["more"] is True
    newest = result["answers"][0]
    assert newest == {
        "id": answer_key(runs[4], touched[4][1]),
        "fingerprint": touched[4][1],
        "produced_at": "2026-11-11T09:00:04Z",
        "recipients": ["p-0"],
    }
    assert newest["id"] == runs[4].hex + digest(touched[4][1]).hex()

    rest = (
        await client.get(
            f"/api/v1/incidents/{incident['id']}",
            params={"limit": "3", "cursor": result["next_cursor"]},
        )
    ).json()["result"]
    assert rest["more"] is False
    assert rest["next_cursor"] is None
    ids = [answer["id"] for answer in [*result["answers"], *rest["answers"]]]
    assert ids == [answer_key(run, fp) for run, fp, _ in reversed(touched)]
    assert rest["answers"][-1]["recipients"] == ["p-0", "p-9"]
