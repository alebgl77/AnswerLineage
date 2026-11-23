"""Webhooks signés : chaque changement d'état d'un incident, annoncé au récepteur.

Un changement d'état met son message dans la file des webhooks
(``webhook_queue``), dans la transaction qui le fait, quand un webhook est en
service (``answerlineage_api.incidents.webhooks``). La boucle des webhooks tourne
à côté de la projection, dans chaque worker qui a un webhook, sans la retarder :

- **en service.** À son démarrage, puis à chaque passage de la borne, la boucle
  repousse ``webhook_stats.in_service_until`` de la borne d'âge de la file : tant
  qu'elle n'est pas passée, un changement d'état est mis en file. Un worker sans
  webhook n'y touche pas ; un worker à webhook arrêté quelques heures ne fait
  rien perdre, la file attend son retour ;
- **un lot à la fois.** Elle réclame un lot (``FOR UPDATE SKIP LOCKED`` : deux
  workers ne réclament jamais le même message), l'envoie message par message,
  puis le retire de la file avec ses compteurs, dans une seule transaction ;
- **ce qui part** est un message à gabarit fixe (``message``) : la version du
  format, l'identifiant de livraison, l'heure d'émission, la sorte d'événement
  et son heure, l'incident par son identifiant, son état, sa gravité, l'état
  de son analyse avec ses décomptes, et sa quarantaine, son état et son mode.
  Aucun texte libre, aucun pseudonyme, aucun nom de jeu de données ;
- **signé** sous le secret partagé, et le précédent pendant une rotation, avec
  l'heure d'émission (``webhook_signature.py``) ;
- **au moins une fois.** Un envoi dont l'issue n'est pas sûre est refait, avec
  le même identifiant de livraison, une nouvelle heure d'émission et une
  nouvelle signature ; un récepteur qui refuse le message (``400``, ``413``,
  ``422``) ne l'acceptera pas mieux plus tard : il est abandonné, compté perdu ;
- **une panne** arrête le lot au premier échec ; la reprise attend de plus en
  plus, jusqu'à un plafond, chaque attente tirée au hasard (``retry_delay``) ;
- **une file bornée**, en âge et en nombre : ce qui déborde, les plus anciens
  d'abord, est retiré, compté perdu, et dit au journal.

La destination et son client sont ceux de la réémission (``destination.py``) :
mêmes règles d'adresse, TLS vérifié, aucune redirection suivie, délais bornés.
Le journal ne dit que des nombres, des statuts et des classes d'erreur : ni
l'adresse du récepteur, ni le secret, ni rien d'un message.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import Counter
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from random import Random
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_workers.destination import Destination, Sender
from answerlineage_workers.reemission import duration, retry_delay
from answerlineage_workers.webhook_signature import HEADER, signature_header

logger = logging.getLogger("answerlineage_workers")

URL_VARIABLE: Final = "ANSWERLINEAGE_WEBHOOK_URL"
# Les noms des variables, pas des secrets.
SECRET_VARIABLE: Final = "ANSWERLINEAGE_WEBHOOK_SECRET"  # noqa: S105
PREVIOUS_SECRET_VARIABLE: Final = "ANSWERLINEAGE_WEBHOOK_PREVIOUS_SECRET"  # noqa: S105
# La version du format des messages : celle de ``spec/webhooks/v1/``.
FORMAT_VERSION: Final = 1
# Un secret partagé : de l'ASCII imprimable sans espace, d'au moins 32 caractères.
MIN_SECRET_LENGTH: Final = 32
MAX_SECRET_LENGTH: Final = 512
DEFAULT_MAX_MESSAGES: Final = 10_000
MAX_MAX_MESSAGES: Final = 1_000_000
DEFAULT_MAX_AGE: Final = timedelta(days=7)
MAX_MAX_AGE: Final = timedelta(days=90)
DEFAULT_BATCH_SIZE: Final = 100
MAX_BATCH_SIZE: Final = 1_000
# Envoi d'un lot : passé ce temps, ce qui reste attend le lot suivant.
BATCH_SECONDS: Final = 30.0
# Reprises : d'une seconde, doublées à chaque échec, jusqu'à cinq minutes.
BACKOFF_BASE: Final = 1.0
BACKOFF_CAP: Final = 300.0
# La borne de la file et la mise en service sont tenues à ce délai près.
TRIM_EVERY: Final = timedelta(seconds=10)
TRIM_CHUNK: Final = 10_000
TRIM_CHUNKS: Final = 10

_SECRET = re.compile(rf"[\x21-\x7e]{{{MIN_SECRET_LENGTH},{MAX_SECRET_LENGTH}}}")

_CLAIM = text(
    """
    SELECT id, delivery_id, event_type, incident_id, occurred_at, incident_state, severity,
           analysis_state, analysis_error, answers, recipients, lower_bound,
           quarantine_state, quarantine_mode
    FROM webhook_queue
    ORDER BY id
    LIMIT :limit
    FOR UPDATE SKIP LOCKED
    """
)
_SETTLED = text("DELETE FROM webhook_queue WHERE id = ANY(:ids)")
_FAILED = text(
    "UPDATE webhook_queue SET attempts = attempts + 1, last_error = :error WHERE id = :id"
)
_COUNT = text(
    """
    INSERT INTO webhook_stats (id, sent, rejected, dropped)
    VALUES (true, :sent, :rejected, :dropped)
    ON CONFLICT (id) DO UPDATE SET
        sent = webhook_stats.sent + EXCLUDED.sent,
        rejected = webhook_stats.rejected + EXCLUDED.rejected,
        dropped = webhook_stats.dropped + EXCLUDED.dropped
    """
)
_IN_SERVICE = text(
    """
    INSERT INTO webhook_stats (id, in_service_until)
    VALUES (true, now() + make_interval(secs => :max_age))
    ON CONFLICT (id) DO UPDATE SET in_service_until = EXCLUDED.in_service_until
    """
)
# Un message en cours d'envoi est verrouillé : la borne le saute, il part ou reste.
_TOO_OLD = text(
    """
    WITH doomed AS (
        SELECT id FROM webhook_queue
        WHERE enqueued_at < now() - make_interval(secs => :max_age)
        ORDER BY enqueued_at
        LIMIT :chunk
        FOR UPDATE SKIP LOCKED
    )
    DELETE FROM webhook_queue AS q USING doomed WHERE q.id = doomed.id
    """
)
_BEYOND = text("SELECT id FROM webhook_queue ORDER BY id DESC OFFSET :max_messages LIMIT 1")
_TOO_MANY = text(
    """
    WITH doomed AS (
        SELECT id FROM webhook_queue WHERE id <= :beyond
        ORDER BY id
        LIMIT :chunk
        FOR UPDATE SKIP LOCKED
    )
    DELETE FROM webhook_queue AS q USING doomed WHERE q.id = doomed.id
    """
)


class WebhookSecretError(ValueError):
    """Un secret refusé. Le message nomme la variable, jamais sa valeur."""


def checked_secret(value: str, variable: str) -> str:
    """Le secret, s'il est de l'ASCII imprimable sans espace, de 32 à 512 caractères."""
    if not _SECRET.fullmatch(value):
        raise WebhookSecretError(
            f"{variable} doit être un texte ASCII imprimable, sans espace, de"
            f" {MIN_SECRET_LENGTH} à {MAX_SECRET_LENGTH} caractères."
        )
    return value


@dataclass(frozen=True)
class Webhook:
    """Le récepteur des webhooks, ses secrets et les bornes de sa file."""

    destination: Destination
    # Jamais affichés : ni par ``repr``, ni dans un message.
    secret: str = field(repr=False)
    # Le secret précédent, pendant une rotation : chaque message est signé sous les deux.
    previous_secret: str | None = field(default=None, repr=False)
    max_messages: int = DEFAULT_MAX_MESSAGES
    max_age: timedelta = DEFAULT_MAX_AGE
    batch_size: int = DEFAULT_BATCH_SIZE

    def __post_init__(self) -> None:
        checked_secret(self.secret, SECRET_VARIABLE)
        if self.previous_secret is not None:
            checked_secret(self.previous_secret, PREVIOUS_SECRET_VARIABLE)
        if not 1 <= self.max_messages <= MAX_MAX_MESSAGES:
            raise ValueError(f"La file des webhooks garde de 1 à {MAX_MAX_MESSAGES} messages.")
        if not timedelta(seconds=1) <= self.max_age <= MAX_MAX_AGE:
            raise ValueError(
                f"La file des webhooks garde un message d'une seconde à {MAX_MAX_AGE.days} jours."
            )
        if not 1 <= self.batch_size <= MAX_BATCH_SIZE:
            raise ValueError(f"Un lot de webhooks compte de 1 à {MAX_BATCH_SIZE} messages.")

    @property
    def secrets(self) -> tuple[str, ...]:
        """Les secrets sous lesquels signer : le secret, puis le précédent s'il y en a un."""
        if self.previous_secret is None:
            return (self.secret,)
        return (self.secret, self.previous_secret)


def _instant(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _second(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def message(row: Mapping[str, Any], *, emitted_at: int) -> dict[str, Any]:
    """Le message d'une ligne de la file, émis à ``emitted_at`` (secondes depuis 1970).

    Un gabarit fixe : chaque valeur est un identifiant, un code d'une liste
    fermée, une heure ou un nombre, tels que la file les tient.
    """
    done = row["analysis_state"] == "done"
    counts = (
        {
            "answers": row["answers"],
            "recipients": row["recipients"],
            "lower_bound": row["lower_bound"],
        }
        if done
        else None
    )
    quarantine = (
        None
        if row["quarantine_state"] is None
        else {"state": row["quarantine_state"], "mode": row["quarantine_mode"]}
    )
    return {
        "version": FORMAT_VERSION,
        "delivery_id": str(row["delivery_id"]),
        "emitted_at": _second(emitted_at),
        "type": row["event_type"],
        "occurred_at": _instant(row["occurred_at"]),
        "incident": {
            "id": str(row["incident_id"]),
            "state": row["incident_state"],
            "severity": row["severity"],
            "analysis": {
                "state": row["analysis_state"],
                "error": row["analysis_error"],
                "counts": counts,
            },
            "quarantine": quarantine,
        },
    }


def encode(document: Mapping[str, Any]) -> bytes:
    """Le corps envoyé : le message en JSON compact, clés triées, en ASCII."""
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("ascii")


@dataclass(frozen=True)
class WebhookResult:
    """Ce qu'un lot a fait : messages remis, refusés, et l'échec qui l'a arrêté."""

    sent: int = 0
    rejected: int = 0
    # Statut ou classe de l'échec qui a arrêté le lot : jamais un message.
    failure: str | None = None

    @property
    def empty(self) -> bool:
        return not (self.sent or self.rejected) and self.failure is None


async def deliver_batch(
    engine: AsyncEngine,
    sender: Sender,
    webhook: Webhook,
    *,
    stop: asyncio.Event | None = None,
    budget: float = BATCH_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    wall: Callable[[], float] = time.time,
) -> WebhookResult:
    """Réclame un lot, l'envoie signé et le retire de la file, dans une transaction.

    Chaque envoi est signé à son heure d'émission, lue sur ``wall``. Le lot
    s'arrête au premier échec : le message en cause garde son essai et la
    classe de l'échec, les suivants attendent sans essai compté. Il s'arrête
    aussi à ``stop`` et passé ``budget`` secondes.
    """
    sent = rejected = 0
    failure: str | None = None
    refusals: Counter[str] = Counter()
    async with engine.begin() as conn:
        rows = (await conn.execute(_CLAIM, {"limit": webhook.batch_size})).mappings().all()
        settled: list[int] = []
        ends = clock() + budget
        for row in rows:
            if (stop is not None and stop.is_set()) or clock() >= ends:
                break
            emitted_at = int(wall())
            body = encode(message(dict(row), emitted_at=emitted_at))
            signature = signature_header(webhook.secrets, emitted_at, body)
            delivery = await sender.send(body, stop, headers={HEADER: signature})
            if delivery.verdict == "interrupted":
                break
            if delivery.verdict == "failed":
                failure = delivery.reason
                await conn.execute(_FAILED, {"id": row["id"], "error": delivery.reason})
                break
            settled.append(row["id"])
            if delivery.verdict == "sent":
                sent += 1
            else:
                rejected += 1
                refusals[delivery.reason] += 1
        if settled:
            await conn.execute(_SETTLED, {"ids": settled})
        if sent or rejected:
            await conn.execute(_COUNT, {"sent": sent, "rejected": rejected, "dropped": 0})
    for reason, count in sorted(refusals.items()):
        logger.warning(
            "Webhook : %d message(s) refusé(s) par le récepteur (%s) : abandonnés, comptés perdus.",
            count,
            reason,
        )
    return WebhookResult(sent=sent, rejected=rejected, failure=failure)


async def keep_in_service(engine: AsyncEngine, webhook: Webhook) -> None:
    """Dit le webhook en service pour la borne d'âge de la file, à partir de maintenant."""
    async with engine.begin() as conn:
        await conn.execute(_IN_SERVICE, {"max_age": webhook.max_age.total_seconds()})


async def trim(engine: AsyncEngine, webhook: Webhook) -> int:
    """Retire de la file ce qui dépasse ses bornes, les plus anciens d'abord ; rend leur nombre.

    Les messages retirés sont comptés perdus dans la transaction qui les retire.
    """
    dropped = 0
    for statement in (_TOO_OLD, _TOO_MANY):
        for _ in range(TRIM_CHUNKS):
            async with engine.begin() as conn:
                params: dict[str, Any] = {"chunk": TRIM_CHUNK}
                if statement is _TOO_OLD:
                    params["max_age"] = webhook.max_age.total_seconds()
                else:
                    beyond = (
                        await conn.execute(_BEYOND, {"max_messages": webhook.max_messages})
                    ).scalar_one_or_none()
                    if beyond is None:
                        break
                    params["beyond"] = beyond
                removed = (await conn.execute(statement, params)).rowcount
                if removed:
                    await conn.execute(_COUNT, {"sent": 0, "rejected": 0, "dropped": removed})
            dropped += removed
            if removed < TRIM_CHUNK:
                break
    if dropped:
        logger.warning(
            "Webhook : %d message(s) abandonné(s), les plus anciens, au-delà des bornes de la"
            " file (%d messages, %s) : comptés perdus.",
            dropped,
            webhook.max_messages,
            duration(webhook.max_age),
        )
    return dropped


async def run_webhooks(
    engine: AsyncEngine,
    stop: asyncio.Event,
    webhook: Webhook,
    *,
    sender: Sender | None = None,
    idle_delay: float = 1.0,
    error_delay: float = 5.0,
    backoff: tuple[float, float] = (BACKOFF_BASE, BACKOFF_CAP),
    random: Random | None = None,
    trim_every: timedelta = TRIM_EVERY,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Dit le webhook en service et vide sa file vers le récepteur, jusqu'à ``stop``.

    Rien n'y arrête la boucle : une erreur de la base ou du récepteur est
    journalisée par sa classe, et reprise plus tard.
    """
    sender = sender or Sender(webhook.destination)
    random = random or Random()  # noqa: S311 — une gigue, pas un secret.
    failures = 0
    next_trim = clock()
    logger.info(
        "Webhook en marche : destination en %s, messages signés sous %d secret(s), file bornée"
        " à %d messages et %s.",
        webhook.destination.scheme,
        len(webhook.secrets),
        webhook.max_messages,
        duration(webhook.max_age),
    )
    try:
        while not stop.is_set():
            if clock() >= next_trim:
                next_trim = clock() + trim_every.total_seconds()
                try:
                    await keep_in_service(engine, webhook)
                    await trim(engine, webhook)
                except Exception as error:
                    logger.error(
                        "Mise en service ou bornes de la file des webhooks non tenues (%s),"
                        " nouvel essai à suivre.",
                        type(error).__name__,
                    )
            delay = 0.0
            try:
                result = await deliver_batch(engine, sender, webhook, stop=stop)
            except Exception as error:
                # Base injoignable : le lot est resté dans la file, sans essai compté.
                logger.error(
                    "Lot de webhooks non traité (%s), nouvel essai à suivre.",
                    type(error).__name__,
                )
                delay = error_delay
            else:
                if result.failure is not None:
                    failures += 1
                    delay = retry_delay(failures, backoff, random)
                    if failures == 1:
                        logger.warning(
                            "Webhook en échec (%s) : les messages attendent dans la file ;"
                            " reprise dans %.1f s, puis de plus en plus espacée.",
                            result.failure,
                            delay,
                        )
                else:
                    if failures:
                        logger.info(
                            "Webhook rétabli après %d échec(s) : le récepteur accepte de"
                            " nouveau les messages.",
                            failures,
                        )
                        failures = 0
                    if result.empty:
                        delay = idle_delay
            if delay:
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=delay)
    finally:
        sender.close()
        logger.info("Webhook arrêté.")
