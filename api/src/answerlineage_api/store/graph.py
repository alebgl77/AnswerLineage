"""Écriture du graphe de lignage.

``record_run`` enregistre ce qu'un événement apprend d'un run : son agent ou
son outil, ses lectures, ses écritures, ses réponses et leur diffusion. Les
écritures sont idempotentes et commutatives : rejouer un enregistrement, ou
enregistrer deux événements du même run dans un ordre quelconque, donne le
même graphe.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Table, and_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from answerlineage_api.store import schema

RunKind = Literal["agent", "tool", "job"]
RunState = Literal["START", "RUNNING", "COMPLETE", "ABORT", "FAIL", "OTHER"]
DatasetKind = Literal["table", "document", "api", "memory", "answer"]
Precision = Literal["table", "columns", "subset", "keys"]
Origin = Literal["analysis", "reconciliation"]


@dataclass(frozen=True)
class AgentRef:
    namespace: str
    name: str


@dataclass(frozen=True)
class ToolRef:
    server: str
    name: str


@dataclass(frozen=True)
class DatasetRef:
    namespace: str
    name: str
    kind: DatasetKind = "table"
    version: str | None = None


@dataclass(frozen=True)
class Read:
    dataset: DatasetRef
    precision: Precision = "table"
    columns: tuple[str, ...] = ()
    origin: Origin = "analysis"


@dataclass(frozen=True)
class Write:
    dataset: DatasetRef


@dataclass(frozen=True)
class Delivery:
    channel: str
    message_ref: str
    # Pseudonymes des destinataires, jamais leur identité.
    recipients: tuple[str, ...] = ()


@dataclass(frozen=True)
class Answer:
    # Empreinte de la réponse : le contenu n'est jamais stocké.
    fingerprint: str
    deliveries: tuple[Delivery, ...] = ()


@dataclass(frozen=True)
class RunRecord:
    """Ce qu'un événement apprend d'un run."""

    run_id: UUID
    kind: RunKind
    state: RunState
    event_time: datetime
    agent: AgentRef | None = None
    tool: ToolRef | None = None
    parent_run_id: UUID | None = None
    trace_id: str | None = None
    span_id: str | None = None
    reads: tuple[Read, ...] = ()
    writes: tuple[Write, ...] = ()
    answers: tuple[Answer, ...] = ()


# Le dernier événement fixe l'état du run. À heure égale, un état terminal
# l'emporte, pour que le résultat ne dépende pas de l'ordre d'arrivée. Un run
# « job » se précise en outil ou en agent, jamais l'inverse.
_UPSERT_RUN = text(
    """
    INSERT INTO run (run_id, kind, agent_id, tool_id, parent_run_id, trace_id, span_id,
                     started_at, ended_at, state, state_time)
    VALUES (:run_id, :kind, :agent_id, :tool_id, :parent_run_id, :trace_id, :span_id,
            :started_at, :ended_at, :state, :state_time)
    ON CONFLICT (run_id) DO UPDATE SET
        kind = CASE
            WHEN array_position(ARRAY['job', 'tool', 'agent'], EXCLUDED.kind)
               > array_position(ARRAY['job', 'tool', 'agent'], run.kind)
            THEN EXCLUDED.kind ELSE run.kind END,
        agent_id = COALESCE(run.agent_id, EXCLUDED.agent_id),
        tool_id = COALESCE(run.tool_id, EXCLUDED.tool_id),
        parent_run_id = COALESCE(run.parent_run_id, EXCLUDED.parent_run_id),
        trace_id = COALESCE(run.trace_id, EXCLUDED.trace_id),
        span_id = COALESCE(run.span_id, EXCLUDED.span_id),
        started_at = LEAST(run.started_at, EXCLUDED.started_at),
        ended_at = GREATEST(run.ended_at, EXCLUDED.ended_at),
        state = CASE
            WHEN (EXCLUDED.state_time,
                  array_position(ARRAY['OTHER', 'START', 'RUNNING', 'ABORT', 'FAIL', 'COMPLETE'],
                                 EXCLUDED.state))
               > (run.state_time,
                  array_position(ARRAY['OTHER', 'START', 'RUNNING', 'ABORT', 'FAIL', 'COMPLETE'],
                                 run.state))
            THEN EXCLUDED.state ELSE run.state END,
        state_time = GREATEST(run.state_time, EXCLUDED.state_time)
    """
)

# Une lecture par run, jeu de données et version. Deux événements se fusionnent :
# la précision la plus fine l'emporte, les colonnes s'additionnent, la
# réconciliation avec l'entrepôt prime sur l'analyse.
_UPSERT_READ = text(
    f"""
    INSERT INTO read_edge (run_id, dataset_id, dataset_version_id, precision, columns, origin,
                           event_time)
    VALUES (:run_id, :dataset_id, :dataset_version_id, :precision,
            CAST(:columns AS text[]), :origin, :event_time)
    ON CONFLICT ON CONSTRAINT {schema.READ_EDGE_KEY} DO UPDATE SET
        precision = CASE
            WHEN array_position(ARRAY['table', 'columns', 'subset', 'keys'], EXCLUDED.precision)
               > array_position(ARRAY['table', 'columns', 'subset', 'keys'], read_edge.precision)
            THEN EXCLUDED.precision ELSE read_edge.precision END,
        columns = (
            SELECT COALESCE(array_agg(DISTINCT c ORDER BY c), CAST('{{}}' AS text[]))
            FROM unnest(read_edge.columns || EXCLUDED.columns) AS c
        ),
        origin = CASE
            WHEN EXCLUDED.origin = 'reconciliation' THEN EXCLUDED.origin
            ELSE read_edge.origin END,
        event_time = LEAST(read_edge.event_time, EXCLUDED.event_time)
    """  # noqa: S608 — seul un nom de contrainte constant est interpolé.
)

_UPSERT_WRITE = text(
    f"""
    INSERT INTO write_edge (run_id, dataset_id, dataset_version_id, event_time)
    VALUES (:run_id, :dataset_id, :dataset_version_id, :event_time)
    ON CONFLICT ON CONSTRAINT {schema.WRITE_EDGE_KEY} DO UPDATE SET
        event_time = LEAST(write_edge.event_time, EXCLUDED.event_time)
    """  # noqa: S608 — seul un nom de contrainte constant est interpolé.
)

_UPSERT_ANSWER = text(
    """
    INSERT INTO answer (run_id, fingerprint, event_time)
    VALUES (:run_id, :fingerprint, :event_time)
    ON CONFLICT (run_id, fingerprint) DO UPDATE SET
        event_time = LEAST(answer.event_time, EXCLUDED.event_time)
    RETURNING id
    """
)

_UPSERT_DELIVERY = text(
    """
    INSERT INTO delivery (answer_id, channel, message_ref, event_time)
    VALUES (:answer_id, :channel, :message_ref, :event_time)
    ON CONFLICT (answer_id, channel, message_ref) DO UPDATE SET
        event_time = LEAST(delivery.event_time, EXCLUDED.event_time)
    RETURNING id
    """
)

_LINK_RECIPIENT = text(
    """
    INSERT INTO delivery_recipient (delivery_id, recipient_id)
    VALUES (:delivery_id, :recipient_id)
    ON CONFLICT (delivery_id, recipient_id) DO NOTHING
    """
)


async def _get_or_create(
    conn: AsyncConnection,
    table: Table,
    key: Mapping[str, Any],
    defaults: Mapping[str, Any] | None = None,
) -> int:
    """Renvoie l'identifiant de la ligne portant cette clé, en la créant au besoin.

    Sûr sous concurrence : un doublon inséré par une autre transaction est
    attendu puis relu, sans erreur de contrainte. La lecture d'abord évite de
    réécrire à chaque événement des lignes très partagées.
    """
    lookup = select(table.c.id).where(
        and_(*(table.c[name] == value for name, value in key.items()))
    )
    found = (await conn.execute(lookup)).scalar_one_or_none()
    if found is not None:
        return int(found)
    created = (
        await conn.execute(
            insert(table)
            .values(**key, **(defaults or {}))
            .on_conflict_do_nothing(index_elements=list(key))
            .returning(table.c.id)
        )
    ).scalar_one_or_none()
    if created is not None:
        return int(created)
    return int((await conn.execute(lookup)).scalar_one())


async def _dataset_ids(conn: AsyncConnection, ref: DatasetRef) -> tuple[int, int | None]:
    dataset_id = await _get_or_create(
        conn, schema.dataset, {"namespace": ref.namespace, "name": ref.name}, {"kind": ref.kind}
    )
    if ref.version is None:
        return dataset_id, None
    version_id = await _get_or_create(
        conn, schema.dataset_version, {"dataset_id": dataset_id, "version": ref.version}
    )
    return dataset_id, version_id


def _dataset_order(ref: DatasetRef) -> tuple[str, str, str]:
    return (ref.namespace, ref.name, ref.version or "")


async def record_run(conn: AsyncConnection, record: RunRecord) -> None:
    """Enregistre un run et ses arêtes dans la transaction en cours."""
    if record.event_time.tzinfo is None:
        raise ValueError("event_time doit porter un fuseau horaire.")

    agent_id = (
        await _get_or_create(
            conn, schema.agent, {"namespace": record.agent.namespace, "name": record.agent.name}
        )
        if record.agent is not None
        else None
    )
    tool_id = (
        await _get_or_create(
            conn, schema.tool, {"server": record.tool.server, "name": record.tool.name}
        )
        if record.tool is not None
        else None
    )
    await conn.execute(
        _UPSERT_RUN,
        {
            "run_id": record.run_id,
            "kind": record.kind,
            "agent_id": agent_id,
            "tool_id": tool_id,
            "parent_run_id": record.parent_run_id,
            "trace_id": record.trace_id,
            "span_id": record.span_id,
            "started_at": record.event_time if record.state == "START" else None,
            "ended_at": record.event_time
            if record.state in ("COMPLETE", "ABORT", "FAIL")
            else None,
            "state": record.state,
            "state_time": record.event_time,
        },
    )

    # Ordre stable des jeux de données : deux transactions concurrentes posent
    # leurs verrous dans le même ordre.
    for read in sorted(record.reads, key=lambda item: _dataset_order(item.dataset)):
        dataset_id, version_id = await _dataset_ids(conn, read.dataset)
        await conn.execute(
            _UPSERT_READ,
            {
                "run_id": record.run_id,
                "dataset_id": dataset_id,
                "dataset_version_id": version_id,
                "precision": read.precision,
                "columns": sorted(set(read.columns)),
                "origin": read.origin,
                "event_time": record.event_time,
            },
        )
    for write in sorted(record.writes, key=lambda item: _dataset_order(item.dataset)):
        dataset_id, version_id = await _dataset_ids(conn, write.dataset)
        await conn.execute(
            _UPSERT_WRITE,
            {
                "run_id": record.run_id,
                "dataset_id": dataset_id,
                "dataset_version_id": version_id,
                "event_time": record.event_time,
            },
        )

    for answer in sorted(record.answers, key=lambda item: item.fingerprint):
        answer_id = (
            await conn.execute(
                _UPSERT_ANSWER,
                {
                    "run_id": record.run_id,
                    "fingerprint": answer.fingerprint,
                    "event_time": record.event_time,
                },
            )
        ).scalar_one()
        for delivery in sorted(answer.deliveries, key=lambda d: (d.channel, d.message_ref)):
            delivery_id = (
                await conn.execute(
                    _UPSERT_DELIVERY,
                    {
                        "answer_id": answer_id,
                        "channel": delivery.channel,
                        "message_ref": delivery.message_ref,
                        "event_time": record.event_time,
                    },
                )
            ).scalar_one()
            for pseudonym in sorted(set(delivery.recipients)):
                recipient_id = await _get_or_create(
                    conn, schema.recipient, {"pseudonym": pseudonym}
                )
                await conn.execute(
                    _LINK_RECIPIENT, {"delivery_id": delivery_id, "recipient_id": recipient_id}
                )
