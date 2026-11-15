"""Le graphe en cinq colonnes : données, outils, agents, réponses, destinataires."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_api.lineage import overview
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

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
SALES = AgentRef("agent://ventes", "assistant-commercial")
FINANCE = AgentRef("agent://finance", "analyste-tresorerie")
SQL = ToolRef("entrepot", "execute_sql")
ACCOUNTS_TOOL = ToolRef("crm", "rechercher_comptes")
RATES = DatasetRef("bigquery", "finance.ref.taux_change")
ORDERS = DatasetRef("bigquery", "ventes.suivi.commandes")
ACCOUNTS = DatasetRef("postgres://crm:5432", "crm.public.comptes")


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(engine))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield client


async def record(engine: AsyncEngine, *records: RunRecord) -> None:
    async with engine.begin() as conn:
        for item in records:
            await record_run(conn, item)


def minutes(count: int) -> datetime:
    return T0 + timedelta(minutes=count)


def fingerprint(rank: int) -> str:
    return f"sha256:{rank:064x}"


def answering(
    run_id: UUID,
    rank: int,
    *,
    agent: AgentRef | None = SALES,
    recipients: tuple[str, ...] = ("p-1",),
    channel: str = "slack",
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        kind="agent" if agent is not None else "job",
        state="COMPLETE",
        event_time=minutes(rank),
        agent=agent,
        answers=(
            Answer(
                fingerprint(rank),
                deliveries=(Delivery(channel, f"message-{rank}", recipients=recipients),),
            ),
        ),
    )


def calling(parent: UUID, tool: ToolRef, *reads: Read) -> RunRecord:
    return RunRecord(
        run_id=uuid4(),
        kind="tool",
        state="COMPLETE",
        event_time=T0,
        tool=tool,
        parent_run_id=parent,
        reads=reads,
    )


async def graph(client: httpx.AsyncClient, **params: Any) -> dict[str, Any]:
    response = await client.get("/api/v1/graph", params=params)
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


async def test_an_empty_graph_has_five_empty_columns(client: httpx.AsyncClient) -> None:
    assert await graph(client) == {
        "datasets": [],
        "tools": [],
        "agents": [],
        "answers": [],
        "recipients": [],
        "more": False,
        "next_cursor": None,
        "truncated": False,
    }


async def test_the_five_columns_and_the_chain_of_each_answer(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    first, second = uuid4(), uuid4()
    await record(
        engine,
        answering(first, 1, recipients=("p-2", "p-1")),
        calling(first, SQL, Read(RATES, "columns", ("taux",)), Read(ORDERS)),
        calling(first, SQL, Read(RATES)),
        calling(first, ACCOUNTS_TOOL, Read(ACCOUNTS)),
        answering(second, 2, agent=FINANCE, recipients=("p-3",), channel="email"),
        calling(second, SQL, Read(RATES)),
    )

    assert await graph(client) == {
        "datasets": [
            {
                "id": "dataset-0",
                "namespace": "bigquery",
                "name": "finance.ref.taux_change",
                "kind": "table",
            },
            {
                "id": "dataset-1",
                "namespace": "bigquery",
                "name": "ventes.suivi.commandes",
                "kind": "table",
            },
            {
                "id": "dataset-2",
                "namespace": "postgres://crm:5432",
                "name": "crm.public.comptes",
                "kind": "table",
            },
        ],
        "tools": [
            {"id": "tool-0", "server": "crm", "name": "rechercher_comptes"},
            {"id": "tool-1", "server": "entrepot", "name": "execute_sql"},
        ],
        "agents": [
            {"id": "agent-0", "namespace": "agent://finance", "name": "analyste-tresorerie"},
            {"id": "agent-1", "namespace": "agent://ventes", "name": "assistant-commercial"},
        ],
        # Les réponses les plus récentes d'abord.
        "answers": [
            {
                "id": answer_key(second, fingerprint(2)),
                "fingerprint": fingerprint(2),
                "produced_at": "2026-11-02T09:02:00Z",
                "agent": "agent-0",
                "channels": ["email"],
                "recipients": ["recipient-2"],
                "tools": ["tool-1"],
                "reads": [{"dataset": "dataset-0", "tool": "tool-1"}],
            },
            {
                "id": answer_key(first, fingerprint(1)),
                "fingerprint": fingerprint(1),
                "produced_at": "2026-11-02T09:01:00Z",
                "agent": "agent-1",
                "channels": ["slack"],
                "recipients": ["recipient-0", "recipient-1"],
                "tools": ["tool-0", "tool-1"],
                "reads": [
                    {"dataset": "dataset-0", "tool": "tool-1"},
                    {"dataset": "dataset-1", "tool": "tool-1"},
                    {"dataset": "dataset-2", "tool": "tool-0"},
                ],
            },
        ],
        "recipients": [
            {"id": "recipient-0", "pseudonym": "p-1"},
            {"id": "recipient-1", "pseudonym": "p-2"},
            {"id": "recipient-2", "pseudonym": "p-3"},
        ],
        "more": False,
        "next_cursor": None,
        "truncated": False,
    }


async def test_a_tool_that_read_nothing_and_a_read_made_without_tool_both_appear(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    run_id = uuid4()
    await record(
        engine,
        answering(run_id, 1, agent=None, recipients=()),
        RunRecord(run_id=run_id, kind="job", state="RUNNING", event_time=T0, reads=(Read(ORDERS),)),
        calling(run_id, ACCOUNTS_TOOL),
    )

    found = await graph(client)
    (answer,) = found["answers"]
    assert answer["agent"] is None
    assert answer["recipients"] == []
    assert answer["tools"] == ["tool-0"]
    assert answer["reads"] == [{"dataset": "dataset-0", "tool": None}]
    assert found["agents"] == []
    assert [tool["name"] for tool in found["tools"]] == ["rechercher_comptes"]


async def test_only_what_the_listed_answers_touch_is_shown(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    runs = [uuid4() for _ in range(3)]
    await record(
        engine,
        answering(runs[0], 1, recipients=("p-ancien",)),
        calling(runs[0], ACCOUNTS_TOOL, Read(ACCOUNTS)),
        answering(runs[1], 2, agent=FINANCE),
        calling(runs[1], SQL, Read(RATES)),
        answering(runs[2], 3, agent=FINANCE),
        calling(runs[2], SQL, Read(ORDERS)),
        # Une lecture sans réponse n'entre pas dans la vue.
        calling(uuid4(), SQL, Read(DatasetRef("bigquery", "rh.pilotage.effectifs"))),
    )

    recent = await graph(client, limit=2)
    assert [answer["fingerprint"] for answer in recent["answers"]] == [
        fingerprint(3),
        fingerprint(2),
    ]
    assert recent["more"] is True
    assert [dataset["name"] for dataset in recent["datasets"]] == [
        "finance.ref.taux_change",
        "ventes.suivi.commandes",
    ]
    assert [agent["name"] for agent in recent["agents"]] == ["analyste-tresorerie"]
    assert [recipient["pseudonym"] for recipient in recent["recipients"]] == ["p-1"]

    older = await graph(client, limit=2, cursor=recent["next_cursor"])
    assert [answer["fingerprint"] for answer in older["answers"]] == [fingerprint(1)]
    assert older["more"] is False
    assert [recipient["pseudonym"] for recipient in older["recipients"]] == ["p-ancien"]

    everything = await graph(client, limit=200)
    assert len(everything["answers"]) == 3
    assert everything["more"] is False


@pytest.mark.parametrize(
    ("params", "path"),
    [
        ({"limit": "0"}, "/query/limit"),
        ({"limit": "201"}, "/query/limit"),
        ({"limit": "beaucoup"}, "/query/limit"),
        ({"cursor": "hier"}, "/query/cursor"),
        ({"cursor": "2026-11-02T09:00:00Z"}, "/query/cursor"),
        ({"cursor": f"{'1' * 19}.{'0' * 48}"}, "/query/cursor"),
        ({"cursor": f"9{'9' * 17}.{'0' * 48}"}, "/query/cursor"),
    ],
)
async def test_the_view_is_bounded(
    client: httpx.AsyncClient, params: dict[str, str], path: str
) -> None:
    response = await client.get("/api/v1/graph", params=params)
    assert response.status_code == 422
    assert [error["path"] for error in response.json()["errors"]] == [path]
    assert "hier" not in response.text
    assert "beaucoup" not in response.text


async def test_run_trees_beyond_the_bound_are_reported_as_truncated(
    client: httpx.AsyncClient, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = uuid4()
    await record(
        engine,
        answering(run_id, 1),
        *(calling(run_id, SQL, Read(ORDERS)) for _ in range(4)),
    )
    monkeypatch.setattr(overview, "MAX_RUNS", 5)
    assert (await graph(client))["truncated"] is False
    monkeypatch.setattr(overview, "MAX_RUNS", 3)
    assert (await graph(client))["truncated"] is True


async def test_reads_beyond_the_bound_are_reported_as_truncated(
    client: httpx.AsyncClient, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = uuid4()
    await record(
        engine,
        answering(run_id, 1),
        calling(run_id, SQL, Read(RATES), Read(ORDERS)),
        calling(run_id, ACCOUNTS_TOOL, Read(ACCOUNTS)),
    )
    monkeypatch.setattr(overview, "MAX_READS", 3)
    assert (await graph(client))["truncated"] is False
    monkeypatch.setattr(overview, "MAX_READS", 2)
    bounded = await graph(client)
    assert bounded["truncated"] is True
    assert len(bounded["answers"][0]["reads"]) == 2


async def test_recipients_beyond_the_bound_are_reported_as_truncated(
    client: httpx.AsyncClient, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    await record(engine, answering(uuid4(), 1, recipients=("p-1", "p-2", "p-3")))
    monkeypatch.setattr(overview, "MAX_LINKS", 3)
    assert (await graph(client))["truncated"] is False
    monkeypatch.setattr(overview, "MAX_LINKS", 2)
    bounded = await graph(client)
    assert bounded["truncated"] is True
    assert len(bounded["recipients"]) == 2


async def test_several_answers_of_one_run_share_its_chain(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    run_id = uuid4()
    again = answering(run_id, 1)
    other = RunRecord(
        run_id=run_id,
        kind="agent",
        state="COMPLETE",
        event_time=minutes(1),
        agent=SALES,
        answers=(Answer(fingerprint(9)),),
    )
    await record(engine, again, other, calling(run_id, SQL, Read(RATES)))

    found = await graph(client)
    assert len(found["answers"]) == 2
    assert {answer["fingerprint"] for answer in found["answers"]} == {
        fingerprint(1),
        fingerprint(9),
    }
    for answer in found["answers"]:
        assert answer["reads"] == [{"dataset": "dataset-0", "tool": "tool-0"}]
    assert sorted(len(answer["recipients"]) for answer in found["answers"]) == [0, 1]


async def test_the_chain_of_one_answer_can_be_asked_for_by_its_identifier(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    old, recent = uuid4(), uuid4()
    await record(
        engine,
        answering(old, 1, recipients=("p-ancien",)),
        calling(old, ACCOUNTS_TOOL, Read(ACCOUNTS)),
        answering(recent, 2, agent=FINANCE),
        calling(recent, SQL, Read(RATES)),
    )

    found = await graph(client, answer=answer_key(old, fingerprint(1)))

    assert [answer["fingerprint"] for answer in found["answers"]] == [fingerprint(1)]
    assert [dataset["name"] for dataset in found["datasets"]] == ["crm.public.comptes"]
    assert [recipient["pseudonym"] for recipient in found["recipients"]] == ["p-ancien"]
    assert [agent["name"] for agent in found["agents"]] == ["assistant-commercial"]
    assert (found["more"], found["next_cursor"], found["truncated"]) == (False, None, False)


async def test_the_chain_of_an_unknown_answer_is_not_found(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await record(engine, answering(uuid4(), 1))
    response = await client.get(
        "/api/v1/graph", params={"answer": answer_key(uuid4(), fingerprint(1))}
    )
    assert (response.status_code, response.json()) == (404, {"error": "unknown_answer"})
    malformed = await client.get("/api/v1/graph", params={"answer": "pas-un-identifiant"})
    assert malformed.status_code == 422
    assert [error["path"] for error in malformed.json()["errors"]] == ["/query/answer"]
