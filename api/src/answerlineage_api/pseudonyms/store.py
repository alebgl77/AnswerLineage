"""La révélation d'un destinataire, et son journal.

Révéler, c'est dire laquelle des identités candidates a le pseudonyme visé. Le
calcul se fait hors de la base, par le coffre à clés ; la base ne reçoit que le
journal de l'action. Dans une seule transaction :

1. un verrou consultatif propre à l'auteur, pour que deux révélations simultanées
   de la même personne se comptent l'une après l'autre ;
2. la borne de débit : ``REVELATIONS_PER_HOUR`` révélations par auteur sur l'heure
   glissante, comptées dans le journal lui-même, qu'aucun réplica de l'API ne
   contourne ;
3. le destinataire est-il connu, du graphe ou des résultats d'un incident ;
4. une ligne au journal, son heure posée par la base.

Une révélation refusée pour le débit n'est pas journalisée : le journal ne grandit
pas au rythme de qui insiste.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from answerlineage_api.lineage.reading import Reader
from answerlineage_api.pseudonyms.models import (
    RevelationAuthor,
    RevelationEntry,
    RevelationJournal,
    RevelationRequest,
)
from answerlineage_api.store.schema import RevealResult

# Révélations au plus par auteur, sur l'heure glissante : assez pour prévenir les
# destinataires d'un incident l'un après l'autre, trop peu pour parcourir un annuaire.
REVELATIONS_PER_HOUR: Final = 20
WINDOW: Final = timedelta(hours=1)
# Espace des verrous consultatifs des révélations : « REVE » en ASCII. La forme à deux
# entiers ne rencontre pas les verrous à une clé des workers.
_LOCK_SPACE: Final = 0x52455645
JOURNAL_CURSOR_PATTERN: Final = r"^[0-9]{1,18}$"

_LOCK = text("SELECT pg_advisory_xact_lock(:space, hashtext(:issuer || ' ' || :subject))")
_RECENT = text(
    """
    SELECT count(*) AS revealed, min(revealed_at) AS oldest, now() AS now
    FROM recipient_revelation
    WHERE author_issuer = :issuer AND author_subject = :subject
      AND revealed_at > now() - :window
    """
)
_KNOWN = text(
    """
    SELECT EXISTS (SELECT 1 FROM recipient WHERE pseudonym = :pseudonym)
        OR EXISTS (SELECT 1 FROM incident_recipient WHERE pseudonym = :pseudonym)
    """
)
_JOURNALED = text(
    """
    INSERT INTO recipient_revelation (author_issuer, author_subject, reason, pseudonym,
                                      candidates, result)
    VALUES (:issuer, :subject, :reason, :pseudonym, :candidates, :result)
    RETURNING id
    """
)
_JOURNAL = text(
    """
    SELECT id, revealed_at, author_issuer, author_subject, reason, pseudonym, candidates, result
    FROM recipient_revelation
    WHERE (CAST(:before AS bigint) IS NULL OR id < :before)
    ORDER BY id DESC
    LIMIT :limit
    """
)


@dataclass(frozen=True)
class Author:
    """L'auteur d'une révélation : l'émetteur OIDC et le sujet de son jeton."""

    issuer: str
    subject: str


@dataclass(frozen=True)
class Outcome:
    """Une révélation journalisée : son numéro et son résultat."""

    revelation: int
    result: RevealResult


class RevelationRateLimitedError(Exception):
    """L'auteur a atteint la borne de débit ; ``retry_after`` : secondes avant une place."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("revelation_rate_limited")
        self.retry_after = retry_after


async def reveal(
    conn: AsyncConnection, author: Author, asked: RevelationRequest, matched: int | None
) -> Outcome:
    """Journalise la révélation et rend son résultat. ``matched`` est le rang de la candidate
    qui a le pseudonyme, calculé par le coffre ; il ne compte que si le destinataire est
    connu. ``RevelationRateLimitedError`` au-delà de la borne de débit, sans rien journaliser.
    """
    who = {"issuer": author.issuer, "subject": author.subject}
    await conn.execute(_LOCK, {"space": _LOCK_SPACE, **who})
    recent = (await conn.execute(_RECENT, {**who, "window": WINDOW})).one()
    if recent.revealed >= REVELATIONS_PER_HOUR:
        free_at = recent.oldest + WINDOW
        raise RevelationRateLimitedError(max(1, math.ceil((free_at - recent.now).total_seconds())))
    known = bool((await conn.execute(_KNOWN, {"pseudonym": asked.pseudonym})).scalar_one())
    result: RevealResult = (
        "unknown_recipient" if not known else "matched" if matched is not None else "no_match"
    )
    revelation = (
        await conn.execute(
            _JOURNALED,
            {
                **who,
                "reason": asked.reason,
                "pseudonym": asked.pseudonym,
                "candidates": len(asked.candidates),
                "result": result,
            },
        )
    ).scalar_one()
    return Outcome(int(revelation), result)


async def journal(reader: Reader, *, limit: int, before: int | None) -> RevelationJournal:
    """Une page du journal, la révélation la plus récente d'abord."""
    found = await reader.fetch(_JOURNAL, {"before": before}, limit=limit)
    page = found.rows
    return RevelationJournal(
        revelations=[
            RevelationEntry(
                id=row.id,
                revealed_at=row.revealed_at,
                author=RevelationAuthor(issuer=row.author_issuer, subject=row.author_subject),
                reason=row.reason,
                pseudonym=row.pseudonym,
                candidates=row.candidates,
                result=row.result,
            )
            for row in page
        ],
        next_cursor=str(page[-1].id) if found.truncated else None,
    )
