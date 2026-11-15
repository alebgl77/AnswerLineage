"""Chaque route répond conformément à la spécification OpenAPI publiée."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import schemathesis
from hypothesis import HealthCheck, settings
from schemathesis.checks import (
    content_type_conformance,
    not_a_server_error,
    response_schema_conformance,
    status_code_conformance,
)
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from answerlineage_api.app import create_app
from answerlineage_api.openapi import render

PUBLISHED = Path(__file__).resolve().parents[2] / "spec" / "openapi" / "answerlineage-v1.json"
# « Conformément à son schéma » : jamais d'erreur du serveur, statut documenté,
# type de contenu documenté, corps conforme au schéma.
CONFORMANCE: list[Any] = [
    not_a_server_error,
    status_code_conformance,
    content_type_conformance,
    response_schema_conformance,
]


def test_published_contract_is_the_one_the_application_serves() -> None:
    """Le fichier publié se met à jour avec ``make openapi``."""
    assert PUBLISHED.read_text(encoding="utf-8") == render()


@pytest.fixture
def api_schema(database: URL) -> Iterator[Any]:
    # Une connexion par requête : chaque appel généré tourne dans sa propre boucle.
    engine = create_async_engine(database, poolclass=NullPool, hide_parameters=True)
    try:
        # Le client de test se présente sous le nom « testserver ».
        app = create_app(engine, allowed_hosts=["testserver"])
        yield schemathesis.openapi.from_asgi("/api/v1/openapi.json", app)
    finally:
        asyncio.run(engine.dispose())


schema = schemathesis.pytest.from_fixture("api_schema")


@pytest.mark.integration
@schema.parametrize()
@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
def test_every_route_answers_as_its_schema_says(case: Any) -> None:
    case.call_and_validate(checks=CONFORMANCE)
