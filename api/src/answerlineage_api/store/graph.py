"""Écriture du graphe de lignage.

``record_run`` enregistre ce qu'un événement apprend d'un run : son agent ou
son outil, ses lectures, ses écritures, ses réponses et leur diffusion. Les
écritures sont idempotentes et commutatives : rejouer un enregistrement, ou
enregistrer deux événements du même run dans un ordre quelconque, donne le
même graphe, heures comprises.

Chaque ligne garde la plus ancienne heure d'enregistrement qu'on lui présente :
la réception de l'événement qui l'a fait connaître le premier. Une lecture garde
la première et la dernière heure d'événement reçues : un événement antidaté
élargit son intervalle, il ne la déplace pas.

Plusieurs transactions écrivent le graphe en même temps, une par lot de la file,
et chacune garde jusqu'à sa validation les lignes qu'elle a créées ou modifiées.
``prepare_runs`` pose d'avance, dans un ordre global, celles que les lots se
disputent : sans lui, deux lots qui créent les mêmes jeux de données dans des
ordres différents s'attendent l'un l'autre, et PostgreSQL en annule un.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
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
    # Réception de l'événement par AnswerLineage (``event_raw.recorded_at``) :
    # l'heure d'enregistrement des lignes qu'il fait connaître. Inconnue, c'est
    # l'heure de la transaction.
    recorded_at: datetime | None = None


# Heure d'enregistrement présentée : la réception de l'événement, ou la transaction.
_RECORDED = "COALESCE(CAST(:recorded_at AS timestamptz), now())"

# Le dernier événement fixe l'état du run. À heure égale, un état terminal
# l'emporte, pour que le résultat ne dépende pas de l'ordre d'arrivée. Un run
# « job » se précise en outil ou en agent, jamais l'inverse.
_UPSERT_RUN = text(
    f"""
    INSERT INTO run (run_id, kind, agent_id, tool_id, parent_run_id, trace_id, span_id,
                     started_at, ended_at, state, state_time, recorded_at)
    VALUES (:run_id, :kind, :agent_id, :tool_id, :parent_run_id, :trace_id, :span_id,
            :started_at, :ended_at, :state, :state_time, {_RECORDED})
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
        state_time = GREATEST(run.state_time, EXCLUDED.state_time),
        recorded_at = LEAST(run.recorded_at, EXCLUDED.recorded_at)
    """  # noqa: S608 — seul un fragment constant est interpolé.
)

# Une lecture par run, jeu de données et version. Deux événements se fusionnent :
# la précision la plus fine l'emporte, les colonnes s'additionnent, la
# réconciliation avec l'entrepôt prime sur l'analyse, et l'intervalle des heures
# d'événement s'étend de la première à la dernière.
_UPSERT_READ = text(
    f"""
    INSERT INTO read_edge (run_id, dataset_id, dataset_version_id, precision, columns, origin,
                           event_time, last_event_time, recorded_at)
    VALUES (:run_id, :dataset_id, :dataset_version_id, :precision,
            CAST(:columns AS text[]), :origin, :event_time, :event_time, {_RECORDED})
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
        event_time = LEAST(read_edge.event_time, EXCLUDED.event_time),
        last_event_time = GREATEST(read_edge.last_event_time, EXCLUDED.last_event_time),
        recorded_at = LEAST(read_edge.recorded_at, EXCLUDED.recorded_at)
    """  # noqa: S608 — seuls un nom de contrainte et un fragment constants sont interpolés.
)

_UPSERT_WRITE = text(
    f"""
    INSERT INTO write_edge (run_id, dataset_id, dataset_version_id, event_time, recorded_at)
    VALUES (:run_id, :dataset_id, :dataset_version_id, :event_time, {_RECORDED})
    ON CONFLICT ON CONSTRAINT {schema.WRITE_EDGE_KEY} DO UPDATE SET
        event_time = LEAST(write_edge.event_time, EXCLUDED.event_time),
        recorded_at = LEAST(write_edge.recorded_at, EXCLUDED.recorded_at)
    """  # noqa: S608 — seuls un nom de contrainte et un fragment constants sont interpolés.
)

_UPSERT_ANSWER = text(
    f"""
    INSERT INTO answer (run_id, fingerprint, event_time, recorded_at)
    VALUES (:run_id, :fingerprint, :event_time, {_RECORDED})
    ON CONFLICT (run_id, fingerprint) DO UPDATE SET
        event_time = LEAST(answer.event_time, EXCLUDED.event_time),
        recorded_at = LEAST(answer.recorded_at, EXCLUDED.recorded_at)
    RETURNING id
    """  # noqa: S608 — seul un fragment constant est interpolé.
)

_UPSERT_DELIVERY = text(
    f"""
    INSERT INTO delivery (answer_id, channel, message_ref, event_time, recorded_at)
    VALUES (:answer_id, :channel, :message_ref, :event_time, {_RECORDED})
    ON CONFLICT (answer_id, channel, message_ref) DO UPDATE SET
        event_time = LEAST(delivery.event_time, EXCLUDED.event_time),
        recorded_at = LEAST(delivery.recorded_at, EXCLUDED.recorded_at)
    RETURNING id
    """  # noqa: S608 — seul un fragment constant est interpolé.
)

# Un lien déjà connu n'est réécrit que par un événement plus ancien.
_LINK_RECIPIENT = text(
    f"""
    INSERT INTO delivery_recipient AS link (delivery_id, recipient_id, event_time, recorded_at)
    VALUES (:delivery_id, :recipient_id, :event_time, {_RECORDED})
    ON CONFLICT (delivery_id, recipient_id) DO UPDATE SET
        event_time = LEAST(link.event_time, EXCLUDED.event_time),
        recorded_at = LEAST(link.recorded_at, EXCLUDED.recorded_at)
    WHERE EXCLUDED.event_time < link.event_time OR EXCLUDED.recorded_at < link.recorded_at
    """  # noqa: S608 — seul un fragment constant est interpolé.
)


async def _get_or_create(
    conn: AsyncConnection,
    table: Table,
    key: Mapping[str, Any],
    recorded_at: datetime | None,
    defaults: Mapping[str, Any] | None = None,
) -> int:
    """Renvoie l'identifiant de la ligne portant cette clé, en la créant au besoin.

    Sûr sous concurrence : un doublon inséré par une autre transaction est
    attendu puis relu, sans erreur de contrainte. La lecture d'abord évite de
    réécrire à chaque événement des lignes très partagées : une ligne connue
    n'est réécrite que pour avancer son heure d'enregistrement, quand
    l'événement a été reçu avant celui qui l'a fait connaître.
    """
    lookup = select(table.c.id, table.c.recorded_at).where(
        and_(*(table.c[name] == value for name, value in key.items()))
    )
    found = (await conn.execute(lookup)).one_or_none()
    if found is None:
        values = {**key, **(defaults or {})}
        if recorded_at is not None:
            values["recorded_at"] = recorded_at
        created = (
            await conn.execute(
                insert(table)
                .values(**values)
                .on_conflict_do_nothing(index_elements=list(key))
                .returning(table.c.id)
            )
        ).scalar_one_or_none()
        if created is not None:
            return int(created)
        found = (await conn.execute(lookup)).one()
    if recorded_at is not None and recorded_at < found.recorded_at:
        await conn.execute(
            table.update()
            .where(table.c.id == found.id, table.c.recorded_at > recorded_at)
            .values(recorded_at=recorded_at)
        )
    return int(found.id)


@dataclass(frozen=True)
class PreparedRuns:
    """Ce que ``prepare_runs`` a posé pour un lot, que ``record_run`` n'a pas à refaire.

    Les lignes partagées, par table et clé naturelle : leur identifiant, et la plus
    ancienne réception du lot qui les nomme, qu'elles portent déjà (nulle : l'heure
    de la transaction). Les enregistrements dont le run est déjà écrit.
    """

    ids: Mapping[tuple[str, tuple[object, ...]], tuple[int, datetime | None]]
    runs: frozenset[RunRecord]


async def _shared_id(
    conn: AsyncConnection,
    prepared: PreparedRuns | None,
    table: Table,
    key: Mapping[str, Any],
    recorded_at: datetime | None,
    defaults: Mapping[str, Any] | None = None,
) -> int:
    """L'identifiant d'une ligne partagée.

    Celui que le lot a posé, quand la ligne porte déjà une réception qui ne suit pas
    celle-ci ; sinon ``_get_or_create``, qui avance son heure d'enregistrement.
    """
    if prepared is not None:
        known = prepared.ids.get((table.name, tuple(key.values())))
        if known is not None:
            found, earliest = known
            if recorded_at is None or (earliest is not None and earliest <= recorded_at):
                return found
    return await _get_or_create(conn, table, key, recorded_at, defaults)


async def _dataset_ids(
    conn: AsyncConnection,
    ref: DatasetRef,
    recorded_at: datetime | None,
    prepared: PreparedRuns | None = None,
) -> tuple[int, int | None]:
    dataset_id = await _shared_id(
        conn,
        prepared,
        schema.dataset,
        {"namespace": ref.namespace, "name": ref.name},
        recorded_at,
        {"kind": ref.kind},
    )
    if ref.version is None:
        return dataset_id, None
    version_id = await _shared_id(
        conn,
        prepared,
        schema.dataset_version,
        {"dataset_id": dataset_id, "version": ref.version},
        recorded_at,
    )
    return dataset_id, version_id


def _dataset_order(ref: DatasetRef) -> tuple[str, str, str]:
    return (ref.namespace, ref.name, ref.version or "")


def _checked_times(record: RunRecord) -> None:
    for moment in (record.event_time, record.recorded_at):
        if moment is not None and moment.tzinfo is None:
            raise ValueError("event_time et recorded_at doivent porter un fuseau horaire.")


def _datasets(record: RunRecord) -> list[DatasetRef]:
    """Les jeux de données d'un run, dans l'ordre où ``record_run`` les écrit."""
    reads = sorted((read.dataset for read in record.reads), key=_dataset_order)
    writes = sorted((write.dataset for write in record.writes), key=_dataset_order)
    return [*reads, *writes]


async def _upsert_run(
    conn: AsyncConnection, record: RunRecord, agent_id: int | None, tool_id: int | None
) -> None:
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
            "recorded_at": record.recorded_at,
        },
    )


class _Earliest[K: (str, tuple[str, str], tuple[str, str, str])]:
    """Les clés d'une table, chacune avec la plus ancienne réception qui la nomme.

    Une réception inconnue est l'heure de la transaction, postérieure à toute autre.
    La première valeur annexe donnée pour une clé est gardée, comme la nature du jeu
    de données que fixe le premier événement qui le crée.
    """

    def __init__(self) -> None:
        self.rows: dict[K, tuple[datetime | None, Mapping[str, Any]]] = {}

    def add(self, key: K, at: datetime | None, defaults: Mapping[str, Any] | None = None) -> None:
        known = self.rows.get(key)
        if known is None:
            self.rows[key] = (at, defaults or {})
        elif at is not None and (known[0] is None or at < known[0]):
            self.rows[key] = (at, known[1])

    def ordered(self) -> Iterable[tuple[K, datetime | None, Mapping[str, Any]]]:
        """Dans l'ordre de la clé naturelle : le même pour toutes les transactions."""
        for key in sorted(self.rows):
            at, defaults = self.rows[key]
            yield key, at, defaults


async def prepare_runs(conn: AsyncConnection, records: Sequence[RunRecord]) -> PreparedRuns:
    """Pose d'avance les lignes qu'un lot partage avec d'autres, dans un ordre global.

    Un lot de la file projette ses événements l'un après l'autre dans une seule
    transaction, qui garde jusqu'à sa validation chaque ligne qu'elle a créée, ou
    dont elle a avancé l'heure d'enregistrement, et chaque run qu'elle a écrit. Deux
    lots qui y viennent dans des ordres différents s'attendent l'un l'autre : un
    interblocage, que PostgreSQL rompt en annulant l'un des deux.

    Avant la projection du lot, ``prepare_runs`` écrit ces lignes, table par table
    (agents, outils, jeux de données, versions, destinataires, puis runs) et dans
    chaque table dans l'ordre de la clé naturelle : chaque transaction prend ses
    verrous dans le même ordre, et n'attend qu'une transaction qui n'attend pas la
    sienne. Chaque ligne partagée reçoit la plus ancienne réception du lot qui la
    nomme ; chaque run, l'écriture de chacun de ses événements, dans l'ordre du lot.
    Les arêtes d'un run ne sont écrites que par la transaction qui tient ce run :
    elles ne se disputent pas.

    Rend ce qui est posé, dont ``record_run`` se sert pour ne pas le refaire. Le
    graphe obtenu est celui que donne ``record_run`` seul, événement après
    événement, dans l'ordre du lot.
    """
    for record in records:
        _checked_times(record)
    agents: _Earliest[tuple[str, str]] = _Earliest()
    tools: _Earliest[tuple[str, str]] = _Earliest()
    datasets: _Earliest[tuple[str, str]] = _Earliest()
    versions: _Earliest[tuple[str, str, str]] = _Earliest()
    recipients: _Earliest[str] = _Earliest()
    for record in records:
        at = record.recorded_at
        if record.agent is not None:
            agents.add((record.agent.namespace, record.agent.name), at)
        if record.tool is not None:
            tools.add((record.tool.server, record.tool.name), at)
        for ref in _datasets(record):
            datasets.add((ref.namespace, ref.name), at, {"kind": ref.kind})
            if ref.version is not None:
                versions.add((ref.namespace, ref.name, ref.version), at)
        for answer in record.answers:
            for delivery in answer.deliveries:
                for pseudonym in delivery.recipients:
                    recipients.add(pseudonym, at)

    ids: dict[tuple[str, tuple[object, ...]], tuple[int, datetime | None]] = {}

    async def shared(
        table: Table,
        key: Mapping[str, Any],
        at: datetime | None,
        defaults: Mapping[str, Any] | None = None,
    ) -> int:
        found = await _get_or_create(conn, table, key, at, defaults)
        ids[(table.name, tuple(key.values()))] = (found, at)
        return found

    for (namespace, name), at, _ in agents.ordered():
        await shared(schema.agent, {"namespace": namespace, "name": name}, at)
    for (server, name), at, _ in tools.ordered():
        await shared(schema.tool, {"server": server, "name": name}, at)
    dataset_ids = {
        key: await shared(schema.dataset, {"namespace": key[0], "name": key[1]}, at, defaults)
        for key, at, defaults in datasets.ordered()
    }
    for (namespace, name, version), at, _ in versions.ordered():
        dataset_id = dataset_ids[(namespace, name)]
        await shared(schema.dataset_version, {"dataset_id": dataset_id, "version": version}, at)
    for pseudonym, at, _ in recipients.ordered():
        await shared(schema.recipient, {"pseudonym": pseudonym}, at)
    # Les événements d'un même run gardent l'ordre du lot : le tri est stable.
    for record in sorted(records, key=lambda item: item.run_id):
        agent, tool = record.agent, record.tool
        await _upsert_run(
            conn,
            record,
            None if agent is None else ids[("agent", (agent.namespace, agent.name))][0],
            None if tool is None else ids[("tool", (tool.server, tool.name))][0],
        )
    return PreparedRuns(ids=ids, runs=frozenset(records))


async def record_run(
    conn: AsyncConnection, record: RunRecord, prepared: PreparedRuns | None = None
) -> None:
    """Enregistre un run et ses arêtes dans la transaction en cours.

    ``prepared`` est ce que ``prepare_runs`` a posé pour le lot de cet
    enregistrement, dans la même transaction : ses lignes partagées et son run ne
    sont ni relus ni réécrits.
    """
    _checked_times(record)
    recorded_at = record.recorded_at
    times = {"event_time": record.event_time, "recorded_at": recorded_at}

    agent_id = (
        await _shared_id(
            conn,
            prepared,
            schema.agent,
            {"namespace": record.agent.namespace, "name": record.agent.name},
            recorded_at,
        )
        if record.agent is not None
        else None
    )
    tool_id = (
        await _shared_id(
            conn,
            prepared,
            schema.tool,
            {"server": record.tool.server, "name": record.tool.name},
            recorded_at,
        )
        if record.tool is not None
        else None
    )
    if prepared is None or record not in prepared.runs:
        await _upsert_run(conn, record, agent_id, tool_id)

    # Ordre stable des jeux de données : deux transactions concurrentes posent
    # leurs verrous dans le même ordre.
    for read in sorted(record.reads, key=lambda item: _dataset_order(item.dataset)):
        dataset_id, version_id = await _dataset_ids(conn, read.dataset, recorded_at, prepared)
        await conn.execute(
            _UPSERT_READ,
            {
                "run_id": record.run_id,
                "dataset_id": dataset_id,
                "dataset_version_id": version_id,
                "precision": read.precision,
                "columns": sorted(set(read.columns)),
                "origin": read.origin,
                **times,
            },
        )
    for write in sorted(record.writes, key=lambda item: _dataset_order(item.dataset)):
        dataset_id, version_id = await _dataset_ids(conn, write.dataset, recorded_at, prepared)
        await conn.execute(
            _UPSERT_WRITE,
            {
                "run_id": record.run_id,
                "dataset_id": dataset_id,
                "dataset_version_id": version_id,
                **times,
            },
        )

    for answer in sorted(record.answers, key=lambda item: item.fingerprint):
        answer_id = (
            await conn.execute(
                _UPSERT_ANSWER,
                {"run_id": record.run_id, "fingerprint": answer.fingerprint, **times},
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
                        **times,
                    },
                )
            ).scalar_one()
            for pseudonym in sorted(set(delivery.recipients)):
                recipient_id = await _shared_id(
                    conn, prepared, schema.recipient, {"pseudonym": pseudonym}, recorded_at
                )
                await conn.execute(
                    _LINK_RECIPIENT,
                    {"delivery_id": delivery_id, "recipient_id": recipient_id, **times},
                )
