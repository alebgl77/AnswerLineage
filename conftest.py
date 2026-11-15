"""Fixtures partagées : un PostgreSQL réel pour les tests d'intégration.

Le serveur vient de ``ANSWERLINEAGE_TEST_DATABASE_URL`` s'il est fourni, sinon
de Testcontainers avec l'image ``ANSWERLINEAGE_TEST_POSTGRES_IMAGE``. Chaque
test reçoit sa propre base, clonée d'un modèle déjà migré.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

DEFAULT_POSTGRES_IMAGE = "postgres:18"
_TEMPLATE = "answerlineage_template"


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


@pytest.fixture(scope="session")
def postgres_server() -> Iterator[URL]:
    """URL d'administration d'un serveur PostgreSQL jetable."""
    provided = os.environ.get("ANSWERLINEAGE_TEST_DATABASE_URL")
    if provided:
        yield make_url(provided).set(drivername="postgresql+psycopg")
        return

    image = os.environ.get("ANSWERLINEAGE_TEST_POSTGRES_IMAGE", DEFAULT_POSTGRES_IMAGE)
    try:
        from testcontainers.community.postgres import PostgresContainer

        container = PostgresContainer(image, driver="psycopg")
        container.start()
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
