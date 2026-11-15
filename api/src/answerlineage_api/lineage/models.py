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


class DatasetNode(BaseModel):
    id: str = Field(description="Identifiant du nœud, valable dans cette réponse seulement.")
    namespace: str
    name: str
    kind: str


class ToolNode(BaseModel):
    id: str = Field(description="Identifiant du nœud, valable dans cette réponse seulement.")
    server: str
    name: str


class AgentNode(BaseModel):
    id: str = Field(description="Identifiant du nœud, valable dans cette réponse seulement.")
    namespace: str
    name: str


class RecipientNode(BaseModel):
    id: str = Field(description="Identifiant du nœud, valable dans cette réponse seulement.")
    pseudonym: str = Field(description="Pseudonyme du destinataire, jamais son identité.")


class AnswerRead(BaseModel):
    """Une lecture faite pour produire la réponse."""

    dataset: str = Field(description="Nœud du jeu de données lu.")
    tool: str | None = Field(description="Nœud de l'outil qui a lu, quand un outil a lu.")


class AnswerNode(BaseModel):
    """Une réponse et sa chaîne, d'une colonne à l'autre."""

    id: str = Field(description="Identifiant stable de la réponse.")
    fingerprint: str
    produced_at: AwareDatetime
    agent: str | None = Field(description="Nœud de l'agent qui a répondu, quand il est connu.")
    channels: list[str] = Field(description="Canaux par lesquels la réponse a été diffusée.")
    recipients: list[str] = Field(description="Nœuds des destinataires.")
    tools: list[str] = Field(description="Nœuds des outils appelés.")
    reads: list[AnswerRead]


class GraphView(BaseModel):
    """Le graphe en cinq colonnes : données, outils, agents, réponses, destinataires."""

    datasets: list[DatasetNode]
    tools: list[ToolNode]
    agents: list[AgentNode]
    answers: list[AnswerNode] = Field(description="Les plus récentes d'abord.")
    recipients: list[RecipientNode]
    more: bool = Field(description="Vrai quand des réponses plus anciennes existent.")
    truncated: bool = Field(
        description="Vrai quand les arbres de runs dépassent la borne : la vue est partielle."
    )


class DatasetName(BaseModel):
    namespace: str
    name: str
    kind: str


class Window(BaseModel):
    """Fenêtre de l'incident : début inclus, fin exclue."""

    start: AwareDatetime = Field(serialization_alias="from")
    end: AwareDatetime = Field(serialization_alias="to")


class ImpactCounts(BaseModel):
    """Décomptes sur tout l'incident, quelle que soit la page demandée."""

    answers: int = Field(description="Réponses touchées.")
    recipients: int = Field(description="Destinataires distincts de ces réponses.")


class Coverage(BaseModel):
    """Ce que l'on sait de la part des appels captés pendant la fenêtre."""

    status: Literal["unknown"] = Field(
        description="« unknown » : la couverture du lignage n'est pas encore mesurée."
    )


class ImpactResult(BaseModel):
    """Les réponses et les destinataires touchés par un jeu de données faux pendant une fenêtre."""

    dataset: DatasetName
    window: Window
    counts: ImpactCounts
    coverage: Coverage
    graph: GraphView = Field(
        description="Les réponses touchées, les plus récentes d'abord, avec leur chaîne."
    )
    truncated: bool = Field(
        description="Vrai quand la propagation dépasse la borne : le résultat est partiel."
    )


class DatasetList(BaseModel):
    """Les jeux de données connus du graphe, par espace de noms puis par nom."""

    datasets: list[DatasetName]
    more: bool = Field(description="Vrai quand la liste dépasse la borne demandée.")
