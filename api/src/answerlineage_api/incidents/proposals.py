"""Les propositions d'incident nées des tests de qualité en échec : les lire, en décider.

Les workers relèvent les échecs dans les événements et en font des propositions
(``answerlineage_workers.proposals``). Une proposition attend un humain : rien
n'en part, ni analyse, ni annonce, ni quarantaine.

**Confirmer** déclare l'incident, dans une seule transaction : la proposition est
verrouillée, l'incident déclaré sur sa table avec la gravité que donne l'humain
et la fenêtre qu'il donne ou la fenêtre déduite (``store.declare`` : son
périmètre, sa demande d'analyse et l'annonce ``incident.declared``), puis la
proposition passe confirmée, avec son incident. **Écarter** n'annonce rien. Les
deux sont idempotents, et prennent un verrou sur la table des propositions,
comme les workers qui les créent : une décision ne croise ni une création ni une
autre décision.

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
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Row, TextClause, text
from sqlalchemy.ext.asyncio import AsyncConnection

from answerlineage_api.incidents import store
from answerlineage_api.incidents.models import (
    MAX_WINDOW,
    DeclaredDataset,
    DeclaredScope,
    DeclaredWindow,
    IncidentDataset,
    IncidentDeclaration,
    IncidentProposal,
    IncidentProposalDetail,
    IncidentProposalList,
    ProposalConfirmation,
    ProposalCounts,
    ProposalDecision,
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
# Une fenêtre déduite finit au plus tôt juste après le dernier échec : elle le comprend.
_AFTER_LAST_FAILURE: Final = timedelta(seconds=1)

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


# Décider d'une proposition.


class ProposalDecidedError(Exception):
    """La proposition est déjà décidée autrement : ``code`` dit comment."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DerivedWindowTooLongError(Exception):
    """La fenêtre déduite dure plus que la fenêtre d'un incident : le confirmeur la donne."""


_LOCK = text("LOCK TABLE incident_proposal IN SHARE ROW EXCLUSIVE MODE")
_STATE = text("SELECT state FROM incident_proposal WHERE id = :id FOR UPDATE")
_NOW = text("SELECT now()")
_CONFIRM = text(
    """
    UPDATE incident_proposal
    SET state = 'confirmed', incident_id = :incident,
        decided_at = GREATEST(clock_timestamp(), proposed_at)
    WHERE id = :id
    """
)
_DISMISS = text(
    """
    UPDATE incident_proposal
    SET state = 'dismissed', decided_at = GREATEST(clock_timestamp(), proposed_at)
    WHERE id = :id
    """
)
# Ce qu'une décision demandée à une proposition déjà décidée autrement répond.
_REFUSALS: Final = {
    "confirmed": "proposal_confirmed",
    "dismissed": "proposal_dismissed",
    "expired": "proposal_expired",
}


async def _current(conn: AsyncConnection, proposal_id: UUID) -> IncidentProposal:
    row = (
        await conn.execute(
            _ONE, {"id": proposal_id, "max_columns": MAX_PROPOSAL_COLUMNS, "limit": 1}
        )
    ).one()
    named = (
        await conn.execute(_NAMED, {"ids": [proposal_id], "limit": MAX_PROPOSAL_COLUMNS})
    ).all()
    return proposal_of(row, [found.column_name for found in named])


async def _locked(conn: AsyncConnection, proposal_id: UUID) -> str | None:
    """Verrouille la table des propositions, puis la proposition ; rend son état."""
    await conn.execute(_LOCK)
    state: str | None = (await conn.execute(_STATE, {"id": proposal_id})).scalar_one_or_none()
    return state


def _window(found: IncidentProposal, now: datetime) -> DeclaredWindow:
    """La fenêtre déduite : de son début à la fin de l'échec, ou à maintenant ; elle comprend
    toujours le dernier échec."""
    end = found.resolved_at or max(now, found.last_failed_at + _AFTER_LAST_FAILURE)
    if end - found.window.start > MAX_WINDOW:
        raise DerivedWindowTooLongError
    return DeclaredWindow.model_construct(start=found.window.start, end=end)


async def confirm(
    conn: AsyncConnection, proposal_id: UUID, confirmation: ProposalConfirmation
) -> ProposalDecision | None:
    """Confirme la proposition dans la transaction de ``conn`` : l'incident est déclaré sur sa
    table, avec la gravité donnée et la fenêtre donnée ou déduite, son analyse demandée et
    sa déclaration annoncée ; ``None`` si la proposition est inconnue.

    Idempotent : une proposition déjà confirmée est rendue telle quelle. Lève
    ``ProposalDecidedError`` pour une proposition écartée ou expirée, et
    ``DerivedWindowTooLongError`` quand la fenêtre déduite dure plus de 31 jours.
    """
    state = await _locked(conn, proposal_id)
    if state is None:
        return None
    found = await _current(conn, proposal_id)
    if state == "confirmed":
        return ProposalDecision(proposal_id=proposal_id, changed=False, proposal=found)
    if state != "signaled":
        raise ProposalDecidedError(_REFUSALS[state])
    now: datetime = (await conn.execute(_NOW)).scalar_one()
    window = confirmation.window or _window(found, now)
    declaration = IncidentDeclaration(
        scope=DeclaredScope(
            datasets=[DeclaredDataset(namespace=found.dataset.namespace, name=found.dataset.name)]
        ),
        window=window,
        severity=confirmation.severity,
    )
    incident = await store.declare(conn, declaration)
    await conn.execute(_CONFIRM, {"id": proposal_id, "incident": incident.id})
    return ProposalDecision(
        proposal_id=proposal_id, changed=True, proposal=await _current(conn, proposal_id)
    )


async def dismiss(conn: AsyncConnection, proposal_id: UUID) -> ProposalDecision | None:
    """Écarte la proposition dans la transaction de ``conn`` ; ``None`` si elle est inconnue.

    Rien n'est annoncé. Idempotent : une proposition déjà écartée est rendue telle quelle.
    Lève ``ProposalDecidedError`` pour une proposition confirmée ou expirée.
    """
    state = await _locked(conn, proposal_id)
    if state is None:
        return None
    if state == "dismissed":
        return ProposalDecision(
            proposal_id=proposal_id, changed=False, proposal=await _current(conn, proposal_id)
        )
    if state != "signaled":
        raise ProposalDecidedError(_REFUSALS[state])
    await conn.execute(_DISMISS, {"id": proposal_id})
    return ProposalDecision(
        proposal_id=proposal_id, changed=True, proposal=await _current(conn, proposal_id)
    )
