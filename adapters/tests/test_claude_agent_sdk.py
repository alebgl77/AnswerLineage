"""L'adaptateur Claude Agent SDK : les crochets d'un agent émettent son lignage.

Les crochets sont appelés avec les vrais types du SDK installé, sans modèle ni
réseau : directement, ou par le protocole de contrôle du SDK, la CLI jouée par
le test. Chaque crochet rend aussitôt une sortie asynchrone, et l'événement part
à côté : rien ne retarde l'agent.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookContext,
    HookInput,
    PostToolUseFailureHookInput,
    PostToolUseHookInput,
    StopHookInput,
    SubagentStartHookInput,
    SubagentStopHookInput,
    UserPromptSubmitHookInput,
)
from claude_agent_sdk.types import HookEvent

from adapters.tests.fake_cli import FakeCli
from answerlineage_adapters.claude_agent_sdk import (
    BUILTIN_SERVER,
    FRAMEWORK,
    ClaudeAgentLineage,
)
from answerlineage_adapters.settings import ENV_FINGERPRINT_KEY, AdapterSettings, AgentJob
from answerlineage_emitter.events import UNDESCRIBED_TOOL, UNRESOLVED_TOOL
from answerlineage_emitter.fingerprint import (
    ARGUMENTS_LABEL,
    RESULT_LABEL,
    FingerprintKey,
    canonical_json,
)
from answerlineage_emitter.outbox import Outbox, OutboxSettings
from answerlineage_emitter.sink import MemorySink
from answerlineage_emitter.tracecontext import TraceContext, derive_run_id

pytestmark = pytest.mark.anyio

AGENT = AgentJob.create("agent://finance", "analyste-change")
KEY = FingerprintKey(bytes(range(32)))
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
SESSION, PROMPT = "session-1", "8f7c6a3e-0000-4000-8000-000000000001"
CONTEXT = HookContext(signal=None)
SQL_TOOL = "mcp__entrepot__execute_sql"
ARGUMENTS = {"query": "SELECT taux FROM finance.ref.taux_change"}
RESPONSE = [{"type": "text", "text": "taux : 1,08"}]


TRANSCRIPT = "/srv/agent/.claude/transcript.jsonl"


def prompt_submitted(prompt: str = "Quel est le taux du jour ?") -> UserPromptSubmitHookInput:
    return UserPromptSubmitHookInput(
        session_id=SESSION,
        transcript_path=TRANSCRIPT,
        cwd="/srv/agent",
        prompt_id=PROMPT,
        hook_event_name="UserPromptSubmit",
        prompt=prompt,
    )


def tool_used(
    tool: str = SQL_TOOL,
    arguments: dict[str, Any] | None = None,
    response: Any = None,
    *,
    session_id: str = SESSION,
    agent_id: str | None = None,
) -> PostToolUseHookInput:
    data = PostToolUseHookInput(
        session_id=session_id,
        transcript_path=TRANSCRIPT,
        cwd="/srv/agent",
        prompt_id=PROMPT,
        hook_event_name="PostToolUse",
        tool_name=tool,
        tool_input=ARGUMENTS if arguments is None else arguments,
        tool_response=RESPONSE if response is None else response,
        tool_use_id="toolu_01",
        duration_ms=42,
    )
    if agent_id is not None:
        data["agent_id"], data["agent_type"] = agent_id, "relecteur"
    return data


def tool_failed(error: str, *, interrupted: bool = False) -> PostToolUseFailureHookInput:
    return PostToolUseFailureHookInput(
        session_id=SESSION,
        transcript_path=TRANSCRIPT,
        cwd="/srv/agent",
        prompt_id=PROMPT,
        hook_event_name="PostToolUseFailure",
        tool_name=SQL_TOOL,
        tool_input=ARGUMENTS,
        tool_use_id="toolu_02",
        error=error,
        is_interrupt=interrupted,
        duration_ms=7,
    )


def subagent_started(
    agent_id: str = "sous-1", agent_type: str = "relecteur"
) -> SubagentStartHookInput:
    return SubagentStartHookInput(
        session_id=SESSION,
        transcript_path=TRANSCRIPT,
        cwd="/srv/agent",
        prompt_id=PROMPT,
        hook_event_name="SubagentStart",
        agent_id=agent_id,
        agent_type=agent_type,
    )


def subagent_stopped(agent_id: str = "sous-1") -> SubagentStopHookInput:
    return SubagentStopHookInput(
        session_id=SESSION,
        transcript_path=TRANSCRIPT,
        cwd="/srv/agent",
        prompt_id=PROMPT,
        hook_event_name="SubagentStop",
        stop_hook_active=False,
        agent_id=agent_id,
        agent_transcript_path="/srv/agent/.claude/sous-1.jsonl",
        agent_type="relecteur",
        last_assistant_message="Relu.",
    )


def stopped(answer: str = "Le taux du jour est de 1,08.") -> StopHookInput:
    return StopHookInput(
        session_id=SESSION,
        transcript_path=TRANSCRIPT,
        cwd="/srv/agent",
        prompt_id=PROMPT,
        hook_event_name="Stop",
        stop_hook_active=False,
        last_assistant_message=answer,
    )


class Clock:
    """Une horloge qui avance d'une seconde à chaque lecture : chaque événement a son heure."""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def lineage(sink: Any = None, **options: Any) -> tuple[ClaudeAgentLineage, MemorySink]:
    held = MemorySink() if sink is None else sink
    return ClaudeAgentLineage(AGENT, held, clock=Clock(), **options), held


async def fire(adapter: ClaudeAgentLineage, event: HookEvent, data: Mapping[str, Any]) -> Any:
    """Appelle le crochet que l'adaptateur déclare pour cet événement, comme le SDK le ferait."""
    (matcher,) = adapter.hooks()[event]
    (callback,) = matcher.hooks
    return await callback(cast(HookInput, data), data.get("tool_use_id"), CONTEXT)


def by_type(events: list[Mapping[str, Any]], kind: str) -> list[Mapping[str, Any]]:
    return [event for event in events if event["eventType"] == kind]


# Le succès d'un appel d'outil.


async def test_a_tool_call_is_emitted_from_post_tool_use_in_async_mode() -> None:
    adapter, sink = lineage(fingerprint_key=KEY)
    async with adapter:
        answer = await fire(adapter, "PostToolUse", tool_used())
        assert answer == {"async_": True}
        await fire(adapter, "Stop", stopped())
    opened, called, _ = sink.events
    assert opened["eventType"] == "START"
    assert opened["job"] == AGENT.job()
    assert opened["run"]["facets"]["answerlineage_agent"]["framework"] == FRAMEWORK

    assert called["eventType"] == "COMPLETE"
    assert called["job"] == {"namespace": "mcp://entrepot", "name": "execute_sql"}
    facets = called["run"]["facets"]
    assert facets["parent"]["run"] == {"runId": opened["run"]["runId"]}
    assert facets["parent"]["job"] == AGENT.job()
    described = facets["answerlineage_toolCall"]
    assert (described["server"], described["tool"]) == ("entrepot", "execute_sql")
    assert described["invalidToolName"] is False
    assert (described["readResolution"], described["unresolvedReason"]) == (
        "unresolved",
        UNDESCRIBED_TOOL,
    )
    # Les empreintes sont celles de tout émetteur : la forme canonique des arguments, et
    # celle du résultat que l'agent a reçu.
    result = canonical_json(RESPONSE)
    assert result is not None
    assert described["argumentsFingerprint"] == KEY.fingerprint(
        ARGUMENTS_LABEL, canonical_json(ARGUMENTS) or b""
    )
    assert described["resultFingerprint"] == KEY.fingerprint(RESULT_LABEL, result)
    assert described["resultSize"] == len(result)
    # L'identifiant de run dérive du contexte de trace que la facette cite, sous la trace
    # de l'agent.
    context = TraceContext(described["traceId"], described["spanId"])
    assert called["run"]["runId"] == str(derive_run_id(context))
    assert "traceOrigin" not in described
    qos = facets["answerlineage_qos"]
    assert (qos["latencyMs"], qos["outcome"], qos["errorCount"]) == (42, "success", 0)
    assert called["inputs"] == called["outputs"] == []


async def test_the_hook_returns_without_waiting_for_the_send() -> None:
    """Une API qui ne répond jamais ne retient pas le crochet : il rend la main aussitôt."""
    hold = asyncio.Event()

    async def never(_: httpx.Request) -> httpx.Response:
        await hold.wait()
        return httpx.Response(202)  # pragma: no cover — jamais atteint.

    async with httpx.AsyncClient(transport=httpx.MockTransport(never)) as client:
        box = Outbox(OutboxSettings.create("http://127.0.0.1:9", flush_seconds=0), client=client)
        adapter = ClaudeAgentLineage(AGENT, box, fingerprint_key=KEY)
        async with adapter:
            started = time.monotonic()
            for _ in range(50):
                assert await fire(adapter, "PostToolUse", tool_used()) == {"async_": True}
            assert await fire(adapter, "Stop", stopped()) == {"async_": True}
            spent = time.monotonic() - started
            await adapter.drain()
            # L'ouverture du run de l'agent, cinquante appels, sa fin : tout attend l'API.
            assert box.queued == 52
    # Cinquante et un crochets en bien moins d'une seconde, l'API pendante.
    assert spent < 0.5
    assert adapter.stats.emitted == 52
    assert box.stats.dropped_shutdown == 52


async def test_a_mcp_tool_and_a_builtin_tool_are_told_apart() -> None:
    adapter, sink = lineage()
    async with adapter:
        await fire(adapter, "PostToolUse", tool_used("mcp__crm-v2__clients.lire"))
        await fire(adapter, "PostToolUse", tool_used("Read", {"file_path": "/srv/a.txt"}))
    mcp, builtin = (event for event in sink.events if event["eventType"] == "COMPLETE")
    assert mcp["job"] == {"namespace": "mcp://crm-v2", "name": "clients.lire"}
    assert builtin["job"] == {"namespace": f"{BUILTIN_SERVER}://builtin", "name": "Read"}
    assert builtin["run"]["facets"]["answerlineage_toolCall"]["server"] == BUILTIN_SERVER
    # Sans clé, aucune empreinte : un hachage sans clé s'inverserait par énumération.
    for event in (mcp, builtin):
        described = event["run"]["facets"]["answerlineage_toolCall"]
        assert "argumentsFingerprint" not in described
        assert "resultFingerprint" not in described


@pytest.mark.parametrize(
    "tool",
    ["outil <b>", "outil avec espaces", "mcp__serveur__", "x" * 200, "mcp__a b__c", ""],
)
async def test_a_hostile_tool_name_is_not_copied(tool: str) -> None:
    adapter, sink = lineage()
    async with adapter:
        await fire(adapter, "PostToolUse", tool_used(tool))
    (called,) = by_type(sink.events, "COMPLETE")
    described = called["run"]["facets"]["answerlineage_toolCall"]
    assert described["invalidToolName"] is True
    assert "tool" not in described
    assert called["job"]["name"] == UNRESOLVED_TOOL
    if tool:
        assert tool not in repr(sink.events)


# L'échec d'un appel d'outil.


async def test_a_failed_tool_is_recorded_with_the_failing_step() -> None:
    adapter, sink = lineage(fingerprint_key=KEY)
    error = "canari-" + secrets.token_hex(6)
    async with adapter:
        await fire(adapter, "PostToolUse", tool_used())
        answer = await fire(adapter, "PostToolUseFailure", tool_failed(f"Erreur : {error}"))
    assert answer == {"async_": True}
    (opened,) = by_type(sink.events, "START")
    (failed,) = by_type(sink.events, "FAIL")
    # L'étape fautive : cet appel de cet outil, rattaché au run de l'agent qui l'a fait.
    assert failed["job"] == {"namespace": "mcp://entrepot", "name": "execute_sql"}
    assert failed["run"]["facets"]["parent"]["run"]["runId"] == opened["run"]["runId"]
    described = failed["run"]["facets"]["answerlineage_toolCall"]
    assert described["argumentsFingerprint"].startswith("hmac-sha256:")
    assert "resultFingerprint" not in described
    qos = failed["run"]["facets"]["answerlineage_qos"]
    assert (qos["latencyMs"], qos["errorCount"]) == (7, 1)
    assert "outcome" not in qos
    # Le message d'erreur est un texte libre : il ne sort pas.
    assert error not in repr(sink.events)
    # Deux appels, deux runs distincts sous la même trace.
    (called,) = by_type(sink.events, "COMPLETE")
    assert called["run"]["runId"] != failed["run"]["runId"]
    assert called["run"]["facets"]["answerlineage_toolCall"]["traceId"] == described["traceId"]


async def test_an_interrupted_tool_is_an_abort_by_the_client() -> None:
    adapter, sink = lineage()
    async with adapter:
        await fire(adapter, "PostToolUseFailure", tool_failed("Interrompu", interrupted=True))
        await fire(adapter, "Stop", stopped())
    (aborted,) = by_type(sink.events, "ABORT")
    assert aborted["job"]["name"] == "execute_sql"
    qos = aborted["run"]["facets"]["answerlineage_qos"]
    assert (qos["outcome"], qos["errorCount"]) == ("client_abort", 0)


# L'agent, ses sous-agents, son arrêt.


async def test_the_run_of_the_agent_opens_with_the_prompt_and_ends_with_the_stop() -> None:
    adapter, sink = lineage()
    async with adapter:
        await fire(adapter, "UserPromptSubmit", prompt_submitted())
        await fire(adapter, "PostToolUse", tool_used())
        await fire(adapter, "Stop", stopped())
        # Un second arrêt du même tour, quand un autre crochet a relancé l'agent, ne
        # clôt pas un second run.
        await fire(adapter, "Stop", stopped())
    assert [event["eventType"] for event in sink.events] == ["START", "COMPLETE", "COMPLETE"]
    opened, _, ended = sink.events
    assert ended["run"]["runId"] == opened["run"]["runId"]
    assert ended["job"] == AGENT.job()
    # La réponse de l'agent n'est pas dans ce run : sa diffusion viendra avec les
    # destinataires pseudonymisés.
    assert ended["outputs"] == []


async def test_each_prompt_of_a_session_is_its_own_run() -> None:
    adapter, sink = lineage()
    async with adapter:
        await fire(adapter, "UserPromptSubmit", prompt_submitted())
        await fire(adapter, "Stop", stopped())
        second = {**prompt_submitted(), "prompt_id": "8f7c6a3e-0000-4000-8000-000000000002"}
        await fire(adapter, "UserPromptSubmit", second)
        await fire(adapter, "Stop", {**stopped(), "prompt_id": second["prompt_id"]})
    assert [event["eventType"] for event in sink.events] == ["START", "COMPLETE"] * 2
    first, _, other, _ = sink.events
    assert other["run"]["runId"] != first["run"]["runId"]


async def test_a_subagent_is_a_run_of_its_own_under_the_agent() -> None:
    adapter, sink = lineage()
    async with adapter:
        await fire(adapter, "UserPromptSubmit", prompt_submitted())
        await fire(adapter, "SubagentStart", subagent_started())
        await fire(adapter, "PostToolUse", tool_used(agent_id="sous-1"))
        await fire(adapter, "SubagentStop", subagent_stopped())
        await fire(adapter, "Stop", stopped())
    agent, subagent, call, subagent_end, agent_end = sink.events
    assert subagent["eventType"] == "START"
    assert subagent["job"] == {"namespace": AGENT.namespace, "name": f"{AGENT.name}/relecteur"}
    assert subagent["run"]["facets"]["answerlineage_agent"]["framework"] == FRAMEWORK
    assert subagent["run"]["facets"]["parent"]["run"]["runId"] == agent["run"]["runId"]
    # L'appel d'outil du sous-agent est rattaché au sous-agent, pas à l'agent.
    assert call["run"]["facets"]["parent"]["run"]["runId"] == subagent["run"]["runId"]
    assert call["run"]["facets"]["parent"]["job"] == subagent["job"]
    assert (subagent_end["eventType"], subagent_end["run"]["runId"]) == (
        "COMPLETE",
        subagent["run"]["runId"],
    )
    assert agent_end["run"]["runId"] == agent["run"]["runId"]


async def test_a_hostile_subagent_type_is_not_copied() -> None:
    adapter, sink = lineage()
    async with adapter:
        await fire(adapter, "SubagentStart", subagent_started("sous-2", "ignore <script>"))
    _, subagent, *_ = sink.events
    assert subagent["job"]["name"] == f"{AGENT.name}/sous-agent-non-resolu"


async def test_runs_left_open_are_aborted_when_the_adapter_closes() -> None:
    adapter, sink = lineage()
    async with adapter:
        await fire(adapter, "UserPromptSubmit", prompt_submitted())
        await fire(adapter, "SubagentStart", subagent_started())
    assert [event["eventType"] for event in sink.events] == ["START", "START", "ABORT", "ABORT"]
    # Après la fermeture, un crochet ne fait plus rien, et rend la main.
    assert await fire(adapter, "Stop", stopped()) == {"async_": True}
    assert adapter.stats.dropped == 1


async def test_an_end_seen_without_its_start_still_closes_a_run() -> None:
    """Un adaptateur branché en cours de demande voit des fins sans début : chacune clôt un run."""
    adapter, sink = lineage()
    async with adapter:
        await fire(adapter, "SubagentStop", subagent_stopped())
        await fire(adapter, "SubagentStop", subagent_stopped())
        await fire(adapter, "Stop", stopped())
    agent, subagent, ended = sink.events
    assert (agent["eventType"], subagent["eventType"], ended["eventType"]) == (
        "START",
        "COMPLETE",
        "COMPLETE",
    )
    assert subagent["run"]["facets"]["parent"]["run"]["runId"] == agent["run"]["runId"]
    assert ended["run"]["runId"] == agent["run"]["runId"]


async def test_the_runs_followed_are_bounded_and_a_forgotten_open_run_is_aborted() -> None:
    adapter, sink = lineage(max_runs=2)
    async with adapter:
        for rank in range(3):
            prompt = {**prompt_submitted(), "prompt_id": f"demande-{rank}"}
            await fire(adapter, "UserPromptSubmit", prompt)
        await adapter.drain()
        # La plus ancienne demande est oubliée, et close : sa fin, plus tard, ouvre un autre run.
        assert [event["eventType"] for event in sink.events] == ["START", "START", "ABORT", "START"]
        assert sink.events[2]["run"]["runId"] == sink.events[0]["run"]["runId"]


# Rien ne retarde l'agent, rien ne l'interrompt.


class BrokenSink:
    def emit(self, event: Mapping[str, Any]) -> None:
        raise RuntimeError("panne de la destination")

    def report_coverage(self, report: Mapping[str, Any]) -> None:  # pragma: no cover
        raise RuntimeError("panne de la destination")


async def test_a_failing_destination_never_reaches_the_agent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter, _ = lineage(BrokenSink())
    with caplog.at_level(logging.WARNING, logger="answerlineage_adapters"):
        async with adapter:
            assert await fire(adapter, "PostToolUse", tool_used()) == {"async_": True}
    # L'ouverture du run de l'agent, l'appel, et la clôture du run à la fermeture.
    assert adapter.stats.failures == 3
    assert "RuntimeError" in caplog.text
    assert "panne de la destination" not in caplog.text


async def test_a_hook_input_the_adapter_cannot_read_never_reaches_the_agent() -> None:
    adapter, sink = lineage()
    async with adapter:
        for hostile in ({}, {"tool_name": 3, "session_id": ["x"]}, {"tool_input": "texte"}):
            assert await fire(adapter, "PostToolUse", hostile) == {"async_": True}
    assert len(by_type(sink.events, "COMPLETE")) == 3


async def test_an_error_of_the_adapter_is_counted_and_never_reaches_the_agent() -> None:
    """Une horloge en panne fait échouer le crochet ou la construction : l'agent n'en voit rien."""

    def broken() -> datetime:
        raise OSError("horloge")

    adapter = ClaudeAgentLineage(AGENT, MemorySink(), clock=broken)
    async with adapter:
        assert await fire(adapter, "PostToolUse", tool_used()) == {"async_": True}
    # Le crochet, puis la clôture du run de l'agent à la fermeture.
    assert adapter.stats.failures == 2

    unreadable = ClaudeAgentLineage(AGENT, MemorySink(), clock=lambda: cast(datetime, "midi"))
    async with unreadable:
        assert await fire(unreadable, "PostToolUse", tool_used()) == {"async_": True}
    # L'ouverture du run, l'appel, la clôture : aucun ne se construit, chacun est compté.
    assert (unreadable.stats.failures, unreadable.stats.emitted) == (3, 0)


async def test_the_adapter_is_built_from_its_settings() -> None:
    written = secrets.token_hex(32)
    settings = AdapterSettings.from_environment({ENV_FINGERPRINT_KEY: written}, agent=AGENT)
    adapter = ClaudeAgentLineage.from_settings(settings, flush_seconds=1)
    async with adapter:
        await fire(adapter, "PostToolUse", tool_used())
        await fire(adapter, "Stop", stopped())
    # Sans adresse d'API, rien ne part ; la clé des réglages empreint les arguments.
    assert adapter.stats.emitted == 3
    assert repr(adapter) == ("ClaudeAgentLineage(AdapterStats(emitted=3, dropped=0, failures=0))")
    assert written not in repr(adapter)


async def test_what_is_not_built_within_the_closing_delay_is_dropped_and_counted() -> None:
    adapter, sink = lineage(flush_seconds=0)
    for _ in range(3):
        await fire(adapter, "PostToolUse", tool_used())
    await adapter.aclose()
    assert sink.events == []
    assert adapter.stats.dropped == 4


async def test_the_events_waiting_to_be_built_are_bounded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter, sink = lineage(max_pending=5)
    # Les crochets ne rendent pas la main à la boucle : les événements s'accumulent, et
    # au-delà de la borne, les plus anciens sont abandonnés et comptés. Ici, l'ouverture du
    # run de l'agent et les trois premiers appels.
    with caplog.at_level(logging.WARNING, logger="answerlineage_adapters"):
        for _ in range(8):
            await fire(adapter, "PostToolUse", tool_used())
        await adapter.aclose()
    assert adapter.stats.dropped == 4
    assert [event["eventType"] for event in sink.events] == ["COMPLETE"] * 5 + ["ABORT"]
    assert caplog.text.count("file pleine") == 1


async def test_hostile_identifiers_from_the_sdk_are_bounded() -> None:
    adapter, sink = lineage()
    async with adapter:
        for rank in range(3):
            long_session = "s" * 10_000 + str(rank)
            await fire(adapter, "PostToolUse", tool_used(session_id=long_session))
    # Des identifiants de session démesurés ne sont pas gardés : tous comptent pour une
    # session sans identifiant, donc un seul run d'agent.
    assert len(by_type(sink.events, "START")) == 1
    assert "s" * 300 not in repr(sink.events)


# Par le protocole du SDK.


async def test_the_sdk_declares_the_hooks_and_the_cli_gets_an_async_answer() -> None:
    adapter, sink = lineage(fingerprint_key=KEY)
    cli = FakeCli()
    async with adapter:
        options = ClaudeAgentOptions(hooks=adapter.hooks())
        async with ClaudeSDKClient(options=options, transport=cli):
            assert set(cli.hooks) == {
                "UserPromptSubmit",
                "PostToolUse",
                "PostToolUseFailure",
                "SubagentStart",
                "SubagentStop",
                "Stop",
            }
            # La CLI reçoit « async » : elle poursuit sans attendre le crochet.
            assert await cli.fire("UserPromptSubmit", prompt_submitted()) == [{"async": True}]
            assert await cli.fire("PostToolUse", tool_used(), "toolu_01") == [{"async": True}]
            failed = tool_failed("Erreur")
            assert await cli.fire("PostToolUseFailure", failed, "toolu_02") == [{"async": True}]
            assert await cli.fire("Stop", stopped()) == [{"async": True}]
    assert [event["eventType"] for event in sink.events] == [
        "START",
        "COMPLETE",
        "FAIL",
        "COMPLETE",
    ]


async def test_the_tools_to_follow_can_be_narrowed_and_other_hooks_are_kept() -> None:
    adapter, _ = lineage()
    mine = adapter.hooks()["Stop"][0]
    hooks = adapter.hooks(existing={"Stop": [mine]}, tools="mcp__.*")
    assert hooks["Stop"][0] is mine
    assert len(hooks["Stop"]) == 2
    assert {matcher.matcher for matcher in hooks["PostToolUse"]} == {"mcp__.*"}
    assert hooks["UserPromptSubmit"][0].matcher is None
