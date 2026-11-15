"""Remonter : d'une réponse à ses sources.

Les sources d'une réponse sont les lectures de son run et de tous ses runs
enfants : appels d'outils, sous-agents. Le run parent n'en fait pas partie.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from answerlineage_api.lineage.models import (
    AgentName,
    AnswerSummary,
    DatasetSource,
    Precision,
    Sources,
    ToolUse,
)

# Borne de l'arbre des runs parcouru pour une réponse.
MAX_RUNS = 10_000
# Réponses d'un même run examinées pour retrouver un identifiant.
_MAX_ANSWERS_PER_RUN = 1_000
# De la lecture la moins précise à la plus précise.
_PRECISIONS: tuple[Precision, ...] = ("table", "columns", "subset", "keys")

_ANSWER_COLUMNS = """
    a.run_id, a.fingerprint, a.event_time, g.namespace AS agent_namespace, g.name AS agent_name
    FROM answer a
    JOIN run r ON r.run_id = a.run_id
    LEFT JOIN agent g ON g.id = r.agent_id
"""

_BY_MESSAGE = text(
    f"""
    SELECT {_ANSWER_COLUMNS}
    JOIN delivery d ON d.answer_id = a.id
    WHERE d.channel = :channel AND d.message_ref = :message_ref
    ORDER BY a.event_time, a.run_id, a.fingerprint
    LIMIT :limit
    """
)

_BY_RUN = text(
    f"""
    SELECT {_ANSWER_COLUMNS}
    WHERE a.run_id = :run_id
    ORDER BY a.fingerprint
    LIMIT :limit
    """
)

# UNION, et non UNION ALL : deux runs qui se désignent l'un l'autre comme
# parent ne font pas tourner la recherche sans fin.
_RUN_TREE = text(
    """
    WITH RECURSIVE tree(run_id) AS (
        SELECT CAST(:root AS uuid)
        UNION
        SELECT child.run_id FROM run child JOIN tree ON child.parent_run_id = tree.run_id
    )
    SELECT run_id FROM tree WHERE run_id <> :root LIMIT :limit
    """
)

_ACTORS = text(
    """
    SELECT g.namespace AS agent_namespace, g.name AS agent_name,
           t.server AS tool_server, t.name AS tool_name
    FROM run r
    LEFT JOIN agent g ON g.id = r.agent_id
    LEFT JOIN tool t ON t.id = r.tool_id
    WHERE r.run_id = ANY(:run_ids)
    """
)

_READS = text(
    """
    SELECT d.namespace, d.name, d.kind, v.version, e.precision, e.columns, e.event_time
    FROM read_edge e
    JOIN dataset d ON d.id = e.dataset_id
    LEFT JOIN dataset_version v ON v.id = e.dataset_version_id
    WHERE e.run_id = ANY(:run_ids)
    """
)


def answer_key(run_id: UUID, fingerprint: str) -> str:
    """Identifiant stable d'une réponse : son run, puis un condensé de son empreinte.

    Il se recalcule à partir du graphe et ne recopie aucun texte de l'émetteur.
    """
    return run_id.hex + hashlib.sha256(fingerprint.encode()).hexdigest()[:16]


def _summary(row: Row[Any]) -> AnswerSummary:
    agent = (
        AgentName(namespace=row.agent_namespace, name=row.agent_name)
        if row.agent_name is not None
        else None
    )
    return AnswerSummary(
        id=answer_key(row.run_id, row.fingerprint),
        fingerprint=row.fingerprint,
        produced_at=row.event_time,
        run_id=row.run_id,
        agent=agent,
    )


async def find_by_message(
    conn: AsyncConnection, channel: str, message_ref: str, limit: int
) -> list[AnswerSummary]:
    """Réponses diffusées par ce message, les plus anciennes d'abord."""
    rows = await conn.execute(
        _BY_MESSAGE, {"channel": channel, "message_ref": message_ref, "limit": limit}
    )
    return [_summary(row) for row in rows]


async def find_answer(conn: AsyncConnection, answer_id: str) -> AnswerSummary | None:
    """La réponse que désigne cet identifiant, déjà validé par la route."""
    run_id = UUID(hex=answer_id[:32])
    rows = await conn.execute(_BY_RUN, {"run_id": run_id, "limit": _MAX_ANSWERS_PER_RUN})
    for row in rows:
        if answer_key(run_id, row.fingerprint) == answer_id:
            return _summary(row)
    return None


def _datasets(rows: Sequence[Row[Any]]) -> list[DatasetSource]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        source = merged.setdefault(
            (row.namespace, row.name),
            {
                "kind": row.kind,
                "versions": set(),
                "precision": len(_PRECISIONS) - 1,
                "columns": set(),
                "first": row.event_time,
                "last": row.event_time,
            },
        )
        if row.version is not None:
            source["versions"].add(row.version)
        # La lecture la moins précise l'emporte : un jeu lu en entier une
        # seule fois est une source en entier.
        source["precision"] = min(source["precision"], _PRECISIONS.index(row.precision))
        source["columns"].update(row.columns)
        source["first"] = min(source["first"], row.event_time)
        source["last"] = max(source["last"], row.event_time)
    return [
        DatasetSource(
            namespace=namespace,
            name=name,
            kind=source["kind"],
            versions=sorted(source["versions"]),
            precision=_PRECISIONS[source["precision"]],
            columns=sorted(source["columns"]) if source["precision"] > 0 else [],
            first_read_at=source["first"],
            last_read_at=source["last"],
        )
        for (namespace, name), source in sorted(merged.items())
    ]


async def sources(conn: AsyncConnection, answer: AnswerSummary) -> Sources:
    """Jeux de données, versions, outils et agents intervenus dans la réponse."""
    children = (
        (await conn.execute(_RUN_TREE, {"root": answer.run_id, "limit": MAX_RUNS})).scalars().all()
    )
    truncated = len(children) >= MAX_RUNS
    run_ids = [answer.run_id, *children[: MAX_RUNS - 1]]

    agents: set[tuple[str, str]] = set()
    calls: dict[tuple[str, str], int] = {}
    for row in await conn.execute(_ACTORS, {"run_ids": run_ids}):
        if row.agent_name is not None:
            agents.add((row.agent_namespace, row.agent_name))
        if row.tool_name is not None:
            tool = (row.tool_server, row.tool_name)
            calls[tool] = calls.get(tool, 0) + 1
    reads = (await conn.execute(_READS, {"run_ids": run_ids})).all()

    return Sources(
        answer=answer,
        agents=[AgentName(namespace=namespace, name=name) for namespace, name in sorted(agents)],
        tools=[
            ToolUse(server=server, name=name, calls=count)
            for (server, name), count in sorted(calls.items())
        ],
        datasets=_datasets(reads),
        truncated=truncated,
    )
