"""Couverture du lignage : la part des appels relayés qui sont présents dans le graphe.

Un émetteur publie ses relevés, fenêtre par fenêtre (``ingest/coverage.py``).
La couverture compare ses compteurs au graphe : pour chaque fenêtre d'un
relevé, ses événements d'appel d'outil reçus et déjà projetés, dont l'heure
tombe dans la fenêtre, rapportés aux appels qu'il déclare avoir vus. Un
événement perdu en route se voit ainsi, même si l'émetteur croit l'avoir émis.
On compte des événements, pas des runs : deux appels peuvent partager un run.

Ce que la mesure ignore est dit, jamais tu. Les trous connus d'une période :

- les fenêtres où l'émetteur déclare des événements perdus ;
- celles où il déclare des requêtes non inspectées : elles cachaient peut-être
  des appels, en nombre inconnu, que le taux ne peut donc pas compter ;
- celles où le graphe contient moins d'événements que d'appels vus ;
- les intervalles sans relevé, entre deux relevés d'un émetteur connu ou après
  son dernier (silence), et le début d'une période qui précède le premier
  relevé d'un émetteur.

Une période sans relevé n'est jamais présentée comme couverte : la mesure est
« measured » quand des relevés la couvrent tout entière, « partial » quand ils
n'en couvrent qu'une part, « unknown » quand aucun ne la touche. La partie de
la période qui n'est pas encore passée n'est pas mesurable : elle n'est ni
couverte ni un trou. Un relevé qui déborde de la période compte en entier.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text

from answerlineage_api.lineage.models import (
    Coverage,
    CoverageHole,
    CoverageStatus,
    EmitterCoverage,
    HoleKind,
    Window,
)
from answerlineage_api.lineage.reading import Reader

# Émetteurs pris en compte pour une période, relevés lus, trous rendus.
MAX_EMITTERS = 1_000
MAX_REPORTS = 50_000
MAX_HOLES = 1_000

_EMITTERS = text(
    """
    SELECT emitter, last_report_at, silent_since FROM coverage_emitter
    ORDER BY emitter
    LIMIT :limit
    """
)

# Les émetteurs connus avant la fin d'une période : un relevé commence avant elle.
_KNOWN_BY = text(
    """
    SELECT e.emitter, e.last_report_at, e.silent_since FROM coverage_emitter e
    WHERE EXISTS (
        SELECT 1 FROM coverage_report r WHERE r.emitter = e.emitter AND r.window_start < :end
    )
    ORDER BY e.emitter
    LIMIT :limit
    """
)

# Les émetteurs qui ont un relevé achevé avant le début de la période : un
# intervalle sans relevé, au début de la période, est pour eux un silence.
_KNOWN_BEFORE = text(
    """
    SELECT emitter FROM unnest(CAST(:emitters AS text[])) AS known(emitter)
    WHERE EXISTS (
        SELECT 1 FROM coverage_report r
        WHERE r.emitter = known.emitter AND r.window_start < :start AND r.window_end <= :start
    )
    """
)

# Les relevés qui recoupent la période, et pour chacun ses événements d'appel
# reçus et déjà projetés (sortis de la file), comptés au plus jusqu'aux appels vus.
_REPORTS = text(
    """
    SELECT r.emitter, r.window_start, r.window_end, r.calls_seen, r.events_emitted,
           r.events_lost, r.uninspected,
           (SELECT count(*) FROM (
                SELECT 1 FROM event_raw e
                WHERE e.tool_call AND e.emitter = r.emitter
                  AND e.event_time >= r.window_start AND e.event_time < r.window_end
                  AND NOT EXISTS (SELECT 1 FROM ingest_queue q WHERE q.event_id = e.event_id)
                LIMIT r.calls_seen
           ) AS projected) AS in_graph
    FROM coverage_report r
    WHERE r.emitter = ANY(CAST(:emitters AS text[]))
      AND r.window_start < :end AND r.window_end > :start
    ORDER BY r.emitter, r.window_start, r.window_end
    LIMIT :limit
    """
)


@dataclass(frozen=True)
class Report:
    """Un relevé et ce que le graphe en contient."""

    emitter: str
    start: datetime
    end: datetime
    calls_seen: int
    events_emitted: int
    events_lost: int
    uninspected: int
    in_graph: int

    @property
    def missing(self) -> int:
        return self.calls_seen - min(self.in_graph, self.calls_seen)


@dataclass
class _Tally:
    calls_seen: int = 0
    events_in_graph: int = 0
    events_lost: int = 0
    uninspected: int = 0
    holes: list[CoverageHole] = field(default_factory=list)

    def add(self, report: Report) -> None:
        self.calls_seen += report.calls_seen
        self.events_in_graph += report.calls_seen - report.missing
        self.events_lost += report.events_lost
        self.uninspected += report.uninspected


def _hole(
    emitter: str, kind: HoleKind, start: datetime, end: datetime, count: int | None
) -> CoverageHole:
    return CoverageHole(
        emitter=emitter, kind=kind, window=Window(start=start, end=end), count=count
    )


def _merged(holes: Iterable[CoverageHole]) -> list[CoverageHole]:
    """Les trous d'un émetteur, ceux d'une même nature qui se suivent réunis en un seul."""
    merged: list[CoverageHole] = []
    last: dict[HoleKind, CoverageHole] = {}
    for hole in sorted(holes, key=lambda item: (item.window.start, item.window.end)):
        previous = last.get(hole.kind)
        if previous is not None and previous.window.end == hole.window.start:
            previous.window.end = hole.window.end
            if previous.count is not None and hole.count is not None:
                previous.count += hole.count
            continue
        copy = hole.model_copy(deep=True)
        merged.append(copy)
        last[hole.kind] = copy
    return merged


def _measure(
    emitter: str,
    reports: Sequence[Report],
    *,
    known_before: bool,
    start: datetime,
    end: datetime,
    now: datetime,
) -> tuple[CoverageStatus, _Tally]:
    """La couverture d'un émetteur sur la période, d'après ses relevés qui la recoupent."""
    tally = _Tally()
    # Ce qui n'est pas encore passé ne se mesure pas.
    until = min(end, max(now, start))
    holes: list[CoverageHole] = []
    covered_to = start
    known = known_before
    gaps = 0
    for report in sorted(reports, key=lambda item: (item.start, item.end)):
        if report.start > covered_to and covered_to < until:
            kind: HoleKind = "silence" if known else "before_first_report"
            holes.append(_hole(emitter, kind, covered_to, min(report.start, until), None))
            gaps += 1
        known = True
        covered_to = max(covered_to, report.end)
        tally.add(report)
        if report.events_lost:
            holes.append(
                _hole(emitter, "events_lost", report.start, report.end, report.events_lost)
            )
        if report.uninspected:
            holes.append(
                _hole(emitter, "uninspected", report.start, report.end, report.uninspected)
            )
        if report.missing:
            holes.append(
                _hole(emitter, "missing_from_graph", report.start, report.end, report.missing)
            )
    if known and covered_to < until:
        holes.append(_hole(emitter, "silence", covered_to, until, None))
        gaps += 1
    tally.holes = _merged(holes)
    if not reports:
        return "unknown", tally
    return ("partial" if gaps else "measured"), tally


def _coverage(status: CoverageStatus, tally: _Tally, *, truncated: bool) -> Coverage:
    holes = sorted(tally.holes, key=lambda hole: (hole.window.start, hole.emitter, hole.kind))
    truncated = truncated or len(holes) > MAX_HOLES
    if truncated and status == "measured":
        # Une mesure qui n'a pas tout lu n'est jamais donnée pour complète.
        status = "partial"
    return Coverage(
        status=status,
        rate=tally.events_in_graph / tally.calls_seen if tally.calls_seen else None,
        calls_seen=tally.calls_seen,
        events_in_graph=tally.events_in_graph,
        events_lost=tally.events_lost,
        uninspected=tally.uninspected,
        holes=holes[:MAX_HOLES],
        truncated=truncated,
    )


@dataclass(frozen=True)
class _Read:
    reports: dict[str, list[Report]]
    known_before: set[str]
    truncated: bool


async def _read(reader: Reader, emitters: list[str], start: datetime, end: datetime) -> _Read:
    found = await reader.fetch(
        _REPORTS, {"emitters": emitters, "start": start, "end": end}, limit=MAX_REPORTS
    )
    reports: dict[str, list[Report]] = {emitter: [] for emitter in emitters}
    for row in found.rows:
        reports[row.emitter].append(
            Report(
                emitter=row.emitter,
                start=row.window_start,
                end=row.window_end,
                calls_seen=row.calls_seen,
                events_emitted=row.events_emitted,
                events_lost=row.events_lost,
                uninspected=row.uninspected,
                in_graph=row.in_graph,
            )
        )
    before = await reader.execute(_KNOWN_BEFORE, {"emitters": emitters, "start": start})
    return _Read(reports, {row.emitter for row in before}, found.truncated)


def _muted_since(row: Any) -> datetime | None:
    """Muet tant que le contrôle l'a signalé et qu'aucun relevé n'est venu depuis."""
    silent: datetime | None = row.silent_since
    return silent if silent is not None and silent == row.last_report_at else None


async def by_emitter(
    reader: Reader, *, start: datetime, end: datetime, now: datetime, limit: int
) -> tuple[list[EmitterCoverage], bool]:
    """La couverture de chaque émetteur connu, par identifiant ; vrai quand d'autres suivent."""
    page = await reader.fetch(_EMITTERS, {}, limit=limit)
    emitters = [row.emitter for row in page.rows]
    read = await _read(reader, emitters, start, end)
    result = []
    for row in page.rows:
        status, tally = _measure(
            row.emitter,
            read.reports[row.emitter],
            known_before=row.emitter in read.known_before,
            start=start,
            end=end,
            now=now,
        )
        result.append(
            EmitterCoverage(
                emitter=row.emitter,
                last_report_at=row.last_report_at,
                muted_since=_muted_since(row),
                coverage=_coverage(status, tally, truncated=read.truncated),
            )
        )
    return result, page.truncated


async def over_period(reader: Reader, *, start: datetime, end: datetime, now: datetime) -> Coverage:
    """La couverture de tous les émetteurs connus avant la fin de la période, réunis."""
    page = await reader.fetch(_KNOWN_BY, {"end": end}, limit=MAX_EMITTERS)
    emitters = [row.emitter for row in page.rows]
    read = await _read(reader, emitters, start, end)
    total = _Tally()
    statuses: set[CoverageStatus] = set()
    for emitter in emitters:
        status, tally = _measure(
            emitter,
            read.reports[emitter],
            known_before=emitter in read.known_before,
            start=start,
            end=end,
            now=now,
        )
        statuses.add(status)
        total.calls_seen += tally.calls_seen
        total.events_in_graph += tally.events_in_graph
        total.events_lost += tally.events_lost
        total.uninspected += tally.uninspected
        total.holes.extend(tally.holes)
    overall: CoverageStatus
    if statuses <= {"unknown"}:
        overall = "unknown"
    elif statuses == {"measured"}:
        overall = "measured"
    else:
        overall = "partial"
    return _coverage(overall, total, truncated=read.truncated or page.truncated)
