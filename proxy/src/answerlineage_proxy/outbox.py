"""Boîte d'envoi du proxy : ses événements et ses relevés partent vers l'API d'ingestion,
hors du chemin des appels.

La boîte est celle de tout émetteur (``answerlineage_emitter.outbox``) : prise
en charge en temps constant, sérialisation et envoi par lots compressés dans
des tâches de fond, bornes en nombre et en octets, reprises espacées, vidage
borné à l'arrêt. Le proxy lui donne son journal, et compte chaque perte qu'elle
lui dit dans la fenêtre de couverture de l'événement.
"""

from __future__ import annotations

from answerlineage_emitter.outbox import (
    BACKOFF_BASE,
    BACKOFF_CAP,
    BATCH_BYTES,
    BATCH_EVENTS,
    DEFAULT_FLUSH_SECONDS,
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_EVENTS,
    FLUSH_SECONDS_LIMITS,
    MAX_BYTES_LIMITS,
    MAX_EVENTS_LIMITS,
    MAX_REPORTS,
    RETRY_AFTER_CAP,
    Outbox,
    OutboxSettings,
    OutboxStats,
    retry_delay,
)

__all__ = [
    "BACKOFF_BASE",
    "BACKOFF_CAP",
    "BATCH_BYTES",
    "BATCH_EVENTS",
    "DEFAULT_FLUSH_SECONDS",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_EVENTS",
    "FLUSH_SECONDS_LIMITS",
    "MAX_BYTES_LIMITS",
    "MAX_EVENTS_LIMITS",
    "MAX_REPORTS",
    "RETRY_AFTER_CAP",
    "Outbox",
    "OutboxSettings",
    "OutboxStats",
    "retry_delay",
]
