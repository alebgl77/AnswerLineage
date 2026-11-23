"""Le corpus de vérité : des scénarios d'incident et leurs réponses attendues."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from answerlineage_corpus import cli
from answerlineage_corpus.scenarios import (
    CORPUS_VERSION,
    LEVELS,
    Answer,
    CorpusError,
    check_corpus,
    check_scenario,
    corpus_digest,
    load_corpus,
    recorded_digest,
    scenario_files,
)
from answerlineage_corpus.scenarios import Scenario as Built

Scenario = dict[str, Any]


@pytest.fixture
def directory(tmp_path: Path) -> Path:
    """Une copie du corpus, que le test peut modifier."""
    for name, raw in scenario_files().items():
        (tmp_path / f"{name}.json").write_text(json.dumps(raw), encoding="utf-8")
    return tmp_path


def scenario(name: str) -> Scenario:
    raw: Scenario = copy.deepcopy(scenario_files()[name])
    return raw


def test_the_corpus_is_coherent() -> None:
    assert check_corpus() == []
    corpus = load_corpus()
    assert corpus.version == CORPUS_VERSION
    assert len(corpus.scenarios) >= 12
    assert [item.name for item in corpus] == sorted(item.name for item in corpus)


def exposed(name: str) -> tuple[Built, list[str]]:
    """Un scénario d'exposition, et l'ordre attendu de ses jeux de données par leur nom."""
    item = next(found for found in load_corpus() if found.name == name)
    assert item.exposure is not None
    return item, [dataset.name for dataset in item.exposure.datasets]


def readers(item: Built, dataset: str) -> list[Answer]:
    """Les réponses dont un appel a lu ce jeu de données : ces scénarios n'ont pas de sous-agent."""
    return [
        run.answer
        for run in item.runs
        if run.answer is not None
        and any(read.dataset.name == dataset for call in run.calls for read in call.reads)
    ]


def test_at_least_three_scenarios_hold_an_exposure_order_that_is_not_trivial() -> None:
    assert sum(1 for item in load_corpus() if item.exposure is not None) >= 3

    # Beaucoup de réponses anciennes : ni le jeu le plus lu, ni le plus récemment lu n'est en tête.
    item, order = exposed("exposition-anciennes-recentes")
    most_read = max(order, key=lambda name: len(readers(item, name)))
    latest = max(order, key=lambda name: max(answer.at for answer in readers(item, name)))
    assert order[0] not in (most_read, latest)
    assert order[-1] == most_read

    # Une réponse à beaucoup de destinataires passe devant plusieurs réponses à peu.
    item, order = exposed("exposition-destinataires")
    assert [len(readers(item, name)) for name in order] == sorted(
        len(readers(item, name)) for name in order
    )
    (first,) = readers(item, order[0])
    assert len(first.recipients) >= 10

    # Une colonne lue par une seule réponse est la plus exposée de sa table.
    item, order = exposed("exposition-colonne-unique")
    assert item.exposure is not None
    columns = dict(item.exposure.columns)
    rates = next(dataset for dataset in columns if dataset.name == order[0])
    top = columns[rates][0]
    named = [
        run.answer
        for run in item.runs
        if run.answer is not None
        and any(top in read.columns for call in run.calls for read in call.reads)
    ]
    assert len(named) == 1
    # Les colonnes à égalité se suivent par leur nom.
    assert list(columns[rates][1:]) == sorted(columns[rates][1:])


def test_it_covers_the_four_precision_levels_transitivity_and_discarded_answers() -> None:
    corpus = load_corpus()
    assert {item.level for item in corpus} == set(LEVELS)
    transitive = [item for item in corpus if item.expected("indirect")]
    assert len(transitive) >= 2
    assert sum(1 for item in corpus if item.discarded) >= 4
    # Chaque niveau plus fin que la table a au moins une réponse écartée : c'est
    # là que se mesure la précision.
    for level in ("columns", "subset", "keys"):
        assert any(item.discarded for item in corpus if item.level == level)
    assert any(not item.touched for item in corpus)


def test_every_answer_of_a_scenario_has_its_truth() -> None:
    for item in load_corpus():
        classified = (
            [expected.answer for expected in item.touched]
            + [answer for answer, _ in item.discarded]
            + list(item.untouched)
        )
        assert sorted(answer.key for answer in classified) == sorted(
            answer.key for answer in item.answers
        )
        for expected in item.touched:
            assert expected.confidence in ("certaine", "probable", "possible")
            assert expected.path == "direct" or expected.confidence != "certaine"
        for _, reason in item.discarded:
            assert reason


def test_a_scenario_added_without_truth_fails_the_check(directory: Path) -> None:
    added = scenario("table-fenetre")
    del added["truth"]
    (directory / "nouveau.json").write_text(json.dumps(added), encoding="utf-8")

    assert check_corpus(directory) == ["nouveau : aucune vérité associée"]
    with pytest.raises(CorpusError) as refused:
        load_corpus(directory)
    assert refused.value.problems == ["nouveau : aucune vérité associée"]


def forget_an_answer(raw: Scenario) -> None:
    raw["truth"]["untouched"].remove("autre-table")


def classify_an_answer_twice(raw: Scenario) -> None:
    raw["truth"]["untouched"].append("matin")


def call_touched_an_answer_that_read_too_late(raw: Scenario) -> None:
    raw["truth"]["untouched"].remove("heure-de-la-correction")
    raw["truth"]["touched"].append({"answer": "heure-de-la-correction", "confidence": "certaine"})


def call_untouched_an_answer_that_read_the_table(raw: Scenario) -> None:
    raw["truth"]["touched"] = [
        item for item in raw["truth"]["touched"] if item["answer"] != "matin"
    ]
    raw["truth"]["untouched"].append("matin")


def discard_an_answer_that_never_read_the_table(raw: Scenario) -> None:
    raw["truth"]["untouched"].remove("autre-table")
    raw["truth"]["discarded"].append({"answer": "autre-table", "reason": "colonnes disjointes"})


def declare_the_wrong_level(raw: Scenario) -> None:
    raw["level"] = "columns"


def invent_a_level(raw: Scenario) -> None:
    raw["level"] = "ligne"


def invent_a_confidence(raw: Scenario) -> None:
    raw["truth"]["touched"][0]["confidence"] = "sûre"


def empty_the_window(raw: Scenario) -> None:
    raw["incident"]["to"] = raw["incident"]["from"]


def let_a_failed_run_answer(raw: Scenario) -> None:
    raw["runs"][-1]["answer"] = {
        "id": "x",
        "at": "2026-11-03T11:01:00Z",
        "channel": "c",
        "recipients": [],
    }
    raw["truth"]["untouched"].append("x")


@pytest.mark.parametrize(
    ("mutate", "problem"),
    [
        (forget_an_answer, "la réponse « autre-table » n'a pas de vérité"),
        (classify_an_answer_twice, "la réponse « matin » est classée plusieurs fois"),
        (call_touched_an_answer_that_read_too_late, "rien n'a lu la table pendant la fenêtre"),
        (call_untouched_an_answer_that_read_the_table, "la description du scénario la relie"),
        (discard_an_answer_that_never_read_the_table, "elle n'est pas touchée"),
        (declare_the_wrong_level, "l'incident est décrit au niveau « table »"),
        (invent_a_level, "niveau inconnu « ligne »"),
        (invent_a_confidence, "confiance inconnue"),
        (empty_the_window, "la fenêtre de l'incident est vide"),
        (let_a_failed_run_answer, "a échoué et ne peut pas avoir répondu"),
    ],
)
def test_a_truth_the_scenario_contradicts_fails_the_check(
    mutate: Callable[[Scenario], None], problem: str
) -> None:
    raw = scenario("table-fenetre")
    mutate(raw)
    problems = check_scenario("essai", raw)
    assert any(problem in found for found in problems), problems
    assert all(found.startswith("essai : ") for found in problems)


def call_direct_an_indirect_answer(raw: Scenario) -> None:
    del raw["truth"]["touched"][1]["path"]


def call_certain_an_indirect_answer(raw: Scenario) -> None:
    raw["truth"]["touched"][1]["confidence"] = "certaine"


def call_indirect_an_answer_read_before_the_note_was_written(raw: Scenario) -> None:
    raw["truth"]["untouched"].remove("note-lue-avant-son-ecriture")
    raw["truth"]["touched"].append(
        {"answer": "note-lue-avant-son-ecriture", "confidence": "probable", "path": "indirect"}
    )


def call_indirect_an_answer_that_read_the_table(raw: Scenario) -> None:
    raw["truth"]["touched"][0] = {
        "answer": "ecrit-la-note",
        "confidence": "probable",
        "path": "indirect",
    }


def forget_the_answer_that_read_the_note(raw: Scenario) -> None:
    raw["truth"]["touched"].pop(1)
    raw["truth"]["untouched"].append("lit-la-note")


def invent_a_path(raw: Scenario) -> None:
    raw["truth"]["touched"][1]["path"] = "détourné"


@pytest.mark.parametrize(
    ("mutate", "problem"),
    [
        (call_direct_an_indirect_answer, "rien n'a lu la table pendant la fenêtre"),
        (call_certain_an_indirect_answer, "est indirecte : au plus probable"),
        (
            call_indirect_an_answer_read_before_the_note_was_written,
            "aucune sortie d'un run touché ne la précède",
        ),
        (call_indirect_an_answer_that_read_the_table, "son chemin est direct"),
        (forget_the_answer_that_read_the_note, "la description du scénario la relie"),
        (invent_a_path, "chemin inconnu"),
    ],
)
def test_transitivity_only_goes_forward_in_time(
    mutate: Callable[[Scenario], None], problem: str
) -> None:
    raw = scenario("transitivite-memoire")
    mutate(raw)
    problems = check_scenario("essai", raw)
    assert any(problem in found for found in problems), problems


def name_an_unknown_dataset(raw: Scenario) -> None:
    raw["incident"]["dataset"] = "inconnu"


def drop_the_time_zone(raw: Scenario) -> None:
    raw["runs"][0]["start"] = "2026-11-03T09:00:00"


def give_columns_as_text(raw: Scenario) -> None:
    raw["runs"][0]["calls"][0]["reads"][0]["columns"] = "taux"


def name_an_unknown_answer(raw: Scenario) -> None:
    raw["truth"]["untouched"].append("fantome")


def describe_a_subset_as_a_list(raw: Scenario) -> None:
    raw["incident"]["subset"] = ["USD"]


def name_an_unknown_parent(raw: Scenario) -> None:
    raw["runs"][0]["parent"] = "fantome"


def name_two_runs_alike(raw: Scenario) -> None:
    raw["runs"][1]["id"] = raw["runs"][0]["id"]


def discard_without_reason(raw: Scenario) -> None:
    raw["truth"]["untouched"].remove("heure-de-la-correction")
    raw["truth"]["touched"] = [
        item for item in raw["truth"]["touched"] if item["answer"] != "matin"
    ]
    raw["truth"]["discarded"].append({"answer": "matin", "reason": " "})
    raw["truth"]["untouched"].append("heure-de-la-correction")


@pytest.mark.parametrize(
    ("mutate", "problem"),
    [
        (name_an_unknown_dataset, "forme invalide (KeyError"),
        (drop_the_time_zone, "forme invalide (ValueError"),
        (give_columns_as_text, "forme invalide (TypeError"),
        (name_an_unknown_answer, "forme invalide (KeyError"),
        (describe_a_subset_as_a_list, "forme invalide (TypeError"),
        (name_an_unknown_parent, "désigne un parent inconnu"),
        (name_two_runs_alike, "deux runs portent le même identifiant"),
        (discard_without_reason, "est écartée sans motif"),
    ],
)
def test_a_malformed_scenario_fails_the_check(
    mutate: Callable[[Scenario], None], problem: str
) -> None:
    raw = scenario("table-fenetre")
    mutate(raw)
    problems = check_scenario("essai", raw)
    assert any(problem in found for found in problems), problems


def swap_the_two_first_datasets(raw: Scenario) -> None:
    order = raw["truth"]["exposure"]["datasets"]
    order[0], order[1] = order[1], order[0]


def forget_an_exposed_dataset(raw: Scenario) -> None:
    raw["truth"]["exposure"]["datasets"].pop()


def name_a_dataset_twice(raw: Scenario) -> None:
    raw["truth"]["exposure"]["datasets"].append(raw["truth"]["exposure"]["datasets"][0])


def rank_a_dataset_no_answer_depends_on(raw: Scenario) -> None:
    # Seul un run qui n'a pas répondu lit les encaissements.
    raw["truth"]["exposure"]["datasets"].append("encaissements")


def score_at_the_time_of_the_old_answers(raw: Scenario) -> None:
    # Le lendemain des réponses anciennes, ce sont elles qui pèsent le plus.
    raw["truth"]["exposure"]["at"] = "2026-08-04T12:00:00Z"


@pytest.mark.parametrize(
    ("mutate", "problem"),
    [
        (swap_the_two_first_datasets, "l'ordre d'exposition des jeux de données contredit"),
        (forget_an_exposed_dataset, "ne nomme pas exactement les jeux de données"),
        (name_a_dataset_twice, "figure deux fois dans l'ordre d'exposition"),
        (rank_a_dataset_no_answer_depends_on, "ne nomme pas exactement les jeux de données"),
        (
            score_at_the_time_of_the_old_answers,
            "l'ordre d'exposition des jeux de données contredit",
        ),
    ],
)
def test_an_exposure_order_the_scenario_contradicts_fails_the_check(
    mutate: Callable[[Scenario], None], problem: str
) -> None:
    raw = scenario("exposition-anciennes-recentes")
    assert check_scenario("essai", raw) == []
    mutate(raw)
    problems = check_scenario("essai", raw)
    assert any(problem in found for found in problems), problems


def swap_two_columns(raw: Scenario) -> None:
    order = raw["truth"]["exposure"]["columns"]["taux"]
    order[0], order[1] = order[1], order[0]


def put_the_tied_columns_out_of_their_name_order(raw: Scenario) -> None:
    order = raw["truth"]["exposure"]["columns"]["taux"]
    order[1], order[2] = order[2], order[1]


def forget_the_columns_of_a_dataset(raw: Scenario) -> None:
    del raw["truth"]["exposure"]["columns"]["factures"]


def forget_a_column(raw: Scenario) -> None:
    raw["truth"]["exposure"]["columns"]["factures"].pop()


def invent_a_column(raw: Scenario) -> None:
    raw["truth"]["exposure"]["columns"]["factures"].append("echeance")


@pytest.mark.parametrize(
    ("mutate", "problem"),
    [
        (swap_two_columns, "l'ordre d'exposition des colonnes de « finance.ref.taux_change »"),
        (
            put_the_tied_columns_out_of_their_name_order,
            "l'ordre d'exposition des colonnes de « finance.ref.taux_change »",
        ),
        (forget_the_columns_of_a_dataset, "les colonnes attendues de « finance.compta.factures »"),
        (forget_a_column, "les colonnes attendues de « finance.compta.factures »"),
        (invent_a_column, "les colonnes attendues de « finance.compta.factures »"),
    ],
)
def test_a_column_order_the_scenario_contradicts_fails_the_check(
    mutate: Callable[[Scenario], None], problem: str
) -> None:
    raw = scenario("exposition-colonne-unique")
    assert check_scenario("essai", raw) == []
    mutate(raw)
    problems = check_scenario("essai", raw)
    assert any(problem in found for found in problems), problems


def give_the_order_as_text(raw: Scenario) -> None:
    raw["truth"]["exposure"] = "stocks, commandes, factures"


def give_the_columns_as_a_list(raw: Scenario) -> None:
    raw["truth"]["exposure"]["columns"] = ["montant"]


def drop_the_time_zone_of_the_reference(raw: Scenario) -> None:
    raw["truth"]["exposure"]["at"] = "2026-11-01T12:00:00"


def rank_an_unknown_dataset(raw: Scenario) -> None:
    raw["truth"]["exposure"]["datasets"].append("fantome")


@pytest.mark.parametrize(
    ("mutate", "problem"),
    [
        (give_the_order_as_text, "forme invalide (TypeError"),
        (give_the_columns_as_a_list, "forme invalide (TypeError"),
        (drop_the_time_zone_of_the_reference, "forme invalide (ValueError"),
        (rank_an_unknown_dataset, "forme invalide (KeyError"),
    ],
)
def test_a_malformed_exposure_order_fails_the_check(
    mutate: Callable[[Scenario], None], problem: str
) -> None:
    raw = scenario("exposition-anciennes-recentes")
    mutate(raw)
    problems = check_scenario("essai", raw)
    assert any(problem in found for found in problems), problems


def test_a_scenario_is_a_json_object_and_a_corpus_is_readable(tmp_path: Path) -> None:
    assert check_scenario("essai", ["pas", "un", "objet"]) == [
        "essai : un scénario est un objet JSON"
    ]
    (tmp_path / "casse.json").write_text("{", encoding="utf-8")
    assert check_corpus(tmp_path) == ["corpus illisible (JSONDecodeError)"]


def test_a_corpus_must_cover_every_level_transitivity_and_discarded_answers(
    tmp_path: Path,
) -> None:
    (tmp_path / "seul.json").write_text(json.dumps(scenario("table-fenetre")), encoding="utf-8")
    assert check_corpus(tmp_path) == [
        "aucun scénario au niveau « columns »",
        "aucun scénario au niveau « subset »",
        "aucun scénario au niveau « keys »",
        "aucun scénario de transitivité",
        "aucun scénario avec une réponse écartée",
        "aucun scénario avec un ordre d'exposition",
    ]


def test_the_version_of_the_corpus_is_tied_to_its_content(directory: Path) -> None:
    """Changer un scénario ou sa vérité demande une nouvelle version du corpus."""
    assert corpus_digest() == recorded_digest()
    assert corpus_digest(directory) == recorded_digest(CORPUS_VERSION)
    changed = scenario("colonnes")
    changed["title"] += " (modifié)"
    (directory / "colonnes.json").write_text(json.dumps(changed), encoding="utf-8")
    assert corpus_digest(directory) != recorded_digest()


def test_scenarios_carry_pseudonyms_and_fingerprints_never_names() -> None:
    first, second = load_corpus(), load_corpus()
    assert first == second
    fingerprints = [answer.fingerprint for item in first for answer in item.answers]
    assert len(set(fingerprints)) == len(fingerprints)
    for item in first:
        for answer in item.answers:
            assert re.fullmatch(r"sha256:[0-9a-f]{64}", answer.fingerprint)
            assert re.fullmatch(r"[0-9a-f]{16}", answer.message_ref)
            assert all(re.fullmatch(r"[0-9a-f]{32}", pseudonym) for pseudonym in answer.recipients)


def test_events_of_a_scenario_tell_its_runs_in_the_order_of_time() -> None:
    item = next(found for found in load_corpus() if found.name == "transitivite-chaine")
    events = item.events()
    times = [event["eventTime"] for event in events]
    assert times == sorted(times)
    assert len(events) == sum(2 + len(run.calls) for run in item.runs)

    by_run = {run.key: run for run in item.runs}
    derived = by_run["calcule-la-derivee"]
    closing = next(
        event
        for event in events
        if event["run"]["runId"] == str(derived.run_id) and event["eventType"] == "COMPLETE"
    )
    # Une sortie qui n'est pas une réponse est une écriture.
    assert closing["outputs"] == [{"namespace": "bigquery", "name": "finance.marts.tresorerie"}]
    assert closing["eventTime"] == "2026-11-03T09:02:00.000000Z"

    reader = by_run["lit-la-note"]
    read = next(
        event
        for event in events
        if event["run"]["facets"].get("parent", {}).get("run") == {"runId": str(reader.run_id)}
    )
    (note,) = read["inputs"]
    assert note["facets"]["datasetType"]["datasetType"] == "memory"


def test_a_delegated_run_and_a_failed_run_are_told_as_such() -> None:
    corpus = {item.name: item for item in load_corpus()}
    delegated = corpus["table-sous-agent"]
    runs = {run.key: run for run in delegated.runs}
    events = delegated.events()
    child = [e for e in events if e["run"]["runId"] == str(runs["delegation-tresorerie"].run_id)]
    assert [event["eventType"] for event in child] == ["START", "COMPLETE"]
    for event in child:
        assert event["run"]["facets"]["parent"]["run"] == {"runId": str(runs["synthese"].run_id)}
        assert event["outputs"] == []

    failed = corpus["table-fenetre"]
    silent = next(run for run in failed.runs if run.failed)
    closing = [e for e in failed.events() if e["run"]["runId"] == str(silent.run_id)][-1]
    assert (closing["eventType"], closing["outputs"]) == ("FAIL", [])


def test_the_command_line_checks_the_corpus(
    directory: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["check"]) == 0
    printed = capsys.readouterr().out
    assert "colonnes : niveau columns, 3 touchée(s), 1 écartée(s), 1 hors d'atteinte\n" in printed
    assert (
        "exposition-destinataires : niveau table, 3 touchée(s), 0 écartée(s), 6 hors d'atteinte,"
        " ordre d'exposition de 3 jeu(x) de données\n"
    ) in printed
    assert f"scénarios, version {CORPUS_VERSION}.\n" in printed
    assert cli.main(["check", "--directory", str(directory)]) == 0
    capsys.readouterr()

    added = scenario("colonnes")
    del added["truth"]
    (directory / "nouveau.json").write_text(json.dumps(added), encoding="utf-8")
    assert cli.main(["check", "--directory", str(directory)]) == 1
    assert capsys.readouterr().err == (
        "nouveau : aucune vérité associée\nCorpus de vérité incohérent : 1 problème(s).\n"
    )
