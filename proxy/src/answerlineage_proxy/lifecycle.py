"""Cycle de vie du proxy HTTP : sondes de santé et arrêt gracieux.

Plusieurs réplicas du proxy se placent derrière un répartiteur, qui n'envoie les
appels qu'aux réplicas prêts. Pour qu'un arrêt ou un redéploiement ne fasse
échouer aucun appel :

- **la santé se lit à part**, sur un port d'administration qui n'écoute que sur
  la boucle locale (``/livez``, ``/readyz``), jamais sur le port des appels, dont
  tout chemin appartient au serveur MCP relayé. Ses réponses sont fixes : un état
  d'une liste fermée, aucune donnée d'appel. Le port d'administration contrôle
  l'en-tête ``Host`` comme le port des appels : seule la boucle locale y est admise ;
- **l'arrêt est gracieux.** Au signal SIGTERM, le proxy se déclare non prêt et sert
  encore pendant le délai de retrait, le temps que le répartiteur le retire de sa
  rotation ; il ferme alors son écoute, laisse aux appels en cours le délai de
  grâce pour se terminer, coupe ceux qui restent, et vide sa boîte d'envoi dans sa
  propre borne. SIGINT, celui d'un Ctrl-C au terminal, saute le délai de retrait ;
  un second signal l'écourte.

Rien de cela n'est sur le chemin d'un appel : le relais ne lit ni l'état ni les
délais, et le port d'administration répond sans rien lire du trafic.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import socket
import threading
import time
from collections.abc import Callable, Generator, Mapping
from typing import Any, Final, Literal

import uvicorn
from uvicorn.config import STARTUP_FAILURE

from answerlineage_proxy.app import McpProxy, Receive, Scope, Send
from answerlineage_proxy.config import ADMIN_HOST, ProxySettings
from answerlineage_proxy.guard import LOOPBACK_HOSTS, RequestGuard

logger = logging.getLogger("answerlineage_proxy")

# Réglages du serveur HTTP qui sert le proxy. Le journal d'accès contiendrait
# chemins et paramètres de requête ; les en-têtes Server et Date du serveur MCP
# sont relayés tels quels, le serveur HTTP n'en ajoute pas.
SERVER_OPTIONS: Final[Mapping[str, Any]] = {
    "access_log": False,
    "server_header": False,
    "date_header": False,
    "log_level": "warning",
}
# Connexions simultanées admises sur le port d'administration : une sonde n'en garde
# qu'une ou deux. Au-delà, le serveur HTTP répond 503 sans appeler l'application.
ADMIN_CONCURRENCY: Final = 8
# Une connexion inactive du port d'administration est fermée au bout de ce délai.
ADMIN_KEEP_ALIVE: Final = 5
# Signal qui passe par le délai de retrait : celui des orchestrateurs et de ``docker stop``.
WITHDRAWN_ON: Final = signal.SIGTERM
_CAUGHT: Final = (signal.SIGTERM, signal.SIGINT)
# Attente entre deux regards sur un serveur HTTP qui démarre.
_STARTUP_POLL: Final = 0.005

State = Literal["starting", "ready", "draining", "stopped"]

_JSON: Final = (b"content-type", b"application/json")
_BODIES: Final[Mapping[str, bytes]] = {
    "alive": b'{"status":"alive"}',
    "starting": b'{"status":"starting"}',
    "ready": b'{"status":"ready"}',
    "draining": b'{"status":"draining"}',
    "stopped": b'{"status":"stopped"}',
    "not_found": b'{"error":"not_found"}',
    "method_not_allowed": b'{"error":"method_not_allowed"}',
    "host_not_allowed": b'{"error":"host_not_allowed"}',
    "origin_not_allowed": b'{"error":"origin_not_allowed"}',
    "cross_site_not_allowed": b'{"error":"cross_site_not_allowed"}',
}


class Health:
    """L'état du proxy, tel que le port d'administration le dit : vivant tant qu'il
    répond, prêt entre la fin de son démarrage et le début de son arrêt."""

    def __init__(self) -> None:
        self.state: State = "starting"

    @property
    def ready(self) -> bool:
        return self.state == "ready"


class AdminApp:
    """Application ASGI du port d'administration : ``/livez`` et ``/readyz``.

    Les réponses sont fixes, d'une liste fermée ; rien de la requête n'est lu au-delà
    de sa méthode, de son chemin et des en-têtes Host et Origin, ni recopié."""

    def __init__(self, health: Health) -> None:
        self.health = health
        self._guard = RequestGuard.create(LOOPBACK_HOSTS)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        headers = [(bytes(name), bytes(value)) for name, value in scope["headers"]]
        refusal = self._guard.refusal(headers)
        if refusal is not None:
            await _respond(send, 403, refusal)
        elif scope["method"] not in ("GET", "HEAD"):
            await _respond(send, 405, "method_not_allowed", [(b"allow", b"GET, HEAD")])
        elif scope["path"] == "/livez":
            await _respond(send, 200, "alive")
        elif scope["path"] == "/readyz":
            state = self.health.state
            await _respond(send, 200 if state == "ready" else 503, state)
        else:
            await _respond(send, 404, "not_found")


async def _respond(
    send: Send, status: int, body_name: str, headers: list[tuple[bytes, bytes]] | None = None
) -> None:
    body = _BODIES[body_name]
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                _JSON,
                (b"cache-control", b"no-store"),
                (b"content-length", str(len(body)).encode("ascii")),
                *(headers or []),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


class _StartupError(Exception):
    """Le serveur HTTP n'a pas démarré : port pris, ou démarrage de l'application en échec.
    Le serveur HTTP en a déjà journalisé la cause."""


class _Server(uvicorn.Server):
    """Serveur HTTP qui laisse les signaux au cycle de vie : il ne s'arrête que quand
    celui-ci le lui demande, et ne relance aucun signal en sortant. Un démarrage en échec
    lève une erreur plutôt que d'arrêter l'interpréteur."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        yield

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        try:
            await super().startup(sockets=sockets)
        except SystemExit:
            raise _StartupError from None


def _bound(server: uvicorn.Server) -> tuple[str, int]:
    """L'adresse où un serveur HTTP démarré écoute, port tiré compris."""
    address = server.servers[0].sockets[0].getsockname()
    return str(address[0]), int(address[1])


def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:  # pragma: no cover — seuls SIGTERM et SIGINT sont attrapés.
        return str(number)


class Lifecycle:
    """Sert un proxy HTTP : démarrage, sondes de santé, arrêt gracieux.

    ``request_stop`` commence l'arrêt ; ``serve`` l'appelle à la réception de SIGTERM ou
    SIGINT quand il tourne dans le fil principal.
    """

    def __init__(
        self,
        app: McpProxy,
        settings: ProxySettings,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self.settings = settings
        self.health = Health()
        # Adresses où le proxy écoute, une fois prêt : appels, puis santé.
        self.address: tuple[str, int] | None = None
        self.admin_address: tuple[str, int] | None = None
        self.listening = asyncio.Event()
        self._clock = clock
        self._stop = asyncio.Event()
        self._hurry = asyncio.Event()
        self._signal: int | None = None
        self._stopping_since: float | None = None

    def request_stop(self, number: int = WITHDRAWN_ON) -> None:
        """Commence l'arrêt gracieux ; un second signal écourte le délai de retrait."""
        if self._signal is not None:
            if not self._hurry.is_set():
                self._hurry.set()
                logger.warning(
                    "Second signal (%s) : le délai de retrait est écourté.", _signal_name(number)
                )
            return
        self._signal = number
        self._stopping_since = self._clock()
        self._stop.set()

    def config(self) -> uvicorn.Config:
        """Le serveur des appels : sans journal d'accès ni en-tête ajouté ; à l'arrêt, les
        appels en cours ont le délai de grâce pour se terminer."""
        return uvicorn.Config(
            self.app,
            host=self.settings.host,
            port=self.settings.port,
            timeout_graceful_shutdown=self.settings.shutdown_grace,
            **SERVER_OPTIONS,
        )

    def admin_config(self, port: int) -> uvicorn.Config:
        """Le serveur de la santé : la boucle locale seulement, quelques connexions au plus."""
        return uvicorn.Config(
            AdminApp(self.health),
            host=ADMIN_HOST,
            port=port,
            lifespan="off",
            ws="none",
            limit_concurrency=ADMIN_CONCURRENCY,
            timeout_keep_alive=ADMIN_KEEP_ALIVE,
            timeout_graceful_shutdown=1,
            **SERVER_OPTIONS,
        )

    async def serve(self) -> int:
        """Sert jusqu'à l'arrêt ; rend 0 après un arrêt gracieux, 3 quand le proxy n'a pas
        pu démarrer, comme le serveur HTTP le fait seul."""
        loop = asyncio.get_running_loop()

        def received(number: int) -> None:
            loop.call_soon_threadsafe(self.request_stop, number)

        with _caught_signals(received):
            try:
                return await self._serve()
            except _StartupError:
                logger.error("Proxy non démarré : voir l'erreur du serveur HTTP ci-dessus.")
                return STARTUP_FAILURE

    async def _serve(self) -> int:
        admin: _Server | None = None
        admin_task: asyncio.Future[None] | None = None
        if self.settings.admin_port is not None:
            admin = _Server(self.admin_config(self.settings.admin_port))
            admin_task = asyncio.ensure_future(admin.serve())
        server = _Server(self.config())
        task: asyncio.Future[None] | None = None
        try:
            if (
                admin is not None
                and admin_task is not None
                and await self._started(admin, admin_task)
            ):
                self.admin_address = _bound(admin)
            task = asyncio.ensure_future(server.serve())
            if await self._started(server, task):
                self.address = _bound(server)
                self.health.state = "ready"
                self.listening.set()
                self._say_ready()
                await self._until_stopped(task)
                if not task.done():
                    await self._withdraw(task)
                    logger.info(
                        "Écoute fermée : %d appel(s) en cours, %g s au plus pour se terminer.",
                        len(server.server_state.tasks),
                        self.settings.shutdown_grace,
                    )
            server.should_exit = True
            cut = self.app.stats.cut_at_shutdown
            await task
            cut = self.app.stats.cut_at_shutdown - cut
            if cut:
                logger.warning("Arrêt : %d appel(s) coupé(s) au-delà du délai de grâce.", cut)
        finally:
            if task is not None and not task.done():
                server.should_exit = True
                await asyncio.gather(task, return_exceptions=True)
            self.health.state = "stopped"
            if admin is not None and admin_task is not None:
                admin.should_exit = True
                await asyncio.gather(admin_task, return_exceptions=True)
        if self._stopping_since is not None:
            logger.info("Arrêt terminé en %.1f s.", self._clock() - self._stopping_since)
        return 0

    async def _started(self, server: uvicorn.Server, task: asyncio.Future[None]) -> bool:
        """Attend qu'un serveur HTTP écoute ; faux si un arrêt est demandé avant. Un
        démarrage en échec lève son erreur."""
        while not self._stop.is_set():
            if server.started:
                return True
            if task.done():
                task.result()
                raise _StartupError
            await asyncio.sleep(_STARTUP_POLL)
        return False

    async def _until_stopped(self, task: asyncio.Future[None]) -> None:
        stop: asyncio.Future[Any] = asyncio.ensure_future(self._stop.wait())
        try:
            await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop.cancel()

    async def _withdraw(self, task: asyncio.Future[None]) -> None:
        """Non prêt, le proxy sert encore pendant le délai de retrait, sauf après SIGINT
        ou un second signal."""
        self.health.state = "draining"
        delay = self.settings.shutdown_delay if self._signal == WITHDRAWN_ON else 0.0
        name = _signal_name(self._signal if self._signal is not None else WITHDRAWN_ON)
        if delay <= 0 or self._hurry.is_set():
            logger.info("Arrêt demandé (%s) : non prêt, l'écoute se ferme.", name)
            return
        logger.info(
            "Arrêt demandé (%s) : non prêt ; le proxy sert encore %g s, le temps d'être retiré "
            "de la rotation.",
            name,
            delay,
        )
        hurry: asyncio.Future[Any] = asyncio.ensure_future(self._hurry.wait())
        try:
            await asyncio.wait({task, hurry}, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
        finally:
            hurry.cancel()

    def _say_ready(self) -> None:
        if self.address is None:  # pragma: no cover — appelé une fois l'écoute ouverte.
            return
        host, port = self.address
        if self.admin_address is None:
            logger.info("Prêt : appels sur %s:%d, sans port d'administration.", host, port)
            return
        logger.info(
            "Prêt : appels sur %s:%d, santé sur %s:%d (/livez, /readyz).",
            host,
            port,
            *self.admin_address,
        )


@contextlib.contextmanager
def _caught_signals(handler: Callable[[int], None]) -> Generator[None]:
    """Remet SIGTERM et SIGINT au cycle de vie le temps de servir, puis rend leurs
    gestionnaires d'avant. Hors du fil principal, aucun signal n'est attrapé."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {
        number: signal.signal(number, lambda received, _: handler(received)) for number in _CAUGHT
    }
    try:
        yield
    finally:
        for number, handled in previous.items():
            signal.signal(number, handled)


def run(app: McpProxy, settings: ProxySettings) -> int:
    """Sert le proxy jusqu'à son arrêt ; rend son code de sortie."""
    return asyncio.run(Lifecycle(app, settings).serve())
