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

Des pseudonymes, des identifiants et des nombres : aucun contenu, aucun texte.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text

from answerlineage_api.lineage.exposure import decay, units
from answerlineage_api.lineage.reading import Reader

# Liens d'une analyse vers ses destinataires, au plus : la borne de l'analyse.
MAX_LINKS: Final = 200_000

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND: Final = timedelta(microseconds=1)

_FORGET = (
    text("DELETE FROM recall_entry_answer WHERE incident_id = :id"),
    text("DELETE FROM recall_entry WHERE incident_id = :id"),
)
_FORGET_LIST = text("DELETE FROM recall_list WHERE incident_id = :id")
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


async def build(reader: Reader, incident_id: UUID, reference_at: datetime) -> int:
    """Fait la liste de rappel de l'incident, à partir du résultat de son analyse enregistré
    dans cette transaction, et rend son nombre de destinataires. La liste précédente est
    remplacée."""
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
    ses destinataires."""
    params = {"id": incident_id}
    for statement in (*_FORGET, _FORGET_LIST):
        await reader.execute(statement, params)
