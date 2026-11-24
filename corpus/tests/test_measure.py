"""La mesure du moteur d'impact et sa porte, éprouvées sur des observations fabriquées.

Une observation est ce que le moteur aurait renvoyé pour chaque scénario : les
empreintes des réponses. Ici, elles sont écrites d'après la vérité du corpus,
puis abîmées une à une.
"""

from __future__ import annotations

import json
from fractions import Fraction

from answerlineage_corpus.measure import (
    MIN_TABLE_RECALL,
    Tally,
    as_json,
    gate,
    measure,
    render,
)
from answerlineage_corpus.scenarios import Scenario, load_corpus

CORPUS = load_corpus()
# Le seuil tel que les messages l'écrivent : « 0,99 ».
THRESHOLD = str(MIN_TABLE_RECALL).replace(".", ",")


def scenario_named(name: str) -> Scenario:
    return next(item for item in CORPUS if item.name == name)


def perfect() -> dict[str, set[str]]:
    """Ce que renvoie un moteur exact au niveau table : tout lecteur de la fenêtre."""
    return {
        item.name: {answer.fingerprint for answer in item.expected("direct")}
        | {answer.fingerprint for answer, _ in item.discarded}
        for item in CORPUS
    }


def fingerprint(scenario: str, key: str) -> str:
    return next(
        answer.fingerprint for answer in scenario_named(scenario).answers if answer.key == key
    )


def names(count: int, prefix: str = "s") -> frozenset[str]:
    return frozenset(f"{prefix}/{rank}" for rank in range(count))


def test_a_table_level_engine_that_returns_every_reader_passes_the_gate() -> None:
    result = measure(CORPUS, perfect())

    direct = sum(len(item.expected("direct")) for item in CORPUS)
    discarded = sum(len(item.discarded) for item in CORPUS)
    assert len(result.table.expected) == direct + discarded
    assert result.table.missed == frozenset()
    assert result.table.extra == frozenset()
    assert result.table.recall == 1
    assert result.table.precision == 1
    assert gate(result.table) == []


def test_the_answers_the_corpus_discards_count_against_the_declared_precision() -> None:
    result = measure(CORPUS, perfect())

    # Un moteur au niveau table renvoie les réponses écartées : la précision déclarée le dit.
    assert result.declared.extra == frozenset(
        f"{item.name}/{answer.key}" for item in CORPUS for answer, _ in item.discarded
    )
    assert result.declared.missed == frozenset()
    assert result.declared.precision is not None
    assert result.declared.precision < 1
    by_level = result.declared_by_level()
    assert list(by_level) == ["table", "columns", "subset", "keys"]
    assert by_level["columns"].extra == {
        "colonnes/autres-colonnes",
        "exposition-colonne-unique/date-de-valeur",
    }
    assert by_level["subset"].precision == Fraction(3, 5)
    # La version saine d'une table rechargée s'écarte dans un scénario de niveau table.
    assert by_level["table"].extra == {"version-corrigee/version-saine"}


def test_answers_reached_through_a_written_output_are_measured_apart() -> None:
    result = measure(CORPUS, perfect())

    assert result.transitive.missed == {
        "transitivite-chaine/lit-la-derivee",
        "transitivite-chaine/lit-la-note",
        "transitivite-memoire/lit-la-note",
    }
    # Le moteur ne suit pas encore les sorties : la porte n'en dépend pas.
    assert not result.table.expected & result.transitive.expected
    assert gate(result.table) == []


def test_one_missed_answer_fails_the_gate_which_names_it_then_gives_the_recall() -> None:
    returned = perfect()
    returned["table-sous-agent"].discard(fingerprint("table-sous-agent", "synthese"))
    result = measure(CORPUS, returned)
    expected = len(result.table.expected)

    assert result.table.missed == {"table-sous-agent/synthese"}
    assert gate(result.table) == [
        "Réponses manquées au niveau table :",
        "  table-sous-agent/synthese",
        f"Rappel au niveau table : {(expected - 1) / expected:.3f}".replace(".", ",")
        + f" ({expected - 1}/{expected}), sous le seuil de {THRESHOLD}.",
    ]


def test_a_recall_exactly_at_the_threshold_passes_and_one_answer_less_fails() -> None:
    threshold = Fraction(str(MIN_TABLE_RECALL))
    expected = names(threshold.denominator)
    found = names(threshold.numerator)
    at_threshold = Tally(expected=expected, found=found, missed=expected - found)
    assert at_threshold.recall == threshold
    assert gate(at_threshold) == []

    fewer = names(threshold.numerator - 1)
    below = Tally(expected=expected, found=fewer, missed=expected - fewer)
    problems = gate(below)
    assert problems[0] == "Réponses manquées au niveau table :"
    assert problems[1:-1] == [f"  {name}" for name in sorted(expected - fewer)]
    assert problems[-1].startswith("Rappel au niveau table : ")


def test_an_answer_the_corpus_puts_out_of_reach_fails_the_gate() -> None:
    returned = perfect()
    returned["fenetre-sans-lecture"].add(fingerprint("fenetre-sans-lecture", "la-veille"))
    result = measure(CORPUS, returned)

    assert result.table.recall == 1
    assert result.table.extra == {"fenetre-sans-lecture/la-veille"}
    problems = gate(result.table)
    assert problems[:2] == [
        "Réponses renvoyées que le corpus dit hors d'atteinte :",
        "  fenetre-sans-lecture/la-veille",
    ]
    assert problems[2].startswith("Précision au niveau table : ")


def test_an_answer_unknown_to_the_scenario_counts_as_extra_without_being_copied() -> None:
    returned = perfect()
    returned["cles"].add("sha256:" + "f" * 64)
    result = measure(CORPUS, returned)

    assert result.table.extra == {"cles/inconnue-1"}
    assert not any("ffff" in line for line in gate(result.table))


def test_a_scenario_without_observation_returned_nothing() -> None:
    returned = perfect()
    del returned["table-fenetre"]
    result = measure(CORPUS, returned)

    assert result.table.missed == {
        "table-fenetre/matin",
        "table-fenetre/reponse-rendue-apres-la-correction",
    }


def test_nothing_expected_cannot_pass_the_gate() -> None:
    assert Tally().recall is None
    assert Tally().precision is None
    assert gate(Tally()) == [
        "Aucune réponse attendue au niveau table : le rappel ne se mesure pas."
    ]


def test_the_table_shows_every_scenario_every_level_and_the_verdict() -> None:
    returned = perfect()
    returned["table-sous-agent"].discard(fingerprint("table-sous-agent", "synthese"))
    shown = render(measure(CORPUS, returned))
    lines = shown.splitlines()

    assert (
        lines[0]
        == f"Corpus de vérité, version {CORPUS.version} : {len(CORPUS.scenarios)} scénarios"
    )
    for item in CORPUS:
        assert sum(1 for line in lines if line.startswith(item.name + " ")) == 2, item.name
    for label in ("colonnes", "sous-ensemble", "clés"):
        assert any(line.startswith(f"niveau {label} ") for line in lines), label
    assert sum(1 for line in lines if line.startswith("ensemble ")) == 2
    assert (
        "Transitivité, que le moteur ne suit pas encore : 3 attendues, 0 retrouvée, 3 manquées."
        in lines
    )
    assert lines[-4:] == [
        "Porte de qualité : rouge.",
        "Réponses manquées au niveau table :",
        "  table-sous-agent/synthese",
        lines[-1],
    ]
    assert lines[-1].startswith("Rappel au niveau table : ")

    green = render(measure(CORPUS, perfect())).splitlines()
    assert green[-1].startswith("Porte de qualité : verte. Rappel au niveau table 1,000 (")
    assert green[-1].endswith(f"seuil {THRESHOLD} ; aucune réponse hors d'atteinte renvoyée.")


def test_the_report_for_machines_holds_the_same_measure() -> None:
    returned = perfect()
    returned["colonnes"].discard(fingerprint("colonnes", "deux-appels"))
    result = measure(CORPUS, returned)
    report = json.loads(json.dumps(as_json(result)))

    assert report["corpus_version"] == CORPUS.version
    assert report["minimum_table_recall"] == MIN_TABLE_RECALL
    assert report["passed"] is False
    assert report["problems"] == gate(result.table)
    assert report["table"]["missed"] == ["colonnes/deux-appels"]
    assert report["table"]["expected"] == len(result.table.expected)
    assert report["table"]["recall"] == float(result.table.recall or 0)
    assert [item["name"] for item in report["scenarios"]] == [item.name for item in CORPUS]
    columns = next(item for item in report["scenarios"] if item["name"] == "colonnes")
    assert columns["level"] == "columns"
    assert columns["declared"]["extra"] == ["colonnes/autres-colonnes"]
    assert set(report["declared"]) == {"table", "columns", "subset", "keys", "all"}
    assert report["transitive"]["precision"] is None
