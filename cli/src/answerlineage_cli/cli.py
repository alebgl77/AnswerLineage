"""Ligne de commande : ``answerlineage impact | doctor | demo | proxy``.

``impact`` propage un incident de données jusqu'aux réponses et aux
destinataires touchés ; ``doctor`` vérifie chaque composant d'une
installation ; ``demo`` lance la démonstration d'un clone du dépôt. Toutes
trois parlent à l'API par HTTP, à l'adresse de ``--url`` ou de
``ANSWERLINEAGE_API_URL``, ``http://127.0.0.1:8000`` par défaut. ``proxy``
cède la place au proxy MCP, avec ses arguments (``proxy.py``).

Codes de sortie (``exits.py``) : 0 succès ; 1 rien de touché (impact), une
anomalie (doctor) ou un échec de Docker (demo) ; 2 usage fautif ; 3 résultat
incomplet (impact) ; 4 API injoignable ; 5 refus de l'API ou réponse invalide.
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

from answerlineage_cli import demo, doctor, impact, proxy
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
            "AnswerLineage depuis le terminal : propager un incident, vérifier une "
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

    check = commands.add_parser(
        "doctor",
        help="vérifie chaque composant d'une installation et liste les anomalies",
        description=(
            "Vérifie l'API, l'adresse, la base et ses migrations, la file d'ingestion, les "
            "émetteurs, les partitions et l'explorateur. Codes de sortie : 0 aucune anomalie ; "
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


def _doctor(api: Api, args: argparse.Namespace) -> Exit:
    checks = doctor.diagnose(api, queue_max_age=args.queue_max_age)
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


_COMMANDS = {"impact": _impact, "doctor": _doctor, "demo": _demo}


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
