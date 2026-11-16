"""Un appelant et un appelé jouets de l'extension A2A, écrits d'après ``docs/a2a.md`` seul.

Rien n'y vient d'AnswerLineage : ni la suite de conformité, ni le proxy, ni
l'API. La dérivation de l'identifiant de run est refaite d'après la page du
proxy, les schémas sont lus dans ``spec/a2a/v1/`` et appliqués avec
``jsonschema``, comme le ferait une équipe qui ne lit pas le code. Leurs
échanges doivent passer la suite de l'extension : c'est la preuve qu'elle
s'implémente à partir de sa spécification.

Les deux agents parlent A2A 1.0 sous sa forme JSON : une requête capturée est
``{"serviceParameters": …, "params": SendMessageRequest}``, une réponse
``{"serviceParameters": …, "result": SendMessageResponse}``.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator

URI = "https://github.com/alebgl77/AnswerLineage/tree/main/spec/a2a/v1"
SPEC = Path(__file__).resolve().parents[2] / "spec" / "a2a" / "v1"
EXTENSIONS = "A2A-Extensions"
MAX_DATASETS = 100
# Plus précise vers la fin : un jeu lu plusieurs fois garde la moins précise.
PRECISIONS = ("table", "columns", "subset", "keys")
PRODUCER = "https://exemple.org/agents-jouets"
RUN_EVENT = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
PARENT = "https://openlineage.io/spec/facets/1-2-0/ParentRunFacet.json#/$defs/ParentRunFacet"
FACETS = "https://raw.githubusercontent.com/alebgl77/AnswerLineage/main/spec/facets/1-0-0/"

# La forme d'un nom, réduite à ce qu'un jouet sait vérifier : ni blanc, ni
# contrôle, ni balise. Plus stricte que l'ingestion n'est pas un défaut.
_NAME = re.compile(r"[^\s<>\"'`\x00-\x1f\x7f-\x9f]+")


def schema(name: str) -> Draft202012Validator:
    return Draft202012Validator(json.loads((SPEC / f"{name}.json").read_text("utf-8")))


def run_id_of(traceparent: str) -> str:
    """L'identifiant du run de l'appel, par la dérivation que décrit ``docs/proxy.md``."""
    _, trace_id, parent_id, _ = traceparent.split("-")
    digest = hashlib.sha256(
        b"answerlineage/run-id/v1" + bytes.fromhex(trace_id) + bytes.fromhex(parent_id)
    ).digest()
    octets = bytearray(digest[:16])
    octets[6] = (octets[6] & 0x0F) | 0x80
    octets[8] = (octets[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(octets)))


def extensions_of(parameters: object) -> list[str]:
    """Les URI que nomment les paramètres de service, sans égard à la casse de leur nom."""
    if not isinstance(parameters, dict):
        return []
    return [
        item.strip()
        for key, value in parameters.items()
        if key.lower() == EXTENSIONS.lower() and isinstance(value, str)
        for item in value.split(",")
        if item.strip()
    ]


def is_name(value: object) -> bool:
    return (
        isinstance(value, str) and _NAME.fullmatch(unicodedata.normalize("NFC", value)) is not None
    )


@dataclass(frozen=True)
class Read:
    """Ce que l'appelé a lu : un jeu de données, sa version, la précision de la lecture."""

    namespace: str
    name: str
    version: str | None = None
    precision: str = "table"


@dataclass
class ToyCallee:
    """Un agent appelé : il déclare l'extension, rattache son run et rend un résumé."""

    run_id: str
    reads: Sequence[Read] = ()
    unresolved: int = 0
    shape: Literal["task", "message"] = "task"
    answer: str = "Le taux retenu pour novembre est de 1,0842 dollar pour un euro."
    call_job: dict[str, str] = field(
        default_factory=lambda: {"namespace": "a2a://change", "name": "taux-du-mois"}
    )
    # Le parent qu'il a donné à son run, quand il l'a rattaché.
    parent: str | None = None

    def card(self) -> dict[str, Any]:
        return {
            "name": "Agent des taux de change",
            "description": "Donne le taux de change retenu pour un mois.",
            "version": "1.0.0",
            "capabilities": {
                "streaming": False,
                "extensions": [
                    {
                        "uri": URI,
                        "description": "Rattache son run à celui de l'appel ; rend un résumé.",
                        "required": False,
                        "params": {"callJob": dict(self.call_job)},
                    }
                ],
            },
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            "skills": [],
        }

    def _attachment(self, context: object) -> dict[str, str]:
        if context is None:
            return {"attachment": "missing"}
        if not schema("CallerContext").is_valid(context):
            return {"attachment": "rejected", "rejection": "context_malformed"}
        assert isinstance(context, dict)
        if run_id_of(context["traceparent"]) != context["runId"]:
            return {"attachment": "rejected", "rejection": "run_id_not_derived"}
        self.parent = context["runId"]
        return {"attachment": "attached", "parentRunId": context["runId"]}

    def _datasets(self) -> list[dict[str, str]]:
        merged: dict[tuple[str, str, str | None], str] = {}
        for read in self.reads:
            key = (read.namespace, read.name, read.version)
            known = merged.get(key, read.precision)
            merged[key] = min(known, read.precision, key=PRECISIONS.index)
        listed = []
        for (namespace, name, version), precision in sorted(
            merged.items(), key=lambda item: (item[0][0], item[0][1], item[0][2] or "")
        ):
            entry = {"namespace": namespace, "name": name, "precision": precision}
            if version is not None:
                entry["version"] = version
            listed.append(entry)
        return listed

    def summary(self, context: object) -> dict[str, Any]:
        datasets = self._datasets()
        return {
            "runId": self.run_id,
            **self._attachment(context),
            "datasets": datasets[:MAX_DATASETS],
            "datasetCount": len(datasets),
            "unresolvedReads": self.unresolved,
            "truncated": len(datasets) > MAX_DATASETS,
        }

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        """Répond à une requête capturée ; le lignage ne fait jamais échouer l'appel."""
        message = request["params"]["message"]
        reply: dict[str, Any] = {
            "messageId": "reponse-1",
            "role": "ROLE_AGENT",
            "parts": [{"text": self.answer}],
        }
        if URI not in extensions_of(request.get("serviceParameters")):
            return {"serviceParameters": {}, "result": {"message": reply}}
        metadata = message.get("metadata")
        context = metadata.get(URI) if isinstance(metadata, dict) else None
        lineage = {URI: self.summary(context)}
        if self.shape == "message":
            result: dict[str, Any] = {
                "message": {**reply, "extensions": [URI], "metadata": lineage}
            }
        else:
            result = {
                "task": {
                    "id": "tache-1",
                    "contextId": "contexte-1",
                    "status": {"state": "TASK_STATE_COMPLETED"},
                    "artifacts": [{"artifactId": "reponse", "parts": [{"text": self.answer}]}],
                    "metadata": lineage,
                }
            }
        return {"serviceParameters": {EXTENSIONS: URI}, "result": result}


@dataclass
class ToyCaller:
    """Un agent appelant : il active l'extension si l'appelé la déclare, et lit le résumé."""

    traceparent: str = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    question: str = "Quel taux de change retenir pour les ventes de novembre ?"

    @property
    def run_id(self) -> str:
        return run_id_of(self.traceparent)

    @staticmethod
    def declaration(card: dict[str, Any]) -> dict[str, Any] | None:
        for entry in card.get("capabilities", {}).get("extensions", []):
            if entry.get("uri") == URI and schema("AgentExtension").is_valid(entry):
                return dict(entry)
        return None

    def request(self, card: dict[str, Any]) -> dict[str, Any]:
        message: dict[str, Any] = {
            "messageId": "question-1",
            "role": "ROLE_USER",
            "parts": [{"text": self.question}],
        }
        parameters = {"A2A-Version": "1.0"}
        if self.declaration(card) is not None:
            parameters[EXTENSIONS] = URI
            message["extensions"] = [URI]
            message["metadata"] = {URI: {"traceparent": self.traceparent, "runId": self.run_id}}
        return {"serviceParameters": parameters, "params": {"message": message}}

    def summary(self, response: dict[str, Any]) -> dict[str, Any] | None:
        """Le résumé reçu, s'il est valable ; sinon rien, et l'appel reste ce qu'il est."""
        if URI not in extensions_of(response.get("serviceParameters")):
            return None
        result = response.get("result", {})
        holder = result.get("task") or result.get("message") or {}
        found = holder.get("metadata", {}).get(URI)
        if not schema("LineageSummary").is_valid(found):
            return None
        names = [
            value
            for dataset in found["datasets"]
            for key in ("namespace", "name", "version")
            if (value := dataset.get(key)) is not None
        ]
        if not all(is_name(value) for value in names):
            return None
        attached = found["attachment"] == "attached"
        if (attached and found["parentRunId"] != self.run_id) or found["runId"] == self.run_id:
            return None
        return dict(found)


def exchange(caller: ToyCaller, callee: ToyCallee) -> tuple[dict[str, Any], dict[str, Any]]:
    """Ce qu'un intégrateur capture : la carte de l'appelé, puis la requête et la réponse."""
    card = callee.card()
    request = caller.request(card)
    return card, {"request": request, "response": callee.handle(request)}


def _facet(name: str, **fields: Any) -> dict[str, Any]:
    return {"_producer": PRODUCER, "_schemaURL": f"{FACETS}{name}.json#/$defs/{name}", **fields}


def _event(kind: str, second: int, run: dict[str, Any], job: dict[str, str]) -> dict[str, Any]:
    return {
        "eventType": kind,
        "eventTime": f"2026-11-02T09:00:{second:02d}.000000Z",
        "producer": PRODUCER,
        "schemaURL": RUN_EVENT,
        "run": run,
        "job": job,
        "inputs": [],
        "outputs": [],
    }


def _parent(run_id: str, job: dict[str, str]) -> dict[str, Any]:
    return {"_producer": PRODUCER, "_schemaURL": PARENT, "run": {"runId": run_id}, "job": job}


def lineage_events(
    caller: ToyCaller, callee: ToyCallee, *, agent_run: str, tool_run: str
) -> list[dict[str, Any]]:
    """Les événements OpenLineage des deux agents, d'après docs/a2a.md et docs/facettes.md.

    L'appelant émet son run et le run de son appel, enfant du sien, sous le job
    que la carte déclare ; l'appelé émet son run, rattaché au run de l'appel,
    et l'appel d'outil qui a lu la table.
    """
    declaration = ToyCaller.declaration(callee.card())
    assert declaration is not None
    call_job = declaration["params"]["callJob"]
    agent_job = {"namespace": "agent://ventes", "name": "analyste-ventes"}
    callee_job = {"namespace": "agent://change", "name": "agent-des-taux"}
    agent = {"answerlineage_agent": _facet("AgentRunFacet")}
    call_parent = {"parent": _parent(agent_run, agent_job)}
    events = [
        _event("START", 0, {"runId": agent_run, "facets": agent}, agent_job),
        _event("START", 1, {"runId": caller.run_id, "facets": call_parent}, call_job),
    ]
    callee_facets = dict(agent)
    if callee.parent is not None:
        callee_facets["parent"] = _parent(callee.parent, call_job)
    events.append(
        _event("COMPLETE", 3, {"runId": callee.run_id, "facets": callee_facets}, callee_job)
    )
    tool = _event(
        "COMPLETE",
        2,
        {
            "runId": tool_run,
            "facets": {
                "parent": _parent(callee.run_id, callee_job),
                "answerlineage_toolCall": _facet("ToolCallRunFacet", server="entrepot"),
            },
        },
        {"namespace": "mcp://entrepot", "name": "execute_sql"},
    )
    tool["inputs"] = [{"namespace": read.namespace, "name": read.name} for read in callee.reads]
    events.append(tool)
    events.append(_event("COMPLETE", 4, {"runId": caller.run_id, "facets": call_parent}, call_job))
    answer = _event("COMPLETE", 5, {"runId": agent_run, "facets": agent}, agent_job)
    answer["outputs"] = [
        {
            "namespace": agent_job["namespace"],
            "name": "analyste-ventes/reponses",
            "outputFacets": {
                "answerlineage_delivery": _facet(
                    "DeliveryOutputDatasetFacet",
                    fingerprint="sha256:" + "5b1d" * 16,
                    channel="mail",
                    messageRef="m-1",
                    recipients=["8f3a"],
                )
            },
        }
    ]
    events.append(answer)
    return events
