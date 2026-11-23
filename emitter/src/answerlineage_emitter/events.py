"""Les briques des événements OpenLineage d'un émetteur : le RunEvent et ses facettes.

Un événement ne porte aucun contenu : ni argument, ni résultat, ni en-tête, ni
jeton. Chaque facette propre se réclame d'un schéma publié dans
``spec/facets/``, à une adresse qui ne change plus ; chaque émetteur y met ce
qu'il sait de l'appel, et se nomme comme producteur.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Final, Literal
from uuid import UUID

from answerlineage_emitter import __version__

# OTHER porte des métadonnées sur un run sans changer son état : la sortie d'un run clos.
EventType = Literal["START", "COMPLETE", "FAIL", "ABORT", "OTHER"]

RUN_EVENT_SCHEMA: Final = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
_OWN: Final = "https://raw.githubusercontent.com/alebgl77/AnswerLineage/main/spec/facets/"
_STANDARD: Final = "https://openlineage.io/spec/facets/"

TOOL_CALL_FACET: Final = "answerlineage_toolCall"
# Schéma publié de la facette (spec/facets/1-0-0/). La version 1-1-0 ajoute ce qu'est
# devenue la clé lue d'après une règle de lecture, la version 1-2-0 si la requête SQL
# relayée a reçu le commentaire qui porte l'identifiant de run : une facette ne s'en
# réclame que lorsqu'elle le dit.
TOOL_CALL_FACET_SCHEMA: Final = _OWN + "1-0-0/ToolCallRunFacet.json#/$defs/ToolCallRunFacet"
TOOL_CALL_FACET_SCHEMA_READ_KEYS: Final = (
    _OWN + "1-1-0/ToolCallRunFacet.json#/$defs/ToolCallRunFacet"
)
TOOL_CALL_FACET_SCHEMA_RUN_ID_TAG: Final = (
    _OWN + "1-2-0/ToolCallRunFacet.json#/$defs/ToolCallRunFacet"
)
# Clés lues dans un jeu de données d'entrée, sous leurs empreintes à clé (spec/facets/1-0-0/).
KEYS_FACET: Final = "answerlineage_keys"
KEYS_FACET_SCHEMA: Final = _OWN + "1-0-0/KeysInputDatasetFacet.json#/$defs/KeysInputDatasetFacet"
# Qualité de service d'un run : latence, latence ajoutée, issue, reprises (spec/facets/1-1-0/).
QOS_FACET: Final = "answerlineage_qos"
QOS_FACET_SCHEMA: Final = _OWN + "1-1-0/QosRunFacet.json#/$defs/QosRunFacet"
# L'agent qui exécute un run : le graphe reconnaît un run d'agent à cette facette.
AGENT_FACET: Final = "answerlineage_agent"
AGENT_FACET_SCHEMA: Final = _OWN + "1-0-0/AgentRunFacet.json#/$defs/AgentRunFacet"
# La diffusion d'une réponse, sur la sortie du run de l'agent qui l'a produite. La version
# 1-1-0 écrit la forme du pseudonyme d'un destinataire, que l'ingestion exige.
DELIVERY_FACET: Final = "answerlineage_delivery"
DELIVERY_FACET_SCHEMA: Final = (
    _OWN + "1-1-0/DeliveryOutputDatasetFacet.json#/$defs/DeliveryOutputDatasetFacet"
)
# Facette standard : le run parent, celui de l'agent pour un appel d'outil ou un sous-agent.
PARENT_FACET: Final = "parent"
PARENT_FACET_SCHEMA: Final = _STANDARD + "1-2-0/ParentRunFacet.json#/$defs/ParentRunFacet"
# Facette standard : les champs d'un jeu de données lu sont ceux que l'appel lit.
SCHEMA_FACET_SCHEMA: Final = _STANDARD + "1-2-0/SchemaDatasetFacet.json#/$defs/SchemaDatasetFacet"
# Facette de job standard : la requête SQL du job, sans ses littéraux.
SQL_FACET: Final = "sql"
SQL_FACET_SCHEMA: Final = _STANDARD + "1-1-0/SQLJobFacet.json#/$defs/SQLJobFacet"
# Nom de job d'un appel dont le nom d'outil n'a pas une forme admise.
UNRESOLVED_TOOL: Final = "outil-non-resolu"
# Motif d'une lecture non résolue : l'outil n'a ni règle de lecture ni SQL déclaré.
UNDESCRIBED_TOOL: Final = "undescribed_tool"


def producer(component: str) -> str:
    """L'adresse du producteur : le composant d'AnswerLineage qui émet, à sa version."""
    return f"https://github.com/alebgl77/AnswerLineage/tree/{__version__}/{component}"


def format_event_time(moment: datetime) -> str:
    """L'heure d'un événement : UTC, à la microseconde, suffixée de ``Z``."""
    return moment.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def facet(producer: str, schema_url: str, **fields: Any) -> dict[str, Any]:
    """Une facette : son producteur et l'adresse de son schéma, puis ses champs."""
    return {"_producer": producer, "_schemaURL": schema_url, **fields}


def parent_facet(producer: str, run_id: UUID, job: dict[str, str]) -> dict[str, Any]:
    """La facette standard ``parent`` : le run parent et son job."""
    return facet(producer, PARENT_FACET_SCHEMA, run={"runId": str(run_id)}, job=job)


def schema_facet(producer: str, columns: Sequence[str]) -> dict[str, Any]:
    """La facette standard ``schema`` d'un jeu de données lu : les colonnes lues."""
    return facet(producer, SCHEMA_FACET_SCHEMA, fields=[{"name": column} for column in columns])


def run_event(
    *,
    producer: str,
    event_type: EventType,
    event_time: datetime,
    run_id: UUID,
    run_facets: dict[str, Any],
    job: dict[str, Any],
    inputs: list[dict[str, Any]] | None = None,
    outputs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Un RunEvent OpenLineage (schéma 2-0-2). Les facettes, le job et les jeux de données
    y entrent tels quels, sans copie."""
    return {
        "eventType": event_type,
        "eventTime": format_event_time(event_time),
        "producer": producer,
        "schemaURL": RUN_EVENT_SCHEMA,
        "run": {"runId": str(run_id), "facets": run_facets},
        "job": job,
        "inputs": [] if inputs is None else inputs,
        "outputs": [] if outputs is None else outputs,
    }
