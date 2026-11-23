"""Événements OpenLineage émis par le proxy : un par appel d'outil.

Un événement ne porte aucun contenu : ni argument, ni résultat, ni en-tête,
ni jeton. Des arguments et du résultat, il ne garde que des empreintes à clé et
une taille ; d'une requête SQL, que sa forme sans littéraux ; d'une clé lue
d'après une règle de lecture, que son empreinte à clé. Le RunEvent et ses
facettes se construisent avec les briques de tout émetteur
(``answerlineage_emitter.events``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from answerlineage_emitter.events import (
    KEYS_FACET,
    KEYS_FACET_SCHEMA,
    QOS_FACET,
    QOS_FACET_SCHEMA,
    RUN_EVENT_SCHEMA,
    SCHEMA_FACET_SCHEMA,
    SQL_FACET,
    SQL_FACET_SCHEMA,
    TOOL_CALL_FACET,
    TOOL_CALL_FACET_SCHEMA,
    TOOL_CALL_FACET_SCHEMA_QUARANTINE,
    TOOL_CALL_FACET_SCHEMA_READ_KEYS,
    TOOL_CALL_FACET_SCHEMA_RUN_ID_TAG,
    UNDESCRIBED_TOOL,
    UNRESOLVED_TOOL,
    facet,
    format_event_time,
    producer,
    run_event,
    schema_facet,
)
from answerlineage_proxy.annotation import QuarantineNote
from answerlineage_proxy.inspection import Inspection
from answerlineage_proxy.rules import DeclaredDataset, DeclaredReads
from answerlineage_proxy.signals import CallSignals
from answerlineage_proxy.sql import SqlAnalysis
from answerlineage_proxy.tracecontext import CallTrace

__all__ = [
    "KEYS_FACET",
    "KEYS_FACET_SCHEMA",
    "PRODUCER",
    "QOS_FACET",
    "QOS_FACET_SCHEMA",
    "RUN_EVENT_SCHEMA",
    "SCHEMA_FACET_SCHEMA",
    "SQL_FACET",
    "SQL_FACET_SCHEMA",
    "TOOL_CALL_FACET",
    "TOOL_CALL_FACET_SCHEMA",
    "TOOL_CALL_FACET_SCHEMA_QUARANTINE",
    "TOOL_CALL_FACET_SCHEMA_READ_KEYS",
    "TOOL_CALL_FACET_SCHEMA_RUN_ID_TAG",
    "UNDESCRIBED_TOOL",
    "UNRESOLVED_TOOL",
    "Outcome",
    "Transport",
    "build_tool_call_event",
    "format_event_time",
]

Outcome = Literal["COMPLETE", "FAIL", "ABORT"]
# Transport de l'appel relayé : un serveur MCP en HTTP, ou lancé par le proxy en stdio.
Transport = Literal["http", "stdio"]

PRODUCER = producer("proxy")


def _declared_input(dataset: DeclaredDataset, keys: tuple[str, ...]) -> dict[str, Any]:
    """Un jeu de données qu'une règle déclare lu : ses colonnes, et les clés lues."""
    item: dict[str, Any] = {"namespace": dataset.namespace, "name": dataset.name}
    if dataset.columns:
        item["facets"] = {"schema": schema_facet(PRODUCER, dataset.columns)}
    if keys:
        item["inputFacets"] = {KEYS_FACET: facet(PRODUCER, KEYS_FACET_SCHEMA, hmacs=list(keys))}
    return item


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
    dialect: str | None = None,
    arguments_fingerprint: str | None = None,
    result_fingerprint: str | None = None,
    result_size: int | None = None,
    signals: CallSignals | None = None,
    declared: DeclaredReads | None = None,
    transport: Transport = "http",
    tagged_with_run_id: bool | None = None,
    quarantine: QuarantineNote | None = None,
) -> dict[str, Any]:
    """Construit le RunEvent d'un appel d'outil relayé.

    ``reads`` est l'analyse de la requête SQL d'un outil déclaré : chaque table
    lue devient un jeu de données d'entrée, sous ``namespace``. ``trace`` est le
    contexte de trace de l'appel, dont ``run_id`` dérive : la facette le cite,
    pour passer du lignage à la trace. La forme sans littéraux de la requête,
    quand l'analyse en a établi une, va dans la facette de job ``sql``, avec
    ``dialect``. Les empreintes et la taille du résultat vont dans la facette de
    l'appel ; ``signals``, latence, issue et reprises, dans la facette de
    qualité de service. ``declared`` sont les lectures qu'une règle déclare :
    chaque jeu de données porte ses colonnes et les empreintes des clés lues.
    ``transport`` est celui de l'appel relayé : ``http``, ou ``stdio`` pour un
    serveur lancé par le proxy. ``tagged_with_run_id`` dit, pour un outil dont la
    requête relayée doit porter l'identifiant de run en commentaire, si elle l'a
    reçu : un indicateur, jamais la requête. ``quarantine`` dit, pour un appel
    retenu par une quarantaine, la décision et si le résultat a été annoté : des
    codes, des identifiants d'incident et une durée, jamais l'avertissement.
    """
    described = facet(
        PRODUCER,
        TOOL_CALL_FACET_SCHEMA,
        server=server,
        transport=transport,
        headerMismatch=sorted(inspection.header_mismatches),
        invalidToolName=inspection.invalid_tool_name,
    )
    if inspection.tool is not None:
        described["tool"] = inspection.tool
    if inspection.protocol_version is not None:
        described["protocolVersion"] = inspection.protocol_version
    if http_status is not None:
        described["httpStatus"] = http_status
    if proxy_error is not None:
        described["proxyError"] = proxy_error
    if arguments_fingerprint is not None:
        described["argumentsFingerprint"] = arguments_fingerprint
    if result_fingerprint is not None:
        described["resultFingerprint"] = result_fingerprint
    if result_size is not None:
        described["resultSize"] = result_size
    if trace is not None:
        described["traceId"] = trace.context.trace_id
        described["spanId"] = trace.context.parent_id
        described["traceOrigin"] = trace.origin
    inputs: list[dict[str, Any]] = []
    job: dict[str, Any] = {
        "namespace": f"mcp://{server}",
        "name": inspection.tool or UNRESOLVED_TOOL,
    }
    if reads is not None and reads.shape is not None:
        sql = facet(PRODUCER, SQL_FACET_SCHEMA, query=reads.shape)
        if dialect is not None:
            sql["dialect"] = dialect
        job["facets"] = {SQL_FACET: sql}
    if reads is not None:
        described["readResolution"] = "resolved" if reads.resolved else "unresolved"
        if reads.reason is not None:
            described["unresolvedReason"] = reads.reason
        for table in reads.tables:
            dataset: dict[str, Any] = {"namespace": namespace or "", "name": table.name}
            if table.precision == "columns":
                dataset["facets"] = {"schema": schema_facet(PRODUCER, table.columns)}
            inputs.append(dataset)
    if declared is not None:
        described["readResolution"] = "resolved"
        if declared.key_status is not None:
            described["readKeys"] = declared.key_status
            described["_schemaURL"] = TOOL_CALL_FACET_SCHEMA_READ_KEYS
        inputs.extend(_declared_input(dataset, declared.keys) for dataset in declared.datasets)
    if tagged_with_run_id is not None:
        described["taggedWithRunId"] = tagged_with_run_id
        described["_schemaURL"] = TOOL_CALL_FACET_SCHEMA_RUN_ID_TAG
    if quarantine is not None:
        described["quarantine"] = quarantine.fields()
        described["_schemaURL"] = TOOL_CALL_FACET_SCHEMA_QUARANTINE
    run_facets: dict[str, Any] = {TOOL_CALL_FACET: described}
    if signals is not None:
        run_facets[QOS_FACET] = facet(PRODUCER, QOS_FACET_SCHEMA, **signals.fields())
    return run_event(
        producer=PRODUCER,
        event_type=outcome,
        event_time=event_time,
        run_id=run_id,
        run_facets=run_facets,
        job=job,
        inputs=inputs,
    )
