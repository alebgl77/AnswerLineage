"""Déclarer un incident, et le relire avec l'état et le résultat de son analyse.

La déclaration écrit l'incident, son périmètre et sa demande d'analyse dans la
transaction de l'appelant : une demande n'existe jamais sans son incident, ni
un incident sans sa demande. Les workers réclament ensuite la demande
(``answerlineage_workers.incidents``). Quand un webhook est en service, la même
transaction met en file l'annonce de la déclaration (``webhooks.py``).

Chaque lecture est bornée : une page d'incidents, une page des réponses
touchées d'un incident, et les destinataires de ces réponses. Un incident se lit
avec sa dernière quarantaine, active ou levée (``quarantine.py``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID, uuid4

from sqlalchemy import Row, TextClause, insert, text
from sqlalchemy.ext.asyncio import AsyncConnection

from answerlineage_api.incidents.models import (
    MAX_DATASETS,
    Analysis,
    AnalysisResult,
    Incident,
    IncidentDataset,
    IncidentDeclaration,
    IncidentDetail,
    IncidentList,
    IncidentScope,
    Quarantine,
    TouchedAnswer,
    in_utc,
)
from answerlineage_api.incidents.webhooks import announce
from answerlineage_api.lineage.models import Coverage, ImpactCounts, Window
from answerlineage_api.lineage.paging import (
    AFTER_CURSOR,
    ANSWER_DIGEST,
    ANSWER_ORDER,
    Cursor,
    decode_instant,
    encode_instant,
)
from answerlineage_api.lineage.reading import Reader
from answerlineage_api.lineage.trace import answer_key
from answerlineage_api.store import schema

# Le curseur d'une liste d'incidents : l'heure de déclaration du dernier, puis son identifiant.
INCIDENT_CURSOR_PATTERN: Final = r"^[0-9]{1,18}\.[0-9a-f]{32}$"
# Les liens d'une page de réponses vers leurs destinataires : la borne de l'analyse.
MAX_LINKS: Final = 200_000

_COLUMNS = """i.id, i.state, i.severity, i.window_start, i.window_end, i.declared_at,
           s.state AS analysis_state, s.requested_at, s.attempts, s.error, s.known_at,
           s.finished_at, s.answers, s.recipients, s.truncated,
           q.state AS quarantine_state, q.mode AS quarantine_mode,
           q.activated_at AS quarantine_activated_at, q.lifted_at AS quarantine_lifted_at,
           pr.id AS proposal_id"""
# L'incident, son analyse, sa dernière quarantaine, s'il en a une, et la proposition dont la
# confirmation l'a déclaré, s'il en vient une.
_FROM = """incident i JOIN incident_analysis s ON s.incident_id = i.id
    LEFT JOIN LATERAL (
        SELECT state, mode, activated_at, lifted_at FROM incident_quarantine
        WHERE incident_id = i.id ORDER BY id DESC LIMIT 1
    ) q ON true
    LEFT JOIN incident_proposal pr ON pr.incident_id = i.id"""

_ONE = text(
    f"""
    SELECT {_COLUMNS}, s.coverage, s.unknown_datasets
    FROM {_FROM}
    WHERE i.id = :id
    LIMIT :limit
    """  # noqa: S608 — seules des colonnes constantes sont interpolées.
)

_PERIMETERS = text(
    """
    SELECT incident_id, namespace, name FROM incident_dataset
    WHERE incident_id = ANY(:ids)
    ORDER BY incident_id, namespace COLLATE "C", name COLLATE "C"
    LIMIT :limit
    """
)

_RECIPIENTS = text(
    """
    SELECT run_id, fingerprint, pseudonym FROM incident_recipient
    WHERE incident_id = :id AND run_id = ANY(:run_ids)
    ORDER BY run_id, fingerprint, pseudonym
    LIMIT :limit
    """
)


def _incidents(cursor: IncidentCursor | None) -> TextClause:
    after = "" if cursor is None else "WHERE (i.declared_at, i.id) < (:cursor_time, :cursor_id)"
    return text(
        f"""
        SELECT {_COLUMNS}
        FROM {_FROM}
        {after}
        ORDER BY i.declared_at DESC, i.id DESC
        LIMIT :limit
        """  # noqa: S608 — seuls des fragments constants sont interpolés.
    )


def _answers(cursor: Cursor | None) -> TextClause:
    # Les réponses de l'incident, sous les noms de colonnes de l'ordre des réponses du graphe.
    after = "" if cursor is None else f"WHERE {AFTER_CURSOR}"
    return text(
        f"""
        SELECT a.run_id, a.fingerprint, a.event_time, {ANSWER_DIGEST} AS digest
        FROM (
            SELECT run_id, fingerprint, produced_at AS event_time
            FROM incident_answer WHERE incident_id = :id
        ) AS a
        {after}
        ORDER BY {ANSWER_ORDER}
        LIMIT :limit
        """  # noqa: S608 — seuls des fragments constants sont interpolés.
    )


@dataclass(frozen=True)
class IncidentCursor:
    """Position dans la liste des incidents : juste après celui-ci."""

    declared_at: datetime
    id: UUID

    def encode(self) -> str:
        return f"{encode_instant(self.declared_at)}.{self.id.hex}"

    @classmethod
    def decode(cls, value: str) -> IncidentCursor:
        """Lit un curseur dont la forme est déjà validée (``INCIDENT_CURSOR_PATTERN``)."""
        micros, identifier = value.split(".")
        return cls(decode_instant(micros), UUID(hex=identifier))

    def parameters(self) -> dict[str, Any]:
        return {"cursor_time": self.declared_at, "cursor_id": self.id}


async def declare(conn: AsyncConnection, declaration: IncidentDeclaration) -> Incident:
    """Enregistre l'incident, son périmètre et sa demande d'analyse ; rend l'incident déclaré."""
    incident_id = uuid4()
    window = declaration.window
    declared_at = (
        await conn.execute(
            insert(schema.incident)
            .values(
                id=incident_id,
                state="qualified",
                severity=declaration.severity,
                window_start=window.start,
                window_end=window.end,
            )
            .returning(schema.incident.c.declared_at)
        )
    ).scalar_one()
    datasets = sorted((item.namespace, item.name) for item in declaration.scope.datasets)
    await conn.execute(
        insert(schema.incident_dataset),
        [
            {"incident_id": incident_id, "namespace": namespace, "name": name}
            for namespace, name in datasets
        ],
    )
    requested_at = (
        await conn.execute(
            insert(schema.incident_analysis)
            .values(incident_id=incident_id)
            .returning(schema.incident_analysis.c.requested_at)
        )
    ).scalar_one()
    await announce(conn, incident_id, "incident.declared")
    return Incident(
        id=incident_id,
        state="qualified",
        severity=declaration.severity,
        scope=IncidentScope(
            datasets=[IncidentDataset(namespace=ns, name=name) for ns, name in datasets]
        ),
        window=Window(start=in_utc(window.start), end=in_utc(window.end)),
        declared_at=in_utc(declared_at),
        analysis=Analysis(
            state="pending",
            requested_at=in_utc(requested_at),
            attempts=0,
            error=None,
            known_at=None,
            finished_at=None,
            counts=None,
            truncated=None,
        ),
        quarantine=None,
        proposal_id=None,
    )


def _optional(value: datetime | None) -> datetime | None:
    return None if value is None else in_utc(value)


def _quarantine(row: Row[Any]) -> Quarantine | None:
    if row.quarantine_state is None:
        return None
    return Quarantine(
        state=row.quarantine_state,
        mode=row.quarantine_mode,
        activated_at=in_utc(row.quarantine_activated_at),
        lifted_at=_optional(row.quarantine_lifted_at),
    )


def _incident(row: Row[Any], datasets: Sequence[IncidentDataset]) -> Incident:
    done = row.analysis_state == "done"
    return Incident(
        id=row.id,
        state=row.state,
        severity=row.severity,
        scope=IncidentScope(datasets=list(datasets)),
        window=Window(start=in_utc(row.window_start), end=in_utc(row.window_end)),
        declared_at=in_utc(row.declared_at),
        analysis=Analysis(
            state=row.analysis_state,
            requested_at=in_utc(row.requested_at),
            attempts=row.attempts,
            error=row.error,
            known_at=_optional(row.known_at),
            finished_at=_optional(row.finished_at),
            counts=ImpactCounts(
                answers=row.answers, recipients=row.recipients, lower_bound=row.truncated
            )
            if done
            else None,
            truncated=row.truncated if done else None,
        ),
        quarantine=_quarantine(row),
        proposal_id=row.proposal_id,
    )


async def _perimeters(reader: Reader, ids: list[UUID]) -> dict[UUID, list[IncidentDataset]]:
    found = await reader.fetch(_PERIMETERS, {"ids": ids}, limit=max(1, len(ids)) * MAX_DATASETS)
    perimeters: dict[UUID, list[IncidentDataset]] = {incident_id: [] for incident_id in ids}
    for row in found.rows:
        perimeters[row.incident_id].append(IncidentDataset(namespace=row.namespace, name=row.name))
    return perimeters


async def incidents(reader: Reader, *, limit: int, cursor: IncidentCursor | None) -> IncidentList:
    """Les ``limit`` incidents déclarés les plus récents, après ``cursor``."""
    params = {} if cursor is None else cursor.parameters()
    page = await reader.fetch(_incidents(cursor), params, limit=limit)
    perimeters = await _perimeters(reader, [row.id for row in page.rows])
    last = page.rows[-1] if page.rows else None
    return IncidentList(
        incidents=[_incident(row, perimeters[row.id]) for row in page.rows],
        more=page.truncated,
        next_cursor=IncidentCursor(last.declared_at, last.id).encode()
        if page.truncated and last is not None
        else None,
    )


async def incident(
    reader: Reader, incident_id: UUID, *, limit: int, cursor: Cursor | None
) -> IncidentDetail | None:
    """L'incident, son analyse et, quand elle est faite, une page des réponses touchées."""
    found = await reader.fetch(_ONE, {"id": incident_id}, limit=1)
    if not found.rows:
        return None
    row = found.rows[0]
    perimeters = await _perimeters(reader, [incident_id])
    summary = _incident(row, perimeters[incident_id])
    result = None
    if row.analysis_state == "done":
        result = await _result(reader, row, limit=limit, cursor=cursor)
    return IncidentDetail(**dict(summary), result=result)


async def _result(
    reader: Reader, row: Row[Any], *, limit: int, cursor: Cursor | None
) -> AnalysisResult:
    params = {"id": row.id} | ({} if cursor is None else cursor.parameters())
    page = await reader.fetch(_answers(cursor), params, limit=limit)
    recipients: dict[tuple[UUID, str], list[str]] = {
        (answer.run_id, answer.fingerprint): [] for answer in page.rows
    }
    linked = await reader.fetch(
        _RECIPIENTS,
        {"id": row.id, "run_ids": sorted({answer.run_id for answer in page.rows})},
        limit=MAX_LINKS,
    )
    for link in linked.rows:
        listed = recipients.get((link.run_id, link.fingerprint))
        if listed is not None:
            listed.append(link.pseudonym)
    return AnalysisResult(
        unknown_datasets=[IncidentDataset(**item) for item in row.unknown_datasets],
        coverage=Coverage.model_validate(row.coverage),
        answers=[
            TouchedAnswer(
                id=answer_key(answer.run_id, answer.fingerprint),
                fingerprint=answer.fingerprint,
                produced_at=in_utc(answer.event_time),
                recipients=recipients[answer.run_id, answer.fingerprint],
            )
            for answer in page.rows
        ],
        more=page.truncated,
        next_cursor=Cursor.after(page.rows[-1]).encode() if page.truncated and page.rows else None,
    )
