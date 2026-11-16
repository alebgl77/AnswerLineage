"""``answerlineage incident`` : déclarer un incident de données, et lire son analyse.

``declare`` envoie la déclaration à ``POST /api/v1/incidents`` : le périmètre,
la fenêtre de validité et la gravité, contrôlés avant tout envoi. L'API crée
l'incident à l'état « qualifié » et demande son analyse d'impact, que les
workers font ensuite ; la commande rend l'incident déclaré et son identifiant.

``show`` lit ``GET /api/v1/incidents/{id}`` : l'incident, l'état de son
analyse et, quand elle est faite, une page des réponses touchées avec leurs
destinataires, la couverture du lignage et ses trous. Son code de sortie dit ce
que l'analyse permet de conclure, comme celui de ``impact`` ; une analyse en
attente ou en échec a le sien.

Toute réponse de l'API est validée en entier avant d'être affichée, comme pour
``impact`` : un identifiant n'est affiché que s'il a la forme d'un nom, un état
ou un code n'est lu que dans sa liste fermée.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from answerlineage_cli import impact, shape
from answerlineage_cli.client import Api, InvalidResponseError, Received
from answerlineage_cli.exits import Exit
from answerlineage_cli.names import (
    DATASET_NAME_MAX,
    FINGERPRINT_MAX,
    NAMESPACE_MAX,
    NORMAL_FORM,
    RECIPIENT_MAX,
    is_identifier,
)

PATH: Final = "/api/v1/incidents"
# Ce que l'API admet et rend : listes fermées, bornes du périmètre et de la fenêtre.
SEVERITIES: Final = ("low", "medium", "high", "critical")
INCIDENT_STATES: Final = ("qualified",)
ANALYSIS_STATES: Final = ("pending", "done", "failed")
ANALYSIS_ERRORS: Final = ("query_timeout", "analysis_error")
MAX_DATASETS: Final = 20
MAX_WINDOW_DAYS: Final = 31
# Un espace de noms ou un nom donné à --dataset, avant d'être contrôlé comme un nom.
MAX_DATASET_ARGUMENT: Final = DATASET_NAME_MAX
# Réponses touchées par page.
MAX_LIMIT: Final = 200
# Une page de 200 réponses et de leurs destinataires.
MAX_RESPONSE_BYTES: Final = 32 * 1024 * 1024
MAX_RECIPIENTS: Final = 200_000
INCIDENT_ID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

SEVERITY_LABELS: Final = {
    "low": "faible",
    "medium": "moyenne",
    "high": "élevée",
    "critical": "critique",
}
STATE_LABELS: Final = {"qualified": "qualifié"}
ERROR_LABELS: Final = {
    "query_timeout": "une lecture a dépassé le délai de l'analyse",
    "analysis_error": "une erreur est survenue pendant l'analyse",
}


@dataclass(frozen=True)
class Declaration:
    """Ce que la commande déclare, déjà contrôlé : identifiants, fenêtre en UTC, gravité."""

    datasets: tuple[tuple[str, str], ...]
    start: str
    end: str
    severity: str

    def document(self) -> dict[str, Any]:
        return {
            "scope": {"datasets": [{"namespace": ns, "name": name} for ns, name in self.datasets]},
            "window": {"from": self.start, "to": self.end},
            "severity": self.severity,
        }


def declaration_fault(datasets: Sequence[Sequence[str]], start: str, end: str) -> str | None:
    """Le motif qui rend une déclaration fautive avant tout envoi, ou ``None``.

    Le motif est un texte fixe : il ne recopie aucune valeur donnée.
    """
    if len(datasets) > MAX_DATASETS:
        return f"--dataset se donne au plus {MAX_DATASETS} fois."
    seen: set[tuple[str, str]] = set()
    for namespace, name in datasets:
        if not is_identifier(namespace, max_length=NAMESPACE_MAX) or not is_identifier(
            name, max_length=DATASET_NAME_MAX
        ):
            return (
                "--dataset attend un espace de noms et un nom qui ont la forme d'un nom : "
                "lettres, chiffres et . _ - / : @ = + ~ % , # $ * [ ], sans espace."
            )
        key = (
            unicodedata.normalize(NORMAL_FORM, namespace),
            unicodedata.normalize(NORMAL_FORM, name),
        )
        if key in seen:
            return "un même jeu de données est donné deux fois à --dataset."
        seen.add(key)
    begin, finish = datetime.fromisoformat(start), datetime.fromisoformat(end)
    if finish <= begin:
        return "--to doit suivre --from."
    if (finish - begin).total_seconds() > MAX_WINDOW_DAYS * 86_400:
        return f"une fenêtre de validité dure au plus {MAX_WINDOW_DAYS} jours."
    return None


# Validation d'une réponse de l'API.


def _window(value: Any, where: str) -> dict[str, str]:
    window = shape.obj(value, where, ("from", "to"))
    return {key: shape.instant(window[key], f"{where}/{key}") for key in ("from", "to")}


def _dataset(value: Any, where: str) -> dict[str, str]:
    dataset = shape.obj(value, where, ("namespace", "name"))
    return {
        "namespace": shape.identifier(dataset["namespace"], f"{where}/namespace", NAMESPACE_MAX),
        "name": shape.identifier(dataset["name"], f"{where}/name", DATASET_NAME_MAX),
    }


def _datasets(value: Any, where: str) -> list[dict[str, str]]:
    listed = shape.items(value, where, MAX_DATASETS)
    return [_dataset(item, f"{where}/{rank}") for rank, item in enumerate(listed)]


def _optional_instant(value: Any, where: str) -> str | None:
    return None if value is None else shape.instant(value, where)


def _analysis(value: Any, where: str) -> dict[str, Any]:
    fields = ("state", "requested_at", "attempts", "error", "known_at", "finished_at")
    analysis = shape.obj(value, where, (*fields, "counts", "truncated"))
    state = shape.choice(analysis["state"], f"{where}/state", ANALYSIS_STATES)
    error = analysis["error"]
    counts = analysis["counts"]
    validated: dict[str, Any] = {
        "state": state,
        "requested_at": shape.instant(analysis["requested_at"], f"{where}/requested_at"),
        "attempts": shape.count(analysis["attempts"], f"{where}/attempts"),
        "error": None if error is None else shape.choice(error, f"{where}/error", ANALYSIS_ERRORS),
        "known_at": _optional_instant(analysis["known_at"], f"{where}/known_at"),
        "finished_at": _optional_instant(analysis["finished_at"], f"{where}/finished_at"),
        "counts": None,
        "truncated": None,
    }
    done = state == "done"
    if done:
        found = shape.obj(counts, f"{where}/counts", ("answers", "recipients", "lower_bound"))
        validated["counts"] = {
            "answers": shape.count(found["answers"], f"{where}/counts/answers"),
            "recipients": shape.count(found["recipients"], f"{where}/counts/recipients"),
            "lower_bound": shape.flag(found["lower_bound"], f"{where}/counts/lower_bound"),
        }
        validated["truncated"] = shape.flag(analysis["truncated"], f"{where}/truncated")
    # Un état et ce qu'il porte vont ensemble : une réponse qui les mêle n'est pas lue.
    elif counts is not None or analysis["truncated"] is not None:
        raise shape.fault(f"{where}/counts")
    if (done and validated["known_at"] is None) or (state == "failed" and error is None):
        raise shape.fault(f"{where}/state")
    if (state == "pending") != (validated["finished_at"] is None):
        raise shape.fault(f"{where}/finished_at")
    return validated


_INCIDENT_FIELDS: Final = ("id", "state", "severity", "scope", "window", "declared_at", "analysis")


def validate_incident(document: Any, where: str = "") -> dict[str, Any]:
    """Un incident rendu par l'API, validé, réduit aux champs de son contrat."""
    incident = shape.obj(document, where, _INCIDENT_FIELDS)
    scope = shape.obj(incident["scope"], f"{where}/scope", ("datasets",))
    return {
        "id": shape.pattern(incident["id"], f"{where}/id", INCIDENT_ID),
        "state": shape.choice(incident["state"], f"{where}/state", INCIDENT_STATES),
        "severity": shape.choice(incident["severity"], f"{where}/severity", SEVERITIES),
        "scope": {"datasets": _datasets(scope["datasets"], f"{where}/scope/datasets")},
        "window": _window(incident["window"], f"{where}/window"),
        "declared_at": shape.instant(incident["declared_at"], f"{where}/declared_at"),
        "analysis": _analysis(incident["analysis"], f"{where}/analysis"),
    }


def _answer(value: Any, where: str) -> dict[str, Any]:
    answer = shape.obj(value, where, ("id", "fingerprint", "produced_at", "recipients"))
    recipients = shape.items(answer["recipients"], f"{where}/recipients", MAX_RECIPIENTS)
    return {
        "id": shape.pattern(answer["id"], f"{where}/id", impact.ANSWER_ID),
        "fingerprint": shape.identifier(
            answer["fingerprint"], f"{where}/fingerprint", FINGERPRINT_MAX
        ),
        "produced_at": shape.instant(answer["produced_at"], f"{where}/produced_at"),
        "recipients": [
            shape.identifier(item, f"{where}/recipients/{rank}", RECIPIENT_MAX)
            for rank, item in enumerate(recipients)
        ],
    }


def _result(value: Any, where: str) -> dict[str, Any]:
    fields = ("unknown_datasets", "coverage", "answers", "more", "next_cursor")
    result = shape.obj(value, where, fields)
    answers = shape.items(result["answers"], f"{where}/answers", MAX_LIMIT)
    cursor = result["next_cursor"]
    return {
        "unknown_datasets": _datasets(result["unknown_datasets"], f"{where}/unknown_datasets"),
        "coverage": impact.coverage(result["coverage"], f"{where}/coverage"),
        "answers": [_answer(item, f"{where}/answers/{rank}") for rank, item in enumerate(answers)],
        "more": shape.flag(result["more"], f"{where}/more"),
        "next_cursor": None
        if cursor is None
        else shape.pattern(cursor, f"{where}/next_cursor", impact.CURSOR),
    }


def validate_detail(document: Any, incident_id: str) -> dict[str, Any]:
    """L'incident demandé, son analyse et son résultat, validés."""
    incident = validate_incident(document)
    if incident["id"] != incident_id:
        raise shape.fault("/id")
    found = shape.obj(document, "", (*_INCIDENT_FIELDS, "result"))["result"]
    done = incident["analysis"]["state"] == "done"
    if done != (found is not None):
        raise shape.fault("/result")
    return {**incident, "result": _result(found, "/result") if done else None}


# Échanges avec l'API.


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
        "unknown_incident",
        "query_timeout",
        "host_not_allowed",
        "origin_not_allowed",
        "cross_site_not_allowed",
    }
)
# Le champ ou le paramètre refusé par l'API, et l'option qui le donne, par préfixe du chemin.
OPTIONS_BY_PATH: Final = (
    ("/scope", "--dataset"),
    ("/window/from", "--from"),
    ("/window/to", "--to"),
    ("/window", "--from et --to"),
    ("/severity", "--severity"),
    ("/query/limit", "--limit"),
    ("/query/cursor", "--cursor"),
    ("/path/incident_id", "l'identifiant de l'incident"),
)


def _option(path: str) -> str | None:
    for prefix, option in OPTIONS_BY_PATH:
        if path == prefix or path.startswith(f"{prefix}/"):
            return option
    return None


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
            option = _option(path) if isinstance(path, str) else None
            if option is not None:
                return RefusedError(received.status, None, option)
    return RefusedError(received.status, None, None)


def declare(api: Api, declaration: Declaration) -> dict[str, Any]:
    """Déclare l'incident ; ``RefusedError`` quand l'API refuse la déclaration."""
    received = api.post(PATH, document=declaration.document(), max_bytes=MAX_RESPONSE_BYTES)
    if received.status != 201:
        raise _refusal(received)
    return validate_incident(received.json())


def fetch(api: Api, incident_id: str, limit: int | None, cursor: str | None) -> dict[str, Any]:
    """L'incident et son analyse ; ``RefusedError`` quand l'API refuse la requête."""
    params = {} if limit is None else {"limit": str(limit)}
    if cursor is not None:
        params["cursor"] = cursor
    received = api.get(f"{PATH}/{incident_id}", params=params, max_bytes=MAX_RESPONSE_BYTES)
    if received.status != 200:
        raise _refusal(received)
    return validate_detail(received.json(), incident_id)


def refusal_message(refused: RefusedError) -> tuple[Exit, str]:
    """Le message d'un refus de l'API, écrit d'avance ; le code de sortie qui va avec."""
    if refused.option is not None:
        return Exit.USAGE, f"L'API a refusé un paramètre : {refused.option}."
    if refused.status == 422:
        return Exit.USAGE, "L'API a refusé un paramètre de la requête."
    messages = {
        "unknown_incident": (
            "Aucun incident ne porte cet identifiant : reprendre celui que rend "
            "answerlineage incident declare."
        ),
        "query_timeout": "La lecture a dépassé son délai maximal dans l'API : réessayer.",
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


# Sortie lisible.


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count <= 1 else many}"


def _named(dataset: Mapping[str, str]) -> str:
    return f"{dataset['name']} (espace de noms {dataset['namespace']})"


def _summary(incident: Mapping[str, Any]) -> list[str]:
    window = incident["window"]
    severity = incident["severity"]
    lines = [
        f"  état : {STATE_LABELS[incident['state']]}",
        f"  gravité : {SEVERITY_LABELS[severity]} ({severity})",
    ]
    datasets = incident["scope"]["datasets"]
    lines.append(f"  périmètre : {_named(datasets[0])}" if datasets else "  périmètre : vide")
    lines += [f"              {_named(dataset)}" for dataset in datasets[1:]]
    lines += [
        f"  fenêtre : du {window['from']} au {window['to']}, fin exclue",
        f"  déclaré le {incident['declared_at']}",
    ]
    return lines


def render_declared(incident: Mapping[str, Any]) -> str:
    lines = [
        f"Incident déclaré : {incident['id']}",
        *_summary(incident),
        "Analyse : demandée, en attente d'un worker.",
        f"Suivre l'analyse : answerlineage incident show {incident['id']}",
    ]
    return "\n".join(lines) + "\n"


def _analysis_line(analysis: Mapping[str, Any]) -> str:
    state = analysis["state"]
    if state == "done":
        return (
            f"Analyse : faite le {analysis['finished_at']}, "
            f"sur le graphe tel que connu le {analysis['known_at']}"
        )
    failures = ""
    if analysis["attempts"]:
        failures = (
            f" ({_plural(analysis['attempts'], 'essai', 'essais')} en échec : "
            f"{ERROR_LABELS[analysis['error']]})"
            if analysis["error"] is not None
            else f" ({_plural(analysis['attempts'], 'essai', 'essais')} en échec)"
        )
    if state == "pending":
        return f"Analyse : en attente d'un worker{failures}."
    return f"Analyse : en échec le {analysis['finished_at']}{failures}."


def incomplete_reasons(detail: Mapping[str, Any]) -> list[str]:
    """Ce qui empêche de conclure sur une analyse faite ; vide quand elle est complète."""
    analysis, result = detail["analysis"], detail["result"]
    reasons = impact.incomplete_reasons(
        {
            "truncated": analysis["truncated"],
            "counts": analysis["counts"],
            "coverage": result["coverage"],
        }
    )
    if result["unknown_datasets"]:
        reasons.append(
            "un jeu de données du périmètre n'est pas connu du graphe : aucune lecture n'en "
            "est connue, ou son nom est mal écrit."
        )
    return reasons


def verdict(detail: Mapping[str, Any]) -> Exit:
    state = detail["analysis"]["state"]
    if state == "pending":
        return Exit.PENDING
    if state == "failed":
        return Exit.ANALYSIS_FAILED
    if incomplete_reasons(detail):
        return Exit.INCOMPLETE
    return Exit.OK if detail["analysis"]["counts"]["answers"] > 0 else Exit.NOTHING_TOUCHED


def _result_lines(detail: Mapping[str, Any]) -> list[str]:
    analysis, result = detail["analysis"], detail["result"]
    counts = analysis["counts"]
    minimum = "au moins " if counts["lower_bound"] else ""
    lines = [
        "",
        minimum
        + _plural(counts["answers"], "réponse touchée", "réponses touchées")
        + f", {minimum}"
        + _plural(counts["recipients"], "destinataire", "destinataires"),
        *impact.coverage_lines(result["coverage"]),
    ]
    if result["unknown_datasets"]:
        lines.append("Jeux de données inconnus du graphe à l'heure de l'analyse :")
        lines += [f"  {_named(dataset)}" for dataset in result["unknown_datasets"]]
    answers = result["answers"]
    if answers:
        lines += [
            "",
            f"Réponses touchées, les plus récentes d'abord ({len(answers)} sur cette page) :",
        ]
    for answer in answers:
        lines.append(f"  {answer['produced_at']}  {answer['id']}")
        if answer["recipients"]:
            lines.append(f"    destinataires : {', '.join(answer['recipients'])}")
    pseudonyms = sorted({pseudonym for answer in answers for pseudonym in answer["recipients"]})
    if pseudonyms:
        lines += ["", f"Destinataires, pseudonymisés ({len(pseudonyms)} sur cette page) :"]
        lines += [f"  {pseudonym}" for pseudonym in pseudonyms]
    if result["next_cursor"] is not None:
        lines += [
            "",
            f"Réponses plus anciennes : relancer avec --cursor {result['next_cursor']}",
        ]
    return lines


def _conclusion(detail: Mapping[str, Any]) -> list[str]:
    reasons = incomplete_reasons(detail)
    touched = detail["analysis"]["counts"]["answers"] > 0
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


def render_detail(detail: Mapping[str, Any]) -> str:
    analysis = detail["analysis"]
    lines = [f"Incident {detail['id']}", *_summary(detail), _analysis_line(analysis)]
    if analysis["state"] == "done":
        lines += [*_result_lines(detail), "", *_conclusion(detail)]
    elif analysis["state"] == "pending":
        lines.append(f"Relancer plus tard : answerlineage incident show {detail['id']}")
    elif analysis["error"] == "query_timeout":
        lines.append(
            "L'analyse a dépassé son délai : déclarer de nouveau l'incident sur une fenêtre ou un "
            "périmètre plus petits."
        )
    else:
        lines.append("Le journal des workers nomme la classe de l'erreur.")
    return "\n".join(lines) + "\n"


def render_json(document: Mapping[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def run_declare(
    api: Api, declaration: Declaration, output: str, write: Callable[[str], None]
) -> Exit:
    """Déclare l'incident et écrit l'incident déclaré ; un refus remonte en ``RefusedError``."""
    incident = declare(api, declaration)
    write(render_json(incident) if output == "json" else render_declared(incident))
    return Exit.OK


def run_show(
    api: Api,
    incident_id: str,
    *,
    limit: int | None,
    cursor: str | None,
    output: str,
    write: Callable[[str], None],
) -> Exit:
    """Lit l'incident et son analyse ; le code de sortie dit ce qu'elle permet de conclure."""
    detail = fetch(api, incident_id, limit, cursor)
    write(render_json(detail) if output == "json" else render_detail(detail))
    return verdict(detail)
