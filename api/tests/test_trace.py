"""Remonter d'une réponse à ses sources : jeux de données, versions, outils, agents."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_api.lineage import trace
from answerlineage_api.lineage.trace import answer_key
from answerlineage_api.store.graph import (
    AgentRef,
    Answer,
    DatasetRef,
    Delivery,
    Read,
    RunRecord,
    ToolRef,
    record_run,
)
from api.tests.issuer import authentication, signed_in

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
SALES = AgentRef("agent://ventes", "assistant-commercial")
FINANCE = AgentRef("agent://finance", "analyste-tresorerie")
SQL = ToolRef("entrepot", "execute_sql")
ACCOUNTS_TOOL = ToolRef("crm", "rechercher_comptes")
RATES = DatasetRef("bigquery", "finance.ref.taux_change", version="2026-11-02")
ORDERS = DatasetRef("bigquery", "ventes.suivi.commandes")
ACCOUNTS = DatasetRef("postgres://crm:5432", "crm.public.comptes")
FINGERPRINT = "sha256:" + "ab" * 32
# Aucun émetteur n'a publié de relevé : la couverture de la remontée n'est pas mesurée.
NOT_MEASURED: dict[str, Any] = {
    "status": "unknown",
    "rate": None,
    "calls_seen": 0,
    "events_in_graph": 0,
    "events_lost": 0,
    "uninspected": 0,
    "instances": 0,
    "holes": [],
    "truncated": False,
}


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(engine, authentication=authentication()))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://localhost", headers=signed_in()
    ) as client:
        yield client


async def record(engine: AsyncEngine, *records: RunRecord) -> None:
    async with engine.begin() as conn:
        for item in records:
            await record_run(conn, item)


def seconds(count: int) -> datetime:
    return T0 + timedelta(seconds=count)


def answering(
    run_id: UUID,
    *,
    agent: AgentRef = SALES,
    fingerprint: str = FINGERPRINT,
    message: tuple[str, str] = ("slack", "C07AB/1730538030.000100"),
    parent: UUID | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        kind="agent",
        state="COMPLETE",
        event_time=seconds(30),
        agent=agent,
        parent_run_id=parent,
        answers=(
            Answer(
                fingerprint,
                deliveries=(Delivery(message[0], message[1], recipients=("p-1", "p-2")),),
            ),
        ),
    )


def calling(parent: UUID, tool: ToolRef, at: int, *reads: Read) -> RunRecord:
    return RunRecord(
        run_id=uuid4(),
        kind="tool",
        state="COMPLETE",
        event_time=seconds(at),
        tool=tool,
        parent_run_id=parent,
        reads=reads,
    )


def summary(run_id: UUID, *, agent: AgentRef | None = SALES) -> dict[str, Any]:
    return {
        "id": answer_key(run_id, FINGERPRINT),
        "fingerprint": FINGERPRINT,
        "produced_at": "2026-11-02T09:00:30Z",
        "run_id": str(run_id),
        "agent": None if agent is None else {"namespace": agent.namespace, "name": agent.name},
    }


async def sources(client: httpx.AsyncClient, run_id: UUID) -> dict[str, Any]:
    response = await client.get(f"/api/v1/answers/{answer_key(run_id, FINGERPRINT)}/sources")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


async def test_a_recipient_finds_the_answer_behind_the_message_received(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    run_id = uuid4()
    await record(engine, answering(run_id), answering(uuid4(), message=("slack", "autre")))

    response = await client.get(
        "/api/v1/answers", params={"channel": "slack", "message_ref": "C07AB/1730538030.000100"}
    )

    assert response.status_code == 200
    assert response.json() == {"answers": [summary(run_id)]}


async def test_an_unknown_message_finds_nothing(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await record(engine, answering(uuid4()))
    for params in (
        {"channel": "teams", "message_ref": "C07AB/1730538030.000100"},
        {"channel": "slack", "message_ref": "inconnu"},
    ):
        response = await client.get("/api/v1/answers", params=params)
        assert (response.status_code, response.json()) == (200, {"answers": []})


async def test_a_message_carrying_several_answers_lists_them_up_to_the_limit(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    runs = sorted((uuid4() for _ in range(3)), key=lambda run_id: run_id.hex)
    await record(engine, *(answering(run_id) for run_id in runs))
    params = {"channel": "slack", "message_ref": "C07AB/1730538030.000100"}

    everything = await client.get("/api/v1/answers", params=params)
    assert [answer["run_id"] for answer in everything.json()["answers"]] == [
        str(run_id) for run_id in runs
    ]
    first = await client.get("/api/v1/answers", params={**params, "limit": 1})
    assert [answer["run_id"] for answer in first.json()["answers"]] == [str(runs[0])]


async def test_sources_of_an_answer_are_its_datasets_versions_tools_and_agents(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    run_id = uuid4()
    await record(
        engine,
        answering(run_id),
        calling(run_id, SQL, 5, Read(RATES, "columns", ("taux", "devise")), Read(ORDERS)),
        calling(run_id, SQL, 12, Read(RATES, "columns", ("date_valeur", "taux"))),
        calling(run_id, ACCOUNTS_TOOL, 20, Read(ACCOUNTS)),
    )

    assert await sources(client, run_id) == {
        "answer": summary(run_id),
        "agents": [{"namespace": "agent://ventes", "name": "assistant-commercial"}],
        "tools": [
            {"server": "crm", "name": "rechercher_comptes", "calls": 1},
            {"server": "entrepot", "name": "execute_sql", "calls": 2},
        ],
        "datasets": [
            {
                "namespace": "bigquery",
                "name": "finance.ref.taux_change",
                "kind": "table",
                "versions": ["2026-11-02"],
                "precision": "columns",
                "columns": ["date_valeur", "devise", "taux"],
                "first_read_at": "2026-11-02T09:00:05Z",
                "last_read_at": "2026-11-02T09:00:12Z",
            },
            {
                "namespace": "bigquery",
                "name": "ventes.suivi.commandes",
                "kind": "table",
                "versions": [],
                "precision": "table",
                "columns": [],
                "first_read_at": "2026-11-02T09:00:05Z",
                "last_read_at": "2026-11-02T09:00:05Z",
            },
            {
                "namespace": "postgres://crm:5432",
                "name": "crm.public.comptes",
                "kind": "table",
                "versions": [],
                "precision": "table",
                "columns": [],
                "first_read_at": "2026-11-02T09:00:20Z",
                "last_read_at": "2026-11-02T09:00:20Z",
            },
        ],
        # De la première lecture à la seconde de la réponse, comprise.
        "period": {"from": "2026-11-02T09:00:05Z", "to": "2026-11-02T09:00:31Z"},
        "coverage": NOT_MEASURED,
        "truncated": False,
    }


async def test_a_read_spread_over_several_events_gives_its_first_and_last_time(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    run_id, call = uuid4(), uuid4()
    first = RunRecord(
        call, "tool", "START", seconds(5), tool=SQL, parent_run_id=run_id, reads=(Read(ORDERS),)
    )
    last = replace(first, state="COMPLETE", event_time=seconds(25))
    await record(engine, answering(run_id), last, first)

    (dataset,) = (await sources(client, run_id))["datasets"]
    assert (dataset["first_read_at"], dataset["last_read_at"]) == (
        "2026-11-02T09:00:05Z",
        "2026-11-02T09:00:25Z",
    )


async def test_a_table_read_whole_even_once_is_a_source_as_a_whole(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    run_id = uuid4()
    monday = DatasetRef("bigquery", "ventes.suivi.commandes", version="2026-11-02")
    tuesday = DatasetRef("bigquery", "ventes.suivi.commandes", version="2026-11-03")
    await record(
        engine,
        answering(run_id),
        calling(run_id, SQL, 5, Read(monday, "columns", ("montant",))),
        calling(run_id, SQL, 9, Read(tuesday)),
    )

    (dataset,) = (await sources(client, run_id))["datasets"]
    assert (dataset["precision"], dataset["columns"]) == ("table", [])
    assert dataset["versions"] == ["2026-11-02", "2026-11-03"]


@pytest.mark.parametrize(
    ("first", "second", "merged"),
    [
        # Quelques lignes, mais toutes leurs colonnes : aucune colonne n'est hors de la source.
        (Read(ORDERS, "subset"), Read(ORDERS, "columns", ("montant",)), ("table", [])),
        (Read(ORDERS, "keys"), Read(ORDERS, "columns", ("montant",)), ("table", [])),
        # Deux restrictions des lignes différentes : toutes les lignes, les colonnes réunies.
        (
            Read(ORDERS, "subset", ("statut",)),
            Read(ORDERS, "keys", ("montant",)),
            ("columns", ["montant", "statut"]),
        ),
        (
            Read(ORDERS, "keys", ("statut",)),
            Read(ORDERS, "keys", ("montant",)),
            ("keys", ["montant", "statut"]),
        ),
    ],
)
async def test_reads_of_a_dataset_by_several_runs_merge_without_saying_less_than_either(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    first: Read,
    second: Read,
    merged: tuple[str, list[str]],
) -> None:
    run_id = uuid4()
    await record(
        engine, answering(run_id), calling(run_id, SQL, 5, first), calling(run_id, SQL, 9, second)
    )

    (dataset,) = (await sources(client, run_id))["datasets"]
    assert (dataset["precision"], dataset["columns"]) == merged


async def test_sources_include_what_a_sub_agent_read_and_the_agent_s_own_reads(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    run_id, delegated = uuid4(), uuid4()
    await record(
        engine,
        answering(run_id),
        RunRecord(
            run_id=run_id,
            kind="agent",
            state="RUNNING",
            event_time=seconds(2),
            reads=(Read(ORDERS),),
        ),
        RunRecord(
            run_id=delegated,
            kind="agent",
            state="COMPLETE",
            event_time=seconds(20),
            agent=FINANCE,
            parent_run_id=run_id,
        ),
        calling(delegated, SQL, 10, Read(RATES, "columns", ("taux",))),
    )

    found = await sources(client, run_id)
    assert found["agents"] == [
        {"namespace": "agent://finance", "name": "analyste-tresorerie"},
        {"namespace": "agent://ventes", "name": "assistant-commercial"},
    ]
    assert found["tools"] == [{"server": "entrepot", "name": "execute_sql", "calls": 1}]
    assert [dataset["name"] for dataset in found["datasets"]] == [
        "finance.ref.taux_change",
        "ventes.suivi.commandes",
    ]


async def test_sources_leave_out_what_other_runs_read(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    run_id, parent, other = uuid4(), uuid4(), uuid4()
    await record(
        engine,
        answering(run_id, parent=parent),
        calling(run_id, SQL, 5, Read(RATES, "columns", ("taux",))),
        # Ni le run parent, ni un run voisin, ne sont des sources de cette réponse.
        RunRecord(
            run_id=parent, kind="agent", state="COMPLETE", event_time=seconds(40), agent=FINANCE
        ),
        calling(parent, ACCOUNTS_TOOL, 3, Read(ACCOUNTS)),
        answering(other, fingerprint="sha256:" + "cd" * 32, message=("slack", "autre")),
        calling(other, SQL, 7, Read(ORDERS)),
    )

    found = await sources(client, run_id)
    assert [dataset["name"] for dataset in found["datasets"]] == ["finance.ref.taux_change"]
    assert found["agents"] == [{"namespace": "agent://ventes", "name": "assistant-commercial"}]
    assert found["tools"] == [{"server": "entrepot", "name": "execute_sql", "calls": 1}]


async def test_an_answer_without_captured_read_has_no_source(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    run_id = uuid4()
    record_without_agent = RunRecord(
        run_id=run_id,
        kind="job",
        state="COMPLETE",
        event_time=seconds(30),
        answers=(Answer(FINGERPRINT),),
    )
    await record(engine, record_without_agent)

    assert await sources(client, run_id) == {
        "answer": summary(run_id, agent=None),
        "agents": [],
        "tools": [],
        "datasets": [],
        "period": {"from": "2026-11-02T09:00:30Z", "to": "2026-11-02T09:00:31Z"},
        "coverage": NOT_MEASURED,
        "truncated": False,
    }


async def test_two_answers_of_one_run_are_told_apart(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    run_id = uuid4()
    second = "sha256:" + "cd" * 32
    await record(engine, answering(run_id), answering(run_id, fingerprint=second))

    assert answer_key(run_id, second) != answer_key(run_id, FINGERPRINT)
    response = await client.get(f"/api/v1/answers/{answer_key(run_id, second)}/sources")
    assert response.json()["answer"]["fingerprint"] == second
    assert (await sources(client, run_id))["answer"]["fingerprint"] == FINGERPRINT


async def test_an_unknown_answer_is_not_found(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    run_id = uuid4()
    await record(engine, answering(run_id))
    for key in (answer_key(uuid4(), FINGERPRINT), answer_key(run_id, "sha256:autre")):
        response = await client.get(f"/api/v1/answers/{key}/sources")
        assert (response.status_code, response.json()) == (404, {"error": "unknown_answer"})


@pytest.mark.parametrize("key", ["42", "g" * 48, "A" * 48, "0" * 47, "0" * 49, "%00" * 16])
async def test_a_malformed_identifier_is_refused_without_being_echoed(
    client: httpx.AsyncClient, key: str
) -> None:
    response = await client.get(f"/api/v1/answers/{key}/sources")
    assert response.status_code == 422
    (error,) = response.json()["errors"]
    assert error["path"] == "/path/answer_id"
    assert set(error) == {"path", "type", "message"}
    assert key not in response.text


@pytest.mark.parametrize(
    ("params", "path"),
    [
        ({"channel": "slack"}, "/query/message_ref"),
        ({"message_ref": "m"}, "/query/channel"),
        ({"channel": "", "message_ref": "m"}, "/query/channel"),
        ({"channel": "s" * 201, "message_ref": "m"}, "/query/channel"),
        ({"channel": "slack", "message_ref": "m" * 501}, "/query/message_ref"),
        ({"channel": "slack", "message_ref": "a\x00b"}, "/query/message_ref"),
        ({"channel": "slack", "message_ref": "m", "limit": "0"}, "/query/limit"),
        ({"channel": "slack", "message_ref": "m", "limit": "101"}, "/query/limit"),
        ({"channel": "slack", "message_ref": "m", "limit": "dix"}, "/query/limit"),
    ],
)
async def test_a_search_names_one_message_within_bounds(
    client: httpx.AsyncClient, params: dict[str, str], path: str
) -> None:
    response = await client.get("/api/v1/answers", params=params)
    assert response.status_code == 422
    assert [error["path"] for error in response.json()["errors"]] == [path]
    assert "dix" not in response.text


async def test_runs_that_name_each_other_as_parent_do_not_hang_the_search(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    run_id, child = uuid4(), uuid4()
    await record(
        engine,
        answering(run_id, parent=child),
        RunRecord(
            run_id=child,
            kind="agent",
            state="COMPLETE",
            event_time=seconds(20),
            agent=FINANCE,
            parent_run_id=run_id,
            reads=(Read(ORDERS),),
        ),
    )

    found = await sources(client, run_id)
    assert [dataset["name"] for dataset in found["datasets"]] == ["ventes.suivi.commandes"]
    assert len(found["agents"]) == 2
    assert found["truncated"] is False


async def test_a_run_tree_beyond_the_bound_is_reported_as_truncated(
    client: httpx.AsyncClient, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = uuid4()
    await record(
        engine,
        answering(run_id),
        *(calling(run_id, SQL, 5 + rank, Read(ORDERS)) for rank in range(4)),
    )

    monkeypatch.setattr(trace, "MAX_RUNS", 5)
    assert (await sources(client, run_id))["truncated"] is False
    monkeypatch.setattr(trace, "MAX_RUNS", 3)
    bounded = await sources(client, run_id)
    assert bounded["truncated"] is True
    assert sum(tool["calls"] for tool in bounded["tools"]) <= 3


async def test_beyond_the_bound_on_runs_the_sources_keep_the_first_calls_by_identifier(
    client: httpx.AsyncClient, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = uuid4()
    datasets = [DatasetRef("bigquery", f"ventes.suivi.table_{rank}") for rank in range(4)]
    # Les appels arrivent dans l'ordre inverse de leurs identifiants.
    await record(
        engine,
        answering(run_id),
        *(
            replace(calling(run_id, SQL, 5 + rank, Read(dataset)), run_id=UUID(int=rank + 1))
            for rank, dataset in reversed(list(enumerate(datasets)))
        ),
    )

    # Le run de la réponse, puis ses deux appels de plus petit identifiant.
    monkeypatch.setattr(trace, "MAX_RUNS", 3)
    bounded = await sources(client, run_id)
    assert bounded["truncated"] is True
    assert [dataset["name"] for dataset in bounded["datasets"]] == [
        datasets[0].name,
        datasets[1].name,
    ]


async def test_reads_beyond_the_bound_are_reported_as_truncated(
    client: httpx.AsyncClient, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = uuid4()
    await record(
        engine,
        answering(run_id),
        calling(run_id, SQL, 5, Read(RATES, "columns", ("taux",)), Read(ORDERS)),
        calling(run_id, ACCOUNTS_TOOL, 20, Read(ACCOUNTS)),
    )

    monkeypatch.setattr(trace, "MAX_READS", 3)
    assert (await sources(client, run_id))["truncated"] is False
    monkeypatch.setattr(trace, "MAX_READS", 2)
    bounded = await sources(client, run_id)
    assert bounded["truncated"] is True
    # Les lectures les plus anciennes d'abord : la dernière n'a pas été lue.
    assert [dataset["name"] for dataset in bounded["datasets"]] == [
        "finance.ref.taux_change",
        "ventes.suivi.commandes",
    ]
