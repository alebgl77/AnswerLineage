"""Ligne de commande du worker : ``answerlineage-worker``.

La base vient de ``ANSWERLINEAGE_DATABASE_URL``. Le worker s'arrête proprement
sur SIGINT ou SIGTERM, à la fin du lot en cours, qui compte au plus
``--batch-size`` événements. Un événement que la projection n'a pas pu intégrer
en ``--max-attempts`` essais va au rebut, ou au premier échec quand l'erreur ne
se corrigera pas en réessayant. Il contrôle aussi, chaque minute, les émetteurs
muets depuis plus de ``--silence-minutes``, et entretient chaque heure les
partitions mensuelles : ``--partitions-ahead`` mois créés à l'avance,
``--retention-months`` mois conservés. Une conservation absurde
(nulle, négative, plus courte que l'avance), un lot ou un nombre d'essais hors
de leurs bornes empêchent le worker de démarrer.

``answerlineage-worker rebuild`` reconstruit le graphe depuis les événements
bruts (``rebuild.py``). Sans ``--yes``, il n'en donne qu'un aperçu et ne change
rien ; quand des lignes du graphe, plus anciennes que le premier événement
conservé, ne seraient pas rendues, il faut aussi ``--accept-loss``. Le code de
sortie est 0 quand le graphe est reconstruit, 1 quand rien n'a été fait ou que
la reconstruction est interrompue, 2 pour une configuration invalide.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import timedelta

from sqlalchemy.exc import DBAPIError

from answerlineage_api.store.engine import (
    DatabaseConfigurationError,
    create_engine,
    database_url_from_environment,
)
from answerlineage_workers.partitions import (
    DEFAULT_RETENTION,
    MAX_AHEAD_MONTHS,
    MAX_RETENTION_MONTHS,
    MIN_AHEAD_MONTHS,
    Retention,
)
from answerlineage_workers.queue import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_MAX_ATTEMPTS,
    MAX_BATCH_SIZE,
    MAX_MAX_ATTEMPTS,
    run_worker,
)
from answerlineage_workers.rebuild import (
    RESUME_COMMAND,
    RebuildRefusedError,
    assess,
    rebuild_graph,
)
from answerlineage_workers.silence import DEFAULT_SILENCE

logger = logging.getLogger("answerlineage_workers")


@contextmanager
def _stopped_by_signals(stop: asyncio.Event) -> Iterator[None]:
    """``SIGINT`` et ``SIGTERM`` lèvent ``stop`` : le lot en cours finit, rien n'est perdu."""
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    try:
        yield
    finally:
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)


async def serve(
    batch_size: int,
    stop: asyncio.Event | None = None,
    silence_threshold: timedelta = DEFAULT_SILENCE,
    retention: Retention = DEFAULT_RETENTION,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> None:
    engine = create_engine(database_url_from_environment())
    stop = stop or asyncio.Event()
    try:
        with _stopped_by_signals(stop):
            logger.info("Worker démarré, lots de %d événements.", batch_size)
            await run_worker(
                engine,
                stop,
                batch_size=batch_size,
                silence_threshold=silence_threshold,
                retention=retention,
                max_attempts=max_attempts,
            )
    finally:
        await engine.dispose()
        logger.info("Worker arrêté.")


async def rebuild(
    *,
    confirmed: bool,
    accept_loss: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    stop: asyncio.Event | None = None,
) -> int:
    """Reconstruit le graphe, ou n'en donne que l'aperçu sans ``confirmed``.

    Rend le code de sortie de la commande.

    Le journal dit l'aperçu, la progression et le résultat : des nombres et des
    heures, jamais un contenu, et d'une erreur seulement sa classe.
    """
    engine = create_engine(database_url_from_environment())
    stop = stop or asyncio.Event()
    try:
        if not confirmed:
            for line in (await assess(engine)).lines():
                logger.info("%s", line)
            return 1
        with _stopped_by_signals(stop):
            report = await rebuild_graph(
                engine, accept_loss=accept_loss, batch_size=batch_size, stop=stop
            )
    except RebuildRefusedError as refusal:
        logger.error("%s", refusal)
        return 1
    except DBAPIError as error:
        logger.error(
            "Reconstruction du graphe en échec (%s). « %s » la relance, ou la reprend où la"
            " file en est.",
            type(error.orig).__name__ if error.orig is not None else type(error).__name__,
            RESUME_COMMAND,
        )
        return 1
    finally:
        await engine.dispose()
    for line in report.lines():
        logger.info("%s", line)
    return 0 if report.finished else 1


def _minutes(value: str) -> timedelta:
    if not value.isdigit() or not 1 <= int(value) <= 10_080:
        raise argparse.ArgumentTypeError("un nombre entier de minutes, de 1 à 10080 (une semaine)")
    return timedelta(minutes=int(value))


def _months(low: int, high: int) -> Callable[[str], int]:
    def parse(value: str) -> int:
        if not value.isdigit() or not low <= int(value) <= high:
            raise argparse.ArgumentTypeError(f"un nombre entier de mois, de {low} à {high}")
        return int(value)

    return parse


def _attempts(value: str) -> int:
    if not value.isdigit() or not 1 <= int(value) <= MAX_MAX_ATTEMPTS:
        raise argparse.ArgumentTypeError(f"un nombre entier d'essais, de 1 à {MAX_MAX_ATTEMPTS}")
    return int(value)


def _batch_size(value: str) -> int:
    if not value.isdigit() or not 1 <= int(value) <= MAX_BATCH_SIZE:
        raise argparse.ArgumentTypeError(f"un nombre entier d'événements, de 1 à {MAX_BATCH_SIZE}")
    return int(value)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="answerlineage-worker",
        description="Projette les événements reçus dans le graphe de lignage.",
    )
    parser.add_argument(
        "--batch-size",
        type=_batch_size,
        default=DEFAULT_BATCH_SIZE,
        help=(
            f"événements projetés par lot, de 1 à {MAX_BATCH_SIZE}"
            f" ({DEFAULT_BATCH_SIZE} par défaut)"
        ),
    )
    parser.add_argument(
        "--silence-minutes",
        type=_minutes,
        default=DEFAULT_SILENCE,
        help="minutes sans relevé au-delà desquelles un émetteur est signalé muet (10 par défaut)",
    )
    parser.add_argument(
        "--retention-months",
        type=_months(1, MAX_RETENTION_MONTHS),
        default=DEFAULT_RETENTION.months,
        help="mois conservés dans les tables partitionnées (13 par défaut)",
    )
    parser.add_argument(
        "--partitions-ahead",
        type=_months(MIN_AHEAD_MONTHS, MAX_AHEAD_MONTHS),
        default=DEFAULT_RETENTION.ahead,
        help="mois créés à l'avance après le mois courant (2 par défaut)",
    )
    parser.add_argument(
        "--max-attempts",
        type=_attempts,
        default=DEFAULT_MAX_ATTEMPTS,
        help=(
            "essais en échec au-delà desquels un événement va au rebut "
            f"({DEFAULT_MAX_ATTEMPTS} par défaut)"
        ),
    )
    commands = parser.add_subparsers(dest="command", title="sous-commandes")
    rebuilding = commands.add_parser(
        "rebuild",
        help="reconstruit le graphe depuis les événements bruts",
        description=(
            "Vide le graphe et le rejoue depuis les événements bruts conservés, ou reprend une"
            " reconstruction inachevée. Sans --yes, donne un aperçu et ne change rien."
        ),
    )
    rebuilding.add_argument(
        "--yes", action="store_true", help="confirme : vide le graphe et rejoue les événements"
    )
    rebuilding.add_argument(
        "--accept-loss",
        action="store_true",
        help=(
            "accepte de perdre les lignes du graphe enregistrées avant le premier événement"
            " conservé, que la reconstruction ne rend pas telles quelles"
        ),
    )
    rebuilding.add_argument(
        "--batch-size",
        dest="rebuild_batch_size",
        type=_batch_size,
        default=DEFAULT_BATCH_SIZE,
        help=(
            f"événements projetés par lot, de 1 à {MAX_BATCH_SIZE}"
            f" ({DEFAULT_BATCH_SIZE} par défaut)"
        ),
    )
    args = parser.parse_args(argv)
    try:
        retention = Retention(months=args.retention_months, ahead=args.partitions_ahead)
    except ValueError as error:
        parser.error(str(error))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.command == "rebuild":
        try:
            return asyncio.run(
                rebuild(
                    confirmed=args.yes,
                    accept_loss=args.accept_loss,
                    batch_size=args.rebuild_batch_size,
                )
            )
        except DatabaseConfigurationError as error:
            print(f"Configuration invalide : {error}", file=sys.stderr)
            return 2
    try:
        asyncio.run(
            serve(
                args.batch_size,
                silence_threshold=args.silence_minutes,
                retention=retention,
                max_attempts=args.max_attempts,
            )
        )
    except DatabaseConfigurationError as error:
        print(f"Configuration invalide : {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
