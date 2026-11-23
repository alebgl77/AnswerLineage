"""Les propositions d'incident nées des tests de qualité en échec : les lire.

Les workers relèvent les échecs dans les événements et en font des propositions
(``answerlineage_workers.proposals``). Une proposition attend un humain : rien
n'en part, ni analyse, ni annonce, ni quarantaine.

Chaque lecture est bornée : une page de propositions, les colonnes de chacune
(``MAX_PROPOSAL_COLUMNS``), et les runs en échec d'une proposition, comptés
jusqu'à ``MAX_COUNTED_RUNS``. La fin de l'échec (``resolved_at``) se déduit à la
lecture de la dernière réussite et du dernier échec connus de chaque colonne en
échec : toutes réussissent de nouveau, et elle est la plus récente de leurs
réussites, ce qui allonge la fenêtre plutôt que de la raccourcir.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Row, TextClause, text

from answerlineage_api.incidents.models import (
    IncidentDataset,
    IncidentProposal,
    IncidentProposalDetail,
    IncidentProposalList,
    ProposalCounts,
    ProposedWindow,
    in_utc,
)
from answerlineage_api.lineage.paging import decode_instant, encode_instant
from answerlineage_api.lineage.reading import Reader
from answerlineage_api.store.schema import MAX_PROPOSAL_COLUMNS, ProposalState

# Le curseur d'une liste de propositions : l'heure de proposition de la dernière, puis son
# identifiant.
PROPOSAL_CURSOR_PATTERN: Final = r"^[0-9]{1,18}\.[0-9a-f]{32}$"
# Runs en échec comptés pour une proposition : au-delà, le décompte est un minimum.
MAX_COUNTED_RUNS: Final = 10_000

# La proposition, et la fin de son échec, déduite de ses colonnes : nulle tant que l'une
# échoue, qu'une n'est pas suivie, ou que la borne des colonnes nommées est atteinte sur
# une proposition qui porte sur la table entière (d'autres colonnes ont pu échouer).
_COLUMNS = """p.id, p.state, p.namespace, p.name, p.whole_table, p.classes, p.severity,
           p.emitter, p.first_failed_at, p.last_failed_at, p.window_start, p.window_basis,
           p.proposed_at, p.decided_at, p.incident_id,
           CASE WHEN r.passed AND NOT (p.whole_table AND r.named >= :max_columns)
                THEN r.last_passed_at END AS resolved_at"""
_FROM = """incident_proposal p
    LEFT JOIN LATERAL (
        SELECT bool_and(coalesce(q.last_passed_at > q.last_failed_at, false)) AS passed,
               max(q.last_passed_at) AS last_passed_at,
               count(*) FILTER (WHERE k.column_name <> '') AS named
        FROM (
            SELECT c.column_name FROM incident_proposal_column c WHERE c.proposal_id = p.id
            UNION ALL
            SELECT '' WHERE p.whole_table
        ) AS k
        LEFT JOIN quality_column q
          ON q.namespace = p.namespace AND q.name = p.name AND q.column_name = k.column_name
    ) AS r ON true"""

_ONE = text(
    f"SELECT {_COLUMNS} FROM {_FROM} WHERE p.id = :id LIMIT :limit"  # noqa: S608 — constant.
)
_NAMED = text(
    """
    SELECT proposal_id, column_name FROM incident_proposal_column
    WHERE proposal_id = ANY(:ids)
    ORDER BY proposal_id, column_name COLLATE "C"
    LIMIT :limit
    """
)
_FAILED_RUNS = text(
    """
    SELECT count(*) FROM (
        SELECT run_id FROM quality_signal
        WHERE proposal_id = :id AND state = 'attached'
        GROUP BY run_id
        LIMIT :limit
    ) AS runs
    """
)


def _proposals(state: ProposalState | None, cursor: ProposalCursor | None) -> TextClause:
    conditions = []
    if state is not None:
        conditions.append("p.state = :state")
    if cursor is not None:
        conditions.append("(p.proposed_at, p.id) < (:cursor_time, :cursor_id)")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    return text(
        f"""
        SELECT {_COLUMNS} FROM {_FROM}
        {where}
        ORDER BY p.proposed_at DESC, p.id DESC
        LIMIT :limit
        """  # noqa: S608 — seuls des fragments constants sont interpolés.
    )


@dataclass(frozen=True)
class ProposalCursor:
    """Position dans la liste des propositions : juste après celle-ci."""

    proposed_at: datetime
    id: UUID

    def encode(self) -> str:
        return f"{encode_instant(self.proposed_at)}.{self.id.hex}"

    @classmethod
    def decode(cls, value: str) -> ProposalCursor:
        """Lit un curseur dont la forme est déjà validée (``PROPOSAL_CURSOR_PATTERN``)."""
        micros, identifier = value.split(".")
        return cls(decode_instant(micros), UUID(hex=identifier))

    def parameters(self) -> dict[str, Any]:
        return {"cursor_time": self.proposed_at, "cursor_id": self.id}


def _optional(value: datetime | None) -> datetime | None:
    return None if value is None else in_utc(value)


def proposal_of(row: Row[Any], columns: Sequence[str]) -> IncidentProposal:
    """Une proposition lue dans la base, avec ses colonnes nommées."""
    return IncidentProposal(
        id=row.id,
        state=row.state,
        dataset=IncidentDataset(namespace=row.namespace, name=row.name),
        whole_table=row.whole_table,
        columns=list(columns),
        assertions=list(row.classes),
        severity=row.severity,
        emitter=row.emitter,
        first_failed_at=in_utc(row.first_failed_at),
        last_failed_at=in_utc(row.last_failed_at),
        resolved_at=_optional(row.resolved_at),
        window=ProposedWindow(start=in_utc(row.window_start), basis=row.window_basis),
        proposed_at=in_utc(row.proposed_at),
        decided_at=_optional(row.decided_at),
        incident_id=row.incident_id,
    )


async def _named(reader: Reader, ids: list[UUID]) -> dict[UUID, list[str]]:
    found = await reader.fetch(_NAMED, {"ids": ids}, limit=max(1, len(ids)) * MAX_PROPOSAL_COLUMNS)
    named: dict[UUID, list[str]] = {proposal: [] for proposal in ids}
    for row in found.rows:
        named[row.proposal_id].append(row.column_name)
    return named


async def proposals(
    reader: Reader, *, state: ProposalState | None, limit: int, cursor: ProposalCursor | None
) -> IncidentProposalList:
    """Les ``limit`` propositions les plus récentes, dans l'état ``state`` s'il est donné,
    après ``cursor``."""
    params: dict[str, Any] = {"max_columns": MAX_PROPOSAL_COLUMNS}
    if state is not None:
        params["state"] = state
    if cursor is not None:
        params |= cursor.parameters()
    page = await reader.fetch(_proposals(state, cursor), params, limit=limit)
    named = await _named(reader, [row.id for row in page.rows])
    last = page.rows[-1] if page.rows else None
    return IncidentProposalList(
        proposals=[proposal_of(row, named[row.id]) for row in page.rows],
        more=page.truncated,
        next_cursor=ProposalCursor(last.proposed_at, last.id).encode()
        if page.truncated and last is not None
        else None,
    )


async def proposal(reader: Reader, proposal_id: UUID) -> IncidentProposalDetail | None:
    """La proposition et ses décomptes ; ``None`` si aucune ne porte cet identifiant."""
    found = await reader.fetch(
        _ONE, {"id": proposal_id, "max_columns": MAX_PROPOSAL_COLUMNS}, limit=1
    )
    if not found.rows:
        return None
    named = await _named(reader, [proposal_id])
    counted: int = (
        await reader.execute(_FAILED_RUNS, {"id": proposal_id, "limit": MAX_COUNTED_RUNS + 1})
    ).scalar_one()
    summary = proposal_of(found.rows[0], named[proposal_id])
    return IncidentProposalDetail(
        **dict(summary),
        counts=ProposalCounts(
            failed_runs=min(counted, MAX_COUNTED_RUNS), lower_bound=counted > MAX_COUNTED_RUNS
        ),
    )
