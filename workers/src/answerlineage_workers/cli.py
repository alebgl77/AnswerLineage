"""Ligne de commande du worker : ``answerlineage-worker``.

La base vient de ``ANSWERLINEAGE_DATABASE_URL``. Le worker s'arrête proprement
sur SIGINT ou SIGTERM, à la fin du lot en cours, qui compte au plus
``--batch-size`` événements. Un événement que la projection n'a pas pu intégrer
en ``--max-attempts`` essais va au rebut, ou au premier échec quand l'erreur ne
se corrigera pas en réessayant. Il contrôle aussi, chaque minute, les émetteurs
muets depuis plus de ``--silence-minutes``, et entretient chaque heure les
partitions mensuelles : ``--partitions-ahead`` mois créés à l'avance,
``--retention-months`` mois conservés. Chaque minute, il fait remonter au
responsable de données les listes de rappel restées sans décision plus de
``--recall-decision-hours`` heures. Une conservation absurde (nulle, négative,
plus courte que l'avance), un lot, un nombre d'essais ou un délai hors de leurs
bornes empêchent le worker de démarrer.

Avec une destination de réémission (``--reemit-url`` ou
``ANSWERLINEAGE_REEMIT_URL``, et ``ANSWERLINEAGE_REEMIT_AUTHORIZATION`` pour son
en-tête d'authentification), les événements projetés partent vers un
consommateur OpenLineage externe (``reemission.py``), par une file bornée à
``--reemit-max-events`` événements et ``--reemit-max-age-hours`` heures. Une
destination refusée (adresse de métadonnées, ``http`` hors de la boucle locale,
identifiant dans l'adresse) empêche le worker de démarrer, sans être citée.

Avec un webhook (``--webhook-url`` ou ``ANSWERLINEAGE_WEBHOOK_URL``, et son
secret partagé dans ``ANSWERLINEAGE_WEBHOOK_SECRET``, le précédent dans
``ANSWERLINEAGE_WEBHOOK_PREVIOUS_SECRET`` pendant une rotation), les changements
d'état des incidents sont annoncés au récepteur par des messages signés
(``webhooks.py``), par une file bornée à ``--webhook-max-messages`` messages et
``--webhook-max-age-hours`` heures. Le webhook obéit aux règles d'adresse de la
réémission ; une adresse refusée, un secret absent, trop court ou réglé sans
webhook empêchent le worker de démarrer, sans être cités.

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
import os
import signal
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import timedelta

from sqlalchemy.exc import DBAPIError

from answerlineage_api.store.engine import (
    DatabaseConfigurationError,
    create_engine,
    database_url_from_environment,
)
from answerlineage_workers import webhooks
from answerlineage_workers.destination import (
    AUTHORIZATION_VARIABLE,
    URL_VARIABLE,
    DestinationError,
    Resolver,
    check_resolution,
    parse_destination,
    resolve,
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
from answerlineage_workers.recall import (
    DEFAULT_DECISION_DELAY,
    MAX_DECISION_DELAY,
    MIN_DECISION_DELAY,
)
from answerlineage_workers.reemission import (
    DEFAULT_MAX_AGE,
    DEFAULT_MAX_EVENTS,
    MAX_MAX_AGE,
    MAX_MAX_EVENTS,
    Reemission,
)
from answerlineage_workers.silence import DEFAULT_SILENCE
from answerlineage_workers.webhooks import (
    PREVIOUS_SECRET_VARIABLE,
    SECRET_VARIABLE,
    Webhook,
    WebhookSecretError,
    checked_secret,
)

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


class ReemissionConfigurationError(ValueError):
    """Le réglage de la réémission est refusé. Le message est un gabarit fixe."""


def reemission_from(
    url: str | None,
    environ: Mapping[str, str],
    *,
    max_events: int = DEFAULT_MAX_EVENTS,
    max_age: timedelta = DEFAULT_MAX_AGE,
    resolver: Resolver = resolve,
) -> Reemission | None:
    """La destination réglée par l'exploitant, contrôlée ; aucune sans adresse.

    L'adresse vient de l'option, sinon de ``ANSWERLINEAGE_REEMIT_URL`` ; l'en-tête
    d'authentification, de ``ANSWERLINEAGE_REEMIT_AUTHORIZATION`` seulement :
    une option se lit dans la liste des processus. Une variable vide ne règle
    rien.
    """
    url = url or environ.get(URL_VARIABLE) or None
    if url is None:
        if environ.get(AUTHORIZATION_VARIABLE):
            raise ReemissionConfigurationError(
                f"{AUTHORIZATION_VARIABLE} est réglée sans destination de réémission."
            )
        return None
    try:
        destination = parse_destination(url, environ.get(AUTHORIZATION_VARIABLE) or None)
        check_resolution(destination, resolver)
    except DestinationError as error:
        raise ReemissionConfigurationError(str(error)) from None
    return Reemission(destination, max_events=max_events, max_age=max_age)


class WebhookConfigurationError(ValueError):
    """Le réglage du webhook est refusé. Le message est un gabarit fixe."""


def webhook_from(
    url: str | None,
    environ: Mapping[str, str],
    *,
    max_messages: int = webhooks.DEFAULT_MAX_MESSAGES,
    max_age: timedelta = webhooks.DEFAULT_MAX_AGE,
    resolver: Resolver = resolve,
) -> Webhook | None:
    """Le webhook réglé par l'exploitant, contrôlé ; aucun sans adresse.

    L'adresse vient de l'option, sinon de ``ANSWERLINEAGE_WEBHOOK_URL`` ; le
    secret partagé, de ``ANSWERLINEAGE_WEBHOOK_SECRET`` seulement, et le
    précédent, pendant une rotation, de ``ANSWERLINEAGE_WEBHOOK_PREVIOUS_SECRET`` :
    une option se lit dans la liste des processus. Une variable vide ne règle
    rien. Aucun message ne cite l'adresse ni un secret.
    """
    url = url or environ.get(webhooks.URL_VARIABLE) or None
    secret = environ.get(SECRET_VARIABLE) or None
    previous = environ.get(PREVIOUS_SECRET_VARIABLE) or None
    if url is None:
        for variable, value in ((SECRET_VARIABLE, secret), (PREVIOUS_SECRET_VARIABLE, previous)):
            if value is not None:
                raise WebhookConfigurationError(f"{variable} est réglée sans webhook.")
        return None
    if secret is None:
        raise WebhookConfigurationError(
            f"Un webhook signe ses messages : {SECRET_VARIABLE} doit être réglée."
        )
    try:
        checked_secret(secret, SECRET_VARIABLE)
        if previous is not None:
            checked_secret(previous, PREVIOUS_SECRET_VARIABLE)
        destination = parse_destination(url)
        check_resolution(destination, resolver)
    except (WebhookSecretError, DestinationError) as error:
        raise WebhookConfigurationError(f"webhook : {error}") from None
    return Webhook(
        destination,
        secret,
        previous_secret=previous,
        max_messages=max_messages,
        max_age=max_age,
    )


async def serve(
    batch_size: int,
    stop: asyncio.Event | None = None,
    silence_threshold: timedelta = DEFAULT_SILENCE,
    retention: Retention = DEFAULT_RETENTION,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    reemission: Reemission | None = None,
    webhook: Webhook | None = None,
    recall_delay: timedelta = DEFAULT_DECISION_DELAY,
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
                reemission=reemission,
                webhook=webhook,
                recall_delay=recall_delay,
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
    reemit: bool = False,
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
                engine,
                accept_loss=accept_loss,
                batch_size=batch_size,
                stop=stop,
                reemit=reemit,
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


def _decision_hours(value: str) -> timedelta:
    low, high = MIN_DECISION_DELAY // timedelta(hours=1), MAX_DECISION_DELAY // timedelta(hours=1)
    if not value.isdigit() or not low <= int(value) <= high:
        raise argparse.ArgumentTypeError(f"un nombre entier d'heures, de {low} à {high}")
    return timedelta(hours=int(value))


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


def _reemit_events(value: str) -> int:
    if not value.isdigit() or not 1 <= int(value) <= MAX_MAX_EVENTS:
        raise argparse.ArgumentTypeError(f"un nombre entier d'événements, de 1 à {MAX_MAX_EVENTS}")
    return int(value)


def _reemit_hours(value: str) -> timedelta:
    most = MAX_MAX_AGE // timedelta(hours=1)
    if not value.isdigit() or not 1 <= int(value) <= most:
        raise argparse.ArgumentTypeError(f"un nombre entier d'heures, de 1 à {most}")
    return timedelta(hours=int(value))


def _webhook_messages(value: str) -> int:
    if not value.isdigit() or not 1 <= int(value) <= webhooks.MAX_MAX_MESSAGES:
        raise argparse.ArgumentTypeError(
            f"un nombre entier de messages, de 1 à {webhooks.MAX_MAX_MESSAGES}"
        )
    return int(value)


def _webhook_hours(value: str) -> timedelta:
    most = webhooks.MAX_MAX_AGE // timedelta(hours=1)
    if not value.isdigit() or not 1 <= int(value) <= most:
        raise argparse.ArgumentTypeError(f"un nombre entier d'heures, de 1 à {most}")
    return timedelta(hours=int(value))


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
        "--recall-decision-hours",
        type=_decision_hours,
        default=DEFAULT_DECISION_DELAY,
        help=(
            "heures sans décision au-delà desquelles une liste de rappel remonte au responsable"
            " de données, de 1 à 720 (48 par défaut)"
        ),
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
    parser.add_argument(
        "--reemit-url",
        metavar="URL",
        help=(
            "point d'entrée OpenLineage d'un consommateur externe où réémettre les événements"
            f" projetés, en https ({URL_VARIABLE} par défaut ; aucun sinon). L'en-tête"
            f" d'authentification se lit de {AUTHORIZATION_VARIABLE}"
        ),
    )
    parser.add_argument(
        "--reemit-max-events",
        type=_reemit_events,
        default=DEFAULT_MAX_EVENTS,
        help=(
            "événements gardés au plus dans la file de réémission ; au-delà, les plus anciens"
            f" sont perdus ({DEFAULT_MAX_EVENTS} par défaut)"
        ),
    )
    parser.add_argument(
        "--reemit-max-age-hours",
        type=_reemit_hours,
        default=DEFAULT_MAX_AGE,
        help=(
            "heures pendant lesquelles un événement attend au plus d'être réémis"
            f" ({DEFAULT_MAX_AGE // timedelta(hours=1)} par défaut)"
        ),
    )
    parser.add_argument(
        "--webhook-url",
        metavar="URL",
        help=(
            "adresse du récepteur où annoncer les changements d'état des incidents, en https"
            f" ({webhooks.URL_VARIABLE} par défaut ; aucun sinon). Le secret partagé se lit de"
            f" {SECRET_VARIABLE}, le précédent, pendant une rotation, de"
            f" {PREVIOUS_SECRET_VARIABLE}"
        ),
    )
    parser.add_argument(
        "--webhook-max-messages",
        type=_webhook_messages,
        default=webhooks.DEFAULT_MAX_MESSAGES,
        help=(
            "messages gardés au plus dans la file des webhooks ; au-delà, les plus anciens sont"
            f" perdus ({webhooks.DEFAULT_MAX_MESSAGES} par défaut)"
        ),
    )
    parser.add_argument(
        "--webhook-max-age-hours",
        type=_webhook_hours,
        default=webhooks.DEFAULT_MAX_AGE,
        help=(
            "heures pendant lesquelles un message attend au plus d'être annoncé, et pendant"
            " lesquelles le webhook reste en service après le dernier passage d'un worker"
            f" ({webhooks.DEFAULT_MAX_AGE // timedelta(hours=1)} par défaut)"
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
    try:
        reemission = reemission_from(
            args.reemit_url,
            os.environ,
            max_events=args.reemit_max_events,
            max_age=args.reemit_max_age_hours,
        )
        webhook = webhook_from(
            args.webhook_url,
            os.environ,
            max_messages=args.webhook_max_messages,
            max_age=args.webhook_max_age_hours,
        )
    except (ReemissionConfigurationError, WebhookConfigurationError) as error:
        print(f"Configuration invalide : {error}", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.command == "rebuild":
        try:
            return asyncio.run(
                rebuild(
                    confirmed=args.yes,
                    accept_loss=args.accept_loss,
                    batch_size=args.rebuild_batch_size,
                    reemit=reemission is not None,
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
                reemission=reemission,
                webhook=webhook,
                recall_delay=args.recall_decision_hours,
            )
        )
    except DatabaseConfigurationError as error:
        print(f"Configuration invalide : {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
