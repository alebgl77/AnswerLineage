"""Lire les listes de rappel, et journaliser la décision qu'un humain prend sur l'une d'elles.

Chaque lecture est bornée : une page d'entrées, par rang, et pour chacune au plus
``MAX_ENTRY_ANSWERS`` réponses, les plus récentes d'abord ; une page de listes. Un curseur
ne recopie rien de l'émetteur.

Décider se fait dans une seule transaction : la liste est verrouillée, la décision déjà
prise relue, puis la nouvelle écrite au journal, avec son auteur, l'heure posée par la base,
et un code de chaque liste fermée. Les workers verrouillent la liste de même pour la refaire
ou la faire remonter : une décision ne croise ni une liste refaite, ni sa remontée. Une
décision est définitive : la même se redemande sans rien changer, une autre est refusée.
La base fige ensuite la liste, et c'est elle qui refuse tout envoi sans validation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Row, TextClause, text
from sqlalchemy.ext.asyncio import AsyncConnection

from answerlineage_api.incidents.models import in_utc
from answerlineage_api.lineage.exposure import SCORE_SCALE
from answerlineage_api.lineage.paging import ANSWER_DIGEST, decode_instant, encode_instant
from answerlineage_api.lineage.reading import Reader
from answerlineage_api.lineage.trace import answer_key
from answerlineage_api.recall.models import (
    MAX_ENTRY_ANSWERS,
    RecallAnswer,
    RecallAuthor,
    RecallCounts,
    RecallDecisionChange,
    RecallDecisionRecord,
    RecallDecisionRequest,
    RecallEntry,
    RecallList,
    RecallLists,
    RecallState,
    RecallSummary,
)
from answerlineage_api.store.schema import SEVERITIES

# Le curseur d'une page d'entrées : le rang de sa dernière entrée.
CURSOR_PATTERN: Final = r"^[0-9]{1,9}$"
# Le curseur d'une page de listes : le poids de la gravité, l'heure de la liste en
# microsecondes depuis l'an 1, puis l'incident.
LISTS_CURSOR_PATTERN: Final = r"^[1-4]\.[0-9]{1,18}\.[0-9a-f]{32}$"
# Une liste a au plus une décision et une remontée.
_JOURNAL_ROWS: Final = 2

_LIST = text(
    """
    SELECT i.severity, r.incident_id, r.built_at, r.reference_at, r.recipients, r.answers,
           (SELECT count(*) FROM recall_dispatch p WHERE p.incident_id = i.id) AS dispatched
    FROM incident i LEFT JOIN recall_list r ON r.incident_id = i.id
    WHERE i.id = :id
    LIMIT :limit
    """
)
_JOURNAL = text(
    """
    SELECT decision, decided_at, author_issuer, author_subject, template, reason
    FROM recall_decision WHERE incident_id = :id
    ORDER BY id
    LIMIT :limit
    """
)
_ENTRIES = text(
    """
    SELECT rank, pseudonym, exposure, answers, latest_answer_at FROM recall_entry
    WHERE incident_id = :id AND rank > :after
    ORDER BY rank
    LIMIT :limit
    """
)
# Les réponses de chaque entrée de la page, les plus récentes d'abord, comme les réponses
# d'un incident : par heure, run, puis condensé de l'empreinte.
_ANSWERS = text(
    f"""
    SELECT e.pseudonym, x.run_id, x.fingerprint, x.produced_at
    FROM unnest(CAST(:pseudonyms AS text[])) AS e(pseudonym)
    CROSS JOIN LATERAL (
        SELECT a.run_id, a.fingerprint, a.produced_at FROM recall_entry_answer a
        WHERE a.incident_id = :id AND a.pseudonym = e.pseudonym
        ORDER BY a.produced_at DESC, a.run_id DESC, {ANSWER_DIGEST} DESC
        LIMIT :per_entry
    ) AS x
    LIMIT :limit
    """  # noqa: S608 — seul un fragment constant est interpolé.
)
_LOCK = text("SELECT incident_id FROM recall_list WHERE incident_id = :id FOR UPDATE")
_DECIDE = text(
    """
    INSERT INTO recall_decision (incident_id, decision, author_issuer, author_subject,
                                 template, reason)
    VALUES (:id, :decision, :issuer, :subject, :template, :reason)
    """
)


def _lists(cursor: ListsCursor | None) -> TextClause:
    after = (
        ""
        if cursor is None
        else """AND (weight < :cursor_weight OR (weight = :cursor_weight
                 AND (built_at, incident_id) > (:cursor_built, :cursor_id)))"""
    )
    return text(
        f"""
        WITH listed AS (
            SELECT r.incident_id, i.severity, r.built_at, r.recipients, r.answers,
                   array_position(CAST(:severities AS text[]), i.severity) AS weight,
                   d.decision, d.decided_at, d.author_issuer, d.author_subject, d.template,
                   d.reason, e.decided_at AS escalated_at,
                   (SELECT count(*) FROM recall_dispatch p
                    WHERE p.incident_id = r.incident_id) AS dispatched
            FROM recall_list r
            JOIN incident i ON i.id = r.incident_id
            LEFT JOIN recall_decision d
              ON d.incident_id = r.incident_id AND d.decision <> 'escalated'
            LEFT JOIN recall_decision e
              ON e.incident_id = r.incident_id AND e.decision = 'escalated'
        )
        SELECT * FROM listed
        WHERE (CAST(:state AS text) IS NULL OR coalesce(decision, 'pending') = :state)
          AND (CAST(:escalated AS boolean) IS NULL OR (escalated_at IS NOT NULL) = :escalated)
          {after}
        ORDER BY weight DESC, built_at, incident_id
        LIMIT :limit
        """  # noqa: S608 — seuls des fragments constants sont interpolés.
    )


@dataclass(frozen=True)
class Missing:
    """Ce qui manque pour lire ou décider une liste : l'incident, ou sa liste."""

    code: str


UNKNOWN_INCIDENT: Final = Missing("unknown_incident")
NO_RECALL_LIST: Final = Missing("no_recall_list")


@dataclass(frozen=True)
class Author:
    """L'auteur d'une décision : l'émetteur OIDC et le sujet de son jeton."""

    issuer: str
    subject: str


class RecallListDecidedError(Exception):
    """La liste porte déjà une autre décision : une décision est définitive."""


@dataclass(frozen=True)
class ListsCursor:
    """Position dans les listes : juste après celle-ci, dans l'ordre de la gravité, puis de
    l'heure de la liste, puis de l'incident."""

    weight: int
    built_at: datetime
    incident_id: UUID

    def encode(self) -> str:
        return f"{self.weight}.{encode_instant(self.built_at)}.{self.incident_id.hex}"

    @classmethod
    def decode(cls, value: str) -> ListsCursor:
        """Lit un curseur à la forme déjà validée (``LISTS_CURSOR_PATTERN``) ; ``CursorError``
        s'il ne désigne aucun instant."""
        weight, micros, identifier = value.split(".")
        return cls(int(weight), decode_instant(micros), UUID(hex=identifier))

    def parameters(self) -> dict[str, Any]:
        return {
            "cursor_weight": self.weight,
            "cursor_built": self.built_at,
            "cursor_id": self.incident_id,
        }


@dataclass(frozen=True)
class _Status:
    state: RecallState
    decision: RecallDecisionRecord | None
    escalated_at: datetime | None


def _record(row: Row[Any]) -> RecallDecisionRecord:
    return RecallDecisionRecord(
        decision=row.decision,
        decided_at=in_utc(row.decided_at),
        author=RecallAuthor(issuer=row.author_issuer, subject=row.author_subject),
        template=row.template,
        reason=row.reason,
    )


def _status(rows: Sequence[Row[Any]]) -> _Status:
    """L'état d'une liste, tiré de son journal : sa décision, sa remontée."""
    decided = next((row for row in rows if row.decision != "escalated"), None)
    escalated = next((row for row in rows if row.decision == "escalated"), None)
    return _Status(
        state="pending" if decided is None else decided.decision,
        decision=None if decided is None else _record(decided),
        escalated_at=None if escalated is None else in_utc(escalated.decided_at),
    )


async def recall_list(
    reader: Reader, incident_id: UUID, *, limit: int, after: int
) -> RecallList | Missing:
    """La liste de rappel de l'incident, son état, et ses ``limit`` entrées qui suivent le
    rang ``after``."""
    found = await reader.fetch(_LIST, {"id": incident_id}, limit=1)
    if not found.rows:
        return UNKNOWN_INCIDENT
    head = found.rows[0]
    if head.incident_id is None:
        return NO_RECALL_LIST
    journal = await reader.fetch(_JOURNAL, {"id": incident_id}, limit=_JOURNAL_ROWS)
    status = _status(journal.rows)
    page = await reader.fetch(_ENTRIES, {"id": incident_id, "after": after}, limit=limit)
    answers: dict[str, list[RecallAnswer]] = {row.pseudonym: [] for row in page.rows}
    listed = await reader.fetch(
        _ANSWERS,
        {"id": incident_id, "pseudonyms": list(answers), "per_entry": MAX_ENTRY_ANSWERS},
        limit=max(1, len(answers)) * MAX_ENTRY_ANSWERS,
    )
    for row in listed.rows:
        answers[row.pseudonym].append(_answer(row))
    last = page.rows[-1] if page.rows else None
    return RecallList(
        incident_id=incident_id,
        severity=head.severity,
        built_at=in_utc(head.built_at),
        counts=RecallCounts(recipients=head.recipients, answers=head.answers),
        state=status.state,
        decision=status.decision,
        escalated_at=status.escalated_at,
        dispatched=head.dispatched,
        reference_at=in_utc(head.reference_at),
        entries=[
            RecallEntry(
                rank=row.rank,
                recipient=row.pseudonym,
                exposure=row.exposure / SCORE_SCALE,
                answer_count=row.answers,
                latest_answer_at=in_utc(row.latest_answer_at),
                answers=answers[row.pseudonym],
                more_answers=row.answers > len(answers[row.pseudonym]),
            )
            for row in page.rows
        ],
        more=page.truncated,
        next_cursor=str(last.rank) if page.truncated and last is not None else None,
    )


def _answer(row: Any) -> RecallAnswer:
    return RecallAnswer(
        id=answer_key(row.run_id, row.fingerprint),
        fingerprint=row.fingerprint,
        produced_at=in_utc(row.produced_at),
    )


async def recall_lists(
    reader: Reader,
    *,
    state: RecallState | None,
    escalated: bool | None,
    limit: int,
    cursor: ListsCursor | None,
) -> RecallLists:
    """Les listes de rappel, de l'incident le plus grave au moins grave, puis la plus ancienne
    d'abord ; seulement celles dans cet état, remontées ou non, quand c'est demandé."""
    params: dict[str, Any] = {
        "severities": list(SEVERITIES),
        "state": state,
        "escalated": escalated,
        **({} if cursor is None else cursor.parameters()),
    }
    page = await reader.fetch(_lists(cursor), params, limit=limit)
    last = page.rows[-1] if page.rows else None
    return RecallLists(
        lists=[
            RecallSummary(
                incident_id=row.incident_id,
                severity=row.severity,
                built_at=in_utc(row.built_at),
                counts=RecallCounts(recipients=row.recipients, answers=row.answers),
                state="pending" if row.decision is None else row.decision,
                decision=None if row.decision is None else _record(row),
                escalated_at=None if row.escalated_at is None else in_utc(row.escalated_at),
                dispatched=row.dispatched,
            )
            for row in page.rows
        ],
        more=page.truncated,
        next_cursor=ListsCursor(last.weight, last.built_at, last.incident_id).encode()
        if page.truncated and last is not None
        else None,
    )


async def decide(
    conn: AsyncConnection, incident_id: UUID, author: Author, asked: RecallDecisionRequest
) -> RecallDecisionChange | Missing:
    """Journalise la décision sur la liste de l'incident, dans la transaction de ``conn``.

    La même décision redemandée ne change rien (``changed`` faux) ;
    ``RecallListDecidedError`` quand la liste en porte déjà une autre."""
    found = (await conn.execute(_LIST, {"id": incident_id, "limit": 1})).first()
    if found is None:
        return UNKNOWN_INCIDENT
    # Le verrou de la liste : ni les workers qui la refont ou la font remonter, ni une autre
    # décision ne la changent avant la fin de cette transaction.
    locked = (await conn.execute(_LOCK, {"id": incident_id})).first()
    if locked is None:
        return NO_RECALL_LIST
    template = asked.chosen_template
    journal = {"id": incident_id, "limit": _JOURNAL_ROWS}
    status = _status((await conn.execute(_JOURNAL, journal)).all())
    decided = status.decision
    changed = decided is None
    if decided is None:
        await conn.execute(
            _DECIDE,
            {
                "id": incident_id,
                "decision": asked.decision,
                "issuer": author.issuer,
                "subject": author.subject,
                "template": template,
                "reason": asked.reason,
            },
        )
        status = _status((await conn.execute(_JOURNAL, journal)).all())
    elif (decided.decision, decided.template, decided.reason) != (
        asked.decision,
        template,
        asked.reason,
    ):
        raise RecallListDecidedError
    return RecallDecisionChange(
        incident_id=incident_id,
        changed=changed,
        state=status.state,
        decision=status.decision,
        escalated_at=status.escalated_at,
        dispatched=found.dispatched,
    )
