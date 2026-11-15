"""Événements OpenLineage émis par le proxy : un par appel d'outil.

Un événement ne porte aucun contenu : ni argument, ni résultat, ni en-tête,
ni jeton.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from answerlineage_proxy import __version__
from answerlineage_proxy.inspection import Inspection
from answerlineage_proxy.sql import SqlAnalysis
from answerlineage_proxy.tracecontext import CallTrace

Outcome = Literal["COMPLETE", "FAIL", "ABORT"]

PRODUCER = f"https://github.com/alebgl77/AnswerLineage/tree/{__version__}/proxy"
RUN_EVENT_SCHEMA = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
TOOL_CALL_FACET = "answerlineage_toolCall"
# Schéma publié de la facette, à une adresse qui ne change plus (spec/facets/1-0-0/).
TOOL_CALL_FACET_SCHEMA = (
    "https://raw.githubusercontent.com/alebgl77/AnswerLineage/main/spec/facets/1-0-0/"
    "ToolCallRunFacet.json#/$defs/ToolCallRunFacet"
)
# Facette standard : les champs d'un jeu de données lu sont ceux que la requête lit.
SCHEMA_FACET_SCHEMA = (
    "https://openlineage.io/spec/facets/1-2-0/SchemaDatasetFacet.json#/$defs/SchemaDatasetFacet"
)
# Nom de job d'un appel dont le nom d'outil n'a pas une forme admise.
UNRESOLVED_TOOL = "outil-non-resolu"


def format_event_time(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def build_tool_call_event(
    *,
    run_id: UUID,
    event_time: datetime,
    outcome: Outcome,
    server: str,
    inspection: Inspection,
    http_status: int | None = None,
    proxy_error: str | None = None,
    reads: SqlAnalysis | None = None,
    namespace: str | None = None,
    trace: CallTrace | None = None,
) -> dict[str, Any]:
    """Construit le RunEvent d'un appel d'outil relayé.

    ``reads`` est l'analyse de la requête SQL d'un outil déclaré : chaque table
    lue devient un jeu de données d'entrée, sous ``namespace``. ``trace`` est le
    contexte de trace de l'appel, dont ``run_id`` dérive : la facette le cite,
    pour passer du lignage à la trace.
    """
    facet: dict[str, Any] = {
        "_producer": PRODUCER,
        "_schemaURL": TOOL_CALL_FACET_SCHEMA,
        "server": server,
        "transport": "http",
        "headerMismatch": sorted(inspection.header_mismatches),
        "invalidToolName": inspection.invalid_tool_name,
    }
    if inspection.tool is not None:
        facet["tool"] = inspection.tool
    if inspection.protocol_version is not None:
        facet["protocolVersion"] = inspection.protocol_version
    if http_status is not None:
        facet["httpStatus"] = http_status
    if proxy_error is not None:
        facet["proxyError"] = proxy_error
    if trace is not None:
        facet["traceId"] = trace.context.trace_id
        facet["spanId"] = trace.context.parent_id
        facet["traceOrigin"] = trace.origin
    inputs: list[dict[str, Any]] = []
    if reads is not None:
        facet["readResolution"] = "resolved" if reads.resolved else "unresolved"
        if reads.reason is not None:
            facet["unresolvedReason"] = reads.reason
        for table in reads.tables:
            dataset: dict[str, Any] = {"namespace": namespace or "", "name": table.name}
            if table.precision == "columns":
                dataset["facets"] = {
                    "schema": {
                        "_producer": PRODUCER,
                        "_schemaURL": SCHEMA_FACET_SCHEMA,
                        "fields": [{"name": column} for column in table.columns],
                    }
                }
            inputs.append(dataset)
    return {
        "eventType": outcome,
        "eventTime": format_event_time(event_time),
        "producer": PRODUCER,
        "schemaURL": RUN_EVENT_SCHEMA,
        "run": {"runId": str(run_id), "facets": {TOOL_CALL_FACET: facet}},
        "job": {"namespace": f"mcp://{server}", "name": inspection.tool or UNRESOLVED_TOOL},
        "inputs": inputs,
        "outputs": [],
    }
