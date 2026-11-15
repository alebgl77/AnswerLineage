"""Projection d'un RunEvent OpenLineage vers le graphe de lignage.

L'événement a déjà été validé par l'API d'ingestion. La projection est
tolérante : une facette propre mal formée est ignorée, elle n'empêche pas
d'enregistrer le reste de l'événement.

| Dans l'événement | Dans le graphe |
| --- | --- |
| facette ``answerlineage_toolCall`` | run d'outil, rattaché à son serveur et à son outil |
| facette ``answerlineage_agent`` | run d'agent, rattaché à l'agent que nomme le job |
| facette ``parent`` | run parent |
| ``inputs`` | lectures ; les champs de la facette ``schema`` sont les colonnes lues |
| ``outputs`` avec la facette ``answerlineage_delivery`` | réponse et sa diffusion |
| autres ``outputs`` | écritures |
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from answerlineage_api.store.graph import (
    AgentRef,
    Answer,
    DatasetKind,
    DatasetRef,
    Delivery,
    Read,
    RunKind,
    RunRecord,
    RunState,
    ToolRef,
    Write,
)

AGENT_FACET = "answerlineage_agent"
TOOL_CALL_FACET = "answerlineage_toolCall"
DELIVERY_FACET = "answerlineage_delivery"

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_SERVER_PREFIX = "mcp://"
_DATASET_KINDS: tuple[DatasetKind, ...] = ("table", "document", "api", "memory", "answer")
_STATES: tuple[RunState, ...] = ("START", "RUNNING", "COMPLETE", "ABORT", "FAIL", "OTHER")


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _facet(owner: Mapping[str, Any], name: str, *containers: str) -> Mapping[str, Any]:
    for container in containers or ("facets",):
        facet = _mapping(owner.get(container)).get(name)
        if isinstance(facet, Mapping):
            return facet
    return {}


def _parent_run_id(run: Mapping[str, Any]) -> UUID | None:
    value = _mapping(_facet(run, "parent").get("run")).get("runId")
    if isinstance(value, str) and _UUID.fullmatch(value):
        return UUID(value)
    return None


def _dataset(dataset: Mapping[str, Any]) -> DatasetRef:
    declared = _facet(dataset, "datasetType").get("datasetType")
    kind: DatasetKind = "table"
    for candidate in _DATASET_KINDS:
        if isinstance(declared, str) and declared.lower() == candidate:
            kind = candidate
    return DatasetRef(
        namespace=str(dataset["namespace"]),
        name=str(dataset["name"]),
        kind=kind,
        version=_text(_facet(dataset, "version").get("datasetVersion")),
    )


def _read(dataset: Mapping[str, Any]) -> Read:
    fields = _facet(dataset, "schema").get("fields")
    columns = sorted(
        {
            name
            for field in (fields if isinstance(fields, list) else [])
            if (name := _text(_mapping(field).get("name"))) is not None
        }
    )
    if columns:
        return Read(_dataset(dataset), precision="columns", columns=tuple(columns))
    return Read(_dataset(dataset))


def _answer(facet: Mapping[str, Any]) -> Answer | None:
    fingerprint = _text(facet.get("fingerprint"))
    if fingerprint is None:
        return None
    channel, message_ref = _text(facet.get("channel")), _text(facet.get("messageRef"))
    if channel is None or message_ref is None:
        return Answer(fingerprint=fingerprint)
    recipients = facet.get("recipients")
    pseudonyms = tuple(
        sorted(
            {item for item in (recipients if isinstance(recipients, list) else []) if _text(item)}
        )
    )
    return Answer(
        fingerprint=fingerprint,
        deliveries=(Delivery(channel=channel, message_ref=message_ref, recipients=pseudonyms),),
    )


def project(payload: Mapping[str, Any]) -> RunRecord:
    """Traduit un RunEvent validé en enregistrement du graphe."""
    run = _mapping(payload["run"])
    job = _mapping(payload["job"])
    declared_state = payload.get("eventType", "OTHER")
    state: RunState = "OTHER"
    for candidate in _STATES:
        if declared_state == candidate:
            state = candidate

    kind: RunKind = "job"
    agent: AgentRef | None = None
    tool: ToolRef | None = None
    tool_call = _facet(run, TOOL_CALL_FACET)
    if tool_call:
        kind = "tool"
        namespace = str(job["namespace"])
        server = _text(tool_call.get("server")) or namespace.removeprefix(_SERVER_PREFIX)
        tool = ToolRef(server=server, name=_text(tool_call.get("tool")) or str(job["name"]))
    elif _facet(run, AGENT_FACET):
        kind = "agent"
        agent = AgentRef(namespace=str(job["namespace"]), name=str(job["name"]))

    reads: list[Read] = []
    for dataset in payload.get("inputs") or []:
        reads.append(_read(_mapping(dataset)))
    writes: list[Write] = []
    answers: list[Answer] = []
    for dataset in payload.get("outputs") or []:
        output = _mapping(dataset)
        delivery = _facet(output, DELIVERY_FACET, "facets", "outputFacets")
        answer = _answer(delivery) if delivery else None
        if answer is not None:
            answers.append(answer)
        else:
            writes.append(Write(_dataset(output)))

    return RunRecord(
        run_id=UUID(str(run["runId"])),
        kind=kind,
        state=state,
        event_time=datetime.fromisoformat(str(payload["eventTime"]).replace("Z", "+00:00")),
        agent=agent,
        tool=tool,
        parent_run_id=_parent_run_id(run),
        reads=tuple(reads),
        writes=tuple(writes),
        answers=tuple(answers),
    )
