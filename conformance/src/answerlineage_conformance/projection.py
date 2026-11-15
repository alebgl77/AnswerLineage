"""Projection : ce que l'émetteur déclare et que le graphe ignorerait.

Chaque événement que l'ingestion accepterait est projeté par le code des
workers. La projection est tolérante : une facette mal formée est ignorée sans
bruit. Le kit compare donc ce que l'événement déclare à ce que le graphe en
retiendrait : un parent, une diffusion ou des colonnes perdus en route sont
des réponses qu'un rappel ne retrouverait pas, ou retrouverait à tort.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from answerlineage_api.identifiers import normalize_identifier
from answerlineage_api.ingest.models import DELIVERY_FACET
from answerlineage_api.store.graph import RunRecord
from answerlineage_conformance.findings import Finding, items, mapping, run_facets
from answerlineage_workers.projection import project

# Là où la projection lit une diffusion, dans cet ordre.
_DELIVERY_CONTAINERS = ("facets", "outputFacets")


def records(
    accepted: Mapping[int, Mapping[str, Any]],
) -> tuple[list[Finding], dict[int, RunRecord]]:
    """Ce que le graphe retiendrait de chaque événement accepté, par rang."""
    findings: list[Finding] = []
    projected: dict[int, RunRecord] = {}
    for rank, event in accepted.items():
        try:
            projected[rank] = project(event)
        except Exception as error:
            # Comme la file de traitement : la classe de l'erreur, jamais son message.
            findings.append(Finding("projection_failed", rank, (), reason=type(error).__name__))
    return findings, projected


def _declared(values: object, name: str | None = None) -> int:
    """Nombre d'éléments distincts que déclare une liste d'identifiants.

    Deux écritures d'un même nom n'en font qu'un, comme dans le graphe. Un
    élément qui n'est pas un texte compte : le graphe l'ignorerait.
    """
    if not isinstance(values, list):
        return 0 if values is None else 1
    texts = set()
    unusable = 0
    for value in values:
        text = mapping(value).get(name) if name is not None else value
        if isinstance(text, str):
            texts.add(normalize_identifier(text))
        else:
            unusable += 1
    return len(texts) + unusable


def _delivery(output: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]] | None:
    for container in _DELIVERY_CONTAINERS:
        facet = mapping(output.get(container)).get(DELIVERY_FACET)
        if isinstance(facet, Mapping):
            return container, facet
    return None


def dropped(
    accepted: Mapping[int, Mapping[str, Any]], projected: Mapping[int, RunRecord]
) -> list[Finding]:
    """Parent, diffusion et colonnes déclarés que la projection a laissés tomber."""
    findings: list[Finding] = []
    for rank, record in projected.items():
        event = accepted[rank]
        if isinstance(run_facets(event).get("parent"), Mapping) and record.parent_run_id is None:
            findings.append(Finding("parent_dropped", rank, ("run", "facets", "parent")))

        for index, read in enumerate(record.reads):
            schema = mapping(mapping(items(event.get("inputs"))[index]).get("facets")).get("schema")
            if len(read.columns) < _declared(mapping(schema).get("fields"), "name"):
                fields = ("inputs", index, "facets", "schema", "fields")
                findings.append(Finding("columns_dropped", rank, fields))

        for index, output in enumerate(items(event.get("outputs"))):
            declared = _delivery(mapping(output))
            if declared is None:
                continue
            container, facet = declared
            where = ("outputs", index, container, DELIVERY_FACET)
            # La sortie projetée seule : une réponse et sa diffusion, ou une écriture ordinaire.
            answers = project({**event, "inputs": [], "outputs": [output]}).answers
            if not answers or not answers[0].deliveries:
                findings.append(Finding("delivery_dropped", rank, where))
            elif len(answers[0].deliveries[0].recipients) < _declared(facet.get("recipients")):
                findings.append(Finding("delivery_dropped", rank, (*where, "recipients")))
    return findings
