"""RunEvent d'OpenLineage, tel que l'API d'ingestion l'accepte.

Le contrat est le schéma officiel 2-0-2 d'OpenLineage 1.55 (copie dans
``spec/openlineage/2-0-2``). Ces modèles en sont la traduction pour le chemin
chaud : un test les compare au schéma officiel, événement par événement.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AnyUrl,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
)

_RFC_3339 = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?([Zz]|[+-][0-9]{2}:[0-9]{2})"
)
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _rfc_3339(value: object) -> object:
    if not isinstance(value, str) or not _RFC_3339.fullmatch(value):
        raise ValueError("date et heure attendues au format RFC 3339, avec fuseau horaire")
    return value


def _canonical_uuid(value: object) -> object:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise ValueError("UUID attendu sous sa forme canonique")
    return value


EventTime = Annotated[AwareDatetime, BeforeValidator(_rfc_3339)]
RunId = Annotated[UUID, BeforeValidator(_canonical_uuid)]
EventType = Literal["START", "RUNNING", "COMPLETE", "ABORT", "FAIL", "OTHER"]


class Facet(BaseModel):
    """Facette OpenLineage : tout champ est admis, producteur et schéma sont exigés."""

    model_config = ConfigDict(extra="allow", populate_by_name=False)

    producer: AnyUrl = Field(alias="_producer")
    schema_url: AnyUrl = Field(alias="_schemaURL")


class Run(BaseModel):
    model_config = ConfigDict(extra="ignore")

    run_id: RunId = Field(alias="runId")
    facets: dict[str, Facet] = Field(default_factory=dict)


class Job(BaseModel):
    model_config = ConfigDict(extra="ignore")

    namespace: str
    name: str
    facets: dict[str, Facet] = Field(default_factory=dict)


class InputDataset(BaseModel):
    model_config = ConfigDict(extra="ignore")

    namespace: str
    name: str
    facets: dict[str, Facet] = Field(default_factory=dict)
    input_facets: dict[str, Facet] = Field(default_factory=dict, alias="inputFacets")


class OutputDataset(BaseModel):
    model_config = ConfigDict(extra="ignore")

    namespace: str
    name: str
    facets: dict[str, Facet] = Field(default_factory=dict)
    output_facets: dict[str, Facet] = Field(default_factory=dict, alias="outputFacets")


class RunEvent(BaseModel):
    """Événement d'exécution OpenLineage."""

    model_config = ConfigDict(extra="ignore")

    event_time: EventTime = Field(alias="eventTime")
    producer: AnyUrl
    schema_url: AnyUrl = Field(alias="schemaURL")
    event_type: EventType = Field(default="OTHER", alias="eventType")
    run: Run
    job: Job
    inputs: list[InputDataset] = Field(default_factory=list)
    outputs: list[OutputDataset] = Field(default_factory=list)


class FieldError(BaseModel):
    """Champ fautif d'un événement rejeté."""

    path: str = Field(description="Pointeur JSON (RFC 6901) vers le champ fautif.")
    type: str = Field(description="Code stable du motif de rejet.")
    message: str


_RUN_EVENT = TypeAdapter(RunEvent)


def json_pointer(location: tuple[int | str, ...]) -> str:
    """Pointeur JSON (RFC 6901) d'un emplacement dans l'événement."""
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in location)


def validate_event(raw: object) -> tuple[RunEvent | None, list[FieldError]]:
    """Valide un événement. Renvoie l'événement, ou la liste de ses champs fautifs."""
    if not isinstance(raw, dict):
        return None, [
            FieldError(path="", type="not_an_object", message="Un événement est un objet JSON.")
        ]
    try:
        return _RUN_EVENT.validate_python(raw), []
    except ValidationError as error:
        # Ni la valeur reçue ni son contexte ne sont renvoyés : chemin, code, motif.
        return None, [
            FieldError(path=json_pointer(item["loc"]), type=item["type"], message=item["msg"])
            for item in error.errors(include_url=False, include_input=False, include_context=False)
        ]


def run_event_json_schema() -> dict[str, Any]:
    """Schéma JSON du RunEvent, pour la spécification OpenAPI."""
    return RunEvent.model_json_schema(by_alias=True, ref_template="#/components/schemas/{model}")
