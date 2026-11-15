"""Ligne de commande du worker : ``answerlineage-worker``.

La base vient de ``ANSWERLINEAGE_DATABASE_URL``. Le worker s'arrête proprement
sur SIGINT ou SIGTERM, à la fin du lot en cours.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Sequence

from answerlineage_api.store.engine import (
    DatabaseConfigurationError,
    create_engine,
    database_url_from_environment,
)
from answerlineage_workers.queue import DEFAULT_BATCH_SIZE, run_worker

logger = logging.getLogger("answerlineage_workers")


async def serve(batch_size: int, stop: asyncio.Event | None = None) -> None:
    engine = create_engine(database_url_from_environment())
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    try:
        logger.info("Worker démarré, lots de %d événements.", batch_size)
        await run_worker(engine, stop, batch_size=batch_size)
    finally:
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)
        await engine.dispose()
        logger.info("Worker arrêté.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="answerlineage-worker",
        description="Projette les événements reçus dans le graphe de lignage.",
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(serve(args.batch_size))
    except DatabaseConfigurationError as error:
        print(f"Configuration invalide : {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
