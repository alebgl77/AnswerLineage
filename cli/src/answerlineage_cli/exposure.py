"""``answerlineage exposure`` : les jeux de données et leurs colonnes, les plus exposés d'abord.

La commande interroge ``GET /api/v1/exposure`` et valide la réponse entière
avant d'en afficher quoi que ce soit, comme ``impact``. La sortie lisible dit
l'heure de référence, la règle du score, puis chaque jeu de données : son
score, ses réponses et ses destinataires, et ses colonnes les plus exposées.
La sortie JSON est la réponse de l'API, réduite aux champs de son contrat.

Le code de sortie dit si le classement est complet (0) ou si une lecture du
graphe a dépassé sa borne (3) : les scores sont alors des minimums.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from answerlineage_cli import shape
from answerlineage_cli.client import Api, InvalidResponseError, Received
from answerlineage_cli.exits import Exit
from answerlineage_cli.names import COLUMN_MAX, DATASET_NAME_MAX, NAMESPACE_MAX

PATH: Final = "/api/v1/exposure"
# Bornes de la route : jeux de données par page, colonnes rendues par jeu.
MAX_LIMIT: Final = 200
MAX_COLUMNS: Final = 100
# Une page de 200 jeux et de leurs 100 colonnes : quelques mégaoctets au plus.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
CURSOR: Final = re.compile(r"[0-9]{1,18}\.[0-9]{1,18}\.[0-9]{1,19}")


def _column(value: Any, where: str) -> dict[str, Any]:
    column = shape.obj(value, where, ("name", "score", "answers"))
    return {
        "name": shape.identifier(column["name"], f"{where}/name", COLUMN_MAX),
        "score": shape.score(column["score"], f"{where}/score"),
        "answers": shape.count(column["answers"], f"{where}/answers"),
    }


def _dataset(value: Any, where: str) -> dict[str, Any]:
    fields = ("namespace", "name", "kind", "score", "answers", "recipients", "columns")
    dataset = shape.obj(value, where, (*fields, "more_columns"))
    columns = shape.items(dataset["columns"], f"{where}/columns", MAX_COLUMNS)
    return {
        "namespace": shape.identifier(dataset["namespace"], f"{where}/namespace", NAMESPACE_MAX),
        "name": shape.identifier(dataset["name"], f"{where}/name", DATASET_NAME_MAX),
        "kind": shape.identifier(dataset["kind"], f"{where}/kind", NAMESPACE_MAX),
        "score": shape.score(dataset["score"], f"{where}/score"),
        "answers": shape.count(dataset["answers"], f"{where}/answers"),
        "recipients": shape.count(dataset["recipients"], f"{where}/recipients"),
        "columns": [_column(item, f"{where}/columns/{rank}") for rank, item in enumerate(columns)],
        "more_columns": shape.flag(dataset["more_columns"], f"{where}/more_columns"),
    }


def validate(document: Any) -> dict[str, Any]:
    """La réponse de la route, validée, réduite aux champs de son contrat."""
    fields = ("at", "datasets", "more", "next_cursor", "truncated")
    result = shape.obj(document, "", fields, ("known_at",))
    datasets = shape.items(result["datasets"], "/datasets", MAX_LIMIT)
    cursor = result["next_cursor"]
    validated: dict[str, Any] = {
        "at": shape.instant(result["at"], "/at"),
        "datasets": [_dataset(item, f"/datasets/{rank}") for rank, item in enumerate(datasets)],
        "more": shape.flag(result["more"], "/more"),
        "next_cursor": None if cursor is None else shape.pattern(cursor, "/next_cursor", CURSOR),
        "truncated": shape.flag(result["truncated"], "/truncated"),
    }
    # Une page qui annonce une suite donne son curseur, et seulement elle.
    if validated["more"] != (validated["next_cursor"] is not None):
        raise shape.fault("/next_cursor")
    if "known_at" in result:
        validated["known_at"] = shape.instant(result["known_at"], "/known_at")
    return validated


@dataclass(frozen=True)
class Query:
    """Les paramètres de la route, déjà contrôlés par la ligne de commande."""

    limit: int | None = None
    cursor: str | None = None
    at: str | None = None
    known_at: str | None = None

    def params(self) -> dict[str, str]:
        params = {"limit": None if self.limit is None else str(self.limit)}
        params |= {"cursor": self.cursor, "at": self.at, "known_at": self.known_at}
        return {name: value for name, value in params.items() if value is not None}


class RefusedError(Exception):
    """L'API a refusé la requête : son statut, le code du refus et l'option fautive, si connus."""

    def __init__(self, status: int, code: str | None, option: str | None) -> None:
        super().__init__(status)
        self.status = status
        self.code = code
        self.option = option


# Les codes de refus que la commande sait dire ; tout autre n'est pas recopié.
REFUSALS: Final = {
    "query_timeout": (
        "La lecture a dépassé son délai maximal dans l'API : réessayer, ou interroger le graphe "
        "à une date antérieure (--known-at)."
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
# Chaque paramètre de la route, et l'option qui le donne.
OPTIONS_BY_PARAMETER: Final = {
    "limit": "--limit",
    "cursor": "--cursor",
    "at": "--at",
    "known_at": "--known-at",
}
OPTIONS: Final = {f"/query/{name}": option for name, option in OPTIONS_BY_PARAMETER.items()}


def _refusal(received: Received) -> RefusedError:
    """Le refus de l'API : son code s'il est connu, l'option fautive d'un 422."""
    try:
        document = received.json()
    except InvalidResponseError:
        return RefusedError(received.status, None, None)
    if not isinstance(document, dict):
        return RefusedError(received.status, None, None)
    code = document.get("error")
    if isinstance(code, str) and code in REFUSALS:
        return RefusedError(received.status, code, None)
    errors = document.get("errors")
    if received.status == 422 and isinstance(errors, list):
        for error in errors:
            path = error.get("path") if isinstance(error, dict) else None
            if isinstance(path, str) and path in OPTIONS:
                return RefusedError(received.status, None, OPTIONS[path])
    return RefusedError(received.status, None, None)


def refusal_message(refused: RefusedError) -> tuple[Exit, str]:
    """Le message d'un refus de l'API, écrit d'avance ; le code de sortie qui va avec."""
    if refused.option is not None:
        return Exit.USAGE, f"L'API a refusé un paramètre : {refused.option}."
    if refused.status == 422:
        return Exit.USAGE, "L'API a refusé un paramètre de la requête."
    if refused.code is not None:
        return Exit.API_ERROR, REFUSALS[refused.code]
    return Exit.API_ERROR, f"L'API a répondu HTTP {refused.status}, sans résultat lisible."


def fetch(api: Api, query: Query) -> dict[str, Any]:
    """Le classement, validé ; ``RefusedError`` quand l'API refuse la requête."""
    received = api.get(PATH, params=query.params(), max_bytes=MAX_RESPONSE_BYTES)
    if received.status != 200:
        raise _refusal(received)
    return validate(received.json())


# Sortie lisible.


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count <= 1 else many}"


def number(value: float) -> str:
    """Un score au millionième, à la française, sans zéro inutile : « 1,95432 », « 4 »."""
    return f"{value:.6f}".rstrip("0").rstrip(".").replace(".", ",")


def _column_label(column: Mapping[str, Any]) -> str:
    answers = _plural(column["answers"], "réponse", "réponses")
    return f"{column['name']} {number(column['score'])} ({answers})"


def render_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"Exposition des jeux de données au {result['at']}",
        "Score : chaque réponse qui dépend d'un jeu de données y pèse 1 plus un par destinataire, "
        "moitié moins tous les 30 jours.",
    ]
    if "known_at" in result:
        lines.append(f"Graphe : tel que connu le {result['known_at']}")
    datasets = result["datasets"]
    if datasets:
        lines += [
            "",
            f"Jeux de données, les plus exposés d'abord ({len(datasets)} sur cette page) :",
        ]
    for dataset in datasets:
        lines.append(
            f"  {number(dataset['score'])}  {dataset['name']} "
            f"(espace de noms {dataset['namespace']}, {dataset['kind']})"
        )
        lines.append(
            "    "
            + _plural(dataset["answers"], "réponse", "réponses")
            + ", "
            + _plural(dataset["recipients"], "destinataire", "destinataires")
        )
        if dataset["columns"]:
            listed = ", ".join(_column_label(column) for column in dataset["columns"])
            lines.append(f"    colonnes : {listed}")
        if dataset["more_columns"]:
            lines.append("    d'autres colonnes, moins exposées, suivent")
    if not datasets and result["next_cursor"] is None:
        lines += ["", "Aucun jeu de données n'est exposé : aucune réponse n'en dépend."]
    if result["next_cursor"] is not None:
        lines += [
            "",
            f"Jeux de données moins exposés : relancer avec --cursor {result['next_cursor']}",
        ]
    lines.append("")
    if result["truncated"]:
        lines.append(
            "Classement incomplet : une lecture du graphe a dépassé sa borne. Les scores sont des "
            "minimums, et l'ordre peut différer de celui du graphe entier."
        )
    else:
        lines.append(
            "Classement complet : chaque réponse qui dépend d'un jeu de données y est comptée."
        )
    return "\n".join(lines) + "\n"


def render_json(result: Mapping[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2) + "\n"


def run(api: Api, query: Query, output: str, write: Callable[[str], None]) -> Exit:
    """Classe les jeux de données et écrit le résultat ; un refus remonte en ``RefusedError``."""
    result = fetch(api, query)
    write(render_json(result) if output == "json" else render_text(result))
    return Exit.INCOMPLETE if result["truncated"] else Exit.OK
