"""L'adaptateur Claude Agent SDK passe le kit de conformité, comme tout émetteur.

Une demande est jouée avec trois canaris : dans le prompt et les arguments de
l'outil, dans son résultat et un message d'erreur, dans la réponse de l'agent et
celle d'un sous-agent. Les crochets passent par le vrai protocole du SDK, la CLI
jouée par le test, sans modèle ni réseau, et les événements partent par la vraie
boîte d'envoi vers une API simulée. Le kit juge ensuite ce que l'API a reçu, le
journal au niveau le plus bavard, les sorties et les compteurs.

La capture de l'adaptateur porte l'agent, ses sous-agents et ses appels
d'outils, mais pas encore de chaîne complète : l'adaptateur ne décrit pas les
lectures de ses outils, et la diffusion de la réponse viendra avec les
destinataires pseudonymisés. Le kit le dit, et rien d'autre.
"""

from __future__ import annotations

import gzip
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from adapters.tests.fake_cli import FakeCli
from adapters.tests.test_claude_agent_sdk import (
    prompt_submitted,
    stopped,
    subagent_started,
    subagent_stopped,
    tool_failed,
    tool_used,
)
from answerlineage_adapters.claude_agent_sdk import ClaudeAgentLineage
from answerlineage_adapters.settings import AgentJob
from answerlineage_conformance import cli
from answerlineage_conformance.canaries import Searcher, canaries_document, new_canaries
from answerlineage_emitter.fingerprint import FingerprintKey
from answerlineage_emitter.outbox import Outbox, OutboxSettings

pytestmark = pytest.mark.anyio

AGENT = AgentJob.create("agent://finance", "analyste-change")
RECEIVED_AT = datetime(2026, 11, 3, tzinfo=UTC)


class Api:
    """L'API d'ingestion simulée : elle garde les événements reçus, tels que la boîte les envoie."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.events.extend(json.loads(gzip.decompress(request.content)))
        return httpx.Response(202, json={"accepted": 0, "duplicates": 0, "rejected": []})


async def test_three_canaries_cross_the_adapter_and_come_out_nowhere(
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    canaries = new_canaries()
    searcher = Searcher(canaries)
    in_prompt, in_result, in_answer = (canary.value for canary in canaries)
    prompt = f"Quel est le taux de change du jour ? {in_prompt}"
    # L'agent tire de la demande le littéral de sa requête : le prompt part dans l'argument.
    query = f"SELECT taux FROM finance.ref.taux_change WHERE devise = '{in_prompt}'"  # noqa: S608
    arguments = {"query": query}
    result = [{"type": "text", "text": f"taux : 1,08 ({in_result})"}]
    error = f"Délai dépassé en lisant {in_result}"
    answer = f"Le taux de change du jour est de 1,08. {in_answer}"

    api = Api()
    moments = iter(
        datetime(2026, 11, 2, 9, 0, tzinfo=UTC) + timedelta(seconds=n) for n in range(99)
    )
    with caplog.at_level(logging.DEBUG):
        async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
            box = Outbox(
                OutboxSettings.create("http://127.0.0.1:9", flush_seconds=5), client=client
            )
            adapter = ClaudeAgentLineage(
                AGENT,
                box,
                fingerprint_key=FingerprintKey(bytes(range(32))),
                clock=lambda: next(moments),
            )
            cli_side = FakeCli()
            async with adapter:
                options = ClaudeAgentOptions(hooks=adapter.hooks())
                async with ClaudeSDKClient(options=options, transport=cli_side):
                    await cli_side.fire("UserPromptSubmit", prompt_submitted(prompt))
                    await cli_side.fire(
                        "PostToolUse", tool_used(arguments=arguments, response=result)
                    )
                    await cli_side.fire("SubagentStart", subagent_started())
                    await cli_side.fire(
                        "PostToolUseFailure", {**tool_failed(error), "agent_id": "sous-1"}
                    )
                    await cli_side.fire(
                        "SubagentStop", {**subagent_stopped(), "last_assistant_message": answer}
                    )
                    await cli_side.fire("Stop", stopped(answer))
    output = capfd.readouterr()

    # Le parcours a bien eu lieu : l'agent, son sous-agent, deux appels, tout est arrivé.
    assert [event["eventType"] for event in api.events] == [
        "START",
        "COMPLETE",
        "START",
        "FAIL",
        "COMPLETE",
        "COMPLETE",
    ]
    assert box.stats.sent == 6
    witness = "\n".join(
        [
            f"utilisateur : {prompt}",
            f"appel reçu par l'outil : {json.dumps(arguments, ensure_ascii=False)}",
            f"résultat reçu par l'agent : {json.dumps(result, ensure_ascii=False)}",
            f"échec reçu par le sous-agent : {error}",
            f"réponse reçue par l'utilisateur : {answer}",
        ]
    )
    assert searcher.missing(witness) == []

    # Rien, nulle part, sous aucune forme.
    events = json.dumps(api.events, ensure_ascii=False)
    counters = "\n".join([repr(adapter), repr(adapter.stats), repr(box.stats)])
    places = {
        "les événements": events,
        "le journal": caplog.text,
        "la sortie standard": output.out,
        "la sortie d'erreur": output.err,
        "les compteurs": counters,
    }
    seen = {name: searcher.sightings(place) for name, place in places.items()}
    assert {name: sightings for name, sightings in seen.items() if sightings} == {}

    # Le verdict du kit, sur les fichiers qu'un intégrateur garderait de ce parcours.
    files = {
        "evenements.json": events,
        "canaris.json": json.dumps(canaries_document(canaries)),
        "echange.txt": witness,
        "journal.log": "\n".join([caplog.text, output.out, output.err]),
        "compteurs.txt": counters,
    }
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    status = cli.main(
        [
            "check",
            str(tmp_path / "evenements.json"),
            "--canaries",
            str(tmp_path / "canaris.json"),
            "--witness",
            str(tmp_path / "echange.txt"),
            "--logs",
            str(tmp_path / "journal.log"),
            "--metrics",
            str(tmp_path / "compteurs.txt"),
            "--received-at",
            RECEIVED_AT.isoformat(),
            "--format",
            "json",
        ]
    )
    report = json.loads(capfd.readouterr().out)
    # Forme, contexte, contenu, projection : seule manque la chaîne complète, que
    # l'adaptateur ne porte pas encore (ni lecture décrite, ni diffusion de la réponse).
    assert [problem["code"] for problem in report["problems"]] == ["no_complete_chain"]
    assert (status, report["profile"]) == (1, "adapter")
    content = [control for control in report["controls"] if control["group"] == "content"]
    assert {control["code"] for control in content} == {
        "canary_missing_from_witness",
        "canary_in_events",
        "canary_in_logs",
        "canary_in_metrics",
    }
    assert all(control["problems"] == 0 for control in content)
