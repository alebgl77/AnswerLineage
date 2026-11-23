"""Ce que rend la lecture d'une liste de rappel : des pseudonymes, des identifiants et des
nombres, jamais une identité ni un texte."""

from __future__ import annotations

from typing import Final
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field

from answerlineage_api.store.schema import Severity

# Réponses rendues au plus par entrée, les plus récentes d'abord : un destinataire peut en
# avoir reçu des milliers, et ``answer_count`` les compte toutes.
MAX_ENTRY_ANSWERS: Final = 100


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


class RecallList(BaseModel):
    """La liste de rappel d'un incident : un message par destinataire, les plus exposés
    d'abord, et une page de ses entrées."""

    incident_id: UUID
    severity: Severity = Field(description="Gravité de l'incident.")
    built_at: AwareDatetime = Field(
        description="Heure de la transaction qui a fait la liste avec l'analyse."
    )
    reference_at: AwareDatetime = Field(
        description=(
            "Heure de référence de l'exposition : celle du graphe lu par l'analyse (« known_at »)."
        )
    )
    counts: RecallCounts
    entries: list[RecallEntry] = Field(description="Par rang.")
    more: bool = Field(description="Vrai quand des entrées moins exposées suivent.")
    next_cursor: str | None = Field(
        description=(
            "Curseur de la page suivante, à rendre tel quel dans le paramètre « cursor »."
            " Nul quand il n'y en a plus."
        )
    )
