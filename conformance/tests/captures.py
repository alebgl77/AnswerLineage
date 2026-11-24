"""Captures d'exemple pour les tests du kit.

``request()`` est ce qu'un adaptateur d'agents émet pour une demande : le run
de l'agent, un appel d'outil rattaché à lui, la table lue, la réponse et sa
diffusion. ``captured_call()`` est ce qu'un point de capture émet pour un appel
d'outil : son contexte de trace, et l'identifiant de run qui en dérive.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

from answerlineage_conformance.canaries import Canary, Role
from answerlineage_conformance.content import Evidence
from answerlineage_conformance.controls import Profile
from answerlineage_conformance.judgement import Judgement, Problem, judge
from answerlineage_conformance.sources import FileRole, SourceFile, source

PRODUCER = "https://exemple.org/adaptateur"
RUN_EVENT = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
OWN = "https://raw.githubusercontent.com/alebgl77/AnswerLineage/main/spec/facets/1-0-0/"
STANDARD = "https://openlineage.io/spec/facets/"
# Heure de référence des tests : aucun ne dépend de l'horloge.
RECEIVED_AT = datetime(2026, 11, 3, tzinfo=UTC)

AGENT_RUN = "0193a9c5-7a10-7c3e-9d5e-2f1b6c8a0001"
TOOL_RUN = "0193a9c5-7a10-7c3e-9d5e-2f1b6c8a0002"
OTHER_RUN = "0193a9c5-7a10-7c3e-9d5e-2f1b6c8a0003"
AGENT_JOB = {"namespace": "agent://ventes", "name": "analyste-ventes"}

# L'exemple de docs/proxy.md : ce contexte de trace donne cet identifiant de run.
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"
DERIVED_RUN = "a7e19836-59d9-8033-bd72-8a4bccb7271f"


def own(name: str, version: str = "1-0-0", **fields: Any) -> dict[str, Any]:
    """Une facette propre, à l'adresse publiée de son schéma."""
    address = OWN.replace("1-0-0", version) + f"{name}.json#/$defs/{name}"
    return {"_producer": PRODUCER, "_schemaURL": address, **fields}


def standard(path: str, **fields: Any) -> dict[str, Any]:
    """Une facette standard d'OpenLineage."""
    return {"_producer": PRODUCER, "_schemaURL": STANDARD + path, **fields}


def event(
    kind: str, second: int, run: dict[str, Any], job: dict[str, str], **sides: Any
) -> dict[str, Any]:
    return {
        "eventType": kind,
        "eventTime": f"2026-11-02T09:00:{second:02d}.000000Z",
        "producer": PRODUCER,
        "schemaURL": RUN_EVENT,
        "run": run,
        "job": job,
        "inputs": sides.get("inputs", []),
        "outputs": sides.get("outputs", []),
    }


def parent(run_id: str = AGENT_RUN, job: dict[str, str] | None = None) -> dict[str, Any]:
    return standard(
        "1-2-0/ParentRunFacet.json#/$defs/ParentRunFacet",
        run={"runId": run_id},
        job=dict(job or AGENT_JOB),
    )


def agent_start(run_id: str = AGENT_RUN, **facets: Any) -> dict[str, Any]:
    run = {"runId": run_id, "facets": {"answerlineage_agent": own("AgentRunFacet"), **facets}}
    return event("START", 0, run, dict(AGENT_JOB))


def table_read() -> dict[str, Any]:
    return {
        "namespace": "bigquery",
        "name": "finance.ref.taux_change",
        "facets": {
            "schema": standard(
                "1-2-0/SchemaDatasetFacet.json#/$defs/SchemaDatasetFacet",
                fields=[{"name": "devise"}, {"name": "taux"}],
            )
        },
    }


def tool_call(run_id: str = TOOL_RUN, **facet_fields: Any) -> dict[str, Any]:
    """L'appel d'outil, run enfant de celui de l'agent, avec la table qu'il a lue."""
    run = {
        "runId": run_id,
        "facets": {
            "parent": parent(),
            "answerlineage_toolCall": own(
                "ToolCallRunFacet", server="entrepot", tool="execute_sql", **facet_fields
            ),
        },
    }
    job = {"namespace": "mcp://entrepot", "name": "execute_sql"}
    return event("COMPLETE", 1, run, job, inputs=[table_read()])


# Deux destinataires, sous la seule forme admise : un pseudonyme HMAC.
PSEUDONYMS = ("hmac-sha256:" + "8f3a" * 16, "hmac-sha256:" + "c41d" * 16)


def answer(**fields: Any) -> dict[str, Any]:
    """La réponse de l'agent : une sortie décrite par son empreinte et sa diffusion."""
    described = {
        "fingerprint": "sha256:" + "5b" * 32,
        "channel": "mail",
        "messageRef": "m-1",
        "recipients": list(PSEUDONYMS),
        **fields,
    }
    return {
        "namespace": AGENT_JOB["namespace"],
        "name": f"{AGENT_JOB['name']}/reponses",
        "outputFacets": {"answerlineage_delivery": own("DeliveryOutputDatasetFacet", **described)},
    }


def agent_complete(run_id: str = AGENT_RUN, **facets: Any) -> dict[str, Any]:
    run = {"runId": run_id, "facets": {"answerlineage_agent": own("AgentRunFacet"), **facets}}
    return event("COMPLETE", 2, run, dict(AGENT_JOB), outputs=[answer()])


def request() -> list[dict[str, Any]]:
    """Une demande complète, dans l'ordre où ses événements se produisent."""
    return [agent_start(), tool_call(), agent_complete()]


def captured_call(
    trace_id: str | None = TRACE_ID,
    span_id: str | None = SPAN_ID,
    run_id: str = DERIVED_RUN,
    **facet_fields: Any,
) -> dict[str, Any]:
    """Un appel d'outil vu par un point de capture, qui ne connaît pas l'agent."""
    context = {"traceId": trace_id, "spanId": span_id, "traceOrigin": "meta"}
    cited = {name: value for name, value in context.items() if value is not None}
    described = own("ToolCallRunFacet", server="entrepot", tool="execute_sql", **cited)
    described.update(facet_fields)
    run = {"runId": run_id, "facets": {"answerlineage_toolCall": described}}
    job = {"namespace": "mcp://entrepot", "name": "execute_sql"}
    return event("COMPLETE", 1, run, job, inputs=[table_read()])


def mutated(
    events: list[dict[str, Any]], rank: int, path: list[str | int], value: Any = ...
) -> Any:
    """Copie de la capture où un champ d'un événement est remplacé, ou retiré sans valeur."""
    changed = copy.deepcopy(events)
    target: Any = changed[rank]
    for key in path[:-1]:
        target = target[key]
    if value is ...:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return changed


def moved(
    events: list[dict[str, Any]], rank: int, origin: list[str | int], destination: list[str | int]
) -> Any:
    """Copie de la capture où un champ d'un événement change de place ou de nom."""
    changed = copy.deepcopy(events)
    target: Any = changed[rank]
    for key in origin[:-1]:
        target = target[key]
    value = target.pop(origin[-1])
    target = changed[rank]
    for key in destination[:-1]:
        target = target.setdefault(key, {}) if isinstance(target, dict) else target[key]
    target[destination[-1]] = value
    return changed


def judged(
    events: list[Any], *, profile: Profile = "adapter", evidence: Evidence | None = None
) -> Judgement:
    return judge(events, profile=profile, received_at=RECEIVED_AT, evidence=evidence)


def found(
    events: list[Any],
    group: str,
    *,
    profile: Profile = "adapter",
    evidence: Evidence | None = None,
) -> list[tuple[str, int | None, str | None]]:
    """Les problèmes d'un groupe : code, rang de l'événement, pointeur."""
    return [
        (problem.code, problem.event, problem.pointer)
        for problem in judged(events, profile=profile, evidence=evidence).problems
        if problem.group == group
    ]


def reasons(problems: tuple[Problem, ...], code: str) -> list[tuple[str | None, str | None]]:
    """Pointeur et motif des problèmes d'un code."""
    return [(problem.pointer, problem.reason) for problem in problems if problem.code == code]


# Canaris fixes : le tirage est donné, pour que les tests ne dépendent pas du hasard.
DRAWS: dict[Role, str] = {
    "prompt": "0f1e2d3c4b5a",
    "tool_result": "6978695a4b3c",
    "answer": "a1b2c3d4e5f6",
}
MARKERS: dict[Role, str] = {"prompt": "prompt", "tool_result": "resultat", "answer": "reponse"}
CANARIES = tuple(Canary(role, f"canari-{MARKERS[role]}-{draw}") for role, draw in DRAWS.items())
PROMPT, TOOL_RESULT, ANSWER = (canary.value for canary in CANARIES)


def text_file(role: FileRole, name: str, content: str | bytes) -> SourceFile:
    data = content if isinstance(content, bytes) else content.encode("utf-8")
    return source(role, name, data)


def witness(*seen: str) -> SourceFile:
    """Le témoin : l'échange tel que l'utilisateur et l'outil l'ont vu."""
    lines = seen or (
        f"utilisateur : Quel est le taux du jour ? {PROMPT}",
        f"outil : {{'taux': 1.08, 'note': '{TOOL_RESULT}'}}",
        f"agent : Le taux est de 1,08. {ANSWER}",
    )
    return text_file("witness", "echange.txt", "\n".join(lines) + "\n")


def evidence(
    logs: str | bytes | None = None,
    metrics: str | bytes | None = None,
    seen: SourceFile | None = None,
) -> Evidence:
    return Evidence(
        canaries=CANARIES,
        witness=seen or witness(),
        logs=() if logs is None else (text_file("logs", "agent.log", logs),),
        metrics=() if metrics is None else (text_file("metrics", "metriques.txt", metrics),),
    )
