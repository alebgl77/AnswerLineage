"""Juger et conclure, sur des observations fabriquées : aucune surface n'est jouée ici."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from answerlineage_corpus.abuse import CANARY, Case, load_abuse_corpus
from answerlineage_corpus.abuse_verdict import (
    RAW_EVENTS,
    RESPONSE_THEN_EVENT,
    Observation,
    conclude,
    judge,
    judge_healthy,
)

CORPUS = load_abuse_corpus()
FINGERPRINT = "sha256:5d1c0e6b9f3a4872d0c1b2a39e8f7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c"
TAUX = "finance.ref.taux_change"


def case(identifier: str) -> Case:
    return next(found for found in CORPUS if found.id == identifier)


def event(tool: str | None = "rechercher_comptes", **facet: Any) -> dict[str, Any]:
    """L'événement d'un appel d'outil, réduit à ce que le jugement lit."""
    call: dict[str, Any] = {
        "invalidToolName": tool is None,
        "headerMismatch": [],
        "protocolVersion": "2026-07-28",
        **facet,
    }
    if tool is not None:
        call["tool"] = tool
    inputs = [{"namespace": "bigquery", "name": name} for name in call.pop("reads", [])]
    return {
        "run": {"facets": {"answerlineage_toolCall": call}},
        "job": {"name": tool or "outil-non-resolu"},
        "inputs": inputs,
    }


def relayed(*events: dict[str, Any], **facts: Any) -> Observation:
    """Un appel relayé à l'identique, suivi de ses événements."""
    return Observation(
        status=200,
        forwarded=1,
        identical=True,
        events=events,
        order=RESPONSE_THEN_EVENT if events else ("reponse",),
        **facts,
    )


def rejection(path: str, code: str, index: int = 0) -> dict[str, Any]:
    return {"index": index, "errors": [{"path": path, "type": code, "message": "motif fixe"}]}


SQL_CALL = event("execute_sql", readResolution="resolved", reads=[TAUX])
IMPACT = {
    "counts": {"answers": 1, "recipients": 2, "lower_bound": False},
    "graph": {"answers": [{"id": "a", "fingerprint": FINGERPRINT}]},
    "truncated": False,
}
ONE_READ = {"run": 1, "tool": 1, "dataset": 1, "dataset_version": 1, "read_edge": 1}
MARKUP = Observation(
    status=422,
    body={
        "accepted": 0,
        "duplicates": 0,
        "rejected": [rejection("/inputs/0/name", "identifier_markup")],
    },
)

# Pour chaque verdict, ce qu'observe une surface qui bloque le cas comme il le déclare.
BLOCKED: dict[str, Observation] = {
    "identifiants/outil-instruction": relayed(event(None, headerMismatch=["mcp-name"])),
    "entetes/nom-contredit": relayed(event(headerMismatch=["mcp-name"])),
    "entetes/methode-contredite": relayed(
        event("execute_sql", readResolution="resolved", reads=[TAUX], headerMismatch=["mcp-method"])
    ),
    "sql/budget-epuise": relayed(
        event("execute_sql", readResolution="unresolved", unresolvedReason="budget_exceeded")
    ),
    "capture/lot-json-rpc": relayed(uninspected=1),
    "emetteur/hote-relaye-proxy": Observation(status=403, body={"error": "host_not_allowed"}),
    "lots/lot-1001": Observation(status=413, body={"error": "batch_too_large"}),
    "lots/corps-trop-grand": Observation(
        status=413, body={"error": "body_too_large"}, sent=17, consumed=0
    ),
    "identifiants/jeu-balise": MARKUP,
    "identifiants/evenement-seul-balise": Observation(
        status=422, body={"errors": rejection("/inputs/0/name", "identifier_markup")["errors"]}
    ),
    "invisibles/jeu-caractere-nul": Observation(
        status=422,
        body={"accepted": 0, "duplicates": 0, "rejected": [rejection("", "unstorable_text")]},
    ),
    "invisibles/deux-ecritures": Observation(
        status=202,
        body={"accepted": 2, "duplicates": 0, "rejected": []},
        stored=2,
        queued=2,
        graph={"run": 2, "tool": 1, "dataset": 1, "read_edge": 2},
        # L'événement accepté est archivé tel qu'il a été reçu : ce n'est pas une fuite.
        places={
            RAW_EVENTS: "re" + chr(0x301) + "fe" + chr(0x301) + "rentiel",
            "base": "référentiel",
        },
    ),
    "invisibles/message-caractere-nul": Observation(
        status=422,
        body={"errors": rejection("/query/message_ref", "string_pattern_mismatch")["errors"]},
    ),
    "identifiants/jeu-inconnu-balise": Observation(status=404, body={"error": "unknown_dataset"}),
    "lots/parents-mutuels": Observation(status=200, body=IMPACT, places={RAW_EVENTS: CANARY}),
    "lots/lecture-hors-delai": Observation(status=503, body={"error": "query_timeout"}),
    "identifiants/adresse-balise-propager": Observation(
        text="Propager\nCe jeu de données n'est pas connu du graphe.", places={"page": "Propager"}
    ),
}


@pytest.mark.parametrize("identifier", BLOCKED)
def test_a_case_blocked_as_it_declares_has_no_problem(identifier: str) -> None:
    assert judge(case(identifier), BLOCKED[identifier]) == []
    assert conclude(case(identifier), []).outcome == "bloque"


def not_blocked(identifier: str, **facts: Any) -> list[str]:
    """Problèmes du cas quand l'observation qui le bloque change sur un point."""
    return judge(case(identifier), replace(BLOCKED[identifier], **facts))


def test_a_proxy_case_is_neutralised_only_when_the_call_passes_and_the_event_says_so() -> None:
    tool = "identifiants/outil-instruction"
    assert not_blocked(tool, events=(event("rechercher_comptes"),)) == [
        "invalidToolName attendu, outil décrit obtenu"
    ]
    assert not_blocked(tool, forwarded=0) == ["une requête relayée attendue, 0 obtenue(s)"]
    assert not_blocked(tool, identical=False) == [
        "relais à l'identique attendu, requête ou réponse modifiée obtenue"
    ]
    assert not_blocked(tool, events=(), order=("reponse",)) == ["un événement attendu, 0 obtenu(s)"]
    # Rien ne retarde un appel : la réponse précède l'événement.
    assert not_blocked(tool, order=("evenement", "reponse")) == [
        "événement émis après la réponse attendu, avant elle obtenu"
    ]
    assert not_blocked(tool, uninspected=1) == [
        "requête inspectée attendue, comptée comme non inspectée obtenue"
    ]

    header = "entetes/nom-contredit"
    assert not_blocked(header, events=(event(),)) == [
        "headerMismatch mcp-name attendu, aucun écart obtenu"
    ]
    assert not_blocked(header, events=(event(headerMismatch=["mcp-method"]),)) == [
        "headerMismatch mcp-name attendu, mcp-method obtenu"
    ]
    # Le corps décide : l'événement décrit l'outil du corps, pas celui de l'en-tête.
    assert not_blocked(header, events=(event(CANARY, headerMismatch=["mcp-name"]),)) == [
        "outil rechercher_comptes décrit attendu, une valeur non affichée obtenu"
    ]
    assert not_blocked(
        header, events=(event(headerMismatch=["mcp-name"], protocolVersion="2025-11-25"),)
    ) == ["révision 2026-07-28 décrite attendue, 2025-11-25 obtenue"]
    unread = event("execute_sql", readResolution="unresolved", headerMismatch=["mcp-method"])
    assert not_blocked("entetes/methode-contredite", events=(unread,)) == [
        f"lecture résolue de {TAUX} attendue, unresolved (aucune table) obtenue"
    ]

    sql = "sql/budget-epuise"
    assert not_blocked(sql, events=(SQL_CALL,)) == [
        "lecture non résolue (budget_exceeded) attendue, resolved obtenue"
    ]
    assert not_blocked(sql, events=(event("execute_sql"),)) == [
        "lecture non résolue (budget_exceeded) attendue, aucune lecture décrite obtenue"
    ]
    other = event("execute_sql", readResolution="unresolved", unresolvedReason="too_large")
    assert not_blocked(sql, events=(other,)) == [
        "lecture non résolue (budget_exceeded) attendue, unresolved too_large obtenue"
    ]
    unnamed = event(None, readResolution="unresolved", unresolvedReason="budget_exceeded")
    assert not_blocked(sql, events=(unnamed,)) == [
        "outil execute_sql décrit attendu, un autre obtenu"
    ]


def test_a_request_the_proxy_cannot_read_is_counted_and_never_described() -> None:
    counted = "capture/lot-json-rpc"
    assert not_blocked(counted, uninspected=0) == ["uninspected 1 attendu, 0 obtenu"]
    assert not_blocked(counted, events=(event(),), order=RESPONSE_THEN_EVENT) == [
        "aucun événement attendu, 1 obtenu(s)"
    ]
    assert not_blocked(counted, forwarded=2) == ["une requête relayée attendue, 2 obtenue(s)"]


def test_a_refusal_is_a_status_a_code_and_nothing_let_in() -> None:
    proxy = "emetteur/hote-relaye-proxy"
    assert not_blocked(proxy, status=200, body={"result": {}}, forwarded=1, events=(event(),)) == [
        "403 host_not_allowed attendu, 200 obtenu",
        "aucune requête relayée attendue, 1 obtenue(s)",
        "aucun événement attendu, 1 obtenu(s)",
    ]
    assert not_blocked(proxy, body={"error": "origin_not_allowed"}) == [
        "403 host_not_allowed attendu, 403 origin_not_allowed obtenu"
    ]

    batch = "lots/lot-1001"
    accepted = {"accepted": 1001, "duplicates": 1000, "rejected": []}
    assert not_blocked(batch, status=202, body=accepted, stored=1, queued=1, graph=ONE_READ) == [
        "413 batch_too_large attendu, 202 obtenu",
        "aucun événement enregistré attendu, 1 obtenu(s)",
        "file vide attendue, 1 en file obtenu(s)",
        "graphe inchangé attendu, dataset 1, dataset_version 1, read_edge 1, run 1, tool 1 obtenu",
    ]
    assert not_blocked(batch, status=None, body=None) == [
        "413 batch_too_large attendu, aucune réponse obtenu"
    ]
    assert not_blocked("lots/corps-trop-grand", consumed=17) == [
        "lecture du corps interrompue attendue, corps lu en entier obtenu"
    ]

    read = "invisibles/message-caractere-nul"
    assert not_blocked(read, status=200, body={"answers": []}) == [
        "422 string_pattern_mismatch à /query/message_ref attendu, 200 obtenu"
    ]
    elsewhere = {"errors": rejection("/query/channel", "string_too_long")["errors"]}
    assert not_blocked(read, body=elsewhere) == [
        "422 string_pattern_mismatch à /query/message_ref attendu, 422 string_too_long obtenu"
    ]
    assert not_blocked("identifiants/jeu-inconnu-balise", status=500, body=None) == [
        "404 unknown_dataset attendu, 500 obtenu"
    ]


def test_a_rejection_names_the_event_its_path_and_its_code() -> None:
    rejected = "identifiants/jeu-balise"
    elsewhere = {"accepted": 0, "rejected": [rejection("/job/name", "identifier_whitespace")]}
    assert not_blocked(rejected, body=elsewhere) == [
        "rejet de l'événement 0, identifier_markup à /inputs/0/name attendu, "
        "422 identifier_whitespace obtenu"
    ]
    other_rank = {"accepted": 0, "rejected": [rejection("/inputs/0/name", "identifier_markup", 1)]}
    assert not_blocked(rejected, body=other_rank)[0].endswith(
        "attendu, 422 identifier_markup obtenu"
    )
    passed = {"accepted": 1, "duplicates": 0, "rejected": []}
    assert not_blocked(rejected, status=202, body=passed, stored=1, queued=1, graph=ONE_READ)[
        :2
    ] == [
        "rejet de l'événement 0, identifier_markup à /inputs/0/name attendu, 202 obtenu",
        "aucun événement enregistré attendu, 1 obtenu(s)",
    ]
    assert not_blocked(rejected, pending=1) == ["file vide attendue, 1 en file obtenu(s)"]

    alone = "identifiants/evenement-seul-balise"
    assert not_blocked(alone, status=201, body={"event_id": "e", "duplicate": False}, stored=1) == [
        "rejet identifier_markup à /inputs/0/name attendu, 201 obtenu",
        "aucun événement enregistré attendu, 1 obtenu(s)",
    ]
    assert not_blocked("invisibles/jeu-caractere-nul", body=MARKUP.body) == [
        "rejet de l'événement 0, unstorable_text pour l'événement entier attendu, "
        "422 identifier_markup obtenu"
    ]


def test_the_rest_of_a_batch_is_accepted_when_one_event_is_rejected() -> None:
    """Une base de plusieurs événements : seul l'événement altéré est rejeté."""
    twin = case("invisibles/deux-ecritures")
    one_of_two = replace(
        twin, expect={"verdict": "rejete", "index": 1, "path": "/inputs/0/name", "code": "x"}
    )
    body = {"accepted": 1, "duplicates": 0, "rejected": [rejection("/inputs/0/name", "x", 1)]}
    kept = Observation(status=202, body=body, stored=1, queued=1, graph=ONE_READ)
    assert judge(one_of_two, kept) == []
    assert judge(one_of_two, replace(kept, stored=2, pending=1)) == [
        "1 événement(s) enregistré(s) attendu(s), 2 obtenu(s)",
        "file vidée par le worker attendue, 1 en file obtenu(s)",
    ]
    assert judge(one_of_two, replace(kept, status=422))[0].startswith("rejet de l'événement 1, x")


def test_a_request_without_effect_gets_what_the_healthy_request_gets() -> None:
    twin = "invisibles/deux-ecritures"
    two_nodes = {"run": 2, "tool": 1, "dataset": 2, "read_edge": 2}
    assert not_blocked(twin, graph=two_nodes) == [
        "sans effet : graphe dataset 1, read_edge 2, run 2, tool 1 attendu, "
        "dataset 2, read_edge 2, run 2, tool 1 obtenu"
    ]
    refused = {"accepted": 1, "duplicates": 0, "rejected": [rejection("/inputs/0/name", "x", 1)]}
    assert not_blocked(twin, body=refused, stored=1, queued=1) == [
        "sans effet : /accepted : 2 attendu, 1 obtenu",
        "sans effet : /rejected : 0 élément(s) attendu(s), 1 obtenu(s)",
        "sans effet : 2 événement(s) enregistré(s) et mis en file attendu(s), 1 et 1 obtenu(s)",
    ]
    assert not_blocked(twin, status=422) == ["sans effet : 202 attendu, 422 obtenu"]
    assert not_blocked(twin, pending=1) == [
        "sans effet : file vidée par le worker attendue, 1 en file obtenu(s)"
    ]
    # La forme décomposée dans le graphe ferait deux nœuds : c'est une fuite.
    decomposed = "re" + chr(0x301) + "fe" + chr(0x301) + "rentiel"
    assert not_blocked(twin, places={"base": decomposed, "réponse": decomposed}) == [
        "la valeur hostile a fui : base, réponse"
    ]


def test_a_bounded_read_answers_in_time_and_says_when_it_is_truncated() -> None:
    cycle = "lots/parents-mutuels"
    assert not_blocked(cycle, status=None, body=None) == [
        "réponse 200 dans le délai attendue, aucune réponse obtenu"
    ]
    assert not_blocked(cycle, status=500, body=None) == [
        "réponse 200 dans le délai attendue, 500 obtenu"
    ]
    assert not_blocked(cycle, body={**IMPACT, "truncated": True}) == [
        "truncated false attendu, true obtenu"
    ]
    # Une lecture qui se dit complète retrouve la réponse : la boucle ne la cache pas.
    hidden = {**IMPACT, "counts": {"answers": 0, "recipients": 0, "lower_bound": False}}
    assert not_blocked(cycle, body={**hidden, "graph": {"answers": []}}) == [
        "/counts/answers : 1 attendu, 0 obtenu",
        "/counts/recipients : 2 attendu, 0 obtenu",
        "/graph/answers : 1 élément(s) attendu(s), 0 obtenu(s)",
    ]
    truncated = replace(case(cycle), expect={"verdict": "borne", "status": 200, "truncated": True})
    assert judge(truncated, Observation(status=200, body={"truncated": True})) == []

    slow = "lots/lecture-hors-delai"
    assert not_blocked(slow, status=200, body=IMPACT) == ["503 query_timeout attendu, 200 obtenu"]
    assert not_blocked(slow, status=None, body=None) == [
        "503 query_timeout attendu, aucune réponse obtenu"
    ]


def test_a_hostile_address_leaves_the_page_inert() -> None:
    address = "identifiants/adresse-balise-propager"
    assert not_blocked(address, text="Propager") == ["message fixe attendu, absent de la page"]
    assert not_blocked(address, errors=2) == [
        "aucune erreur dans la console attendue, 2 obtenue(s)"
    ]
    assert not_blocked(address, injected=1) == [
        "aucun élément né de l'adresse attendu, 1 obtenu(s)"
    ]
    assert not_blocked(
        address, places={"nœuds": f"<img onerror={CANARY}>", "page": "Propager"}
    ) == ["la valeur hostile a fui : nœuds"]


def test_the_hostile_value_is_searched_everywhere_and_never_quoted() -> None:
    """Un cas « bloqué » dont la valeur a fui est un échec, qui dit où sans dire quoi."""
    places = {"réponse": "{}", "base": f"x {CANARY} y", "journaux": f"refus de {CANARY}"}
    assert not_blocked("identifiants/jeu-balise", places={**places, RAW_EVENTS: ""}) == [
        "la valeur hostile a fui : base, journaux"
    ]
    # Rien ne devait entrer : l'archive des événements reçus est fouillée aussi.
    assert not_blocked("identifiants/jeu-balise", places={RAW_EVENTS: CANARY}) == [
        f"la valeur hostile a fui : {RAW_EVENTS}"
    ]
    encoded = case("entetes/nom-contredit-en-base64")
    seen = replace(
        BLOCKED["entetes/nom-contredit"], places={"journaux": "=?base64?Y2FuYXJpN2MxZA==?="}
    )
    assert judge(encoded, seen) == ["la valeur hostile a fui : journaux"]
    # Une recette sans valeur hostile ne laisse rien à chercher.
    assert not_blocked("lots/lot-1001", places={"base": CANARY}) == []

    # Ce que le produit rend ne s'écrit que sous la forme d'un nombre ou d'un code simple.
    echoed = not_blocked(
        "identifiants/jeu-balise",
        status=422,
        body={"accepted": 0, "rejected": [rejection("/inputs/0/name", f"<script>{CANARY}", 0)]},
    )
    assert echoed == [
        "rejet de l'événement 0, identifier_markup à /inputs/0/name attendu, "
        "422 une valeur non affichée obtenu"
    ]
    for body in ({"error": CANARY}, {"error": "x" * 200}, {"error": ["liste"]}, {"error": 7}):
        (problem,) = not_blocked("lots/lot-1001", body=body)
        assert CANARY not in problem
        assert problem.startswith("413 batch_too_large attendu, 413 ")


def test_a_case_that_is_not_blocked_fails_and_its_message_starts_with_its_name() -> None:
    batch = case("lots/lot-1001")
    problems = judge(batch, Observation(status=202, body={"accepted": 1001}))
    failure = conclude(batch, problems)
    assert failure.outcome == "echec"
    assert failure.message == "lots/lot-1001 : 413 batch_too_large attendu, 202 obtenu"
    several = conclude(batch, ["premier", "second"])
    assert several.message == "lots/lot-1001 : premier ; second"


def test_an_open_case_is_an_expected_failure_until_it_is_found_blocked() -> None:
    backdated = case("emetteur/lecture-antidatee")
    assert backdated.is_open
    missed = {**IMPACT, "counts": {"answers": 0, "recipients": 0, "lower_bound": False}}
    still_open = conclude(backdated, judge(backdated, Observation(status=200, body=missed)))
    assert still_open.outcome == "echec_attendu"
    assert still_open.message.startswith(
        "emetteur/lecture-antidatee : cas ouvert, "
        "sans effet : /counts/answers : 1 attendu, 0 obtenu"
    )
    assert still_open.message.endswith(str(backdated.open_reason))

    # La protection est arrivée : le cas échoue tant que son exception n'est pas retirée.
    now_blocked = conclude(backdated, judge(backdated, Observation(status=200, body=IMPACT)))
    assert now_blocked.outcome == "echec"
    assert now_blocked.message == (
        "emetteur/lecture-antidatee : ce cas ouvert est désormais bloqué, "
        "retirer son exception de abuse_open.json"
    )


def base(name: str) -> Any:
    return next(found for found in CORPUS.bases if found.name == name)


def test_the_healthy_request_of_a_base_passes_only_as_its_outcome_says() -> None:
    proxy = base("proxy/appel-sql")
    assert judge_healthy(proxy, relayed(SQL_CALL)) == []
    assert judge_healthy(proxy, relayed(event("execute_sql", readResolution="unresolved"))) == [
        f"lecture résolue de {TAUX} attendue, unresolved (aucune table) obtenue"
    ]
    mismatched = event("execute_sql", readResolution="resolved", reads=[TAUX], headerMismatch=["x"])
    assert judge_healthy(proxy, relayed(mismatched)) == [
        "aucun écart d'en-têtes attendu, un écart obtenu"
    ]
    assert judge_healthy(proxy, relayed(uninspected=1)) == [
        "un événement attendu, 0 obtenu(s)",
        "requête inspectée attendue, comptée comme non inspectée obtenue",
    ]

    ingestion = base("ingestion/appel-outil")
    accepted = {"accepted": 1, "duplicates": 0, "rejected": []}
    stored = Observation(status=202, body=accepted, stored=1, queued=1, graph=ONE_READ)
    assert judge_healthy(ingestion, stored) == []
    assert judge_healthy(ingestion, replace(stored, graph={})) == [
        "graphe dataset 1, dataset_version 1, read_edge 1, run 1, tool 1 attendu, vide obtenu"
    ]
    assert judge_healthy(ingestion, replace(stored, body=["liste"])) == [
        "/ : un objet attendu, une valeur non affichée obtenu"
    ]
    assert judge_healthy(ingestion, replace(stored, body={"accepted": True, "duplicates": 0})) == [
        "/accepted : 1 attendu, true obtenu",
        "/rejected attendu, absent",
    ]

    # Un relevé de couverture n'enregistre aucun événement : la base le déclare.
    statement = base("ingestion/releve")
    merged = {"emitter": "mcp://entrepot", "calls_seen": 1, "events_emitted": 1, "events_lost": 0}
    kept = Observation(status=201, body=merged, graph={"coverage_report": 1, "coverage_emitter": 1})
    assert judge_healthy(statement, kept) == []
    assert judge_healthy(statement, replace(kept, stored=1, queued=1)) == [
        "0 événement(s) enregistré(s) et mis en file attendu(s), 1 et 1 obtenu(s)"
    ]
    # Un taux se compare et s'écrit comme un nombre.
    rated = base("lecture/couverture")
    measured = {"emitter": "mcp://entrepot", "coverage": {"rate": 0.001, "calls_seen": 1000}}
    assert judge_healthy(rated, Observation(status=200, body={"emitters": [measured]})) == [
        "/emitters/0/coverage/rate : 1.0 attendu, 0.001 obtenu",
        "/emitters/0/coverage/calls_seen : 1 attendu, 1000 obtenu",
        "/emitters/0/coverage/events_in_graph attendu, absent",
        "/emitters/0/coverage/events_lost attendu, absent",
    ]

    read = base("lecture/propagation")
    assert judge_healthy(read, Observation(status=200, body=IMPACT)) == []
    assert judge_healthy(read, Observation(status=404, body={"error": "unknown_dataset"})) == [
        "200 attendu, 404 unknown_dataset obtenu"
    ]

    page = base("explorateur/propager")
    shown = Observation(text="Réponses touchées 1 Couverture du lignage", nodes=("n",) * 6)
    assert judge_healthy(page, shown) == []
    assert judge_healthy(page, replace(shown, text="Chargement…", nodes=(), errors=1)) == [
        "« Réponses touchées » attendu sur la page, absent",
        "« Couverture du lignage » attendu sur la page, absent",
        "6 nœuds attendus, 0 obtenu(s)",
        "aucune erreur dans la console attendue, 1 obtenue(s)",
    ]
