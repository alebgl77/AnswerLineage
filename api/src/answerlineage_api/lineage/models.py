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
        description=(
            "Vrai quand l'arbre des runs ou ses lectures dépassent leur borne :"
            " les sources sont partielles."
        )
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
    next_cursor: str | None = Field(
        description=(
            "Curseur de la page suivante, à rendre tel quel dans le paramètre « cursor »."
            " Nul quand il n'y a plus de réponse."
        )
    )
    truncated: bool = Field(
        description=(
            "Vrai quand les chaînes des réponses dépassent une borne (runs, lectures,"
            " destinataires) : la vue est partielle."
        )
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
    lower_bound: bool = Field(
        description=(
            "Vrai quand le résultat est tronqué : les décomptes sont des minimums,"
            " et zéro ne veut pas dire que rien n'est touché."
        )
    )


CoverageStatus = Literal["measured", "partial", "unknown"]
HoleKind = Literal[
    "events_lost", "uninspected", "missing_from_graph", "silence", "before_first_report"
]


class CoverageHole(BaseModel):
    """Un trou connu du lignage : une fenêtre où des appels ont pu échapper au graphe."""

    emitter: str = Field(description="Émetteur concerné, l'espace de noms de ses jobs.")
    kind: HoleKind = Field(
        description=(
            "« events_lost » : l'émetteur déclare des événements perdus."
            " « uninspected » : il a relayé des requêtes sans pouvoir les inspecter, qui cachent"
            " peut-être des appels, en nombre inconnu."
            " « missing_from_graph » : le graphe contient moins d'événements que d'appels vus."
            " « silence » : aucun relevé, entre deux relevés ou après le dernier."
            " « before_first_report » : la période commence avant le premier relevé de l'émetteur."
        )
    )
    window: Window
    count: int | None = Field(
        description=(
            "Événements perdus ou manquants, requêtes non inspectées ;"
            " nul pour un silence, où rien n'est compté."
        )
    )


class Coverage(BaseModel):
    """Part des appels relayés présents dans le graphe, et trous connus, sur une période."""

    status: CoverageStatus = Field(
        description=(
            "« measured » : toute la période est couverte par des relevés."
            " « partial » : une partie seulement, les trous disent laquelle manque."
            " « unknown » : aucun relevé, la couverture n'est pas mesurée."
        )
    )
    rate: float | None = Field(
        description=(
            "Part des appels vus présents dans le graphe, de 0 à 1 : dans chaque fenêtre, les"
            " événements comptés ne dépassent pas les appels vus. Nulle quand aucun appel n'a"
            " été vu."
        )
    )
    calls_seen: int = Field(description="Appels vus par les émetteurs, sur leurs relevés.")
    events_in_graph: int = Field(description="Événements de ces appels présents dans le graphe.")
    events_lost: int = Field(description="Événements déclarés perdus par les émetteurs.")
    uninspected: int = Field(description="Requêtes relayées sans pouvoir être inspectées.")
    holes: list[CoverageHole] = Field(description="Trous connus, par heure de début.")
    truncated: bool = Field(
        description=(
            "Vrai quand les relevés ou les trous dépassent leur borne : la mesure est partielle,"
            " et elle n'est jamais donnée pour complète."
        )
    )


class ImpactResult(BaseModel):
    """Les réponses et les destinataires touchés par un jeu de données faux pendant une fenêtre."""

    dataset: DatasetName
    window: Window
    counts: ImpactCounts
    coverage: Coverage = Field(
        description=(
            "Couverture du lignage sur la fenêtre de l'incident, tous émetteurs confondus :"
            " une réponse dont l'appel n'a pas été capté ne peut pas être trouvée."
        )
    )
    graph: GraphView = Field(
        description="Les réponses touchées, les plus récentes d'abord, avec leur chaîne."
    )
    truncated: bool = Field(
        description=(
            "Vrai quand la propagation dépasse une borne : le résultat est partiel, d'autres"
            " réponses peuvent être touchées, et les décomptes sont des minimums."
        )
    )


class EmitterCoverage(BaseModel):
    """Un émetteur connu et sa couverture sur la période."""

    emitter: str = Field(description="Identifiant de l'émetteur, l'espace de noms de ses jobs.")
    last_report_at: AwareDatetime = Field(
        description="Réception du dernier relevé qui a changé quelque chose."
    )
    muted_since: AwareDatetime | None = Field(
        description=(
            "Signalé muet par le contrôle des émetteurs : aucun relevé depuis cette heure."
            " Nul quand l'émetteur publie."
        )
    )
    coverage: Coverage


class CoverageList(BaseModel):
    """La couverture du lignage par émetteur, sur une période."""

    window: Window
    emitters: list[EmitterCoverage] = Field(description="Par identifiant d'émetteur.")
    more: bool = Field(description="Vrai quand d'autres émetteurs suivent la borne demandée.")


class DatasetList(BaseModel):
    """Les jeux de données connus du graphe, par espace de noms puis par nom."""

    datasets: list[DatasetName]
    more: bool = Field(description="Vrai quand la liste dépasse la borne demandée.")
