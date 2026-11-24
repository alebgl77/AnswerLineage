"""``answerlineage doctor`` : vérifie chaque composant d'une installation.

Les vérifications, dans l'ordre : l'API répond et sa version ; elle accepte
l'adresse utilisée (en-têtes Host et Origin) ; elle accepte le jeton, et son
rôle ouvre la route de santé ; elle lit sa base ; le schéma
est à jour ; la file d'ingestion avance ; aucun événement n'attend au rebut ;
aucun émetteur n'est muet ; aucune partition mensuelle ne manque ; la
réémission vers un consommateur OpenLineage externe n'est pas en retard ; les
webhooks qui annoncent les incidents ne sont pas en retard ; aucun incident
proposé par un test de qualité n'attend une décision depuis plus d'un jour ;
l'explorateur est servi. Les neuf du milieu viennent de la route
``GET /api/v1/health``.

Chaque anomalie a un code stable, un message et un remède écrits d'avance :
aucun texte reçu de l'API n'y est recopié, seulement des nombres et l'adresse
donnée par l'utilisateur. Une vérification qui dépend d'une autre en échec
est dite « non vérifiée », jamais « bonne » ; la marche des workers, qui ne
publient aucun signal de vie, ne l'est jamais.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Final, Literal

from answerlineage_cli import shape
from answerlineage_cli.client import (
    ENV_TOKEN,
    ENV_TOKEN_FILE,
    Api,
    InvalidResponseError,
    NotAuthorizedError,
    Received,
    UnreachableError,
)

HEALTH: Final = "/api/v1/health"
MAX_HEALTH_BYTES: Final = 64 * 1024
MAX_PAGE_BYTES: Final = 1024 * 1024
DEFAULT_QUEUE_MAX_AGE: Final = 300
MAX_QUEUE_MAX_AGE: Final = 7 * 24 * 3600
DEFAULT_REEMISSION_MAX_AGE: Final = 300
DEFAULT_WEBHOOK_MAX_AGE: Final = 300
# Une proposition qui attend plus d'un jour attend un humain qui ne la voit pas.
PROPOSAL_MAX_AGE: Final = 24 * 3600
# La version majeure de l'API que la commande sait lire.
API_MAJOR: Final = 1

DATABASE_STATES: Final = ("available", "unavailable")
MIGRATION_STATES: Final = ("current", "behind", "unknown_revision", "not_migrated")

CHECKS: Final = (
    "api",
    "address",
    "access",
    "database",
    "migrations",
    "queue",
    "dead_letters",
    "emitters",
    "partitions",
    "reemission",
    "webhooks",
    "proposals",
    "explorer",
    "workers",
)
_LABELS: Final = {
    "api": "API",
    "address": "Adresse",
    "access": "Accès",
    "database": "Base",
    "migrations": "Migrations",
    "queue": "File",
    "dead_letters": "Rebut",
    "emitters": "Émetteurs",
    "partitions": "Partitions",
    "reemission": "Réémission",
    "webhooks": "Webhooks",
    "proposals": "Propositions",
    "explorer": "Explorateur",
    "workers": "Workers",
}

Status = Literal["ok", "anomaly", "not_checked"]


@dataclass(frozen=True)
class Anomaly:
    """Une anomalie connue : la vérification qui la trouve, son message et son remède."""

    check: str
    message: str
    remedy: str


_MIGRATE = (
    "Appliquer les migrations : python -m answerlineage_api.store.migrate upgrade, avec "
    "ANSWERLINEAGE_DATABASE_URL ; dans la démonstration, docker compose run --rm migrate."
)

ANOMALIES: Final[Mapping[str, Anomaly]] = {
    "api_unreachable": Anomaly(
        "api",
        "L'API ne répond pas à cette adresse.",
        "Lancer l'API (answerlineage demo depuis un clone du dépôt, ou uvicorn --factory "
        "answerlineage_api.app:create_app), ou corriger l'adresse : --url ou "
        "ANSWERLINEAGE_API_URL.",
    ),
    "api_invalid_response": Anomaly(
        "api",
        "L'adresse ne répond pas comme une API AnswerLineage que la commande sait lire.",
        "Vérifier que l'adresse désigne une API AnswerLineage, de la version de la commande.",
    ),
    "api_version_unsupported": Anomaly(
        "api",
        f"L'API n'est pas de la version majeure que la commande sait lire ({API_MAJOR}).",
        "Installer la commande de la même version que l'API.",
    ),
    "host_not_allowed": Anomaly(
        "address",
        "L'API refuse le nom d'hôte de cette adresse (en-tête Host).",
        "Ajouter ce nom à ANSWERLINEAGE_ALLOWED_HOSTS dans l'environnement de l'API, "
        "ou la joindre par la boucle locale.",
    ),
    "origin_not_allowed": Anomaly(
        "address",
        "L'API refuse l'origine de cette adresse (en-tête Origin) : une page servie à cette "
        "adresse ne pourrait pas l'appeler.",
        "Ajouter cette origine à ANSWERLINEAGE_ALLOWED_ORIGINS, ou son nom d'hôte à "
        "ANSWERLINEAGE_ALLOWED_HOSTS, dans l'environnement de l'API.",
    ),
    "address_refused": Anomaly(
        "address",
        "L'API refuse les requêtes faites à cette adresse.",
        "Vérifier ANSWERLINEAGE_ALLOWED_HOSTS et ANSWERLINEAGE_ALLOWED_ORIGINS dans "
        "l'environnement de l'API.",
    ),
    "token_missing": Anomaly(
        "access",
        "L'API demande un jeton porteur OIDC, et la commande n'en a pas.",
        f"Donner un jeton par {ENV_TOKEN}, ou par un fichier ({ENV_TOKEN_FILE} ou --token-file).",
    ),
    "token_refused": Anomaly(
        "access",
        "L'API refuse le jeton : il est mal formé, expiré, ou n'a pas été émis pour elle.",
        "Obtenir un nouveau jeton de l'émetteur OIDC de l'API, pour son audience.",
    ),
    "role_insufficient": Anomaly(
        "access",
        "Le jeton n'a pas le rôle qu'ouvre la route de santé : administrateur ou auditeur.",
        "Demander l'un de ces rôles à l'administrateur de l'émetteur OIDC "
        "(docs/securite/authentification.md).",
    ),
    "authentication_unavailable": Anomaly(
        "access",
        "L'API ne peut pas vérifier le jeton : son émetteur OIDC ne lui répond pas.",
        "Vérifier que l'émetteur OIDC tourne et que l'API le joint "
        "(ANSWERLINEAGE_OIDC_ISSUER dans l'environnement de l'API).",
    ),
    "database_unavailable": Anomaly(
        "database",
        "L'API ne lit pas sa base PostgreSQL, ou pas dans son délai.",
        "Vérifier que PostgreSQL tourne, et que ANSWERLINEAGE_DATABASE_URL, dans "
        "l'environnement de l'API, le désigne.",
    ),
    "schema_not_migrated": Anomaly(
        "migrations",
        "La base n'a pas le schéma d'AnswerLineage : aucune migration n'est appliquée.",
        _MIGRATE,
    ),
    "schema_behind": Anomaly(
        "migrations",
        "Le schéma de la base est en retard sur l'API : des migrations restent à appliquer.",
        _MIGRATE,
    ),
    "schema_unknown_revision": Anomaly(
        "migrations",
        "La base porte une révision du schéma que l'API ne connaît pas : l'API est plus "
        "ancienne que la base.",
        "Mettre l'API à la version qui a migré la base.",
    ),
    "queue_stalled": Anomaly(
        "queue",
        "La file d'ingestion n'avance pas : {pending}, le plus ancien depuis {age} "
        "(seuil : {threshold}).",
        "Lancer un worker (answerlineage-worker) ; s'il tourne, lire son journal : un "
        "événement fautif y est remis à plus tard, de plus en plus espacé.",
    ),
    "dead_letters_waiting": Anomaly(
        "dead_letters",
        "{events} au rebut, que les workers n'ont pas pu intégrer au graphe ; le plus ancien "
        "y est depuis {age}.",
        "Lire le motif de chacun (answerlineage dead-letters list) et corriger sa cause, puis "
        "les rejouer (answerlineage dead-letters replay) : un événement qui échoue encore "
        "revient au rebut.",
    ),
    "emitters_muted": Anomaly(
        "emitters",
        "{muted} : aucun relevé de couverture reçu depuis le seuil du contrôle des workers.",
        "Vérifier que ces émetteurs tournent et joignent l'API ; GET /api/v1/coverage les "
        "nomme (champ muted_since).",
    ),
    "partitions_missing": Anomaly(
        "partitions",
        "{missing} : l'ingestion échouera pour un mois sans partition.",
        "Vérifier qu'un worker tourne : il crée les partitions chaque heure, et son journal "
        "nomme la table et le mois qui manquent.",
    ),
    "reemission_lagging": Anomaly(
        "reemission",
        "La réémission vers le consommateur OpenLineage externe est en retard : {queued}, le "
        "plus ancien depuis {age} (seuil : {threshold}) ; {lost} depuis la mise en service.",
        "Vérifier que la destination répond et que les workers la joignent : leur journal donne "
        "la classe de l'échec ou le statut de la réponse. Vérifier ANSWERLINEAGE_REEMIT_URL et "
        "ANSWERLINEAGE_REEMIT_AUTHORIZATION dans l'environnement des workers, et qu'un worker "
        "tourne avec ce réglage. Au-delà des bornes de la file, les plus anciens sont perdus.",
    ),
    "webhooks_lagging": Anomaly(
        "webhooks",
        "Les webhooks des incidents sont en retard : {queued}, le plus ancien depuis {age} "
        "(seuil : {threshold}) ; {lost} depuis la mise en service.",
        "Vérifier que le récepteur répond et que les workers le joignent : leur journal donne "
        "la classe de l'échec ou le statut de la réponse ; un récepteur qui refuse la signature "
        "répond souvent 401 ou 403, vérifier alors le secret. Vérifier ANSWERLINEAGE_WEBHOOK_URL "
        "et ANSWERLINEAGE_WEBHOOK_SECRET dans l'environnement des workers, et qu'un worker "
        "tourne avec ce réglage. Au-delà des bornes de la file, les plus anciens sont perdus.",
    ),
    "proposals_waiting": Anomaly(
        "proposals",
        "Des incidents proposés par des tests de qualité en échec attendent une décision : "
        "{waiting}, la plus ancienne depuis {age} (seuil : {threshold}) ; {refused}, {lost} "
        "depuis la mise en service.",
        "Les lire (answerlineage incident proposals --state signaled), puis confirmer chacune "
        "(answerlineage incident confirm <identifiant> --severity <gravité>) ou l'écarter "
        "(answerlineage incident dismiss <identifiant>). Sans décision, une proposition expire "
        "après 7 jours sans nouvel échec, et au-delà de 100 ouvertes, les échecs suivants sont "
        "refusés.",
    ),
    "explorer_missing": Anomaly(
        "explorer",
        "L'explorateur n'est pas servi à cette adresse.",
        "Construire l'explorateur (make ui) et lancer l'API avec ANSWERLINEAGE_UI_DIR=ui/dist ; "
        "l'image de démonstration le sert déjà.",
    ),
}

_WORKERS = (
    "Les workers ne publient aucun signal de vie : leur marche se lit dans la file, qui "
    "avance, et dans le contrôle des émetteurs muets, qui ne tourne qu'avec eux."
)


@dataclass(frozen=True)
class Check:
    """Le résultat d'une vérification. ``code`` est celui de l'anomalie, s'il y en a une."""

    check: str
    status: Status
    code: str | None
    message: str
    remedy: str | None


def _ok(check: str, message: str) -> Check:
    return Check(check, "ok", None, message, None)


def _unchecked(check: str, reason: str) -> Check:
    return Check(check, "not_checked", None, f"Non vérifié : {reason}", None)


def _anomaly(code: str, **facts: str) -> Check:
    anomaly = ANOMALIES[code]
    return Check(anomaly.check, "anomaly", code, anomaly.message.format(**facts), anomaly.remedy)


def _plural(count: int, one: str, many: str, *, beyond: bool = False) -> str:
    return f"{'plus de ' if beyond else ''}{count} {one if count <= 1 else many}"


def duration(seconds: int) -> str:
    """« 45 s », « 12 min », « 2 h 05 min », « 3 j 4 h »."""
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes:02d} min"
    days, hours = divmod(hours, 24)
    return f"{days} j {hours} h"


@dataclass(frozen=True)
class _Tallies:
    """Les décomptes de la route de santé, donnés quand le schéma est à jour."""

    queue: dict[str, Any]
    dead_letters: dict[str, Any]
    emitters: dict[str, Any]
    partitions: dict[str, Any]
    # Absentes d'une API qui ne les publie pas encore.
    reemission: dict[str, Any] | None
    webhooks: dict[str, Any] | None
    proposals: dict[str, Any] | None = None


@dataclass(frozen=True)
class _Health:
    version: str
    database: str
    migrations: str | None
    tallies: _Tallies | None


def _tally(value: Any, where: str, fields: Sequence[str]) -> dict[str, Any] | None:
    if value is None:
        return None
    document = shape.obj(value, where, fields)
    checked: dict[str, Any] = {}
    for key in fields:
        if key in ("lower_bound", "in_service"):
            checked[key] = shape.flag(document[key], f"{where}/{key}")
        elif key == "oldest_age_seconds":
            checked[key] = shape.optional_count(document[key], f"{where}/{key}")
        else:
            checked[key] = shape.count(document[key], f"{where}/{key}")
    return checked


def parse_health(document: Any) -> _Health:
    """La réponse de la route de santé, validée ; ``InvalidResponseError`` si incohérente."""
    fields = (
        "api_version",
        "database",
        "migrations",
        "queue",
        "dead_letters",
        "emitters",
        "partitions",
    )
    found = shape.obj(document, "", fields, ("reemission", "webhooks", "proposals"))
    version = shape.version(found["api_version"], "/api_version")
    database = shape.choice(found["database"], "/database", DATABASE_STATES)
    migrations = found["migrations"]
    if migrations is not None:
        migrations = shape.choice(migrations, "/migrations", MIGRATION_STATES)
    queue = _tally(found["queue"], "/queue", ("pending", "lower_bound", "oldest_age_seconds"))
    buried = _tally(
        found["dead_letters"], "/dead_letters", ("events", "lower_bound", "oldest_age_seconds")
    )
    emitters = _tally(found["emitters"], "/emitters", ("known", "muted", "lower_bound"))
    partitions = _tally(found["partitions"], "/partitions", ("missing", "lower_bound"))
    reemission = _tally(
        found.get("reemission"),
        "/reemission",
        ("queued", "retrying", "lower_bound", "oldest_age_seconds", "sent", "lost", "rejected"),
    )
    webhooks = _tally(
        found.get("webhooks"),
        "/webhooks",
        (
            "in_service",
            "queued",
            "retrying",
            "lower_bound",
            "oldest_age_seconds",
            "sent",
            "lost",
            "rejected",
        ),
    )
    proposals = _tally(
        found.get("proposals"), "/proposals", ("open", "oldest_age_seconds", "refused", "lost")
    )
    # Une base disponible a un état de ses migrations ; un schéma à jour, ses décomptes.
    if (database == "available") != (migrations is not None):
        raise shape.fault("/migrations")
    if queue is None or buried is None or emitters is None or partitions is None:
        if migrations == "current":
            raise shape.fault("/queue")
        return _Health(version, database, migrations, None)
    if migrations != "current":
        raise shape.fault("/queue")
    # Le rebut a un plus ancien dès qu'il n'est pas vide, et seulement alors.
    if (buried["events"] == 0) != (buried["oldest_age_seconds"] is None):
        raise shape.fault("/dead_letters/oldest_age_seconds")
    if "proposals" in found and proposals is None:
        raise shape.fault("/proposals")
    # Des propositions qui attendent ont une plus ancienne, et seulement alors.
    if proposals is not None and (proposals["open"] == 0) != (
        proposals["oldest_age_seconds"] is None
    ):
        raise shape.fault("/proposals/oldest_age_seconds")
    for name, tally in (("reemission", reemission), ("webhooks", webhooks)):
        if name in found and tally is None:
            # Une API qui publie cet état le donne dès que le schéma est à jour.
            raise shape.fault(f"/{name}")
        if tally is not None:
            if (tally["queued"] == 0) != (tally["oldest_age_seconds"] is None):
                raise shape.fault(f"/{name}/oldest_age_seconds")
            if tally["rejected"] > tally["lost"]:
                raise shape.fault(f"/{name}/rejected")
    return _Health(
        version,
        database,
        migrations,
        _Tallies(queue, buried, emitters, partitions, reemission, webhooks, proposals),
    )


def _refusal_code(received: Received) -> str | None:
    try:
        document = received.json()
    except InvalidResponseError:
        return None
    code = document.get("error") if isinstance(document, dict) else None
    return code if isinstance(code, str) else None


def _origin(base: str) -> str:
    scheme, _, rest = base.partition("://")
    return f"{scheme}://{rest.partition('/')[0]}"


class _Report:
    """Les vérifications, rangées par nom ; celles qui manquent à la fin ne sont pas vérifiées."""

    def __init__(self) -> None:
        self.checks: dict[str, Check] = {}

    def add(self, check: Check) -> None:
        self.checks.setdefault(check.check, check)

    def finish(self, reason: str) -> list[Check]:
        self.add(Check("workers", "not_checked", None, _WORKERS, None))
        for name in CHECKS:
            self.add(_unchecked(name, reason))
        return [self.checks[name] for name in CHECKS]


def diagnose(
    api: Api,
    *,
    queue_max_age: int = DEFAULT_QUEUE_MAX_AGE,
    reemission_max_age: int = DEFAULT_REEMISSION_MAX_AGE,
    webhook_max_age: int = DEFAULT_WEBHOOK_MAX_AGE,
) -> list[Check]:
    report = _Report()
    try:
        received = api.get(
            HEALTH, max_bytes=MAX_HEALTH_BYTES, headers={"origin": _origin(api.base)}
        )
        if received.status == 403 and _refusal_code(received) == "origin_not_allowed":
            report.add(_anomaly("origin_not_allowed"))
            received = api.get(HEALTH, max_bytes=MAX_HEALTH_BYTES)
    except UnreachableError:
        report.add(_anomaly("api_unreachable"))
        return report.finish("l'API ne répond pas.")
    except InvalidResponseError:
        report.add(_anomaly("api_invalid_response"))
        return report.finish("la réponse de l'API n'est pas lisible.")
    except NotAuthorizedError as refused:
        report.add(_ok("api", "L'API répond."))
        report.add(_ok("address", "L'API accepte cette adresse (en-têtes Host et Origin)."))
        report.add(_anomaly(_ACCESS_REFUSALS[refused.code]))
        _explorer(api, report)
        return report.finish("l'API ne livre pas son état sans un jeton qu'elle accepte.")

    refusal = _refusal_code(received) if received.status != 200 else None
    if received.status == 403 and refusal in (
        "host_not_allowed",
        "origin_not_allowed",
        "cross_site_not_allowed",
    ):
        report.add(_ok("api", "L'API répond."))
        report.add(
            _anomaly("host_not_allowed" if refusal == "host_not_allowed" else "address_refused")
        )
        return report.finish("l'API refuse cette adresse.")
    if received.status == 503 and refusal == "query_timeout":
        report.add(_ok("api", "L'API répond."))
        report.add(_ok("address", "L'API accepte cette adresse (en-têtes Host et Origin)."))
        report.add(_access(api))
        report.add(_anomaly("database_unavailable"))
        _explorer(api, report)
        return report.finish("l'API ne lit pas sa base.")
    try:
        if received.status != 200:
            raise InvalidResponseError("statut inattendu")
        health = parse_health(received.json())
    except InvalidResponseError:
        report.add(_anomaly("api_invalid_response"))
        return report.finish("la réponse de l'API n'est pas lisible.")
    if int(health.version.partition(".")[0]) != API_MAJOR:
        report.add(_anomaly("api_version_unsupported"))
        return report.finish("la version de l'API n'est pas lue par la commande.")

    report.add(_ok("api", f"L'API répond : API {health.version}."))
    report.add(_ok("address", "L'API accepte cette adresse (en-têtes Host et Origin)."))
    report.add(_access(api))
    _installation(health, report, queue_max_age, reemission_max_age, webhook_max_age)
    _explorer(api, report)
    return report.finish(_unchecked_reason(health))


# Un refus de l'API pour le jeton, et l'anomalie qui le dit.
_ACCESS_REFUSALS: Final = {
    "missing_token": "token_missing",
    "invalid_token": "token_refused",
    "expired_token": "token_refused",
    "insufficient_role": "role_insufficient",
    "authentication_unavailable": "authentication_unavailable",
}


def _access(api: Api) -> Check:
    """L'API a livré son état : avec le jeton de la commande, ou sans en demander."""
    if api.authenticated:
        return _ok("access", "L'API accepte le jeton, et son rôle ouvre la route de santé.")
    return _ok(
        "access",
        "L'API répond sans jeton : son authentification est désactivée, comme dans la "
        "démonstration.",
    )


def _unchecked_reason(health: _Health) -> str:
    if health.database != "available":
        return "l'API ne lit pas sa base."
    return "le schéma de la base n'est pas à jour."


def _installation(
    health: _Health,
    report: _Report,
    queue_max_age: int,
    reemission_max_age: int,
    webhook_max_age: int,
) -> None:
    if health.database != "available":
        report.add(_anomaly("database_unavailable"))
        return
    report.add(_ok("database", "L'API lit sa base PostgreSQL."))
    if health.migrations != "current":
        report.add(
            _anomaly(
                {
                    "not_migrated": "schema_not_migrated",
                    "behind": "schema_behind",
                    "unknown_revision": "schema_unknown_revision",
                }[str(health.migrations)]
            )
        )
        return
    report.add(_ok("migrations", "Le schéma de la base est à jour."))
    if health.tallies is None:  # pragma: no cover — parse_health le refuse pour un schéma à jour.
        return
    report.add(_queue(health.tallies.queue, queue_max_age))
    report.add(_dead_letters(health.tallies.dead_letters))
    report.add(_emitters(health.tallies.emitters))
    partitions = health.tallies.partitions
    if partitions["missing"]:
        missing = _plural(
            partitions["missing"],
            "partition mensuelle manquante signalée par les workers",
            "partitions mensuelles manquantes signalées par les workers",
            beyond=partitions["lower_bound"],
        )
        report.add(_anomaly("partitions_missing", missing=_sentence(missing)))
    else:
        report.add(_ok("partitions", "Aucune partition mensuelle ne manque."))
    report.add(_reemission(health.tallies.reemission, reemission_max_age))
    report.add(_webhooks(health.tallies.webhooks, webhook_max_age))
    report.add(_proposals(health.tallies.proposals))


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def _queue(queue: Mapping[str, Any], max_age: int) -> Check:
    age = queue["oldest_age_seconds"]
    beyond = queue["lower_bound"]
    pending = _plural(
        queue["pending"], "événement en attente", "événements en attente", beyond=beyond
    )
    if age is None:
        return _ok("queue", "La file d'ingestion est vide.")
    if age > max_age:
        return _anomaly(
            "queue_stalled", pending=pending, age=duration(age), threshold=duration(max_age)
        )
    oldest = duration(age)
    return _ok("queue", f"La file d'ingestion avance : {pending}, le plus ancien depuis {oldest}.")


def _dead_letters(buried: Mapping[str, Any]) -> Check:
    if buried["events"] == 0:
        return _ok("dead_letters", "La file de rebut est vide.")
    events = _plural(buried["events"], "événement", "événements", beyond=buried["lower_bound"])
    return _anomaly(
        "dead_letters_waiting",
        events=_sentence(events),
        age=duration(buried["oldest_age_seconds"]),
    )


def _emitters(emitters: Mapping[str, Any]) -> Check:
    beyond = emitters["lower_bound"]
    if emitters["known"] == 0:
        return _unchecked(
            "emitters",
            "aucun émetteur n'a publié de relevé de couverture : leur silence ne se mesure pas.",
        )
    if emitters["muted"]:
        muted = _plural(emitters["muted"], "émetteur muet", "émetteurs muets", beyond=beyond)
        return _anomaly("emitters_muted", muted=_sentence(muted))
    known = _plural(emitters["known"], "émetteur connu", "émetteurs connus", beyond=beyond)
    return _ok("emitters", f"{_sentence(known)}, aucun muet.")


def _reemission(reemission: Mapping[str, Any] | None, max_age: int) -> Check:
    if reemission is None:
        return _unchecked("reemission", "l'API ne publie pas l'état de la réémission.")
    if not (reemission["queued"] or reemission["sent"] or reemission["lost"]):
        return _unchecked(
            "reemission",
            "aucun événement n'a été mis en file de réémission : sans destination réglée aux "
            "workers, rien n'est réémis.",
        )
    beyond = reemission["lower_bound"]
    queued = _plural(
        reemission["queued"], "événement à réémettre", "événements à réémettre", beyond=beyond
    )
    lost = _plural(reemission["lost"], "perdu", "perdus")
    age = reemission["oldest_age_seconds"]
    if age is not None and age > max_age:
        return _anomaly(
            "reemission_lagging",
            queued=queued,
            age=duration(age),
            threshold=duration(max_age),
            lost=lost,
        )
    sent = _plural(reemission["sent"], "remis", "remis")
    waiting = "file vide" if age is None else f"{queued}, le plus ancien depuis {duration(age)}"
    return _ok("reemission", f"La réémission avance : {waiting} ; {sent}, {lost}.")


def _webhooks(webhooks: Mapping[str, Any] | None, max_age: int) -> Check:
    if webhooks is None:
        return _unchecked("webhooks", "l'API ne publie pas l'état des webhooks.")
    age = webhooks["oldest_age_seconds"]
    if not webhooks["in_service"] and age is None:
        return _unchecked(
            "webhooks",
            "aucun webhook n'est en service : sans webhook réglé aux workers, aucun changement "
            "d'état d'incident n'est annoncé.",
        )
    queued = _plural(
        webhooks["queued"],
        "message à envoyer",
        "messages à envoyer",
        beyond=webhooks["lower_bound"],
    )
    lost = _plural(webhooks["lost"], "perdu", "perdus")
    if age is not None and age > max_age:
        return _anomaly(
            "webhooks_lagging",
            queued=queued,
            age=duration(age),
            threshold=duration(max_age),
            lost=lost,
        )
    sent = _plural(webhooks["sent"], "remis", "remis")
    waiting = "file vide" if age is None else f"{queued}, le plus ancien depuis {duration(age)}"
    return _ok("webhooks", f"Les webhooks avancent : {waiting} ; {sent}, {lost}.")


def _proposals(proposals: Mapping[str, Any] | None) -> Check:
    if proposals is None:
        return _unchecked("proposals", "l'API ne publie pas l'état des incidents proposés.")
    refused = _plural(proposals["refused"], "signal refusé", "signaux refusés")
    lost = _plural(proposals["lost"], "perdu", "perdus")
    age = proposals["oldest_age_seconds"]
    if age is None:
        return _ok(
            "proposals",
            f"Aucun incident proposé n'attend de décision ; {refused}, {lost}.",
        )
    waiting = _plural(proposals["open"], "proposition en attente", "propositions en attente")
    if age > PROPOSAL_MAX_AGE:
        return _anomaly(
            "proposals_waiting",
            waiting=waiting,
            age=duration(age),
            threshold=duration(PROPOSAL_MAX_AGE),
            refused=refused,
            lost=lost,
        )
    return _ok(
        "proposals",
        f"{_sentence(waiting)}, la plus ancienne depuis {duration(age)} ; {refused}, {lost}.",
    )


def _explorer(api: Api, report: _Report) -> None:
    try:
        page = api.get("/", max_bytes=MAX_PAGE_BYTES, headers={"accept": "text/html"})
    except (UnreachableError, InvalidResponseError):
        report.add(_anomaly("explorer_missing"))
        return
    if page.status == 200 and page.content_type == "text/html" and b'id="root"' in page.body:
        report.add(_ok("explorer", f"L'explorateur est servi : {api.base}/"))
    else:
        report.add(_anomaly("explorer_missing"))


def anomalies(checks: Sequence[Check]) -> list[str]:
    return [check.code for check in checks if check.code is not None]


def render_json(base: str, checks: Sequence[Check]) -> str:
    report = {
        "url": base,
        "checks": [asdict(check) for check in checks],
        "anomalies": anomalies(checks),
    }
    return json.dumps(report, ensure_ascii=False, indent=2) + "\n"


_STATUS_LABELS: Final = {"ok": "bon", "anomaly": "anomalie", "not_checked": "non vérifié"}


def render_text(base: str, checks: Sequence[Check]) -> str:
    lines = [f"Vérification de l'installation : {base}", ""]
    indent = " " * 28
    for check in checks:
        status = _STATUS_LABELS[check.status]
        message = check.message if check.code is None else f"{check.code} : {check.message}"
        lines.append(f"  {status:<12} {_LABELS[check.check]:<12} {message}")
        if check.remedy is not None:
            lines.append(f"{indent}Remède : {check.remedy}")
    found = anomalies(checks)
    lines += [
        "",
        "Aucune anomalie." if not found else _plural(len(found), "anomalie.", "anomalies."),
    ]
    return "\n".join(lines) + "\n"
