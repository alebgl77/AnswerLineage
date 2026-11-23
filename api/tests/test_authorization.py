"""Chaque route exige un jeton et un rôle : des tests générés depuis la spécification publiée.

Les cas viennent de l'OpenAPI, opération par opération (Schemathesis) : sans
jeton, l'appel reçoit ``401`` ; avec un jeton dont aucun rôle n'ouvre la route,
``403``. Les rôles de chaque opération sont lus dans la spécification, que la
matrice des droits remplit ; une route absente de la matrice fait échouer les
tests. La base n'est jamais jointe : un refus vient avant la route.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import schemathesis
from fastapi import APIRouter, FastAPI
from hypothesis import HealthCheck, settings
from schemathesis.generation.modes import GenerationMode
from sqlalchemy.ext.asyncio import create_async_engine

from answerlineage_api.app import create_app
from answerlineage_api.auth import matrix
from answerlineage_api.auth.matrix import ACCESS, SCHEME, render_markdown
from answerlineage_api.auth.roles import PERSON_ROLES, Role
from api.tests.issuer import TEST_ISSUER, authentication

ROOT = Path(__file__).resolve().parents[2]
PUBLISHED = ROOT / "spec" / "openapi" / "answerlineage-v1.json"
DOCUMENT = ROOT / "docs" / "securite" / "authentification.md"
# Une base que rien ne joint : un refus d'accès vient avant toute lecture.
NOWHERE = "postgresql+psycopg://personne@127.0.0.1:1/aucune"


def published() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(PUBLISHED.read_text(encoding="utf-8"))
    return document


def roles_of(operation: dict[str, Any]) -> frozenset[str]:
    """Les rôles qui ouvrent une opération, tels que la spécification les publie."""
    return frozenset(role for need in operation["security"] for role in need[SCHEME])


@pytest.fixture
def api_schema() -> Iterator[Any]:
    engine = create_async_engine(NOWHERE)
    try:
        app = create_app(engine, allowed_hosts=["testserver"], authentication=authentication())
        loaded = schemathesis.openapi.from_asgi("/api/v1/openapi.json", app)
        # Des requêtes que la route accepterait : seul le jeton décide de l'accès, et le
        # test le pose lui-même, sans qu'aucun en-tête d'autorisation ne soit généré.
        loaded.config.generation.update(
            modes=[GenerationMode.POSITIVE], with_security_parameters=False
        )
        yield loaded
    finally:
        asyncio.run(engine.dispose())


schema = schemathesis.pytest.from_fixture("api_schema")
CHECKED = settings(
    max_examples=3,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)


def called(case: Any, token: str | None) -> Any:
    """Le cas généré, sans autre en-tête d'autorisation que celui du jeton donné."""
    for name in [name for name in case.headers if name.lower() == "authorization"]:
        del case.headers[name]
    return case.call(headers=None if token is None else {"Authorization": f"Bearer {token}"})


@schema.parametrize()
@CHECKED
def test_every_route_refuses_a_call_without_a_token(case: Any) -> None:
    operation = published()["paths"][case.operation.path].get(case.method.lower())
    if operation is None:
        # Une méthode que l'opération n'a pas : le routeur la refuse avant toute route.
        assert called(case, None).status_code == 405
        return
    answered = called(case, None)
    if not roles_of(operation):
        # La sonde de vie et la connexion de l'explorateur : ouvertes, sans rien du lignage.
        assert answered.status_code == 200
        return
    assert answered.status_code == 401
    assert answered.json() == {"error": "missing_token"}
    assert answered.headers["www-authenticate"] == ["Bearer"]


@schema.parametrize()
@CHECKED
def test_every_route_refuses_a_role_that_does_not_open_it(case: Any) -> None:
    operation = published()["paths"][case.operation.path].get(case.method.lower())
    allowed = frozenset() if operation is None else roles_of(operation)
    if not allowed:
        return
    others = [role for role in Role if role.value not in allowed]
    for roles in ([], others):
        answered = called(case, TEST_ISSUER.token(*roles))
        assert answered.status_code == 403, roles
        assert answered.json() == {"error": "insufficient_role"}
        assert answered.headers["www-authenticate"] == ['Bearer error="insufficient_scope"']


def test_every_operation_of_the_specification_is_in_the_matrix() -> None:
    """Critère : chaque route figure à la matrice, et la spécification en publie les rôles."""
    operations = {
        (method.upper(), path): operation
        for path, item in published()["paths"].items()
        for method, operation in item.items()
    }
    assert set(operations) == set(ACCESS)
    for key, operation in operations.items():
        assert roles_of(operation) == {role.value for role in ACCESS[key].roles}, key


def test_a_route_missing_from_the_matrix_is_named() -> None:
    router = APIRouter(prefix="/api/v1")

    @router.get("/oubliee")
    async def forgotten() -> None:  # pragma: no cover — jamais appelée.
        return None

    app = FastAPI()
    app.include_router(router)
    found = matrix.undeclared(app.routes)
    assert "GET /api/v1/oubliee n'est pas dans la matrice" in found


def test_the_application_does_not_build_with_a_route_missing_from_the_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reduced = {key: access for key, access in ACCESS.items() if key != ("GET", "/api/v1/graph")}
    monkeypatch.setattr(matrix, "ACCESS", reduced)
    with pytest.raises(RuntimeError, match="GET /api/v1/graph n'est pas dans la matrice"):
        create_app(authentication=authentication())


def test_every_person_role_opens_something_and_only_emitters_ingest() -> None:
    opened = {role for access in ACCESS.values() for role in access.roles}
    assert opened == set(Role)
    for (method, path), access in ACCESS.items():
        if path.startswith("/api/v1/lineage") or (method, path) == ("POST", "/api/v1/coverage"):
            assert access.roles == {Role.EMITTER}
        elif Role.EMITTER in access.roles:
            # Hors de l'ingestion, l'émetteur ne lit que les périmètres en quarantaine.
            assert path == "/api/v1/quarantine"
    assert ACCESS[("GET", "/api/v1/graph")].roles >= PERSON_ROLES


def test_the_documented_matrix_is_the_one_of_the_code() -> None:
    """Critère : la matrice des droits, telle que la documentation la donne, est celle du code."""
    text = DOCUMENT.read_text(encoding="utf-8")
    assert render_markdown() in text, (
        "La matrice de docs/securite/authentification.md n'est plus celle du code : "
        "y recopier answerlineage_api.auth.matrix.render_markdown()."
    )
