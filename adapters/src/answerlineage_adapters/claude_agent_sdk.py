"""Adaptateur du Claude Agent SDK : le lignage d'un agent, capté par ses crochets.

L'adaptateur déclare des crochets (``hooks``) que l'application passe à
``ClaudeAgentOptions`` ; la logique de l'agent ne change pas. Noms et charges
utiles vérifiés dans le SDK 0.2.182 (CLI Claude Code 2.1.325) :

| Crochet | Ce que l'adaptateur émet |
| --- | --- |
| ``UserPromptSubmit`` | L'ouverture du run de l'agent (``START``) : une demande, un run |
| ``PostToolUse`` | L'appel d'outil réussi (``COMPLETE``), run enfant de l'agent ou du sous-agent |
| ``PostToolUseFailure`` | L'étape fautive : l'appel en échec (``FAIL``) ou interrompu (``ABORT``) |
| ``SubagentStart``, ``SubagentStop`` | Le run du sous-agent, enfant de celui de l'agent |
| ``Stop`` | La fin du run de l'agent (``COMPLETE``) |

**Rien ne retarde l'agent.** Chaque crochet rend aussitôt une sortie
asynchrone (``{"async_": True}``) : la CLI poursuit sans l'attendre. Le crochet
ne fait que des opérations en mémoire, en temps constant ; l'événement se
construit à côté, dans une tâche de fond, ses empreintes dans un fil, et part
par la boîte d'envoi de l'émetteur commun. La file des événements à construire
est bornée : pleine, elle abandonne le plus ancien, et le compte. Une erreur de
l'adaptateur est comptée et journalisée par sa classe ; elle n'atteint jamais l'agent.

**Aucun contenu ne sort.** Le prompt, les arguments, le résultat, le message
d'erreur et la réponse ne sont ni recopiés ni journalisés : arguments et
résultat partent en empreintes HMAC à clé de locataire, comme ceux du proxy.

**Toute entrée est hostile.** Un nom d'outil ou de sous-agent n'est recopié que
s'il a une forme admise ; les identifiants de session, de demande et de
sous-agent ne servent que de clés en mémoire, bornées en longueur et en nombre.

Les runs dérivent du contexte de trace W3C : chaque demande ouvre une trace, et
chaque sous-agent et chaque appel d'outil y est un span, dont l'identifiant de
run dérive par la dérivation publiée.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Literal, cast
from uuid import UUID

from claude_agent_sdk import HookContext, HookInput, HookMatcher
from claude_agent_sdk.types import AsyncHookJSONOutput, HookEvent, HookJSONOutput

from answerlineage_adapters.settings import LOGGER, AdapterSettings, AgentJob
from answerlineage_emitter.events import (
    AGENT_FACET,
    AGENT_FACET_SCHEMA,
    PARENT_FACET,
    QOS_FACET,
    QOS_FACET_SCHEMA,
    TOOL_CALL_FACET,
    TOOL_CALL_FACET_SCHEMA,
    UNDESCRIBED_TOOL,
    UNRESOLVED_TOOL,
    EventType,
    facet,
    parent_facet,
    producer,
    run_event,
)
from answerlineage_emitter.fingerprint import (
    ARGUMENTS_LABEL,
    RESULT_LABEL,
    FingerprintKey,
    canonical_json,
)
from answerlineage_emitter.sink import DeferredSink, EventSink
from answerlineage_emitter.tracecontext import (
    TraceContext,
    derive_run_id,
    new_span,
    new_trace_context,
)

PRODUCER: Final = producer("adapters")
# Le cadre de l'agent, dans la facette answerlineage_agent.
FRAMEWORK: Final = "claude-agent-sdk"
# Le serveur des outils intégrés à Claude Code (Read, Bash, WebFetch…), qui ne sont pas MCP,
# et l'espace de noms de leurs jobs ; un outil MCP a pour espace de noms mcp://<serveur>.
BUILTIN_SERVER: Final = "claude-code"
BUILTIN_NAMESPACE: Final = "claude-code://builtin"
# Le serveur d'un outil MCP dont le nom ne se lit pas, et le nom de job d'un sous-agent dont
# le type n'a pas une forme admise.
UNRESOLVED_SERVER: Final = "serveur-non-resolu"
UNRESOLVED_SUBAGENT: Final = "sous-agent-non-resolu"
# Événements à construire gardés au plus ; au-delà, le plus ancien est abandonné.
MAX_PENDING: Final = 10_000
# Runs d'agent et de sous-agent suivis au plus ; au-delà, le plus ancien est oublié, et
# clos en ABORT s'il était encore ouvert.
MAX_RUNS: Final = 10_000
# Délai donné à la construction des derniers événements, à la fermeture.
DEFAULT_FLUSH_SECONDS: Final = 5.0
# Un identifiant du SDK (session, demande, sous-agent) plus long n'est pas gardé.
MAX_KEY_LENGTH: Final = 256
MAX_DURATION_MS: Final = 1_000_000_000

# Un nom d'outil MCP, tel que le SDK le compose : mcp__<serveur>__<outil>.
_MCP_TOOL: Final = re.compile(r"mcp__([A-Za-z0-9_.-]{1,128}?)__([A-Za-z0-9_.-]{1,128})")
# Le nom d'un outil, d'un serveur ou d'un type de sous-agent : la forme que recommande MCP.
_NAME: Final = re.compile(r"[A-Za-z0-9_.-]{1,128}")

ToolOutcome = Literal["success", "failure", "interrupt"]
# Un appel réussi se clôt, un appel en échec échoue, un appel interrompu est abandonné.
_EVENT_TYPES: Final[dict[ToolOutcome, EventType]] = {
    "success": "COMPLETE",
    "failure": "FAIL",
    "interrupt": "ABORT",
}


@dataclass
class AdapterStats:
    """Compteurs de l'adaptateur : des nombres, sans aucune valeur issue de l'agent."""

    # Événements construits et remis à la destination.
    emitted: int = 0
    # Abandonnés : file pleine, crochet appelé après la fermeture, ou délai de fermeture écoulé.
    dropped: int = 0
    # Erreurs de l'adaptateur ou de la destination, comptées et journalisées par leur classe.
    failures: int = 0


@dataclass
class _Run:
    """Un run suivi en mémoire : son contexte de trace, son job, son parent."""

    trace: TraceContext
    job: dict[str, str]
    parent: _Run | None = None
    open: bool = True
    run_id: UUID = field(init=False)

    def __post_init__(self) -> None:
        self.run_id = derive_run_id(self.trace)


@dataclass
class _Pending:
    """Un événement à construire : ``build`` le rend ; ``heavy`` le fait construire dans un fil."""

    build: Callable[[], dict[str, Any]]
    heavy: bool
    dropped: bool = False


@dataclass(frozen=True)
class _ToolCall:
    """Ce qu'un crochet d'outil a vu, gardé le temps de construire l'événement."""

    moment: datetime
    trace: TraceContext
    parent: _Run
    namespace: str
    server: str
    tool: str | None
    arguments: object
    response: object
    has_response: bool
    outcome: ToolOutcome
    duration_ms: int | None


def _async() -> AsyncHookJSONOutput:
    """Ce que rend chaque crochet : la CLI poursuit sans l'attendre."""
    return {"async_": True}


def _key(value: object) -> str:
    """Un identifiant du SDK gardé comme clé en mémoire : texte borné, sinon aucun."""
    return value if isinstance(value, str) and len(value) <= MAX_KEY_LENGTH else ""


def _tool_name(value: object) -> tuple[str, str, str | None]:
    """L'espace de noms du job, le serveur et l'outil d'un nom d'outil du SDK ; l'outil est
    ``None`` s'il n'a pas une forme admise, et le serveur d'un nom MCP illisible, inconnu."""
    if isinstance(value, str):
        found = _MCP_TOOL.fullmatch(value)
        if found is not None:
            return f"mcp://{found[1]}", found[1], found[2]
        if value.startswith("mcp__"):
            return f"mcp://{UNRESOLVED_SERVER}", UNRESOLVED_SERVER, None
        if _NAME.fullmatch(value):
            return BUILTIN_NAMESPACE, BUILTIN_SERVER, value
    return BUILTIN_NAMESPACE, BUILTIN_SERVER, None


def _duration(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= MAX_DURATION_MS else None


class ClaudeAgentLineage:
    """Les crochets qui émettent le lignage d'un agent du Claude Agent SDK.

    ``async with`` démarre la destination et la construction des événements, et
    les vide à la sortie : à employer autour de la vie de l'agent.
    """

    def __init__(
        self,
        agent: AgentJob,
        sink: EventSink,
        *,
        fingerprint_key: FingerprintKey | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_pending: int = MAX_PENDING,
        max_runs: int = MAX_RUNS,
        flush_seconds: float = DEFAULT_FLUSH_SECONDS,
        logger: logging.Logger = LOGGER,
    ) -> None:
        self.stats = AdapterStats()
        self._agent = agent
        self._sink = sink
        self._key = fingerprint_key
        self._clock = clock
        self._max_pending = max_pending
        self._max_runs = max_runs
        self._flush_seconds = flush_seconds
        self._logger = logger
        self._agents: OrderedDict[tuple[str, str], _Run] = OrderedDict()
        self._subagents: OrderedDict[str, _Run] = OrderedDict()
        self._pending: deque[_Pending] = deque()
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._started = False
        self._closed = False
        self._overflowing = False
        self._failures_logged: set[str] = set()

    @classmethod
    def from_settings(cls, settings: AdapterSettings, **options: Any) -> ClaudeAgentLineage:
        """L'adaptateur que décrivent des réglages : leur agent, leur destination, leur clé."""
        return cls(
            settings.agent, settings.sink(), fingerprint_key=settings.fingerprint_key, **options
        )

    def __repr__(self) -> str:
        return f"ClaudeAgentLineage({self.stats!r})"

    # Les crochets déclarés au SDK.

    def hooks(
        self,
        existing: Mapping[HookEvent, Sequence[HookMatcher]] | None = None,
        *,
        tools: str | None = None,
    ) -> dict[HookEvent, list[HookMatcher]]:
        """Les crochets à passer à ``ClaudeAgentOptions(hooks=…)``, après ceux d'``existing``.

        ``tools`` restreint les outils suivis, avec la syntaxe des filtres du SDK
        (``"mcp__.*"`` : les seuls outils MCP) ; par défaut, tous.
        """
        own: dict[HookEvent, HookMatcher] = {
            "UserPromptSubmit": HookMatcher(hooks=[self.on_user_prompt_submit]),
            "PostToolUse": HookMatcher(matcher=tools, hooks=[self.on_post_tool_use]),
            "PostToolUseFailure": HookMatcher(matcher=tools, hooks=[self.on_post_tool_use_failure]),
            "SubagentStart": HookMatcher(hooks=[self.on_subagent_start]),
            "SubagentStop": HookMatcher(hooks=[self.on_subagent_stop]),
            "Stop": HookMatcher(hooks=[self.on_stop]),
        }
        merged: dict[HookEvent, list[HookMatcher]] = {
            event: list(matchers) for event, matchers in (existing or {}).items()
        }
        for event, matcher in own.items():
            merged.setdefault(event, []).append(matcher)
        return merged

    async def on_user_prompt_submit(
        self, data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        """Une demande : le run de l'agent s'ouvre. Le prompt n'est pas lu."""
        return self._handle(lambda fields: self._agent_run(fields, opening=True), data)

    async def on_post_tool_use(
        self, data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        """Un appel d'outil réussi."""
        return self._handle(lambda fields: self._tool_call(fields, "success"), data)

    async def on_post_tool_use_failure(
        self, data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        """Un appel d'outil en échec, ou interrompu. Le message d'erreur n'est pas lu."""

        def failed(fields: Mapping[str, object]) -> None:
            interrupted = fields.get("is_interrupt") is True
            self._tool_call(fields, "interrupt" if interrupted else "failure")

        return self._handle(failed, data)

    async def on_subagent_start(
        self, data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        """Un sous-agent démarre : son run s'ouvre, enfant de celui de l'agent."""
        return self._handle(lambda fields: self._subagent_run(fields, opening=True), data)

    async def on_subagent_stop(
        self, data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        """Un sous-agent s'arrête : son run se clôt. Son dernier message n'est pas lu."""
        return self._handle(self._subagent_stopped, data)

    async def on_stop(
        self, data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        """L'agent s'arrête : son run se clôt. Sa réponse n'est pas lue."""
        return self._handle(self._agent_stopped, data)

    def _handle(
        self, work: Callable[[Mapping[str, object]], object], data: HookInput
    ) -> HookJSONOutput:
        """Fait le travail d'un crochet, en mémoire seulement, et rend la main aussitôt.

        Aucune erreur n'atteint l'agent : elle est comptée et journalisée par sa classe.
        """
        if self._closed:
            self.stats.dropped += 1
            return _async()
        try:
            fields = cast(Mapping[str, object], data) if isinstance(data, Mapping) else {}
            work(fields)
            self._ensure_worker()
        except Exception as error:
            self._failed(error)
        return _async()

    # Les runs suivis.

    def _agent_run(self, fields: Mapping[str, object], *, opening: bool = False) -> _Run:
        """Le run de l'agent pour la demande en cours ; ouvert s'il n'existe pas encore, ou si
        une nouvelle demande arrive après sa clôture."""
        key = (_key(fields.get("session_id")), _key(fields.get("prompt_id")))
        run = self._agents.get(key)
        if run is not None and not (opening and not run.open):
            self._agents.move_to_end(key)
            return run
        run = _Run(new_trace_context(), self._agent.job())
        self._remember(self._agents, key, run)
        self._queue_agent_event("START", run)
        return run

    def _agent_stopped(self, fields: Mapping[str, object]) -> None:
        key = (_key(fields.get("session_id")), _key(fields.get("prompt_id")))
        run = self._agents.get(key)
        if run is None:
            # Un agent dont l'adaptateur n'a pas vu le début : son run se clôt seul.
            run = _Run(new_trace_context(), self._agent.job())
            self._remember(self._agents, key, run)
        elif not run.open:
            # Un second arrêt du même tour : le run est déjà clos.
            return
        run.open = False
        # La réponse et sa diffusion iront dans les sorties de ce run, avec les destinataires
        # pseudonymisés : rien de la réponse n'est lu ici.
        self._queue_agent_event("COMPLETE", run)

    def _subagent_job(self, kind: object) -> dict[str, str]:
        name = kind if isinstance(kind, str) and _NAME.fullmatch(kind) else UNRESOLVED_SUBAGENT
        return {"namespace": self._agent.namespace, "name": f"{self._agent.name}/{name}"}

    def _subagent_run(self, fields: Mapping[str, object], *, opening: bool = False) -> _Run | None:
        """Le run du sous-agent qui fait le travail, ``None`` sur le fil principal."""
        agent_id = _key(fields.get("agent_id"))
        if not agent_id:
            return None
        run = self._subagents.get(agent_id)
        if run is not None and not (opening and not run.open):
            self._subagents.move_to_end(agent_id)
            return run
        parent = self._agent_run(fields)
        run = _Run(new_span(parent.trace), self._subagent_job(fields.get("agent_type")), parent)
        self._remember(self._subagents, agent_id, run)
        self._queue_agent_event("START", run)
        return run

    def _subagent_stopped(self, fields: Mapping[str, object]) -> None:
        agent_id = _key(fields.get("agent_id"))
        run = self._subagents.get(agent_id) if agent_id else None
        if run is None:
            parent = self._agent_run(fields)
            run = _Run(new_span(parent.trace), self._subagent_job(fields.get("agent_type")), parent)
            if agent_id:
                self._remember(self._subagents, agent_id, run)
        elif not run.open:
            return
        run.open = False
        self._queue_agent_event("COMPLETE", run)

    def _remember[K](self, runs: OrderedDict[K, _Run], key: K, run: _Run) -> None:
        runs[key] = run
        runs.move_to_end(key)
        while len(runs) > self._max_runs:
            _, forgotten = runs.popitem(last=False)
            if forgotten.open:
                forgotten.open = False
                self._queue_agent_event("ABORT", forgotten)

    def _tool_call(self, fields: Mapping[str, object], outcome: ToolOutcome) -> None:
        parent = self._subagent_run(fields) or self._agent_run(fields)
        namespace, server, tool = _tool_name(fields.get("tool_name"))
        call = _ToolCall(
            moment=self._clock(),
            trace=new_span(parent.trace),
            parent=parent,
            namespace=namespace,
            server=server,
            tool=tool,
            arguments=fields.get("tool_input", {}),
            response=fields.get("tool_response"),
            has_response="tool_response" in fields,
            outcome=outcome,
            duration_ms=_duration(fields.get("duration_ms")),
        )
        self._queue(_Pending(lambda: self._tool_event(call), heavy=True))

    # Les événements.

    def _queue_agent_event(self, event_type: EventType, run: _Run) -> None:
        moment = self._clock()
        self._queue(_Pending(lambda: self._agent_event(event_type, run, moment), heavy=False))

    def _agent_event(self, event_type: EventType, run: _Run, moment: datetime) -> dict[str, Any]:
        facets: dict[str, Any] = {
            AGENT_FACET: facet(PRODUCER, AGENT_FACET_SCHEMA, framework=FRAMEWORK)
        }
        if run.parent is not None:
            facets[PARENT_FACET] = parent_facet(PRODUCER, run.parent.run_id, run.parent.job)
        return run_event(
            producer=PRODUCER,
            event_type=event_type,
            event_time=moment,
            run_id=run.run_id,
            run_facets=facets,
            job=dict(run.job),
        )

    def _tool_event(self, call: _ToolCall) -> dict[str, Any]:
        """L'événement d'un appel d'outil : construit dans un fil, ses empreintes comprises."""
        described = facet(
            PRODUCER,
            TOOL_CALL_FACET_SCHEMA,
            server=call.server,
            invalidToolName=call.tool is None,
            readResolution="unresolved",
            unresolvedReason=UNDESCRIBED_TOOL,
            traceId=call.trace.trace_id,
            spanId=call.trace.parent_id,
        )
        if call.tool is not None:
            described["tool"] = call.tool
        if self._key is not None:
            arguments = canonical_json(call.arguments)
            if arguments is not None:
                described["argumentsFingerprint"] = self._key.fingerprint(
                    ARGUMENTS_LABEL, arguments
                )
        if call.has_response:
            result = canonical_json(call.response)
            if result is not None:
                described["resultSize"] = len(result)
                if self._key is not None:
                    described["resultFingerprint"] = self._key.fingerprint(RESULT_LABEL, result)
        qos: dict[str, Any] = {"errorCount": 1 if call.outcome == "failure" else 0}
        if call.duration_ms is not None:
            qos["latencyMs"] = call.duration_ms
        if call.outcome == "success":
            qos["outcome"] = "success"
        elif call.outcome == "interrupt":
            qos["outcome"] = "client_abort"
        return run_event(
            producer=PRODUCER,
            event_type=_EVENT_TYPES[call.outcome],
            event_time=call.moment,
            run_id=derive_run_id(call.trace),
            run_facets={
                PARENT_FACET: parent_facet(PRODUCER, call.parent.run_id, call.parent.job),
                TOOL_CALL_FACET: described,
                QOS_FACET: facet(PRODUCER, QOS_FACET_SCHEMA, **qos),
            },
            job={
                "namespace": call.namespace,
                "name": call.tool or UNRESOLVED_TOOL,
            },
        )

    # La file des événements à construire, et sa tâche de fond.

    def _queue(self, pending: _Pending) -> None:
        self._pending.append(pending)
        while len(self._pending) > self._max_pending:
            evicted = self._pending.popleft()
            evicted.dropped = True
            self.stats.dropped += 1
            if not self._overflowing:
                self._overflowing = True
                self._logger.warning(
                    "Lignage de l'agent : file pleine, les événements les plus anciens sont "
                    "abandonnés et comptés."
                )
        self._wake.set()

    def _ensure_worker(self) -> None:
        if self._worker is None and not self._closed:
            self._worker = asyncio.ensure_future(self._work())

    async def start(self) -> None:
        """Démarre la destination, si elle remet plus tard, et la construction des événements."""
        if self._started:
            return
        self._started = True
        if isinstance(self._sink, DeferredSink):
            await self._sink.start()
        self._ensure_worker()

    async def _work(self) -> None:
        while True:
            while not self._pending:
                self._overflowing = False
                self._wake.clear()
                await self._wake.wait()
            await self._build_next()

    async def _build_next(self) -> None:
        """Construit l'événement en tête de file, puis le remet. Il ne quitte la file qu'une
        fois construit : interrompu, il reste à construire."""
        pending = self._pending[0]
        try:
            event = await asyncio.to_thread(pending.build) if pending.heavy else pending.build()
        except Exception as error:
            event = None
            self._failed(error)
        if self._pending and self._pending[0] is pending:
            self._pending.popleft()
        if event is None or pending.dropped:
            return
        try:
            self._sink.emit(event)
        except Exception as error:
            self._failed(error)
            return
        self.stats.emitted += 1

    async def drain(self) -> None:
        """Attend que les événements en file soient construits et remis à la destination."""
        while self._pending:
            if self._worker is None:
                await self._build_next()
            else:
                await asyncio.sleep(0.001)

    def _failed(self, error: Exception) -> None:
        self.stats.failures += 1
        name = type(error).__name__
        if name not in self._failures_logged and len(self._failures_logged) < 64:
            self._failures_logged.add(name)
            self._logger.warning(
                "Lignage de l'agent : erreur de l'adaptateur (%s), comptée ; l'agent n'en est "
                "pas affecté.",
                name,
            )

    # La fermeture.

    async def __aenter__(self) -> ClaudeAgentLineage:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Clôt en ABORT les runs restés ouverts, construit et remet ce qui attend, dans le
        délai de fermeture, puis ferme la destination. Le reste est abandonné et compté."""
        if self._closed:
            return
        self._closed = True
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._flush_seconds
        try:
            async with asyncio.timeout_at(deadline):
                await self._build_until(deadline)
                if loop.time() < deadline:
                    self._abort_open_runs()
                await self._build_until(deadline)
        except TimeoutError:
            pass
        self.stats.dropped += len(self._pending)
        self._pending.clear()
        if isinstance(self._sink, DeferredSink):
            await self._sink.aclose()

    async def _build_until(self, deadline: float) -> None:
        loop = asyncio.get_running_loop()
        while self._pending and loop.time() < deadline:
            await self._build_next()

    def _abort_open_runs(self) -> None:
        for runs in (self._subagents, self._agents):
            for run in runs.values():
                if run.open:
                    run.open = False
                    try:
                        self._queue_agent_event("ABORT", run)
                    except Exception as error:
                        self._failed(error)
