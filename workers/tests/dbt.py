"""Événements de tests de données, à la forme qu'émet dbt-ol 1.55.

dbt émet un run par modèle testé, de job ``<projet>.<schéma>.<modèle>.test`` et
de type TEST ; son entrée est le modèle, et la facette standard
``dataQualityAssertions`` y liste les assertions, dans ``inputFacets`` et, par
compatibilité, dans ``facets``. ``assertion`` est le nom du test générique, ou
celui du test singulier ; ``severity`` est en minuscules ; ``actual`` porte le
nombre d'échecs en texte.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

PRODUCER: Final = "https://github.com/OpenLineage/OpenLineage/tree/1.55.0/integration/dbt"
RUN_EVENT: Final = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
FACET_URL: Final = (
    "https://openlineage.io/spec/facets/1-1-0/DataQualityAssertionsDatasetFacet.json"
    "#/$defs/DataQualityAssertionsDatasetFacet"
)
JOB_TYPE_URL: Final = (
    "https://openlineage.io/spec/facets/2-0-3/JobTypeJobFacet.json#/$defs/JobTypeJobFacet"
)
WAREHOUSE: Final = "postgres://entrepot:5432"
CUSTOMERS: Final = "analytique.public.clients"
ORDERS: Final = "analytique.public.commandes"
# Il y a deux jours, à l'heure ronde : un échec récent propose un incident, un ancien non.
T0: Final = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(days=2)


def at(hours: float, origin: datetime = T0) -> str:
    """Une heure d'événement, ``hours`` après ``origin``, en RFC 3339."""
    return (origin + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def run_id(rank: int) -> str:
    return str(UUID(int=0x019A_0000_0000_7000_8000_0000_0000_0000 + rank))


def assertion(
    kind: str, success: bool, column: str | None = None, severity: str = "error", **free: Any
) -> dict[str, Any]:
    """Une assertion : ``free`` porte les champs de texte libre (nom, description…)."""
    found: dict[str, Any] = {"assertion": kind, "success": success, "severity": severity}
    if column is not None:
        found["column"] = column
    return found | free


def facet(assertions: list[Any]) -> dict[str, Any]:
    return {"_producer": PRODUCER, "_schemaURL": FACET_URL, "assertions": assertions}


def model(
    assertions: list[Any],
    *,
    namespace: str = WAREHOUSE,
    name: str = CUSTOMERS,
    containers: tuple[str, ...] = ("inputFacets", "facets"),
) -> dict[str, Any]:
    """Le modèle testé, entrée du run de test, avec ses assertions."""
    return {"namespace": namespace, "name": name} | {
        container: {"dataQualityAssertions": facet(assertions)} for container in containers
    }


def dbt_test(
    rank: int,
    hours: float,
    *inputs: dict[str, Any],
    event_type: str = "COMPLETE",
    origin: datetime = T0,
) -> dict[str, Any]:
    """Le run de test d'un modèle, à l'heure ``hours`` après ``origin``."""
    return {
        "eventType": event_type,
        "eventTime": at(hours, origin),
        "producer": PRODUCER,
        "schemaURL": RUN_EVENT,
        "run": {"runId": run_id(rank), "facets": {}},
        "job": {
            "namespace": "dbt",
            "name": "analytique.public.clients.test",
            "facets": {
                "jobType": {
                    "_producer": PRODUCER,
                    "_schemaURL": JOB_TYPE_URL,
                    "processingType": "BATCH",
                    "integration": "DBT",
                    "jobType": "TEST",
                }
            },
        },
        "inputs": list(inputs),
        "outputs": [],
    }


def failing(
    rank: int,
    hours: float,
    column: str | None = "email",
    *,
    name: str = CUSTOMERS,
    origin: datetime = T0,
    kind: str = "not_null",
    severity: str = "error",
) -> dict[str, Any]:
    """Un run de test dont une assertion échoue sur ``column`` du jeu ``name``."""
    return dbt_test(
        rank,
        hours,
        model([assertion(kind, False, column, severity, actual="3")], name=name),
        origin=origin,
    )


def passing(
    rank: int,
    hours: float,
    column: str | None = "email",
    *,
    name: str = CUSTOMERS,
    origin: datetime = T0,
) -> dict[str, Any]:
    """Un run de test dont l'assertion sur ``column`` du jeu ``name`` réussit."""
    return dbt_test(
        rank,
        hours,
        model([assertion("not_null", True, column, actual="0")], name=name),
        origin=origin,
    )
