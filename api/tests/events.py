"""Événements OpenLineage d'exemple pour les tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
from uuid import UUID

PRODUCER = "https://github.com/alebgl77/AnswerLineage/tree/0.0.0/proxy"
SCHEMA_URL = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
OFFICIAL_SCHEMA = (
    Path(__file__).resolve().parents[2] / "spec" / "openlineage" / "2-0-2" / "OpenLineage.json"
)


def facet(**fields: Any) -> dict[str, Any]:
    return {
        "_producer": PRODUCER,
        "_schemaURL": "https://openlineage.io/spec/facets/1-0-0/X.json",
        **fields,
    }


def run_event(index: int = 0, **overrides: Any) -> dict[str, Any]:
    """Appel d'outil complet : run enfant, table lue, facettes."""
    event: dict[str, Any] = {
        "eventType": "COMPLETE",
        "eventTime": f"2026-11-02T09:00:{index % 60:02d}.{index:06d}Z",
        "producer": PRODUCER,
        "schemaURL": SCHEMA_URL,
        "run": {
            "runId": str(UUID(int=0x0193A9C57A107C3E9D5E2F1B6C8A0000 + index)),
            "facets": {
                "parent": facet(
                    run={"runId": "0193a9c5-7a10-7c3e-9d5e-2f1b6c8affff"},
                    job={"namespace": "agent://ventes", "name": "analyste-ventes"},
                ),
                "answerlineage_toolCall": facet(server="entrepot", tool="execute_sql"),
            },
        },
        "job": {"namespace": "mcp://entrepot", "name": "execute_sql"},
        "inputs": [
            {
                "namespace": "bigquery",
                "name": "finance.ref.taux_change",
                "facets": {"schema": facet(fields=[{"name": "devise"}, {"name": "taux"}])},
            }
        ],
        "outputs": [],
    }
    event.update(overrides)
    return event


def minimal_event() -> dict[str, Any]:
    return {
        "eventTime": "2026-11-02T09:00:00+01:00",
        "producer": PRODUCER,
        "schemaURL": SCHEMA_URL,
        "run": {"runId": "0193a9c5-7a10-7c3e-9d5e-2f1b6c8a4e77"},
        "job": {"namespace": "mcp://entrepot", "name": "execute_sql"},
    }


def mutated(path: list[str | int], value: Any = ..., *, base: dict[str, Any] | None = None) -> Any:
    """Copie de l'événement où un champ est remplacé, ou retiré si aucune valeur n'est donnée."""
    event = copy.deepcopy(base if base is not None else run_event())
    target: Any = event
    for key in path[:-1]:
        target = target[key]
    if value is ...:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return event


def official_run_event_schema() -> dict[str, Any]:
    specification = json.loads(OFFICIAL_SCHEMA.read_text(encoding="utf-8"))
    return {
        "$schema": specification["$schema"],
        "$id": specification["$id"],
        "$defs": specification["$defs"],
        "$ref": "#/$defs/RunEvent",
    }
