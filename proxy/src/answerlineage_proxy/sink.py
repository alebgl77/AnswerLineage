"""Destination des événements du proxy.

``emit`` ne bloque jamais et ne lève jamais : la capture ne doit ni retarder
ni interrompre un appel d'agent (ADR-005). L'envoi asynchrone vers l'API
d'ingestion arrive avec la boîte d'envoi (US-0406).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from typing import Any, Protocol


class EventSink(Protocol):
    def emit(self, event: Mapping[str, Any]) -> None:
        """Prend en charge un événement, sans bloquer."""


class NullSink:
    """Compte les événements sans les conserver."""

    def __init__(self) -> None:
        self.discarded = 0

    def emit(self, event: Mapping[str, Any]) -> None:
        self.discarded += 1


class MemorySink:
    """File bornée en mémoire : pleine, elle abandonne l'événement le plus ancien."""

    def __init__(self, capacity: int = 10_000) -> None:
        if capacity < 1:
            raise ValueError("La capacité doit être d'au moins un événement.")
        self._events: deque[Mapping[str, Any]] = deque(maxlen=capacity)
        self.dropped = 0

    def emit(self, event: Mapping[str, Any]) -> None:
        if len(self._events) == self._events.maxlen:
            self.dropped += 1
        self._events.append(event)

    @property
    def events(self) -> list[Mapping[str, Any]]:
        return list(self._events)

    def drain(self) -> list[Mapping[str, Any]]:
        """Renvoie les événements en attente et vide la file."""
        events = list(self._events)
        self._events.clear()
        return events
