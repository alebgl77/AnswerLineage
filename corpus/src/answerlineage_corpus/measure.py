"""Mesure du moteur d'impact sur le corpus de vérité, et sa porte de qualité.

Le moteur se mesure comme un modèle : pour chaque scénario, les réponses qu'il
a renvoyées sont comparées à la vérité écrite à la main. La mesure ne calcule
rien avec le produit : elle reçoit ce que le moteur a renvoyé, les empreintes
des réponses, et la vérité vient du seul corpus.

La même vérité se lit de deux façons :

- **au niveau table**, celui où le moteur propage : toute réponse dont un run a
  lu la table pendant la fenêtre est attendue, y compris celles que le corpus
  écarte à un niveau plus fin, et une réponse que le corpus dit hors d'atteinte
  ne doit jamais être renvoyée. C'est la porte de qualité ;
- **à la précision déclarée** par le scénario (colonnes, sous-ensemble, clés,
  version) : les réponses écartées comptent en trop. Cette précision est
  mesurée et rapportée ; elle mesure ce que gagnerait une propagation plus fine.

Les réponses touchées par transitivité, qui dépendent d'une sortie d'un run
touché et non de la table elle-même, sont mesurées à part : le moteur ne suit
pas encore ce qu'un run touché a écrit, et la porte n'en dépend pas.

Une réponse se désigne par son scénario et son identifiant dans le scénario,
« table-fenetre/matin » : des noms synthétiques, jamais une valeur reçue.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from answerlineage_corpus.scenarios import LEVELS, Corpus, Level, Scenario

# Rappel minimal au niveau table, sur l'ensemble du corpus. Taire une réponse
# touchée est le pire défaut d'un rappel : celui qui la lit ne sera jamais
# prévenu. Sur les quelques dizaines de réponses attendues du corpus, 0,99 n'en
# tolère aucune manquée ; quand il grandira, une sur cent au plus.
MIN_TABLE_RECALL = 0.99

_LEVEL_LABELS: dict[Level, str] = {
    "table": "table",
    "columns": "colonnes",
    "subset": "sous-ensemble",
    "keys": "clés",
}


@dataclass(frozen=True)
class Tally:
    """Réponses attendues, retrouvées, manquées et en trop, désignées « scénario/réponse ».

    Une réponse renvoyée qui n'est ni attendue ni exclue par la vérité, comme
    une réponse touchée par transitivité au niveau table, ne compte ni pour ni
    contre : elle n'est pas dans ``found`` et pas dans ``extra``.
    """

    expected: frozenset[str] = field(default_factory=frozenset)
    found: frozenset[str] = field(default_factory=frozenset)
    missed: frozenset[str] = field(default_factory=frozenset)
    extra: frozenset[str] = field(default_factory=frozenset)

    @property
    def recall(self) -> Fraction | None:
        """Part des réponses attendues qui sont retrouvées ; nulle quand rien n'est attendu."""
        return Fraction(len(self.found), len(self.expected)) if self.expected else None

    @property
    def precision(self) -> Fraction | None:
        """Part des réponses renvoyées et jugées qui sont attendues ; nulle sans réponse jugée."""
        judged = len(self.found) + len(self.extra)
        return Fraction(len(self.found), judged) if judged else None

    def __add__(self, other: Tally) -> Tally:
        return Tally(
            expected=self.expected | other.expected,
            found=self.found | other.found,
            missed=self.missed | other.missed,
            extra=self.extra | other.extra,
        )


def _total(tallies: Iterable[Tally]) -> Tally:
    total = Tally()
    for tally in tallies:
        total += tally
    return total


@dataclass(frozen=True)
class ScenarioMeasure:
    name: str
    level: Level
    table: Tally
    declared: Tally
    transitive: Tally


@dataclass(frozen=True)
class CorpusMeasure:
    version: str
    scenarios: tuple[ScenarioMeasure, ...]

    @property
    def table(self) -> Tally:
        return _total(item.table for item in self.scenarios)

    @property
    def declared(self) -> Tally:
        return _total(item.declared for item in self.scenarios)

    @property
    def transitive(self) -> Tally:
        return _total(item.transitive for item in self.scenarios)

    def declared_by_level(self) -> dict[Level, Tally]:
        """La précision déclarée, réunie par niveau de précision des scénarios."""
        return {
            level: _total(item.declared for item in self.scenarios if item.level == level)
            for level in LEVELS
            if any(item.level == level for item in self.scenarios)
        }


def _tally(
    names: Mapping[str, str],
    returned: set[str],
    *,
    positives: set[str],
    negatives: set[str],
) -> Tally:
    def named(fingerprints: set[str]) -> frozenset[str]:
        return frozenset(names[fingerprint] for fingerprint in fingerprints)

    return Tally(
        expected=named(positives),
        found=named(positives & returned),
        missed=named(positives - returned),
        extra=named(negatives & returned),
    )


def _measure_scenario(scenario: Scenario, returned: Collection[str]) -> ScenarioMeasure:
    got = set(returned)
    names = {answer.fingerprint: f"{scenario.name}/{answer.key}" for answer in scenario.answers}
    # Une réponse que le scénario ne connaît pas est en trop. Son empreinte vient
    # du moteur : elle est désignée par son rang, jamais recopiée.
    unknown = sorted(got - names.keys())
    for rank, fingerprint in enumerate(unknown, start=1):
        names[fingerprint] = f"{scenario.name}/inconnue-{rank}"

    direct = {answer.fingerprint for answer in scenario.expected("direct")}
    discarded = {answer.fingerprint for answer, _ in scenario.discarded}
    untouched = {answer.fingerprint for answer in scenario.untouched}
    return ScenarioMeasure(
        name=scenario.name,
        level=scenario.level,
        # Au niveau table, une réponse écartée a lu la table pendant la fenêtre : elle est attendue.
        table=_tally(names, got, positives=direct | discarded, negatives=untouched | set(unknown)),
        declared=_tally(
            names, got, positives=direct, negatives=discarded | untouched | set(unknown)
        ),
        transitive=_tally(
            names,
            got,
            positives={answer.fingerprint for answer in scenario.expected("indirect")},
            negatives=set(),
        ),
    )


def measure(corpus: Corpus, returned: Mapping[str, Collection[str]]) -> CorpusMeasure:
    """Mesure le moteur : ``returned`` donne, par scénario, les empreintes qu'il a renvoyées.

    Un scénario absent de ``returned`` n'a rien renvoyé.
    """
    return CorpusMeasure(
        version=corpus.version,
        scenarios=tuple(
            _measure_scenario(scenario, returned.get(scenario.name, ())) for scenario in corpus
        ),
    )


def _number(value: Fraction | None, digits: int = 3) -> str:
    return "—" if value is None else f"{float(value):.{digits}f}".replace(".", ",")


def _threshold(minimum: float) -> str:
    return str(minimum).replace(".", ",")


def gate(table: Tally, minimum: float = MIN_TABLE_RECALL) -> list[str]:
    """Ce qui fait échouer la porte, ligne à ligne ; la liste est vide quand elle passe.

    ``table`` est la mesure au niveau table sur l'ensemble du corpus. La porte
    exige un rappel d'au moins ``minimum``, et qu'aucune réponse que le corpus
    dit hors d'atteinte ne soit renvoyée.
    """
    problems: list[str] = []
    recall = table.recall
    if recall is None:
        problems.append("Aucune réponse attendue au niveau table : le rappel ne se mesure pas.")
    elif recall < Fraction(str(minimum)):
        problems.append("Réponses manquées au niveau table :")
        problems.extend(f"  {name}" for name in sorted(table.missed))
        problems.append(
            f"Rappel au niveau table : {_number(recall)} ({len(table.found)}/"
            f"{len(table.expected)}), sous le seuil de {_threshold(minimum)}."
        )
    if table.extra:
        problems.append("Réponses renvoyées que le corpus dit hors d'atteinte :")
        problems.extend(f"  {name}" for name in sorted(table.extra))
        problems.append(
            f"Précision au niveau table : {_number(table.precision)} ({len(table.found)}/"
            f"{len(table.found) + len(table.extra)})."
        )
    return problems


_HEADER = ("attendues", "retrouvées", "manquées", "en trop", "rappel", "précision")


def _row(label: str, level: str, tally: Tally, width: int) -> str:
    cells = (
        len(tally.expected),
        len(tally.found),
        len(tally.missed),
        len(tally.extra),
        _number(tally.recall),
        _number(tally.precision),
    )
    numbers = "".join(
        f"{cell!s:>{len(title) + 2}}" for cell, title in zip(cells, _HEADER, strict=True)
    )
    return f"{label:<{width}}{level:<15}{numbers}".rstrip()


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {many if count > 1 else one}"


def render(result: CorpusMeasure, minimum: float = MIN_TABLE_RECALL) -> str:
    """Le tableau de mesure, scénario par scénario puis en tout, et le verdict de la porte."""
    width = max([len("ensemble"), *(len(item.name) for item in result.scenarios)]) + 2
    header = f"{'scénario':<{width}}{'niveau':<15}" + "".join(
        f"{title:>{len(title) + 2}}" for title in _HEADER
    )
    lines = [
        f"Corpus de vérité, version {result.version} : {len(result.scenarios)} scénarios",
        "",
        "Au niveau table, porte de qualité : tout lecteur de la table pendant la fenêtre"
        " est attendu.",
        header,
    ]
    lines += [
        _row(item.name, _LEVEL_LABELS[item.level], item.table, width) for item in result.scenarios
    ]
    lines += [_row("ensemble", "", result.table, width), ""]

    lines += [
        "À la précision déclarée par le corpus : les réponses écartées comptent en trop.",
        header,
    ]
    lines += [
        _row(item.name, _LEVEL_LABELS[item.level], item.declared, width)
        for item in result.scenarios
    ]
    lines += [
        _row(f"niveau {_LEVEL_LABELS[level]}", "", tally, width)
        for level, tally in result.declared_by_level().items()
    ]
    lines += [_row("ensemble", "", result.declared, width), ""]

    transitive = result.transitive
    lines.append(
        "Transitivité, que le moteur ne suit pas encore : "
        f"{_plural(len(transitive.expected), 'attendue', 'attendues')}, "
        f"{_plural(len(transitive.found), 'retrouvée', 'retrouvées')}, "
        f"{_plural(len(transitive.missed), 'manquée', 'manquées')}."
    )
    lines.append("")

    problems = gate(result.table, minimum)
    if problems:
        lines += ["Porte de qualité : rouge.", *problems]
    else:
        table = result.table
        lines.append(
            f"Porte de qualité : verte. Rappel au niveau table {_number(table.recall)} "
            f"({len(table.found)}/{len(table.expected)}), seuil {_threshold(minimum)} ; "
            "aucune réponse hors d'atteinte renvoyée."
        )
    return "\n".join(lines)


def _tally_json(tally: Tally) -> dict[str, Any]:
    return {
        "expected": len(tally.expected),
        "found": len(tally.found),
        "missed": sorted(tally.missed),
        "extra": sorted(tally.extra),
        "recall": None if tally.recall is None else float(tally.recall),
        "precision": None if tally.precision is None else float(tally.precision),
    }


def as_json(result: CorpusMeasure, minimum: float = MIN_TABLE_RECALL) -> dict[str, Any]:
    """La même mesure, lisible par une machine."""
    problems = gate(result.table, minimum)
    return {
        "corpus_version": result.version,
        "minimum_table_recall": minimum,
        "passed": not problems,
        "problems": problems,
        "table": _tally_json(result.table),
        "declared": {
            **{level: _tally_json(tally) for level, tally in result.declared_by_level().items()},
            "all": _tally_json(result.declared),
        },
        "transitive": _tally_json(result.transitive),
        "scenarios": [
            {
                "name": item.name,
                "level": item.level,
                "table": _tally_json(item.table),
                "declared": _tally_json(item.declared),
                "transitive": _tally_json(item.transitive),
            }
            for item in result.scenarios
        ],
    }
