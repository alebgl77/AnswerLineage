"""Signaux de qualité : ce qu'un événement dit des tests de qualité de ses entrées.

Un outil de tests de données, dbt par exemple, émet un run par modèle testé ;
son entrée est le jeu de données testé, et la facette standard
``dataQualityAssertions`` (OpenLineage, version 1-1-0) y liste les assertions,
chacune avec sa réussite. La projection en relève, dans la transaction du lot :

- **les échecs**, un signal par run, jeu de données et colonne (``''`` : la
  table), qui réunit les classes d'assertions en échec et leur plus forte
  gravité (``quality_signal``) : un worker en fera une proposition d'incident
  (``proposals.py``) ;
- **les réussites et les échecs par colonne**, au plus récent
  (``quality_column``) : ils fondent la fenêtre déduite d'une proposition.

**Une lecture fermée.** De chaque assertion, quatre champs seulement :
``success``, un booléen strict, sans quoi l'assertion est ignorée ;
``assertion``, ramené à une classe d'une liste fermée (``other`` pour le
reste) ; ``severity``, ramené à ``error``, ``warn`` ou ``unspecified`` ;
``column``, recontrôlé comme un nom, sans quoi l'assertion porte sur la table
entière. Le nom, la description, le corps, les valeurs attendue et observée, les
paramètres ne sont jamais lus : aucun texte libre d'un émetteur n'entre dans la
base, le journal ni les compteurs. La facette est lue dans ``inputFacets``,
sinon dans ``facets`` (dbt écrit les deux) ; mal formée, elle est ignorée, et
l'événement est projeté comme les autres.

**Bornée.** ``MAX_ASSERTIONS_PER_DATASET`` assertions lues par jeu d'entrée,
``MAX_ASSERTIONS_PER_EVENT`` par événement, ``MAX_KEYS_PER_EVENT`` colonnes
relevées par événement, ``MAX_PENDING_SIGNALS`` signaux en attente,
``MAX_TRACKED_COLUMNS`` colonnes suivies : ce qui dépasse est compté
(``quality_stats``), jamais cité.

**Idempotente et commutative.** Un signal a pour clé son run, son jeu et sa
colonne ; rejouer l'événement, reconstruire le graphe, rejouer le rebut, ou
recevoir le début et la fin d'un run dans le désordre, réunit au lieu de doubler.
Les heures de ``quality_column`` s'écrivent au plus grand.

**Fraîcheur.** Un événement reçu ou daté de plus de ``FRESHNESS`` ne propose
rien : son signal naît « stale », compté.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, cast
from uuid import UUID

from sqlalchemy import func, literal_column, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnClause

from answerlineage_api import identifiers
from answerlineage_api.identifiers import identifier_fault, normalize_identifier
from answerlineage_api.ingest.models import parse_event_time
from answerlineage_api.store import schema
from answerlineage_api.store.schema import (
    ASSERTION_CLASSES,
    INCIDENT_WINDOW_DAYS,
    QUALITY_SEVERITIES,
    AssertionClass,
    QualitySeverity,
)

logger = logging.getLogger("answerlineage_workers")

# La facette standard d'OpenLineage, et les conteneurs où la chercher, dans l'ordre.
ASSERTIONS_FACET: Final = "dataQualityAssertions"
CONTAINERS: Final = ("inputFacets", "facets")
# La table entière : un nom de colonne n'est jamais vide.
TABLE: Final = ""

MAX_ASSERTIONS_PER_DATASET: Final = 1_000
MAX_ASSERTIONS_PER_EVENT: Final = 10_000
# Colonnes relevées par événement, échecs d'abord : au-delà, perdues et comptées.
MAX_KEYS_PER_EVENT: Final = 1_000
MAX_PENDING_SIGNALS: Final = 100_000
MAX_TRACKED_COLUMNS: Final = 100_000
# Au-delà, un événement ne propose rien : la fenêtre d'un incident dure au plus autant.
FRESHNESS: Final = timedelta(days=INCIDENT_WINDOW_DAYS)

# Un nom de classe ou de gravité plus long n'est d'aucune liste : il n'est pas lu.
_MAX_CODE: Final = 64
_CLASSES: Final = frozenset(ASSERTION_CLASSES) - {"other"}
_RANK: Final = {severity: rank for rank, severity in enumerate(QUALITY_SEVERITIES)}

_PENDING = text(
    "SELECT count(*) FROM (SELECT 1 FROM quality_signal WHERE state = 'pending' LIMIT :limit) s"
)
_TRACKED = text("SELECT count(*) FROM (SELECT 1 FROM quality_column LIMIT :limit) AS tracked")
_NOW = text("SELECT now()")
# Deux écritures d'un même signal se réunissent : classes, plus forte gravité, plus tôt.
_MERGED_CLASSES: Final[ColumnClause[Any]] = literal_column(
    "ARRAY(SELECT DISTINCT c FROM unnest(quality_signal.classes || excluded.classes) AS c"
    " ORDER BY c)"
)
_STRONGEST: Final[ColumnClause[Any]] = literal_column(
    "CASE WHEN 'error' IN (quality_signal.severity, excluded.severity) THEN 'error'"
    " WHEN 'warn' IN (quality_signal.severity, excluded.severity) THEN 'warn'"
    " ELSE 'unspecified' END"
)


@dataclass(frozen=True)
class Key:
    """Une colonne testée d'un jeu de données ; ``TABLE`` pour la table entière."""

    namespace: str
    name: str
    column: str


@dataclass(frozen=True)
class Failure:
    """Les assertions en échec d'un run sur une colonne : leurs classes, leur plus forte gravité."""

    key: Key
    classes: tuple[AssertionClass, ...]
    severity: QualitySeverity


@dataclass(frozen=True)
class Assessment:
    """Ce qu'un événement dit de la qualité de ses entrées, borné, sans texte libre."""

    run_id: UUID
    emitter: str
    event_time: datetime
    failures: tuple[Failure, ...] = ()
    passes: tuple[Key, ...] = ()
    # Colonnes au-delà de la borne par événement ; colonnes qui n'ont pas la forme
    # d'un nom ; assertions non lues au-delà de leurs bornes.
    dropped: int = 0
    invalid_columns: int = 0
    assertions_truncated: int = 0

    @property
    def empty(self) -> bool:
        return not (self.failures or self.passes or self.anomalies)

    @property
    def anomalies(self) -> bool:
        return bool(self.dropped or self.invalid_columns or self.assertions_truncated)


@dataclass
class _Tally:
    """Ce que les assertions d'un événement disent d'une colonne, en cours de lecture."""

    failed: bool = False
    classes: set[AssertionClass] = field(default_factory=set)
    severity: QualitySeverity = "unspecified"


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _identifier(value: object, max_length: int) -> str | None:
    if not isinstance(value, str) or identifier_fault(value, max_length=max_length) is not None:
        return None
    return normalize_identifier(value)


def assertion_class(value: object) -> AssertionClass:
    """La classe d'une assertion : son nom s'il est de la liste fermée, « other » sinon."""
    if isinstance(value, str) and len(value) <= _MAX_CODE:
        lowered = value.lower()
        if lowered in _CLASSES:
            return cast(AssertionClass, lowered)
    return "other"


def assertion_severity(value: object) -> QualitySeverity:
    """La gravité réglée d'une assertion : « error », « warn », sinon « unspecified »."""
    if isinstance(value, str) and len(value) <= _MAX_CODE:
        lowered = value.lower()
        if lowered in ("error", "warn"):
            return cast(QualitySeverity, lowered)
    return "unspecified"


def assertion_column(value: object) -> tuple[str, bool]:
    """La colonne d'une assertion, normalisée ; la table quand il n'y en a pas, ou quand
    elle n'a pas la forme d'un nom (une expression, par exemple). Le second élément est
    vrai dans ce dernier cas."""
    if value is None or value == "":
        return TABLE, False
    column = _identifier(value, identifiers.COLUMN_MAX)
    return (TABLE, True) if column is None else (column, False)


def stronger(first: QualitySeverity, second: QualitySeverity) -> QualitySeverity:
    return first if _RANK[first] >= _RANK[second] else second


def _assertions(dataset: Mapping[str, Any], facet: str) -> list[Any] | None:
    """Les assertions de la facette ``facet`` du jeu, ou ``None`` quand elle manque ou est
    mal formée."""
    for container in CONTAINERS:
        found = _mapping(dataset.get(container)).get(facet)
        if isinstance(found, Mapping):
            assertions = found.get("assertions")
            return assertions if isinstance(assertions, list) else None
    return None


@dataclass(frozen=True)
class Reading:
    """Une assertion lue : sa réussite, sa classe, sa gravité, et la colonne qu'elle donne."""

    success: bool
    assertion: AssertionClass
    severity: QualitySeverity
    column: object


def read_standard(item: Mapping[str, Any]) -> Reading | None:
    """Une assertion de ``dataQualityAssertions`` ; ``None`` si sa réussite n'est pas un booléen."""
    success = item.get("success")
    if not isinstance(success, bool):
        return None
    return Reading(
        success=success,
        assertion=assertion_class(item.get("assertion")),
        severity=assertion_severity(item.get("severity")),
        column=item.get("column"),
    )


# Les facettes lues, et comment lire une de leurs assertions.
READERS: Final = ((ASSERTIONS_FACET, read_standard),)


def assess(payload: Mapping[str, Any]) -> Assessment | None:
    """Les échecs et les réussites des tests de qualité de l'événement, bornés.

    Rend ``None`` quand l'événement n'en porte aucun, ou que son run, son job ou son
    heure ne se lisent pas : la projection l'a déjà intégré au graphe, rien n'est à
    relever.
    """
    inputs = payload.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        return None
    tallies: dict[Key, _Tally] = {}
    budget = MAX_ASSERTIONS_PER_EVENT
    truncated = invalid = 0
    found = False
    for dataset in inputs:
        entry = _mapping(dataset)
        namespace = _identifier(entry.get("namespace"), identifiers.NAMESPACE_MAX)
        name = _identifier(entry.get("name"), identifiers.DATASET_NAME_MAX)
        if namespace is None or name is None:
            continue
        for facet, read in READERS:
            assertions = _assertions(entry, facet)
            if assertions is None:
                continue
            found = True
            allowed = min(MAX_ASSERTIONS_PER_DATASET, budget)
            truncated += max(0, len(assertions) - allowed)
            budget -= min(len(assertions), allowed)
            for item in assertions[:allowed]:
                reading = read(_mapping(item)) if isinstance(item, Mapping) else None
                if reading is None:
                    continue
                column, faulty = assertion_column(reading.column)
                invalid += int(faulty)
                tally = tallies.setdefault(Key(namespace, name, column), _Tally())
                if not reading.success:
                    tally.failed = True
                    tally.classes.add(reading.assertion)
                    tally.severity = stronger(tally.severity, reading.severity)
    if not found:
        return None
    try:
        run_id = UUID(str(_mapping(payload["run"])["runId"]))
        emitter = _identifier(_mapping(payload["job"]).get("namespace"), identifiers.NAMESPACE_MAX)
        event_time = parse_event_time(payload["eventTime"])
    except (KeyError, TypeError, ValueError):
        return None
    if emitter is None:
        return None
    return _bounded(run_id, emitter, event_time, tallies, truncated, invalid)


def _bounded(
    run_id: UUID,
    emitter: str,
    event_time: datetime,
    tallies: Mapping[Key, _Tally],
    truncated: int,
    invalid: int,
) -> Assessment:
    """Les colonnes relevées, échecs d'abord, dans l'ordre de leur clé, sous leur borne."""

    def order(key: Key) -> tuple[str, str, str]:
        return key.namespace, key.name, key.column

    failed = sorted((key for key, tally in tallies.items() if tally.failed), key=order)
    passed = sorted((key for key, tally in tallies.items() if not tally.failed), key=order)
    kept_failed = failed[:MAX_KEYS_PER_EVENT]
    kept_passed = passed[: MAX_KEYS_PER_EVENT - len(kept_failed)]
    return Assessment(
        run_id=run_id,
        emitter=emitter,
        event_time=event_time,
        failures=tuple(
            Failure(
                key,
                tuple(sorted(tallies[key].classes)),
                tallies[key].severity,
            )
            for key in kept_failed
        ),
        passes=tuple(kept_passed),
        dropped=len(failed) + len(passed) - len(kept_failed) - len(kept_passed),
        invalid_columns=invalid,
        assertions_truncated=truncated,
    )


async def record(conn: AsyncConnection, assessment: Assessment, received_at: datetime) -> None:
    """Écrit les signaux d'échec, la dernière réussite et le dernier échec de chaque
    colonne, et les compteurs, dans la transaction de ``conn``."""
    now: datetime = (await conn.execute(_NOW)).scalar_one()
    dropped = assessment.dropped
    stale = 0
    if assessment.failures or assessment.passes:
        await _track(conn, assessment)
    if assessment.failures:
        written, stale = await _signal(conn, assessment, received_at, now)
        dropped += len(assessment.failures) - written
    lost = (
        ("dropped", dropped),
        ("stale", stale),
        ("invalid_columns", assessment.invalid_columns),
        ("assertions_truncated", assessment.assertions_truncated),
    )
    await count(conn, "projection", **{name: number for name, number in lost if number})
    if dropped or assessment.assertions_truncated:
        # Des nombres seulement : rien de l'événement n'est cité.
        logger.warning(
            "Tests de qualité bornés : %d assertion(s) non lue(s), %d colonne(s) perdue(s).",
            assessment.assertions_truncated,
            dropped,
        )


def _rows(keys: Iterable[Key], **values: Any) -> list[dict[str, Any]]:
    return [
        {"namespace": key.namespace, "name": key.name, "column_name": key.column, **values}
        for key in keys
    ]


async def _track(conn: AsyncConnection, assessment: Assessment) -> None:
    """La dernière réussite et le dernier échec de chaque colonne, au plus grand.

    Au-delà de ``MAX_TRACKED_COLUMNS`` colonnes suivies, seules celles déjà suivies
    sont mises à jour.
    """
    at = assessment.event_time
    rows = _rows(
        (failure.key for failure in assessment.failures), last_passed_at=None, last_failed_at=at
    ) + _rows(assessment.passes, last_passed_at=at, last_failed_at=None)
    rows.sort(key=lambda row: (row["namespace"], row["name"], row["column_name"]))
    table = schema.quality_column
    tracked = (await conn.execute(_TRACKED, {"limit": MAX_TRACKED_COLUMNS})).scalar_one()
    if tracked < MAX_TRACKED_COLUMNS:
        statement = insert(table).values(rows)
        await conn.execute(
            statement.on_conflict_do_update(
                index_elements=[table.c.namespace, table.c.name, table.c.column_name],
                set_={
                    "last_passed_at": func.greatest(
                        table.c.last_passed_at, statement.excluded.last_passed_at
                    ),
                    "last_failed_at": func.greatest(
                        table.c.last_failed_at, statement.excluded.last_failed_at
                    ),
                },
            )
        )
        return
    for row in rows:
        await conn.execute(
            update(table)
            .where(
                table.c.namespace == row["namespace"],
                table.c.name == row["name"],
                table.c.column_name == row["column_name"],
            )
            .values(
                last_passed_at=func.greatest(table.c.last_passed_at, row["last_passed_at"]),
                last_failed_at=func.greatest(table.c.last_failed_at, row["last_failed_at"]),
            )
        )


async def _signal(
    conn: AsyncConnection, assessment: Assessment, received_at: datetime, now: datetime
) -> tuple[int, int]:
    """Écrit les signaux d'échec ; rend combien sont écrits, et combien sont trop anciens.

    Rien n'est écrit quand ``MAX_PENDING_SIGNALS`` signaux attendent déjà un worker.
    """
    pending = (await conn.execute(_PENDING, {"limit": MAX_PENDING_SIGNALS})).scalar_one()
    if pending >= MAX_PENDING_SIGNALS:
        return 0, 0
    stale = min(assessment.event_time, received_at) < now - FRESHNESS
    table = schema.quality_signal
    statement = insert(table).values(
        [
            {
                "run_id": assessment.run_id,
                "namespace": failure.key.namespace,
                "name": failure.key.name,
                "column_name": failure.key.column,
                "classes": list(failure.classes),
                "severity": failure.severity,
                "emitter": assessment.emitter,
                "event_time": assessment.event_time,
                "recorded_at": received_at,
                "state": "stale" if stale else "pending",
            }
            for failure in assessment.failures
        ]
    )
    await conn.execute(
        statement.on_conflict_do_update(
            index_elements=[table.c.run_id, table.c.namespace, table.c.name, table.c.column_name],
            set_={
                "classes": _MERGED_CLASSES,
                "severity": _STRONGEST,
                "event_time": func.least(table.c.event_time, statement.excluded.event_time),
            },
        )
    )
    written = len(assessment.failures)
    return written, written if stale else 0


async def count(conn: AsyncConnection, stage: str, **counters: int) -> None:
    """Ajoute aux compteurs de l'étape ``stage`` ; rien quand tous sont nuls."""
    if not counters:
        return
    table = schema.quality_stats
    statement = insert(table).values(stage=stage, **counters)
    await conn.execute(
        statement.on_conflict_do_update(
            index_elements=[table.c.stage],
            set_={name: table.c[name] + statement.excluded[name] for name in counters},
        )
    )


async def lost(conn: AsyncConnection, assessment: Assessment) -> None:
    """Compte perdus les échecs d'un événement dont l'écriture a échoué."""
    await count(conn, "projection", dropped=len(assessment.failures))
