"""Destination des événements du proxy, et de ses relevés de couverture.

Les destinations sont celles de tout émetteur (``answerlineage_emitter.sink``) :
``emit`` et ``report_coverage`` ne bloquent jamais. Une destination à remise
différée, comme la boîte d'envoi, dit plus tard si chaque événement a été remis
ou perdu, et le proxy le compte dans la fenêtre de couverture de son heure.
"""

from __future__ import annotations

from answerlineage_emitter.sink import (
    DeferredSink,
    EventSink,
    MemorySink,
    NullSink,
    Settled,
    receipt,
)

__all__ = ["DeferredSink", "EventSink", "MemorySink", "NullSink", "Settled", "receipt"]
