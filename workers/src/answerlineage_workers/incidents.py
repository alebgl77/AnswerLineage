"""Analyse d'impact des incidents déclarés.

La déclaration d'un incident écrit une demande d'analyse dans sa transaction
(``incident_analysis``, état ``pending``). Un worker la réclame, lance la
propagation sur chaque jeu de données du périmètre et la fenêtre de validité,
et enregistre le résultat : les réponses touchées, par leur run et leur
empreinte, les pseudonymes de leurs destinataires, la couverture du lignage sur
la fenêtre et ses trous, et si une borne a été dépassée. Des identifiants
seulement, jamais de contenu.

- **Le graphe tel qu'il était connu.** La propagation lit le graphe tel qu'il
  était connu à l'heure de l'analyse (``known_at``) : l'heure de la base au
  premier essai, gardée pour les suivants. Un rejeu lit donc le même graphe.
- **Sur un graphe entier.** Aucune analyse ne part pendant une reconstruction du
  graphe, ni tant que la file n'a pas rendu au graphe ce qu'elle y a remis
  (``gate.py``) : la demande attend, et lit ensuite le graphe reconstruit.
- **Une analyse, un worker.** La demande est réclamée et traitée dans une seule
  transaction, verrouillée : les autres workers la sautent
  (``FOR UPDATE SKIP LOCKED``). Un worker arrêté en cours d'analyse n'écrit
  rien, et la demande redevient visible.
- **Sans doublon.** Le résultat remplace celui d'une analyse précédente du même
  incident : rejouer une analyse ne duplique ni réponse ni destinataire.
- **Un échec se dit par un code.** Une analyse en échec est remise à plus tard,
  avec un délai qui double à chaque essai ; au-delà de ``MAX_ATTEMPTS`` essais,
  elle est en échec pour de bon. Seul un code d'une liste fermée est gardé :
  ``query_timeout`` quand une lecture dépasse le délai de l'analyse,
  ``analysis_error`` pour toute autre erreur, dont seule la classe est
  journalisée.
- **Sa liste de rappel avec elle.** Une analyse faite et non tronquée fait la liste
  de rappel de l'incident dans la transaction qui l'enregistre (``recall.py``) : un
  message par destinataire, les plus exposés d'abord. Une analyse tronquée ne dit
  pas tous les destinataires : elle n'a pas de liste.
- **Annoncée dans sa transaction.** Une analyse faite, ou en échec pour de bon,
  met son annonce dans la file des webhooks quand un webhook est en service
  (``answerlineage_api.incidents.webhooks``), dans la transaction qui
  l'enregistre ; une analyse remise à plus tard ne change pas d'état et
  n'annonce rien.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal
from uuid import UUID

from sqlalchemy import func, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from answerlineage_api.incidents.models import MAX_DATASETS
from answerlineage_api.incidents.webhooks import announce
from answerlineage_api.lineage import coverage, impact
from answerlineage_api.lineage.paging import ANSWER_ORDER
from answerlineage_api.lineage.reading import Reader, ReadTimedOut, known
from answerlineage_api.store import schema
from answerlineage_workers import gate, recall

logger = logging.getLogger("answerlineage_workers")

# Une analyse lit plus que la page d'une route : elle a une minute.
DEFAULT_ANALYSIS_TIMEOUT: Final = timedelta(minutes=1)
# Huit essais espacés de 5 s à 10 min : un quart d'heure pour qu'une panne passagère passe.
MAX_ATTEMPTS: Final = 8
RETRY_BASE_SECONDS: Final = 5.0
RETRY_CAP_SECONDS: Final = 3600.0

# Bornes du résultat, celles de la propagation : runs réunis sur tout le périmètre,
# réponses gardées, liens de diffusion vers leurs destinataires.
MAX_RUNS: Final = 200_000
MAX_ANSWERS: Final = impact.MAX_ANSWERS
MAX_LINKS: Final = impact.MAX_LINKS

Outcome = Literal["done", "postponed", "failed"]
# Analyse un incident : sa connexion, son identifiant, l'heure du graphe lu, l'échéance.
Analyzer = Callable[[AsyncConnection, UUID, datetime, float], Awaitable[None]]

# Aucune analyse tant qu'une reconstruction n'a pas rendu au graphe ce qu'elle a
# remis en file : sur un graphe partiel, l'analyse manquerait des réponses.
_CLAIM = text(
    f"""
    SELECT incident_id, coalesce(known_at, now()) AS known_at, attempts
    FROM incident_analysis
    WHERE state = 'pending' AND available_at <= now() AND NOT {gate.REBUILD_PENDING}
    ORDER BY available_at, incident_id
    LIMIT 1
    FOR UPDATE SKIP LOCKED
    """  # noqa: S608 — seul un fragment constant est interpolé.
)
_POSTPONE = text(
    """
    UPDATE incident_analysis
    SET attempts = attempts + 1,
        error = :error,
        known_at = :known_at,
        state = CASE WHEN attempts + 1 >= :max_attempts THEN 'failed' ELSE 'pending' END,
        finished_at = CASE WHEN attempts + 1 >= :max_attempts THEN clock_timestamp() END,
        available_at = now() + make_interval(secs => LEAST(:cap, :base * power(2, attempts)))
    WHERE incident_id = :id
    RETURNING state
    """
)

_NO_STATEMENT_TIMEOUT = text("SET LOCAL statement_timeout TO DEFAULT")

_INCIDENT = text("SELECT window_start, window_end FROM incident WHERE id = :id LIMIT :limit")
_PERIMETER = text(
    """
    SELECT namespace, name FROM incident_dataset WHERE incident_id = :id
    ORDER BY namespace COLLATE "C", name COLLATE "C" LIMIT :limit
    """
)
_FORGET_RECIPIENTS = text("DELETE FROM incident_recipient WHERE incident_id = :id")
_FORGET_ANSWERS = text("DELETE FROM incident_answer WHERE incident_id = :id")
# Chaque décompte lit au plus une ligne de plus que sa borne.
_COUNT_ANSWERS = text(
    f"""
    SELECT count(*) FROM (
        SELECT 1 FROM answer a WHERE a.run_id = ANY(:run_ids) AND {known("a")} LIMIT :limit
    ) AS bounded
    """  # noqa: S608 — seul un fragment constant est interpolé.
)
# Les plus récentes d'abord : sous la borne, ce sont elles qui restent.
_KEEP_ANSWERS = text(
    f"""
    INSERT INTO incident_answer (incident_id, run_id, fingerprint, produced_at)
    SELECT :id, a.run_id, a.fingerprint, a.event_time FROM answer a
    WHERE a.run_id = ANY(:run_ids) AND {known("a")}
    ORDER BY {ANSWER_ORDER}
    LIMIT :limit
    """  # noqa: S608 — seuls des fragments constants sont interpolés.
)
_LINKS = f"""
    FROM incident_answer t
    JOIN answer a ON a.run_id = t.run_id AND a.fingerprint = t.fingerprint
    JOIN delivery d ON d.answer_id = a.id AND {known("d")}
    JOIN delivery_recipient l ON l.delivery_id = d.id AND {known("l")}
    JOIN recipient c ON c.id = l.recipient_id
    WHERE t.incident_id = :id
"""
_COUNT_LINKS = text(
    f"SELECT count(*) FROM (SELECT 1 {_LINKS} LIMIT :limit) AS bounded"  # noqa: S608 — constant.
)
_KEEP_RECIPIENTS = text(
    f"""
    INSERT INTO incident_recipient (incident_id, run_id, fingerprint, pseudonym)
    SELECT :id, t.run_id, t.fingerprint, c.pseudonym {_LINKS}
    ORDER BY t.produced_at DESC, t.run_id DESC, t.fingerprint, c.pseudonym
    LIMIT :limit
    ON CONFLICT DO NOTHING
    """
)
_DISTINCT_RECIPIENTS = text(
    "SELECT count(DISTINCT pseudonym) FROM incident_recipient WHERE incident_id = :id"
)


@dataclass(frozen=True)
class Processed:
    """Une demande d'analyse réclamée, et ce qu'il en est advenu."""

    incident_id: UUID
    outcome: Outcome


async def analyze_incident(
    conn: AsyncConnection, incident_id: UUID, known_at: datetime, deadline: float
) -> None:
    """Analyse l'incident sur le graphe connu à ``known_at`` et enregistre le résultat.

    Chaque lecture part sous le temps qui reste avant ``deadline`` (horloge
    monotone) ; au-delà, ``ReadTimedOut``.
    """
    reader = Reader(conn, deadline=deadline)
    incident = (await reader.fetch(_INCIDENT, {"id": incident_id}, limit=1)).rows[0]
    start, end = incident.window_start, incident.window_end
    perimeter = await reader.fetch(_PERIMETER, {"id": incident_id}, limit=MAX_DATASETS)

    run_ids: dict[UUID, None] = {}
    unknown: list[dict[str, str]] = []
    truncated = perimeter.truncated
    for dataset in perimeter.rows:
        touched = await impact.touched_runs(
            reader,
            namespace=dataset.namespace,
            name=dataset.name,
            start=start,
            end=end,
            known_at=known_at,
        )
        if touched is None:
            unknown.append({"namespace": dataset.namespace, "name": dataset.name})
            continue
        truncated = truncated or touched.truncated
        run_ids.update(dict.fromkeys(touched.run_ids))
    if len(run_ids) > MAX_RUNS:
        truncated = True
    runs = list(run_ids)[:MAX_RUNS]

    as_of = {"id": incident_id, "known_at": known_at}
    await reader.execute(_FORGET_RECIPIENTS, as_of)
    await reader.execute(_FORGET_ANSWERS, as_of)
    answers = await _bounded(reader, _COUNT_ANSWERS, {**as_of, "run_ids": runs}, MAX_ANSWERS)
    await reader.execute(_KEEP_ANSWERS, {**as_of, "run_ids": runs, "limit": MAX_ANSWERS})
    links = await _bounded(reader, _COUNT_LINKS, as_of, MAX_LINKS)
    await reader.execute(_KEEP_RECIPIENTS, {**as_of, "limit": MAX_LINKS})
    recipients = (await reader.execute(_DISTINCT_RECIPIENTS, as_of)).scalar_one()
    truncated = truncated or answers > MAX_ANSWERS or links > MAX_LINKS

    measured = await coverage.over_period(reader, start=start, end=end, now=known_at)
    table = schema.incident_analysis
    await reader.execute(
        table.update()
        .where(table.c.incident_id == incident_id)
        .values(
            state="done",
            error=None,
            known_at=known_at,
            finished_at=func.clock_timestamp(),
            answers=min(answers, MAX_ANSWERS),
            recipients=recipients,
            truncated=truncated,
            coverage=measured.model_dump(mode="json"),
            unknown_datasets=unknown,
        ),
        {},
    )
    if truncated:
        await recall.forget(reader, incident_id)
    else:
        await recall.build(reader, incident_id, known_at)


async def _bounded(reader: Reader, statement: Any, params: dict[str, Any], bound: int) -> int:
    """Un décompte qui s'arrête une ligne après sa borne."""
    counted: int = (await reader.execute(statement, {**params, "limit": bound + 1})).scalar_one()
    return counted


def _error_code(error: Exception) -> str:
    return "query_timeout" if isinstance(error, ReadTimedOut) else "analysis_error"


async def process_analysis(
    engine: AsyncEngine,
    *,
    timeout: timedelta = DEFAULT_ANALYSIS_TIMEOUT,
    analyze: Analyzer = analyze_incident,
) -> Processed | None:
    """Réclame une demande d'analyse, la traite et l'enregistre, dans une transaction.

    Rend ``None`` quand aucune demande n'attend, ou qu'une reconstruction du
    graphe tourne ou n'a pas encore rendu au graphe ce qu'elle a remis en file :
    la demande attend qu'il soit entier.
    """
    async with engine.begin() as conn:
        if not await gate.share(conn):
            return None
        claimed = (await conn.execute(_CLAIM)).first()
        if claimed is None:
            return None
        incident_id: UUID = claimed.incident_id
        try:
            # Point de reprise : un échec n'annule que l'analyse, pas la demande.
            async with conn.begin_nested():
                deadline = time.monotonic() + timeout.total_seconds()
                await analyze(conn, incident_id, claimed.known_at, deadline)
        except Exception as error:
            state = (
                await conn.execute(
                    _POSTPONE,
                    {
                        "id": incident_id,
                        "error": _error_code(error),
                        "known_at": claimed.known_at,
                        "max_attempts": MAX_ATTEMPTS,
                        "base": RETRY_BASE_SECONDS,
                        "cap": RETRY_CAP_SECONDS,
                    },
                )
            ).scalar_one()
            # L'identifiant est tiré par l'API ; seule la classe de l'erreur est gardée.
            if state == "failed":
                await announce(conn, incident_id, "incident.analysis_failed")
                logger.error(
                    "Analyse de l'incident %s en échec après %d essais (%s).",
                    incident_id,
                    MAX_ATTEMPTS,
                    type(error).__name__,
                )
                return Processed(incident_id, "failed")
            logger.warning(
                "Analyse de l'incident %s remise à plus tard (%s).",
                incident_id,
                type(error).__name__,
            )
            return Processed(incident_id, "postponed")
        # Le délai posé pour les lectures de l'analyse ne vaut pas pour sa validation.
        await conn.execute(_NO_STATEMENT_TIMEOUT)
        await announce(conn, incident_id, "incident.analysed")
    logger.info("Incident %s analysé.", incident_id)
    return Processed(incident_id, "done")
