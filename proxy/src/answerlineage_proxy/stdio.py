"""Proxy MCP en stdio : il lance le serveur MCP et relaie ses flux, octet pour octet.

``answerlineage-proxy [réglages] -- <commande> [arguments…]`` lance la commande
comme processus enfant, directement, sans shell, et se place entre le client
et elle : l'entrée standard du proxy va à celle de l'enfant, la sortie standard
de l'enfant à celle du proxy. La sortie d'erreur de l'enfant est la sienne : le
proxy ne la lit pas. Le proxy n'écrit jamais rien sur sa sortie standard : ses
messages vont dans son journal, sur la sortie d'erreur.

Le relais tient dans deux fils, un par sens, qui lisent un morceau, l'écrivent
de l'autre côté, et remettent une copie à l'inspection : ils n'attendent jamais
l'inspection. Celle-ci tourne à part, sur la boucle asyncio du proxy : elle
découpe chaque flux en messages (une ligne JSON-RPC, selon le transport stdio de
MCP), lit chaque message de façon bornée, rattache une réponse à sa requête par
son identifiant et décrit un appel d'outil une fois sa réponse rendue, avec les
règles du mode HTTP (``CallCapture``). Si elle prend du retard, les copies en
trop ne sont pas gardées : les messages qu'elles portaient sont comptés, jamais
devinés, et le relais n'est pas freiné.

En stdio, il n'y a ni en-têtes, ni statut HTTP, ni rien qui écoute sur le
réseau : pas de contrôle Host ni Origin. Le contexte de trace se lit dans
``params._meta.traceparent`` du message ; à défaut, le proxy en crée un, qui ne
sert qu'à l'identifiant de run : un message n'est jamais modifié.

Aucun argument, résultat, identifiant de requête, ni la commande lancée, n'est
conservé ni journalisé. POSIX seulement.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import select
import signal
import subprocess
import sys
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import IO, Final, Literal, TypeGuard

from answerlineage_proxy.analyzer import SqlAnalyzer
from answerlineage_proxy.app import CallCapture, _Exchange, _now
from answerlineage_proxy.config import (
    DEFAULT_SQL_BUDGET_MS,
    DEFAULT_SQL_CACHE_SIZE,
    DEFAULT_SQL_QUEUE_SIZE,
    ENV_FINGERPRINT_KEY,
    ENV_FINGERPRINT_KEY_FILE,
    ENV_SERVER_NAME,
    ENV_SQL_BUDGET_MS,
    ENV_SQL_CACHE_SIZE,
    ENV_SQL_QUEUE_SIZE,
    ProxyConfigurationError,
    SqlTool,
    StdioSettings,
    fingerprint_key,
    integer,
    read_rules,
)
from answerlineage_proxy.coverage import DEFAULT_WINDOW
from answerlineage_proxy.events import Outcome
from answerlineage_proxy.fingerprint import RESULT_LABEL, Digest
from answerlineage_proxy.inspection import (
    Inspection,
    inspect_message,
    inspect_prefix,
    parse_message,
)
from answerlineage_proxy.prefix import PREFIX_BYTES, read_message_prefix, read_reply_prefix
from answerlineage_proxy.signals import (
    CallOutcome,
    CallTiming,
    Failure,
    RetryMemory,
    bounded_code,
    reply_error,
)
from answerlineage_proxy.sink import EventSink
from answerlineage_proxy.tracecontext import CallTrace, resolve_trace

logger = logging.getLogger("answerlineage_proxy")

# Signaux transmis à l'enfant : le proxy ne s'arrête qu'avec lui. SIGHUP aussi,
# pour que la fermeture d'un terminal ne laisse pas l'enfant seul.
FORWARDED_SIGNALS: Final = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
# Codes de sortie d'une commande qui ne démarre pas, comme ceux d'un shell POSIX.
EXIT_NOT_FOUND: Final = 127
EXIT_NOT_EXECUTABLE: Final = 126
# Variable que l'enfant ne reçoit pas : la clé d'empreinte du locataire n'a pas à
# sortir du proxy. Le reste de l'environnement passe tel quel.
WITHHELD_VARIABLES: Final = (ENV_FINGERPRINT_KEY,)

# Morceau lu d'un flux, au plus.
_CHUNK_BYTES: Final = 64 * 1024
# Copies en attente d'inspection, au plus : au-delà, elles ne sont pas gardées. Chaque
# copie compte en plus pour quelques centaines d'octets, ce qu'elle coûte en mémoire.
BACKLOG_BYTES: Final = 8 * 1024 * 1024
_ITEM_BYTES: Final = 256
# Requêtes en attente de leur réponse, au plus, et octets des appels d'outil gardés
# pour être décrits une fois leur réponse rendue : au-delà, la plus ancienne est oubliée.
PENDING_REQUESTS: Final = 1024
PENDING_BYTES: Final = 32 * 1024 * 1024

CANCELLED: Final = "notifications/cancelled"

Direction = Literal["client", "server"]
_CLIENT: Final[Direction] = "client"
_SERVER: Final[Direction] = "server"


def exit_status(returncode: int) -> int:
    """Le code de sortie du proxy : celui de l'enfant, ou 128 + le signal qui l'a arrêté."""
    return returncode if returncode >= 0 else 128 - returncode


class _Stamp:
    """Horodatages d'un morceau relayé, sur une horloge monotone, en nanosecondes : lu,
    prêt à être écrit, écrit."""

    __slots__ = ("read_at", "sent_at", "written_at")

    def __init__(self, read_at: int) -> None:
        self.read_at = read_at
        self.sent_at: int | None = None
        self.written_at: int | None = None


@dataclass(slots=True)
class _Copy:
    """Copie d'un morceau relayé, remise à l'inspection."""

    direction: Direction
    data: bytes
    stamp: _Stamp


@dataclass(slots=True)
class _Gap:
    """Morceaux relayés dont la copie n'a pas été gardée : l'inspection avait pris du retard.

    Seul compte ce qui permet de reprendre le fil : le nombre de fins de ligne,
    donc de messages perdus, et si le dernier morceau s'achevait sur une fin de ligne.
    """

    direction: Direction
    newlines: int = 0
    boundary: bool = False
    size: int = 0

    def add(self, data: bytes) -> None:
        self.newlines += data.count(b"\n")
        self.boundary = data.endswith(b"\n")
        self.size += len(data)


class _Backlog:
    """Copies remises par les fils de relais, en attente d'inspection, bornées en octets.

    ``offer`` ne bloque jamais : au-delà de la borne, la copie devient une lacune, et
    les lacunes successives d'un même sens se fondent en une seule.
    """

    def __init__(self, limit: int, wake: Callable[[], None]) -> None:
        self._limit = limit
        self._wake = wake
        self._lock = threading.Lock()
        self._items: deque[_Copy | _Gap] = deque()
        self._size = 0
        # Lacune qui est la dernière remise de son sens : la suite s'y fond.
        self._gaps: dict[Direction, _Gap] = {}
        self._signaled = False
        self._closed = False

    def offer(self, direction: Direction, data: bytes, stamp: _Stamp) -> None:
        with self._lock:
            cost = len(data) + _ITEM_BYTES
            if self._size + cost <= self._limit:
                self._items.append(_Copy(direction, data, stamp))
                self._size += cost
                self._gaps.pop(direction, None)
            else:
                gap = self._gaps.get(direction)
                if gap is None:
                    gap = self._gaps[direction] = _Gap(direction)
                    self._items.append(gap)
                    self._size += _ITEM_BYTES
                gap.add(data)
            wake = not self._signaled
            self._signaled = True
        if wake:
            self._wake()

    def take(self) -> tuple[list[_Copy | _Gap], bool]:
        """Les copies en attente, et vrai quand plus rien ne viendra."""
        with self._lock:
            items = list(self._items)
            self._items.clear()
            self._gaps.clear()
            self._size = 0
            self._signaled = False
            return items, self._closed

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._wake()


@dataclass
class _Line:
    """Un message entier, tel que l'inspection l'a reconstitué."""

    # Le message, s'il tient dans la taille lue ; sinon son début seulement.
    content: bytes | None = field(repr=False)
    head: bytes = field(repr=False)
    # Octets du message, sans la fin de ligne.
    size: int
    # Lecture du premier morceau, écriture du dernier, travail du proxy sur ses morceaux.
    started: int
    ended: int
    added: int
    # Morceaux dont l'écriture n'était pas encore prête à l'inspection : comptés plus tard.
    unsettled: tuple[_Stamp, ...]
    digest: Digest | None = field(repr=False)


class _Lines:
    """Découpe un flux en messages, une ligne chacun, en mémoire bornée.

    Un message plus long que ``limit`` n'est pas gardé : seul son début
    (``PREFIX_BYTES``) l'est, et, avec une clé, son empreinte se calcule au fil de
    l'eau. Une lacune fait perdre le message en cours : la suite est sautée
    jusqu'à la fin de ligne suivante.
    """

    def __init__(self, limit: int, digests: Callable[[], Digest] | None) -> None:
        self._limit = limit
        self._digests = digests
        self.skipping = False
        self._reset()

    def _reset(self) -> None:
        self._parts: list[bytes] = []
        self._head = b""
        self._size = 0
        self._oversized = False
        self._started: int | None = None
        self._added = 0
        self._unsettled: list[_Stamp] = []
        self._digest = self._digests() if self._digests is not None else None

    @property
    def partial(self) -> bool:
        """Un message est commencé, ou sauté, sans être fini."""
        return self.skipping or self._started is not None

    def feed(self, data: bytes, stamp: _Stamp) -> list[_Line | None]:
        """Les messages que ce morceau achève ; ``None`` pour un message perdu."""
        done: list[_Line | None] = []
        start = 0
        while start < len(data):
            end = data.find(b"\n", start)
            if self.skipping:
                if end < 0:
                    break
                self.skipping = False
                done.append(None)
                start = end + 1
                continue
            self._take(data[start:] if end < 0 else data[start:end], stamp)
            if end < 0:
                break
            done.append(self._finish(stamp))
            start = end + 1
        return done

    def gap(self, gap: _Gap) -> int:
        """Une lacune : le message en cours est perdu, et ceux qu'elle achève. Rend leur
        nombre ; celui qui reste ouvert après elle sera compté à sa fin de ligne."""
        self._reset()
        self.skipping = not gap.boundary
        return gap.newlines

    def _take(self, piece: bytes, stamp: _Stamp) -> None:
        # Un message est d'un seul tenant : il ne prend qu'une part de chaque morceau, et le
        # travail du proxy sur ce morceau lui est compté une fois.
        if self._started is None:
            self._started = stamp.read_at
        if stamp.sent_at is not None:
            self._added += stamp.sent_at - stamp.read_at
        else:
            self._unsettled.append(stamp)
        self._size += len(piece)
        if self._digest is not None:
            self._digest.update(piece)
        if not self._oversized:
            if self._size <= self._limit:
                self._parts.append(piece)
                return
            self._oversized = True
            self._head = b"".join([*self._parts, piece[:PREFIX_BYTES]])[:PREFIX_BYTES]
            self._parts = []
        elif len(self._head) < PREFIX_BYTES:
            self._head += piece[: PREFIX_BYTES - len(self._head)]

    def _finish(self, stamp: _Stamp) -> _Line:
        ended = stamp.written_at or stamp.sent_at or stamp.read_at
        line = _Line(
            content=None if self._oversized else b"".join(self._parts),
            head=self._head,
            size=self._size,
            started=self._started if self._started is not None else stamp.read_at,
            ended=ended,
            added=self._added,
            unsettled=tuple(self._unsettled),
            digest=self._digest,
        )
        self._reset()
        return line


@dataclass
class _Call:
    """Un appel d'outil relayé, en attente de sa réponse.

    De sa requête, il ne garde que ce que l'inspection en a tiré et ses horodatages ;
    ``weight`` est la taille de la requête lue, qui borne ce que l'inspection garde.
    """

    inspection: Inspection
    trace: CallTrace
    oversized: bool
    started: int
    added: int
    unsettled: tuple[_Stamp, ...]
    # Réponses illisibles déjà vues quand l'appel est parti : s'il en vient d'autres et
    # que sa réponse manque, l'une d'elles était peut-être la sienne.
    unidentified: int
    weight: int


@dataclass
class _Pending:
    """Une requête en attente de sa réponse. ``replies`` dépasse 1 quand un identifiant
    revient avant sa réponse : il n'y a plus moyen de savoir à quelle requête répond
    une réponse, et l'appel d'outil qu'il portait est perdu."""

    call: _Call | None
    replies: int = 1


def _is_reply(message: object) -> bool:
    """Une réponse JSON-RPC : ni requête ni notification, un résultat ou une erreur."""
    return (
        isinstance(message, dict)
        and "method" not in message
        and ("result" in message or "error" in message)
    )


def _usable(identifier: object) -> TypeGuard[str | int]:
    """Un identifiant JSON-RPC que MCP admet : une chaîne ou un entier."""
    return isinstance(identifier, str) or (
        isinstance(identifier, int) and not isinstance(identifier, bool)
    )


class StdioProxy(CallCapture[StdioSettings]):
    """Lance un serveur MCP en stdio et relaie ses flux ; un événement par appel d'outil."""

    transport = "stdio"

    def __init__(
        self,
        settings: StdioSettings,
        sink: EventSink,
        *,
        input_fd: int = 0,
        output_fd: int = 1,
        environment: Mapping[str, str] | None = None,
        forwarded_signals: Sequence[signal.Signals] = FORWARDED_SIGNALS,
        clock: Callable[[], datetime] = _now,
        analyzer: SqlAnalyzer | None = None,
        coverage_window: timedelta = DEFAULT_WINDOW,
        retries: RetryMemory | None = None,
        backlog_bytes: int | None = None,
        pending_requests: int = PENDING_REQUESTS,
        pending_bytes: int | None = None,
    ) -> None:
        super().__init__(
            settings,
            sink,
            clock=clock,
            analyzer=analyzer,
            coverage_window=coverage_window,
            retries=retries,
        )
        self._input = input_fd
        self._output = output_fd
        self._environment = environment
        self._forwarded = tuple(forwarded_signals)
        limit = settings.max_inspected_bytes
        self._backlog_bytes = (
            backlog_bytes if backlog_bytes is not None else max(BACKLOG_BYTES, 4 * limit)
        )
        self._capacity = pending_requests
        self._pending_bytes = (
            pending_bytes if pending_bytes is not None else max(PENDING_BYTES, 4 * limit)
        )
        key = settings.fingerprint_key
        self._lines: dict[Direction, _Lines] = {
            _CLIENT: _Lines(limit, None),
            _SERVER: _Lines(limit, (lambda: key.digest(RESULT_LABEL)) if key is not None else None),
        }
        # Requêtes en attente, par empreinte de leur identifiant, à clé tirée au
        # démarrage : un identifiant reçu n'est jamais gardé tel quel, même en mémoire.
        self._identifier_key = secrets.token_bytes(32)
        self._requests: OrderedDict[bytes, _Pending] = OrderedDict()
        self._weight = 0
        # Réponses du serveur rattachées à aucune requête, faute d'avoir pu les lire.
        self._unidentified = 0
        # Côté qui a fermé le premier, et quand : il dit l'issue des appels restés sans réponse.
        self._first_end: Direction | None = None
        self._ended_at: int | None = None
        self._lagging_logged = False
        # Tube qui réveille les fils de relais quand l'enfant s'est arrêté.
        self._stop_read, self._stop_write = -1, -1

    # Le processus enfant et les fils de relais.

    async def run(self) -> int:
        """Lance le serveur, relaie jusqu'à son arrêt, puis rend son code de sortie."""
        try:
            process = subprocess.Popen(  # noqa: S603 — une liste d'arguments, jamais de shell.
                self.settings.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,
                env=self._child_environment(),
                bufsize=0,
            )
        except (OSError, ValueError) as error:
            return self._not_started(error)
        child_input, child_output = process.stdin, process.stdout
        if child_input is None or child_output is None:  # pragma: no cover — tubes demandés.
            raise RuntimeError("tubes de l'enfant manquants")
        loop = asyncio.get_running_loop()
        woken = asyncio.Event()

        def wake() -> None:
            loop.call_soon_threadsafe(woken.set)

        backlog = _Backlog(self._backlog_bytes, wake)
        self._stop_read, self._stop_write = os.pipe()
        pumps = [
            threading.Thread(
                target=self._pump_client,
                args=(child_input, backlog),
                name="relais-client",
                daemon=True,
            ),
            threading.Thread(
                target=self._pump_server,
                args=(child_output, backlog),
                name="relais-serveur",
                daemon=True,
            ),
        ]
        installed: list[signal.Signals] = []
        inspection: asyncio.Future[None] | None = None
        try:
            logger.info("Serveur MCP « %s » lancé en stdio.", self.settings.server_name)
            await self._start_reporting()
            for number in self._forwarded:
                loop.add_signal_handler(number, _forward, process, number)
                installed.append(number)
            for pump in pumps:
                pump.start()
            inspection = asyncio.ensure_future(self._inspect_backlog(backlog, woken))
            return exit_status(await asyncio.to_thread(process.wait))
        finally:
            if process.poll() is None:
                # Le proxy est interrompu : son enfant ne lui survit pas.
                process.kill()
                process.wait()
            self._note_end(_SERVER)
            for number in installed:
                loop.remove_signal_handler(number)
            os.write(self._stop_write, b"\0")
            await asyncio.to_thread(_join, [pump for pump in pumps if pump.ident is not None])
            # Le client voit la fin de la sortie du proxy, même si le relais n'a pas commencé.
            _release(self._output)
            backlog.close()
            if inspection is not None:
                await inspection
            self._settle_pending()
            child_input.close()
            child_output.close()
            os.close(self._stop_read)
            os.close(self._stop_write)
            await self.aclose()

    def _child_environment(self) -> dict[str, str]:
        environment = dict(os.environ if self._environment is None else self._environment)
        for name in WITHHELD_VARIABLES:
            environment.pop(name, None)
        return environment

    @staticmethod
    def _not_started(error: OSError | ValueError) -> int:
        """Une erreur claire, au texte fixe : ni la commande ni ses arguments n'y figurent."""
        if isinstance(error, FileNotFoundError):
            logger.error("Serveur MCP non lancé : commande introuvable.")
            return EXIT_NOT_FOUND
        if isinstance(error, PermissionError):
            logger.error("Serveur MCP non lancé : commande non exécutable.")
            return EXIT_NOT_EXECUTABLE
        logger.error("Serveur MCP non lancé (%s).", type(error).__name__)
        return EXIT_NOT_EXECUTABLE

    def _note_end(self, side: Direction) -> None:
        if self._first_end is None:
            self._first_end = side
            self._ended_at = time.monotonic_ns()

    def _wait_readable(self, fd: int, *, draining: bool = False) -> bool:
        """Attend des données sur ``fd`` ; faux si le relais doit s'arrêter avant.

        Après l'arrêt de l'enfant, la sortie se vide de ce qu'il a écrit sans
        attendre ce qu'un descendant y écrirait encore. Un flux en erreur se dit
        lisible : sa lecture le trouvera fermé.
        """
        try:
            if draining:
                readable, _, _ = select.select([fd], [], [], 0)
            else:
                readable, _, _ = select.select([fd, self._stop_read], [], [])
        except (OSError, ValueError):
            return True
        return fd in readable

    def _pump_client(self, child_input: IO[bytes], backlog: _Backlog) -> None:
        """Entrée du client vers l'entrée de l'enfant. La copie part avant l'écriture : une
        requête est toujours remise à l'inspection avant sa réponse."""
        target = child_input.fileno()
        try:
            while self._wait_readable(self._input):
                data = _read(self._input)
                if not data:
                    # Le client a fermé son entrée : l'enfant voit la fin de la sienne.
                    self._note_end(_CLIENT)
                    return
                stamp = _Stamp(time.monotonic_ns())
                backlog.offer(_CLIENT, data, stamp)
                stamp.sent_at = time.monotonic_ns()
                if not _write_all(target, data):
                    # L'enfant a fermé son entrée : celle du proxy se ferme aussi.
                    self._note_end(_SERVER)
                    _release(self._input)
                    return
        except Exception as error:
            logger.error("Relais vers le serveur MCP interrompu (%s).", type(error).__name__)
        finally:
            child_input.close()

    def _pump_server(self, child_output: IO[bytes], backlog: _Backlog) -> None:
        """Sortie de l'enfant vers la sortie du client. La copie part après l'écriture : une
        réponse n'est décrite que rendue."""
        source = child_output.fileno()
        draining = False
        try:
            while True:
                if not self._wait_readable(source, draining=draining):
                    if draining:
                        break
                    draining = True
                    continue
                stamp = _Stamp(time.monotonic_ns())
                data = _read(source)
                if not data:
                    break
                stamp.sent_at = time.monotonic_ns()
                if not _write_all(self._output, data):
                    # Le client ne lit plus : l'enfant le voit à sa prochaine écriture.
                    self._note_end(_CLIENT)
                    child_output.close()
                    return
                stamp.written_at = time.monotonic_ns()
                backlog.offer(_SERVER, data, stamp)
        except Exception as error:
            logger.error("Relais vers le client interrompu (%s).", type(error).__name__)
        # L'enfant a fermé sa sortie : le client voit la fin de la sienne.
        self._note_end(_SERVER)
        _release(self._output)

    # L'inspection, sur la boucle du proxy.

    async def _inspect_backlog(self, backlog: _Backlog, woken: asyncio.Event) -> None:
        while True:
            items, closed = backlog.take()
            for item in items:
                try:
                    self._consume(item)
                except Exception as error:
                    logger.error("Copie non inspectée (%s).", type(error).__name__)
            if closed:
                break
            await woken.wait()
            woken.clear()
        for direction, lines in self._lines.items():
            if lines.partial:
                # Un message inachevé à la fermeture du flux n'est pas un message.
                self._lost(direction, 1)

    def _consume(self, item: _Copy | _Gap) -> None:
        lines = self._lines[item.direction]
        if isinstance(item, _Gap):
            self.stats.lagged_bytes += item.size
            if not self._lagging_logged:
                self._lagging_logged = True
                logger.warning(
                    "Inspection en retard : des messages sont relayés sans être inspectés."
                )
            self._lost(item.direction, lines.gap(item))
            return
        for line in lines.feed(item.data, item.stamp):
            if line is None:
                self._lost(item.direction, 1)
                continue
            try:
                if item.direction == _CLIENT:
                    self.stats.relayed += 1
                    self._on_request(line)
                else:
                    self._on_reply(line)
            except Exception as error:
                logger.error("Message non inspecté (%s).", type(error).__name__)

    def _lost(self, direction: Direction, count: int) -> None:
        """Des messages relayés sans avoir pu être lus : une requête est comptée non
        inspectée, une réponse ne peut plus être rattachée."""
        for _ in range(count):
            if direction == _CLIENT:
                self.stats.relayed += 1
                self._inspected(None)
            else:
                self._unidentified_reply()

    def _unidentified_reply(self) -> None:
        self.stats.unidentified_replies += 1
        self._unidentified += 1

    def _key(self, identifier: str | int) -> bytes:
        text = f"s:{identifier}" if isinstance(identifier, str) else f"i:{identifier}"
        return hashlib.blake2b(
            text.encode("utf-8", "surrogatepass"), key=self._identifier_key, digest_size=16
        ).digest()

    def _on_request(self, line: _Line) -> None:
        """Un message du client : une requête, une notification, ou sa réponse au serveur."""
        if line.content is None:
            shape = read_message_prefix(line.head)
            if shape.reply and not shape.method:
                return
            inspection = inspect_prefix(line.head, [])
            if inspection is None or shape.identifier is None:
                # Appel d'outil qu'on ne pourrait pas suivre jusqu'à sa réponse : non décrit.
                self._inspected(None)
                return
            self.stats.inspected_from_prefix += 1
            self._call(inspection, shape.identifier, line, oversized=True)
            return
        message = parse_message(line.content)
        if _is_reply(message):
            # La réponse du client à une requête du serveur.
            return
        inspection = inspect_message(
            message,
            [],
            self._sql_arguments,
            keep_arguments=self.settings.fingerprint_key is not None,
        )
        self._inspected(inspection)
        if inspection is None or not isinstance(message, dict):
            return
        identifier = message.get("id")
        if not inspection.is_tool_call:
            if message.get("method") == CANCELLED:
                self._cancel(message.get("params"))
            elif _usable(identifier):
                self._expect(self._key(identifier), None)
            return
        if not _usable(identifier):
            # Sans identifiant, pas de réponse à attendre : l'appel n'est pas décrit.
            self._inspected(None)
            return
        self._call(inspection, identifier, line, oversized=False)

    def _call(
        self, inspection: Inspection, identifier: str | int, line: _Line, *, oversized: bool
    ) -> None:
        # Rien n'est jamais ajouté à un message : un contexte créé ne sert qu'au run.
        trace = replace(resolve_trace(inspection.trace_context, []), forwarded=None)
        call = _Call(
            inspection=inspection,
            trace=trace,
            oversized=oversized,
            started=line.started,
            added=line.added,
            unsettled=line.unsettled,
            unidentified=self._unidentified,
            weight=0 if oversized else line.size,
        )
        self._expect(self._key(identifier), call)

    def _expect(self, key: bytes, call: _Call | None) -> None:
        """Attend la réponse d'une requête. Un identifiant qui revient avant sa réponse rend
        les deux requêtes indiscernables : l'appel d'outil en attente est perdu, le nouveau
        n'est pas décrit."""
        known = self._requests.get(key)
        if known is not None:
            known.replies += 1
            self._lose(self._forget(known))
            if call is not None:
                self._inspected(None)
            return
        weight = call.weight if call is not None else 0
        while self._requests and (
            len(self._requests) >= self._capacity or self._weight + weight > self._pending_bytes
        ):
            _, oldest = self._requests.popitem(last=False)
            self._lose(self._forget(oldest))
        self._requests[key] = _Pending(call)
        self._weight += weight

    def _forget(self, pending: _Pending) -> _Call | None:
        call, pending.call = pending.call, None
        if call is not None:
            self._weight -= call.weight
        return call

    def _lose(self, call: _Call | None) -> None:
        """Un appel d'outil dont l'issue ne peut pas être établie : son événement est perdu,
        et compté comme tel."""
        if call is None:
            return
        self.stats.unmatched_calls += 1
        moment = self._clock()
        self.coverage.begin(moment)
        self.coverage.settle(moment, emitted=False)
        logger.warning("Appel d'outil sans issue établie : son événement est perdu.")

    def _cancel(self, params: object) -> None:
        """Le client renonce à une requête : son appel d'outil s'achève sur un abandon."""
        identifier = params.get("requestId") if isinstance(params, dict) else None
        if not _usable(identifier):
            return
        pending = self._requests.pop(self._key(identifier), None)
        if pending is None:
            return
        call = self._forget(pending)
        if call is not None:
            self._conclude(call, "ABORT", "client_abort", time.monotonic_ns())

    def _on_reply(self, line: _Line) -> None:
        """Un message du serveur : une réponse, ou une requête ou notification du serveur."""
        if line.content is None:
            shape = read_message_prefix(line.head)
            if shape.method:
                return
            if not shape.reply or shape.identifier is None:
                self._unidentified_reply()
                return
            prefix = read_reply_prefix(line.head)
            self._answered(shape.identifier, (prefix.error, bounded_code(prefix.code)), line)
            return
        message = parse_message(line.content)
        if isinstance(message, dict) and "method" in message:
            return
        identifier = message.get("id") if isinstance(message, dict) else None
        if not _is_reply(message) or not _usable(identifier):
            self._unidentified_reply()
            return
        self._answered(identifier, reply_error(message), line)

    def _answered(self, identifier: str | int, error: tuple[bool, int | None], line: _Line) -> None:
        key = self._key(identifier)
        pending = self._requests.get(key)
        if pending is None:
            return
        pending.replies -= 1
        if pending.replies <= 0:
            del self._requests[key]
        call = self._forget(pending)
        if call is not None:
            self._conclude(call, "COMPLETE", None, line.ended, reply=line, error=error)

    def _conclude(
        self,
        call: _Call,
        outcome: Outcome,
        failure: Failure | None,
        ended: int,
        *,
        reply: _Line | None = None,
        error: tuple[bool, int | None] = (False, None),
    ) -> None:
        """Décrit un appel terminé et émet son événement, avec les règles du mode HTTP.

        Sa latence va de la lecture du premier morceau de la requête à l'écriture du
        dernier morceau de la réponse ; sa latence ajoutée est le temps que le proxy a
        passé sur leurs morceaux, entre leur lecture et leur écriture."""
        added = call.added + sum(
            stamp.sent_at - stamp.read_at for stamp in call.unsettled if stamp.sent_at is not None
        )
        if reply is not None:
            added += reply.added
        exchange = _Exchange(
            outcome=outcome,
            trace=call.trace,
            timing=CallTiming.measured(started=call.started, added=added, ended=ended),
            failure=failure,
            delivered=reply is not None,
            observed=True,
            reply_error=error,
        )
        if reply is not None:
            exchange.size = reply.size
            exchange.relayed_whole = True
            exchange.digest = reply.digest
        self._describe(call.inspection, exchange, oversized=call.oversized)

    def _settle_pending(self) -> None:
        """À la fin du relais, les appels restés sans réponse : abandonnés par le client s'il
        est parti le premier, en échec sinon. Si une réponse illisible est passée depuis
        leur départ, leur issue n'est pas connue : leur événement est perdu."""
        client_left = self._first_end == _CLIENT
        ended = self._ended_at if self._ended_at is not None else time.monotonic_ns()
        for pending in self._requests.values():
            call = self._forget(pending)
            if call is None:
                continue
            if self._unidentified > call.unidentified:
                self._lose(call)
            elif client_left:
                self._conclude(call, "ABORT", "client_abort", ended)
            else:
                self._conclude(call, "FAIL", "transport_error", ended)
        self._requests.clear()

    def _assess(self, exchange: _Exchange) -> tuple[CallOutcome, int | None]:
        if exchange.failure is not None:
            return exchange.failure, None
        error, code = exchange.reply_error
        return ("jsonrpc_error", code) if error else ("success", None)


def _forward(process: subprocess.Popen[bytes], number: signal.Signals) -> None:
    """Transmet un signal reçu à l'enfant ; le proxy s'arrêtera avec lui."""
    logger.info("Signal %s transmis au serveur MCP.", number.name)
    process.send_signal(number)


def _read(fd: int) -> bytes:
    """Lit un morceau ; un flux en erreur se lit comme fermé."""
    try:
        return os.read(fd, _CHUNK_BYTES)
    except OSError:
        return b""


def _write_all(fd: int, data: bytes) -> bool:
    """Écrit tout le morceau ; faux si l'autre extrémité est fermée."""
    view = memoryview(data)
    try:
        while view:
            view = view[os.write(fd, view) :]
    except OSError:
        return False
    return True


def _release(fd: int) -> None:
    """Ferme un flux pour l'autre extrémité, sans libérer le descripteur : il désigne
    désormais ``/dev/null``, et aucune ouverture ultérieure ne le reprend."""
    null = os.open(os.devnull, os.O_RDWR)
    try:
        os.dup2(null, fd)
    finally:
        os.close(null)


def _join(threads: Sequence[threading.Thread]) -> None:
    for thread in threads:
        thread.join()


# Ligne de commande : answerlineage-proxy [réglages] -- <commande> [arguments…]

# Réglages du mode HTTP qui n'ont pas de sens en stdio : rien n'écoute sur le réseau.
_HTTP_ONLY: Final = {
    "upstream": "--upstream",
    "host": "--host",
    "port": "--port",
    "allow_host": "--allow-host",
    "allow_origin": "--allow-origin",
    "no_preserve_host": "--no-preserve-host",
}


def _split(arguments: Sequence[str]) -> tuple[list[str], list[str]]:
    """Les réglages qui précèdent ``--``, et la commande qui le suit."""
    split = list(arguments).index("--")
    return list(arguments[:split]), list(arguments[split + 1 :])


def settings_from_args(
    arguments: Sequence[str], environment: Mapping[str, str]
) -> tuple[StdioSettings, timedelta]:
    """Les réglages d'un proxy stdio : ceux qui précèdent ``--``, puis la commande."""
    from answerlineage_proxy.cli import build_parser, coverage_window_from_args

    options, command = _split(arguments)
    args = build_parser().parse_args(options)
    for name, option in _HTTP_ONLY.items():
        value = getattr(args, name)
        # Une option donnée, même à zéro : seuls ses défauts (rien, faux, liste vide) passent.
        if value is not None and value is not False and value != []:
            raise ProxyConfigurationError(f"L'option {option} est sans objet en mode stdio.")

    def tuned(value: int | None, variable: str, default: int) -> int:
        return value if value is not None else integer(environment, variable, default)

    settings = StdioSettings.create(
        command,
        args.server_name or environment.get(ENV_SERVER_NAME) or None,
        max_inspected_bytes=args.max_inspected_bytes,
        sql_tools=dict(SqlTool.parse(value) for value in args.sql_tool),
        sql_budget_ms=tuned(args.sql_budget_ms, ENV_SQL_BUDGET_MS, DEFAULT_SQL_BUDGET_MS),
        sql_queue_size=tuned(args.sql_queue_size, ENV_SQL_QUEUE_SIZE, DEFAULT_SQL_QUEUE_SIZE),
        sql_cache_size=tuned(args.sql_cache_size, ENV_SQL_CACHE_SIZE, DEFAULT_SQL_CACHE_SIZE),
        fingerprint_key=fingerprint_key(environment, args.fingerprint_key_file),
        read_rules=read_rules(environment, args.read_rules_file),
    )
    return settings, coverage_window_from_args(options, environment)


def main(arguments: Sequence[str], environment: Mapping[str, str]) -> int:
    """Lance le proxy stdio ; rend le code de sortie du serveur MCP.

    Avec une adresse d'API, ses événements partent par la boîte d'envoi, comme en HTTP.
    L'arrêt suit la fin du serveur : la boîte s'y vide dans son délai, qui borne le temps
    que la sortie du proxy peut suivre celle du serveur.
    """
    from answerlineage_proxy.cli import event_sink, outbox_settings_from_args

    try:
        settings, coverage_window = settings_from_args(arguments, environment)
        outbox = outbox_settings_from_args(_split(arguments)[0], environment)
    except ProxyConfigurationError as error:
        print(f"Configuration invalide : {error}", file=sys.stderr)
        return 2
    if settings.fingerprint_key is None:
        logger.warning(
            "Aucune clé d'empreinte (%s ou %s) : les événements ne portent ni empreinte "
            "des arguments ni empreinte du résultat.",
            ENV_FINGERPRINT_KEY,
            ENV_FINGERPRINT_KEY_FILE,
        )
    for tool in sorted(set(settings.read_rules) & set(settings.sql_tools)):
        # Un nom d'outil validé, jamais une valeur reçue.
        logger.warning(
            "La règle de lecture de l'outil « %s » est ignorée : c'est un outil SQL, "
            "sa requête est analysée.",
            tool,
        )
    proxy = StdioProxy(settings, event_sink(outbox), coverage_window=coverage_window)
    return asyncio.run(proxy.run())
