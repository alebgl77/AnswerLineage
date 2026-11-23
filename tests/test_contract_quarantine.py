"""Contrat entre le proxy et l'API : les périmètres en quarantaine que l'une diffuse, l'autre
les lit.

Le proxy ne dépend pas de l'API : les modes qu'il connaît et les bornes de la
réponse y sont écrits une seconde fois. Ce test les tient d'accord, et vérifie
qu'une réponse que l'API rend à ses bornes, cent quarantaines de vingt jeux, tient
dans ce que le proxy accepte de lire.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from starlette.responses import JSONResponse

from answerlineage_api.incidents.models import (
    MAX_DATASETS,
    ActiveQuarantine,
    ActiveQuarantines,
    QuarantinedDataset,
)
from answerlineage_api.incidents.quarantine import version_of
from answerlineage_api.store.schema import MAX_ACTIVE_QUARANTINES, QUARANTINE_MODES
from answerlineage_proxy import quarantine as proxy


def test_the_proxy_knows_the_modes_and_the_bounds_of_the_api() -> None:
    assert proxy.MODES == QUARANTINE_MODES
    assert proxy.MAX_QUARANTINES == MAX_ACTIVE_QUARANTINES
    assert proxy.MAX_DATASETS == MAX_DATASETS


def test_a_response_of_the_api_at_its_bounds_is_read_whole_by_the_proxy() -> None:
    quarantines = [
        ActiveQuarantine(
            incident_id=UUID(int=rank),
            mode="warn",
            activated_at=datetime(2026, 11, 16, 8, 55, rank % 60, 204517, tzinfo=UTC),
            datasets=[
                QuarantinedDataset(
                    namespace="bigquery://projet-données-référence",
                    name=f"entrepôt.ventes_{rank:03d}.commandes_détaillées_{index:02d}_"
                    + "x" * 160,
                )
                for index in range(MAX_DATASETS)
            ],
        )
        for rank in range(MAX_ACTIVE_QUARANTINES)
    ]
    document = ActiveQuarantines(version=version_of(quarantines), quarantines=quarantines)
    # Le corps tel que la route le rend.
    body = JSONResponse(document.model_dump(mode="json")).body

    known = proxy.parse(bytes(body))
    assert len(body) <= proxy.MAX_RESPONSE_BYTES
    assert known.version == document.version
    assert len(known.scopes) == MAX_ACTIVE_QUARANTINES
    assert known.datasets == MAX_ACTIVE_QUARANTINES * MAX_DATASETS
    assert {scope.incident_id for scope in known.scopes} == {
        str(item.incident_id) for item in quarantines
    }
