"""Lire les incidents proposés par des tests de qualité en échec.

Les propositions sont écrites ici comme les workers les écrivent ; leur
naissance est éprouvée avec les workers (``workers/tests/test_proposals.py``).
Une lecture est bornée, ne recopie jamais une valeur reçue, et dit la fin de
l'échec quand toutes les colonnes en échec réussissent de nouveau.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_api.store import schema

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

# Il y a deux jours, à l'heure ronde : la fenêtre déduite tient dans celle d'un incident.
T0 = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(days=2)
WAREHOUSE = "postgres://entrepot:5432"
CUSTOMERS = "analytique.public.clients"
UNKNOWN = "0193a9c5-7a10-4c3e-9d5e-2f1b6c8a0000"
CANARY = "canari5e3a"


def hours(count: float) -> datetime:
    return T0 + timedelta(hours=count)


def iso(at: datetime) -> str:
    """Une heure telle que l'API l'écrit."""
    return at.isoformat().replace("+00:00", "Z")


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(engine))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield client


async def propose(
    engine: AsyncEngine,
    *columns: str,
    name: str = CUSTOMERS,
    whole_table: bool = False,
    failed: tuple[float, float] = (3, 5),
    runs: int = 1,
) -> UUID:
    """Une proposition signalée, telle qu'un worker l'écrit, ses colonnes et ses signaux."""
    async with engine.begin() as conn:
        proposal: UUID = (
            await conn.execute(
                text(
                    """
                    INSERT INTO incident_proposal (
                        namespace, name, whole_table, classes, severity, first_failed_at,
                        last_failed_at, window_start, window_basis, emitter
                    )
                    VALUES (:namespace, :name, :whole, ARRAY['not_null', 'unique'], 'warn',
                            :first, :last, :start, 'last_pass', 'dbt')
                    RETURNING id
                    """
                ),
                {
                    "namespace": WAREHOUSE,
                    "name": name,
                    "whole": whole_table,
                    "first": hours(failed[0]),
                    "last": hours(failed[1]),
                    "start": hours(1),
                },
            )
        ).scalar_one()
        for column in columns:
            await conn.execute(
                text(
                    "INSERT INTO incident_proposal_column (proposal_id, column_name)"
                    " VALUES (:id, :column)"
                ),
                {"id": proposal, "column": column},
            )
        for _ in range(runs):
            await conn.execute(
                text(
                    """
                    INSERT INTO quality_signal (
                        run_id, namespace, name, column_name, classes, severity, emitter,
                        event_time, recorded_at, state, proposal_id
                    )
                    VALUES (gen_random_uuid(), :namespace, :name, :column, ARRAY['not_null'],
                            'warn', 'dbt', :at, now(), 'attached', :id)
                    """
                ),
                {
                    "namespace": WAREHOUSE,
                    "name": name,
                    "column": columns[0] if columns else "",
                    "at": hours(failed[0]),
                    "id": proposal,
                },
            )
    return proposal


async def tracked(
    engine: AsyncEngine, column: str, passed: float | None, failed: float | None
) -> None:
    """La dernière réussite et le dernier échec connus d'une colonne."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO quality_column (namespace, name, column_name, last_passed_at,
                                            last_failed_at)
                VALUES (:namespace, :name, :column, :passed, :failed)
                """
            ),
            {
                "namespace": WAREHOUSE,
                "name": CUSTOMERS,
                "column": column,
                "passed": None if passed is None else hours(passed),
                "failed": None if failed is None else hours(failed),
            },
        )


async def test_no_proposal_is_an_empty_list(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/incident-proposals")
    assert (response.status_code, response.json()) == (
        200,
        {"proposals": [], "more": False, "next_cursor": None},
    )


async def test_a_proposal_reads_with_its_dataset_columns_classes_and_window(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    proposal = await propose(engine, "pays", "email", runs=3)
    response = await client.get(f"/api/v1/incident-proposals/{proposal}")
    assert response.status_code == 200
    found = response.json()
    proposed_at = found.pop("proposed_at")
    assert datetime.fromisoformat(proposed_at) > T0
    assert found == {
        "id": str(proposal),
        "state": "signaled",
        "dataset": {"namespace": WAREHOUSE, "name": CUSTOMERS},
        "whole_table": False,
        "columns": ["email", "pays"],
        "assertions": ["not_null", "unique"],
        "severity": "warn",
        "emitter": "dbt",
        "first_failed_at": iso(hours(3)),
        "last_failed_at": iso(hours(5)),
        "resolved_at": None,
        "window": {"from": iso(hours(1)), "basis": "last_pass"},
        "decided_at": None,
        "incident_id": None,
        "counts": {"failed_runs": 3, "lower_bound": False},
    }


async def test_the_failure_ends_when_every_failing_column_passes_again(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    proposal = await propose(engine, "email", "pays")

    async def resolved() -> Any:
        return (await client.get(f"/api/v1/incident-proposals/{proposal}")).json()["resolved_at"]

    await tracked(engine, "email", 6, 5)
    assert await resolved() is None, "une colonne en échec n'a pas de réussite connue"
    await tracked(engine, "pays", 4, 5)
    assert await resolved() is None, "la dernière réussite d'une colonne précède son échec"
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE quality_column SET last_passed_at = :at WHERE column_name = 'pays'"),
            {"at": hours(8)},
        )
    # La plus récente des réussites : la fenêtre s'allonge plutôt que de se raccourcir.
    assert await resolved() == iso(hours(8))


async def test_a_whole_table_proposal_ends_when_the_table_passes_again(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    proposal = await propose(engine, whole_table=True)
    await tracked(engine, "", 7, 5)
    found = (await client.get(f"/api/v1/incident-proposals/{proposal}")).json()
    assert (found["columns"], found["resolved_at"]) == ([], iso(hours(7)))


async def test_a_proposal_whose_columns_overflowed_never_says_its_end(
    client: httpx.AsyncClient, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from answerlineage_api.incidents import proposals

    monkeypatch.setattr(proposals, "MAX_PROPOSAL_COLUMNS", 1)
    proposal = await propose(engine, "email", whole_table=True)
    await tracked(engine, "email", 7, 5)
    await tracked(engine, "", 7, 5)
    found = (await client.get(f"/api/v1/incident-proposals/{proposal}")).json()
    assert found["resolved_at"] is None


async def test_the_list_is_paged_most_recent_first_and_filtered_by_state(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    made = [await propose(engine, "email", name=f"analytique.public.t{rank}") for rank in range(5)]
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE incident_proposal SET state = 'dismissed',"
                " decided_at = GREATEST(now(), proposed_at) WHERE id = :id"
            ),
            {"id": made[1]},
        )
    seen: list[str] = []
    cursor: str | None = None
    while True:
        params = {"limit": "2"} | ({} if cursor is None else {"cursor": cursor})
        page = (await client.get("/api/v1/incident-proposals", params=params)).json()
        seen += [item["id"] for item in page["proposals"]]
        cursor = page["next_cursor"]
        if not page["more"]:
            break
    assert seen == [str(proposal) for proposal in reversed(made)]

    dismissed = (
        await client.get("/api/v1/incident-proposals", params={"state": "dismissed"})
    ).json()
    assert [item["id"] for item in dismissed["proposals"]] == [str(made[1])]


@pytest.mark.parametrize(
    ("path", "params", "status", "code"),
    [
        (f"/api/v1/incident-proposals/{UNKNOWN}", {}, 404, "unknown_proposal"),
        (f"/api/v1/incident-proposals/{CANARY}-pas-un-uuid", {}, 422, "string_pattern_mismatch"),
        ("/api/v1/incident-proposals", {"state": CANARY}, 422, "literal_error"),
        ("/api/v1/incident-proposals", {"cursor": f"<{CANARY}>"}, 422, "string_pattern_mismatch"),
        (
            "/api/v1/incident-proposals",
            {"cursor": "999999999999999999." + "0" * 32},
            422,
            "cursor_invalid",
        ),
        ("/api/v1/incident-proposals", {"limit": "101"}, 422, "less_than_equal"),
    ],
)
async def test_a_faulty_request_is_refused_without_its_value(
    client: httpx.AsyncClient, path: str, params: dict[str, str], status: int, code: str
) -> None:
    response = await client.get(path, params=params)
    assert response.status_code == status
    body = response.json()
    codes = [body["error"]] if "error" in body else [error["type"] for error in body["errors"]]
    assert code in codes
    assert CANARY not in response.text


# La base tient les états et les transitions.


def constraint(error: IntegrityError) -> str | None:
    """La contrainte que la base nomme dans son refus."""
    name: str | None = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
    return name


async def test_the_base_refuses_any_state_or_transition_outside_the_closed_list(
    engine: AsyncEngine,
) -> None:
    proposal = await propose(engine, "email")
    refusals = {
        "ck_incident_proposal_state": (
            "UPDATE incident_proposal SET state = 'paused', decided_at = now()"
        ),
        "ck_incident_proposal_decided": "UPDATE incident_proposal SET state = 'dismissed'",
        "ck_incident_proposal_confirmed": (
            "UPDATE incident_proposal SET state = 'confirmed', decided_at = now()"
        ),
        "ck_incident_proposal_classes": "UPDATE incident_proposal SET classes = ARRAY['<b>']",
        "ck_incident_proposal_window": (
            "UPDATE incident_proposal SET window_start = first_failed_at"
        ),
        "uq_incident_proposal_open": (
            "INSERT INTO incident_proposal (namespace, name, whole_table, classes, severity,"
            " first_failed_at, last_failed_at, window_start, window_basis, emitter)"
            " SELECT namespace, name, false, classes, severity, first_failed_at,"
            " last_failed_at, window_start, window_basis, emitter FROM incident_proposal"
        ),
        "ck_quality_signal_proposal": "UPDATE quality_signal SET state = 'pending'",
        "ck_incident_proposal_column_named": (
            "INSERT INTO incident_proposal_column (proposal_id, column_name) VALUES (:id, '')"
        ),
        # Ni le jeu, ni le premier échec, ni le début de la fenêtre ne changent.
        "ck_incident_proposal_transition": (
            "UPDATE incident_proposal SET first_failed_at = first_failed_at - interval '1 s'"
        ),
    }
    for name, statement in refusals.items():
        with pytest.raises(IntegrityError) as refused:
            async with engine.begin() as conn:
                await conn.execute(text(statement), {"id": proposal})
        assert constraint(refused.value) == name, statement

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE incident_proposal SET state = 'expired',"
                " decided_at = GREATEST(now(), proposed_at)"
            )
        )
    # Une décision est définitive.
    for statement in (
        "UPDATE incident_proposal SET state = 'signaled', decided_at = NULL",
        "UPDATE incident_proposal SET state = 'dismissed'",
        "UPDATE incident_proposal SET last_failed_at = last_failed_at",
    ):
        with pytest.raises(IntegrityError) as refused:
            async with engine.begin() as conn:
                await conn.execute(text(statement))
        assert constraint(refused.value) == "ck_incident_proposal_transition", statement


def test_the_migration_holds_the_transitions_as_the_declared_schema_does(
    empty_database: URL, database: URL
) -> None:
    declared_engine = create_engine(empty_database)
    try:
        schema.metadata.create_all(declared_engine)
    finally:
        declared_engine.dispose()

    def transitions(url: URL) -> list[Any]:
        engine = create_engine(url)
        try:
            with engine.connect() as connection:
                return [
                    tuple(row)
                    for row in connection.execute(
                        text(
                            "SELECT proname, prosrc FROM pg_proc"
                            " WHERE proname = 'incident_proposal_transition'"
                        )
                    )
                ]
        finally:
            engine.dispose()

    migrated = transitions(database)
    assert len(migrated) == 1
    assert migrated == transitions(empty_database)
