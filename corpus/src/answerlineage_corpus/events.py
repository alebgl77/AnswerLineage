"""Événements OpenLineage de l'entreprise synthétique.

Chaque demande devient ce que des émetteurs réels enverraient : l'ouverture du
run de l'agent, un événement par appel d'outil, tel que le proxy l'émet, puis
la fin du run avec la réponse et sa diffusion. Aucun événement ne porte de
contenu.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from answerlineage_corpus import __version__
from answerlineage_corpus.enterprise import Agent, Enterprise, Request, TableRead, ToolCall

PRODUCER = f"https://github.com/alebgl77/AnswerLineage/tree/{__version__}/corpus"
RUN_EVENT_SCHEMA = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
PROTOCOL_VERSION = "2026-07-28"

AGENT_FACET = "answerlineage_agent"
TOOL_CALL_FACET = "answerlineage_toolCall"
DELIVERY_FACET = "answerlineage_delivery"

_STANDARD = "https://openlineage.io/spec/facets/"
_PARENT_SCHEMA = _STANDARD + "1-2-0/ParentRunFacet.json#/$defs/ParentRunFacet"
_SCHEMA_SCHEMA = _STANDARD + "1-2-0/SchemaDatasetFacet.json#/$defs/SchemaDatasetFacet"
_VERSION_SCHEMA = (
    _STANDARD + "1-0-1/DatasetVersionDatasetFacet.json#/$defs/DatasetVersionDatasetFacet"
)
_TYPE_SCHEMA = _STANDARD + "1-0-1/DatasetTypeDatasetFacet.json#/$defs/DatasetTypeDatasetFacet"
# Schémas publiés des facettes propres, à des adresses qui ne changent plus (spec/facets/1-0-0/).
_OWN = "https://raw.githubusercontent.com/alebgl77/AnswerLineage/main/spec/facets/1-0-0/"
_AGENT_SCHEMA = _OWN + "AgentRunFacet.json#/$defs/AgentRunFacet"
_TOOL_CALL_SCHEMA = _OWN + "ToolCallRunFacet.json#/$defs/ToolCallRunFacet"
_DELIVERY_SCHEMA = _OWN + "DeliveryOutputDatasetFacet.json#/$defs/DeliveryOutputDatasetFacet"


def _facet(schema: str, **fields: Any) -> dict[str, Any]:
    return {"_producer": PRODUCER, "_schemaURL": schema, **fields}


def _instant(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _event(kind: str, moment: datetime, run: dict[str, Any], job: dict[str, str]) -> dict[str, Any]:
    return {
        "eventType": kind,
        "eventTime": _instant(moment),
        "producer": PRODUCER,
        "schemaURL": RUN_EVENT_SCHEMA,
        "run": run,
        "job": job,
        "inputs": [],
        "outputs": [],
    }


def _parent(run_id: UUID, job: dict[str, str]) -> dict[str, Any]:
    return _facet(_PARENT_SCHEMA, run={"runId": str(run_id)}, job=job)


def dataset_read(
    namespace: str,
    name: str,
    *,
    columns: Sequence[str] = (),
    version: str | None = None,
    kind: str = "table",
) -> dict[str, Any]:
    """Un jeu de données lu : ses colonnes et sa version voyagent dans des facettes standard."""
    dataset: dict[str, Any] = {"namespace": namespace, "name": name}
    facets: dict[str, Any] = {}
    if columns:
        facets["schema"] = _facet(_SCHEMA_SCHEMA, fields=[{"name": column} for column in columns])
    if version is not None:
        facets["version"] = _facet(_VERSION_SCHEMA, datasetVersion=version)
    if kind != "table":
        facets["datasetType"] = _facet(_TYPE_SCHEMA, datasetType=kind)
    if facets:
        dataset["facets"] = facets
    return dataset


def dataset_written(namespace: str, name: str, *, kind: str = "table") -> dict[str, Any]:
    """Un jeu de données écrit par un run : mémoire, fichier, table."""
    dataset: dict[str, Any] = {"namespace": namespace, "name": name}
    if kind != "table":
        dataset["facets"] = {"datasetType": _facet(_TYPE_SCHEMA, datasetType=kind)}
    return dataset


def answer_delivered(
    job: dict[str, str],
    *,
    fingerprint: str,
    channel: str,
    message_ref: str,
    recipients: Sequence[str],
) -> dict[str, Any]:
    """La réponse d'un agent : une sortie décrite par son empreinte et sa diffusion."""
    return {
        "namespace": job["namespace"],
        "name": f"{job['name']}/reponses",
        "outputFacets": {
            DELIVERY_FACET: _facet(
                _DELIVERY_SCHEMA,
                fingerprint=fingerprint,
                channel=channel,
                messageRef=message_ref,
                recipients=list(recipients),
            )
        },
    }


def agent_event(
    kind: str,
    moment: datetime,
    run_id: UUID,
    job: dict[str, str],
    *,
    parent: tuple[UUID, dict[str, str]] | None = None,
    outputs: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Un événement du run d'un agent ; ``parent`` le rattache au run qui l'a délégué."""
    facets: dict[str, Any] = {AGENT_FACET: _facet(_AGENT_SCHEMA)}
    if parent is not None:
        facets["parent"] = _parent(*parent)
    event = _event(kind, moment, {"runId": str(run_id), "facets": facets}, job)
    event["outputs"] = list(outputs)
    return event


def tool_event(
    moment: datetime,
    run_id: UUID,
    *,
    server: str,
    tool: str,
    parent: tuple[UUID, dict[str, str]],
    inputs: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Un appel d'outil terminé, run enfant de celui de l'agent, tel que le proxy l'émet."""
    event = _event(
        "COMPLETE",
        moment,
        {
            "runId": str(run_id),
            "facets": {
                "parent": _parent(*parent),
                TOOL_CALL_FACET: _facet(
                    _TOOL_CALL_SCHEMA,
                    server=server,
                    transport="http",
                    headerMismatch=[],
                    invalidToolName=False,
                    tool=tool,
                    protocolVersion=PROTOCOL_VERSION,
                    httpStatus=200,
                    readResolution="resolved",
                ),
            },
        },
        {"namespace": f"mcp://{server}", "name": tool},
    )
    event["inputs"] = list(inputs)
    return event


def _job(agent: Agent) -> dict[str, str]:
    return {"namespace": agent.namespace, "name": agent.name}


def _input(read: TableRead) -> dict[str, Any]:
    return dataset_read(
        read.table.namespace, read.table.name, columns=read.columns, version=read.version
    )


def tool_call_event(request: Request, call: ToolCall) -> dict[str, Any]:
    """L'appel d'outil, run enfant de la demande, avec les tables qu'il a lues."""
    return tool_event(
        call.time,
        call.run_id,
        server=call.tool.server,
        tool=call.tool.name,
        parent=(request.run_id, _job(request.agent)),
        inputs=[_input(read) for read in call.reads],
    )


def request_events(request: Request) -> list[dict[str, Any]]:
    """Les événements d'une demande, dans l'ordre où ils se produisent."""
    job = _job(request.agent)
    events = [agent_event("START", request.started_at, request.run_id, job)]
    events.extend(tool_call_event(request, call) for call in request.calls)
    if request.answer is None:
        events.append(agent_event("FAIL", request.ended_at, request.run_id, job))
        return events
    answer = answer_delivered(
        job,
        fingerprint=request.answer.fingerprint,
        channel=request.answer.channel,
        message_ref=request.answer.message_ref,
        recipients=request.answer.recipients,
    )
    events.append(agent_event("COMPLETE", request.ended_at, request.run_id, job, outputs=[answer]))
    return events


def in_time_order(events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Les événements dans l'ordre du temps ; à heure égale, leur ordre est gardé."""
    return sorted(events, key=lambda event: str(event["eventTime"]))


def render(enterprise: Enterprise) -> list[dict[str, Any]]:
    """Tous les événements de l'entreprise, dans l'ordre du temps."""
    return in_time_order(
        [event for request in enterprise.requests for event in request_events(request)]
    )
