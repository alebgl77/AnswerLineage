"""Corpus d'abus : juger ce qu'une surface a fait d'un cas, puis conclure.

Trois étages, pour que chacun se teste seul :

- observer : jouer la requête sur la surface et relever des faits, dans une
  ``Observation``. C'est le seul étage qui touche au produit : il vit avec les
  tests, pas ici ;
- juger : comparer l'observation à l'attente écrite du cas (``judge``), ou à
  l'issue saine de sa base (``judge_healthy``), et lister les problèmes ;
- conclure : un cas bloqué, un échec nommé, ou l'échec attendu d'un cas
  ouvert (``conclude``).

Un problème dit l'attendu et l'obtenu, « 413 batch_too_large attendu, 202
obtenu », et le lieu d'une fuite. Il ne cite jamais la valeur hostile : de ce
que le produit a rendu, seuls un nombre ou un code de forme simple s'écrivent.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from answerlineage_corpus.abuse import OPEN_FILE, Base, Case, needles

# Lieu où un événement accepté est archivé tel qu'il a été reçu. Une valeur
# admise s'y trouve donc sous la forme envoyée : il n'est fouillé que lorsque
# rien ne devait entrer.
RAW_EVENTS: Final = "événements bruts"
# Ce qu'un proxy fait dans l'ordre quand rien ne retarde l'appel.
RESPONSE_THEN_EVENT: Final = ("reponse", "evenement")

_TOOL_CALL_FACET: Final = "answerlineage_toolCall"
_SIMPLE = re.compile(r"[A-Za-z0-9_./:-]{1,80}")

Outcome = Literal["bloque", "echec", "echec_attendu"]


@dataclass(frozen=True)
class Observation:
    """Ce qu'une surface a fait d'une requête : des faits, relevés par qui l'a jouée."""

    # Statut de la réponse ; ``None`` quand rien n'est revenu dans le délai d'attente.
    status: int | None = None
    # Corps de la réponse, analysé quand c'est du JSON.
    body: Any = None
    # Où chercher la valeur hostile : nom du lieu, puis tout son texte.
    places: Mapping[str, str] = field(default_factory=dict)

    # Proxy : requêtes arrivées au serveur MCP, et si l'échange est passé octet pour octet.
    forwarded: int = 0
    identical: bool = False
    events: tuple[Mapping[str, Any], ...] = ()
    uninspected: int = 0
    # « reponse » puis « evenement », dans l'ordre où le proxy les a produits.
    order: tuple[str, ...] = ()

    # Ingestion : octets envoyés et octets que l'application a lus ; événements
    # enregistrés ; file avant puis après le passage d'un worker ; lignes du graphe.
    sent: int = 0
    consumed: int = 0
    stored: int = 0
    queued: int = 0
    pending: int = 0
    graph: Mapping[str, int] = field(default_factory=dict)

    # Explorateur : ce que portent les nœuds, le texte de la page, les erreurs de la
    # console, et les éléments qu'une adresse aurait fait naître.
    nodes: tuple[str, ...] = ()
    text: str = ""
    errors: int = 0
    injected: int = 0


@dataclass(frozen=True)
class Conclusion:
    outcome: Outcome
    message: str


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _items(value: object) -> Sequence[Any]:
    return value if isinstance(value, list | tuple) else ()


def _shown(value: object, hidden: Sequence[str]) -> str:
    """Ce qui s'écrit d'une valeur rendue par le produit : un nombre, ou un code simple."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str) and _SIMPLE.fullmatch(value) and not any(n in value for n in hidden):
        return value
    return "une valeur non affichée"


def _answered(seen: Observation, hidden: Sequence[str]) -> str:
    """Le statut obtenu, suivi des codes que la réponse porte."""
    if seen.status is None:
        return "aucune réponse"
    body = _mapping(seen.body)
    codes = [body["error"]] if "error" in body else []
    rejections = [*_items(body.get("rejected")), body]
    codes.extend(
        _mapping(error).get("type")
        for rejection in rejections
        for error in _items(_mapping(rejection).get("errors"))
    )
    return " ".join([str(seen.status), *dict.fromkeys(_shown(code, hidden) for code in codes)])


def _differences(expected: Any, actual: Any, hidden: Sequence[str], where: str = "") -> list[str]:
    """Écarts entre un contenu attendu et un contenu obtenu.

    Un objet attendu est inclus dans l'objet obtenu, qui peut porter d'autres
    champs ; un tableau a le même nombre d'éléments, comparés un à un ; toute
    autre valeur est égale.
    """
    place = where or "/"
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{place} : un objet attendu, {_shown(actual, hidden)} obtenu"]
        return [
            problem
            for key, value in expected.items()
            for problem in (
                _differences(value, actual[key], hidden, f"{where}/{key}")
                if key in actual
                else [f"{where}/{key} attendu, absent"]
            )
        ]
    if isinstance(expected, list):
        count = len(actual) if isinstance(actual, list) else 0
        if not isinstance(actual, list) or count != len(expected):
            return [f"{place} : {len(expected)} élément(s) attendu(s), {count} obtenu(s)"]
        return [
            problem
            for rank, value in enumerate(expected)
            for problem in _differences(value, actual[rank], hidden, f"{where}/{rank}")
        ]
    if type(expected) is not type(actual) or expected != actual:
        return [f"{place} : {_shown(expected, ())} attendu, {_shown(actual, hidden)} obtenu"]
    return []


def _census(graph: Mapping[str, int]) -> str:
    return ", ".join(f"{table} {count}" for table, count in sorted(graph.items())) or "vide"


def _untouched(seen: Observation) -> list[str]:
    """Rien n'est entré : ni événement enregistré, ni file, ni graphe."""
    problems = []
    if seen.stored:
        problems.append(f"aucun événement enregistré attendu, {seen.stored} obtenu(s)")
    if seen.queued or seen.pending:
        problems.append(f"file vide attendue, {max(seen.queued, seen.pending)} en file obtenu(s)")
    if seen.graph:
        problems.append(f"graphe inchangé attendu, {_census(seen.graph)} obtenu")
    return problems


def _tool_call(event: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(_mapping(_mapping(event.get("run")).get("facets")).get(_TOOL_CALL_FACET))


def _relayed(seen: Observation) -> list[str]:
    if seen.forwarded != 1:
        return [f"une requête relayée attendue, {seen.forwarded} obtenue(s)"]
    if not seen.identical:
        return ["relais à l'identique attendu, requête ou réponse modifiée obtenue"]
    return []


def _one_event(seen: Observation) -> list[str]:
    problems = []
    if len(seen.events) != 1:
        problems.append(f"un événement attendu, {len(seen.events)} obtenu(s)")
    elif seen.order != RESPONSE_THEN_EVENT:
        # Rien ne retarde un appel : la réponse est rendue avant que l'événement soit émis.
        problems.append("événement émis après la réponse attendu, avant elle obtenu")
    if seen.uninspected:
        problems.append("requête inspectée attendue, comptée comme non inspectée obtenue")
    return problems


def _described(base: Base, event: Mapping[str, Any], hidden: Sequence[str]) -> list[str]:
    """L'événement décrit l'appel du corps de la requête saine : son outil, sa révision, sa
    lecture."""
    facet, healthy = _tool_call(event), base.healthy
    problems = []
    described = (facet.get("tool"), _mapping(event.get("job")).get("name"))
    if described != (healthy["tool"], healthy["tool"]) or facet.get("invalidToolName") is not False:
        obtained = _shown(facet.get("tool"), hidden)
        problems.append(f"outil {healthy['tool']} décrit attendu, {obtained} obtenu")
    if facet.get("protocolVersion") != healthy.get("protocol"):
        obtained = _shown(facet.get("protocolVersion"), hidden)
        problems.append(f"révision {healthy.get('protocol')} décrite attendue, {obtained} obtenue")
    if "reads" in healthy:
        names = [_mapping(dataset).get("name") for dataset in _items(event.get("inputs"))]
        if facet.get("readResolution") != "resolved" or names != healthy["reads"]:
            obtained = ", ".join(_shown(name, hidden) for name in names) or "aucune table"
            problems.append(
                f"lecture résolue de {', '.join(healthy['reads'])} attendue, "
                f"{_shown(facet.get('readResolution'), hidden)} ({obtained}) obtenue"
            )
    return problems


def _neutralised(case: Case, seen: Observation, hidden: Sequence[str]) -> list[str]:
    problems = [*_relayed(seen), *_one_event(seen)]
    if len(seen.events) != 1:
        return problems
    event, expect = seen.events[0], case.expect
    facet = _tool_call(event)
    mismatches = facet.get("headerMismatch")
    if expect["code"] == "invalidToolName":
        if facet.get("invalidToolName") is not True or "tool" in facet:
            problems.append("invalidToolName attendu, outil décrit obtenu")
    elif expect["code"] == "headerMismatch":
        expected = sorted(expect["headers"])
        if mismatches != expected:
            obtained = (
                ", ".join(_shown(name, hidden) for name in _items(mismatches)) or "aucun écart"
            )
            problems.append(f"headerMismatch {', '.join(expected)} attendu, {obtained} obtenu")
        # Le corps décide : l'appel décrit est celui de la requête saine.
        problems.extend(_described(case.base, event, hidden))
    else:
        resolution = (facet.get("readResolution"), facet.get("unresolvedReason"))
        if resolution != ("unresolved", expect["reason"]) or event.get("inputs"):
            shown = " ".join(_shown(value, hidden) for value in resolution if value is not None)
            problems.append(
                f"lecture non résolue ({expect['reason']}) attendue, "
                f"{shown or 'aucune lecture décrite'} obtenue"
            )
        # L'appel reste décrit : c'est sa lecture qui ne l'est pas.
        if facet.get("tool") != case.base.healthy["tool"]:
            problems.append(f"outil {case.base.healthy['tool']} décrit attendu, un autre obtenu")
    return problems


def _counted(case: Case, seen: Observation, hidden: Sequence[str]) -> list[str]:
    problems = _relayed(seen)
    if seen.events:
        # Décrire un appel d'après une lecture douteuse ferait entrer un outil que le
        # serveur n'a peut-être pas vu.
        problems.append(f"aucun événement attendu, {len(seen.events)} obtenu(s)")
    if seen.uninspected != 1:
        problems.append(f"uninspected 1 attendu, {seen.uninspected} obtenu")
    return problems


def _refused(case: Case, seen: Observation, hidden: Sequence[str]) -> list[str]:
    expect = case.expect
    body = _mapping(seen.body)
    if "path" in expect:
        expected = f"{expect['status']} {expect['code']} à {expect['path']}"
        refused = any(
            (_mapping(error).get("path"), _mapping(error).get("type"))
            == (expect["path"], expect["code"])
            for error in _items(body.get("errors"))
        )
    else:
        expected = f"{expect['status']} {expect['code']}"
        refused = seen.body == {"error": expect["code"]}
    problems = []
    if seen.status != expect["status"] or not refused:
        problems.append(f"{expected} attendu, {_answered(seen, hidden)} obtenu")
    if case.surface == "proxy":
        if seen.forwarded:
            problems.append(f"aucune requête relayée attendue, {seen.forwarded} obtenue(s)")
        if seen.events:
            problems.append(f"aucun événement attendu, {len(seen.events)} obtenu(s)")
    if case.surface == "ingestion":
        problems.extend(_untouched(seen))
        if "read" in expect and seen.consumed >= seen.sent:
            problems.append("lecture du corps interrompue attendue, corps lu en entier obtenu")
    return problems


def _rejected(case: Case, seen: Observation, hidden: Sequence[str]) -> list[str]:
    expect = case.expect
    body = _mapping(seen.body)
    where = f"à {expect['path']}" if expect["path"] else "pour l'événement entier"
    if "index" in expect:
        # Le reste du lot est accepté : seul l'événement altéré est rejeté.
        accepted = _stored_events(case.base) - 1
        expected = f"rejet de l'événement {expect['index']}, {expect['code']} {where}"
        rejections = _items(body.get("rejected"))
        errors = [
            error
            for rejection in rejections
            if _mapping(rejection).get("index") == expect["index"]
            for error in _items(_mapping(rejection).get("errors"))
        ]
        answered = (
            seen.status == (202 if accepted else 422)
            and body.get("accepted") == accepted
            and len(rejections) == 1
        )
    else:
        accepted = 0
        expected = f"rejet {expect['code']} {where}"
        errors = list(_items(body.get("errors")))
        answered = seen.status == 422
    found = any(
        (_mapping(error).get("path"), _mapping(error).get("type"))
        == (expect["path"], expect["code"])
        for error in errors
    )
    problems = []
    if not (answered and found):
        problems.append(f"{expected} attendu, {_answered(seen, hidden)} obtenu")
    if accepted == 0:
        problems.extend(_untouched(seen))
    else:
        if seen.stored != accepted:
            problems.append(
                f"{accepted} événement(s) enregistré(s) attendu(s), {seen.stored} obtenu(s)"
            )
        if seen.pending:
            problems.append(f"file vidée par le worker attendue, {seen.pending} en file obtenu(s)")
    return problems


def _bounded(case: Case, seen: Observation, hidden: Sequence[str]) -> list[str]:
    expect = case.expect
    if expect["status"] != 200:
        expected = f"{expect['status']} {expect['code']}"
        if seen.status != expect["status"] or seen.body != {"error": expect["code"]}:
            return [f"{expected} attendu, {_answered(seen, hidden)} obtenu"]
        return []
    if seen.status != 200:
        return [f"réponse 200 dans le délai attendue, {_answered(seen, hidden)} obtenu"]
    truncated = _mapping(seen.body).get("truncated")
    if truncated is not expect["truncated"]:
        expected = _shown(expect["truncated"], ())
        return [f"truncated {expected} attendu, {_shown(truncated, hidden)} obtenu"]
    if truncated:
        return []
    # Une lecture qui se dit complète rend ce que rend la requête saine.
    return _differences(_mapping(case.base.healthy.get("body")), seen.body, hidden)


def _inert(case: Case, seen: Observation, hidden: Sequence[str]) -> list[str]:
    problems = []
    if case.expect["message"] not in seen.text:
        problems.append("message fixe attendu, absent de la page")
    if seen.errors:
        problems.append(f"aucune erreur dans la console attendue, {seen.errors} obtenue(s)")
    if seen.injected:
        problems.append(f"aucun élément né de l'adresse attendu, {seen.injected} obtenu(s)")
    return problems


def _stored_events(base: Base) -> int:
    """Événements que la requête saine fait enregistrer : ceux qu'elle dit acceptés, un
    pour un événement envoyé seul, ou le nombre que la base déclare (``events``)."""
    accepted = _mapping(base.healthy.get("body")).get("accepted", 1)
    return int(base.healthy.get("events", accepted))


def _as_healthy(base: Base, seen: Observation, hidden: Sequence[str]) -> list[str]:
    """Écarts entre l'observation et l'issue saine de la base."""
    healthy = base.healthy
    if base.surface == "proxy":
        problems = [*_relayed(seen), *_one_event(seen)]
        if len(seen.events) == 1:
            problems.extend(_described(base, seen.events[0], hidden))
            if _tool_call(seen.events[0]).get("headerMismatch") != []:
                problems.append("aucun écart d'en-têtes attendu, un écart obtenu")
        return problems
    if base.surface == "explorateur":
        problems = [
            f"« {text} » attendu sur la page, absent"
            for text in healthy.get("shows", ())
            if text not in seen.text
        ]
        if len(seen.nodes) != healthy["nodes"]:
            problems.append(f"{healthy['nodes']} nœuds attendus, {len(seen.nodes)} obtenu(s)")
        if seen.errors:
            problems.append(f"aucune erreur dans la console attendue, {seen.errors} obtenue(s)")
        return problems
    if seen.status != healthy["status"]:
        return [f"{healthy['status']} attendu, {_answered(seen, hidden)} obtenu"]
    problems = _differences(healthy.get("body", {}), seen.body, hidden)
    if base.surface == "ingestion":
        accepted = _stored_events(base)
        if (seen.stored, seen.queued) != (accepted, accepted):
            problems.append(
                f"{accepted} événement(s) enregistré(s) et mis en file attendu(s), "
                f"{seen.stored} et {seen.queued} obtenu(s)"
            )
        if seen.pending:
            problems.append(f"file vidée par le worker attendue, {seen.pending} en file obtenu(s)")
        if dict(seen.graph) != dict(healthy["graph"]):
            problems.append(
                f"graphe {_census(healthy['graph'])} attendu, {_census(seen.graph)} obtenu"
            )
    return problems


def _unchanged(case: Case, seen: Observation, hidden: Sequence[str]) -> list[str]:
    return [f"sans effet : {problem}" for problem in _as_healthy(case.base, seen, hidden)]


_JUDGES: Final = {
    "neutralise": _neutralised,
    "compte": _counted,
    "refuse": _refused,
    "rejete": _rejected,
    "borne": _bounded,
    "sans_effet": _unchanged,
    "inerte": _inert,
}


def judge(case: Case, seen: Observation) -> list[str]:
    """Problèmes d'un cas joué ; la liste est vide quand il est bloqué comme il le déclare."""
    searched = needles(case)
    # Le canari ne s'écrit dans aucun message, même pour un cas qui ne le cherche pas.
    problems = _JUDGES[case.verdict](case, seen, (*searched, case.canary))
    # Un événement accepté est archivé tel qu'il a été reçu : l'archive n'est fouillée
    # que lorsque rien ne devait entrer.
    spared = (RAW_EVENTS,) if case.verdict in ("sans_effet", "borne") else ()
    leaked = sorted(
        place
        for place, text in seen.places.items()
        if place not in spared and any(needle in text for needle in searched)
    )
    if leaked:
        problems.append("la valeur hostile a fui : " + ", ".join(leaked))
    return problems


def judge_healthy(base: Base, seen: Observation) -> list[str]:
    """Problèmes de la requête saine d'une base ; la liste est vide quand elle passe."""
    return _as_healthy(base, seen, ())


def conclude(case: Case, problems: Sequence[str]) -> Conclusion:
    """Le sort d'un cas : bloqué, échec nommé, ou échec attendu d'un cas ouvert.

    Un cas ouvert qui se trouve bloqué est un échec : la protection est
    arrivée, son exception doit être retirée.
    """
    found = " ; ".join(problems)
    if not case.is_open:
        if problems:
            return Conclusion("echec", f"{case.id} : {found}")
        return Conclusion("bloque", f"{case.id} : bloqué")
    if problems:
        return Conclusion("echec_attendu", f"{case.id} : cas ouvert, {found}. {case.open_reason}")
    return Conclusion(
        "echec",
        f"{case.id} : ce cas ouvert est désormais bloqué, retirer son exception de {OPEN_FILE}",
    )
