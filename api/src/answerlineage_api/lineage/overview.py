"""Vue d'ensemble du graphe, en cinq colonnes.

La vue part des réponses les plus récentes et ne montre que ce qu'elles
touchent : les jeux de données lus pour les produire, les outils appelés, les
agents qui ont répondu et les destinataires. Chaque réponse porte sa propre
chaîne, pour que l'on puisse la suivre d'une colonne à l'autre.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import Row, TextClause, text

from answerlineage_api.lineage.models import (
    AgentNode,
    AnswerNode,
    AnswerRead,
    AnswerSummary,
    DatasetList,
    DatasetName,
    DatasetNode,
    GraphView,
    RecipientNode,
    ToolNode,
)
from answerlineage_api.lineage.paging import (
    AFTER_CURSOR,
    ANSWER_DIGEST,
    ANSWER_ORDER,
    Cursor,
)
from answerlineage_api.lineage.reading import Reader
from answerlineage_api.lineage.trace import answer_key

# Borne des runs parcourus pour toutes les réponses d'une vue.
MAX_RUNS = 20_000
# Borne des couples (run, jeu de données lu) de ces runs.
MAX_READS = 50_000
# Borne des liens de diffusion de ces réponses : canal et destinataire.
MAX_LINKS = 50_000

# Colonnes d'une réponse choisie pour une vue : voir ``view_of``.
ANSWER_COLUMNS = f"""a.id, a.run_id, a.fingerprint, a.event_time, {ANSWER_DIGEST} AS digest,
           g.namespace AS agent_namespace, g.name AS agent_name"""

_DATASETS = text(
    """
    SELECT namespace, name, kind FROM dataset ORDER BY namespace, name LIMIT :limit
    """
)

_ONE_ANSWER = text(
    f"""
    SELECT {ANSWER_COLUMNS}
    FROM answer a
    JOIN run r ON r.run_id = a.run_id
    LEFT JOIN agent g ON g.id = r.agent_id
    WHERE a.run_id = :run_id AND a.fingerprint = :fingerprint
    LIMIT :limit
    """  # noqa: S608 — seules des colonnes constantes sont interpolées.
)


def _recent_answers(cursor: Cursor | None) -> TextClause:
    after = "" if cursor is None else f"WHERE {AFTER_CURSOR}"
    return text(
        f"""
        SELECT {ANSWER_COLUMNS}
        FROM answer a
        JOIN run r ON r.run_id = a.run_id
        LEFT JOIN agent g ON g.id = r.agent_id
        {after}
        ORDER BY {ANSWER_ORDER}
        LIMIT :limit
        """  # noqa: S608 — seuls des fragments constants sont interpolés.
    )


# UNION, et non UNION ALL : des runs qui se désignent l'un l'autre comme parent
# ne font pas tourner la recherche sans fin.
_RUN_TREES = text(
    """
    WITH RECURSIVE tree(root, run_id) AS (
        SELECT root, root FROM unnest(CAST(:roots AS uuid[])) AS root
        UNION
        SELECT tree.root, child.run_id FROM run child JOIN tree ON child.parent_run_id = tree.run_id
    )
    SELECT root, run_id FROM tree LIMIT :limit
    """
)

# Une ligne par run demandé au plus : la borne est celle des arbres.
_TOOLS = text(
    """
    SELECT r.run_id, t.server, t.name
    FROM run r JOIN tool t ON t.id = r.tool_id
    WHERE r.run_id = ANY(:run_ids)
    ORDER BY r.run_id
    LIMIT :limit
    """
)

_READS = text(
    """
    SELECT DISTINCT e.run_id, d.namespace, d.name, d.kind
    FROM read_edge e JOIN dataset d ON d.id = e.dataset_id
    WHERE e.run_id = ANY(:run_ids)
    ORDER BY e.run_id, d.namespace, d.name
    LIMIT :limit
    """
)

_DELIVERIES = text(
    """
    SELECT d.answer_id, d.channel, c.pseudonym
    FROM delivery d
    LEFT JOIN delivery_recipient l ON l.delivery_id = d.id
    LEFT JOIN recipient c ON c.id = l.recipient_id
    WHERE d.answer_id = ANY(:answer_ids)
    ORDER BY d.answer_id, d.channel, c.pseudonym
    LIMIT :limit
    """
)

type _Key = tuple[str, str]


def _ranks[T](items: set[T]) -> dict[T, int]:
    return {item: rank for rank, item in enumerate(sorted(items))}  # type: ignore[type-var]


async def overview(reader: Reader, *, limit: int, cursor: Cursor | None) -> GraphView:
    """Les ``limit`` réponses les plus récentes, après ``cursor``, et ce qu'elles touchent."""
    params = {} if cursor is None else cursor.parameters()
    picked = await reader.fetch(_recent_answers(cursor), params, limit=limit)
    return await view_of(reader, picked.rows, more=picked.truncated)


async def datasets(reader: Reader, *, limit: int) -> DatasetList:
    """Les jeux de données connus, par espace de noms puis par nom."""
    found = await reader.fetch(_DATASETS, {}, limit=limit)
    return DatasetList(
        datasets=[
            DatasetName(namespace=row.namespace, name=row.name, kind=row.kind) for row in found.rows
        ],
        more=found.truncated,
    )


async def view_of_answer(reader: Reader, answer: AnswerSummary) -> GraphView:
    """La vue en cinq colonnes d'une seule réponse : sa chaîne."""
    picked = await reader.fetch(
        _ONE_ANSWER, {"run_id": answer.run_id, "fingerprint": answer.fingerprint}, limit=1
    )
    return await view_of(reader, picked.rows, more=False)


async def view_of(reader: Reader, picked: Sequence[Row[Any]], *, more: bool) -> GraphView:
    """La vue en cinq colonnes de ces réponses : leurs chaînes, et rien d'autre.

    Chaque ligne porte les colonnes de ``ANSWER_COLUMNS``. Quand ``more`` est
    vrai, la vue donne le curseur qui suit la dernière réponse.
    """
    roots: list[UUID] = sorted({row.run_id for row in picked}, key=lambda run_id: run_id.hex)

    trees = await reader.fetch(_RUN_TREES, {"roots": roots}, limit=MAX_RUNS)
    roots_of: dict[UUID, set[UUID]] = {}
    for row in trees.rows:
        roots_of.setdefault(row.run_id, set()).add(row.root)
    run_ids = list(roots_of)

    # Par run racine : les outils appelés et les lectures, avec l'outil qui les a faites.
    tool_of: dict[UUID, _Key] = {}
    tools_of_root: dict[UUID, set[_Key]] = {}
    tools = await reader.fetch(_TOOLS, {"run_ids": run_ids}, limit=len(run_ids))
    for row in tools.rows:
        tool_of[row.run_id] = (row.server, row.name)
        for root in roots_of[row.run_id]:
            tools_of_root.setdefault(root, set()).add((row.server, row.name))
    kinds: dict[_Key, str] = {}
    reads_of_root: dict[UUID, set[tuple[_Key, _Key | None]]] = {}
    reads = await reader.fetch(_READS, {"run_ids": run_ids}, limit=MAX_READS)
    for row in reads.rows:
        dataset = (row.namespace, row.name)
        kinds[dataset] = row.kind
        for root in roots_of[row.run_id]:
            reads_of_root.setdefault(root, set()).add((dataset, tool_of.get(row.run_id)))

    channels: dict[int, set[str]] = {}
    recipients_of: dict[int, set[str]] = {}
    answer_ids = [row.id for row in picked]
    deliveries = await reader.fetch(_DELIVERIES, {"answer_ids": answer_ids}, limit=MAX_LINKS)
    for row in deliveries.rows:
        channels.setdefault(row.answer_id, set()).add(row.channel)
        if row.pseudonym is not None:
            recipients_of.setdefault(row.answer_id, set()).add(row.pseudonym)

    datasets = _ranks(set(kinds))
    tool_ranks = _ranks({tool for used in tools_of_root.values() for tool in used})
    agents = _ranks(
        {(row.agent_namespace, row.agent_name) for row in picked if row.agent_name is not None}
    )
    recipients = _ranks({pseudonym for found in recipients_of.values() for pseudonym in found})

    answers = [
        AnswerNode(
            id=answer_key(row.run_id, row.fingerprint),
            fingerprint=row.fingerprint,
            produced_at=row.event_time,
            agent=(
                f"agent-{agents[(row.agent_namespace, row.agent_name)]}"
                if row.agent_name is not None
                else None
            ),
            channels=sorted(channels.get(row.id, ())),
            recipients=[
                f"recipient-{rank}"
                for rank in sorted(recipients[p] for p in recipients_of.get(row.id, ()))
            ],
            tools=[
                f"tool-{rank}"
                for rank in sorted(tool_ranks[tool] for tool in tools_of_root.get(row.run_id, ()))
            ],
            reads=[
                AnswerRead(
                    dataset=f"dataset-{dataset}", tool=None if tool is None else f"tool-{tool}"
                )
                for dataset, tool in sorted(
                    {
                        (datasets[dataset], None if tool is None else tool_ranks[tool])
                        for dataset, tool in reads_of_root.get(row.run_id, ())
                    },
                    key=lambda read: (read[0], -1 if read[1] is None else read[1]),
                )
            ],
        )
        for row in picked
    ]
    return GraphView(
        datasets=[
            DatasetNode(id=f"dataset-{rank}", namespace=key[0], name=key[1], kind=kinds[key])
            for key, rank in datasets.items()
        ],
        tools=[
            ToolNode(id=f"tool-{rank}", server=key[0], name=key[1])
            for key, rank in tool_ranks.items()
        ],
        agents=[
            AgentNode(id=f"agent-{rank}", namespace=key[0], name=key[1])
            for key, rank in agents.items()
        ],
        answers=answers,
        recipients=[
            RecipientNode(id=f"recipient-{rank}", pseudonym=pseudonym)
            for pseudonym, rank in recipients.items()
        ],
        more=more,
        next_cursor=Cursor.after(picked[-1]).encode() if more and picked else None,
        truncated=trees.truncated or tools.truncated or reads.truncated or deliveries.truncated,
    )
