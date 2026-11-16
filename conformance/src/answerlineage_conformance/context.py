"""Contexte : ce qui relie les événements entre eux, selon le devoir de l'émetteur.

Un adaptateur d'agents connaît l'agent : chaque appel d'outil se rattache à son
run par la facette standard ``parent``, et la capture porte une chaîne
complète, du jeu de données lu à la réponse diffusée.

Un point de capture ne connaît pas l'agent : il ne doit aucun parent, mais
chaque appel cite son contexte de trace, et son identifiant de run est celui
que la dérivation publiée donne pour ce contexte. C'est ce qui permet à un
adaptateur qui voit le même contexte d'émettre pour le même run.

Les événements de deux émetteurs pour un même run se complètent : le devoir se
juge par run, pas par événement.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from answerlineage_api.ingest.models import TOOL_CALL_FACET
from answerlineage_api.ingest.store import UnstorableEventError, event_identifier
from answerlineage_api.store.graph import RunRecord
from answerlineage_conformance.controls import Profile
from answerlineage_conformance.findings import (
    TERMINAL,
    Finding,
    canonical_uuid,
    mapping,
    run_facets,
    run_id,
)
from answerlineage_conformance.runid import derived_from

_PARENT = ("run", "facets", "parent")
_TOOL_CALL = ("run", "facets", TOOL_CALL_FACET)
_EVENT_TYPE = ("eventType",)
_RUN_ID = ("run", "runId")


@dataclass
class _Run:
    """Ce que les événements d'une capture disent d'un run."""

    first: int
    # Rangs des événements qui portent la facette d'un appel d'outil.
    tool_calls: list[int] = field(default_factory=list)
    has_parent: bool = False
    cites_trace: bool = False
    # Événements terminaux distincts : identité de l'événement, puis son rang.
    terminal: dict[str, int] = field(default_factory=dict)


def _identity(event: Mapping[str, Any], rank: int) -> str:
    """Identité d'un événement : celle de l'ingestion, pour qui un renvoi est un doublon."""
    try:
        return event_identifier(event)
    except UnstorableEventError:
        return f"rang-{rank}"


def runs(events: Sequence[object]) -> dict[UUID, _Run]:
    """Les runs de la capture, d'après les événements dont l'identifiant de run est lisible."""
    found: dict[UUID, _Run] = {}
    for rank, raw in enumerate(events):
        identifier = run_id(raw)
        if identifier is None:
            continue
        event = mapping(raw)
        run = found.setdefault(identifier, _Run(first=rank))
        facets = run_facets(event)
        tool_call = facets.get(TOOL_CALL_FACET)
        if isinstance(tool_call, Mapping):
            run.tool_calls.append(rank)
            if tool_call.get("traceId") is not None and tool_call.get("spanId") is not None:
                run.cites_trace = True
        if isinstance(facets.get("parent"), Mapping):
            run.has_parent = True
        if event.get("eventType") in TERMINAL:
            run.terminal.setdefault(_identity(event, rank), rank)
    return found


def _parents(events: Sequence[object], known: Mapping[UUID, _Run]) -> list[Finding]:
    """Adaptateur : chaque appel d'outil a un parent, et chaque parent mène à un run capturé."""
    findings: list[Finding] = []
    for rank, raw in enumerate(events):
        parent = run_facets(raw).get("parent")
        if not isinstance(parent, Mapping):
            continue
        where = (*_PARENT, "run", "runId")
        declared = mapping(parent.get("run")).get("runId")
        identifier = canonical_uuid(declared)
        if declared is None:
            findings.append(Finding("parent_run_id_missing", rank, where))
        elif identifier is None:
            findings.append(Finding("parent_run_id_not_canonical", rank, where))
        elif identifier not in known:
            findings.append(Finding("parent_not_in_capture", rank, where))
    findings.extend(
        Finding("parent_missing", run.tool_calls[0], _PARENT)
        for run in known.values()
        if run.tool_calls and not run.has_parent
    )
    return findings


def _capture_point(events: Sequence[object], known: Mapping[UUID, _Run]) -> list[Finding]:
    """Point de capture : rien que des appels d'outil, chacun avec son contexte de trace."""
    findings: list[Finding] = []
    for rank, raw in enumerate(events):
        if isinstance(raw, dict) and not isinstance(run_facets(raw).get(TOOL_CALL_FACET), Mapping):
            findings.append(Finding("not_a_tool_call", rank, _TOOL_CALL))
    for run in known.values():
        if not run.tool_calls or run.cites_trace:
            continue
        first = run.tool_calls[0]
        tool_call = mapping(run_facets(events[first]).get(TOOL_CALL_FACET))
        findings.extend(
            Finding("trace_context_missing", first, (*_TOOL_CALL, name))
            for name in ("traceId", "spanId")
            if tool_call.get(name) is None
        )
    return findings


def _derivation(events: Sequence[object]) -> list[Finding]:
    """Un contexte de trace cité est celui dont l'identifiant de run dérive."""
    findings: list[Finding] = []
    for rank, raw in enumerate(events):
        tool_call = mapping(run_facets(raw).get(TOOL_CALL_FACET))
        expected = derived_from(tool_call.get("traceId"), tool_call.get("spanId"))
        identifier = run_id(raw)
        # Un contexte mal formé relève du schéma de la facette, un identifiant de run
        # illisible de l'ingestion : ils sont déjà rapportés.
        if expected is not None and identifier is not None and identifier != expected:
            findings.append(Finding("run_id_not_derived", rank, _RUN_ID))
    return findings


def _terminal_events(known: Mapping[UUID, _Run]) -> list[Finding]:
    findings: list[Finding] = []
    for run in known.values():
        if not run.terminal:
            findings.append(Finding("run_without_terminal_event", run.first, _EVENT_TYPE))
        findings.extend(
            Finding("run_with_several_terminal_events", rank, _EVENT_TYPE)
            for rank in sorted(run.terminal.values())[1:]
        )
    return findings


@dataclass
class _Projected:
    """Ce que le graphe retiendrait d'un run, tous ses événements réunis."""

    agent: bool = False
    tool: bool = False
    reads: bool = False
    delivered: bool = False
    parents: set[UUID] = field(default_factory=set)


def _merged(records: Mapping[int, RunRecord]) -> dict[UUID, _Projected]:
    merged: dict[UUID, _Projected] = {}
    for record in records.values():
        run = merged.setdefault(record.run_id, _Projected())
        run.agent = run.agent or record.kind == "agent"
        run.tool = run.tool or record.kind == "tool"
        run.reads = run.reads or bool(record.reads)
        run.delivered = run.delivered or any(answer.deliveries for answer in record.answers)
        if record.parent_run_id is not None:
            run.parents.add(record.parent_run_id)
    return merged


def _answers(start: _Projected, merged: Mapping[UUID, _Projected]) -> bool:
    """Vrai quand un run d'agent qui diffuse une réponse est au-dessus de ``start``."""
    seen: set[UUID] = set()
    pending = list(start.parents)
    while pending:
        identifier = pending.pop()
        above = merged.get(identifier)
        if identifier in seen or above is None:
            continue
        seen.add(identifier)
        if above.agent and above.delivered:
            return True
        pending.extend(above.parents)
    return False


def has_complete_chain(records: Mapping[int, RunRecord]) -> bool:
    """Un jeu de données lu par un appel d'outil, sous un run d'agent qui diffuse une réponse.

    La chaîne se lit dans ce que le graphe retiendrait : elle peut passer par
    un sous-agent, comme la lecture du lignage remonte d'une réponse à ses
    sources à toute profondeur.
    """
    merged = _merged(records)
    return any(
        run.tool and not run.agent and run.reads and _answers(run, merged)
        for run in merged.values()
    )


def has_tool_call(records: Mapping[int, RunRecord]) -> bool:
    return any(record.kind == "tool" for record in records.values())


def context(
    events: Sequence[object], records: Mapping[int, RunRecord], profile: Profile
) -> list[Finding]:
    """Les problèmes de contexte de la capture, selon le profil de l'émetteur."""
    known = runs(events)
    findings = [*_derivation(events), *_terminal_events(known)]
    if profile == "adapter":
        findings.extend(_parents(events, known))
        if not has_complete_chain(records):
            findings.append(Finding("no_complete_chain"))
    else:
        findings.extend(_capture_point(events, known))
        if not has_tool_call(records):
            findings.append(Finding("no_tool_call"))
    return findings
