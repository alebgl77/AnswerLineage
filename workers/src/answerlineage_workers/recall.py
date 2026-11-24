"""La liste de rappel d'un incident : un message par destinataire, les plus exposés d'abord.

La liste se fait de l'analyse d'impact, dans la transaction qui l'enregistre
(``incidents.py``) : une analyse faite et non tronquée a sa liste, une analyse
tronquée n'en a pas, puisque son résultat ne dit pas tous les destinataires.

- **Une entrée par destinataire et par incident.** Un destinataire de cinq
  réponses touchées y figure une fois, avec ses cinq réponses : on ne le prévient
  qu'une fois. Une réponse touchée sans destinataire connu n'y est pas : il n'y a
  personne à prévenir.
- **Rangée par exposition.** L'exposition d'un destinataire reprend les poids du
  score d'exposition (``answerlineage_api.lineage.exposure``) : chaque réponse
  touchée qu'il a reçue pèse 1, et ce poids diminue de moitié tous les 30 jours
  depuis l'heure de la réponse. C'est exactement ce qu'il ajoute au score
  d'exposition des données de l'incident ; le « 1 + destinataires » du score d'un
  jeu compte, lui, les autres personnes, qui ont chacune leur entrée. L'heure de
  référence est celle du graphe lu par l'analyse (``known_at``) : rejouer la même
  analyse donne la même liste.
- **Un ordre total et déterministe.** L'exposition au millionième, la plus forte
  d'abord ; puis le plus de réponses touchées ; puis la réponse la plus récente ;
  puis le pseudonyme, dans l'ordre de ses caractères. Deux entrées d'une liste
  n'ont jamais le même rang.
- **Refaite à l'identique.** Faire la liste remplace la précédente : la même
  analyse donne la même liste, une analyse rejouée la sienne.
- **Décidée, elle ne change plus.** Un responsable d'agent la valide, la modifie
  ou la refuse par l'API ; une analyse rejouée ensuite ne la refait ni ne la
  retire : ce qui a été décidé est ce qui partira. La base le tient aussi
  (``ck_recall_list_frozen``). La liste est verrouillée le temps de la refaire,
  comme le temps d'une décision : l'une attend l'autre.
- **Remontée faute de décision.** Une liste qui a des destinataires et reste sans
  décision au-delà du délai réglé remonte au responsable de données
  (``escalate``) : une ligne au journal des décisions, sans auteur, que l'API
  rend. Une fois par liste, quel que soit le nombre de workers.

Des pseudonymes, des identifiants et des nombres : aucun contenu, aucun texte.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.lineage.exposure import decay, units
from answerlineage_api.lineage.reading import Reader

logger = logging.getLogger("answerlineage_workers")

# Liens d'une analyse vers ses destinataires, au plus : la borne de l'analyse.
MAX_LINKS: Final = 200_000
# Une liste reste deux jours sans décision avant de remonter au responsable de données ; le
# délai se règle d'une heure à un mois.
DEFAULT_DECISION_DELAY: Final = timedelta(hours=48)
MIN_DECISION_DELAY: Final = timedelta(hours=1)
MAX_DECISION_DELAY: Final = timedelta(hours=720)
# Listes remontées au plus par passage : les suivantes, au passage suivant.
MAX_ESCALATIONS: Final = 1_000
# La remontée revient chaque minute : une liste remonte au plus une minute après son délai.
DEFAULT_ESCALATION_EVERY: Final = timedelta(minutes=1)

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND: Final = timedelta(microseconds=1)

_FORGET = (
    text("DELETE FROM recall_entry_answer WHERE incident_id = :id"),
    text("DELETE FROM recall_entry WHERE incident_id = :id"),
)
_FORGET_LIST = text("DELETE FROM recall_list WHERE incident_id = :id")
# La liste, verrouillée le temps de la refaire ou de la retirer, comme le temps d'une décision.
_LOCK = text("SELECT incident_id FROM recall_list WHERE incident_id = :id FOR UPDATE")
_DECIDED = text(
    """
    SELECT EXISTS (
        SELECT 1 FROM recall_decision WHERE incident_id = :id AND decision <> 'escalated'
    )
    """
)
# Les listes restées sans décision ni remontée au-delà du délai, et qui ont des
# destinataires : verrouillées, celles qu'une décision tient sont sautées.
_OVERDUE = text(
    """
    SELECT r.incident_id FROM recall_list r
    WHERE r.recipients > 0 AND r.built_at <= now() - :delay
      AND NOT EXISTS (SELECT 1 FROM recall_decision d WHERE d.incident_id = r.incident_id)
    ORDER BY r.built_at, r.incident_id
    LIMIT :limit
    FOR UPDATE OF r SKIP LOCKED
    """
)
# Relu sous le verrou : une décision prise entre-temps l'emporte, et un autre worker qui
# fait remonter la même liste n'en fait pas deux remontées.
_ESCALATE = text(
    """
    INSERT INTO recall_decision (incident_id, decision)
    SELECT overdue.id, 'escalated' FROM unnest(CAST(:ids AS uuid[])) AS overdue(id)
    WHERE NOT EXISTS (SELECT 1 FROM recall_decision d WHERE d.incident_id = overdue.id)
    ORDER BY overdue.id
    ON CONFLICT (incident_id) WHERE decision = 'escalated' DO NOTHING
    RETURNING incident_id
    """
)
_KEEP_LIST = text(
    """
    INSERT INTO recall_list (incident_id, reference_at, recipients, answers)
    VALUES (:id, :reference_at, 0, 0)
    ON CONFLICT (incident_id) DO UPDATE
    SET built_at = now(), reference_at = excluded.reference_at, recipients = 0, answers = 0
    """
)
# Chaque lien d'une réponse touchée vers l'un de ses destinataires, et l'heure de la réponse.
_LINKS = text(
    """
    SELECT r.pseudonym, a.produced_at
    FROM incident_recipient r
    JOIN incident_answer a
      ON a.incident_id = r.incident_id AND a.run_id = r.run_id AND a.fingerprint = r.fingerprint
    WHERE r.incident_id = :id
    LIMIT :limit
    """
)
_KEEP_ENTRIES = text(
    """
    INSERT INTO recall_entry (incident_id, pseudonym, rank, exposure, answers, latest_answer_at)
    SELECT :id, entry.pseudonym, entry.rank, entry.exposure, entry.answers, entry.latest
    FROM unnest(
        CAST(:pseudonyms AS text[]), CAST(:ranks AS integer[]), CAST(:exposures AS bigint[]),
        CAST(:answers AS integer[]), CAST(:latest AS timestamptz[])
    ) AS entry(pseudonym, rank, exposure, answers, latest)
    """
)
_KEEP_ANSWERS = text(
    """
    INSERT INTO recall_entry_answer (incident_id, pseudonym, run_id, fingerprint, produced_at)
    SELECT r.incident_id, r.pseudonym, r.run_id, r.fingerprint, a.produced_at
    FROM incident_recipient r
    JOIN incident_answer a
      ON a.incident_id = r.incident_id AND a.run_id = r.run_id AND a.fingerprint = r.fingerprint
    WHERE r.incident_id = :id
    """
)
_COUNT = text(
    """
    UPDATE recall_list
    SET recipients = :recipients,
        answers = (
            SELECT count(*) FROM (
                SELECT DISTINCT run_id, fingerprint FROM recall_entry_answer
                WHERE incident_id = :id
            ) AS distinct_answers
        )
    WHERE incident_id = :id
    """
)


@dataclass(frozen=True)
class Entry:
    """Un destinataire de la liste : son exposition en millionièmes, ses réponses touchées."""

    pseudonym: str
    exposure: int
    answers: int
    latest_answer_at: datetime

    def order(self) -> tuple[int, int, int, str]:
        """La clé du rang : la plus forte exposition, le plus de réponses, la plus récente,
        puis le pseudonyme."""
        latest = (self.latest_answer_at - _EPOCH) // _MICROSECOND
        return (-self.exposure, -self.answers, -latest, self.pseudonym)


def ranked(links: Iterable[tuple[str, datetime]], reference_at: datetime) -> list[Entry]:
    """Les entrées de la liste, dans l'ordre de leur rang, à partir des liens d'une réponse
    touchée (son heure) vers l'un de ses destinataires (son pseudonyme).

    Chaque poids se compte en billionièmes entiers, comme le score d'exposition : la somme
    est exacte et ne dépend pas de l'ordre des liens ; elle s'arrondit au millionième.
    """
    weights: dict[str, int] = {}
    counts: dict[str, int] = {}
    latest: dict[str, datetime] = {}
    for pseudonym, produced_at in links:
        weights[pseudonym] = weights.get(pseudonym, 0) + decay(produced_at, reference_at)
        counts[pseudonym] = counts.get(pseudonym, 0) + 1
        if pseudonym not in latest or latest[pseudonym] < produced_at:
            latest[pseudonym] = produced_at
    entries = [
        Entry(pseudonym, units(weight), counts[pseudonym], latest[pseudonym])
        for pseudonym, weight in weights.items()
    ]
    return sorted(entries, key=Entry.order)


async def _decided(reader: Reader, incident_id: UUID) -> bool:
    """Verrouille la liste de l'incident, et dit si elle est décidée."""
    params = {"id": incident_id}
    await reader.execute(_LOCK, params)
    decided = bool((await reader.execute(_DECIDED, params)).scalar_one())
    if decided:
        # L'identifiant est tiré par l'API : le journal ne dit rien d'autre de l'incident.
        logger.info(
            "Liste de rappel de l'incident %s déjà décidée : gardée telle quelle.", incident_id
        )
    return decided


async def build(reader: Reader, incident_id: UUID, reference_at: datetime) -> int | None:
    """Fait la liste de rappel de l'incident, à partir du résultat de son analyse enregistré
    dans cette transaction, et rend son nombre de destinataires. La liste précédente est
    remplacée, sauf décidée : elle est gardée, et rien n'est rendu."""
    if await _decided(reader, incident_id):
        return None
    params: dict[str, Any] = {"id": incident_id}
    await reader.execute(_KEEP_LIST, {**params, "reference_at": reference_at})
    for statement in _FORGET:
        await reader.execute(statement, params)
    found = await reader.fetch(_LINKS, params, limit=MAX_LINKS)
    entries = ranked(((row.pseudonym, row.produced_at) for row in found.rows), reference_at)
    if entries:
        await reader.execute(
            _KEEP_ENTRIES,
            {
                **params,
                "pseudonyms": [entry.pseudonym for entry in entries],
                "ranks": list(range(1, len(entries) + 1)),
                "exposures": [entry.exposure for entry in entries],
                "answers": [entry.answers for entry in entries],
                "latest": [entry.latest_answer_at for entry in entries],
            },
        )
        await reader.execute(_KEEP_ANSWERS, params)
    await reader.execute(_COUNT, {**params, "recipients": len(entries)})
    return len(entries)


async def forget(reader: Reader, incident_id: UUID) -> None:
    """Retire la liste de l'incident : son analyse rejouée est tronquée, et ne dit plus tous
    ses destinataires. Une liste décidée est gardée."""
    if await _decided(reader, incident_id):
        return
    params = {"id": incident_id}
    for statement in (*_FORGET, _FORGET_LIST):
        await reader.execute(statement, params)


def checked_delay(delay: timedelta) -> timedelta:
    """Le délai avant la remontée, refusé hors de ses bornes : d'une heure à 720 heures."""
    if not MIN_DECISION_DELAY <= delay <= MAX_DECISION_DELAY:
        raise ValueError("Le délai avant la remontée va d'une heure à 720 heures.")
    return delay


async def escalate(engine: AsyncEngine, *, delay: timedelta = DEFAULT_DECISION_DELAY) -> list[UUID]:
    """Fait remonter au responsable de données les listes restées sans décision au-delà de
    ``delay`` depuis qu'elles ont été faites, à l'heure de la base, et rend leurs incidents.

    Chaque remontée s'écrit au journal des décisions, sans auteur, une fois par liste."""
    params = {"delay": checked_delay(delay), "limit": MAX_ESCALATIONS}
    async with engine.begin() as conn:
        overdue = sorted((await conn.execute(_OVERDUE, params)).scalars())
        if not overdue:
            return []
        escalated = list((await conn.execute(_ESCALATE, {"ids": overdue})).scalars())
    for incident_id in escalated:
        logger.warning(
            "Liste de rappel de l'incident %s sans décision depuis plus de %d h :"
            " remontée au responsable de données.",
            incident_id,
            delay // timedelta(hours=1),
        )
    return escalated
