"""Remonter : d'une réponse à ses sources.

Les sources d'une réponse sont les lectures de son run et de tous ses runs
enfants : appels d'outils, sous-agents. Le run parent n'en fait pas partie.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Row, text

from answerlineage_api.lineage.models import (
    AgentName,
    AnswerSummary,
    DatasetSource,
    Precision,
    Sources,
    ToolUse,
)
from answerlineage_api.lineage.paging import ANSWER_DIGEST, digest
from answerlineage_api.lineage.reading import Reader, known

# Borne de l'arbre des runs parcouru pour une réponse, son propre run compris.
MAX_RUNS = 10_000
# Borne des lectures faites par cet arbre.
MAX_READS = 50_000
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
      AND {known("a")} AND {known("d")}
    ORDER BY a.event_time, a.run_id, a.fingerprint
    LIMIT :limit
    """
)

# L'identifiant d'une réponse se retrouve dans la base, quel que soit le
# nombre de réponses de son run.
_BY_KEY = text(
    f"""
    SELECT {_ANSWER_COLUMNS}
    WHERE a.run_id = :run_id AND {ANSWER_DIGEST} = :digest AND {known("a")}
    ORDER BY a.fingerprint
    LIMIT :limit
    """
)

# UNION, et non UNION ALL : deux runs qui se désignent l'un l'autre comme
# parent ne font pas tourner la recherche sans fin.
_RUN_TREE = text(
    f"""
    WITH RECURSIVE tree(run_id) AS (
        SELECT CAST(:root AS uuid)
        UNION
        SELECT child.run_id FROM run child JOIN tree ON child.parent_run_id = tree.run_id
        WHERE {known("child")}
    )
    SELECT run_id FROM tree WHERE run_id <> :root LIMIT :limit
    """  # noqa: S608 — seul un fragment constant est interpolé.
)

# Une ligne par run demandé au plus : la borne est celle de l'arbre.

_ACTORS = text(
    """
    SELECT g.namespace AS agent_namespace, g.name AS agent_name,
           t.server AS tool_server, t.name AS tool_name
    FROM run r
    LEFT JOIN agent g ON g.id = r.agent_id
    LEFT JOIN tool t ON t.id = r.tool_id
    WHERE r.run_id = ANY(:run_ids)
    ORDER BY r.run_id
    LIMIT :limit
    """
)

# Les lectures les plus anciennes d'abord : une source partielle garde les premières.
_READS = text(
    f"""
    SELECT d.namespace, d.name, d.kind, v.version, e.precision, e.columns, e.event_time,
           e.last_event_time
    FROM read_edge e
    JOIN dataset d ON d.id = e.dataset_id
    LEFT JOIN dataset_version v ON v.id = e.dataset_version_id
    WHERE e.run_id = ANY(:run_ids) AND {known("e")}
    ORDER BY e.event_time, e.id
    LIMIT :limit
    """  # noqa: S608 — seul un fragment constant est interpolé.
)


def answer_key(run_id: UUID, fingerprint: str) -> str:
    """Identifiant stable d'une réponse : son run, puis un condensé de son empreinte.

    Il se recalcule à partir du graphe et ne recopie aucun texte de l'émetteur.
    """
    return run_id.hex + digest(fingerprint).hex()


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
    reader: Reader,
    channel: str,
    message_ref: str,
    limit: int,
    known_at: datetime | None = None,
) -> list[AnswerSummary]:
    """Réponses diffusées par ce message, les plus anciennes d'abord, ``limit`` au plus.

    Avec ``known_at``, celles que le graphe connaissait à cette date.
    """
    found = await reader.fetch(
        _BY_MESSAGE,
        {"channel": channel, "message_ref": message_ref, "known_at": known_at},
        limit=limit,
    )
    return [_summary(row) for row in found.rows]


async def find_answer(
    reader: Reader, answer_id: str, known_at: datetime | None = None
) -> AnswerSummary | None:
    """La réponse que désigne cet identifiant, déjà validé par la route.

    Avec ``known_at``, une réponse enregistrée après cette date n'est pas trouvée.
    """
    run_id = UUID(hex=answer_id[:32])
    found = await reader.fetch(
        _BY_KEY,
        {"run_id": run_id, "digest": bytes.fromhex(answer_id[32:]), "known_at": known_at},
        limit=1,
    )
    return _summary(found.rows[0]) if found.rows else None


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
                "last": row.last_event_time,
            },
        )
        if row.version is not None:
            source["versions"].add(row.version)
        # La lecture la moins précise l'emporte : un jeu lu en entier une
        # seule fois est une source en entier.
        source["precision"] = min(source["precision"], _PRECISIONS.index(row.precision))
        source["columns"].update(row.columns)
        source["first"] = min(source["first"], row.event_time)
        source["last"] = max(source["last"], row.last_event_time)
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


async def sources(
    reader: Reader, answer: AnswerSummary, known_at: datetime | None = None
) -> Sources:
    """Jeux de données, versions, outils et agents intervenus dans la réponse.

    Avec ``known_at``, ce que le graphe en connaissait à cette date.
    """
    children = await reader.fetch(
        _RUN_TREE, {"root": answer.run_id, "known_at": known_at}, limit=MAX_RUNS - 1
    )
    run_ids = [answer.run_id, *(row.run_id for row in children.rows)]

    agents: set[tuple[str, str]] = set()
    calls: dict[tuple[str, str], int] = {}
    actors = await reader.fetch(_ACTORS, {"run_ids": run_ids}, limit=len(run_ids))
    for row in actors.rows:
        if row.agent_name is not None:
            agents.add((row.agent_namespace, row.agent_name))
        if row.tool_name is not None:
            tool = (row.tool_server, row.tool_name)
            calls[tool] = calls.get(tool, 0) + 1
    reads = await reader.fetch(_READS, {"run_ids": run_ids, "known_at": known_at}, limit=MAX_READS)

    return Sources(
        answer=answer,
        agents=[AgentName(namespace=namespace, name=name) for namespace, name in sorted(agents)],
        tools=[
            ToolUse(server=server, name=name, calls=count)
            for (server, name), count in sorted(calls.items())
        ],
        datasets=_datasets(reads.rows),
        truncated=children.truncated or actors.truncated or reads.truncated,
        known_at=known_at,
    )
