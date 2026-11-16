"""Destination des événements du proxy, et de ses relevés de couverture.

``emit`` et ``report_coverage`` ne bloquent jamais : la capture ne doit ni
retarder ni interrompre un appel d'agent.

Une destination à remise différée (``DeferredSink``), comme la boîte d'envoi
vers l'API d'ingestion (``outbox.py``), prend un événement en charge sans
l'avoir encore remis. Elle dit plus tard, une fois et une seule par événement,
s'il a été remis ou perdu : le transport qui l'emploie le compte alors dans la
fenêtre de couverture de son heure, même si cette fenêtre est déjà close.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from typing import Any, Protocol, runtime_checkable

# Ce que dit une destination à remise différée d'un événement pris en charge : son
# reçu, puis vrai s'il a été remis, faux s'il a été perdu.
Settled = Callable[[str, bool], None]


def receipt(event: Mapping[str, Any]) -> str:
    """Le reçu d'un événement : son heure, telle qu'il la porte.

    Le transport et la destination le tirent de l'événement de la même façon :
    il suffit au transport pour retrouver la fenêtre où il a compté l'appel.
    """
    moment = event.get("eventTime")
    return moment if isinstance(moment, str) else ""


class EventSink(Protocol):
    def emit(self, event: Mapping[str, Any]) -> None:
        """Prend en charge un événement, sans bloquer."""

    def report_coverage(self, report: Mapping[str, Any]) -> None:
        """Prend en charge le relevé de couverture d'une fenêtre close, sans bloquer."""


@runtime_checkable
class DeferredSink(EventSink, Protocol):
    """Destination qui remet ses événements plus tard, et dit ce qu'il en est advenu."""

    def watch(self, settled: Settled) -> None:
        """Désigne qui apprend, pour chaque événement pris en charge, s'il a été remis ou perdu."""

    async def start(self) -> None:
        """Démarre la remise, dans la boucle qui sert le transport."""

    async def aclose(self) -> None:
        """Remet ce qui peut l'être dans le délai d'arrêt ; le reste est perdu, et dit."""


class NullSink:
    """Compte les événements sans les conserver."""

    def __init__(self) -> None:
        self.discarded = 0
        self.discarded_reports = 0

    def emit(self, event: Mapping[str, Any]) -> None:
        self.discarded += 1

    def report_coverage(self, report: Mapping[str, Any]) -> None:
        self.discarded_reports += 1


class MemorySink:
    """File bornée en mémoire : pleine, elle abandonne l'événement le plus ancien."""

    def __init__(self, capacity: int = 10_000) -> None:
        if capacity < 1:
            raise ValueError("La capacité doit être d'au moins un événement.")
        self._events: deque[Mapping[str, Any]] = deque(maxlen=capacity)
        self._reports: deque[Mapping[str, Any]] = deque(maxlen=capacity)
        self.dropped = 0

    def emit(self, event: Mapping[str, Any]) -> None:
        if len(self._events) == self._events.maxlen:
            self.dropped += 1
        self._events.append(event)

    def report_coverage(self, report: Mapping[str, Any]) -> None:
        self._reports.append(report)

    @property
    def events(self) -> list[Mapping[str, Any]]:
        return list(self._events)

    @property
    def reports(self) -> list[Mapping[str, Any]]:
        """Relevés de couverture remis, les plus anciens d'abord ; pleine, la file oublie les
        plus anciens."""
        return list(self._reports)

    def drain(self) -> list[Mapping[str, Any]]:
        """Renvoie les événements en attente et vide la file."""
        events = list(self._events)
        self._events.clear()
        return events
