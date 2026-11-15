"""Ligne de commande du kit de conformité : ``answerlineage-conformance``.

``canaries`` écrit trois canaris. ``check`` juge un émetteur sur les fichiers
qu'une demande jouée avec eux a laissés : il sort ``0`` quand l'émetteur est
conforme, ``1`` quand il ne l'est pas, ``2`` quand une entrée est illisible ou
l'usage fautif. Rien n'est lu que ces fichiers : ni réseau, ni base de données.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from answerlineage_api.ingest.models import parse_event_time
from answerlineage_conformance.canaries import (
    Canary,
    canaries_document,
    new_canaries,
    parse_canaries,
)
from answerlineage_conformance.content import Evidence
from answerlineage_conformance.controls import PROFILES
from answerlineage_conformance.judgement import judge
from answerlineage_conformance.report import Report, render_json, render_text
from answerlineage_conformance.sources import InputError, SourceFile, parse_events, parse_json, read

_RENDERERS = {"text": render_text, "json": render_json}


def _now() -> datetime:
    return datetime.now(UTC)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="answerlineage-conformance",
        description="Juge un émetteur d'événements de lignage sur ce qu'il a émis.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "canaries", help="écrit trois canaris (JSON) : prompt, résultat d'outil, réponse"
    )
    check = commands.add_parser("check", help="juge une capture : 0 conforme, 1 non conforme")
    check.add_argument(
        "events", type=Path, help="événements émis : un tableau JSON, ou un événement par ligne"
    )
    check.add_argument(
        "--canaries", type=Path, required=True, help="fichier écrit par la commande canaries"
    )
    check.add_argument(
        "--witness",
        type=Path,
        required=True,
        help="l'échange tel que l'utilisateur et l'outil l'ont vu, où figurent les trois canaris",
    )
    check.add_argument(
        "--logs", type=Path, action="append", default=[], help="journal de l'émetteur (répétable)"
    )
    check.add_argument(
        "--metrics",
        type=Path,
        action="append",
        default=[],
        help="relevé des métriques de l'émetteur (répétable)",
    )
    check.add_argument(
        "--profile",
        choices=PROFILES,
        default="adapter",
        help="adapter : un adaptateur d'agents, qui doit la chaîne complète (défaut) ; "
        "capture : un point de capture, qui ne voit que des appels d'outil",
    )
    check.add_argument("--format", choices=sorted(_RENDERERS), default="text")
    check.add_argument(
        "--received-at",
        help="heure de référence (RFC 3339) : l'ingestion borne l'heure d'un événement par "
        "celle de sa réception ; par défaut, l'heure courante",
    )
    return parser


def _write(report: str) -> None:
    """Écrit le rapport en UTF-8, fins de ligne comprises, quel que soit le terminal.

    Un rapport est une preuve : ses octets ne dépendent ni de la machine ni de
    ses réglages régionaux.
    """
    binary = getattr(sys.stdout, "buffer", None)
    if binary is None:
        sys.stdout.write(report)
        return
    sys.stdout.flush()
    binary.write(report.encode("utf-8"))
    binary.flush()


def _canaries(file: SourceFile) -> tuple[Canary, ...]:
    document = parse_json(file)
    try:
        return parse_canaries(document)
    except InputError as error:
        raise InputError(f"{file.path} : {error}") from None


def _check(args: argparse.Namespace) -> int:
    received_at = _now()
    if args.received_at is not None:
        try:
            received_at = parse_event_time(args.received_at)
        except ValueError:
            print(
                "Usage fautif : --received-at attend une date et une heure RFC 3339 avec "
                "leur fuseau, par exemple 2026-11-15T12:00:00Z.",
                file=sys.stderr,
            )
            return 2
    try:
        events = read("events", args.events)
        canaries = read("canaries", args.canaries)
        witness = read("witness", args.witness)
        logs = tuple(read("logs", path) for path in args.logs)
        metrics = tuple(read("metrics", path) for path in args.metrics)
        evidence = Evidence(_canaries(canaries), witness, logs, metrics)
        parsed = parse_events(events)
    except InputError as error:
        print(f"Entrée illisible : {error}", file=sys.stderr)
        return 2
    try:
        judgement = judge(parsed, profile=args.profile, received_at=received_at, evidence=evidence)
    except RecursionError:
        # Sans ce garde, l'erreur sortirait avec 1, le code d'un émetteur non conforme.
        print(f"Entrée illisible : {events.path} : événements trop imbriqués.", file=sys.stderr)
        return 2
    report = Report(judgement, received_at, (events, canaries, witness, *logs, *metrics))
    _write(_RENDERERS[args.format](report))
    return 0 if judgement.conformant else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "canaries":
        json.dump(canaries_document(new_canaries()), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    return _check(args)


if __name__ == "__main__":
    sys.exit(main())
