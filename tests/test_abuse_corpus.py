"""Le corpus d'abus, rejoué sur les surfaces réelles : chaque cas doit être bloqué.

Trois étages. Observer, ici : la requête d'un cas est jouée devant le vrai
proxy, la vraie API, un vrai PostgreSQL et un worker, et ce qui s'est passé est
relevé. Juger et conclure sont dans ``answerlineage_corpus.abuse_verdict``. Un
cas qui n'est pas bloqué fait échouer son test, dont le message commence par
son nom ; un cas ouvert est un échec attendu, nommé à chaque passage.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from answerlineage_api.app import create_app
from answerlineage_api.store.engine import create_engine
from answerlineage_corpus.abuse import Base, Case, healthy_play, load_abuse_corpus, play_of
from answerlineage_corpus.abuse_requests import Play
from answerlineage_corpus.abuse_verdict import (
    RAW_EVENTS,
    Conclusion,
    Observation,
    conclude,
    judge,
    judge_healthy,
)
from answerlineage_proxy import app as proxy_app
from answerlineage_proxy.app import McpProxy
from answerlineage_proxy.config import ProxySettings, SqlTool
from answerlineage_proxy.sink import MemorySink
from answerlineage_workers.queue import process_batch
from proxy.tests.conftest import exchange, running

pytestmark = pytest.mark.anyio

CORPUS = load_abuse_corpus()
# L'explorateur se joue dans un navigateur : tests/test_abuse_explorer.py.
CASES = [case for case in CORPUS if case.surface != "explorateur"]
BASES = [base for base in CORPUS.bases if base.surface != "explorateur"]

RESULT = b'{"jsonrpc":"2.0","id":1,"result":{"content":[],"isError":false}}'
# Au-delà, la surface n'a pas répondu : le cas échoue au lieu d'attendre.
PATIENCE = 20.0
CHUNK = 64 * 1024
# Tables qui ne sont pas le graphe : version du schéma, archive des événements reçus, file.
ARCHIVE, QUEUE = "event_raw", "ingest_queue"
BOOKKEEPING = ("alembic_version", ARCHIVE, QUEUE)


def by_surface(item: Case | Base) -> Any:
    """Un cas ou une base, marqué « integration » quand il lui faut PostgreSQL."""
    identifier = item.id if isinstance(item, Case) else item.name
    marks = [] if item.surface == "proxy" else [pytest.mark.integration]
    return pytest.param(item, id=identifier, marks=marks)


class Upstream:
    """Serveur MCP de substitution : il retient ce qu'il reçoit et répond toujours de même."""

    def __init__(self) -> None:
        self.received: list[tuple[list[tuple[str, str]], bytes]] = []

    async def __call__(
        self, scope: proxy_app.Scope, receive: proxy_app.Receive, send: proxy_app.Send
    ) -> None:
        if scope["type"] != "http":
            return
        body = b""
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        headers = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope["headers"]]
        self.received.append((headers, body))
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(RESULT)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": RESULT, "more_body": False})


class Timeline(MemorySink):
    """Les événements émis, et l'ordre dans lequel le proxy répond puis émet."""

    def __init__(self) -> None:
        super().__init__()
        self.order: list[str] = []

    def emit(self, event: Any) -> None:
        self.order.append("evenement")
        super().emit(event)

    def around(self, proxy: McpProxy) -> Any:
        """Le proxy, dont la fin de chaque réponse est notée."""

        async def noted(
            scope: proxy_app.Scope, receive: proxy_app.Receive, send: proxy_app.Send
        ) -> None:
            async def noting(message: proxy_app.Message) -> None:
                await send(message)
                if message["type"] == "http.response.body" and not message.get("more_body", False):
                    self.order.append("reponse")

            await proxy(scope, receive, noting if scope["type"] == "http" else send)

        return noted


def quiet_client(caplog: pytest.LogCaptureFixture) -> None:
    """Le client HTTP du test journalise l'adresse qu'il appelle : ce n'est pas le produit."""
    caplog.set_level(logging.WARNING, logger="httpx")
    caplog.set_level(logging.WARNING, logger="httpcore")


def parsed(body: bytes) -> Any:
    try:
        return json.loads(body)
    except ValueError:
        return None


async def observe_proxy(
    play: Play, caplog: pytest.LogCaptureFixture, **weakened: Any
) -> Observation:
    """Joue la requête devant le proxy, placé devant un serveur MCP de substitution."""
    upstream, timeline = Upstream(), Timeline()
    settings = dict(play.settings)
    tool = settings.pop("sql_tool", None)
    sql_tools = (
        {tool["name"]: SqlTool(tool["argument"], tool["dialect"], tool["namespace"])}
        if tool is not None
        else None
    )
    quiet_client(caplog)
    with caplog.at_level(logging.DEBUG):
        async with running(upstream) as upstream_url:
            proxy = McpProxy(
                ProxySettings.create(
                    upstream_url,
                    settings.pop("server"),
                    sql_tools=sql_tools,
                    **settings,
                    **weakened,
                ),
                timeline,
            )
            async with running(timeline.around(proxy), transparent=True) as url:
                answered = await exchange(
                    play.method, url + play.path, headers=list(play.headers), content=play.body
                )
                # Les analyses du SQL finissent après la réponse : leurs événements sont attendus.
                await proxy.drain()
    events = timeline.events
    identical = (
        [body for _, body in upstream.received] == [play.body]
        and all(header in upstream.received[0][0] for header in play.headers)
        and (answered.status, answered.body) == (200, RESULT)
    )
    return Observation(
        status=answered.status,
        body=parsed(answered.body),
        places={
            "réponse": answered.body.decode("utf-8", errors="replace"),
            "événements": json.dumps(events, ensure_ascii=False),
            "journaux": caplog.text,
            "compteurs": repr(proxy.stats),
        },
        forwarded=len(upstream.received),
        identical=identical,
        events=tuple(events),
        uninspected=proxy.stats.uninspected,
        order=tuple(timeline.order),
    )


class Counted:
    """L'application, et le nombre d'octets de corps qu'elle a lus."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app
        self.consumed = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def counting() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                self.consumed += len(message.get("body", b""))
            return message

        await self._app(scope, counting, send)


async def in_chunks(body: bytes) -> AsyncIterator[bytes]:
    for start in range(0, len(body), CHUNK):
        yield body[start : start + CHUNK]


async def tables(engine: AsyncEngine) -> dict[str, list[str]]:
    """Toute la base : chaque table, ligne par ligne, sous forme de texte.

    Une table partitionnée se lit en entier : ses partitions n'en sont pas d'autres.
    """
    async with engine.connect() as conn:
        names = await conn.execute(
            text(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition"
                " ORDER BY c.relname"
            )
        )
        return {
            name: list(
                (
                    await conn.execute(
                        text(f'SELECT CAST(row_to_json(t) AS text) FROM "{name}" AS t')  # noqa: S608
                    )
                ).scalars()
            )
            for name in names.scalars().all()
        }


@asynccontextmanager
async def held_back(url: URL) -> AsyncIterator[None]:
    """Une autre connexion tient un verrou exclusif sur toutes les tables : la lecture attend."""
    holder = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
    try:
        async with holder.connect() as conn:
            names = await conn.execute(
                text(
                    "SELECT string_agg(quote_ident(tablename), ', ') FROM pg_tables"
                    " WHERE schemaname = 'public'"
                )
            )
            await conn.execute(text(f"LOCK TABLE {names.scalar_one()} IN ACCESS EXCLUSIVE MODE"))
            yield
            await conn.rollback()
    finally:
        await holder.dispose()


async def drain(engine: AsyncEngine) -> None:
    """Laisse un worker projeter tout ce que la file lui offre."""
    while not (await process_batch(engine)).empty:
        pass


async def observe_api(
    play: Play, engine: AsyncEngine, caplog: pytest.LogCaptureFixture, **weakened: Any
) -> Observation:
    """Joue la requête devant l'API : ingestion, puis un worker ; ou lecture d'un graphe
    que les événements et les relevés préalables ont rempli par le même chemin."""
    app = Counted(create_app(engine, **{**play.settings, **weakened}))
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    quiet_client(caplog)
    with caplog.at_level(logging.DEBUG):
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            client.headers.clear()
            if play.seed:
                await client.post("/api/v1/lineage/batch", json=list(play.seed))
                await drain(engine)
            for report in play.reports:
                await client.post("/api/v1/coverage", json=report)
            app.consumed = 0
            request = client.build_request(
                play.method,
                play.path,
                headers=list(play.headers),
                content=play.body if play.announced else in_chunks(play.body),
            )
            answered: httpx.Response | None = None
            try:
                if play.slow:
                    async with held_back(engine.url):
                        answered = await asyncio.wait_for(client.send(request), PATIENCE)
                else:
                    answered = await asyncio.wait_for(client.send(request), PATIENCE)
            except TimeoutError:
                pass
            before = await tables(engine)
            await drain(engine)
            after = await tables(engine)
    return Observation(
        status=None if answered is None else answered.status_code,
        body=None if answered is None else parsed(answered.content),
        places={
            "réponse": "" if answered is None else answered.text,
            "journaux": caplog.text,
            "base": "\n".join(
                row for name, rows in after.items() if name != ARCHIVE for row in rows
            ),
            RAW_EVENTS: "\n".join(after[ARCHIVE]),
        },
        sent=len(play.body),
        consumed=app.consumed,
        stored=len(after[ARCHIVE]),
        queued=len(before[QUEUE]),
        pending=len(after[QUEUE]),
        graph={name: len(rows) for name, rows in after.items() if rows and name not in BOOKKEEPING},
    )


@pytest.fixture
def bench(request: pytest.FixtureRequest, caplog: pytest.LogCaptureFixture) -> Iterator[Any]:
    """Joue une requête sur sa surface. PostgreSQL n'est demandé que par l'API."""
    engines: list[AsyncEngine] = []

    async def observe(surface: str, play: Play, **weakened: Any) -> Observation:
        if surface == "proxy":
            return await observe_proxy(play, caplog, **weakened)
        engine = create_engine(request.getfixturevalue("database"))
        engines.append(engine)
        try:
            return await observe_api(play, engine, caplog, **weakened)
        finally:
            await engine.dispose()

    yield observe


def settle(conclusion: Conclusion) -> None:
    """Le sort du test : un échec nommé, l'échec attendu d'un cas ouvert, ou un succès."""
    if conclusion.outcome == "echec":
        pytest.fail(conclusion.message, pytrace=False)
    if conclusion.outcome == "echec_attendu":
        pytest.xfail(conclusion.message)


async def replay(case: Case, bench: Any, **weakened: Any) -> None:
    """Observe, juge et conclut : le test d'un cas, sur un service tel quel ou affaibli."""
    seen = await bench(case.surface, play_of(case), **weakened)
    settle(conclude(case, judge(case, seen)))


@pytest.mark.parametrize(
    ("identifier", "weakened", "message"),
    [
        pytest.param(
            "lots/lot-1001",
            {"max_batch_events": 100_000},
            "lots/lot-1001 : 413 batch_too_large attendu, 202 obtenu",
            marks=pytest.mark.integration,
            id="lot sans borne",
        ),
        pytest.param(
            "emetteur/hote-relaye-ingestion",
            {"allowed_hosts": ["*"]},
            "emetteur/hote-relaye-ingestion : 403 host_not_allowed attendu, 202 obtenu",
            marks=pytest.mark.integration,
            id="tout hôte accepté par l'API",
        ),
        pytest.param(
            "emetteur/hote-relaye-proxy",
            {"allowed_hosts": ("*",)},
            "emetteur/hote-relaye-proxy : 403 host_not_allowed attendu, 200 obtenu",
            id="tout hôte accepté par le proxy",
        ),
        pytest.param(
            "sql/corps-trop-grand",
            {"max_inspected_bytes": 4 * 1024 * 1024},
            "sql/corps-trop-grand : lecture non résolue (request_too_large) attendue, "
            "resolved obtenue",
            id="proxy qui lit tout corps",
        ),
    ],
)
async def test_a_case_a_weakened_service_lets_through_fails_and_names_it(
    identifier: str, weakened: dict[str, Any], message: str, bench: Any
) -> None:
    """Critère : quand un cas n'est pas bloqué, son test échoue et le nomme."""
    case = next(case for case in CORPUS if case.id == identifier)
    with pytest.raises(pytest.fail.Exception) as failed:
        await replay(case, bench, **weakened)
    assert str(failed.value).startswith(message)
    # L'échec dit l'attendu et l'obtenu, jamais la valeur hostile.
    assert case.canary not in str(failed.value)


@pytest.mark.parametrize("base", [by_surface(base) for base in BASES])
async def test_the_healthy_request_of_every_base_passes(base: Base, bench: Any) -> None:
    """Un cas n'est bloqué que pour la raison qu'il déclare : sans altération, sa requête passe."""
    seen = await bench(base.surface, healthy_play(base))
    assert judge_healthy(base, seen) == [], base.name


@pytest.mark.parametrize("case", [by_surface(case) for case in CASES])
async def test_every_case_of_the_abuse_corpus_is_blocked(case: Case, bench: Any) -> None:
    await replay(case, bench)
