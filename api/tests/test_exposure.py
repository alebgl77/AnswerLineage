"""Le score d'exposition : les jeux de données et leurs colonnes, les plus exposés d'abord."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_api.lineage import exposure
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
AT = T0.isoformat()
SALES = AgentRef("agent://ventes", "assistant-commercial")
SQL = ToolRef("entrepot", "execute_sql")
RATES = DatasetRef("bigquery", "finance.ref.taux_change")
ORDERS = DatasetRef("bigquery", "ventes.suivi.commandes")
ACCOUNTS = DatasetRef("postgres://crm:5432", "crm.public.comptes")


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


def answering(
    run_id: UUID,
    produced_at: datetime,
    *reads: Read,
    recipients: tuple[str, ...] = ("p-1",),
    fingerprint: str | None = None,
) -> list[RunRecord]:
    """Un run d'agent qui répond, et l'appel d'outil qui a fait ses lectures."""
    answered = RunRecord(
        run_id=run_id,
        kind="agent",
        state="COMPLETE",
        event_time=produced_at,
        agent=SALES,
        answers=(
            Answer(
                fingerprint or f"sha256:{run_id.int:064x}",
                deliveries=(Delivery("slack", f"m-{run_id.hex}", recipients=recipients),),
            ),
        ),
    )
    return [answered, calling(run_id, *reads)] if reads else [answered]


def calling(parent: UUID, *reads: Read) -> RunRecord:
    return RunRecord(
        run_id=uuid4(),
        kind="tool",
        state="COMPLETE",
        event_time=T0,
        tool=SQL,
        parent_run_id=parent,
        reads=reads,
    )


async def ranking(client: httpx.AsyncClient, **params: Any) -> dict[str, Any]:
    response = await client.get("/api/v1/exposure", params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def scores(found: dict[str, Any]) -> list[tuple[str, float]]:
    return [(dataset["name"], dataset["score"]) for dataset in found["datasets"]]


async def test_an_empty_graph_exposes_nothing(client: httpx.AsyncClient) -> None:
    assert await ranking(client, at=AT) == {
        "at": "2026-11-02T09:00:00Z",
        "datasets": [],
        "more": False,
        "next_cursor": None,
        "truncated": False,
    }


async def test_each_answer_weighs_one_plus_its_distinct_recipients(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    first, second = uuid4(), uuid4()
    await record(
        engine,
        *answering(first, T0, Read(RATES), Read(ORDERS), recipients=("p-1", "p-2")),
        # Le même destinataire par deux canaux ne compte qu'une fois.
        RunRecord(
            run_id=first,
            kind="agent",
            state="COMPLETE",
            event_time=T0,
            agent=SALES,
            answers=(
                Answer(
                    f"sha256:{first.int:064x}",
                    deliveries=(Delivery("email", "m-courriel", recipients=("p-2",)),),
                ),
            ),
        ),
        *answering(second, T0, Read(RATES), recipients=()),
        # Un run qui a lu sans répondre n'expose rien.
        calling(uuid4(), Read(ACCOUNTS)),
    )

    found = await ranking(client, at=AT)
    assert found["datasets"] == [
        {
            "namespace": "bigquery",
            "name": "finance.ref.taux_change",
            "kind": "table",
            "score": 4.0,
            "answers": 2,
            "recipients": 2,
            "columns": [],
            "more_columns": False,
        },
        {
            "namespace": "bigquery",
            "name": "ventes.suivi.commandes",
            "kind": "table",
            "score": 3.0,
            "answers": 1,
            "recipients": 2,
            "columns": [],
            "more_columns": False,
        },
    ]
    assert (found["more"], found["next_cursor"], found["truncated"]) == (False, None, False)


async def test_an_answer_older_than_thirty_days_weighs_half_and_sixty_days_a_quarter(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await record(engine, *answering(uuid4(), T0, Read(RATES), recipients=("p-1", "p-2", "p-3")))

    assert scores(await ranking(client, at=AT)) == [("finance.ref.taux_change", 4.0)]
    later = T0 + timedelta(days=30)
    assert scores(await ranking(client, at=later.isoformat())) == [("finance.ref.taux_change", 2.0)]
    assert scores(await ranking(client, at=(T0 + timedelta(days=60)).isoformat())) == [
        ("finance.ref.taux_change", 1.0)
    ]
    # L'heure se lit à son instant, quel que soit son fuseau.
    shifted = await ranking(client, at=later.astimezone(timezone(timedelta(hours=2))).isoformat())
    assert scores(shifted) == [("finance.ref.taux_change", 2.0)]
    assert shifted["at"] == "2026-12-02T09:00:00Z"


async def test_a_recent_answer_outweighs_older_ones_and_a_future_one_weighs_as_now(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await record(
        engine,
        *answering(uuid4(), T0 - timedelta(days=90), Read(ORDERS)),
        *answering(uuid4(), T0 - timedelta(days=90), Read(ORDERS)),
        *answering(uuid4(), T0 - timedelta(days=90), Read(ORDERS)),
        *answering(uuid4(), T0 - timedelta(days=1), Read(RATES)),
        *answering(uuid4(), T0 + timedelta(days=5), Read(ACCOUNTS), recipients=()),
    )

    found = await ranking(client, at=AT)
    # Trois réponses de 90 jours : 3 x 2 / 8 ; une réponse d'hier : 2 x 2^(-1/30).
    assert scores(found) == [
        ("finance.ref.taux_change", 1.95432),
        ("crm.public.comptes", 1.0),
        ("ventes.suivi.commandes", 0.75),
    ]


async def test_the_reads_of_sub_agents_count_and_those_of_the_parent_do_not(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    parent, child = uuid4(), uuid4()
    await record(
        engine,
        *answering(parent, T0, Read(RATES), recipients=("p-1",)),
        RunRecord(
            run_id=child,
            kind="agent",
            state="COMPLETE",
            event_time=T0,
            agent=SALES,
            parent_run_id=parent,
            answers=(Answer("sha256:" + "e" * 64),),
        ),
        calling(child, Read(ORDERS)),
    )

    found = await ranking(client, at=AT)
    # La réponse du parent dépend des deux tables ; celle du sous-agent, de la sienne seule.
    assert [(item["name"], item["score"], item["answers"]) for item in found["datasets"]] == [
        ("ventes.suivi.commandes", 3.0, 2),
        ("finance.ref.taux_change", 2.0, 1),
    ]


async def test_datasets_of_equal_score_follow_their_namespace_then_their_name(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    tied = [
        DatasetRef("postgres://crm:5432", "crm.public.comptes"),
        DatasetRef("bigquery", "ventes.suivi.commandes"),
        DatasetRef("bigquery", "finance.ref.taux_change"),
        DatasetRef("bigquery", "finance.compta.factures"),
    ]
    await record(engine, *answering(uuid4(), T0, *(Read(dataset) for dataset in tied)))

    found = await ranking(client, at=AT)
    assert [(item["namespace"], item["name"]) for item in found["datasets"]] == [
        ("bigquery", "finance.compta.factures"),
        ("bigquery", "finance.ref.taux_change"),
        ("bigquery", "ventes.suivi.commandes"),
        ("postgres://crm:5432", "crm.public.comptes"),
    ]
    assert {item["score"] for item in found["datasets"]} == {2.0}


async def test_a_column_counts_its_readers_and_those_who_read_the_whole_table(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await record(
        engine,
        *answering(uuid4(), T0, Read(RATES, "columns", ("devise", "taux")), recipients=("p-1",)),
        *answering(uuid4(), T0, Read(RATES, "columns", ("taux",)), recipients=("p-2",)),
        # Une seule réponse lit la date de valeur, mais elle a quatre destinataires.
        *answering(
            uuid4(),
            T0,
            Read(RATES, "columns", ("date_valeur",)),
            recipients=("p-3", "p-4", "p-5", "p-6"),
        ),
        *answering(uuid4(), T0, Read(RATES), recipients=()),
    )

    (rates,) = (await ranking(client, at=AT))["datasets"]
    assert (rates["score"], rates["answers"], rates["recipients"]) == (10.0, 4, 6)
    assert rates["columns"] == [
        {"name": "date_valeur", "score": 6.0, "answers": 2},
        {"name": "taux", "score": 5.0, "answers": 3},
        {"name": "devise", "score": 3.0, "answers": 2},
    ]
    assert rates["more_columns"] is False


async def test_a_reader_that_names_a_column_and_reads_the_whole_table_counts_once(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    run_id = uuid4()
    await record(
        engine,
        *answering(run_id, T0, Read(RATES, "columns", ("taux",))),
        calling(run_id, Read(RATES)),
        *answering(uuid4(), T0, Read(RATES, "columns", ("devise",)), recipients=()),
    )

    (rates,) = (await ranking(client, at=AT))["datasets"]
    assert rates["score"] == 3.0
    assert rates["columns"] == [
        {"name": "devise", "score": 3.0, "answers": 2},
        {"name": "taux", "score": 2.0, "answers": 1},
    ]


async def test_columns_beyond_their_bound_are_said_to_follow(
    client: httpx.AsyncClient, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    await record(
        engine,
        *answering(uuid4(), T0, Read(RATES, "columns", ("taux", "devise"))),
        *answering(uuid4(), T0, Read(RATES, "columns", ("date_valeur",)), recipients=()),
    )
    monkeypatch.setattr(exposure, "MAX_COLUMNS", 2)
    (rates,) = (await ranking(client, at=AT))["datasets"]
    assert [column["name"] for column in rates["columns"]] == ["devise", "taux"]
    assert rates["more_columns"] is True


async def test_pages_follow_the_ranking_once_and_keep_the_reference_time(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    datasets = [DatasetRef("bigquery", f"ventes.table_{rank:02d}") for rank in range(7)]
    for rank, dataset in enumerate(datasets):
        # Des scores qui se répètent : l'ordre se départage par le nom.
        await record(engine, *answering(uuid4(), T0 - timedelta(days=rank // 2), Read(dataset)))
    everything = [item["name"] for item in (await ranking(client, at=AT, limit=200))["datasets"]]
    assert len(everything) == 7

    walked: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 3} | ({} if cursor is None else {"cursor": cursor})
        if cursor is None:
            params["at"] = AT
        page = await ranking(client, **params)
        assert page["at"] == "2026-11-02T09:00:00Z"
        walked.extend(item["name"] for item in page["datasets"])
        cursor = page["next_cursor"]
        assert page["more"] is (cursor is not None)
        if cursor is None:
            break
    assert walked == everything

    first = await ranking(client, at=AT, limit=3)
    # Le curseur garde l'heure de référence : la redonner est permis, en donner une autre non.
    same = await ranking(client, at=AT, limit=3, cursor=first["next_cursor"])
    assert [item["name"] for item in same["datasets"]] == everything[3:6]
    other = await client.get(
        "/api/v1/exposure",
        params={"at": "2026-12-01T00:00:00Z", "cursor": first["next_cursor"]},
    )
    assert other.status_code == 422
    assert [error["path"] for error in other.json()["errors"]] == ["/query/cursor"]


async def test_a_cursor_on_an_unknown_dataset_designates_no_page(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await record(engine, *answering(uuid4(), T0, Read(RATES)))
    cursor = exposure.ExposureCursor(T0, 2_000_000, 987_654_321).encode()
    response = await client.get("/api/v1/exposure", params={"cursor": cursor})
    assert response.status_code == 422
    (error,) = response.json()["errors"]
    assert (error["path"], error["type"]) == ("/query/cursor", "cursor_invalid")
    assert "987654321" not in response.text


@pytest.mark.parametrize(
    ("params", "path"),
    [
        ({"limit": "0"}, "/query/limit"),
        ({"limit": "201"}, "/query/limit"),
        ({"limit": "beaucoup"}, "/query/limit"),
        ({"cursor": "hier"}, "/query/cursor"),
        ({"cursor": "1.2"}, "/query/cursor"),
        ({"cursor": f"{'9' * 18}.1.1"}, "/query/cursor"),
        ({"at": "hier<script>"}, "/query/at"),
        ({"at": "2026-11-02T09:00:00"}, "/query/at"),
        ({"at": "1969-12-31T23:59:59Z"}, "/query/at"),
        ({"at": "10000-01-01T00:00:00Z"}, "/query/at"),
    ],
)
async def test_the_ranking_is_bounded_and_refuses_without_echoing(
    client: httpx.AsyncClient, params: dict[str, str], path: str
) -> None:
    response = await client.get("/api/v1/exposure", params=params)
    assert response.status_code == 422
    assert [error["path"] for error in response.json()["errors"]] == [path]
    for value in params.values():
        if len(value) > 4:
            assert value not in response.text


async def test_a_reference_time_out_of_range_names_its_reason(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/exposure", params={"at": "1969-12-31T23:59:59Z"})
    (error,) = response.json()["errors"]
    assert error["type"] == "at_out_of_range"


async def test_without_reference_time_the_score_is_taken_at_the_time_of_the_base(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    async with engine.connect() as conn:
        before = (await conn.execute(text("SELECT now()"))).scalar_one()
    await record(engine, *answering(uuid4(), before + timedelta(days=1), Read(RATES)))

    found = await ranking(client)
    async with engine.connect() as conn:
        after = (await conn.execute(text("SELECT now()"))).scalar_one()
    assert before <= datetime.fromisoformat(found["at"]) <= after
    # Une réponse postérieure à l'heure de référence pèse comme une réponse de cet instant.
    assert scores(found) == [("finance.ref.taux_change", 2.0)]
    assert "known_at" not in found


async def test_beyond_the_bound_on_runs_the_most_recent_answers_keep_their_reads(
    client: httpx.AsyncClient, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # La réponse la plus récente arrive la dernière.
    await record(
        engine,
        *answering(uuid4(), T0 - timedelta(days=2), Read(ACCOUNTS)),
        *answering(uuid4(), T0 - timedelta(days=1), Read(ORDERS)),
        *answering(uuid4(), T0, Read(RATES)),
    )

    # Les trois runs des réponses, puis un appel : celui de la plus récente.
    monkeypatch.setattr(exposure, "MAX_RUNS", 4)
    bounded = await ranking(client, at=AT)
    assert bounded["truncated"] is True
    assert scores(bounded) == [("finance.ref.taux_change", 2.0)]


@pytest.mark.parametrize(
    ("bound", "allowed"),
    [
        ("MAX_ANSWERS", 3),
        ("MAX_LINKS", 4),
        ("MAX_RUNS", 6),
        ("MAX_READS", 4),
        ("MAX_COLUMN_READS", 2),
    ],
)
async def test_a_reading_beyond_its_bound_is_reported_and_its_scores_are_minimums(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    bound: str,
    allowed: int,
) -> None:
    await record(
        engine,
        *answering(uuid4(), T0, Read(RATES, "columns", ("taux",)), Read(ORDERS)),
        *answering(uuid4(), T0 - timedelta(days=1), Read(RATES, "columns", ("devise",))),
        *answering(uuid4(), T0 - timedelta(days=2), Read(ACCOUNTS), recipients=("p-1", "p-2")),
    )
    complete = await ranking(client, at=AT)

    monkeypatch.setattr(exposure, bound, allowed)
    assert (await ranking(client, at=AT))["truncated"] is False
    monkeypatch.setattr(exposure, bound, allowed - 1)
    bounded = await ranking(client, at=AT)
    assert bounded["truncated"] is True
    full = {item["name"]: item for item in complete["datasets"]}
    for item in bounded["datasets"]:
        assert item["score"] <= full[item["name"]]["score"]
        for column in item["columns"]:
            # Une colonne ne pèse jamais plus que son jeu de données.
            assert column["score"] <= item["score"]
