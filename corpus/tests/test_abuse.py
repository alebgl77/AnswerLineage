"""Le corpus d'abus : son format, son contrôle, et les requêtes qu'il fabrique."""

from __future__ import annotations

import base64
import copy
import gzip
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from answerlineage_corpus import cli
from answerlineage_corpus.abuse import (
    ABUSE_VERSION,
    BASES_FILE,
    CANARY,
    CASES_DIRECTORY,
    FAMILIES,
    MIN_BLOCKED,
    OPEN_FILE,
    PROXY_REFUSALS,
    SURFACES,
    VERDICTS,
    Base,
    abuse_digest,
    abuse_texts,
    check_abuse_corpus,
    check_bases,
    check_case,
    healthy_play,
    load_abuse_corpus,
    needles,
    play_of,
    recorded_abuse_digest,
)
from answerlineage_corpus.abuse_requests import INCIDENT, RECIPES, AlterationError, build
from answerlineage_corpus.scenarios import CorpusError

Raw = dict[str, Any]
CORPUS = load_abuse_corpus()
ZERO_WIDTH = chr(0x200B)


def parsed(name: str) -> Any:
    return json.loads(abuse_texts()[name])


def bases() -> dict[str, Base]:
    found, problems = check_bases(parsed(BASES_FILE))
    assert problems == []
    return found


def raw_case(identifier: str) -> Raw:
    family, name = identifier.split("/")
    cases = parsed(f"{CASES_DIRECTORY}/{family}.json")["cases"]
    found: Raw = copy.deepcopy(next(case for case in cases if case["name"] == name))
    return found


@pytest.fixture
def directory(tmp_path: Path) -> Path:
    """Une copie du corpus, que le test peut modifier."""
    (tmp_path / CASES_DIRECTORY).mkdir()
    for name, text in abuse_texts().items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path


def rewrite(directory: Path, name: str, change: Callable[[Any], Any]) -> None:
    """Réécrit un fichier de la copie ; ``change`` modifie son contenu, ou en rend un autre."""
    path = directory / name
    content = json.loads(path.read_text(encoding="utf-8"))
    changed = change(content)
    path.write_text(json.dumps(content if changed is None else changed), encoding="utf-8")


def test_the_abuse_corpus_is_coherent() -> None:
    assert check_abuse_corpus() == []
    assert CORPUS.version == ABUSE_VERSION
    identifiers = [case.id for case in CORPUS]
    assert len(set(identifiers)) == len(identifiers) >= 80
    for case in CORPUS:
        assert case.surface == case.base.surface
        assert case.verdict in VERDICTS[case.surface]


def test_it_covers_the_required_families_with_three_blocked_cases_each() -> None:
    """Injections dans les identifiants, SQL pathologique, lots surdimensionnés, caractères
    invisibles, usurpation d'émetteur, écart entre en-têtes et corps, sorties du lignage,
    champs hors contrat."""
    assert FAMILIES == (
        "identifiants",
        "sql",
        "lots",
        "invisibles",
        "emetteur",
        "entetes",
        "capture",
        "champs",
    )
    for family in FAMILIES:
        blocked = [case for case in CORPUS if case.family == family and not case.is_open]
        assert len(blocked) >= MIN_BLOCKED >= 3, family


def test_it_covers_every_surface_and_every_verdict() -> None:
    played = {(case.surface, case.verdict) for case in CORPUS}
    assert played == {(surface, verdict) for surface in SURFACES for verdict in VERDICTS[surface]}
    # Un refus du proxy ne porte que sur Host, Origin et les requêtes d'une autre page.
    refused = {
        case.expect["code"]
        for case in CORPUS
        if (case.surface, case.verdict) == ("proxy", "refuse")
    }
    assert refused == set(PROXY_REFUSALS)


def test_a_hostile_value_carries_the_canary_that_is_searched_afterwards() -> None:
    searched = [case for case in CORPUS if needles(case)]
    assert len(searched) >= 60
    for case in searched:
        assert needles(case)[0] == case.canary
        assert case.canary in json.dumps(case.alter, ensure_ascii=False)
    encoded = next(case for case in CORPUS if case.id == "entetes/nom-contredit-en-base64")
    assert needles(encoded) == (
        CANARY,
        "=?base64?" + base64.b64encode(CANARY.encode()).decode() + "?=",
    )
    # Une recette sans valeur ne laisse rien à chercher, un nombre non plus : il ne porte
    # pas de marque.
    assert needles(next(case for case in CORPUS if case.id == "lots/lot-1001")) == ()
    counters = next(case for case in CORPUS if case.id == "emetteur/releve-compteurs-incoherents")
    assert (counters.alter["value"], needles(counters)) == (5000, ())


def test_open_cases_carry_their_reason_and_do_not_count_as_blocked() -> None:
    declared = {item["case"]: item["reason"] for item in parsed(OPEN_FILE)}
    assert {case.id: case.open_reason for case in CORPUS.open_cases} == declared
    assert all(reason.strip() for reason in declared.values())
    assert all(not case.is_open for case in CORPUS if case.id not in declared)


def drop_the_expectation(raw: Raw) -> None:
    del raw["expect"]


def invent_a_verdict(raw: Raw) -> None:
    raw["expect"]["verdict"] = "ignore"


def borrow_the_verdict_of_another_surface(raw: Raw) -> None:
    raw["expect"] = {"verdict": "neutralise", "code": "invalidToolName"}


def invent_a_surface(raw: Raw) -> None:
    raw["surface"] = "console"


def name_an_unknown_base(raw: Raw) -> None:
    raw["base"] = "ingestion/inconnue"


def name_the_base_of_another_surface(raw: Raw) -> None:
    raw["base"] = "proxy/appel-outil"


def invent_a_recipe(raw: Raw) -> None:
    raw["alter"] = {"recipe": "surcharger", "count": 3}


def alter_two_things(raw: Raw) -> None:
    raw["alter"]["header"] = "content-type"


def alter_nothing(raw: Raw) -> None:
    del raw["alter"]


def give_an_alteration_something_else_to_carry(raw: Raw) -> None:
    raw["alter"]["note"] = "à revoir"


def point_nowhere(raw: Raw) -> None:
    raw["alter"]["field"] = "/body/0/inputs/7/name"


def forget_the_canary(raw: Raw) -> None:
    raw["alter"]["value"] = "<script>alert(1)</script>"


def choose_a_canary_the_value_does_not_carry(raw: Raw) -> None:
    raw["canary"] = "autre-marque"


def leave_the_canary_blank(raw: Raw) -> None:
    raw["canary"] = " "


def drop_the_code(raw: Raw) -> None:
    del raw["expect"]["code"]


def add_an_unknown_expectation(raw: Raw) -> None:
    raw["expect"]["severity"] = "haute"


def give_the_rank_as_text(raw: Raw) -> None:
    raw["expect"]["index"] = "premier"


def drop_the_title(raw: Raw) -> None:
    raw["title"] = " "


def add_an_unknown_field(raw: Raw) -> None:
    raw["severity"] = "haute"


@pytest.mark.parametrize(
    ("mutate", "problem"),
    [
        (drop_the_expectation, "aucune attente"),
        (invent_a_verdict, "verdict inconnu pour la surface « ingestion »"),
        (borrow_the_verdict_of_another_surface, "verdict inconnu pour la surface « ingestion »"),
        (invent_a_surface, "surface inconnue"),
        (name_an_unknown_base, "base inconnue"),
        (name_the_base_of_another_surface, "n'est pas une requête de la surface du cas"),
        (invent_a_recipe, "recette inconnue"),
        (alter_two_things, "et une seule chose"),
        (alter_nothing, "aucune altération"),
        (give_an_alteration_something_else_to_carry, "rien d'autre"),
        (point_nowhere, "altération inapplicable (le pointeur sort d'un tableau)"),
        (forget_the_canary, "la valeur hostile ne porte pas le canari du cas"),
        (choose_a_canary_the_value_does_not_carry, "la valeur hostile ne porte pas le canari"),
        (leave_the_canary_blank, "le canari est un texte non vide"),
        (drop_the_code, "l'attente ne dit pas « code »"),
        (add_an_unknown_expectation, "l'attente porte un champ inconnu « severity »"),
        (give_the_rank_as_text, "le rang d'un événement rejeté est un nombre entier"),
        (drop_the_title, "aucun titre"),
        (add_an_unknown_field, "champ inconnu « severity »"),
    ],
)
def test_a_malformed_case_fails_the_check(mutate: Callable[[Raw], None], problem: str) -> None:
    raw = raw_case("identifiants/jeu-balise")
    assert check_case("essai", raw, bases()) == []
    mutate(raw)
    problems = check_case("essai", raw, bases())
    assert any(problem in found for found in problems), problems
    assert all(found.startswith("essai/jeu-balise : ") for found in problems)


@pytest.mark.parametrize(
    ("identifier", "expect", "problem"),
    [
        # La capture ne bloque jamais un agent : le proxy ne refuse rien d'autre.
        (
            "identifiants/outil-balise",
            {"verdict": "refuse", "status": 403, "code": "tool_not_allowed"},
            "il ne refuse que pour Host, Origin et les requêtes d'une autre page",
        ),
        (
            "emetteur/hote-relaye-proxy",
            {"verdict": "refuse", "status": 400, "code": "host_not_allowed"},
            "un refus de la surface « proxy » porte l'un des statuts 403",
        ),
        (
            "identifiants/outil-balise",
            {"verdict": "neutralise", "code": "blocked"},
            "une requête neutralisée se reconnaît à : invalidToolName, headerMismatch, unresolved",
        ),
        (
            "entetes/nom-contredit",
            {"verdict": "neutralise", "code": "headerMismatch", "headers": []},
            "nomme les en-têtes contredits",
        ),
        (
            "sql/sql-dynamique",
            {"verdict": "neutralise", "code": "unresolved"},
            "une lecture non résolue dit son motif",
        ),
        (
            "capture/lot-json-rpc",
            {"verdict": "compte", "code": "ignored"},
            "une requête comptée l'est comme non inspectée",
        ),
        (
            "lots/lot-1001",
            {"verdict": "refuse", "status": 500, "code": "batch_too_large"},
            "un refus de la surface « ingestion » porte l'un des statuts 400, 403, 413, 415",
        ),
        (
            "lots/lot-1001",
            {"verdict": "refuse", "status": 413, "code": " "},
            "un refus dit son code",
        ),
        (
            "lots/corps-trop-grand",
            {"verdict": "refuse", "status": 413, "code": "body_too_large", "read": "whole"},
            "« read » ne prend que la valeur « partial »",
        ),
        (
            "invisibles/message-caractere-nul",
            {"verdict": "refuse", "status": 422, "code": "string_pattern_mismatch"},
            "un refus 422, et lui seul, dit le chemin du paramètre fautif",
        ),
        (
            "identifiants/jeu-balise",
            {"verdict": "rejete", "index": 0, "path": 3, "code": "identifier_markup"},
            "un rejet dit le chemin du champ fautif et son code",
        ),
        (
            "lots/parents-mutuels",
            {"verdict": "borne", "status": 200},
            "une lecture bornée qui répond dit si elle est tronquée",
        ),
        (
            "lots/lecture-hors-delai",
            {"verdict": "borne", "status": 503},
            "une lecture interrompue dit son code",
        ),
        (
            "lots/lecture-hors-delai",
            {"verdict": "borne", "status": 500, "code": "query_timeout"},
            "une lecture bornée répond 200, ou 503 quand son délai est dépassé",
        ),
        (
            "emetteur/forwarded-sans-effet",
            {"verdict": "sans_effet", "code": "202"},
            "l'attente porte un champ inconnu « code »",
        ),
        (
            "identifiants/adresse-balise-propager",
            {"verdict": "inerte"},
            "une adresse hostile ramène un message fixe",
        ),
        ("identifiants/jeu-balise", ["rejete"], "l'attente est un objet JSON"),
    ],
)
def test_an_expectation_out_of_the_closed_lists_fails_the_check(
    identifier: str, expect: Any, problem: str
) -> None:
    raw = raw_case(identifier)
    assert check_case("essai", raw, bases()) == []
    raw["expect"] = expect
    problems = check_case("essai", raw, bases())
    assert any(problem in found for found in problems), problems


def test_a_case_is_a_named_json_object() -> None:
    assert check_case("essai", ["pas", "un", "objet"], bases()) == [
        "essai : un cas est un objet JSON"
    ]
    unnamed = raw_case("identifiants/jeu-balise")
    unnamed["name"] = "Jeu Balise"
    (problem,) = check_case("essai", unnamed, bases())
    assert problem.startswith("essai : un cas porte un nom")


def empty_a_family(content: Any) -> None:
    del content["cases"][2:]


def open_all_but_two_cases(content: Any) -> None:
    content.extend(
        {"case": f"entetes/{name}", "reason": "pas encore bloqué"}
        for name in ("nom-contredit", "nom-contredit-en-base64", "nom-en-base64-illisible")
    )


def test_a_family_with_fewer_than_three_blocked_cases_fails_the_check(directory: Path) -> None:
    rewrite(directory, f"{CASES_DIRECTORY}/entetes.json", empty_a_family)
    assert check_abuse_corpus(directory) == [
        "famille « entetes » : 2 cas bloqué(s), 3 exigés (un cas ouvert ne compte pas)"
    ]


def test_an_open_case_does_not_count_as_blocked(directory: Path) -> None:
    rewrite(directory, OPEN_FILE, open_all_but_two_cases)
    assert check_abuse_corpus(directory) == [
        "famille « entetes » : 2 cas bloqué(s), 3 exigés (un cas ouvert ne compte pas)"
    ]


def test_a_required_family_that_is_missing_fails_the_check(directory: Path) -> None:
    (directory / CASES_DIRECTORY / "capture.json").unlink()
    assert check_abuse_corpus(directory) == ["famille exigée absente : « capture »"]
    with pytest.raises(CorpusError) as refused:
        load_abuse_corpus(directory)
    assert refused.value.problems == ["famille exigée absente : « capture »"]


def open_without_reason(content: Any) -> None:
    content[0]["reason"] = " "


def open_an_unknown_case(content: Any) -> None:
    content.append({"case": "emetteur/inconnu", "reason": "pas encore bloqué"})


def open_a_case_twice(content: Any) -> None:
    content.append(dict(content[0]))


def open_without_naming_a_case(content: Any) -> None:
    content.append({"reason": "pas encore bloqué"})


def describe_exceptions_as_an_object(content: Any) -> Any:
    return {"exceptions": content}


@pytest.mark.parametrize(
    ("mutate", "problem"),
    [
        (open_without_reason, "emetteur/evenement-au-nom-du-proxy : cas ouvert sans motif"),
        (open_an_unknown_case, f"{OPEN_FILE} : exception pour un cas qui n'est pas au corpus"),
        (
            open_a_case_twice,
            "emetteur/evenement-au-nom-du-proxy : deux exceptions pour le même cas",
        ),
        (open_without_naming_a_case, f"{OPEN_FILE} : une exception nomme son cas"),
        (describe_exceptions_as_an_object, f"{OPEN_FILE} : un tableau d'exceptions est attendu"),
    ],
)
def test_a_malformed_exception_fails_the_check(
    directory: Path, mutate: Callable[[Any], Any], problem: str
) -> None:
    rewrite(directory, OPEN_FILE, mutate)
    problems = check_abuse_corpus(directory)
    assert any(found.startswith(problem) for found in problems), problems


def test_an_invisible_character_written_as_such_fails_the_check(directory: Path) -> None:
    """La revue voit ce qui s'exécute : un caractère invisible s'écrit en échappement JSON."""
    path = directory / CASES_DIRECTORY / "invisibles.json"
    escaped = path.read_text(encoding="utf-8")
    assert escaped.count("\\u200b") == 5
    assert ZERO_WIDTH not in escaped
    path.write_text(escaped.replace("\\u200b", ZERO_WIDTH, 1), encoding="utf-8")
    # Le contenu n'a pas changé : seule son écriture est refusée.
    assert abuse_digest(directory) == recorded_abuse_digest()
    assert check_abuse_corpus(directory) == [
        f"{CASES_DIRECTORY}/invisibles.json : caractère invisible écrit tel quel, ligne 9 "
        "(U+200B) : l'écrire en échappement JSON"
    ]


@pytest.mark.parametrize("code_point", [0x0009, 0x00A0, 0x00AD, 0x0301, 0x202E, 0x3164, 0xFE0F])
def test_what_a_review_would_not_see_is_never_written_as_such(
    directory: Path, code_point: int
) -> None:
    path = directory / OPEN_FILE
    written = path.read_text(encoding="utf-8").replace(" : ", chr(code_point) + ": ", 1)
    path.write_text(written, encoding="utf-8")
    assert check_abuse_corpus(directory)[0] == (
        f"{OPEN_FILE} : caractère invisible écrit tel quel, ligne 4 (U+{code_point:04X}) : "
        "l'écrire en échappement JSON"
    )
    # Les fichiers du paquet, eux, ne portent que des caractères visibles.
    assert all(chr(code_point) not in text for text in abuse_texts().values())


def weaken_an_expectation(content: Any) -> None:
    content["cases"][0]["expect"]["code"] = "payload_too_large"


def drop_a_case(content: Any) -> None:
    del content["cases"][0]


def close_an_open_case(content: Any) -> None:
    del content[0]


@pytest.mark.parametrize(
    ("name", "mutate"),
    [
        (f"{CASES_DIRECTORY}/lots.json", weaken_an_expectation),
        (f"{CASES_DIRECTORY}/identifiants.json", drop_a_case),
        (OPEN_FILE, close_an_open_case),
    ],
    ids=["attente affaiblie", "cas retiré", "exception retirée"],
)
def test_a_content_that_changes_without_a_new_version_fails_the_check(
    directory: Path, name: str, mutate: Callable[[Any], Any]
) -> None:
    """Affaiblir un cas demande une nouvelle version : l'empreinte est liée au contenu."""
    assert abuse_digest() == abuse_digest(directory) == recorded_abuse_digest(ABUSE_VERSION)
    rewrite(directory, name, mutate)
    (problem,) = check_abuse_corpus(directory)
    assert problem.startswith("le corpus d'abus a changé sans nouvelle version")
    assert abuse_digest(directory) in problem
    with pytest.raises(CorpusError):
        load_abuse_corpus(directory)


def test_the_digest_does_not_depend_on_the_layout_of_the_files(directory: Path) -> None:
    rewrite(directory, BASES_FILE, lambda content: None)
    assert abuse_digest(directory) == recorded_abuse_digest()
    assert check_abuse_corpus(directory) == []


def test_two_cases_cannot_share_a_name_and_a_file_must_be_readable(directory: Path) -> None:
    rewrite(
        directory,
        f"{CASES_DIRECTORY}/capture.json",
        lambda content: content["cases"].append(dict(content["cases"][0])),
    )
    assert check_abuse_corpus(directory) == ["capture/lot-json-rpc : deux cas portent ce nom"]

    (directory / CASES_DIRECTORY / "capture.json").write_text(
        '{"title": "a", "title": "b"}', "utf-8"
    )
    assert check_abuse_corpus(directory) == [
        f"{CASES_DIRECTORY}/capture.json : JSON illisible (ValueError : clé répétée)"
    ]
    (directory / CASES_DIRECTORY / "capture.json").write_text('{"title": "sans cas"}', "utf-8")
    assert check_abuse_corpus(directory) == [
        "capture : une famille porte un titre (« title ») et ses cas (« cases »)"
    ]
    (directory / OPEN_FILE).unlink()
    assert check_abuse_corpus(directory) == ["corpus d'abus illisible (FileNotFoundError)"]


def test_a_family_is_named_like_a_case(directory: Path) -> None:
    (directory / CASES_DIRECTORY / "Nouvelle Famille.json").write_text(
        '{"title": "x", "cases": []}', "utf-8"
    )
    assert check_abuse_corpus(directory) == [
        "Nouvelle Famille : le nom d'une famille est fait de minuscules et de chiffres"
    ]


def give_a_base_an_unknown_surface(content: Any) -> None:
    content["bases"]["proxy/appel-outil"]["surface"] = "console"


def name_a_base_after_another_surface(content: Any) -> None:
    content["bases"]["lecture/appel-outil"] = content["bases"].pop("proxy/appel-outil")


def drop_the_healthy_outcome(content: Any) -> None:
    del content["bases"]["proxy/appel-outil"]["healthy"]


def describe_the_request_as_text(content: Any) -> None:
    content["bases"]["proxy/appel-outil"]["request"] = "POST /mcp"


def describe_the_headers_as_a_list(content: Any) -> None:
    content["bases"]["proxy/appel-outil"]["request"]["headers"] = [["accept", "*/*"]]


def name_an_unknown_seed(content: Any) -> None:
    content["bases"]["lecture/graphe"]["seed"] = "inconnu"


def describe_the_settings_as_a_list(content: Any) -> None:
    content["bases"]["proxy/appel-outil"]["settings"] = ["crm"]


def give_a_base_an_unknown_field(content: Any) -> None:
    content["bases"]["proxy/appel-outil"]["severity"] = "haute"


def describe_the_incidents_as_an_object(content: Any) -> None:
    content["bases"]["lecture/incident"]["incidents"] = {"severity": "low"}


def describe_an_incident_as_text(content: Any) -> None:
    content["bases"]["lecture/incident"]["incidents"] = ["incident sur la table des taux"]


def read_an_incident_the_base_does_not_declare(content: Any) -> None:
    del content["bases"]["lecture/incident"]["incidents"]


@pytest.mark.parametrize(
    ("mutate", "problem"),
    [
        (give_a_base_an_unknown_surface, "base « proxy/appel-outil » invalide (ValueError"),
        (name_a_base_after_another_surface, "base « lecture/appel-outil » invalide (ValueError"),
        (drop_the_healthy_outcome, "base « proxy/appel-outil » invalide (KeyError"),
        (describe_the_request_as_text, "base « proxy/appel-outil » invalide (TypeError"),
        (describe_the_headers_as_a_list, "base « proxy/appel-outil » invalide (TypeError"),
        (name_an_unknown_seed, "base « lecture/graphe » invalide (KeyError"),
        (describe_the_settings_as_a_list, "base « proxy/appel-outil » invalide (TypeError"),
        (give_a_base_an_unknown_field, "base « proxy/appel-outil » invalide (ValueError"),
        (describe_the_incidents_as_an_object, "base « lecture/incident » invalide (TypeError"),
        (describe_an_incident_as_text, "base « lecture/incident » invalide (TypeError"),
        (
            read_an_incident_the_base_does_not_declare,
            "base « lecture/incident » invalide (ValueError : le chemin désigne un incident que "
            "la base ne déclare pas)",
        ),
        (lambda content: [], f"{BASES_FILE} : un objet JSON qui porte « bases » est attendu"),
    ],
)
def test_a_malformed_base_fails_the_check(
    directory: Path, mutate: Callable[[Any], Any], problem: str
) -> None:
    rewrite(directory, BASES_FILE, mutate)
    problems = check_abuse_corpus(directory)
    assert any(problem in found for found in problems), problems


REQUEST: Raw = {
    "method": "POST",
    "path": "/mcp",
    "headers": {"content-type": "application/json", "mcp-name": "outil"},
    "body": {"method": "tools/call", "params": {"name": "outil", "arguments": {"q": "SELECT 1"}}},
}
SEED: tuple[Raw, ...] = ({"eventTime": "t0", "run": {"runId": "a"}}, {"eventTime": "t1"})
REPORT: Raw = {"emitter": "mcp://crm", "calls_seen": 1}


def altered(alter: Raw, request: Raw = REQUEST) -> Any:
    return build(request, SEED, {"server": "crm"}, alter, reports=(REPORT,))


def test_a_healthy_request_goes_out_as_it_is_described() -> None:
    play = build(REQUEST, SEED, {"server": "crm"})
    assert (play.method, play.path, play.announced, play.slow) == ("POST", "/mcp", True, False)
    assert play.headers == (("content-type", "application/json"), ("mcp-name", "outil"))
    assert json.loads(play.body) == REQUEST["body"]
    assert (play.seed, play.settings) == (SEED, {"server": "crm"})
    # Une lecture : ses paramètres vont dans le chemin, encodés ; pour l'explorateur, dans
    # le fragment de l'adresse.
    read = {"path": "/api/v1/impact", "query": {"name": "a b", "from": "2026-11-03T06:00:00Z"}}
    assert build(read).path == "/api/v1/impact?name=a+b&from=2026-11-03T06%3A00%3A00Z"
    assert (build(read).method, build(read).body) == ("GET", b"")
    assert build(read, fragment=True).path.startswith("#/api/v1/impact?name=a+b")


DECLARATION: Raw = {
    "scope": {"datasets": [{"namespace": "bigquery", "name": "finance.ref.taux_change"}]},
    "window": {"from": "2026-11-03T06:00:00Z", "to": "2026-11-03T16:00:00Z"},
    "severity": "low",
}


def test_a_read_follows_the_incidents_its_base_declares() -> None:
    """L'API tire l'identifiant d'un incident à sa déclaration : le chemin d'une lecture le
    désigne par une marque, que remplace l'identifiant du premier incident déclaré."""
    read = {"path": f"/api/v1/incidents/{INCIDENT}", "query": {"limit": "5"}}
    play = build(read, incidents=(DECLARATION,))
    assert play.incidents == (DECLARATION,)
    assert play.path == "/api/v1/incidents/{incident}?limit=5"
    assert (build(read).incidents, altered({"recipe": "compresser"}).incidents) == ((), ())
    # Un incident déclaré s'altère comme un événement envoyé avant la lecture.
    severe = build(
        read,
        alter={"field": "/incidents/0/severity", "value": "critical"},
        incidents=(DECLARATION,),
    )
    assert severe.incidents[0]["severity"] == "critical"
    assert DECLARATION["severity"] == "low"


def test_altering_a_field_or_a_header_changes_nothing_else() -> None:
    field = altered({"field": "/body/params/name", "value": "autre"})
    assert json.loads(field.body)["params"] == {"name": "autre", "arguments": {"q": "SELECT 1"}}
    assert field.headers == build(REQUEST).headers
    header = altered({"header": "Mcp-Name", "value": "autre"})
    assert dict(header.headers)["mcp-name"] == "autre"
    assert header.body == build(REQUEST).body
    # Une clé nouvelle s'ajoute, « ~1 » écrit une barre oblique dans un nom.
    added = altered({"field": "/body/params/a~1b", "value": 1})
    assert json.loads(added.body)["params"]["a/b"] == 1
    replayed = altered({"field": "/seed/1/eventTime", "value": "t2"})
    assert [event["eventTime"] for event in replayed.seed] == ["t0", "t2"]
    # La requête de la base, elle, n'a pas bougé.
    assert REQUEST["body"]["params"]["name"] == "outil"


def test_recipes_repeat_nest_inflate_and_compress() -> None:
    batch = {
        "path": "/lot",
        "headers": {},
        "body": [{"inputs": [1, 2], "facets": {"a": 0, "b": 1}}],
    }
    assert (
        len(json.loads(altered({"recipe": "repeter", "field": "/body", "count": 5}, batch).body))
        == 5
    )
    inputs = altered({"recipe": "repeter", "field": "/body/0/inputs", "count": 5}, batch)
    assert json.loads(inputs.body)[0]["inputs"] == [1, 2, 1, 2, 1]
    facets = altered({"recipe": "repeter", "field": "/body/0/facets", "count": 4}, batch)
    assert json.loads(facets.body)[0]["facets"] == {"a": 0, "b": 1, "a_2": 0, "a_3": 0}
    longer = altered(
        {"recipe": "repeter", "field": "/body/params/arguments/q", "motif": " OR 1", "count": 2}
    )
    assert json.loads(longer.body)["params"]["arguments"]["q"] == "SELECT 1 OR 1 OR 1"

    nested = altered(
        {
            "recipe": "imbriquer",
            "field": "/body/params/arguments/q",
            "open": "(",
            "close": ")",
            "depth": 3,
        }
    )
    assert json.loads(nested.body)["params"]["arguments"]["q"] == "(((SELECT 1)))"
    wrapped = altered(
        {"recipe": "imbriquer", "field": "/body", "open": "[", "close": "]", "depth": 2}
    )
    assert json.loads(wrapped.body) == [[REQUEST["body"]]]

    healthy = build(REQUEST).body
    padded = altered({"recipe": "gonfler", "bytes": 500})
    assert (len(padded.body), padded.body.rstrip(), padded.announced) == (500, healthy, True)
    streamed = altered({"recipe": "gonfler", "bytes": 500, "at": "debut", "announce": False})
    assert (len(streamed.body), streamed.body.lstrip(), streamed.announced) == (500, healthy, False)

    compressed = altered({"recipe": "compresser"})
    assert gzip.decompress(compressed.body) == healthy
    assert dict(compressed.headers)["content-encoding"] == "gzip"
    bomb = altered({"recipe": "bombe-gzip", "bytes": 1_000_000})
    assert len(bomb.body) < 5_000
    assert len(gzip.decompress(bomb.body)) == 1_000_000
    assert gzip.decompress(bomb.body).rstrip() == healthy


def test_recipes_duplicate_a_key_move_the_host_encode_slow_down_and_replay() -> None:
    twice = altered({"recipe": "dupliquer-cle", "field": "/body/params/name", "value": "autre"})
    assert twice.body.count(b'"name":') == 2
    # La valeur saine d'abord : un analyseur qui garde la dernière lit l'autre.
    assert json.loads(twice.body)["params"]["name"] == "autre"
    assert (
        json.loads(twice.body, object_pairs_hook=lambda pairs: dict(reversed(pairs)))
        == REQUEST["body"]
    )
    batch = {"path": "/lot", "body": [{"job": {"name": "a"}}, {"job": {"name": "b"}}]}
    second = altered({"recipe": "dupliquer-cle", "field": "/body/1/job/name", "value": "c"}, batch)
    assert second.body == b'[{"job":{"name":"a"}},{"job":{"name":"b","name":"c"}}]'

    moved = dict(
        altered(
            {"recipe": "deplacer-hote", "host": "ailleurs.example", "to": "x-forwarded-host"}
        ).headers
    )
    assert (moved["host"], moved["x-forwarded-host"]) == ("ailleurs.example", "localhost")
    forwarded = dict(
        altered({"recipe": "deplacer-hote", "host": "ailleurs.example", "to": "forwarded"}).headers
    )
    assert (forwarded["host"], forwarded["forwarded"]) == ("ailleurs.example", "host=localhost")

    encoded = dict(
        altered({"recipe": "encoder-base64", "header": "Mcp-Name", "value": "autre"}).headers
    )
    assert encoded["mcp-name"] == "=?base64?YXV0cmU=?="

    slow = altered({"recipe": "ralentir", "timeout_ms": 50})
    assert (slow.slow, slow.settings) == (True, {"server": "crm", "read_timeout_ms": 50})

    again = altered({"recipe": "rejouer", "event": "/seed/0"})
    assert again.seed == (*SEED, SEED[0])
    earlier = altered(
        {"recipe": "rejouer", "event": "/seed/0", "field": "/eventTime", "value": "t-1"}
    )
    assert earlier.seed[2] == {"eventTime": "t-1", "run": {"runId": "a"}}
    assert earlier.seed[:2] == SEED
    # Un relevé de couverture se rejoue comme un événement, à la suite des relevés.
    inflated = altered(
        {"recipe": "rejouer", "event": "/reports/0", "field": "/calls_seen", "value": 9}
    )
    assert inflated.reports == (REPORT, {**REPORT, "calls_seen": 9})
    assert (inflated.seed, altered({"recipe": "compresser"}).reports) == (SEED, (REPORT,))


@pytest.mark.parametrize(
    ("alter", "reason"),
    [
        ({"field": "body/params", "value": 1}, "un pointeur JSON commence par « / »"),
        ({"field": "", "value": 1}, "le pointeur désigne toute la requête"),
        ({"field": "/body/params/name/x", "value": 1}, "le pointeur traverse une valeur simple"),
        ({"field": "/body/absent/x", "value": 1}, "le pointeur ne désigne rien"),
        ({"header": "mcp-name", "value": 3}, "un en-tête et sa valeur sont des textes"),
        ({"recipe": "repeter", "field": "/body"}, "« count » manque"),
        ({"recipe": "repeter", "field": "/body", "count": 2, "size": 1}, "« size » inconnu"),
        ({"recipe": "repeter", "field": "/body", "count": 0}, "« count » est un nombre entier"),
        ({"recipe": "repeter", "field": "/body", "count": True}, "« count » est un nombre entier"),
        ({"recipe": "repeter", "field": "/body/method", "count": 2}, "seul un tableau ou un objet"),
        (
            {"recipe": "repeter", "field": "/body/params", "motif": "x", "count": 2},
            "un motif se répète à la suite d'un texte",
        ),
        (
            {"recipe": "repeter", "field": "/body/method", "motif": "", "count": 2},
            "« motif » est un texte non vide",
        ),
        (
            {"recipe": "imbriquer", "field": "/body/params", "open": "(", "close": ")", "depth": 2},
            "seul un texte, ou le corps entier, s'imbrique",
        ),
        ({"recipe": "gonfler", "bytes": 10}, "le corps de la base dépasse déjà cette taille"),
        ({"recipe": "gonfler", "bytes": 500, "at": "milieu"}, "« at » vaut « debut » ou « fin »"),
        ({"recipe": "gonfler", "bytes": 500, "announce": "non"}, "« announce » vaut true ou false"),
        ({"recipe": "dupliquer-cle", "field": "/headers/mcp-name", "value": "x"}, "dans le corps"),
        ({"recipe": "dupliquer-cle", "field": "/body", "value": "x"}, "ne désigne pas une clé"),
        ({"recipe": "dupliquer-cle", "field": "/body/absent", "value": "x"}, "aucune clé"),
        ({"recipe": "deplacer-hote", "host": "a.example", "to": "via"}, "un en-tête de relais"),
        ({"recipe": "rejouer", "event": "/body"}, "envoyé avant la lecture"),
        ({"recipe": "rejouer", "event": "/seed/4"}, "le pointeur sort d'un tableau"),
        ({"recipe": 3}, "recette inconnue"),
    ],
)
def test_an_alteration_that_does_not_apply_says_why(alter: Raw, reason: str) -> None:
    with pytest.raises(AlterationError, match=reason):
        altered(alter)


def test_every_recipe_of_the_closed_list_is_played_by_a_case() -> None:
    played = {case.alter["recipe"] for case in CORPUS if "recipe" in case.alter}
    assert played == set(RECIPES)


def test_every_case_builds_the_request_it_describes() -> None:
    for case in CORPUS:
        healthy, play = healthy_play(case.base), play_of(case)
        assert play.method == healthy.method
        # Une seule altération : la requête du cas diffère de la requête saine.
        assert play != healthy, case.id
        assert play.path.startswith("#/") == (case.surface == "explorateur")
    oversized = next(case for case in CORPUS if case.id == "lots/corps-trop-grand")
    assert len(play_of(oversized).body) == 16 * 1024 * 1024 + 1
    # Des blancs à la suite du document : il reste le lot de la requête saine.
    assert play_of(oversized).body.rstrip() == healthy_play(oversized.base).body


def test_the_command_line_checks_the_abuse_corpus(
    directory: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["check"]) == 0
    printed = capsys.readouterr().out
    blocked = sum(1 for case in CORPUS if case.family == "sql" and not case.is_open)
    opened = sum(1 for case in CORPUS if case.family == "sql" and case.is_open)
    assert f"sql : {blocked} cas bloqués, {opened} ouvert(s)" in printed
    assert printed.endswith(
        f"Corpus d'abus cohérent : {len(CORPUS.cases)} cas, dont {len(CORPUS.open_cases)} ouverts, "
        f"version {ABUSE_VERSION}.\n"
    )
    assert cli.main(["check", "--abuse-directory", str(directory)]) == 0
    capsys.readouterr()

    rewrite(directory, f"{CASES_DIRECTORY}/entetes.json", empty_a_family)
    assert cli.main(["check", "--abuse-directory", str(directory)]) == 1
    assert capsys.readouterr().err == (
        "famille « entetes » : 2 cas bloqué(s), 3 exigés (un cas ouvert ne compte pas)\n"
        "Corpus d'abus incohérent : 1 problème(s).\n"
    )
