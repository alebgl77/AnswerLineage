"""Destination des événements du proxy, et de ses relevés de couverture.

``emit`` et ``report_coverage`` ne bloquent jamais : la capture ne doit ni
retarder ni interrompre un appel d'agent. L'envoi asynchrone vers l'API
d'ingestion n'est pas encore disponible.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from typing import Any, Protocol


class EventSink(Protocol):
    def emit(self, event: Mapping[str, Any]) -> None:
        """Prend en charge un événement, sans bloquer."""

    def report_coverage(self, report: Mapping[str, Any]) -> None:
        """Prend en charge le relevé de couverture d'une fenêtre close, sans bloquer."""


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
