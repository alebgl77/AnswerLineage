"""Lire la liste de rappel d'un incident, page par page.

Chaque lecture est bornée : une page d'entrées, par rang, et pour chacune au plus
``MAX_ENTRY_ANSWERS`` réponses, les plus récentes d'abord. Le curseur d'une page est
le rang de sa dernière entrée : il ne recopie rien de l'émetteur.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text

from answerlineage_api.incidents.models import in_utc
from answerlineage_api.lineage.exposure import SCORE_SCALE
from answerlineage_api.lineage.paging import ANSWER_DIGEST
from answerlineage_api.lineage.reading import Reader
from answerlineage_api.lineage.trace import answer_key
from answerlineage_api.recall.models import (
    MAX_ENTRY_ANSWERS,
    RecallAnswer,
    RecallCounts,
    RecallEntry,
    RecallList,
)

# Le curseur d'une page : le rang de sa dernière entrée.
CURSOR_PATTERN: Final = r"^[0-9]{1,9}$"

_LIST = text(
    """
    SELECT i.severity, r.incident_id, r.built_at, r.reference_at, r.recipients, r.answers
    FROM incident i LEFT JOIN recall_list r ON r.incident_id = i.id
    WHERE i.id = :id
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


@dataclass(frozen=True)
class Missing:
    """Ce qui manque pour lire une liste : l'incident, ou sa liste."""

    code: str


UNKNOWN_INCIDENT: Final = Missing("unknown_incident")
NO_RECALL_LIST: Final = Missing("no_recall_list")


async def recall_list(
    reader: Reader, incident_id: UUID, *, limit: int, after: int
) -> RecallList | Missing:
    """La liste de rappel de l'incident, et ses ``limit`` entrées qui suivent le rang
    ``after``."""
    found = await reader.fetch(_LIST, {"id": incident_id}, limit=1)
    if not found.rows:
        return UNKNOWN_INCIDENT
    head = found.rows[0]
    if head.incident_id is None:
        return NO_RECALL_LIST
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
        reference_at=in_utc(head.reference_at),
        counts=RecallCounts(recipients=head.recipients, answers=head.answers),
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
