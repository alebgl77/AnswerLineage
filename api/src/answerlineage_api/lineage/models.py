"""Ce que les routes de lecture renvoient : des noms, des empreintes, des heures.

Aucun de ces modèles ne porte de contenu : ni prompt, ni résultat d'outil, ni
réponse.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field

Precision = Literal["table", "columns", "subset", "keys"]


class AgentName(BaseModel):
    namespace: str
    name: str


class AnswerSummary(BaseModel):
    """Une réponse d'agent, désignée par son empreinte."""

    id: str = Field(description="Identifiant stable de la réponse.")
    fingerprint: str = Field(
        description="Empreinte de la réponse : son contenu n'est pas conservé."
    )
    produced_at: AwareDatetime
    run_id: UUID = Field(description="Run de l'agent qui a produit la réponse.")
    agent: AgentName | None = Field(description="Agent du run, quand il est connu.")


class AnswerList(BaseModel):
    answers: list[AnswerSummary]


class ToolUse(BaseModel):
    server: str
    name: str
    calls: int = Field(description="Appels de cet outil faits pour produire la réponse.")


class DatasetSource(BaseModel):
    """Un jeu de données lu pour produire la réponse."""

    namespace: str
    name: str
    kind: str
    versions: list[str] = Field(description="Versions lues, quand l'émetteur les a déclarées.")
    precision: Precision = Field(
        description="Ce que l'on sait de la lecture. « table » : le jeu entier est une source."
    )
    columns: list[str] = Field(description="Colonnes lues, quand toutes les lectures les donnent.")
    first_read_at: AwareDatetime
    last_read_at: AwareDatetime


class Sources(BaseModel):
    """Les sources d'une réponse : ce que son run et ses runs enfants ont lu."""

    answer: AnswerSummary
    agents: list[AgentName]
    tools: list[ToolUse]
    datasets: list[DatasetSource]
    truncated: bool = Field(
        description="Vrai quand l'arbre des runs dépasse la borne : les sources sont partielles."
    )
