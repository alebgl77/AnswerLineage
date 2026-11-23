"""Exposition : un score par jeu de données et par colonne, selon les réponses qui en dépendent.

Une réponse dépend d'un jeu de données quand son run ou l'un de ses runs
enfants l'a lu, à toute profondeur : ce sont ses sources (``trace.py``), sans
fenêtre de temps ni transitivité. Elle dépend d'une colonne quand une de ces
lectures la nomme ; une lecture de la table entière la fait dépendre de chaque
colonne connue du jeu, celles qu'une lecture a nommées. C'est le côté sûr :
une colonne fausse touche aussi ceux qui ont tout lu.

Chaque réponse pèse 1 plus un par destinataire distinct, et ce poids diminue
de moitié tous les 30 jours depuis l'heure de la réponse ::

    poids = 2 ** (-âge / 30 jours) * (1 + destinataires)

Le score d'un jeu de données, ou d'une colonne, est la somme des poids des
réponses qui en dépendent. Il ne fait que croître avec une réponse ou un
destinataire de plus, et une réponse récente y pèse plus qu'une ancienne. Une
réponse postérieure à l'heure de référence pèse comme une réponse de cet
instant.

Le calcul est déterministe : le poids de chaque réponse se compte en
billionièmes entiers, leur somme est donc exacte et ne dépend pas de leur
ordre, et le score s'arrondit au millionième. Le même graphe à la même heure
de référence donne le même score. L'heure de référence est un paramètre ; par
défaut, la date de ``known_at`` quand elle est donnée, sinon l'heure de la base.

Chaque lecture du graphe a sa borne, et un classement qui en dépasse une le dit
(``truncated``) : ses scores sont alors des minimums. Les réponses les plus
récentes, qui pèsent le plus, passent d'abord.

``rank`` est le calcul, réutilisable : il classe tous les jeux de données dont
une réponse dépend. ``Ranking.page`` en tire une page.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Row, text

from answerlineage_api.lineage.overview import RUN_TREES
from answerlineage_api.lineage.paging import CursorError, decode_instant, encode_instant
from answerlineage_api.lineage.reading import EARLIEST, LATEST, Reader, known

HALF_LIFE: Final = timedelta(days=30)
# Le poids d'une réponse se compte en billionièmes : l'arrondi de chacune reste
# loin du millionième du score, même sur des dizaines de milliers de réponses.
WEIGHT_SCALE: Final = 10**12
SCORE_SCALE: Final = 10**6
_PER_UNIT: Final = WEIGHT_SCALE // SCORE_SCALE

# Réponses prises en compte, les plus récentes d'abord.
MAX_ANSWERS = 20_000
# Liens de ces réponses vers leurs destinataires.
MAX_LINKS = 100_000
# Runs de leurs arbres, leur propre run compris.
MAX_RUNS = 50_000
# Couples (run d'une réponse, jeu de données lu dans son arbre).
MAX_READS = 50_000
# Triplets (run d'une réponse, jeu de données, colonne nommée par une lecture).
MAX_COLUMN_READS = 100_000
# Colonnes rendues par jeu de données, les plus exposées d'abord.
MAX_COLUMNS = 100

CURSOR_PATTERN: Final = r"^[0-9]{1,18}\.[0-9]{1,18}\.[0-9]{1,19}$"


def decay(produced_at: datetime, at: datetime) -> int:
    """Le poids d'âge d'une réponse à l'heure ``at``, en billionièmes : divisé par 2 en 30 jours."""
    age = max(at - produced_at, timedelta(0))
    return round(WEIGHT_SCALE * math.pow(2.0, -(age / HALF_LIFE)))


def answer_weight(produced_at: datetime, at: datetime, *, recipients: int) -> int:
    """Le poids d'une réponse, en billionièmes : son poids d'âge, fois 1 plus ses destinataires."""
    return decay(produced_at, at) * (1 + recipients)


def units(weight: int) -> int:
    """Une somme de poids en millionièmes, arrondie au plus proche, la moitié vers le haut."""
    return (weight + _PER_UNIT // 2) // _PER_UNIT


def score(weight: int) -> float:
    """Le score d'une somme de poids, au millionième."""
    return units(weight) / SCORE_SCALE


@dataclass(frozen=True)
class ExposureCursor:
    """Position dans le classement : juste après ce jeu de données, à cette heure de référence.

    Il s'écrit ``<heure>.<score>.<jeu>`` : l'heure de référence en microsecondes
    depuis le 1er janvier de l'an 1, le score en millionièmes, l'identifiant
    interne du jeu de données. Il ne recopie rien de l'émetteur. Les pages
    suivantes se calculent à la même heure de référence que la première.
    """

    at: datetime
    units: int
    dataset_id: int

    def encode(self) -> str:
        return f"{encode_instant(self.at)}.{self.units}.{self.dataset_id}"

    @classmethod
    def decode(cls, value: str) -> ExposureCursor:
        """Lit un curseur à la forme déjà validée (``CURSOR_PATTERN``)."""
        micros, scored, dataset_id = value.split(".")
        at = decode_instant(micros)
        if not EARLIEST <= at <= LATEST:
            raise CursorError
        return cls(at, int(scored), int(dataset_id))


@dataclass(frozen=True)
class ColumnScore:
    name: str
    units: int
    answers: int


@dataclass(frozen=True)
class DatasetScore:
    """Un jeu de données, son score en millionièmes et ses colonnes, les plus exposées d'abord."""

    dataset_id: int
    namespace: str
    name: str
    kind: str
    units: int
    answers: int
    columns: tuple[ColumnScore, ...]
    # Les runs des réponses qui en dépendent : leurs destinataires se comptent à la demande.
    roots: tuple[UUID, ...]

    @property
    def score(self) -> float:
        return self.units / SCORE_SCALE

    def key(self) -> tuple[int, str, str]:
        """Le plus exposé d'abord ; à égalité, par espace de noms puis par nom."""
        return (-self.units, self.namespace, self.name)


@dataclass(frozen=True)
class Ranking:
    """Les jeux de données dont une réponse dépend, les plus exposés d'abord."""

    at: datetime
    datasets: tuple[DatasetScore, ...]
    # Vrai quand une lecture dépasse sa borne : les scores sont des minimums.
    truncated: bool
    recipients_of_root: dict[UUID, frozenset[int]] = field(repr=False)

    def recipients(self, dataset: DatasetScore) -> int:
        """Destinataires distincts des réponses qui dépendent de ce jeu de données."""
        found: set[int] = set()
        for root in dataset.roots:
            found.update(self.recipients_of_root.get(root, ()))
        return len(found)

    def page(
        self, limit: int, cursor: ExposureCursor | None
    ) -> tuple[tuple[DatasetScore, ...], bool]:
        """Les ``limit`` jeux qui suivent ``cursor``, et s'il en reste après.

        ``CursorError`` quand le curseur désigne un jeu de données que le
        classement ne connaît pas.
        """
        following = self.datasets
        if cursor is not None:
            anchor = next(
                (item for item in self.datasets if item.dataset_id == cursor.dataset_id), None
            )
            if anchor is None:
                raise CursorError
            after = (-cursor.units, anchor.namespace, anchor.name)
            following = tuple(item for item in self.datasets if item.key() > after)
        return following[:limit], len(following) > limit


_NOW = text("SELECT now() AS now")

# Les plus récentes d'abord : au-delà de la borne, ce sont les plus anciennes,
# qui pèsent le moins, qui manquent.
_ANSWERS = text(
    f"""
    SELECT a.id, a.run_id, a.event_time FROM answer a
    WHERE {known("a")}
    ORDER BY a.event_time DESC, a.id DESC
    LIMIT :limit
    """  # noqa: S608 — seul un fragment constant est interpolé.
)

_RECIPIENTS = text(
    f"""
    SELECT DISTINCT d.answer_id, l.recipient_id
    FROM delivery d
    JOIN delivery_recipient l ON l.delivery_id = d.id AND {known("l")}
    WHERE d.answer_id = ANY(:answer_ids) AND {known("d")}
    ORDER BY d.answer_id, l.recipient_id
    LIMIT :limit
    """  # noqa: S608 — seuls des fragments constants sont interpolés.
)

# Les arbres arrivent en couples (run racine, run de son arbre). Une lecture de
# la table entière, ou qui ne nomme aucune colonne, fait dépendre de tout le jeu.
_DATASETS_READ = text(
    f"""
    SELECT t.root, d.id AS dataset_id, d.namespace, d.name, d.kind,
           bool_or(e.precision = 'table' OR cardinality(e.columns) = 0) AS whole
    FROM unnest(CAST(:roots AS uuid[]), CAST(:run_ids AS uuid[])) AS t(root, run_id)
    JOIN read_edge e ON e.run_id = t.run_id AND {known("e")}
    JOIN dataset d ON d.id = e.dataset_id
    GROUP BY t.root, d.id
    ORDER BY t.root, d.id
    LIMIT :limit
    """  # noqa: S608 — seul un fragment constant est interpolé.
)

_COLUMNS_READ = text(
    f"""
    SELECT DISTINCT t.root, e.dataset_id, c.name AS column_name
    FROM unnest(CAST(:roots AS uuid[]), CAST(:run_ids AS uuid[])) AS t(root, run_id)
    JOIN read_edge e ON e.run_id = t.run_id AND {known("e")}
    CROSS JOIN LATERAL unnest(e.columns) AS c(name)
    WHERE e.precision <> 'table'
    ORDER BY t.root, e.dataset_id, c.name
    LIMIT :limit
    """  # noqa: S608 — seul un fragment constant est interpolé.
)


@dataclass
class _Root:
    """Les réponses d'un même run : elles ont les mêmes sources, leurs poids s'additionnent."""

    weight: int = 0
    answers: int = 0
    recipients: set[int] = field(default_factory=set)


@dataclass
class _Tally:
    namespace: str
    name: str
    kind: str
    weight: int = 0
    answers: int = 0
    roots: list[UUID] = field(default_factory=list)
    # Ce que pèsent les réponses qui ont lu la table entière : chaque colonne le compte.
    whole_weight: int = 0
    whole_answers: int = 0
    # Par colonne nommée : le poids et les réponses de celles qui n'ont pas tout lu.
    named: dict[str, list[int]] = field(default_factory=dict)

    def columns(self) -> tuple[ColumnScore, ...]:
        found = [
            ColumnScore(
                name=name,
                units=units(self.whole_weight + weight),
                answers=self.whole_answers + answers,
            )
            for name, (weight, answers) in self.named.items()
        ]
        return tuple(sorted(found, key=lambda column: (-column.units, column.name)))


async def _reference_time(
    reader: Reader, at: datetime | None, known_at: datetime | None
) -> datetime:
    if at is None:
        at = known_at
    if at is None:
        at = (await reader.execute(_NOW, {})).scalar_one()
    return at.astimezone(UTC)


def _roots(
    rows: Sequence[Row[Any]], at: datetime, recipients: dict[int, set[int]]
) -> dict[UUID, _Root]:
    """Les réponses réunies par run : poids, nombre et destinataires distincts."""
    roots: dict[UUID, _Root] = {}
    for row in rows:
        found = recipients.get(row.id, set())
        root = roots.setdefault(row.run_id, _Root())
        root.weight += answer_weight(row.event_time, at, recipients=len(found))
        root.answers += 1
        root.recipients |= found
    return roots


async def rank(
    reader: Reader, *, at: datetime | None = None, known_at: datetime | None = None
) -> Ranking:
    """Le score de chaque jeu de données dont une réponse dépend, et de ses colonnes.

    ``at`` est l'heure de référence de la décroissance : par défaut ``known_at``,
    sinon l'heure de la base. Avec ``known_at``, sur le graphe tel qu'il était
    connu à cette date.
    """
    at = await _reference_time(reader, at, known_at)
    as_of = {"known_at": known_at}

    answers = await reader.fetch(_ANSWERS, as_of, limit=MAX_ANSWERS)
    links = await reader.fetch(
        _RECIPIENTS, {"answer_ids": [row.id for row in answers.rows], **as_of}, limit=MAX_LINKS
    )
    recipients: dict[int, set[int]] = {}
    for row in links.rows:
        recipients.setdefault(row.answer_id, set()).add(row.recipient_id)
    roots = _roots(answers.rows, at, recipients)

    trees = await reader.fetch(
        RUN_TREES, {"roots": sorted(roots, key=lambda run_id: run_id.hex), **as_of}, limit=MAX_RUNS
    )
    pairs = {
        "roots": [row.root for row in trees.rows],
        "run_ids": [row.run_id for row in trees.rows],
        **as_of,
    }
    reads = await reader.fetch(_DATASETS_READ, pairs, limit=MAX_READS)
    named = await reader.fetch(_COLUMNS_READ, pairs, limit=MAX_COLUMN_READS)

    tallies: dict[int, _Tally] = {}
    whole: dict[tuple[UUID, int], bool] = {}
    for row in reads.rows:
        tally = tallies.setdefault(row.dataset_id, _Tally(row.namespace, row.name, row.kind))
        root = roots[row.root]
        tally.weight += root.weight
        tally.answers += root.answers
        tally.roots.append(row.root)
        whole[(row.root, row.dataset_id)] = row.whole
        if row.whole:
            tally.whole_weight += root.weight
            tally.whole_answers += root.answers
    for row in named.rows:
        # Une colonne ne compte que pour une dépendance que le jeu de données compte aussi.
        dependency = (row.root, row.dataset_id)
        if dependency not in whole:
            continue
        counted = tallies[row.dataset_id].named.setdefault(row.column_name, [0, 0])
        if not whole[dependency]:
            counted[0] += roots[row.root].weight
            counted[1] += roots[row.root].answers

    ranked = sorted(
        (
            DatasetScore(
                dataset_id=dataset_id,
                namespace=tally.namespace,
                name=tally.name,
                kind=tally.kind,
                units=units(tally.weight),
                answers=tally.answers,
                columns=tally.columns(),
                roots=tuple(tally.roots),
            )
            for dataset_id, tally in tallies.items()
        ),
        key=DatasetScore.key,
    )
    return Ranking(
        at=at,
        datasets=tuple(ranked),
        truncated=any(found.truncated for found in (answers, links, trees, reads, named)),
        recipients_of_root={run_id: frozenset(root.recipients) for run_id, root in roots.items()},
    )
