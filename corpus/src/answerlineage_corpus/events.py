"""Événements OpenLineage de l'entreprise synthétique.

Chaque demande devient ce que des émetteurs réels enverraient : l'ouverture du
run de l'agent, un événement par appel d'outil, tel que le proxy l'émet, puis
la fin du run avec la réponse et sa diffusion. Aucun événement ne porte de
contenu.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

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
# Adresses provisoires : les schémas des facettes propres seront publiés à une URL immuable.
_OWN = "https://github.com/alebgl77/AnswerLineage/blob/main/spec/facets/"
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


def _job(agent: Agent) -> dict[str, str]:
    return {"namespace": agent.namespace, "name": agent.name}


def _input(read: TableRead) -> dict[str, Any]:
    dataset: dict[str, Any] = {"namespace": read.table.namespace, "name": read.table.name}
    facets: dict[str, Any] = {}
    if read.columns:
        facets["schema"] = _facet(
            _SCHEMA_SCHEMA, fields=[{"name": column} for column in read.columns]
        )
    if read.version is not None:
        facets["version"] = _facet(_VERSION_SCHEMA, datasetVersion=read.version)
    if facets:
        dataset["facets"] = facets
    return dataset


def tool_call_event(request: Request, call: ToolCall) -> dict[str, Any]:
    """L'appel d'outil, run enfant de la demande, avec les tables qu'il a lues."""
    event = _event(
        "COMPLETE",
        call.time,
        {
            "runId": str(call.run_id),
            "facets": {
                "parent": _facet(
                    _PARENT_SCHEMA, run={"runId": str(request.run_id)}, job=_job(request.agent)
                ),
                TOOL_CALL_FACET: _facet(
                    _TOOL_CALL_SCHEMA,
                    server=call.tool.server,
                    transport="http",
                    headerMismatch=[],
                    invalidToolName=False,
                    tool=call.tool.name,
                    protocolVersion=PROTOCOL_VERSION,
                    httpStatus=200,
                    readResolution="resolved",
                ),
            },
        },
        {"namespace": f"mcp://{call.tool.server}", "name": call.tool.name},
    )
    event["inputs"] = [_input(read) for read in call.reads]
    return event


def request_events(request: Request) -> list[dict[str, Any]]:
    """Les événements d'une demande, dans l'ordre où ils se produisent."""
    run = {"runId": str(request.run_id), "facets": {AGENT_FACET: _facet(_AGENT_SCHEMA)}}
    job = _job(request.agent)
    events = [_event("START", request.started_at, run, job)]
    events.extend(tool_call_event(request, call) for call in request.calls)
    if request.answer is None:
        events.append(_event("FAIL", request.ended_at, run, job))
        return events
    end = _event("COMPLETE", request.ended_at, run, job)
    end["outputs"] = [
        {
            "namespace": request.agent.namespace,
            "name": f"{request.agent.name}/reponses",
            "outputFacets": {
                DELIVERY_FACET: _facet(
                    _DELIVERY_SCHEMA,
                    fingerprint=request.answer.fingerprint,
                    channel=request.answer.channel,
                    messageRef=request.answer.message_ref,
                    recipients=list(request.answer.recipients),
                )
            },
        }
    ]
    events.append(end)
    return events


def render(enterprise: Enterprise) -> list[dict[str, Any]]:
    """Tous les événements de l'entreprise, dans l'ordre du temps."""
    events = [event for request in enterprise.requests for event in request_events(request)]
    # Tri stable : à heure égale, l'ordre de la demande est gardé.
    return sorted(events, key=lambda event: str(event["eventTime"]))
