"""Vérité de l'entreprise synthétique : pour chaque incident, les réponses touchées.

La vérité se publie comme le reste : des empreintes, des références de message
et des pseudonymes, jamais de contenu.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from answerlineage_corpus.enterprise import Enterprise, Incident, Request


def _instant(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _answer(request: Request) -> dict[str, Any]:
    answer = request.answer
    if answer is None:  # pragma: no cover — une demande touchée a toujours une réponse.
        raise ValueError("Une demande sans réponse n'entre pas dans la vérité.")
    return {
        "fingerprint": answer.fingerprint,
        "channel": answer.channel,
        "messageRef": answer.message_ref,
        "recipients": list(answer.recipients),
        "agent": {"namespace": request.agent.namespace, "name": request.agent.name},
    }


def _incident(enterprise: Enterprise, incident: Incident) -> dict[str, Any]:
    answers = [_answer(request) for request in enterprise.touched(incident)]
    return {
        "name": incident.name,
        "dataset": {"namespace": incident.table.namespace, "name": incident.table.name},
        "from": _instant(incident.start),
        "to": _instant(incident.end),
        "answers": answers,
        "recipients": sorted(
            {pseudonym for answer in answers for pseudonym in answer["recipients"]}
        ),
    }


def truth(enterprise: Enterprise) -> dict[str, Any]:
    """Liste exacte des réponses et des destinataires touchés par chaque incident."""
    return {
        "seed": enterprise.seed,
        "incidents": [_incident(enterprise, incident) for incident in enterprise.incidents],
    }
