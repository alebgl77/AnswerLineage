"""Événements OpenLineage émis par le proxy : un par appel d'outil.

Un événement ne porte aucun contenu : ni argument, ni résultat, ni en-tête,
ni jeton. Des arguments et du résultat, il ne garde que des empreintes à clé et
une taille ; d'une requête SQL, que sa forme sans littéraux ; d'une clé lue
d'après une règle de lecture, que son empreinte à clé.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from answerlineage_proxy import __version__
from answerlineage_proxy.inspection import Inspection
from answerlineage_proxy.rules import DeclaredDataset, DeclaredReads
from answerlineage_proxy.signals import CallSignals
from answerlineage_proxy.sql import SqlAnalysis
from answerlineage_proxy.tracecontext import CallTrace

Outcome = Literal["COMPLETE", "FAIL", "ABORT"]
# Transport de l'appel relayé : un serveur MCP en HTTP, ou lancé par le proxy en stdio.
Transport = Literal["http", "stdio"]

PRODUCER = f"https://github.com/alebgl77/AnswerLineage/tree/{__version__}/proxy"
RUN_EVENT_SCHEMA = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
TOOL_CALL_FACET = "answerlineage_toolCall"
# Schéma publié de la facette, à une adresse qui ne change plus (spec/facets/1-0-0/). La
# version 1-1-0 ajoute ce qu'est devenue la clé lue d'après une règle de lecture : la
# facette ne s'en réclame que lorsqu'elle le dit.
TOOL_CALL_FACET_SCHEMA = (
    "https://raw.githubusercontent.com/alebgl77/AnswerLineage/main/spec/facets/1-0-0/"
    "ToolCallRunFacet.json#/$defs/ToolCallRunFacet"
)
TOOL_CALL_FACET_SCHEMA_READ_KEYS = (
    "https://raw.githubusercontent.com/alebgl77/AnswerLineage/main/spec/facets/1-1-0/"
    "ToolCallRunFacet.json#/$defs/ToolCallRunFacet"
)
# La version 1-2-0 dit si la requête SQL relayée a reçu le commentaire qui porte
# l'identifiant de run : la facette ne s'en réclame que lorsqu'elle le dit.
TOOL_CALL_FACET_SCHEMA_RUN_ID_TAG = (
    "https://raw.githubusercontent.com/alebgl77/AnswerLineage/main/spec/facets/1-2-0/"
    "ToolCallRunFacet.json#/$defs/ToolCallRunFacet"
)
# Clés lues dans un jeu de données d'entrée, sous leurs empreintes à clé (spec/facets/1-0-0/).
KEYS_FACET = "answerlineage_keys"
KEYS_FACET_SCHEMA = (
    "https://raw.githubusercontent.com/alebgl77/AnswerLineage/main/spec/facets/1-0-0/"
    "KeysInputDatasetFacet.json#/$defs/KeysInputDatasetFacet"
)
# Qualité de service de l'appel : latence, latence ajoutée, issue, reprises (spec/facets/1-1-0/).
QOS_FACET = "answerlineage_qos"
QOS_FACET_SCHEMA = (
    "https://raw.githubusercontent.com/alebgl77/AnswerLineage/main/spec/facets/1-1-0/"
    "QosRunFacet.json#/$defs/QosRunFacet"
)
# Facette standard : les champs d'un jeu de données lu sont ceux que la requête lit.
SCHEMA_FACET_SCHEMA = (
    "https://openlineage.io/spec/facets/1-2-0/SchemaDatasetFacet.json#/$defs/SchemaDatasetFacet"
)
# Facette de job standard : la requête SQL du job, ici sans ses littéraux.
SQL_FACET = "sql"
SQL_FACET_SCHEMA = "https://openlineage.io/spec/facets/1-1-0/SQLJobFacet.json#/$defs/SQLJobFacet"
# Nom de job d'un appel dont le nom d'outil n'a pas une forme admise.
UNRESOLVED_TOOL = "outil-non-resolu"
# Motif d'une lecture non résolue : l'outil n'a ni règle de lecture ni SQL déclaré.
UNDESCRIBED_TOOL = "undescribed_tool"


def _schema_facet(columns: tuple[str, ...]) -> dict[str, Any]:
    return {
        "_producer": PRODUCER,
        "_schemaURL": SCHEMA_FACET_SCHEMA,
        "fields": [{"name": column} for column in columns],
    }


def _declared_input(dataset: DeclaredDataset, keys: tuple[str, ...]) -> dict[str, Any]:
    """Un jeu de données qu'une règle déclare lu : ses colonnes, et les clés lues."""
    item: dict[str, Any] = {"namespace": dataset.namespace, "name": dataset.name}
    if dataset.columns:
        item["facets"] = {"schema": _schema_facet(dataset.columns)}
    if keys:
        item["inputFacets"] = {
            KEYS_FACET: {
                "_producer": PRODUCER,
                "_schemaURL": KEYS_FACET_SCHEMA,
                "hmacs": list(keys),
            }
        }
    return item


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
    dialect: str | None = None,
    arguments_fingerprint: str | None = None,
    result_fingerprint: str | None = None,
    result_size: int | None = None,
    signals: CallSignals | None = None,
    declared: DeclaredReads | None = None,
    transport: Transport = "http",
    tagged_with_run_id: bool | None = None,
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
    reçu : un indicateur, jamais la requête.
    """
    facet: dict[str, Any] = {
        "_producer": PRODUCER,
        "_schemaURL": TOOL_CALL_FACET_SCHEMA,
        "server": server,
        "transport": transport,
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
    if arguments_fingerprint is not None:
        facet["argumentsFingerprint"] = arguments_fingerprint
    if result_fingerprint is not None:
        facet["resultFingerprint"] = result_fingerprint
    if result_size is not None:
        facet["resultSize"] = result_size
    if trace is not None:
        facet["traceId"] = trace.context.trace_id
        facet["spanId"] = trace.context.parent_id
        facet["traceOrigin"] = trace.origin
    inputs: list[dict[str, Any]] = []
    job: dict[str, Any] = {
        "namespace": f"mcp://{server}",
        "name": inspection.tool or UNRESOLVED_TOOL,
    }
    if reads is not None and reads.shape is not None:
        sql: dict[str, Any] = {
            "_producer": PRODUCER,
            "_schemaURL": SQL_FACET_SCHEMA,
            "query": reads.shape,
        }
        if dialect is not None:
            sql["dialect"] = dialect
        job["facets"] = {SQL_FACET: sql}
    if reads is not None:
        facet["readResolution"] = "resolved" if reads.resolved else "unresolved"
        if reads.reason is not None:
            facet["unresolvedReason"] = reads.reason
        for table in reads.tables:
            dataset: dict[str, Any] = {"namespace": namespace or "", "name": table.name}
            if table.precision == "columns":
                dataset["facets"] = {"schema": _schema_facet(table.columns)}
            inputs.append(dataset)
    if declared is not None:
        facet["readResolution"] = "resolved"
        if declared.key_status is not None:
            facet["readKeys"] = declared.key_status
            facet["_schemaURL"] = TOOL_CALL_FACET_SCHEMA_READ_KEYS
        inputs.extend(_declared_input(dataset, declared.keys) for dataset in declared.datasets)
    if tagged_with_run_id is not None:
        facet["taggedWithRunId"] = tagged_with_run_id
        facet["_schemaURL"] = TOOL_CALL_FACET_SCHEMA_RUN_ID_TAG
    run_facets: dict[str, Any] = {TOOL_CALL_FACET: facet}
    if signals is not None:
        run_facets[QOS_FACET] = {
            "_producer": PRODUCER,
            "_schemaURL": QOS_FACET_SCHEMA,
            **signals.fields(),
        }
    return {
        "eventType": outcome,
        "eventTime": format_event_time(event_time),
        "producer": PRODUCER,
        "schemaURL": RUN_EVENT_SCHEMA,
        "run": {"runId": str(run_id), "facets": run_facets},
        "job": job,
        "inputs": inputs,
        "outputs": [],
    }
