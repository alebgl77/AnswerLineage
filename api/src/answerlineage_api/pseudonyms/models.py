"""La demande de révélation d'un destinataire, et ce que rendent les routes.

Une demande porte le pseudonyme visé, un motif d'une liste fermée et des identités
candidates, bornées en nombre et en longueur. Aucune faute ne cite la valeur reçue :
une identité candidate n'est jamais recopiée, ni dans une réponse, ni dans la base,
ni dans un journal.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from answerlineage_api.ingest.models import TOO_MANY_ITEMS, FieldError, json_pointer
from answerlineage_api.pseudonyms.vault import (
    IDENTITY_MAX,
    PSEUDONYM_LENGTH,
    PSEUDONYM_PATTERN,
    canonical_identity,
    is_pseudonym,
)
from answerlineage_api.store.schema import MAX_REVEAL_CANDIDATES, RevealReason, RevealResult

# Le corps d'une demande : cent candidates de 320 caractères, même échappés en JSON.
MAX_REVELATION_BYTES: Final = 256 * 1024
UNKNOWN_FIELD: Final = "unknown_field"
PSEUDONYM_MALFORMED: Final = "pseudonym_malformed"
CANDIDATE_NOT_IDENTITY: Final = "candidate_not_identity"
NO_CANDIDATE: Final = "no_candidate"


class RevelationRequest(BaseModel):
    """Révéler un destinataire : le pseudonyme visé, le motif, les identités candidates.

    Aucun texte libre : le motif vient d'une liste fermée, et les candidates ne sont
    gardées nulle part."""

    model_config = ConfigDict(extra="forbid")

    pseudonym: str = Field(
        description=(
            "Pseudonyme du destinataire, tel que l'API le donne : « hmac-sha256: » suivi de 64"
            " chiffres hexadécimaux minuscules."
        ),
        json_schema_extra={
            "pattern": PSEUDONYM_PATTERN,
            "minLength": PSEUDONYM_LENGTH,
            "maxLength": PSEUDONYM_LENGTH,
        },
    )
    reason: RevealReason = Field(
        description=(
            "Motif, d'une liste fermée : « recall », prévenir le destinataire d'un rappel ;"
            " « data_subject_request », répondre à une demande de la personne concernée ;"
            " « legal_obligation », répondre à une obligation légale ;"
            " « incident_investigation », instruire un incident."
        )
    )
    candidates: list[str] = Field(
        description=(
            "Identités candidates, tirées de l'annuaire ou du canal de la diffusion : adresse,"
            " identifiant de messagerie, numéro. Ni stockées, ni renvoyées, ni journalisées."
        ),
        json_schema_extra={
            "minItems": 1,
            "maxItems": MAX_REVEAL_CANDIDATES,
            "items": {"type": "string", "minLength": 1, "maxLength": IDENTITY_MAX},
        },
    )


class Revelation(BaseModel):
    """Ce qu'a donné une révélation, journalisée sous ``revelation``."""

    revelation: int = Field(description="Numéro de la révélation dans le journal.")
    result: Literal["matched", "no_match"] = Field(
        description=("« matched » : une candidate a ce pseudonyme ; « no_match » : aucune.")
    )
    candidate: int | None = Field(
        description=(
            "Rang, à partir de zéro, de la première candidate qui a ce pseudonyme ; nul sans"
            " correspondance. L'identité elle-même n'est jamais renvoyée."
        )
    )


class RevelationAuthor(BaseModel):
    """L'auteur d'une révélation : son identité OIDC."""

    issuer: str = Field(description="Émetteur OIDC qui a signé le jeton de l'auteur.")
    subject: str = Field(description="Sujet du jeton de l'auteur (« sub »).")


class RevelationEntry(BaseModel):
    """Une révélation du journal : qui, quand, pourquoi, quel pseudonyme, quel résultat."""

    id: int
    revealed_at: datetime = Field(description="Heure de la révélation, posée par la base.")
    author: RevelationAuthor
    reason: RevealReason
    pseudonym: str
    candidates: int = Field(description="Nombre d'identités candidates, jamais les candidates.")
    result: RevealResult = Field(
        description=(
            "« matched », « no_match », ou « unknown_recipient » : aucun destinataire connu ne"
            " porte ce pseudonyme."
        )
    )


class RevelationJournal(BaseModel):
    """Une page du journal des révélations, la plus récente d'abord."""

    revelations: list[RevelationEntry]
    next_cursor: str | None = Field(
        description="Curseur de la page suivante ; nul quand il n'y en a plus."
    )


_REQUEST: TypeAdapter[RevelationRequest] = TypeAdapter(RevelationRequest)


def revelation_request_json_schema() -> dict[str, Any]:
    """Schéma JSON de la demande, pour la spécification OpenAPI."""
    return RevelationRequest.model_json_schema(
        by_alias=True, ref_template="#/components/schemas/{model}"
    )


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


def validate_revelation(raw: object) -> tuple[RevelationRequest | None, list[FieldError]]:
    """Valide une demande de révélation. Une faute se dit par son chemin, un code et un motif
    fixe, jamais par la valeur reçue."""
    if not isinstance(raw, dict):
        return None, [_error("", "not_an_object", "Une demande de révélation est un objet JSON.")]
    listed = raw.get("candidates")
    if isinstance(listed, list) and len(listed) > MAX_REVEAL_CANDIDATES:
        # Une liste trop longue n'est pas parcourue.
        return None, [
            _error(
                "/candidates",
                TOO_MANY_ITEMS,
                f"Ce champ compte au plus {MAX_REVEAL_CANDIDATES} éléments.",
            )
        ]
    try:
        asked = _REQUEST.validate_python(raw)
    except ValidationError as error:
        return None, _schema_errors(error)
    errors = []
    if not is_pseudonym(asked.pseudonym):
        errors.append(
            _error(
                "/pseudonym",
                PSEUDONYM_MALFORMED,
                "Un pseudonyme s'écrit « hmac-sha256: » suivi de 64 chiffres hexadécimaux"
                " minuscules.",
            )
        )
    if not asked.candidates:
        errors.append(_error("/candidates", NO_CANDIDATE, "Une révélation porte une candidate."))
    errors.extend(
        _error(
            f"/candidates/{rank}",
            CANDIDATE_NOT_IDENTITY,
            f"Une identité candidate compte de 1 à {IDENTITY_MAX} caractères, sans blanc,"
            " séparateur, caractère de contrôle ni de formatage.",
        )
        for rank, candidate in enumerate(asked.candidates)
        if canonical_identity(candidate) is None
    )
    if errors:
        return None, errors
    return asked, []
