"""Un incident de données : ce qu'un responsable déclare, et ce que son analyse a trouvé.

Un incident nomme son périmètre, un ou plusieurs jeux de données par leur
espace de noms et leur nom, sa fenêtre de validité, du début inclus à la fin
exclue, et sa gravité, d'une liste fermée. Il ne porte aucun texte libre : rien
de ce qu'un déclarant écrirait ne peut atteindre un destinataire.

La déclaration est une entrée hostile comme une autre : champs connus
seulement, identifiants contrôlés comme à l'ingestion, nombre de jeux et durée
de la fenêtre bornés. Un refus donne le chemin du champ fautif, un code stable
et un motif fixe, jamais la valeur reçue ni le nom d'un champ inconnu.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from answerlineage_api import identifiers
from answerlineage_api.identifiers import identifier_fault, normalize_identifier
from answerlineage_api.ingest.models import EventTime, FieldError, json_pointer
from answerlineage_api.lineage.models import Coverage, ImpactCounts, Window
from answerlineage_api.store.schema import (
    INCIDENT_WINDOW_DAYS,
    MAX_ACTIVE_QUARANTINES,
    MAX_PROPOSAL_COLUMNS,
    MAX_SIGNAL_CLASSES,
    AnalysisError,
    AnalysisState,
    AssertionClass,
    IncidentState,
    ProposalState,
    QualitySeverity,
    QuarantineMode,
    QuarantineState,
    Severity,
    WindowBasis,
)

# Un incident touche une table, parfois les quelques tables d'un même chargement.
MAX_DATASETS: Final = 20
# Une fenêtre de validité dure au plus un mois : au-delà, la propagation et la
# mesure de la couverture dépasseraient leurs bornes.
MAX_WINDOW: Final = timedelta(days=INCIDENT_WINDOW_DAYS)
# Vingt jeux de données aux noms les plus longs tiennent dans 256 Kio, échappés.
MAX_DECLARATION_BYTES: Final = 256 * 1024
# Rien n'est enregistré avant 1970 ; au-delà de l'an 9999, une date ne se relit plus.
WINDOW_EARLIEST: Final = datetime(1970, 1, 1, tzinfo=UTC)
WINDOW_LATEST: Final = datetime(9999, 12, 30, tzinfo=UTC)

UNKNOWN_FIELD: Final = "unknown_field"
# Une demande de quarantaine ne porte qu'un mode : quelques octets.
MAX_QUARANTINE_BYTES: Final = 4 * 1024
# Une confirmation ne porte qu'une gravité et, au besoin, une fenêtre : quelques octets.
MAX_CONFIRMATION_BYTES: Final = 4 * 1024


class DeclaredDataset(BaseModel):
    """Un jeu de données faux, tel que le déclarant le nomme."""

    model_config = ConfigDict(extra="forbid")

    namespace: str = Field(
        description="Espace de noms du jeu de données, par exemple « bigquery ».",
        json_schema_extra={"minLength": 1, "maxLength": identifiers.NAMESPACE_MAX},
    )
    name: str = Field(
        description="Nom du jeu de données, tel que le graphe le connaît.",
        json_schema_extra={"minLength": 1, "maxLength": identifiers.DATASET_NAME_MAX},
    )


class DeclaredScope(BaseModel):
    """Le périmètre de l'incident."""

    model_config = ConfigDict(extra="forbid")

    datasets: list[DeclaredDataset] = Field(
        description="Les jeux de données faux, sans doublon.",
        json_schema_extra={"minItems": 1, "maxItems": MAX_DATASETS},
    )


class DeclaredWindow(BaseModel):
    """La fenêtre de validité : début inclus, fin exclue, avec fuseau."""

    model_config = ConfigDict(extra="forbid")

    start: EventTime = Field(alias="from", description="Début, inclus (RFC 3339, avec fuseau).")
    end: EventTime = Field(alias="to", description="Fin, exclue (RFC 3339, avec fuseau).")


class IncidentDeclaration(BaseModel):
    """Ce que le responsable de données déclare. Aucun texte libre."""

    model_config = ConfigDict(extra="forbid")

    scope: DeclaredScope
    window: DeclaredWindow
    severity: Severity = Field(
        description=(
            "Gravité de l'incident, de « low » à « critical » : elle ne change pas"
            " l'analyse, elle ordonnera les rappels."
        )
    )


class QuarantineRequest(BaseModel):
    """Ce que demande la mise en quarantaine d'un incident : son mode. Aucun texte libre."""

    model_config = ConfigDict(extra="forbid")

    mode: QuarantineMode = Field(
        description=(
            "Ce que les proxys feront des lectures du périmètre, d'une liste fermée."
            " « warn » : les signaler."
        )
    )


_DECLARATION: TypeAdapter[IncidentDeclaration] = TypeAdapter(IncidentDeclaration)
_WINDOW: TypeAdapter[DeclaredWindow] = TypeAdapter(DeclaredWindow)
_QUARANTINE: TypeAdapter[QuarantineRequest] = TypeAdapter(QuarantineRequest)


def declaration_json_schema() -> dict[str, Any]:
    """Schéma JSON de la déclaration, pour la spécification OpenAPI."""
    return IncidentDeclaration.model_json_schema(
        by_alias=True, ref_template="#/components/schemas/{model}"
    )


def quarantine_request_json_schema() -> dict[str, Any]:
    """Schéma JSON d'une demande de quarantaine, pour la spécification OpenAPI."""
    schema = QuarantineRequest.model_json_schema(ref_template="#/components/schemas/{model}")
    _closed_list(schema["properties"]["mode"])
    return schema


def _error(path: str, kind: str, message: str) -> FieldError:
    return FieldError(path=path, type=kind, message=message)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _schema_errors(error: ValidationError) -> list[FieldError]:
    """Les fautes de forme, sans valeur reçue. Un champ inconnu désigne l'objet qui le porte :
    son nom vient du déclarant et n'est pas cité."""
    found: dict[tuple[str, str], FieldError] = {}
    for item in error.errors(include_url=False, include_input=False, include_context=False):
        location = item["loc"]
        if item["type"] == "extra_forbidden":
            path = json_pointer(location[:-1])
            found[path, UNKNOWN_FIELD] = _error(
                path, UNKNOWN_FIELD, "Cet objet porte un champ que le contrat ne connaît pas."
            )
        else:
            path = json_pointer(location)
            found[path, item["type"]] = _error(path, item["type"], item["msg"])
    return list(found.values())


def _dataset_errors(datasets: list[Any]) -> list[FieldError]:
    """Identifiants mal formés et doublons du périmètre."""
    if not datasets:
        return [
            _error(
                "/scope/datasets",
                "too_few_items",
                "Le périmètre compte au moins un jeu de données.",
            )
        ]
    errors = []
    for index, item in enumerate(datasets):
        for field, bound in (
            ("namespace", identifiers.NAMESPACE_MAX),
            ("name", identifiers.DATASET_NAME_MAX),
        ):
            value = _mapping(item).get(field)
            if isinstance(value, str):
                fault = identifier_fault(value, max_length=bound)
                if fault is not None:
                    errors.append(
                        _error(f"/scope/datasets/{index}/{field}", fault.code, fault.message)
                    )
    if errors:
        return errors
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(datasets):
        namespace, name = _mapping(item).get("namespace"), _mapping(item).get("name")
        if not isinstance(namespace, str) or not isinstance(name, str):
            continue
        key = (normalize_identifier(namespace), normalize_identifier(name))
        if key in seen:
            errors.append(
                _error(
                    f"/scope/datasets/{index}",
                    "dataset_duplicate",
                    "Ce jeu de données figure déjà dans le périmètre.",
                )
            )
        seen.add(key)
    return errors


def _window_errors(window: DeclaredWindow) -> list[FieldError]:
    errors = [
        _error(
            f"/window/{key}",
            "window_out_of_range",
            "Une fenêtre tombe entre le 1er janvier 1970 et l'an 9999.",
        )
        for key, moment in (("from", window.start), ("to", window.end))
        if not WINDOW_EARLIEST <= moment <= WINDOW_LATEST
    ]
    if errors:
        return errors
    if window.end <= window.start:
        return [_error("/window", "window_inverted", "La fin de la fenêtre doit suivre son début.")]
    if window.end - window.start > MAX_WINDOW:
        return [
            _error(
                "/window",
                "window_too_long",
                f"Une fenêtre de validité dure au plus {INCIDENT_WINDOW_DAYS} jours.",
            )
        ]
    return []


def _valid_window(raw: object) -> DeclaredWindow | None:
    try:
        return _WINDOW.validate_python(raw)
    except ValidationError:
        return None


def validate_declaration(raw: object) -> tuple[IncidentDeclaration | None, list[FieldError]]:
    """Valide une déclaration. Renvoie la déclaration, identifiants sous leur forme
    normalisée, ou toutes ses fautes, chacune par son chemin, un code et un motif fixe."""
    if not isinstance(raw, dict):
        return None, [_error("", "not_an_object", "Une déclaration est un objet JSON.")]
    datasets = _mapping(raw.get("scope")).get("datasets")
    if isinstance(datasets, list) and len(datasets) > MAX_DATASETS:
        # Un périmètre trop long n'est pas parcouru ; le motif cite la borne, pas le nombre reçu.
        return None, [
            _error(
                "/scope/datasets",
                "too_many_items",
                f"Le périmètre compte au plus {MAX_DATASETS} jeux de données.",
            )
        ]
    declaration: IncidentDeclaration | None = None
    errors: list[FieldError] = []
    try:
        declaration = _DECLARATION.validate_python(raw)
    except ValidationError as error:
        errors = _schema_errors(error)
    if isinstance(datasets, list):
        errors.extend(_dataset_errors(datasets))
    window = declaration.window if declaration is not None else _valid_window(raw.get("window"))
    if window is not None:
        errors.extend(_window_errors(window))
    if errors or declaration is None:
        return None, errors
    scope = DeclaredScope(
        datasets=[
            DeclaredDataset(
                namespace=normalize_identifier(item.namespace), name=normalize_identifier(item.name)
            )
            for item in declaration.scope.datasets
        ]
    )
    return declaration.model_copy(update={"scope": scope}), []


def validate_quarantine(raw: object) -> tuple[QuarantineRequest | None, list[FieldError]]:
    """Valide une demande de quarantaine : un objet, un mode de la liste, rien d'autre.

    Une faute se dit par son chemin, un code et un motif fixe, jamais par la valeur reçue.
    """
    if not isinstance(raw, dict):
        return None, [_error("", "not_an_object", "Une demande de quarantaine est un objet JSON.")]
    try:
        return _QUARANTINE.validate_python(raw), []
    except ValidationError as error:
        return None, _schema_errors(error)


# Ce que les routes rendent : des identifiants, des heures, des décomptes.


class IncidentDataset(BaseModel):
    """Un jeu de données du périmètre."""

    namespace: str
    name: str


class IncidentScope(BaseModel):
    datasets: list[IncidentDataset] = Field(description="Par espace de noms, puis par nom.")


class Analysis(BaseModel):
    """L'état de l'analyse d'impact de l'incident."""

    state: AnalysisState = Field(
        description=(
            "« pending » : en attente d'un worker, ou remise à plus tard après un échec."
            " « done » : faite, son résultat se lit. « failed » : en échec après plusieurs"
            " essais, « error » dit pourquoi."
        )
    )
    requested_at: AwareDatetime = Field(description="Demande, à la déclaration de l'incident.")
    attempts: int = Field(description="Essais en échec.")
    error: AnalysisError | None = Field(
        description=(
            "Code du dernier échec : « query_timeout », une lecture a dépassé le délai de"
            " l'analyse ; « analysis_error », toute autre erreur. Nul pour une analyse faite."
        )
    )
    known_at: AwareDatetime | None = Field(
        description=(
            "Le graphe est lu tel qu'il était connu à cette heure, fixée au premier essai."
            " Nulle avant lui."
        )
    )
    finished_at: AwareDatetime | None = Field(
        description="Fin de l'analyse, faite ou en échec. Nulle tant qu'elle est en attente."
    )
    counts: ImpactCounts | None = Field(
        description="Réponses touchées et destinataires distincts. Nuls avant l'analyse faite."
    )
    truncated: bool | None = Field(
        description=(
            "Vrai quand la propagation dépasse une de ses bornes : le résultat est partiel."
            " Nul avant l'analyse faite."
        )
    )


def _closed_list(schema: dict[str, Any]) -> None:
    """Une liste fermée s'écrit en énumération, même quand elle ne compte qu'une valeur."""
    if "const" in schema:
        schema["enum"] = [schema.pop("const")]


class Quarantine(BaseModel):
    """Une quarantaine de l'incident : son mode, son état, ses heures, posées par la base."""

    state: QuarantineState = Field(
        description="« active » : les proxys la reçoivent. « lifted » : levée, définitivement."
    )
    mode: QuarantineMode = Field(
        description="Ce que les proxys feront des lectures du périmètre. « warn » : les signaler.",
        json_schema_extra=_closed_list,
    )
    activated_at: AwareDatetime = Field(description="Activation, à l'heure de la base.")
    lifted_at: AwareDatetime | None = Field(
        description="Levée, à l'heure de la base. Nulle tant que la quarantaine est active."
    )


class Incident(BaseModel):
    """Un incident déclaré et l'état de son analyse."""

    id: UUID = Field(description="Identifiant de l'incident, tiré par l'API.")
    state: IncidentState = Field(
        description=(
            "« qualified » : déclaré, son périmètre et sa fenêtre sont établis. La liste est"
            " fermée ; elle s'allongera avec les étapes du rappel."
        ),
        json_schema_extra=_closed_list,
    )
    severity: Severity
    scope: IncidentScope
    window: Window = Field(description="Fenêtre de validité : début inclus, fin exclue.")
    declared_at: AwareDatetime
    analysis: Analysis
    quarantine: Quarantine | None = Field(
        description=(
            "La dernière quarantaine de l'incident, active ou levée. Nulle quand l'incident"
            " n'a jamais été mis en quarantaine."
        )
    )
    proposal_id: UUID | None = Field(
        description=(
            "La proposition née d'un test de qualité en échec dont la confirmation a déclaré"
            " l'incident ; nulle pour un incident déclaré à la main."
        )
    )


class QuarantineChange(BaseModel):
    """L'issue d'une activation ou d'une levée : la quarantaine telle qu'elle est désormais."""

    incident_id: UUID
    changed: bool = Field(
        description=(
            "Vrai quand cette demande a changé l'état de la quarantaine ; faux quand elle était"
            " déjà dans l'état demandé, qui est alors rendu tel quel."
        )
    )
    quarantine: Quarantine


class QuarantinedDataset(BaseModel):
    """Un jeu de données en quarantaine, par son espace de noms et son nom."""

    namespace: str
    name: str


class ActiveQuarantine(BaseModel):
    """Le périmètre d'une quarantaine active : celui de son incident."""

    incident_id: UUID
    mode: QuarantineMode = Field(json_schema_extra=_closed_list)
    activated_at: AwareDatetime
    datasets: list[QuarantinedDataset] = Field(
        description="Par espace de noms, puis par nom.",
        json_schema_extra={"maxItems": MAX_DATASETS},
    )


class ActiveQuarantines(BaseModel):
    """Les périmètres en quarantaine active, tels que les proxys les reçoivent."""

    version: str = Field(
        description=(
            "Empreinte SHA-256 de la liste, en hexadécimal : la même tant que rien ne change."
            " Elle est aussi l'ETag de la réponse : un proxy la rend dans If-None-Match et"
            " reçoit 304, sans corps, quand rien n'a changé."
        ),
        json_schema_extra={"pattern": "^[0-9a-f]{64}$"},
    )
    quarantines: list[ActiveQuarantine] = Field(
        description="Par heure d'activation, puis par incident.",
        json_schema_extra={"maxItems": MAX_ACTIVE_QUARANTINES},
    )


class TouchedAnswer(BaseModel):
    """Une réponse touchée, désignée par son identifiant stable et son empreinte."""

    id: str = Field(description="Identifiant stable de la réponse.")
    fingerprint: str = Field(
        description="Empreinte de la réponse : son contenu n'est pas conservé."
    )
    produced_at: AwareDatetime
    recipients: list[str] = Field(description="Pseudonymes de ses destinataires.")


class AnalysisResult(BaseModel):
    """Ce que l'analyse a trouvé : une page des réponses touchées, et ce qu'elle n'a pas pu voir."""

    unknown_datasets: list[IncidentDataset] = Field(
        description=(
            "Jeux du périmètre que le graphe ne connaissait pas à l'heure de l'analyse :"
            " aucune lecture n'en est connue, ou leur nom est mal écrit."
        )
    )
    coverage: Coverage = Field(
        description=(
            "Couverture du lignage sur la fenêtre de l'incident, tous émetteurs confondus,"
            " à l'heure de l'analyse : une réponse dont l'appel n'a pas été capté manque."
        )
    )
    answers: list[TouchedAnswer] = Field(description="Les plus récentes d'abord.")
    more: bool = Field(description="Vrai quand des réponses plus anciennes suivent.")
    next_cursor: str | None = Field(
        description=(
            "Curseur de la page suivante, à rendre tel quel dans le paramètre « cursor »."
            " Nul quand il n'y a plus de réponse."
        )
    )


class IncidentDetail(Incident):
    """Un incident, l'état de son analyse et, quand elle est faite, son résultat."""

    result: AnalysisResult | None = Field(description="Nul tant que l'analyse n'est pas faite.")


class IncidentList(BaseModel):
    """Les incidents déclarés, les plus récents d'abord."""

    incidents: list[Incident]
    more: bool = Field(description="Vrai quand des incidents plus anciens suivent.")
    next_cursor: str | None = Field(
        description=(
            "Curseur de la page suivante, à rendre tel quel dans le paramètre « cursor »."
            " Nul quand il n'y en a plus."
        )
    )


def in_utc(value: datetime) -> datetime:
    """Une heure lue dans la base, rendue en UTC."""
    return value.astimezone(UTC)


# Les propositions d'incident nées des tests de qualité en échec.


class ProposedWindow(BaseModel):
    """Le début de la fenêtre déduite d'une proposition, et ce qui le fonde."""

    start: AwareDatetime = Field(
        serialization_alias="from",
        description="Début de la fenêtre déduite, inclus : la donnée y est tenue pour juste.",
    )
    basis: WindowBasis = Field(
        description=(
            "« last_pass » : la dernière réussite connue des assertions de la colonne avant le"
            " premier échec, au plus 31 jours avant lui. « lookback » : aucune réussite connue"
            " avant le premier échec ; le début est pris 7 jours avant lui."
        )
    )


class ProposalCounts(BaseModel):
    """Décomptes d'une proposition, lus sur ses signaux."""

    failed_runs: int = Field(description="Runs de test en échec rattachés à la proposition.")
    lower_bound: bool = Field(
        description="Vrai au-delà de la borne : « failed_runs » est un minimum."
    )


class IncidentProposal(BaseModel):
    """Un incident proposé par un test de qualité en échec, en attente d'un humain ou décidé.

    Aucun texte libre : le jeu et ses colonnes par leur nom, contrôlé ; les assertions
    par leur classe, d'une liste fermée ; des heures.
    """

    id: UUID = Field(description="Identifiant de la proposition, tiré par la base.")
    state: ProposalState = Field(
        description=(
            "« signaled » : signalée, en attente d'un humain ; elle s'enrichit des échecs"
            " suivants. « confirmed » : confirmée, l'incident est déclaré (« incident_id »)."
            " « dismissed » : écartée. « expired » : expirée sans nouvel échec. Les trois"
            " dernières sont définitives."
        )
    )
    dataset: IncidentDataset = Field(description="Le jeu de données dont un test échoue.")
    whole_table: bool = Field(
        description=(
            "Vrai quand un échec porte sur la table entière : une assertion sans colonne, une"
            f" colonne qui n'a pas la forme d'un nom, ou plus de {MAX_PROPOSAL_COLUMNS}"
            " colonnes en échec."
        )
    )
    columns: list[str] = Field(
        description="Les colonnes en échec, nommées, dans l'ordre de leur nom.",
        json_schema_extra={"maxItems": MAX_PROPOSAL_COLUMNS},
    )
    assertions: list[AssertionClass] = Field(
        description="Les classes des assertions en échec, d'une liste fermée.",
        json_schema_extra={"minItems": 1, "maxItems": MAX_SIGNAL_CLASSES},
    )
    severity: QualitySeverity = Field(
        description=(
            "La plus forte gravité réglée par l'émetteur sur les assertions en échec :"
            " « error », « warn », ou « unspecified » quand il n'en dit rien de connu."
            " Ce n'est pas la gravité de l'incident, que donne l'humain qui le confirme."
        )
    )
    emitter: str = Field(description="L'émetteur du premier échec : l'espace de noms de son job.")
    first_failed_at: AwareDatetime = Field(
        description="Le premier échec, à l'heure de l'événement."
    )
    last_failed_at: AwareDatetime = Field(description="Le dernier échec, à l'heure de l'événement.")
    resolved_at: AwareDatetime | None = Field(
        description=(
            "Quand toutes les colonnes en échec sont de nouveau en réussite : la plus récente"
            " de leurs réussites. Nulle tant que l'une échoue encore, ou que les colonnes en"
            " échec ne sont pas toutes connues."
        )
    )
    window: ProposedWindow
    proposed_at: AwareDatetime = Field(description="Proposition, à l'heure de la base.")
    decided_at: AwareDatetime | None = Field(
        description="Confirmation, écartement ou expiration ; nulle tant qu'elle est signalée."
    )
    incident_id: UUID | None = Field(
        description="L'incident déclaré à la confirmation ; nul sinon."
    )


class IncidentProposalDetail(IncidentProposal):
    """Une proposition et ses décomptes."""

    counts: ProposalCounts


class ProposalConfirmation(BaseModel):
    """Ce que demande la confirmation d'une proposition : la gravité de l'incident, et sa
    fenêtre quand la fenêtre déduite ne convient pas. Aucun texte libre."""

    model_config = ConfigDict(extra="forbid")

    severity: Severity = Field(
        description=(
            "Gravité de l'incident, de « low » à « critical » : l'appréciation de l'humain qui"
            " confirme, pas celle du test."
        )
    )
    window: DeclaredWindow | None = Field(
        default=None,
        description=(
            "Fenêtre de validité de l'incident, aux règles de la déclaration. Par défaut, la"
            " fenêtre déduite : de son début à la fin de l'échec, ou à la confirmation."
        ),
    )


class ProposalDecision(BaseModel):
    """L'issue d'une confirmation ou d'un écartement : la proposition telle qu'elle est
    désormais."""

    proposal_id: UUID
    changed: bool = Field(
        description=(
            "Vrai quand cette demande a décidé de la proposition ; faux quand elle l'était déjà"
            " ainsi, et elle est alors rendue telle quelle."
        )
    )
    proposal: IncidentProposal


_CONFIRMATION: TypeAdapter[ProposalConfirmation] = TypeAdapter(ProposalConfirmation)


def confirmation_json_schema() -> dict[str, Any]:
    """Schéma JSON d'une confirmation, pour la spécification OpenAPI."""
    return ProposalConfirmation.model_json_schema(
        by_alias=True, ref_template="#/components/schemas/{model}"
    )


def validate_confirmation(raw: object) -> tuple[ProposalConfirmation | None, list[FieldError]]:
    """Valide une confirmation : un objet, une gravité de la liste, au besoin une fenêtre aux
    règles de la déclaration, rien d'autre. Une faute se dit par son chemin, un code et un
    motif fixe, jamais par la valeur reçue."""
    if not isinstance(raw, dict):
        return None, [_error("", "not_an_object", "Une confirmation est un objet JSON.")]
    confirmation: ProposalConfirmation | None = None
    errors: list[FieldError] = []
    try:
        confirmation = _CONFIRMATION.validate_python(raw)
    except ValidationError as error:
        errors = _schema_errors(error)
    window = (
        confirmation.window
        if confirmation is not None
        else _valid_window(raw.get("window"))
        if raw.get("window") is not None
        else None
    )
    if window is not None:
        errors.extend(_window_errors(window))
    if errors or confirmation is None:
        return None, errors
    return confirmation, []


class IncidentProposalList(BaseModel):
    """Les propositions d'incident, les plus récentes d'abord."""

    proposals: list[IncidentProposal]
    more: bool = Field(description="Vrai quand des propositions plus anciennes suivent.")
    next_cursor: str | None = Field(
        description=(
            "Curseur de la page suivante, à rendre tel quel dans le paramètre « cursor »."
            " Nul quand il n'y en a plus."
        )
    )
