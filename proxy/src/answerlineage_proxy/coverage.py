"""Relevés de couverture du proxy : ses compteurs, fenêtre par fenêtre.

Le proxy compte, par fenêtre de temps (une minute par défaut), les appels
d'outils qu'il a vus, les événements qu'il a remis à sa destination et ceux
qu'il a perdus, et les requêtes qu'il a relayées sans pouvoir les inspecter.
Chaque fenêtre close est remise à la destination des événements, au format du
relevé de l'API d'ingestion (``POST /api/v1/coverage``), y compris une fenêtre
sans appel : c'est le battement qui prouve que le proxy n'est pas muet.

Un appel compte dans la fenêtre où tombe l'heure de son événement. Son
événement peut partir après la fin de la fenêtre, une fois le SQL analysé :
la fenêtre ne se clôt qu'une fois tous ses appels réglés. Rien de tout cela
ne retarde un appel : compter est une addition, et la remise se fait hors du
chemin des appels.

Une destination à remise différée (la boîte d'envoi) prend un événement en
charge avant de l'avoir remis : l'appel compte aussitôt parmi les appels vus,
et l'événement parmi les émis une fois remis, parmi les perdus s'il se perd.
La fenêtre se clôt sans attendre ces issues ; une issue qui arrive après la
clôture rend un relevé corrigé de la même fenêtre, dont les compteurs ne font
que grandir : l'API garde le plus grand de chacun.

Les fenêtres sont alignées sur des multiples de leur durée depuis l'origine
des temps Unix : deux proxys découpent le temps de la même façon. La première
commence au démarrage du proxy, la dernière s'achève à son arrêt : deux vies
successives du proxy publient des fenêtres disjointes.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

logger = logging.getLogger("answerlineage_proxy")

DEFAULT_WINDOW: Final = timedelta(minutes=1)
# Une seconde au moins ; un jour au plus, la plus longue fenêtre que l'API admet.
MIN_WINDOW: Final = timedelta(seconds=1)
MAX_WINDOW: Final = timedelta(days=1)
# Une fenêtre échue mais dont un appel n'est pas encore réglé est revue peu après.
_RETRY_SECONDS: Final = 0.1
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

Report = dict[str, Any]


def _instant(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass
class _Counts:
    calls_seen: int = 0
    events_emitted: int = 0
    events_lost: int = 0
    uninspected: int = 0
    # Appels comptés dans la fenêtre dont l'événement n'est pas encore parti.
    pending: int = 0
    # Événements pris en charge par une destination à remise différée, ni remis ni perdus.
    in_flight: int = 0


class CoverageWindows:
    """Les compteurs d'un émetteur, fenêtre par fenêtre, et leur clôture."""

    def __init__(self, emitter: str, *, length: timedelta, started_at: datetime) -> None:
        if not MIN_WINDOW <= length <= MAX_WINDOW:
            raise ValueError("La fenêtre dure d'une seconde à un jour.")
        self.emitter = emitter
        self.length = length
        # Début de la plus ancienne fenêtre pas encore remise.
        self._open = started_at
        self._counts: dict[datetime, _Counts] = {}
        self._closed = False
        # Fenêtres closes dont des événements sont encore en route : leur fin et leurs compteurs.
        self._settling: dict[datetime, tuple[datetime, _Counts]] = {}
        # Fenêtres réunies dans la dernière à l'arrêt : le début de celle-ci.
        self._merged: dict[datetime, datetime] = {}
        # Fenêtre de chaque événement en route, par reçu, dans l'ordre de prise en charge.
        self._flight: dict[str, deque[datetime]] = {}

    def _end_of(self, start: datetime) -> datetime:
        """Fin de la fenêtre qui commence à ``start`` : la frontière alignée suivante."""
        return _EPOCH + ((start - _EPOCH) // self.length + 1) * self.length

    def _start_of(self, moment: datetime) -> datetime:
        """Le début de la fenêtre de cet instant. Un instant antérieur aux fenêtres ouvertes,
        horloge reculée, compte dans la plus ancienne : un appel n'est jamais perdu."""
        if moment < self._end_of(self._open):
            return self._open
        return _EPOCH + ((moment - _EPOCH) // self.length) * self.length

    def _window(self, moment: datetime) -> _Counts:
        return self._counts.setdefault(self._start_of(moment), _Counts())

    def begin(self, moment: datetime) -> None:
        """Un appel dont l'événement porte l'heure ``moment`` va partir."""
        self._window(moment).pending += 1

    def settle(self, moment: datetime, *, emitted: bool) -> None:
        """L'événement de cet appel est parti, ou a été perdu."""
        counts = self._window(moment)
        counts.pending = max(0, counts.pending - 1)
        counts.calls_seen += 1
        if emitted:
            counts.events_emitted += 1
        else:
            counts.events_lost += 1

    def hand_off(self, moment: datetime, receipt: str) -> None:
        """L'événement de cet appel est pris en charge par une destination à remise différée,
        qui dira plus tard, par son reçu, s'il a été remis ou perdu."""
        start = self._start_of(moment)
        counts = self._counts.setdefault(start, _Counts())
        counts.pending = max(0, counts.pending - 1)
        counts.calls_seen += 1
        counts.in_flight += 1
        self._flight.setdefault(receipt, deque()).append(start)

    def outcome(self, receipt: str, *, delivered: bool) -> Report | None:
        """L'événement de ce reçu a été remis, ou perdu. Rend le relevé corrigé de sa
        fenêtre quand elle est déjà close, sinon rien : son relevé le dira à la clôture."""
        starts = self._flight.get(receipt)
        if not starts:
            return None
        start = starts.popleft()
        if not starts:
            del self._flight[receipt]
        start = self._merged.get(start, start)
        counts = self._counts.get(start)
        end: datetime | None = None
        if counts is None:
            settling = self._settling.get(start)
            if settling is None:  # pragma: no cover — tout reçu en route a sa fenêtre.
                return None
            end, counts = settling
        counts.in_flight = max(0, counts.in_flight - 1)
        if delivered:
            counts.events_emitted += 1
        else:
            counts.events_lost += 1
        if end is None:
            return None
        if not counts.in_flight:
            del self._settling[start]
        return self._report(start, end, counts)

    def uninspected(self, moment: datetime) -> None:
        """Une requête relayée sans pouvoir être inspectée : elle cache peut-être un appel."""
        self._window(moment).uninspected += 1

    def _report(self, start: datetime, end: datetime, counts: _Counts | None) -> Report:
        counts = counts or _Counts()
        return {
            "emitter": self.emitter,
            "window": {"from": _instant(start), "to": _instant(end)},
            "calls_seen": counts.calls_seen,
            "events_emitted": counts.events_emitted,
            "events_lost": counts.events_lost,
            "uninspected": counts.uninspected,
        }

    def close_due(self, now: datetime) -> list[Report]:
        """Les relevés des fenêtres échues à ``now``, dans l'ordre, fenêtres vides comprises.

        Une fenêtre dont un appel n'est pas encore réglé reste ouverte, et celles
        qui la suivent aussi : les relevés partent dans l'ordre du temps.
        """
        reports: list[Report] = []
        while not self._closed:
            end = self._end_of(self._open)
            counts = self._counts.get(self._open)
            if end > now or (counts is not None and counts.pending):
                break
            self._counts.pop(self._open, None)
            reports.append(self._report(self._open, end, counts))
            if counts is not None and counts.in_flight:
                self._settling[self._open] = (end, counts)
            self._open = end
        return reports

    def close_all(self, now: datetime) -> list[Report]:
        """À l'arrêt : les fenêtres échues, puis la fenêtre en cours, achevée à ``now``."""
        if self._closed:
            return []
        reports = self.close_due(now)
        current = [start for start in self._counts if start >= self._open]
        if now > self._open or current:
            # La fenêtre s'achève à l'arrêt ; au-delà seulement si l'horloge a compté plus loin.
            end = max([now, *(self._end_of(start) for start in current if start >= now)])
            counts = _Counts()
            for start in current:
                found = self._counts.pop(start)
                # Un appel encore en attente à l'arrêt n'a pas d'événement : il est perdu.
                counts.calls_seen += found.calls_seen + found.pending
                counts.events_emitted += found.events_emitted
                counts.events_lost += found.events_lost + found.pending
                counts.uninspected += found.uninspected
                counts.in_flight += found.in_flight
                if start != self._open:
                    self._merged[start] = self._open
            reports.append(self._report(self._open, end, counts))
            if counts.in_flight:
                self._settling[self._open] = (end, counts)
        self._closed = True
        return reports

    def seconds_until_due(self, now: datetime) -> float:
        """Attente avant la prochaine clôture possible."""
        remaining = (self._end_of(self._open) - now).total_seconds()
        return remaining if remaining > 0 else _RETRY_SECONDS


Sleep = Callable[[float], Awaitable[Any]]


async def report_periodically(
    windows: CoverageWindows,
    clock: Callable[[], datetime],
    deliver: Callable[[list[Report]], None],
    *,
    sleep: Sleep = asyncio.sleep,
) -> None:
    """Remet chaque fenêtre close dès qu'elle l'est, jusqu'à être annulé."""
    while True:
        now = clock()
        deliver(windows.close_due(now))
        await sleep(windows.seconds_until_due(now))
