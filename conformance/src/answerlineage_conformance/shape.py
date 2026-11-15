"""Forme : ce que l'ingestion rejetterait, et les facettes propres hors de leur schéma publié.

L'ingestion est jugée avec son propre code : ``nesting_depth`` pour la
profondeur qu'elle accepte de lire, ``validate_event`` pour le schéma, l'heure,
les identifiants et les bornes de nombre, ``event_identifier`` pour les textes
que la base ne peut pas conserver. Ce que le kit accepte est ce que le produit
accepte. Un événement est jugé comme s'il était envoyé seul : la taille d'un
corps et celle d'un lot tiennent à la façon d'envoyer, que la capture ne dit pas.

Une facette propre est jugée contre le schéma que son ``_schemaURL`` désigne.
Une clé au préfixe d'AnswerLineage qui n'est pas celle d'une facette publiée
est une faute de frappe : le graphe n'y verrait qu'un job ordinaire.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from typing import Any

from answerlineage_api.ingest.body import MAX_DEPTH, nesting_depth
from answerlineage_api.ingest.models import validate_event
from answerlineage_api.ingest.store import UnstorableEventError, event_identifier
from answerlineage_conformance.findings import Finding, items, mapping
from answerlineage_conformance.pointers import Location, location_of
from answerlineage_conformance.schemas import FACETS, is_own, violations

_SIDES = ("inputs", "outputs")
_DATASET_CONTAINERS = ("facets", "inputFacets", "outputFacets")


def ingestion(
    events: Sequence[object], received_at: datetime
) -> tuple[list[Finding], dict[int, Mapping[str, Any]]]:
    """Ce que l'ingestion rejetterait, et les événements qu'elle accepterait, par rang."""
    findings: list[Finding] = []
    accepted: dict[int, Mapping[str, Any]] = {}
    for rank, raw in enumerate(events):
        if isinstance(raw, dict) and nesting_depth(json.dumps(raw).encode()) > MAX_DEPTH:
            # Refusé avant toute analyse : rien d'autre n'en serait dit.
            findings.append(Finding("ingestion_rejected", rank, (), reason="json_too_deep"))
            continue
        _, errors = validate_event(raw, received_at=received_at)
        rejected = [
            Finding("ingestion_rejected", rank, location_of(error.path), reason=error.type)
            for error in errors
        ]
        if isinstance(raw, dict):
            try:
                event_identifier(raw)
            except UnstorableEventError:
                rejected.insert(
                    0, Finding("ingestion_rejected", rank, (), reason="unstorable_text")
                )
            if not rejected:
                accepted[rank] = raw
        findings.extend(rejected)
    return findings, accepted


def _containers(event: Mapping[str, Any]) -> Iterator[tuple[Location, str, Mapping[str, Any]]]:
    """Les porte-facettes d'un événement : emplacement, place, facettes."""
    for owner in ("run", "job"):
        yield (owner, "facets"), f"{owner}.facets", mapping(mapping(event.get(owner)).get("facets"))
    for side in _SIDES:
        for index, dataset in enumerate(items(event.get(side))):
            for container in _DATASET_CONTAINERS:
                facets = mapping(mapping(dataset).get(container))
                yield (side, index, container), f"{side}.{container}", facets


def facets(events: Sequence[object]) -> list[Finding]:
    """Les facettes propres inconnues, mal placées, ou qui ne respectent pas leur schéma."""
    findings: list[Finding] = []
    for rank, raw in enumerate(events):
        for where, place, container in _containers(mapping(raw)):
            for key, facet in container.items():
                if not is_own(key):
                    continue
                location: Location = (*where, key)
                spec = FACETS.get(key)
                if spec is None:
                    findings.append(Finding("facet_unknown", rank, location))
                    continue
                if place not in spec.places:
                    findings.append(Finding("facet_misplaced", rank, location))
                    continue
                address = mapping(facet).get("_schemaURL")
                if isinstance(address, str) and address not in spec.addresses:
                    findings.append(
                        Finding("facet_schema_unknown", rank, (*location, "_schemaURL"))
                    )
                if address not in spec.addresses:
                    address = spec.latest
                findings.extend(
                    Finding("facet_schema_violation", rank, (*location, *inside), reason=rule)
                    for inside, rule in violations(address, facet)
                )
    return findings
