"""Fixtures partagées : un PostgreSQL réel pour les tests d'intégration.

Le serveur vient de ``ANSWERLINEAGE_TEST_DATABASE_URL`` s'il est fourni, sinon
de Testcontainers avec l'image ``ANSWERLINEAGE_TEST_POSTGRES_IMAGE``. Chaque
test reçoit sa propre base, clonée d'un modèle déjà migré.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine

# Images épinglées par leur empreinte, comme toutes celles du dépôt : PostgreSQL, et le
# conteneur qui nettoie derrière Testcontainers (lu par Testcontainers à son import).
_POSTGRES_DIGEST = "sha256:e3d24f6906d0760bf4da0dae872415391ededefad23fd471c47dc53b0ddf3c42"
DEFAULT_POSTGRES_IMAGE = f"public.ecr.aws/docker/library/postgres:18.6@{_POSTGRES_DIGEST}"
os.environ.setdefault(
    "RYUK_CONTAINER_IMAGE",
    "testcontainers/ryuk:0.8.1@sha256:bf3f74a47dee0acda89aba4b2fc9c7fdcf994a084db02a2d06566f07baae022e",
)
_TEMPLATE = "answerlineage_template"
# Un registre public limite parfois le débit des tirages anonymes : le démarrage du
# serveur est retenté après ces pauses, en secondes, avant d'être tenu pour un échec.
REGISTRY_RETRY_DELAYS = (30.0, 60.0)
_RATE_LIMITED = ("toomanyrequests", "429 too many requests")
# Les images officielles de Docker sont copiées telles quelles sur le registre public
# d'AWS : une même empreinte y désigne les mêmes octets. Quand l'un refuse pour excès de
# débit, l'autre sert l'image épinglée ; une étiquette seule n'a pas de repli, car elle
# peut désigner d'autres octets d'un registre à l'autre.
_MIRRORS = (("public.ecr.aws/docker/library/", "docker.io/library/"),)
_PINNED = re.compile(r"@sha256:[0-9a-f]{64}$")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--truth-report",
        dest="truth_report",
        metavar="CHEMIN",
        help="écrit en JSON la mesure du moteur d'impact sur le corpus de vérité",
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _admin_execute(server: URL, statement: str) -> None:
    engine = create_engine(server, isolation_level="AUTOCOMMIT", hide_parameters=True)
    try:
        with engine.connect() as connection:
            connection.execute(text(statement))
    finally:
        engine.dispose()


def _rate_limited(error: Exception) -> bool:
    return any(mark in str(error).lower() for mark in _RATE_LIMITED)


def start_despite_rate_limits[T](
    start: Callable[[], T], sleep: Callable[[float], None] = time.sleep
) -> T:
    """Lance ``start`` ; un refus du registre pour excès de débit est retenté après une pause.

    Toute autre erreur, et le dernier refus, remontent tels quels.
    """
    for delay in (*REGISTRY_RETRY_DELAYS, None):
        try:
            return start()
        except Exception as error:
            if delay is None or not _rate_limited(error):
                raise
            sleep(delay)
    raise AssertionError("inatteignable")  # pragma: no cover


def registry_mirrors(image: str) -> list[str]:
    """L'image, puis la même empreinte sur un autre registre public qui la copie."""
    if not _PINNED.search(image):
        return [image]
    return [image] + [
        mirror + image[len(origin) :] for origin, mirror in _MIRRORS if image.startswith(origin)
    ]


def start_from_mirrors[T](
    images: list[str], start: Callable[[str], T], sleep: Callable[[float], None] = time.sleep
) -> T:
    """Lance ``start`` sur la première image qu'un registre accepte de servir.

    Un refus pour excès de débit passe aussitôt à l'image suivante ; tous refusés, l'essai
    reprend après une pause. Toute autre erreur remonte telle quelle.
    """

    def attempt() -> T:
        for image in images[:-1]:
            try:
                return start(image)
            except Exception as error:
                if not _rate_limited(error):
                    raise
        return start(images[-1])

    return start_despite_rate_limits(attempt, sleep)


@pytest.fixture(scope="session")
def postgres_image() -> str:
    """L'image PostgreSQL des tests : celle que fixe l'environnement, sinon l'image épinglée."""
    return os.environ.get("ANSWERLINEAGE_TEST_POSTGRES_IMAGE", DEFAULT_POSTGRES_IMAGE)


@pytest.fixture(scope="session")
def postgres_server(postgres_image: str) -> Iterator[URL]:
    """URL d'administration d'un serveur PostgreSQL jetable."""
    provided = os.environ.get("ANSWERLINEAGE_TEST_DATABASE_URL")
    if provided:
        yield make_url(provided).set(drivername="postgresql+psycopg")
        return

    image = postgres_image
    try:
        from testcontainers.community.postgres import PostgresContainer

        def started(reference: str) -> PostgresContainer:
            container = PostgresContainer(reference, driver="psycopg")
            container.start()
            return container

        container = start_from_mirrors(registry_mirrors(image), started)
    except Exception as error:
        # En CI, un test d'intégration sauté serait un faux vert.
        if os.environ.get("CI"):
            raise
        pytest.skip(f"PostgreSQL indisponible (Docker requis) : {type(error).__name__}")
    try:
        yield make_url(container.get_connection_url())
    finally:
        container.stop()


def _new_database(server: URL, template: str | None = None) -> URL:
    name = f"al_test_{uuid.uuid4().hex}"
    clause = f' TEMPLATE "{template}"' if template else ""
    _admin_execute(server, f'CREATE DATABASE "{name}"{clause}')
    return server.set(database=name)


def _drop_database(server: URL, url: URL) -> None:
    _admin_execute(server, f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)')


@pytest.fixture(scope="session")
def migrated_template(postgres_server: URL) -> str:
    """Base modèle où toutes les migrations sont appliquées, clonée par chaque test."""
    from answerlineage_api.store.migrate import upgrade

    _admin_execute(postgres_server, f'DROP DATABASE IF EXISTS "{_TEMPLATE}" WITH (FORCE)')
    _admin_execute(postgres_server, f'CREATE DATABASE "{_TEMPLATE}"')
    upgrade(postgres_server.set(database=_TEMPLATE))
    return _TEMPLATE


@pytest.fixture
def empty_database(postgres_server: URL) -> Iterator[URL]:
    """Base vide, sans aucune migration."""
    url = _new_database(postgres_server)
    try:
        yield url
    finally:
        _drop_database(postgres_server, url)


@pytest.fixture
def database(postgres_server: URL, migrated_template: str) -> Iterator[URL]:
    """Base migrée, propre à un test."""
    url = _new_database(postgres_server, migrated_template)
    try:
        yield url
    finally:
        _drop_database(postgres_server, url)


@pytest.fixture
async def engine(database: URL) -> AsyncIterator[AsyncEngine]:
    """Moteur asynchrone sur une base migrée propre au test."""
    from answerlineage_api.store.engine import create_engine

    engine = create_engine(database)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def fresh_engine(
    postgres_server: URL, migrated_template: str
) -> Callable[[], AbstractAsyncContextManager[AsyncEngine]]:
    """Fabrique de moteurs, chacun sur sa base migrée : pour un test qui en ouvre plusieurs."""
    from answerlineage_api.store.engine import create_engine

    @asynccontextmanager
    async def opened() -> AsyncIterator[AsyncEngine]:
        url = _new_database(postgres_server, migrated_template)
        engine = create_engine(url)
        try:
            yield engine
        finally:
            await engine.dispose()
            _drop_database(postgres_server, url)

    return opened
