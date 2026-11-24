"""Boîte d'envoi : les événements et les relevés d'un émetteur partent vers l'API
d'ingestion, hors du chemin des appels.

Une panne d'AnswerLineage ne doit ni ralentir ni bloquer un agent. Prendre un
événement en charge (``emit``) est donc un ajout en mémoire, en temps constant,
sans entrée-sortie ni attente : aucun appel n'attend la boîte. Tout le reste se
fait à côté, dans deux tâches de fond, dans la boucle de l'émetteur :

- la sérialisation, dans un fil, qui rend l'interpréteur à la boucle entre deux
  événements : la boucle qui relaie les appels n'attend jamais un lot entier ;
- l'envoi, par lots d'au plus 500 événements et 1 Mio, compressés en gzip, à la
  route des lots de l'API ; les relevés de couverture passent avant, dans leur
  propre file, que les événements n'évincent jamais.

La boîte est bornée, en nombre d'événements et en octets. Pleine, elle
abandonne le plus ancien. Elle est en mémoire : l'émetteur reste sans état, et un
redémarrage perd ce qui n'était pas parti. Chaque perte, débordement, rejet par
l'API ou arrêt, est dite à qui écoute la boîte (``watch``) : le proxy la compte
dans la fenêtre de couverture de l'événement, et son relevé, corrigé au besoin,
la marque comme un trou du lignage.

Livraison au moins une fois : un lot dont l'issue n'est pas sûre est renvoyé,
et l'ingestion, idempotente, ne l'enregistre qu'une fois. Une API indisponible
est reprise avec une attente exponentielle, plafonnée et tirée au hasard, qui
respecte ``Retry-After`` dans une borne. À l'arrêt, la boîte se vide dans un
délai borné ; le reste est perdu, et le dernier relevé le dit si le temps le permet.

Avec un jeton (``credentials.py``), chaque envoi le porte. Il est demandé dans
la tâche d'envoi, jamais sur le chemin d'un appel : un émetteur OIDC en panne
se lit comme une API en panne, et les événements attendent dans la boîte. Un
refus ``401`` de l'API fait oublier le jeton, et l'envoi est repris avec un
nouveau.

Rien du contenu des événements, de l'adresse de l'API ni de ses réponses n'est
journalisé : un échec se dit par sa classe ou son statut, dans le journal de
l'émetteur qui emploie la boîte (``logger``).
"""

from __future__ import annotations

import asyncio
import gzip
import itertools
import json
import logging
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from random import Random
from typing import Any, Final, Literal

import httpx

from answerlineage_emitter.config import ConfigurationError
from answerlineage_emitter.credentials import Tokens, TokenUnavailableError
from answerlineage_emitter.sink import Settled, receipt

# Journal par défaut ; chaque émetteur donne le sien.
DEFAULT_LOGGER: Final = logging.getLogger("answerlineage_emitter")

DEFAULT_MAX_EVENTS: Final = 10_000
DEFAULT_MAX_BYTES: Final = 32 * 1024 * 1024
DEFAULT_FLUSH_SECONDS: Final[float] = 5
# Bornes des réglages : (minimum, maximum).
MAX_EVENTS_LIMITS: Final = (1, 1_000_000)
MAX_BYTES_LIMITS: Final = (1024 * 1024, 1024 * 1024 * 1024)
FLUSH_SECONDS_LIMITS: Final = (0, 300)

# Un lot : au plus 500 événements et 1 Mio avant compression, loin des bornes de
# l'API (1 000 événements, 16 Mio). Un événement plus lourd part seul.
BATCH_EVENTS: Final = 500
BATCH_BYTES: Final = 1024 * 1024
# Relevés en attente : une semaine de fenêtres d'une minute. Au-delà, le plus ancien
# se perd, et la période qu'il couvrait se lit comme un silence.
MAX_REPORTS: Final = 10_000
# Réponse de l'API lue au plus : le bilan d'un lot entièrement rejeté y tient.
RESPONSE_BYTES: Final = 1024 * 1024
# Délais d'un envoi : connexion, puis chaque lecture et écriture, puis l'envoi entier,
# qu'une réponse servie goutte à goutte ne prolonge pas.
CONNECT_TIMEOUT: Final = 5.0
READ_TIMEOUT: Final = 10.0
REQUEST_TIMEOUT: Final = 30.0
# Reprises : de 0,5 s, doublées à chaque échec, jusqu'à 30 s ; Retry-After jusqu'à 60 s.
BACKOFF_BASE: Final = 0.5
BACKOFF_CAP: Final = 30.0
RETRY_AFTER_CAP: Final = 60.0
# À l'arrêt, part du délai gardée pour remettre les derniers relevés.
_REPORT_RESERVE: Final = 0.2
_REPORT_RESERVE_MAX: Final = 1.0
_EVENTS_PATH: Final = b"/api/v1/lineage/batch"
_COVERAGE_PATH: Final = b"/api/v1/coverage"
_RETRY_AFTER_LENGTH: Final = 64
# httpx journalise l'URL de chaque requête, httpcore les en-têtes en mode débogage.
_CLIENT_LOGGERS: Final = ("httpx", "httpcore")


def _quiet_client_logs() -> None:
    for name in _CLIENT_LOGGERS:
        client_logger = logging.getLogger(name)
        if client_logger.level == logging.NOTSET or client_logger.level < logging.WARNING:
            client_logger.setLevel(logging.WARNING)


@dataclass(frozen=True)
class OutboxSettings:
    """L'adresse de l'API d'AnswerLineage et les bornes de la boîte d'envoi."""

    api_url: httpx.URL
    max_events: int = DEFAULT_MAX_EVENTS
    max_bytes: int = DEFAULT_MAX_BYTES
    # Délai de vidage à l'arrêt, en secondes.
    flush_seconds: float = DEFAULT_FLUSH_SECONDS

    @classmethod
    def create(
        cls,
        api_url: str,
        *,
        max_events: int = DEFAULT_MAX_EVENTS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        flush_seconds: float = DEFAULT_FLUSH_SECONDS,
    ) -> OutboxSettings:
        """Contrôle l'adresse, réglée par l'exploitant au démarrage, et les bornes.

        L'adresse est en http ou https, sans identifiant, paramètre ni fragment :
        elle peut apparaître dans la configuration. Aucun message ne la cite.
        """
        try:
            url = httpx.URL(api_url)
        except (httpx.InvalidURL, TypeError):
            raise ConfigurationError("L'adresse de l'API est illisible.") from None
        if url.scheme not in ("http", "https") or not url.host:
            raise ConfigurationError("L'adresse de l'API doit être une URL http ou https.")
        if url.userinfo:
            raise ConfigurationError("L'adresse de l'API ne doit contenir aucun identifiant.")
        if url.query or url.fragment:
            raise ConfigurationError("L'adresse de l'API ne doit porter ni paramètres ni fragment.")
        for name, value, (minimum, maximum) in (
            ("max_events", max_events, MAX_EVENTS_LIMITS),
            ("max_bytes", max_bytes, MAX_BYTES_LIMITS),
            ("flush_seconds", flush_seconds, FLUSH_SECONDS_LIMITS),
        ):
            if not minimum <= value <= maximum:
                raise ConfigurationError(f"{name} doit être compris entre {minimum} et {maximum}.")
        return cls(url, max_events, max_bytes, flush_seconds)

    def _route(self, path: bytes) -> httpx.URL:
        return self.api_url.copy_with(raw_path=self.api_url.raw_path.rstrip(b"/") + path)

    @property
    def events_url(self) -> httpx.URL:
        return self._route(_EVENTS_PATH)

    @property
    def coverage_url(self) -> httpx.URL:
        return self._route(_COVERAGE_PATH)


@dataclass
class OutboxStats:
    """Compteurs de la boîte d'envoi, sans aucune valeur issue des événements."""

    # Événements remis : l'API les a acceptés.
    sent: int = 0
    # Abandonnés, boîte pleine : le plus ancien part le premier.
    dropped_overflow: int = 0
    # Perdus à l'arrêt, faute de temps pour les envoyer, ou pris en charge après l'arrêt.
    dropped_shutdown: int = 0
    # Rejetés par l'API, ou impossibles à sérialiser : jamais retentés.
    rejected: int = 0
    # Envois repris après un échec.
    retries: int = 0
    # Relevés de couverture remis, et perdus : file pleine, refusés par l'API, ou arrêt.
    reports_sent: int = 0
    reports_dropped: int = 0


class _Entry:
    """Un événement dans la boîte : d'abord tel quel, puis sérialisé."""

    __slots__ = ("data", "dropped", "enqueued", "event", "receipt")

    def __init__(self, event: Mapping[str, Any], enqueued: float) -> None:
        self.event: Mapping[str, Any] | None = event
        self.receipt = receipt(event)
        self.enqueued = enqueued
        self.data: bytes | None = None
        self.dropped = False


def _encode(events: Sequence[Mapping[str, Any]]) -> list[bytes | None]:
    """Sérialise des événements, dans un fil. ``None`` pour un événement qui n'est pas du JSON.

    Après chaque événement, le fil rend l'interpréteur : la boucle qui relaie les
    appels n'attend jamais plus que la sérialisation d'un seul événement.
    """
    encoded: list[bytes | None] = []
    for event in events:
        try:
            text = json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            encoded.append(text.encode("utf-8"))
        except (TypeError, ValueError, RecursionError):
            encoded.append(None)
        time.sleep(0)
    return encoded


def _compress(parts: Sequence[bytes]) -> bytes:
    return gzip.compress(b"[" + b",".join(parts) + b"]", compresslevel=6, mtime=0)


def retry_delay(headers: httpx.Headers, now: datetime) -> float | None:
    """L'attente que demande ``Retry-After`` : des secondes ou une date, bornée à 60 s.

    Une valeur illisible ne demande rien : la reprise suit alors sa propre attente.
    """
    value = headers.get("retry-after")
    if value is None or len(value) > _RETRY_AFTER_LENGTH:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        return min(float(value), RETRY_AFTER_CAP)
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if moment.tzinfo is None:
        return None
    return min(max(0.0, (moment - now).total_seconds()), RETRY_AFTER_CAP)


def _rejected_indexes(body: bytes, size: int) -> set[int]:
    """Les rangs que le bilan d'un lot désigne comme rejetés : ceux qui ont la forme attendue.

    Un bilan illisible ne désigne rien.
    """
    try:
        result = json.loads(body)
    except ValueError:
        return set()
    rejected = result.get("rejected") if isinstance(result, dict) else None
    if not isinstance(rejected, list):
        return set()
    return {
        item["index"]
        for item in rejected
        if isinstance(item, dict) and type(item.get("index")) is int and 0 <= item["index"] < size
    }


Verdict = Literal["delivered", "rejected", "split", "retry"]


@dataclass(frozen=True)
class _Answer:
    verdict: Verdict
    retry_after: float | None = None
    rejected: frozenset[int] = frozenset()
    reason: str = ""


def _batch_answer(status: int, body: bytes | None, size: int) -> _Answer:
    """Ce que fait la boîte d'une réponse de la route des lots."""
    if 200 <= status < 300:
        readable = body is not None
        rejected = _rejected_indexes(body, size) if readable and body else set()
        return _Answer("delivered", rejected=frozenset(rejected))
    if status == 422:
        # Aucun événement du lot n'est valide.
        return _Answer("rejected")
    if status in (400, 413):
        # Le corps entier est refusé : coupé en deux, le lot isole l'événement fautif.
        return _Answer("split")
    return _Answer("retry", reason=f"statut {status}")


class Outbox:
    """Destination à remise différée : la boîte d'envoi vers l'API d'ingestion."""

    def __init__(
        self,
        settings: OutboxSettings,
        *,
        client: httpx.AsyncClient | None = None,
        request_timeout: float = REQUEST_TIMEOUT,
        backoff: tuple[float, float] = (BACKOFF_BASE, BACKOFF_CAP),
        max_reports: int = MAX_REPORTS,
        random: Random | None = None,
        clock: Callable[[], float] = time.monotonic,
        tokens: Tokens | None = None,
        logger: logging.Logger = DEFAULT_LOGGER,
    ) -> None:
        _quiet_client_logs()
        self._logger = logger
        self.settings = settings
        self.stats = OutboxStats()
        self._tokens = tokens
        self._client = client
        self._owns_client = client is None
        self._request_timeout = request_timeout
        self._backoff = backoff
        self._max_reports = max_reports
        self._random = random or Random()  # noqa: S311 — une gigue, pas un secret.
        self._clock = clock
        self._settled: Settled | None = None
        # Les événements, du plus ancien au plus récent : sérialisés, puis tels quels.
        self._ready: deque[_Entry] = deque()
        self._ready_bytes = 0
        self._raw: deque[_Entry] = deque()
        # Le lot en cours d'envoi, hors des bornes de la file.
        self._sending: list[_Entry] = []
        self._batch_limit = BATCH_EVENTS
        # Relevés en attente, un par fenêtre : un relevé corrigé remplace le précédent.
        self._reports: OrderedDict[tuple[str, ...], Mapping[str, Any]] = OrderedDict()
        # Le relevé en cours d'envoi : interrompu à l'arrêt, il est retenté une fois.
        self._report_in_flight: tuple[tuple[str, ...], Mapping[str, Any]] | None = None
        self._attempt = 0
        self._retry_at = 0.0
        self._failing = False
        self._overflowing = False
        self._encode_wanted = asyncio.Event()
        self._send_wanted = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._closing = False
        # Arrêtée : les événements ne sont plus pris en charge ; puis les relevés non plus,
        # une fois remis les derniers, qui disent les pertes de l'arrêt.
        self._closed = False
        self._finished = False

    # Prise en charge, sur le chemin de l'appel : rien n'y attend.

    def watch(self, settled: Settled) -> None:
        self._settled = settled

    def emit(self, event: Mapping[str, Any]) -> None:
        """Ajoute l'événement à la boîte ; pleine, elle abandonne le plus ancien."""
        entry = _Entry(event, self._clock())
        if self._closed:
            entry.event = None
            self.stats.dropped_shutdown += 1
            self._settle(entry, delivered=False)
            return
        self._raw.append(entry)
        while len(self._ready) + len(self._raw) > self.settings.max_events:
            self._evict()
        self._encode_wanted.set()

    def report_coverage(self, report: Mapping[str, Any]) -> None:
        """Met le relevé en file, devant les événements ; celui d'une fenêtre déjà en file
        est remplacé par sa correction."""
        if self._finished:
            self.stats.reports_dropped += 1
            return
        key = _report_key(report)
        self._reports[key] = report
        if len(self._reports) > self._max_reports:
            self._reports.popitem(last=False)
            self.stats.reports_dropped += 1
        self._send_wanted.set()

    @property
    def queued(self) -> int:
        """Événements en file, et dans le lot en cours d'envoi."""
        return len(self._ready) + len(self._raw) + len(self._sending)

    @property
    def queued_bytes(self) -> int:
        """Octets des événements sérialisés en file, hors du lot en cours d'envoi."""
        return self._ready_bytes

    @property
    def oldest_age(self) -> float:
        """Âge du plus ancien événement en attente, en secondes ; zéro sans événement."""
        oldest = [queue[0].enqueued for queue in (self._sending, self._ready, self._raw) if queue]
        return max(0.0, self._clock() - min(oldest)) if oldest else 0.0

    def _settle(self, entry: _Entry, *, delivered: bool) -> None:
        """Dit l'issue d'un événement. Un échec de qui écoute n'atteint jamais la boîte."""
        if self._settled is None:
            return
        try:
            self._settled(entry.receipt, delivered)
        except Exception as error:
            self._logger.error("Issue d'un événement non comptée (%s).", type(error).__name__)

    def _evict(self) -> None:
        entry = self._ready.popleft() if self._ready else self._raw.popleft()
        if entry.data is not None:
            self._ready_bytes -= len(entry.data)
        entry.dropped = True
        entry.event = entry.data = None
        self.stats.dropped_overflow += 1
        if not self._overflowing:
            self._overflowing = True
            self._logger.warning(
                "Boîte d'envoi pleine : les événements les plus anciens sont abandonnés et "
                "comptés perdus."
            )
        self._settle(entry, delivered=False)

    # Tâches de fond.

    async def start(self) -> None:
        if self._tasks or self._closed:
            return
        if self._client is None:
            # Certificat vérifié, aucune redirection suivie, rien de l'environnement (ni
            # mandataire, ni fichier d'identifiants) : seule l'adresse réglée est jointe.
            self._client = httpx.AsyncClient(
                verify=True,
                follow_redirects=False,
                trust_env=False,
                timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
                limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
            )
            # Rien d'autre que ce que la boîte envoie : ni agent, ni encodages annoncés.
            self._client.headers.clear()
        self._tasks = [
            asyncio.ensure_future(self._encode_loop()),
            asyncio.ensure_future(self._send_loop()),
        ]

    async def _encode_loop(self) -> None:
        while True:
            while not self._raw:
                self._encode_wanted.clear()
                await self._encode_wanted.wait()
            try:
                await self._encode_some()
            except Exception as error:  # pragma: no cover — garde-fou de la tâche de fond.
                self._logger.error("Sérialisation en échec (%s).", type(error).__name__)
                await asyncio.sleep(self._backoff[0])

    async def _encode_some(self) -> None:
        # Tout événement encore en file a son contenu : il ne le perd qu'en la quittant.
        chunk = [
            (entry, entry.event)
            for entry in itertools.islice(self._raw, BATCH_EVENTS)
            if entry.event is not None
        ]
        encoded = await asyncio.to_thread(_encode, [event for _, event in chunk])
        for (entry, _), data in zip(chunk, encoded, strict=True):
            if entry.dropped:
                # Abandonné pendant la sérialisation : déjà compté, déjà sorti de la file.
                continue
            if self._raw and self._raw[0] is entry:
                self._raw.popleft()
            else:  # pragma: no cover — la file ne perd que ses plus anciens.
                self._raw.remove(entry)
            entry.event = None
            if data is None or len(data) > self.settings.max_bytes:
                self.stats.rejected += 1
                self._logger.warning(
                    "Événement impossible à envoyer : il est abandonné et compté perdu."
                )
                self._settle(entry, delivered=False)
                continue
            entry.data = data
            self._ready.append(entry)
            self._ready_bytes += len(data)
        self._bound_bytes()
        self._send_wanted.set()

    def _bound_bytes(self) -> None:
        while self._ready_bytes > self.settings.max_bytes and self._ready:
            self._evict()

    async def _send_loop(self) -> None:
        while True:
            while not (self._reports or self._ready):
                self._send_wanted.clear()
                await self._send_wanted.wait()
            delay = self._retry_at - self._clock()
            if delay > 0:
                await asyncio.sleep(delay)
                continue
            try:
                await self._send_next()
            except Exception as error:  # pragma: no cover — garde-fou de la tâche de fond.
                self._logger.error("Envoi en échec (%s).", type(error).__name__)
                self._failed(None, type(error).__name__)

    async def _send_next(self) -> None:
        if self._reports:
            await self._send_report()
        else:
            await self._send_batch()

    async def _post(
        self, url: httpx.URL, content: bytes, headers: Mapping[str, str]
    ) -> tuple[int, httpx.Headers, bytes | None]:
        """Un envoi, borné en durée ; la réponse lue au plus jusqu'à sa borne, ``None`` au-delà.

        Avec un jeton, l'envoi le porte ; ``TokenUnavailableError`` quand l'émetteur n'en
        donne pas. Un refus ``401`` le fait oublier : l'envoi repris en demandera un autre.
        """
        if self._client is None:  # pragma: no cover — démarrée avant tout envoi.
            raise RuntimeError("boîte d'envoi non démarrée")
        if self._tokens is not None:
            headers = {**headers, "authorization": f"Bearer {await self._tokens.token()}"}
        async with asyncio.timeout(self._request_timeout):
            async with self._client.stream("POST", url, content=content, headers=headers) as reply:
                if reply.status_code == 401 and self._tokens is not None:
                    self._tokens.invalidate()
                body = bytearray()
                async for chunk in reply.aiter_bytes():
                    body += chunk
                    if len(body) > RESPONSE_BYTES:
                        return reply.status_code, reply.headers, None
                return reply.status_code, reply.headers, bytes(body)

    async def _send_report(self) -> None:
        key, report = self._reports.popitem(last=False)
        try:
            content = json.dumps(report, allow_nan=False, separators=(",", ":")).encode()
        except (TypeError, ValueError):  # pragma: no cover — un relevé est fait de nombres.
            self.stats.reports_dropped += 1
            return
        self._report_in_flight = (key, report)
        try:
            status, headers, _ = await self._post(
                self.settings.coverage_url, content, {"content-type": "application/json"}
            )
        except (httpx.HTTPError, TimeoutError, TokenUnavailableError) as error:
            self._report_in_flight = None
            self._requeue_report(key, report)
            self._failed(None, _failure(error))
            return
        self._report_in_flight = None
        if 200 <= status < 300:
            self.stats.reports_sent += 1
            self._recovered()
        elif status in (400, 413, 422):
            # Un relevé refusé se lit comme un silence : jamais comme une période couverte.
            self.stats.reports_dropped += 1
            self._recovered()
            self._logger.warning("Relevé de couverture refusé par l'API (statut %d).", status)
        else:
            self._requeue_report(key, report)
            self._failed(retry_delay(headers, datetime.now(UTC)), f"statut {status}")

    def _requeue_report(self, key: tuple[str, ...], report: Mapping[str, Any]) -> None:
        """Remet un relevé en tête, sauf si sa correction est arrivée entre-temps."""
        if key not in self._reports:
            self._reports[key] = report
            self._reports.move_to_end(key, last=False)

    def _take_batch(self) -> list[_Entry]:
        batch: list[_Entry] = []
        size = 2
        while self._ready and len(batch) < self._batch_limit:
            data = self._ready[0].data
            weight = len(data) + 1 if data is not None else 0
            if batch and size + weight > BATCH_BYTES:
                break
            entry = self._ready.popleft()
            self._ready_bytes -= weight - 1
            size += weight
            batch.append(entry)
        return batch

    async def _send_batch(self) -> None:
        batch = self._sending = self._take_batch()
        parts = [entry.data for entry in batch if entry.data is not None]
        try:
            content = await asyncio.to_thread(_compress, parts)
            status, headers, body = await self._post(
                self.settings.events_url,
                content,
                {"content-type": "application/json", "content-encoding": "gzip"},
            )
        except (httpx.HTTPError, TimeoutError, TokenUnavailableError) as error:
            self._sending = []
            self._requeue(batch)
            self._failed(None, _failure(error))
            return
        # Interrompu à l'arrêt, le lot reste en cours d'envoi : l'arrêt le compte perdu.
        self._sending = []
        answer = _batch_answer(status, body, len(batch))
        if answer.verdict == "retry":
            self._requeue(batch)
            self._failed(retry_delay(headers, datetime.now(UTC)), answer.reason)
            return
        self._recovered()
        if answer.verdict == "split" and len(batch) > 1:
            self._batch_limit = max(1, len(batch) // 2)
            self.stats.retries += 1
            self._requeue(batch)
            return
        if answer.verdict != "delivered":
            # Rejet de tout le lot, ou d'un événement seul que l'API refuse.
            self._batch_limit = BATCH_EVENTS
            answer = _Answer("delivered", rejected=frozenset(range(len(batch))))
        if answer.rejected:
            self._logger.warning(
                "%d événements rejetés par l'API : abandonnés et comptés perdus.",
                len(answer.rejected),
            )
        for index, entry in enumerate(batch):
            entry.data = None
            delivered = index not in answer.rejected
            if delivered:
                self.stats.sent += 1
            else:
                self.stats.rejected += 1
            self._settle(entry, delivered=delivered)

    def _requeue(self, batch: list[_Entry]) -> None:
        """Remet un lot en tête de file ; au-delà des bornes, les plus anciens partent."""
        for entry in reversed(batch):
            self._ready.appendleft(entry)
            self._ready_bytes += len(entry.data) if entry.data is not None else 0
        while len(self._ready) + len(self._raw) > self.settings.max_events:
            self._evict()
        self._bound_bytes()

    def _failed(self, retry_after: float | None, reason: str) -> None:
        """Un envoi a échoué : la prochaine tentative attend, de plus en plus, au hasard."""
        self.stats.retries += 1
        self._attempt += 1
        base, cap = self._backoff
        delay = min(cap, base * 2 ** min(self._attempt - 1, 32))
        delay *= self._random.uniform(0.5, 1.0)
        if retry_after is not None:
            delay = max(delay, retry_after)
        self._retry_at = self._clock() + delay
        if not self._failing:
            self._failing = True
            self._logger.warning(
                "API d'ingestion indisponible (%s) : les événements attendent dans la boîte "
                "d'envoi.",
                reason,
            )

    def _recovered(self) -> None:
        self._attempt = 0
        self._retry_at = 0.0
        self._overflowing = False
        if self._failing:
            self._failing = False
            self._logger.info("API d'ingestion de nouveau joignable : la boîte d'envoi se vide.")

    # Arrêt.

    def _pending(self) -> bool:
        return bool(
            self._raw
            or self._ready
            or self._sending
            or self._reports
            or self._report_in_flight is not None
        )

    async def aclose(self) -> None:
        """Vide la boîte dans le délai d'arrêt ; ce qui reste est perdu, et dit.

        Une part du délai est gardée pour les derniers relevés, qui disent ces pertes.
        Un lot interrompu à l'arrêt est compté perdu, même si l'API l'a reçu : le trou
        est alors surestimé, jamais caché.
        """
        if self._closing:
            return
        self._closing = True
        delay = self.settings.flush_seconds
        deadline = self._clock() + delay
        if delay > 0 and self._pending():
            await self.start()
            reserve = min(_REPORT_RESERVE_MAX, delay * _REPORT_RESERVE)
            while self._pending() and self._clock() < deadline - reserve:
                await asyncio.sleep(0.01)
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self._closed = True
        if self._report_in_flight is not None:
            self._requeue_report(*self._report_in_flight)
            self._report_in_flight = None
        lost = [*self._sending, *self._ready, *self._raw]
        self._sending, self._ready, self._raw, self._ready_bytes = [], deque(), deque(), 0
        if lost:
            self._logger.warning(
                "Arrêt : %d événements non envoyés sont comptés perdus.", len(lost)
            )
        for entry in lost:
            entry.event = entry.data = None
            self.stats.dropped_shutdown += 1
            self._settle(entry, delivered=False)
        await self._last_reports(deadline)
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _last_reports(self, deadline: float) -> None:
        """Remet les derniers relevés, chacun une fois, tant que le délai le permet."""
        while self._reports and self._client is not None:
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            key, report = self._reports.popitem(last=False)
            try:
                content = json.dumps(report, allow_nan=False, separators=(",", ":")).encode()
                async with asyncio.timeout(remaining):
                    status, _, _ = await self._post(
                        self.settings.coverage_url, content, {"content-type": "application/json"}
                    )
            except (httpx.HTTPError, TimeoutError, TokenUnavailableError, TypeError, ValueError):
                self._reports[key] = report
                self._reports.move_to_end(key, last=False)
                break
            if 200 <= status < 300:
                self.stats.reports_sent += 1
            else:
                self.stats.reports_dropped += 1
        self.stats.reports_dropped += len(self._reports)
        self._reports.clear()
        self._finished = True


def _failure(error: Exception) -> str:
    """Le motif d'un envoi en échec : un texte fixe ou la classe de l'erreur, jamais son texte."""
    if isinstance(error, TokenUnavailableError):
        return "jeton indisponible"
    return type(error).__name__


def _report_key(report: Mapping[str, Any]) -> tuple[str, ...]:
    """La fenêtre d'un relevé : son émetteur, son instance et ses bornes."""
    window = report.get("window")
    bounds = window if isinstance(window, Mapping) else {}
    return (
        str(report.get("emitter")),
        str(report.get("instance")),
        str(bounds.get("from")),
        str(bounds.get("to")),
    )
