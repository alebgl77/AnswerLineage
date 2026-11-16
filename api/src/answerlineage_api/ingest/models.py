"""RunEvent d'OpenLineage, tel que l'API d'ingestion l'accepte.

Le contrat est le schéma officiel 2-0-2 d'OpenLineage 1.55 (copie dans
``spec/openlineage/2-0-2``). Ces modèles en sont la traduction pour le chemin
chaud : un test les compare au schéma officiel, événement par événement.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final, Literal, get_args
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
    ValidationInfo,
    field_validator,
)
from pydantic_core import PydanticCustomError

from answerlineage_api import identifiers
from answerlineage_api.identifiers import identifier_fault

# Facettes propres à AnswerLineage, lues par la projection.
AGENT_FACET: Final = "answerlineage_agent"
TOOL_CALL_FACET: Final = "answerlineage_toolCall"
DELIVERY_FACET: Final = "answerlineage_delivery"
# Facette de qualité de service d'un run : ses signaux restent dans l'événement reçu.
QOS_FACET: Final = "answerlineage_qos"

# Signaux de qualité de service : durées en millisecondes et compteurs, un milliard au
# plus (plus de onze jours, plus de dix mille par seconde sur un jour) ; une issue
# d'une liste fermée ; un code d'erreur, JSON-RPC ou HTTP, entier sur 32 bits.
SIGNAL_MAX: Final = 1_000_000_000
Outcome = Literal[
    "success", "jsonrpc_error", "http_error", "transport_error", "timeout", "client_abort"
]
OUTCOMES: Final[tuple[str, ...]] = get_args(Outcome)
ERROR_CODE_MIN: Final = -(2**31)
ERROR_CODE_MAX: Final = 2**31 - 1

# Plage admise pour l'heure d'un événement, ramenée en UTC. Aucun événement de
# lignage ne précède 1970, et un jour d'avance couvre tout décalage d'horloge
# comme un fuseau mal déclaré (14 heures au plus). Au-delà, l'heure est fausse :
# elle fixerait pour toujours l'état d'un run ou l'ordre des réponses, et une
# année qui sort de 1 à 9999 une fois ramenée en UTC ne se relit plus.
EVENT_TIME_EARLIEST: Final = datetime(1970, 1, 1, tzinfo=UTC)
EVENT_TIME_AHEAD: Final = timedelta(days=1)

# Nombre d'éléments de ce qui se répète dans un événement, et que la validation
# et la projection parcourent. Larges pour l'usage réel, mais finis.
# Une requête d'agent lit au plus quelques dizaines de tables.
INPUTS_MAX: Final = 1_000
OUTPUTS_MAX: Final = 1_000
# OpenLineage définit une trentaine de facettes standard par objet.
FACETS_MAX: Final = 100
# BigQuery admet 10 000 colonnes par table, PostgreSQL 1 600.
SCHEMA_FIELDS_MAX: Final = 10_000
# La borne du schéma publié de la facette de diffusion.
RECIPIENTS_MAX: Final = 10_000
TOO_MANY_ITEMS: Final = "too_many_items"

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
_EVENT_TIME: TypeAdapter[datetime] = TypeAdapter(EventTime)
RunId = Annotated[UUID, BeforeValidator(_canonical_uuid)]
EventType = Literal["START", "RUNNING", "COMPLETE", "ABORT", "FAIL", "OTHER"]


class Facet(BaseModel):
    """Facette OpenLineage : tout champ est admis, producteur et schéma sont exigés."""

    model_config = ConfigDict(extra="allow", populate_by_name=False)

    producer: AnyUrl = Field(alias="_producer")
    schema_url: AnyUrl = Field(alias="_schemaURL")


# Les bornes figurent dans le contrat publié ; ``count_errors`` et
# ``facet_name_errors`` les appliquent, avec un code stable.
_FACETS_SCHEMA: Final[dict[str, Any]] = {
    "maxProperties": FACETS_MAX,
    "propertyNames": {"minLength": 1, "maxLength": identifiers.FACET_NAME_MAX},
}


def _facets(alias: str | None = None) -> Any:
    return Field(default_factory=dict, alias=alias, json_schema_extra=_FACETS_SCHEMA)


class Run(BaseModel):
    model_config = ConfigDict(extra="ignore")

    run_id: RunId = Field(alias="runId")
    facets: dict[str, Facet] = _facets()


class Job(BaseModel):
    model_config = ConfigDict(extra="ignore")

    namespace: str
    name: str
    facets: dict[str, Facet] = _facets()


class InputDataset(BaseModel):
    model_config = ConfigDict(extra="ignore")

    namespace: str
    name: str
    facets: dict[str, Facet] = _facets()
    input_facets: dict[str, Facet] = _facets("inputFacets")


class OutputDataset(BaseModel):
    model_config = ConfigDict(extra="ignore")

    namespace: str
    name: str
    facets: dict[str, Facet] = _facets()
    output_facets: dict[str, Facet] = _facets("outputFacets")


class RunEvent(BaseModel):
    """Événement d'exécution OpenLineage."""

    model_config = ConfigDict(extra="ignore")

    event_time: EventTime = Field(alias="eventTime")
    producer: AnyUrl
    schema_url: AnyUrl = Field(alias="schemaURL")
    event_type: EventType = Field(default="OTHER", alias="eventType")
    run: Run
    job: Job
    inputs: list[InputDataset] = Field(
        default_factory=list, json_schema_extra={"maxItems": INPUTS_MAX}
    )
    outputs: list[OutputDataset] = Field(
        default_factory=list, json_schema_extra={"maxItems": OUTPUTS_MAX}
    )

    @field_validator("event_time")
    @classmethod
    def _within_range(cls, value: datetime, info: ValidationInfo) -> datetime:
        context = info.context if isinstance(info.context, Mapping) else {}
        received_at = context.get("received_at") or datetime.now(UTC)
        if not EVENT_TIME_EARLIEST <= value <= received_at + EVENT_TIME_AHEAD:
            raise PydanticCustomError(
                "event_time_out_of_range",
                "L'heure de l'événement, ramenée en UTC, tombe entre le 1er janvier 1970 "
                "et un jour après sa réception.",
            )
        return value


class FieldError(BaseModel):
    """Champ fautif d'un événement rejeté."""

    path: str = Field(description="Pointeur JSON (RFC 6901) vers le champ fautif.")
    type: str = Field(description="Code stable du motif de rejet.")
    message: str


_RUN_EVENT = TypeAdapter(RunEvent)


def json_pointer(location: tuple[int | str, ...]) -> str:
    """Pointeur JSON (RFC 6901) d'un emplacement dans l'événement."""
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in location)


def parse_event_time(value: object) -> datetime:
    """Heure d'un événement, lue comme l'ingestion la lit : RFC 3339, ``T`` et ``Z``
    en majuscules comme en minuscules. Lève ``ValidationError`` si elle est illisible."""
    return _EVENT_TIME.validate_python(value)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _items(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


Location = tuple[int | str, ...]


def _identifiers(raw: Mapping[str, Any]) -> Iterator[tuple[Location, object, int]]:
    """Les identifiants d'un événement : emplacement, valeur reçue, longueur maximale.

    Ce sont les champs que la projection recopie dans le graphe, plus le job
    parent. Une facette inconnue, ou un champ libre d'une facette connue, n'en
    contient pas.
    """
    job = _mapping(raw.get("job"))
    yield ("job", "namespace"), job.get("namespace"), identifiers.NAMESPACE_MAX
    yield ("job", "name"), job.get("name"), identifiers.JOB_NAME_MAX

    run_facets = _mapping(_mapping(raw.get("run")).get("facets"))
    parent_job = _mapping(_mapping(run_facets.get("parent")).get("job"))
    parent: Location = ("run", "facets", "parent", "job")
    yield (*parent, "namespace"), parent_job.get("namespace"), identifiers.NAMESPACE_MAX
    yield (*parent, "name"), parent_job.get("name"), identifiers.JOB_NAME_MAX
    tool_call = _mapping(run_facets.get(TOOL_CALL_FACET))
    for field in ("server", "tool"):
        yield ("run", "facets", TOOL_CALL_FACET, field), tool_call.get(field), identifiers.TOOL_MAX

    for side in ("inputs", "outputs"):
        for index, item in enumerate(_items(raw.get(side))):
            dataset = _mapping(item)
            yield (side, index, "namespace"), dataset.get("namespace"), identifiers.NAMESPACE_MAX
            yield (side, index, "name"), dataset.get("name"), identifiers.DATASET_NAME_MAX
            facets = _mapping(dataset.get("facets"))
            where: Location = (side, index, "facets")
            version = _mapping(facets.get("version")).get("datasetVersion")
            yield (*where, "version", "datasetVersion"), version, identifiers.VERSION_MAX
            if side == "inputs":
                fields = _items(_mapping(facets.get("schema")).get("fields"))
                for rank, column in enumerate(fields):
                    name = _mapping(column).get("name")
                    yield (*where, "schema", "fields", rank, "name"), name, identifiers.COLUMN_MAX
                continue
            for container in ("facets", "outputFacets"):
                delivery = _mapping(_mapping(dataset.get(container)).get(DELIVERY_FACET))
                at: Location = (side, index, container, DELIVERY_FACET)
                yield (*at, "fingerprint"), delivery.get("fingerprint"), identifiers.FINGERPRINT_MAX
                yield (*at, "channel"), delivery.get("channel"), identifiers.CHANNEL_MAX
                yield (*at, "messageRef"), delivery.get("messageRef"), identifiers.MESSAGE_REF_MAX
                for rank, recipient in enumerate(_items(delivery.get("recipients"))):
                    yield (*at, "recipients", rank), recipient, identifiers.RECIPIENT_MAX


def identifier_errors(raw: Mapping[str, Any]) -> list[FieldError]:
    """Identifiants d'un événement qui n'ont pas une forme admise.

    Seul un texte est contrôlé ici : le type d'un champ obligatoire relève du
    schéma, et une valeur de facette qui n'est pas un texte est ignorée par la
    projection.
    """
    errors = []
    for location, value, max_length in _identifiers(raw):
        if not isinstance(value, str):
            continue
        fault = identifier_fault(value, max_length=max_length)
        if fault is not None:
            errors.append(
                FieldError(path=json_pointer(location), type=fault.code, message=fault.message)
            )
    return errors


def _facet_containers(raw: Mapping[str, Any]) -> Iterator[tuple[Location, object]]:
    """Les objets d'un événement dont les clés sont des noms de facette."""
    yield ("run", "facets"), _mapping(raw.get("run")).get("facets")
    yield ("job", "facets"), _mapping(raw.get("job")).get("facets")
    for side, own in (("inputs", "inputFacets"), ("outputs", "outputFacets")):
        for index, item in enumerate(_items(raw.get(side))):
            dataset = _mapping(item)
            for container in ("facets", own):
                yield (side, index, container), dataset.get(container)


def _too_many(location: Location, bound: int) -> FieldError:
    # Le motif cite la borne, jamais le nombre reçu.
    return FieldError(
        path=json_pointer(location),
        type=TOO_MANY_ITEMS,
        message=f"Ce champ compte au plus {bound} éléments.",
    )


def _over(value: object, bound: int) -> bool:
    return isinstance(value, list | dict) and len(value) > bound


def count_errors(raw: Mapping[str, Any]) -> list[FieldError]:
    """Ce qui se répète au-delà de sa borne : entrées, sorties, facettes, colonnes, destinataires.

    Un tableau trop long n'est pas parcouru : ce qu'il contient n'est pas contrôlé.
    """
    errors = [
        _too_many((side,), bound)
        for side, bound in (("inputs", INPUTS_MAX), ("outputs", OUTPUTS_MAX))
        if _over(raw.get(side), bound)
    ]
    if errors:
        return errors
    errors = [
        _too_many(location, FACETS_MAX)
        for location, facets in _facet_containers(raw)
        if _over(facets, FACETS_MAX)
    ]
    for side in ("inputs", "outputs"):
        for index, item in enumerate(_items(raw.get(side))):
            dataset = _mapping(item)
            fields = _mapping(_mapping(dataset.get("facets")).get("schema")).get("fields")
            if _over(fields, SCHEMA_FIELDS_MAX):
                at: Location = (side, index, "facets", "schema", "fields")
                errors.append(_too_many(at, SCHEMA_FIELDS_MAX))
            if side == "inputs":
                continue
            for container in ("facets", "outputFacets"):
                delivery = _mapping(_mapping(dataset.get(container)).get(DELIVERY_FACET))
                if _over(delivery.get("recipients"), RECIPIENTS_MAX):
                    at = (side, index, container, DELIVERY_FACET, "recipients")
                    errors.append(_too_many(at, RECIPIENTS_MAX))
    return errors


def facet_name_errors(raw: Mapping[str, Any]) -> list[FieldError]:
    """Noms de facette qui n'ont pas la forme d'un nom.

    Le nom est choisi par l'émetteur : il n'est jamais cité. L'erreur désigne
    l'objet qui le porte, avec le code et le motif fixes de l'identifiant.
    """
    found: dict[tuple[str, str], FieldError] = {}
    for location, facets in _facet_containers(raw):
        for name in _mapping(facets):
            fault = identifier_fault(name, max_length=identifiers.FACET_NAME_MAX)
            if fault is not None:
                path = json_pointer(location)
                found[path, fault.code] = FieldError(
                    path=path, type=fault.code, message=fault.message
                )
    return list(found.values())


_Duration = Annotated[float, Field(strict=True, ge=0, le=SIGNAL_MAX, allow_inf_nan=False)]
_Count = Annotated[int, Field(strict=True, ge=0, le=SIGNAL_MAX)]
_ErrorCode = Annotated[int, Field(strict=True, ge=ERROR_CODE_MIN, le=ERROR_CODE_MAX)]
# Chaque signal présent dans la facette de qualité de service, et ce qu'il admet.
_SIGNALS: Final[Mapping[str, TypeAdapter[Any]]] = {
    "latencyMs": TypeAdapter(_Duration),
    "addedLatencyMs": TypeAdapter(_Duration),
    "outcome": TypeAdapter(Outcome),
    "errorCode": TypeAdapter(_ErrorCode),
    "errorCount": TypeAdapter(_Count),
    "retryCount": TypeAdapter(_Count),
}


def signal_errors(raw: Mapping[str, Any]) -> list[FieldError]:
    """Signaux de qualité de service hors de leurs bornes.

    Durées et compteurs sont des nombres bornés, l'issue vient d'une liste
    fermée, le code d'erreur est un entier sur 32 bits. Un signal absent n'est
    pas exigé ; ``null`` n'en est pas un. Les autres champs de la facette ne
    sont pas contrôlés ici.
    """
    facet = _mapping(_mapping(raw.get("run")).get("facets")).get(QOS_FACET)
    if not isinstance(facet, Mapping):
        return []
    errors: list[FieldError] = []
    for name, adapter in _SIGNALS.items():
        if name not in facet:
            continue
        try:
            adapter.validate_python(facet[name])
        except ValidationError as error:
            path = json_pointer(("run", "facets", QOS_FACET, name))
            errors.extend(
                FieldError(path=path, type=item["type"], message=item["msg"])
                for item in error.errors(
                    include_url=False, include_input=False, include_context=False
                )
            )
    return errors


def _admitted(location: Location) -> bool:
    """Vrai si chaque clé d'un emplacement a la forme d'un nom, et peut donc être citée."""
    return all(
        not isinstance(part, str)
        or identifier_fault(part, max_length=identifiers.FACET_NAME_MAX) is None
        for part in location
    )


def validate_event(
    raw: object, *, received_at: datetime | None = None
) -> tuple[RunEvent | None, list[FieldError]]:
    """Valide un événement. Renvoie l'événement, ou la liste de ses champs fautifs.

    L'heure de l'événement est bornée par ``received_at``, l'heure de sa
    réception ; à défaut, par l'heure courante. Un événement qui dépasse une
    borne de nombre n'est pas validé plus avant : seul ce dépassement est
    rapporté.
    """
    if not isinstance(raw, dict):
        return None, [
            FieldError(path="", type="not_an_object", message="Un événement est un objet JSON.")
        ]
    counted = count_errors(raw)
    if counted:
        return None, counted
    event: RunEvent | None = None
    errors: list[FieldError] = []
    try:
        event = _RUN_EVENT.validate_python(raw, context={"received_at": received_at})
    except ValidationError as error:
        # Ni la valeur reçue ni son contexte ne sont renvoyés : chemin, code, motif.
        # Les seules clés reçues d'un chemin sont des noms de facette ; une erreur
        # sous un nom refusé n'est pas rapportée, le nom suffit à rejeter l'événement.
        errors = [
            FieldError(path=json_pointer(item["loc"]), type=item["type"], message=item["msg"])
            for item in error.errors(include_url=False, include_input=False, include_context=False)
            if _admitted(item["loc"])
        ]
    errors.extend(facet_name_errors(raw))
    errors.extend(identifier_errors(raw))
    errors.extend(signal_errors(raw))
    if errors:
        return None, errors
    return event, []


def run_event_json_schema() -> dict[str, Any]:
    """Schéma JSON du RunEvent, pour la spécification OpenAPI."""
    return RunEvent.model_json_schema(by_alias=True, ref_template="#/components/schemas/{model}")
