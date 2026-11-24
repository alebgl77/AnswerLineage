"""Une liste de rappel, la décision qu'on prend sur elle, et ce que rendent les routes.

Des pseudonymes, des identifiants, des codes de listes fermées et des nombres : jamais une
identité ni un texte. Décider, c'est choisir dans des listes fermées : valider, modifier en
choisissant un autre gabarit, ou refuser pour un motif. Aucun champ n'accueille un texte
libre, qui pourrait finir dans un message de rappel. Un refus donne le chemin du champ
fautif, un code stable et un motif fixe, jamais la valeur reçue ni le nom d'un champ inconnu.
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from answerlineage_api.ingest.models import FieldError, json_pointer
from answerlineage_api.store.schema import (
    DEFAULT_RECALL_TEMPLATE,
    RecallRefusal,
    RecallTemplate,
    Severity,
)

# Réponses rendues au plus par entrée, les plus récentes d'abord : un destinataire peut en
# avoir reçu des milliers, et ``answer_count`` les compte toutes.
MAX_ENTRY_ANSWERS: Final = 100
# Une décision ne porte que trois codes : quelques octets.
MAX_DECISION_BYTES: Final = 4 * 1024
UNKNOWN_FIELD: Final = "unknown_field"
UNEXPECTED_FIELD: Final = "unexpected_field"
MISSING: Final = "missing"

# La décision que prend un humain sur une liste ; « escalated », la remontée, n'en est pas une.
Decided = Literal["validated", "modified", "refused"]
# L'état d'une liste : sans décision, puis décidée, définitivement.
RecallState = Literal["pending", "validated", "modified", "refused"]

_TEMPLATES = (
    " « standard » : la réponse s'appuyait sur une donnée reconnue fausse, ne plus s'y fier ;"
    " « information » : elle a pu s'appuyer sur une donnée reconnue fausse, pour information,"
    " sans action attendue ; « decision_review » : si elle a fondé une décision, la revoir."
)
_REFUSALS = (
    " « not_affected » : les réponses n'ont pas repris la donnée fausse ; « already_informed » :"
    " les destinataires sont déjà prévenus par un autre moyen ; « no_impact » : l'écart est"
    " sans effet sur ce qui leur a été dit ; « duplicate_incident » : un autre incident les"
    " rappelle déjà."
)


class RecallDecisionRequest(BaseModel):
    """Ce que décide un responsable d'agent sur une liste de rappel. Aucun texte libre."""

    model_config = ConfigDict(extra="forbid")

    decision: Decided = Field(
        description=(
            "« validated » : la liste part telle quelle, sous le gabarit standard."
            " « modified » : elle part sous le gabarit choisi (« template »). « refused » :"
            " rien ne part, pour le motif donné (« reason »). Une décision est définitive."
        )
    )
    template: RecallTemplate | None = Field(
        default=None,
        description="Le gabarit du message, d'une liste fermée, pour une liste modifiée seulement."
        + _TEMPLATES,
    )
    reason: RecallRefusal | None = Field(
        default=None,
        description="Le motif, d'une liste fermée, pour une liste refusée seulement." + _REFUSALS,
    )

    @property
    def chosen_template(self) -> str | None:
        """Le gabarit de ce qui partira : le standard pour une liste validée, le choisi pour
        une liste modifiée, aucun pour une liste refusée."""
        if self.decision == "validated":
            return DEFAULT_RECALL_TEMPLATE
        return self.template


class RecallAnswer(BaseModel):
    """Une réponse touchée qu'un destinataire a reçue."""

    id: str = Field(description="Identifiant stable de la réponse.")
    fingerprint: str = Field(
        description="Empreinte de la réponse : son contenu n'est pas conservé."
    )
    produced_at: AwareDatetime


class RecallEntry(BaseModel):
    """Un destinataire de la liste, une fois par incident, avec ses réponses touchées."""

    rank: int = Field(description="Rang dans la liste, à partir de 1 : le plus exposé d'abord.")
    recipient: str = Field(description="Pseudonyme du destinataire, jamais son identité.")
    exposure: float = Field(
        description=(
            "Exposition du destinataire, au millionième : chaque réponse touchée qu'il a reçue"
            " pèse 1, divisé par 2 tous les 30 jours depuis l'heure de la réponse, à l'heure de"
            " référence de la liste."
        )
    )
    answer_count: int = Field(description="Réponses touchées qu'il a reçues, toutes comptées.")
    latest_answer_at: AwareDatetime = Field(description="Heure de la plus récente d'entre elles.")
    answers: list[RecallAnswer] = Field(
        description=(
            f"Ses réponses touchées, les plus récentes d'abord, {MAX_ENTRY_ANSWERS} au plus."
        ),
        json_schema_extra={"maxItems": MAX_ENTRY_ANSWERS},
    )
    more_answers: bool = Field(
        description=f"Vrai quand il en a reçu plus que les {MAX_ENTRY_ANSWERS} rendues."
    )


class RecallCounts(BaseModel):
    """Les décomptes de toute la liste."""

    recipients: int = Field(description="Destinataires de la liste : une entrée chacun.")
    answers: int = Field(description="Réponses touchées distinctes qu'ils ont reçues.")


class RecallAuthor(BaseModel):
    """L'auteur d'une décision : son identité OIDC."""

    issuer: str = Field(description="Émetteur OIDC qui a signé le jeton de l'auteur.")
    subject: str = Field(description="Sujet du jeton de l'auteur (« sub »).")


class RecallDecisionRecord(BaseModel):
    """La décision prise sur la liste, telle que le journal la garde."""

    decision: Decided
    decided_at: AwareDatetime = Field(description="Heure de la décision, posée par la base.")
    author: RecallAuthor
    template: RecallTemplate | None = Field(
        description="Le gabarit de ce qui partira ; nul pour une liste refusée."
    )
    reason: RecallRefusal | None = Field(description="Le motif d'un refus ; nul sinon.")


State = Annotated[
    RecallState,
    Field(
        description=(
            "« pending » : sans décision, rien ne peut partir. « validated », « modified » :"
            " décidée, l'envoi est possible. « refused » : décidée, rien ne partira."
        )
    ),
]
Decision = Annotated[
    RecallDecisionRecord | None,
    Field(description="La décision, avec son auteur et son heure ; nulle tant qu'elle attend."),
]
EscalatedAt = Annotated[
    AwareDatetime | None,
    Field(
        description=(
            "Heure à laquelle la liste, restée sans décision au-delà du délai des workers, est"
            " remontée au responsable de données ; nulle sinon."
        )
    ),
]
Dispatched = Annotated[
    int,
    Field(
        description=(
            "Messages de rappel mis à l'envoi. Aucun tant que la liste n'est pas validée ou"
            " modifiée : la base le refuse."
        )
    ),
]


class RecallSummary(BaseModel):
    """Une liste de rappel, son état et ses décomptes, sans ses entrées."""

    incident_id: UUID
    severity: Severity = Field(description="Gravité de l'incident.")
    built_at: AwareDatetime = Field(
        description="Heure de la transaction qui a fait la liste avec l'analyse."
    )
    counts: RecallCounts
    state: State
    decision: Decision
    escalated_at: EscalatedAt
    dispatched: Dispatched


class RecallList(RecallSummary):
    """La liste de rappel d'un incident : un message par destinataire, les plus exposés
    d'abord, son état, et une page de ses entrées."""

    reference_at: AwareDatetime = Field(
        description=(
            "Heure de référence de l'exposition : celle du graphe lu par l'analyse (« known_at »)."
        )
    )
    entries: list[RecallEntry] = Field(description="Par rang.")
    more: bool = Field(description="Vrai quand des entrées moins exposées suivent.")
    next_cursor: str | None = Field(
        description=(
            "Curseur de la page suivante, à rendre tel quel dans le paramètre « cursor »."
            " Nul quand il n'y en a plus."
        )
    )


class RecallLists(BaseModel):
    """Les listes de rappel, de l'incident le plus grave au moins grave, la plus ancienne
    d'abord."""

    lists: list[RecallSummary]
    more: bool = Field(description="Vrai quand d'autres listes suivent.")
    next_cursor: str | None = Field(
        description=(
            "Curseur de la page suivante, à rendre tel quel dans le paramètre « cursor »."
            " Nul quand il n'y en a plus."
        )
    )


class RecallDecisionChange(BaseModel):
    """L'issue d'une décision : la liste telle qu'elle est désormais."""

    incident_id: UUID
    changed: bool = Field(
        description=(
            "Vrai quand cette demande a décidé de la liste ; faux quand elle l'était déjà ainsi,"
            " et elle est alors rendue telle quelle."
        )
    )
    state: State
    decision: Decision
    escalated_at: EscalatedAt
    dispatched: Dispatched


_REQUEST: TypeAdapter[RecallDecisionRequest] = TypeAdapter(RecallDecisionRequest)


def decision_request_json_schema() -> dict[str, Any]:
    """Schéma JSON d'une décision, pour la spécification OpenAPI."""
    return RecallDecisionRequest.model_json_schema(ref_template="#/components/schemas/{model}")


def _error(path: str, kind: str, message: str) -> FieldError:
    return FieldError(path=path, type=kind, message=message)


def _schema_errors(error: ValidationError) -> list[FieldError]:
    """Les fautes de forme, sans valeur reçue. Un champ inconnu désigne l'objet qui le porte :
    son nom vient du demandeur et n'est pas cité."""
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


def validate_decision(raw: object) -> tuple[RecallDecisionRequest | None, list[FieldError]]:
    """Valide une décision : un objet, une décision de la liste, le gabarit d'une liste
    modifiée et le motif d'une liste refusée, rien d'autre.

    Une faute se dit par son chemin, un code et un motif fixe, jamais par la valeur reçue.
    """
    if not isinstance(raw, dict):
        return None, [_error("", "not_an_object", "Une décision est un objet JSON.")]
    try:
        asked = _REQUEST.validate_python(raw)
    except ValidationError as error:
        return None, _schema_errors(error)
    errors = []
    if asked.decision == "modified" and asked.template is None:
        errors.append(_error("/template", MISSING, "Une liste modifiée choisit son gabarit."))
    if asked.decision != "modified" and asked.template is not None:
        errors.append(
            _error(
                "/template",
                UNEXPECTED_FIELD,
                "Seule une liste modifiée choisit son gabarit : une liste validée part sous le"
                " gabarit standard, une liste refusée ne part pas.",
            )
        )
    if asked.decision == "refused" and asked.reason is None:
        errors.append(_error("/reason", MISSING, "Une liste refusée donne son motif."))
    if asked.decision != "refused" and asked.reason is not None:
        errors.append(
            _error("/reason", UNEXPECTED_FIELD, "Seule une liste refusée donne un motif.")
        )
    if errors:
        return None, errors
    return asked, []
