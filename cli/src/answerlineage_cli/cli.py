"""Ligne de commande : ``answerlineage impact | exposure | incident | dead-letters | …``.

``impact`` propage un incident de données jusqu'aux réponses et aux
destinataires touchés ; ``exposure`` classe les jeux de données et leurs
colonnes selon les réponses qui en dépendent ; ``incident`` déclare un
incident, lit son analyse, le met en quarantaine et la lève, lit les incidents
proposés par des tests de qualité en échec, les confirme ou les écarte ;
``dead-letters`` consulte et rejoue la file de rebut des événements que la
projection n'a pas pu intégrer ; ``doctor`` vérifie chaque composant d'une
installation ; ``demo`` lance la
démonstration d'un clone du dépôt. Toutes parlent à l'API par HTTP, à
l'adresse de ``--url`` ou de ``ANSWERLINEAGE_API_URL``,
``http://127.0.0.1:8000`` par défaut. ``proxy`` cède la place au proxy MCP,
avec ses arguments (``proxy.py``).

Codes de sortie (``exits.py``) : 0 succès ; 1 rien de touché (impact,
incident show), une anomalie (doctor), un échec de Docker (demo), des
événements au rebut (dead-letters list) ou rien à rejouer (dead-letters
replay) ; 2 usage fautif ; 3 résultat incomplet (impact, exposure, incident
show) ou rejeu à poursuivre (dead-letters replay) ; 4 API injoignable ; 5 refus de l'API
ou réponse invalide ; 6 analyse en attente et 7 analyse en échec (incident
show).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

import httpx

from answerlineage_cli import dead_letters, demo, doctor, exposure, impact, incident, proxy
from answerlineage_cli.client import (
    DEFAULT_TIMEOUT,
    DEFAULT_URL,
    ENV_URL,
    MAX_TIMEOUT,
    Api,
    InvalidResponseError,
    UnreachableError,
    UsageError,
    configured_base,
    is_loopback,
)
from answerlineage_cli.exits import Exit

# Les bornes de known_at dans l'API : rien n'est enregistré avant 1970.
_EARLIEST = datetime(1970, 1, 1, tzinfo=UTC)
_LATEST = datetime(9999, 12, 30, tzinfo=UTC)
_MAX_INSTANT_LENGTH = 64


def _bounded(low: int, high: int, unit: str) -> Callable[[str], int]:
    def parse(value: str) -> int:
        if not value.isdigit() or not low <= int(value) <= high:
            raise argparse.ArgumentTypeError(f"un nombre entier {unit}, de {low} à {high}")
        return int(value)

    return parse


def instant(value: str) -> str:
    """Une date et une heure RFC 3339 avec leur fuseau, rendue en UTC pour l'API."""
    refusal = argparse.ArgumentTypeError(
        "une date et une heure RFC 3339 avec leur fuseau, par exemple 2026-11-11T06:00:00Z"
    )
    if len(value) > _MAX_INSTANT_LENGTH or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt ].+", value
    ):
        raise refusal
    try:
        parsed = datetime.fromisoformat(value.upper())
        if parsed.tzinfo is None:
            raise refusal
        moment = parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        raise refusal from None
    return moment.isoformat().replace("+00:00", "Z")


def _dataset_parameter(value: str) -> str:
    if not 1 <= len(value) <= impact.MAX_DATASET_PARAMETER or "\x00" in value:
        raise argparse.ArgumentTypeError(
            f"de 1 à {impact.MAX_DATASET_PARAMETER} caractères, sans caractère nul"
        )
    return value


def _cursor(value: str) -> str:
    if not impact.CURSOR.fullmatch(value):
        raise argparse.ArgumentTypeError("la valeur de next_cursor d'une page précédente")
    return value


def _exposure_cursor(value: str) -> str:
    if not exposure.CURSOR.fullmatch(value):
        raise argparse.ArgumentTypeError("la valeur de next_cursor d'une page précédente")
    return value


def _severity(value: str) -> str:
    if value not in incident.SEVERITIES:
        raise argparse.ArgumentTypeError(
            "une gravité de la liste : " + ", ".join(incident.SEVERITIES)
        )
    return value


def _quarantine_mode(value: str) -> str:
    if value not in incident.QUARANTINE_MODES:
        raise argparse.ArgumentTypeError(
            "un mode de la liste : " + ", ".join(incident.QUARANTINE_MODES)
        )
    return value


def _incident_id(value: str) -> str:
    identifier = value.lower()
    if not incident.INCIDENT_ID.fullmatch(identifier):
        raise argparse.ArgumentTypeError(
            "l'identifiant d'un incident, tel que le rend answerlineage incident declare"
        )
    return identifier


def _proposal_id(value: str) -> str:
    identifier = value.lower()
    if not incident.INCIDENT_ID.fullmatch(identifier):
        raise argparse.ArgumentTypeError(
            "l'identifiant d'une proposition, tel que le rend answerlineage incident proposals"
        )
    return identifier


def _proposal_state(value: str) -> str:
    if value not in incident.PROPOSAL_STATES:
        raise argparse.ArgumentTypeError(
            "un état de la liste : " + ", ".join(incident.PROPOSAL_STATES)
        )
    return value


def _proposal_cursor(value: str) -> str:
    if not incident.PROPOSAL_CURSOR.fullmatch(value):
        raise argparse.ArgumentTypeError("la valeur de next_cursor d'une page précédente")
    return value


def _reason(value: str) -> str:
    if value not in dead_letters.REASONS:
        raise argparse.ArgumentTypeError(
            "un motif de la liste : " + ", ".join(dead_letters.REASONS)
        )
    return value


def _event_id(value: str) -> str:
    identifier = value.lower()
    if not dead_letters.EVENT_ID.fullmatch(identifier):
        raise argparse.ArgumentTypeError(
            "l'identifiant d'un événement : 64 chiffres hexadécimaux, tel que le rend "
            "answerlineage dead-letters list"
        )
    return identifier


def _dead_letter_cursor(value: str) -> str:
    if not dead_letters.CURSOR.fullmatch(value):
        raise argparse.ArgumentTypeError("la valeur de next_cursor d'une page précédente")
    return value


def _emitter(value: str) -> str:
    if len(value) > dead_letters.MAX_EMITTER or "\x00" in value:
        raise argparse.ArgumentTypeError(
            f"un espace de noms de {dead_letters.MAX_EMITTER} caractères au plus"
        )
    return value


def _dataset_part(value: str) -> str:
    if len(value) > incident.MAX_DATASET_ARGUMENT or "\x00" in value:
        raise argparse.ArgumentTypeError(
            f"un espace de noms et un nom de {incident.MAX_DATASET_ARGUMENT} caractères au plus"
        )
    return value


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", help=f"adresse de l'API (ou {ENV_URL}), {DEFAULT_URL} par défaut")
    parser.add_argument(
        "--timeout",
        type=_bounded(1, MAX_TIMEOUT, "de secondes"),
        default=int(DEFAULT_TIMEOUT),
        help=f"délai maximal d'une requête, en secondes, {int(DEFAULT_TIMEOUT)} par défaut",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="answerlineage",
        description=(
            "AnswerLineage depuis le terminal : propager un incident, classer les jeux de "
            "données par exposition, consulter et rejouer la file de rebut, vérifier une "
            "installation, lancer la démonstration."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    propagate = commands.add_parser(
        "impact",
        help="réponses et destinataires touchés par un jeu de données faux pendant une fenêtre",
        description=(
            "Propage un incident de données jusqu'aux réponses et aux destinataires touchés. "
            "Codes de sortie : 0 réponses touchées, liste complète ; 1 aucune réponse touchée, "
            "résultat complet ; 3 résultat incomplet (tronqué, ou couverture du lignage non "
            "mesurée, partielle ou trouée) ; 2 usage ; 4 API injoignable ; 5 refus de l'API."
        ),
    )
    propagate.add_argument(
        "--namespace",
        required=True,
        type=_dataset_parameter,
        help="espace de noms du jeu de données, par exemple bigquery",
    )
    propagate.add_argument(
        "--name", required=True, type=_dataset_parameter, help="nom du jeu de données"
    )
    propagate.add_argument(
        "--from",
        dest="start",
        required=True,
        type=instant,
        help="début de la fenêtre, inclus (RFC 3339, avec fuseau)",
    )
    propagate.add_argument(
        "--to",
        dest="end",
        required=True,
        type=instant,
        help="fin de la fenêtre, exclue (RFC 3339, avec fuseau)",
    )
    propagate.add_argument(
        "--limit",
        type=_bounded(1, impact.MAX_LIMIT, "de réponses"),
        help=f"réponses par page, de 1 à {impact.MAX_LIMIT}, {impact.MAX_LIMIT} par défaut",
    )
    propagate.add_argument(
        "--cursor", type=_cursor, help="page suivante : la valeur de next_cursor"
    )
    propagate.add_argument(
        "--known-at",
        type=instant,
        help="lit le graphe tel qu'il était connu à cette date (RFC 3339, avec fuseau)",
    )
    propagate.add_argument("--format", choices=("text", "json"), default="text")
    _common(propagate)

    ranking = commands.add_parser(
        "exposure",
        help="jeux de données et colonnes classés selon les réponses qui en dépendent",
        description=(
            "Classe les jeux de données, et leurs colonnes, par score d'exposition : chaque "
            "réponse qui en dépend y pèse 1 plus un par destinataire, moitié moins tous les "
            "30 jours. Codes de sortie : 0 classement complet ; 3 classement incomplet (une "
            "borne est dépassée, les scores sont des minimums) ; 2 usage ; 4 API injoignable ; "
            "5 refus de l'API."
        ),
    )
    ranking.add_argument(
        "--limit",
        type=_bounded(1, exposure.MAX_LIMIT, "de jeux de données"),
        help=f"jeux de données par page, de 1 à {exposure.MAX_LIMIT}, 50 par défaut",
    )
    ranking.add_argument(
        "--cursor", type=_exposure_cursor, help="page suivante : la valeur de next_cursor"
    )
    ranking.add_argument(
        "--at",
        type=instant,
        help=(
            "heure de référence du score (RFC 3339, avec fuseau) ; par défaut, la date de "
            "--known-at, sinon l'heure de la base"
        ),
    )
    ranking.add_argument(
        "--known-at",
        type=instant,
        help="lit le graphe tel qu'il était connu à cette date (RFC 3339, avec fuseau)",
    )
    ranking.add_argument("--format", choices=("text", "json"), default="text")
    _common(ranking)

    incidents = commands.add_parser(
        "incident",
        help="déclarer un incident de données et lire son analyse d'impact",
        description=(
            "Déclare un incident de données (declare) et lit l'analyse d'impact que les "
            "workers en font (show) ; lit les incidents proposés par des tests de qualité en "
            "échec (proposals), les confirme (confirm) ou les écarte (dismiss)."
        ),
    )
    actions = incidents.add_subparsers(dest="action", required=True)
    declare = actions.add_parser(
        "declare",
        help="déclare un incident : périmètre, fenêtre de validité et gravité",
        description=(
            "Déclare un incident de données. L'API le crée à l'état « qualifié » et demande "
            "son analyse d'impact. Codes de sortie : 0 incident déclaré ; 2 usage, ou "
            "déclaration refusée par l'API ; 4 API injoignable ; 5 refus de l'API."
        ),
    )
    declare.add_argument(
        "--dataset",
        dest="datasets",
        nargs=2,
        action="append",
        required=True,
        metavar=("ESPACE", "NOM"),
        type=_dataset_part,
        help=(
            "un jeu de données faux, par son espace de noms et son nom ; à répéter, "
            f"{incident.MAX_DATASETS} fois au plus"
        ),
    )
    declare.add_argument(
        "--from",
        dest="start",
        required=True,
        type=instant,
        help="début de la fenêtre de validité, inclus (RFC 3339, avec fuseau)",
    )
    declare.add_argument(
        "--to",
        dest="end",
        required=True,
        type=instant,
        help=(
            "fin de la fenêtre de validité, exclue (RFC 3339, avec fuseau) ; "
            f"{incident.MAX_WINDOW_DAYS} jours au plus après --from"
        ),
    )
    declare.add_argument(
        "--severity",
        required=True,
        type=_severity,
        help="gravité : " + ", ".join(incident.SEVERITIES),
    )
    declare.add_argument("--format", choices=("text", "json"), default="text")
    _common(declare)
    show = actions.add_parser(
        "show",
        help="l'incident, l'état de son analyse et les réponses touchées",
        description=(
            "Lit un incident et son analyse d'impact. Codes de sortie : 0 réponses touchées, "
            "liste complète ; 1 aucune réponse touchée, résultat complet ; 3 résultat "
            "incomplet ; 6 analyse en attente ; 7 analyse en échec ; 2 usage ; "
            "4 API injoignable ; 5 refus de l'API."
        ),
    )
    show.add_argument(
        "incident_id",
        metavar="IDENTIFIANT",
        type=_incident_id,
        help="l'identifiant de l'incident, tel que le rend answerlineage incident declare",
    )
    show.add_argument(
        "--limit",
        type=_bounded(1, incident.MAX_LIMIT, "de réponses"),
        help=f"réponses par page, de 1 à {incident.MAX_LIMIT}, {incident.MAX_LIMIT} par défaut",
    )
    show.add_argument("--cursor", type=_cursor, help="page suivante : la valeur de next_cursor")
    show.add_argument("--format", choices=("text", "json"), default="text")
    _common(show)
    quarantine = actions.add_parser(
        "quarantine",
        help="met un incident en quarantaine, ou la lève : les proxys reçoivent son périmètre",
        description=(
            "Met un incident en quarantaine : les proxys reçoivent son périmètre à leur "
            "prochaine synchronisation. --lift la lève. Les deux sont idempotents. Codes de "
            "sortie : 0 incident en quarantaine, ou quarantaine levée, par cette commande ou "
            "avant elle ; 2 usage ; 4 API injoignable ; 5 refus de l'API (incident inconnu, "
            "borne des quarantaines actives atteinte, rien à lever)."
        ),
    )
    quarantine.add_argument(
        "incident_id",
        metavar="IDENTIFIANT",
        type=_incident_id,
        help="l'identifiant de l'incident, tel que le rend answerlineage incident declare",
    )
    chosen = quarantine.add_mutually_exclusive_group()
    chosen.add_argument(
        "--mode",
        type=_quarantine_mode,
        default=incident.QUARANTINE_MODES[0],
        help=(
            "ce que les proxys feront des lectures du périmètre : "
            + ", ".join(incident.QUARANTINE_MODES)
            + f" (par défaut {incident.QUARANTINE_MODES[0]} : les signaler)"
        ),
    )
    chosen.add_argument("--lift", action="store_true", help="lève la quarantaine de l'incident")
    quarantine.add_argument("--format", choices=("text", "json"), default="text")
    _common(quarantine)
    proposed = actions.add_parser(
        "proposals",
        help="les incidents proposés par des tests de qualité en échec",
        description=(
            "Liste les incidents proposés par des tests de qualité en échec, les plus récents "
            "d'abord : leur table, leurs colonnes, leurs assertions et leur fenêtre déduite. "
            "Codes de sortie : 0 liste lue ; 2 usage ; 4 API injoignable ; 5 refus de l'API."
        ),
    )
    proposed.add_argument(
        "--state",
        type=_proposal_state,
        help="seulement les propositions dans cet état : " + ", ".join(incident.PROPOSAL_STATES),
    )
    proposed.add_argument(
        "--limit",
        type=_bounded(1, incident.MAX_PROPOSALS, "de propositions"),
        help=f"propositions par page, de 1 à {incident.MAX_PROPOSALS}, 20 par défaut",
    )
    proposed.add_argument(
        "--cursor", type=_proposal_cursor, help="page suivante : la valeur de next_cursor"
    )
    proposed.add_argument("--format", choices=("text", "json"), default="text")
    _common(proposed)
    confirmed = actions.add_parser(
        "confirm",
        help="confirme un incident proposé : il est déclaré, son analyse demandée",
        description=(
            "Confirme un incident proposé par un test de qualité en échec : l'incident est "
            "déclaré sur sa table, avec la gravité donnée et la fenêtre déduite, ou celle de "
            "--from et --to. Idempotent. Codes de sortie : 0 proposition confirmée, par cette "
            "commande ou avant elle ; 2 usage, ou confirmation refusée par l'API ; 4 API "
            "injoignable ; 5 refus de l'API (proposition inconnue, écartée ou expirée)."
        ),
    )
    confirmed.add_argument(
        "proposal_id",
        metavar="IDENTIFIANT",
        type=_proposal_id,
        help="l'identifiant de la proposition, tel que le rend answerlineage incident proposals",
    )
    confirmed.add_argument(
        "--severity",
        required=True,
        type=_severity,
        help="gravité de l'incident : " + ", ".join(incident.SEVERITIES),
    )
    confirmed.add_argument(
        "--from",
        dest="start",
        type=instant,
        help="début de la fenêtre de validité, inclus, au lieu du début déduit (avec --to)",
    )
    confirmed.add_argument(
        "--to",
        dest="end",
        type=instant,
        help=(
            "fin de la fenêtre de validité, exclue, au lieu de la fin déduite (avec --from) ; "
            f"{incident.MAX_WINDOW_DAYS} jours au plus après --from"
        ),
    )
    confirmed.add_argument("--format", choices=("text", "json"), default="text")
    _common(confirmed)
    dismissed = actions.add_parser(
        "dismiss",
        help="écarte un incident proposé : rien n'est déclaré ni annoncé",
        description=(
            "Écarte un incident proposé par un test de qualité en échec. Idempotent. Codes de "
            "sortie : 0 proposition écartée, par cette commande ou avant elle ; 2 usage ; "
            "4 API injoignable ; 5 refus de l'API (proposition inconnue, confirmée ou expirée)."
        ),
    )
    dismissed.add_argument(
        "proposal_id",
        metavar="IDENTIFIANT",
        type=_proposal_id,
        help="l'identifiant de la proposition, tel que le rend answerlineage incident proposals",
    )
    dismissed.add_argument("--format", choices=("text", "json"), default="text")
    _common(dismissed)

    rejected = commands.add_parser(
        "dead-letters",
        help="consulter et rejouer les événements que la projection n'a pas pu intégrer",
        description=(
            "Consulte la file de rebut (list) et remet ses événements en file (replay), une fois "
            "le correctif déployé."
        ),
    )
    letters = rejected.add_subparsers(dest="action", required=True)
    listing = letters.add_parser(
        "list",
        help="les événements au rebut, leur émetteur, leur motif et les décomptes par motif",
        description=(
            "Lit la file de rebut, les plus récemment mis au rebut d'abord. Codes de sortie : "
            "0 aucun événement au rebut sous ces filtres ; 1 des événements attendent ; "
            "2 usage ; 4 API injoignable ; 5 refus de l'API."
        ),
    )
    listing.add_argument(
        "--emitter", type=_emitter, help="seulement cet émetteur, par exemple mcp://entrepot"
    )
    listing.add_argument(
        "--reason", type=_reason, help="seulement ce motif : " + ", ".join(dead_letters.REASONS)
    )
    listing.add_argument(
        "--limit",
        type=_bounded(1, dead_letters.MAX_LIMIT, "d'événements"),
        help=f"événements par page, de 1 à {dead_letters.MAX_LIMIT}, 100 par défaut",
    )
    listing.add_argument(
        "--cursor", type=_dead_letter_cursor, help="page suivante : la valeur de next_cursor"
    )
    listing.add_argument("--format", choices=("text", "json"), default="text")
    _common(listing)
    replaying = letters.add_parser(
        "replay",
        help="remet en file les événements du rebut, tous ou filtrés",
        description=(
            "Remet en file les événements du rebut que les filtres désignent, les plus anciens "
            "d'abord, en nombre borné ; tout le rebut avec --all. Codes de sortie : 0 événements "
            "remis en file, aucun ne reste ; 1 rien à rejouer ; 3 d'autres restent, relancer ; "
            "2 usage ; 4 API injoignable ; 5 refus de l'API."
        ),
    )
    replaying.add_argument("--all", action="store_true", help="tout le rebut, sans filtre")
    replaying.add_argument("--emitter", type=_emitter, help="seulement cet émetteur")
    replaying.add_argument(
        "--reason", type=_reason, help="seulement ce motif : " + ", ".join(dead_letters.REASONS)
    )
    replaying.add_argument(
        "--event-id",
        dest="event_ids",
        action="append",
        default=[],
        type=_event_id,
        metavar="IDENTIFIANT",
        help=(f"seulement cet événement ; à répéter, {dead_letters.MAX_EVENT_IDS} fois au plus"),
    )
    replaying.add_argument(
        "--limit",
        type=_bounded(1, dead_letters.MAX_REPLAY, "d'événements"),
        help=(
            f"événements remis en file au plus, de 1 à {dead_letters.MAX_REPLAY}, 1000 par défaut"
        ),
    )
    replaying.add_argument("--format", choices=("text", "json"), default="text")
    _common(replaying)

    check = commands.add_parser(
        "doctor",
        help="vérifie chaque composant d'une installation et liste les anomalies",
        description=(
            "Vérifie l'API, l'adresse, la base et ses migrations, la file d'ingestion, la file "
            "de rebut, les émetteurs, les partitions, la réémission et l'explorateur. Codes de "
            "sortie : 0 aucune "
            "anomalie ; "
            "1 au moins une anomalie ; 2 usage."
        ),
    )
    check.add_argument(
        "--queue-max-age",
        type=_bounded(0, doctor.MAX_QUEUE_MAX_AGE, "de secondes"),
        default=doctor.DEFAULT_QUEUE_MAX_AGE,
        help=(
            "âge du plus ancien événement en attente au-delà duquel la file est en retard, "
            f"en secondes, {doctor.DEFAULT_QUEUE_MAX_AGE} par défaut"
        ),
    )
    check.add_argument(
        "--reemission-max-age",
        type=_bounded(0, doctor.MAX_QUEUE_MAX_AGE, "de secondes"),
        default=doctor.DEFAULT_REEMISSION_MAX_AGE,
        help=(
            "âge du plus ancien événement à réémettre au-delà duquel la réémission est en "
            f"retard, en secondes, {doctor.DEFAULT_REEMISSION_MAX_AGE} par défaut"
        ),
    )
    check.add_argument(
        "--webhook-max-age",
        type=_bounded(0, doctor.MAX_QUEUE_MAX_AGE, "de secondes"),
        default=doctor.DEFAULT_WEBHOOK_MAX_AGE,
        help=(
            "âge du plus ancien message de webhook à envoyer au-delà duquel les webhooks sont "
            f"en retard, en secondes, {doctor.DEFAULT_WEBHOOK_MAX_AGE} par défaut"
        ),
    )
    check.add_argument("--format", choices=("text", "json"), default="text")
    _common(check)

    launch = commands.add_parser(
        "demo",
        help="lance la démonstration d'un clone du dépôt (Docker requis)",
        description=(
            "Lance la démonstration du dépôt avec docker compose, attend qu'elle réponde et "
            "donne l'adresse de l'explorateur. Codes de sortie : 0 elle répond ; 1 Docker absent "
            "ou en échec ; 2 hors d'un clone ; 4 elle ne répond pas dans le délai."
        ),
    )
    launch.add_argument(
        "--directory",
        type=Path,
        default=Path(),
        help="un dossier du clone, le dossier courant par défaut",
    )
    launch.add_argument(
        "--wait",
        type=_bounded(1, demo.MAX_WAIT, "de secondes"),
        default=demo.DEFAULT_WAIT,
        help=f"délai d'attente de la démonstration, en secondes, {demo.DEFAULT_WAIT} par défaut",
    )
    _common(launch)

    # Pour l'aide seulement : main cède la place au proxy avant de lire les arguments,
    # que le proxy lit lui-même (answerlineage proxy --help donne les siens).
    relay = commands.add_parser(
        "proxy",
        add_help=False,
        help=(
            "proxy MCP : devant un serveur HTTP, ou qui lance un serveur stdio donné après "
            "« -- » (answerlineage-proxy requis)"
        ),
    )
    relay.add_argument("arguments", nargs=argparse.REMAINDER)
    return parser


def _write(stream: TextIO, text: str) -> None:
    """Écrit en UTF-8, quel que soit le réglage du terminal."""
    binary = getattr(stream, "buffer", None)
    if binary is None:
        stream.write(text)
        return
    stream.flush()
    binary.write(text.encode("utf-8"))
    binary.flush()


def out(text: str) -> None:
    _write(sys.stdout, text)


def err(text: str) -> None:
    _write(sys.stderr, text + "\n")


def _impact(api: Api, args: argparse.Namespace) -> Exit:
    if datetime.fromisoformat(args.end) <= datetime.fromisoformat(args.start):
        err("Usage fautif : --to doit suivre --from.")
        return Exit.USAGE
    known_at = None if args.known_at is None else datetime.fromisoformat(args.known_at)
    if known_at is not None and not _EARLIEST <= known_at <= _LATEST:
        err("Usage fautif : --known-at tombe entre le 1er janvier 1970 et l'an 9999.")
        return Exit.USAGE
    query = impact.Query(
        namespace=args.namespace,
        name=args.name,
        start=args.start,
        end=args.end,
        limit=args.limit,
        cursor=args.cursor,
        known_at=args.known_at,
    )
    try:
        return impact.run(api, query, args.format, out)
    except impact.RefusedError as refused:
        code, message = impact.refusal_message(refused)
        err(message)
        return code


def _exposure(api: Api, args: argparse.Namespace) -> Exit:
    for option, value in (("--at", args.at), ("--known-at", args.known_at)):
        if value is not None and not _EARLIEST <= datetime.fromisoformat(value) <= _LATEST:
            err(f"Usage fautif : {option} tombe entre le 1er janvier 1970 et l'an 9999.")
            return Exit.USAGE
    query = exposure.Query(limit=args.limit, cursor=args.cursor, at=args.at, known_at=args.known_at)
    try:
        return exposure.run(api, query, args.format, out)
    except exposure.RefusedError as refused:
        code, message = exposure.refusal_message(refused)
        err(message)
        return code


def _incident(api: Api, args: argparse.Namespace) -> Exit:
    try:
        if args.action == "show":
            return incident.run_show(
                api,
                args.incident_id,
                limit=args.limit,
                cursor=args.cursor,
                output=args.format,
                write=out,
            )
        if args.action == "quarantine":
            return incident.run_quarantine(
                api,
                args.incident_id,
                mode=None if args.lift else args.mode,
                output=args.format,
                write=out,
            )
        if args.action == "proposals":
            return incident.run_proposals(
                api,
                state=args.state,
                limit=args.limit,
                cursor=args.cursor,
                output=args.format,
                write=out,
            )
        if args.action == "dismiss":
            return incident.run_dismiss(api, args.proposal_id, output=args.format, write=out)
        if args.action == "confirm":
            return _confirm(api, args)
        fault = incident.declaration_fault(args.datasets, args.start, args.end)
        if fault is not None:
            err(f"Usage fautif : {fault}")
            return Exit.USAGE
        declaration = incident.Declaration(
            datasets=tuple((namespace, name) for namespace, name in args.datasets),
            start=args.start,
            end=args.end,
            severity=args.severity,
        )
        return incident.run_declare(api, declaration, args.format, out)
    except incident.RefusedError as refused:
        code, message = incident.refusal_message(refused)
        err(message)
        return code


def _confirm(api: Api, args: argparse.Namespace) -> Exit:
    """Confirme une proposition, la fenêtre donnée par --from et --to ensemble, ou déduite."""
    if (args.start is None) != (args.end is None):
        err("Usage fautif : --from et --to se donnent ensemble, ou pas du tout.")
        return Exit.USAGE
    window = None
    if args.start is not None:
        begin, finish = datetime.fromisoformat(args.start), datetime.fromisoformat(args.end)
        if finish <= begin:
            err("Usage fautif : --to doit suivre --from.")
            return Exit.USAGE
        if (finish - begin).total_seconds() > incident.MAX_WINDOW_DAYS * 86_400:
            err(
                "Usage fautif : une fenêtre de validité dure au plus "
                f"{incident.MAX_WINDOW_DAYS} jours."
            )
            return Exit.USAGE
        window = (args.start, args.end)
    return incident.run_confirm(
        api,
        args.proposal_id,
        severity=args.severity,
        window=window,
        output=args.format,
        write=out,
    )


def _dead_letters(api: Api, args: argparse.Namespace) -> Exit:
    event_ids = getattr(args, "event_ids", [])
    fault = dead_letters.filters_fault(args.emitter, event_ids)
    if fault is not None:
        err(f"Usage fautif : {fault}")
        return Exit.USAGE
    filters = dead_letters.Filters(
        emitter=args.emitter, reason=args.reason, event_ids=tuple(dict.fromkeys(event_ids))
    )
    try:
        if args.action == "list":
            return dead_letters.run_list(
                api, filters, limit=args.limit, cursor=args.cursor, output=args.format, write=out
            )
        filtered = filters != dead_letters.Filters()
        if filtered == args.all:
            err(
                "Usage fautif : rejouer tout le rebut se demande par --all, sans autre filtre ; "
                "sinon, --emitter, --reason ou --event-id."
            )
            return Exit.USAGE
        return dead_letters.run_replay(
            api, filters, limit=args.limit, output=args.format, write=out
        )
    except dead_letters.RefusedError as refused:
        code, message = dead_letters.refusal_message(refused)
        err(message)
        return code


def _doctor(api: Api, args: argparse.Namespace) -> Exit:
    checks = doctor.diagnose(
        api,
        queue_max_age=args.queue_max_age,
        reemission_max_age=args.reemission_max_age,
        webhook_max_age=args.webhook_max_age,
    )
    render = doctor.render_json if args.format == "json" else doctor.render_text
    out(render(api.base, checks))
    return Exit.ANOMALY if doctor.anomalies(checks) else Exit.OK


def _demo(api: Api, args: argparse.Namespace) -> Exit:
    if not is_loopback(api.base):
        err(
            "Usage fautif : la démonstration n'écoute que sur la boucle locale ; "
            "son adresse est http://127.0.0.1:8000."
        )
        return Exit.USAGE
    try:
        root = demo.start(api, args.directory, args.wait)
    except demo.DemoError as error:
        err(error.message)
        return error.code
    out(demo.ready_message(api.base, root))
    return Exit.OK


_COMMANDS = {
    "impact": _impact,
    "exposure": _exposure,
    "incident": _incident,
    "dead-letters": _dead_letters,
    "doctor": _doctor,
    "demo": _demo,
}


def main(argv: Sequence[str] | None = None, *, transport: httpx.BaseTransport | None = None) -> int:
    """Lance une sous-commande ; ``transport`` remplace le réseau dans les tests."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["proxy"]:
        return proxy.delegate(arguments[1:], err)
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exit_:
        return Exit.OK if exit_.code in (0, None) else Exit.USAGE
    try:
        base = configured_base(args.url, os.environ)
    except UsageError as error:
        err(f"Usage fautif : {error}")
        return Exit.USAGE
    with Api(base, timeout=float(args.timeout), transport=transport) as api:
        try:
            return int(_COMMANDS[args.command](api, args))
        except UnreachableError:
            err(
                f"L'API ne répond pas à {base}, ou pas dans le délai : vérifier qu'elle tourne, "
                f"et l'adresse (--url ou {ENV_URL})."
            )
            return Exit.UNREACHABLE
        except InvalidResponseError as invalid:
            where = f" en {invalid.where}" if invalid.where else ""
            err(f"Réponse de l'API invalide{where} : {invalid.reason}. Rien n'est affiché.")
            return Exit.API_ERROR


if __name__ == "__main__":
    sys.exit(main())
