"""Ligne de commande du worker : ``answerlineage-worker``.

La base vient de ``ANSWERLINEAGE_DATABASE_URL``. Le worker s'arrête proprement
sur SIGINT ou SIGTERM, à la fin du lot en cours. Il contrôle aussi, chaque
minute, les émetteurs muets depuis plus de ``--silence-minutes``, et entretient
chaque heure les partitions mensuelles : ``--partitions-ahead`` mois créés à
l'avance, ``--retention-months`` mois conservés. Une conservation absurde
(nulle, négative, plus courte que l'avance) empêche le worker de démarrer.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Callable, Sequence
from datetime import timedelta

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
from answerlineage_workers.queue import DEFAULT_BATCH_SIZE, run_worker
from answerlineage_workers.silence import DEFAULT_SILENCE

logger = logging.getLogger("answerlineage_workers")


async def serve(
    batch_size: int,
    stop: asyncio.Event | None = None,
    silence_threshold: timedelta = DEFAULT_SILENCE,
    retention: Retention = DEFAULT_RETENTION,
) -> None:
    engine = create_engine(database_url_from_environment())
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    try:
        logger.info("Worker démarré, lots de %d événements.", batch_size)
        await run_worker(
            engine,
            stop,
            batch_size=batch_size,
            silence_threshold=silence_threshold,
            retention=retention,
        )
    finally:
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)
        await engine.dispose()
        logger.info("Worker arrêté.")


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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="answerlineage-worker",
        description="Projette les événements reçus dans le graphe de lignage.",
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
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
    args = parser.parse_args(argv)
    try:
        retention = Retention(months=args.retention_months, ahead=args.partitions_ahead)
    except ValueError as error:
        parser.error(str(error))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(
            serve(args.batch_size, silence_threshold=args.silence_minutes, retention=retention)
        )
    except DatabaseConfigurationError as error:
        print(f"Configuration invalide : {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
