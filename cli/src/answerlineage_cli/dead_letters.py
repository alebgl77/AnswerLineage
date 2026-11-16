"""``answerlineage dead-letters`` : consulter et rejouer la file de rebut.

``list`` lit ``GET /api/v1/ingestion/dead-letters`` : une page des événements
que la projection n'a pas pu intégrer au graphe, chacun avec son émetteur, son
motif, ses essais et ses heures d'échec, et les décomptes par motif. Son code
de sortie dit si des événements attendent.

``replay`` envoie ``POST /api/v1/ingestion/dead-letters/replay`` : l'API remet
en file les événements du rebut que les filtres désignent, en nombre borné, et
un worker les projette de nouveau. Rejouer tout le rebut se demande
explicitement (``--all``) : un filtre oublié ne le rejoue pas en entier.

Toute réponse de l'API est validée en entier avant d'être affichée, comme pour
``impact`` : un identifiant n'est affiché que s'il a la forme d'un nom, un motif
n'est lu que dans sa liste fermée.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from answerlineage_cli import shape
from answerlineage_cli.client import Api, InvalidResponseError, Received
from answerlineage_cli.exits import Exit
from answerlineage_cli.names import NAMESPACE_MAX, is_identifier

PATH: Final = "/api/v1/ingestion/dead-letters"
REPLAY_PATH: Final = f"{PATH}/replay"
# Les motifs, dans l'ordre de la liste fermée de l'API. Les quatre premiers mettent un
# événement au rebut au premier échec ; les deux derniers, après les essais réglés.
REASONS: Final = (
    "invalid_identifier",
    "invalid_event",
    "constraint_violation",
    "invalid_value",
    "database_error",
    "projection_error",
)
REASON_LABELS: Final = {
    "invalid_identifier": "un identifiant obligatoire n'a pas la forme d'un nom",
    "invalid_event": "l'événement n'a pas la forme que la projection lit",
    "constraint_violation": "l'écriture du graphe viole une contrainte de la base",
    "invalid_value": "la base refuse une valeur de l'événement",
    "database_error": "la base a échoué à chaque essai",
    "projection_error": "la projection a échoué à chaque essai",
}
# Bornes de l'API : une page de la liste, un rejeu, les identifiants d'un rejeu.
MAX_LIMIT: Final = 500
MAX_REPLAY: Final = 10_000
MAX_EVENT_IDS: Final = 1_000
# Un émetteur donné à --emitter, avant d'être contrôlé comme un nom.
MAX_EMITTER: Final = NAMESPACE_MAX
EVENT_ID: Final = re.compile(r"[0-9a-f]{64}")
CURSOR: Final = re.compile(r"[0-9]{1,18}\.[0-9a-f]{64}")
RUN_ID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Une page de 500 événements tient en quelques centaines de Kio.
MAX_RESPONSE_BYTES: Final = 4 * 1024 * 1024
# Le corps d'un rejeu lu par l'API ; le bilan qu'elle rend tient en quelques octets.
MAX_REPLAY_RESPONSE_BYTES: Final = 64 * 1024


@dataclass(frozen=True)
class Filters:
    """Ce que la commande demande, déjà contrôlé."""

    emitter: str | None = None
    reason: str | None = None
    event_ids: tuple[str, ...] = ()


def filters_fault(emitter: str | None, event_ids: Sequence[str]) -> str | None:
    """Le motif qui rend des filtres fautifs avant tout envoi, ou ``None``.

    Le motif est un texte fixe : il ne recopie aucune valeur donnée.
    """
    if emitter is not None and not is_identifier(emitter, max_length=NAMESPACE_MAX):
        return (
            "--emitter attend un espace de noms qui a la forme d'un nom : lettres, chiffres et "
            ". _ - / : @ = + ~ % , # $ * [ ], sans espace."
        )
    if len(event_ids) > MAX_EVENT_IDS:
        return f"--event-id se donne au plus {MAX_EVENT_IDS} fois."
    return None


# Validation d'une réponse de l'API.


def _dead_letter(value: Any, where: str) -> dict[str, Any]:
    fields = (
        "event_id",
        "run_id",
        "emitter",
        "reason",
        "attempts",
        "received_at",
        "first_failed_at",
        "last_failed_at",
    )
    item = shape.obj(value, where, fields)
    return {
        "event_id": shape.pattern(item["event_id"], f"{where}/event_id", EVENT_ID),
        "run_id": shape.pattern(item["run_id"], f"{where}/run_id", RUN_ID),
        "emitter": shape.identifier(item["emitter"], f"{where}/emitter", NAMESPACE_MAX),
        "reason": shape.choice(item["reason"], f"{where}/reason", REASONS),
        "attempts": shape.count(item["attempts"], f"{where}/attempts"),
        "received_at": shape.instant(item["received_at"], f"{where}/received_at"),
        "first_failed_at": shape.instant(item["first_failed_at"], f"{where}/first_failed_at"),
        "last_failed_at": shape.instant(item["last_failed_at"], f"{where}/last_failed_at"),
    }


def _counts(value: Any, where: str) -> list[dict[str, Any]]:
    listed = shape.items(value, where, len(REASONS))
    counts = []
    for rank, item in enumerate(listed):
        found = shape.obj(item, f"{where}/{rank}", ("reason", "count"))
        counts.append(
            {
                "reason": shape.choice(found["reason"], f"{where}/{rank}/reason", REASONS),
                "count": shape.count(found["count"], f"{where}/{rank}/count"),
            }
        )
    if len({item["reason"] for item in counts}) != len(counts):
        raise shape.fault(where)
    return counts


def validate_list(document: Any) -> dict[str, Any]:
    """Une page du rebut, validée, réduite aux champs de son contrat."""
    page = shape.obj(document, "", ("dead_letters", "counts", "truncated", "more", "next_cursor"))
    items = shape.items(page["dead_letters"], "/dead_letters", MAX_LIMIT)
    cursor = page["next_cursor"]
    validated = {
        "dead_letters": [
            _dead_letter(item, f"/dead_letters/{rank}") for rank, item in enumerate(items)
        ],
        "counts": _counts(page["counts"], "/counts"),
        "truncated": shape.flag(page["truncated"], "/truncated"),
        "more": shape.flag(page["more"], "/more"),
        "next_cursor": None if cursor is None else shape.pattern(cursor, "/next_cursor", CURSOR),
    }
    # Une page qui annonce une suite donne son curseur, et seulement elle.
    if validated["more"] != (validated["next_cursor"] is not None):
        raise shape.fault("/next_cursor")
    return validated


def validate_replay(document: Any) -> dict[str, Any]:
    result = shape.obj(document, "", ("replayed", "more"))
    return {
        "replayed": shape.count(result["replayed"], "/replayed"),
        "more": shape.flag(result["more"], "/more"),
    }


# Échanges avec l'API.


class RefusedError(Exception):
    """L'API a refusé la requête : son statut, le code du refus et l'option fautive, si connus."""

    def __init__(self, status: int, code: str | None, option: str | None) -> None:
        super().__init__(status)
        self.status = status
        self.code = code
        self.option = option


# Les codes de refus que la commande sait dire ; tout autre n'est pas recopié.
REFUSALS: Final = {
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
# Le paramètre ou le champ refusé par l'API, et l'option qui le donne.
OPTIONS_BY_PATH: Final = (
    ("/query/emitter", "--emitter"),
    ("/query/reason", "--reason"),
    ("/query/limit", "--limit"),
    ("/query/cursor", "--cursor"),
    ("/emitter", "--emitter"),
    ("/reason", "--reason"),
    ("/event_ids", "--event-id"),
    ("/limit", "--limit"),
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


def refusal_message(refused: RefusedError) -> tuple[Exit, str]:
    """Le message d'un refus de l'API, écrit d'avance ; le code de sortie qui va avec."""
    if refused.option is not None:
        return Exit.USAGE, f"L'API a refusé un paramètre : {refused.option}."
    if refused.status == 422:
        return Exit.USAGE, "L'API a refusé un paramètre de la requête."
    if refused.code is not None:
        return Exit.API_ERROR, REFUSALS[refused.code]
    return Exit.API_ERROR, f"L'API a répondu HTTP {refused.status}, sans résultat lisible."


def fetch(api: Api, filters: Filters, limit: int | None, cursor: str | None) -> dict[str, Any]:
    """Une page du rebut ; ``RefusedError`` quand l'API refuse la requête."""
    params = {} if limit is None else {"limit": str(limit)}
    for name, value in (("emitter", filters.emitter), ("reason", filters.reason)):
        if value is not None:
            params[name] = value
    if cursor is not None:
        params["cursor"] = cursor
    received = api.get(PATH, params=params, max_bytes=MAX_RESPONSE_BYTES)
    if received.status != 200:
        raise _refusal(received)
    return validate_list(received.json())


def replay(api: Api, filters: Filters, limit: int | None) -> dict[str, Any]:
    """Remet en file les événements désignés ; ``RefusedError`` quand l'API refuse."""
    document: dict[str, Any] = {}
    if filters.emitter is not None:
        document["emitter"] = filters.emitter
    if filters.reason is not None:
        document["reason"] = filters.reason
    if filters.event_ids:
        document["event_ids"] = list(filters.event_ids)
    if limit is not None:
        document["limit"] = limit
    received = api.post(REPLAY_PATH, document=document, max_bytes=MAX_REPLAY_RESPONSE_BYTES)
    if received.status != 202:
        raise _refusal(received)
    return validate_replay(received.json())


# Sortie lisible.


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count <= 1 else many}"


def waiting(page: Mapping[str, Any], reason: str | None) -> int:
    """Événements au rebut sous les filtres : ceux du motif demandé, ou tous."""
    return sum(item["count"] for item in page["counts"] if reason in (None, item["reason"]))


def _replay_command(filters: Filters, reason: str | None) -> str:
    words = ["answerlineage", "dead-letters", "replay"]
    if filters.emitter is not None:
        words += ["--emitter", filters.emitter]
    if reason is not None:
        words += ["--reason", reason]
    if len(words) == 3:
        words.append("--all")
    return shlex.join(words)


def render_list(page: Mapping[str, Any], filters: Filters) -> str:
    total = waiting(page, None)
    minimum = "au moins " if page["truncated"] else ""
    of = "" if filters.emitter is None else f" de {filters.emitter}"
    if total == 0:
        return f"Rebut vide : aucun événement{of} n'attend.\n"
    lines = [f"Rebut{of} : {minimum}{_plural(total, 'événement', 'événements')}"]
    width = max(len(reason) for reason in REASONS)
    lines += [
        f"  {item['reason']:<{width}}  {minimum}{item['count']:>6}  {REASON_LABELS[item['reason']]}"
        for item in page["counts"]
        if item["count"]
    ]
    items = page["dead_letters"]
    if items:
        lines += [
            "",
            f"Les plus récemment mis au rebut d'abord ({len(items)} sur cette page) :",
        ]
    for item in items:
        lines += [
            f"  {item['last_failed_at']}  {item['reason']}  {item['emitter']}",
            f"    événement {item['event_id']}",
            f"    run {item['run_id']}, reçu le {item['received_at']}",
            f"    {_plural(item['attempts'], 'essai', 'essais')} en échec, "
            f"le premier le {item['first_failed_at']}",
        ]
    if page["next_cursor"] is not None:
        lines += ["", f"Page suivante : relancer avec --cursor {page['next_cursor']}"]
    if waiting(page, filters.reason):
        lines += [
            "",
            "Une fois le correctif déployé, les rejouer : "
            + _replay_command(filters, filters.reason),
        ]
    return "\n".join(lines) + "\n"


def render_replay(result: Mapping[str, Any]) -> str:
    replayed = result["replayed"]
    if replayed:
        lines = [
            f"{_plural(replayed, 'événement remis', 'événements remis')} en file : un worker "
            "les projettera dans le graphe ; un événement qui échoue encore reviendra au rebut."
        ]
    else:
        lines = ["Aucun événement remis en file."]
    if result["more"]:
        lines.append(
            "D'autres événements restent au rebut sous ces filtres : relancer la même commande."
        )
    elif not replayed:
        lines.append("Aucun événement n'attend au rebut sous ces filtres.")
    return "\n".join(lines) + "\n"


def render_json(document: Mapping[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def run_list(
    api: Api,
    filters: Filters,
    *,
    limit: int | None,
    cursor: str | None,
    output: str,
    write: Callable[[str], None],
) -> Exit:
    """Lit une page du rebut ; le code de sortie dit si des événements y attendent."""
    page = fetch(api, filters, limit, cursor)
    write(render_json(page) if output == "json" else render_list(page, filters))
    return Exit.WAITING if waiting(page, filters.reason) else Exit.OK


def run_replay(
    api: Api, filters: Filters, *, limit: int | None, output: str, write: Callable[[str], None]
) -> Exit:
    """Remet en file ; le code de sortie dit si tout est rejoué, rien, ou s'il reste à faire."""
    result = replay(api, filters, limit)
    write(render_json(result) if output == "json" else render_replay(result))
    if result["more"]:
        return Exit.REMAINING
    return Exit.OK if result["replayed"] else Exit.NOTHING_TO_REPLAY
