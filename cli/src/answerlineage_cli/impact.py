"""``answerlineage impact`` : les réponses et les destinataires touchés par un jeu de données faux.

La commande interroge ``GET /api/v1/impact`` et valide la réponse entière
avant d'en afficher quoi que ce soit. La sortie lisible dit le jeu de
données, la fenêtre, les décomptes, la couverture du lignage et ses trous,
la chaîne de chaque réponse, et ce que le résultat permet de conclure. La
sortie JSON est la réponse de l'API, réduite aux champs de son contrat.

Le code de sortie dit la conclusion : des réponses touchées (0), rien de
touché et le résultat est complet (1), ou le résultat ne permet pas de
conclure (3), parce qu'il est tronqué ou que la couverture n'est pas mesurée
sans trou sur toute la fenêtre.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from answerlineage_cli import shape
from answerlineage_cli.client import Api, InvalidResponseError, Received
from answerlineage_cli.exits import Exit
from answerlineage_cli.names import (
    CHANNEL_MAX,
    DATASET_NAME_MAX,
    FINGERPRINT_MAX,
    JOB_NAME_MAX,
    NAMESPACE_MAX,
    RECIPIENT_MAX,
    TOOL_MAX,
)

PATH: Final = "/api/v1/impact"
# Bornes de la route : réponses par page, longueur d'un espace de noms ou d'un nom.
MAX_LIMIT: Final = 200
MAX_DATASET_PARAMETER: Final = 500
# Une page de 200 réponses et leurs chaînes : quelques mégaoctets ; la borne
# laisse de la marge sans laisser une réponse épuiser la mémoire.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
# Une vue tient au plus les bornes de lecture du graphe.
MAX_NODES: Final = 200_000
MAX_HOLES: Final = 1_000

COVERAGE_STATUSES: Final = ("measured", "partial", "unknown")
HOLE_KINDS: Final = (
    "events_lost",
    "uninspected",
    "missing_from_graph",
    "silence",
    "before_first_report",
)
CURSOR: Final = re.compile(r"[0-9]{1,18}\.[0-9a-f]{48}")
ANSWER_ID: Final = re.compile(r"[0-9a-f]{48}")
# Un nœud se désigne dans la réponse qui le donne : la commande ne fait que le relier.
_NODE_MAX: Final = 128


def _window(value: Any, where: str) -> dict[str, str]:
    window = shape.obj(value, where, ("from", "to"))
    return {key: shape.instant(window[key], f"{where}/{key}") for key in ("from", "to")}


def _hole(value: Any, where: str) -> dict[str, Any]:
    hole = shape.obj(value, where, ("emitter", "kind", "window", "count"))
    return {
        "emitter": shape.identifier(hole["emitter"], f"{where}/emitter", NAMESPACE_MAX),
        "kind": shape.choice(hole["kind"], f"{where}/kind", HOLE_KINDS),
        "window": _window(hole["window"], f"{where}/window"),
        "count": shape.optional_count(hole["count"], f"{where}/count"),
    }


def coverage(value: Any, where: str) -> dict[str, Any]:
    """La couverture du lignage, sous la forme commune à l'impact et aux sources."""
    fields = ("status", "rate", "calls_seen", "events_in_graph", "events_lost", "uninspected")
    cover = shape.obj(value, where, (*fields, "holes", "truncated"))
    holes = shape.items(cover["holes"], f"{where}/holes", MAX_HOLES)
    return {
        "status": shape.choice(cover["status"], f"{where}/status", COVERAGE_STATUSES),
        "rate": shape.rate(cover["rate"], f"{where}/rate"),
        **{key: shape.count(cover[key], f"{where}/{key}") for key in fields[2:]},
        "holes": [_hole(item, f"{where}/holes/{rank}") for rank, item in enumerate(holes)],
        "truncated": shape.flag(cover["truncated"], f"{where}/truncated"),
    }


def _nodes(
    value: Any, where: str, fields: Mapping[str, int]
) -> tuple[list[dict[str, str]], set[str]]:
    """Des nœuds d'une colonne, et leurs identifiants ; deux nœuds n'en partagent pas un."""
    nodes = []
    seen: set[str] = set()
    for rank, item in enumerate(shape.items(value, where, MAX_NODES)):
        node = shape.obj(item, f"{where}/{rank}", ("id", *fields))
        name = shape.identifier(node["id"], f"{where}/{rank}/id", _NODE_MAX)
        if name in seen:
            raise shape.fault(f"{where}/{rank}/id")
        seen.add(name)
        nodes.append(
            {"id": name}
            | {
                key: shape.identifier(node[key], f"{where}/{rank}/{key}", bound)
                for key, bound in fields.items()
            }
        )
    return nodes, seen


def _reference(value: Any, where: str, known: set[str]) -> str:
    if not isinstance(value, str) or value not in known:
        raise shape.fault(where)
    return value


def _references(value: Any, where: str, known: set[str]) -> list[str]:
    listed = shape.items(value, where, MAX_NODES)
    return [_reference(item, f"{where}/{rank}", known) for rank, item in enumerate(listed)]


def _answer(value: Any, where: str, known: Mapping[str, set[str]]) -> dict[str, Any]:
    fields = ("id", "fingerprint", "produced_at", "agent", "channels", "recipients", "tools")
    answer = shape.obj(value, where, (*fields, "reads"))
    channels = shape.items(answer["channels"], f"{where}/channels", MAX_NODES)
    reads = shape.items(answer["reads"], f"{where}/reads", MAX_NODES)
    agent = answer["agent"]
    return {
        "id": shape.pattern(answer["id"], f"{where}/id", ANSWER_ID),
        "fingerprint": shape.identifier(
            answer["fingerprint"], f"{where}/fingerprint", FINGERPRINT_MAX
        ),
        "produced_at": shape.instant(answer["produced_at"], f"{where}/produced_at"),
        "agent": None if agent is None else _reference(agent, f"{where}/agent", known["agents"]),
        "channels": [
            shape.identifier(item, f"{where}/channels/{rank}", CHANNEL_MAX)
            for rank, item in enumerate(channels)
        ],
        "recipients": _references(answer["recipients"], f"{where}/recipients", known["recipients"]),
        "tools": _references(answer["tools"], f"{where}/tools", known["tools"]),
        "reads": [_read(item, f"{where}/reads/{rank}", known) for rank, item in enumerate(reads)],
    }


def _read(value: Any, where: str, known: Mapping[str, set[str]]) -> dict[str, str | None]:
    read = shape.obj(value, where, ("dataset", "tool"))
    tool = read["tool"]
    return {
        "dataset": _reference(read["dataset"], f"{where}/dataset", known["datasets"]),
        "tool": None if tool is None else _reference(tool, f"{where}/tool", known["tools"]),
    }


def _graph(value: Any, where: str) -> dict[str, Any]:
    columns = ("datasets", "tools", "agents", "answers", "recipients")
    graph = shape.obj(value, where, (*columns, "more", "next_cursor", "truncated"), ("known_at",))
    datasets, dataset_ids = _nodes(
        graph["datasets"],
        f"{where}/datasets",
        {"namespace": NAMESPACE_MAX, "name": DATASET_NAME_MAX, "kind": NAMESPACE_MAX},
    )
    tools, tool_ids = _nodes(
        graph["tools"], f"{where}/tools", {"server": TOOL_MAX, "name": TOOL_MAX}
    )
    agents, agent_ids = _nodes(
        graph["agents"], f"{where}/agents", {"namespace": NAMESPACE_MAX, "name": JOB_NAME_MAX}
    )
    recipients, recipient_ids = _nodes(
        graph["recipients"], f"{where}/recipients", {"pseudonym": RECIPIENT_MAX}
    )
    known = {
        "datasets": dataset_ids,
        "tools": tool_ids,
        "agents": agent_ids,
        "recipients": recipient_ids,
    }
    answers = shape.items(graph["answers"], f"{where}/answers", MAX_LIMIT)
    cursor = graph["next_cursor"]
    result: dict[str, Any] = {
        "datasets": datasets,
        "tools": tools,
        "agents": agents,
        "answers": [
            _answer(item, f"{where}/answers/{rank}", known) for rank, item in enumerate(answers)
        ],
        "recipients": recipients,
        "more": shape.flag(graph["more"], f"{where}/more"),
        "next_cursor": None
        if cursor is None
        else shape.pattern(cursor, f"{where}/next_cursor", CURSOR),
        "truncated": shape.flag(graph["truncated"], f"{where}/truncated"),
    }
    if "known_at" in graph:
        result["known_at"] = shape.instant(graph["known_at"], f"{where}/known_at")
    return result


def validate(document: Any) -> dict[str, Any]:
    """La réponse de la route, validée, réduite aux champs de son contrat."""
    fields = ("dataset", "window", "counts", "coverage", "graph", "truncated")
    result = shape.obj(document, "", fields, ("known_at",))
    dataset = shape.obj(result["dataset"], "/dataset", ("namespace", "name", "kind"))
    counts = shape.obj(result["counts"], "/counts", ("answers", "recipients", "lower_bound"))
    validated: dict[str, Any] = {
        "dataset": {
            "namespace": shape.identifier(
                dataset["namespace"], "/dataset/namespace", NAMESPACE_MAX
            ),
            "name": shape.identifier(dataset["name"], "/dataset/name", DATASET_NAME_MAX),
            "kind": shape.identifier(dataset["kind"], "/dataset/kind", NAMESPACE_MAX),
        },
        "window": _window(result["window"], "/window"),
        "counts": {
            "answers": shape.count(counts["answers"], "/counts/answers"),
            "recipients": shape.count(counts["recipients"], "/counts/recipients"),
            "lower_bound": shape.flag(counts["lower_bound"], "/counts/lower_bound"),
        },
        "coverage": coverage(result["coverage"], "/coverage"),
        "graph": _graph(result["graph"], "/graph"),
        "truncated": shape.flag(result["truncated"], "/truncated"),
    }
    if "known_at" in result:
        validated["known_at"] = shape.instant(result["known_at"], "/known_at")
    return validated


@dataclass(frozen=True)
class Query:
    """Les paramètres de la route, déjà contrôlés par la ligne de commande."""

    namespace: str
    name: str
    start: str
    end: str
    limit: int | None = None
    cursor: str | None = None
    known_at: str | None = None

    def params(self) -> dict[str, str]:
        params = {
            "namespace": self.namespace,
            "name": self.name,
            "from": self.start,
            "to": self.end,
        }
        if self.limit is not None:
            params["limit"] = str(self.limit)
        if self.cursor is not None:
            params["cursor"] = self.cursor
        if self.known_at is not None:
            params["known_at"] = self.known_at
        return params


def fetch(api: Api, query: Query) -> dict[str, Any]:
    """La propagation, validée ; ``RefusedError`` quand l'API refuse la requête."""
    received = api.get(PATH, params=query.params(), max_bytes=MAX_RESPONSE_BYTES)
    if received.status != 200:
        raise RefusedError(received.status, *_refusal(received))
    return validate(received.json())


class RefusedError(Exception):
    """L'API a refusé la requête : son statut, le code du refus et l'option fautive, si connus."""

    def __init__(self, status: int, code: str | None, option: str | None) -> None:
        super().__init__(status)
        self.status = status
        self.code = code
        self.option = option


# Les codes de refus que la commande sait dire ; tout autre n'est pas recopié.
REFUSALS: Final = frozenset(
    {
        "unknown_dataset",
        "query_timeout",
        "host_not_allowed",
        "origin_not_allowed",
        "cross_site_not_allowed",
    }
)
# Chaque paramètre de la route, et l'option qui le donne : un refus de l'API
# désigne le paramètre par son chemin (« /query/known_at »), la commande le
# dit par son option.
OPTIONS_BY_PARAMETER: Final = {
    "namespace": "--namespace",
    "name": "--name",
    "from": "--from",
    "to": "--to",
    "limit": "--limit",
    "cursor": "--cursor",
    "known_at": "--known-at",
}
OPTIONS: Final = {f"/query/{name}": option for name, option in OPTIONS_BY_PARAMETER.items()}


def _refusal(received: Received) -> tuple[str | None, str | None]:
    """Le code d'un refus de l'API, s'il est connu, et l'option fautive d'un 422."""
    try:
        document = received.json()
    except InvalidResponseError:
        return None, None
    if not isinstance(document, dict):
        return None, None
    code = document.get("error")
    if isinstance(code, str) and code in REFUSALS:
        return code, None
    errors = document.get("errors")
    if received.status == 422 and isinstance(errors, list):
        for error in errors:
            path = error.get("path") if isinstance(error, dict) else None
            if isinstance(path, str) and path in OPTIONS:
                return None, OPTIONS[path]
    return None, None


# Sortie lisible.


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count <= 1 else many}"


def percent(value: float) -> str:
    """« 99,9 % » : arrondi par défaut au dixième, pour ne jamais annoncer plus que mesuré."""
    tenths = math.floor(round(value * 1_000_000) / 1000) / 10
    text = f"{tenths:.1f}".removesuffix(".0").replace(".", ",")
    return f"{text} %"


_HOLE_LABELS: Final = {
    "events_lost": ("événement perdu", "événements perdus"),
    "missing_from_graph": ("événement absent du graphe", "événements absents du graphe"),
    "uninspected": ("requête non inspectée", "requêtes non inspectées"),
}


def hole_label(hole: Mapping[str, Any]) -> str:
    if hole["kind"] == "silence":
        return "aucun relevé (silence)"
    if hole["kind"] == "before_first_report":
        return "avant son premier relevé"
    one, many = _HOLE_LABELS[hole["kind"]]
    return _plural(hole["count"] or 0, one, many)


def coverage_lines(cover: Mapping[str, Any]) -> list[str]:
    """La couverture du lignage et ses trous connus, en phrases fixes."""
    holes = cover["holes"]
    if cover["status"] == "unknown":
        summary = "non mesurée : aucun relevé d'émetteur ne recoupe la fenêtre"
    else:
        state = "mesurée" if cover["status"] == "measured" else "partielle"
        seen = "aucun appel vu" if cover["rate"] is None else percent(cover["rate"])
        rate = seen if cover["rate"] is None else f"{seen} des appels vus sont dans le graphe"
        trous = (
            "sans trou connu" if not holes else _plural(len(holes), "trou connu", "trous connus")
        )
        summary = f"{state}, {rate}, {trous}"
    if cover["truncated"]:
        summary += " (mesure partielle : des relevés ou des trous dépassent leur borne)"
    lines = [f"Couverture du lignage : {summary}"]
    lines += [
        f"  {hole['emitter']} : {hole_label(hole)}, "
        f"du {hole['window']['from']} au {hole['window']['to']}"
        for hole in holes
    ]
    return lines


def incomplete_reasons(result: Mapping[str, Any]) -> list[str]:
    """Ce qui empêche de conclure ; vide quand le résultat est complet."""
    reasons = []
    cover = result["coverage"]
    if result["truncated"] or result["counts"]["lower_bound"]:
        reasons.append(
            "le résultat est tronqué : la propagation a dépassé une de ses bornes, "
            "les décomptes sont des minimums. Réduire la fenêtre et relancer."
        )
    if cover["status"] == "unknown":
        reasons.append("la couverture du lignage n'est pas mesurée sur la fenêtre.")
    elif cover["status"] == "partial":
        reasons.append("les relevés des émetteurs ne couvrent qu'une partie de la fenêtre.")
    if cover["holes"]:
        reasons.append("des trous connus : des appels ont pu échapper au graphe.")
    if cover["truncated"]:
        reasons.append("la mesure de la couverture est partielle.")
    return reasons


def verdict(result: Mapping[str, Any]) -> Exit:
    if incomplete_reasons(result):
        return Exit.INCOMPLETE
    return Exit.OK if result["counts"]["answers"] > 0 else Exit.NOTHING_TOUCHED


def _named(node: Mapping[str, str], name: str, scope: str) -> str:
    return f"{node[name]} ({node[scope]})"


def render_text(result: Mapping[str, Any]) -> str:
    graph = result["graph"]
    datasets = {node["id"]: node for node in graph["datasets"]}
    tools = {node["id"]: node for node in graph["tools"]}
    agents = {node["id"]: node for node in graph["agents"]}
    pseudonyms = {node["id"]: node["pseudonym"] for node in graph["recipients"]}
    dataset, window, counts = result["dataset"], result["window"], result["counts"]
    minimum = "au moins " if counts["lower_bound"] else ""

    lines = [
        f"Jeu de données : {dataset['name']} (espace de noms {dataset['namespace']}, "
        f"{dataset['kind']})",
        f"Fenêtre : du {window['from']} au {window['to']}, fin exclue",
    ]
    if "known_at" in result:
        lines.append(f"Graphe : tel que connu le {result['known_at']}")
    lines += [
        "",
        minimum
        + _plural(counts["answers"], "réponse touchée", "réponses touchées")
        + f", {minimum}"
        + _plural(counts["recipients"], "destinataire", "destinataires"),
        *coverage_lines(result["coverage"]),
    ]
    if graph["answers"]:
        lines += [
            "",
            f"Réponses, les plus récentes d'abord ({len(graph['answers'])} sur cette page) :",
        ]
    for answer in graph["answers"]:
        lines.append(f"  {answer['produced_at']}  {answer['id']}")
        if answer["agent"] is not None:
            lines.append(f"    agent : {_named(agents[answer['agent']], 'name', 'namespace')}")
        if answer["tools"]:
            named = ", ".join(_named(tools[tool], "name", "server") for tool in answer["tools"])
            lines.append(f"    outils : {named}")
        read = dict.fromkeys(read["dataset"] for read in answer["reads"])
        if read:
            lines.append(
                "    lu : " + ", ".join(_named(datasets[key], "name", "namespace") for key in read)
            )
        if answer["channels"]:
            lines.append(f"    canaux : {', '.join(answer['channels'])}")
        if answer["recipients"]:
            listed = ", ".join(pseudonyms[key] for key in answer["recipients"])
            lines.append(f"    destinataires : {listed}")
    if graph["recipients"]:
        lines += ["", f"Destinataires, pseudonymisés ({len(graph['recipients'])} sur cette page) :"]
        lines += [f"  {node['pseudonym']}" for node in graph["recipients"]]
    if graph["next_cursor"] is not None:
        lines += [
            "",
            f"Réponses plus anciennes : relancer avec --cursor {graph['next_cursor']}",
        ]
    lines += ["", *_conclusion(result)]
    return "\n".join(lines) + "\n"


def _conclusion(result: Mapping[str, Any]) -> list[str]:
    reasons = incomplete_reasons(result)
    touched = result["counts"]["answers"] > 0
    if not reasons:
        if touched:
            return ["Liste complète : aucune réponse touchée ne peut y manquer."]
        return ["Aucune réponse touchée, et la couverture le confirme : rien n'est à rappeler."]
    lines = ["Résultat incomplet :", *[f"  - {reason}" for reason in reasons]]
    if touched:
        lines.append("D'autres réponses peuvent être touchées que celles-ci.")
    else:
        lines.append(
            "Aucune réponse touchée n'a été trouvée, mais ce résultat ne permet pas de conclure "
            "qu'aucune réponse n'est touchée."
        )
    return lines


def render_json(result: Mapping[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2) + "\n"


def refusal_message(refused: RefusedError) -> tuple[Exit, str]:
    """Le message d'un refus de l'API, écrit d'avance ; le code de sortie qui va avec."""
    if refused.option is not None:
        return Exit.USAGE, f"L'API a refusé un paramètre : {refused.option}."
    if refused.status == 422:
        return Exit.USAGE, "L'API a refusé un paramètre de la requête."
    messages = {
        "unknown_dataset": (
            "Ce jeu de données n'est pas connu du graphe : vérifier son espace de noms et son nom, "
            "ou la date de --known-at."
        ),
        "query_timeout": (
            "La lecture a dépassé son délai maximal dans l'API : réduire la fenêtre de l'incident."
        ),
        "host_not_allowed": (
            "L'API refuse le nom d'hôte de cette adresse : l'ajouter à "
            "ANSWERLINEAGE_ALLOWED_HOSTS sur l'API, ou la joindre par la boucle locale."
        ),
        "origin_not_allowed": (
            "L'API refuse l'origine de la requête : la déclarer dans "
            "ANSWERLINEAGE_ALLOWED_ORIGINS sur l'API."
        ),
        "cross_site_not_allowed": "L'API refuse une requête venue d'une autre page.",
    }
    if refused.code is not None:
        return Exit.API_ERROR, messages[refused.code]
    return Exit.API_ERROR, f"L'API a répondu HTTP {refused.status}, sans résultat lisible."


def run(api: Api, query: Query, output: str, write: Callable[[str], None]) -> Exit:
    """Propage l'incident et écrit le résultat ; un refus de l'API remonte en ``RefusedError``."""
    result = fetch(api, query)
    write(render_json(result) if output == "json" else render_text(result))
    return verdict(result)
