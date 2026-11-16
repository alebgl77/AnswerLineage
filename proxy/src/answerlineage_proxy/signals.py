"""Signaux de qualité de service d'un appel d'outil : latence, issue, reprises.

Le proxy ne voit ni modèle, ni jetons, ni coût. Il voit l'échange : combien de
temps il a duré, combien le proxy y a ajouté, comment il s'est terminé, et si
l'appel reprend un appel précédent qui a échoué.

Sur le chemin de l'appel, le proxy ne fait que prendre des horodatages, sur une
horloge monotone, et garder une copie bornée de la réponse (son début pour du
JSON, sa fin pour un flux SSE). Tout le reste, la lecture d'une erreur
JSON-RPC, l'issue, les reprises, se calcule une fois l'échange terminé.

Une erreur se dit par une catégorie et un code numérique, jamais par son
message : un message d'erreur peut citer les arguments. Rien de ce module ne
conserve ni ne journalise une valeur reçue ; l'identifiant de session MCP, qui
sert à reconnaître une reprise, n'est gardé que sous une empreinte à clé tirée
au démarrage.

Ce module ne dépend pas du transport : le relais HTTP prend les horodatages et
remet ce qu'il a vu, ``assess`` et ``RetryMemory`` en tirent les signaux.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, get_args

from answerlineage_proxy.prefix import read_reply_prefix
from answerlineage_proxy.tracecontext import CallTrace

CallOutcome = Literal[
    "success", "jsonrpc_error", "http_error", "transport_error", "timeout", "client_abort"
]
OUTCOMES: Final[tuple[str, ...]] = get_args(CallOutcome)
# Issue d'un échange qui n'a pas abouti, établie par le relais lui-même.
Failure = Literal["transport_error", "timeout", "client_abort"]
# Issues comptées comme une erreur : un appel abandonné par le client n'en est pas une.
ERRORS: Final = frozenset({"jsonrpc_error", "http_error", "transport_error", "timeout"})

# Code d'erreur recopié : un entier signé sur 32 bits. Au-delà, le code est tu.
ERROR_CODE_MIN: Final = -(2**31)
ERROR_CODE_MAX: Final = 2**31 - 1
# Borne des durées, en millisecondes, et des compteurs : celle de l'API d'ingestion.
SIGNAL_MAX: Final = 1_000_000_000

# Octets de la réponse gardés pour y lire une erreur JSON-RPC : le début d'un corps
# JSON, la fin d'un flux SSE, où le serveur écrit la réponse avant de le fermer.
SAMPLE_BYTES: Final = 16 * 1024

# Reprises : un appel reprend le précédent s'il commence au plus tard une minute
# après sa fin. Au-delà de 10 000 chaînes de reprises ouvertes, la plus ancienne
# est oubliée.
RETRY_WINDOW_NS: Final = 60 * 1_000_000_000
RETRY_CHAINS: Final = 10_000

_NS_PER_MS: Final = 1_000_000
_JSON_TYPES: Final = ("application/json",)
_SSE_TYPE: Final = "text/event-stream"


class CallTiming:
    """Horodatages d'un échange, sur une horloge monotone, en nanosecondes.

    ``started`` est l'arrivée de la requête, ``ended`` le moment où la réponse
    a été rendue, ou l'échange abandonné. Entre ``resume`` et ``pause``, le
    proxy travaille pour l'appel sans attendre ni le client ni le serveur : ce
    temps est la latence qu'il ajoute. L'horloge démarre en travail.
    """

    __slots__ = ("_clock", "_since", "added", "ended", "started")

    def __init__(self, clock: Callable[[], int] = time.monotonic_ns) -> None:
        self._clock = clock
        self.started = clock()
        self._since: int | None = self.started
        self.added = 0
        self.ended: int | None = None

    def pause(self) -> None:
        """Le proxy attend : le temps écoulé depuis ``resume`` lui est compté."""
        if self._since is not None:
            self.added += self._clock() - self._since
            self._since = None

    def resume(self) -> None:
        """Le proxy reprend son travail pour l'appel."""
        if self._since is None:
            self._since = self._clock()

    def finish(self) -> None:
        """L'échange est terminé. Seul le premier appel compte."""
        if self.ended is None:
            self.pause()
            self.ended = self._clock()


class ReplySample:
    """Copie bornée d'une réponse, pour y lire une erreur JSON-RPC une fois l'échange fini.

    Un corps JSON en garde le début, un flux SSE la fin ; tout autre corps, ou un
    corps compressé, rien. Un morceau de flux SSE est gardé par référence, sans
    copie, tant qu'il compte dans les derniers ``SAMPLE_BYTES`` octets.
    """

    __slots__ = ("_kept", "_kind", "_parts", "size")

    def __init__(self, content_type: str | None, content_encoding: str | None) -> None:
        media = (content_type or "").split(";", 1)[0].strip().lower()
        encoded = content_encoding is not None and content_encoding.strip().lower() != "identity"
        self._kind: Literal["json", "sse"] | None = None
        if not encoded and media in _JSON_TYPES:
            self._kind = "json"
        elif not encoded and media == _SSE_TYPE:
            self._kind = "sse"
        self._parts: deque[bytes] = deque()
        self._kept = 0
        self.size = 0

    def add(self, chunk: bytes) -> None:
        if self._kind is None:
            return
        self.size += len(chunk)
        if self._kind == "json":
            if self._kept < SAMPLE_BYTES:
                part = chunk[: SAMPLE_BYTES - self._kept]
                self._parts.append(part)
                self._kept += len(part)
            return
        self._parts.append(chunk)
        self._kept += len(chunk)
        while self._kept - len(self._parts[0]) >= SAMPLE_BYTES:
            self._kept -= len(self._parts.popleft())

    def error(self) -> tuple[bool, int | None]:
        """L'erreur JSON-RPC que la réponse porte, et son code s'il est lisible et borné."""
        if self._kind is None or not self._parts:
            return False, None
        data = b"".join(self._parts)
        whole = self._kept == self.size
        if self._kind == "json":
            if whole:
                return _reply_error(_strict_json(data))
            prefix = read_reply_prefix(data)
            return prefix.error, _bounded(prefix.code)
        if len(data) > SAMPLE_BYTES:
            data, whole = data[-SAMPLE_BYTES:], False
        return _event_stream_error(data, whole=whole)


def _unambiguous(pairs: list[tuple[str, object]]) -> dict[str, object]:
    members = dict(pairs)
    if len(members) != len(pairs):
        raise ValueError("clé répétée")
    return members


def _strict_json(data: bytes) -> object:
    """Le document, ou ``None`` s'il est illisible ou ambigu (clé répétée)."""
    try:
        return json.loads(data, object_pairs_hook=_unambiguous)
    except (ValueError, RecursionError):
        return None


def _bounded(code: object) -> int | None:
    # Un booléen est un entier pour Python, pas pour JSON-RPC.
    if isinstance(code, int) and not isinstance(code, bool):
        return code if ERROR_CODE_MIN <= code <= ERROR_CODE_MAX else None
    return None


def _reply_error(message: object) -> tuple[bool, int | None]:
    if not isinstance(message, dict):
        return False, None
    error = message.get("error")
    if error is None:
        return False, None
    return True, _bounded(error.get("code")) if isinstance(error, dict) else None


def _is_reply(message: object) -> bool:
    """Une réponse JSON-RPC : ni requête ni notification, un résultat ou une erreur."""
    return (
        isinstance(message, dict)
        and "method" not in message
        and ("result" in message or "error" in message)
    )


def _event_stream_error(data: bytes, *, whole: bool) -> tuple[bool, int | None]:
    """La dernière réponse JSON-RPC d'un flux SSE, lue dans sa fin.

    Le premier événement d'une fin tronquée est incomplet, le dernier aussi
    quand le flux ne s'achève pas sur une ligne vide : ni l'un ni l'autre n'est lu.
    """
    text = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    events = text.split(b"\n\n")[:-1]
    if not whole:
        events = events[1:]
    for event in reversed(events):
        lines = [line[5:] for line in event.split(b"\n") if line.startswith(b"data:")]
        if not lines:
            continue
        message = _strict_json(b"\n".join(line.removeprefix(b" ") for line in lines))
        if _is_reply(message):
            return _reply_error(message)
    return False, None


def assess(
    *, failure: Failure | None, status: int | None, sample: ReplySample | None
) -> tuple[CallOutcome, int | None]:
    """Issue d'un échange et son code d'erreur.

    Dans cet ordre : un échec du relais (transport, délai, client parti) ; un
    statut HTTP d'erreur, dont le code est le statut ; une erreur JSON-RPC dans
    la réponse, dont le code est celui de l'erreur ; sinon, un succès.
    """
    if failure is not None:
        return failure, None
    if status is None:
        return "transport_error", None
    if status >= 400:
        return "http_error", status
    if sample is not None:
        error, code = sample.error()
        if error:
            return "jsonrpc_error", code
    return "success", None


def milliseconds(nanoseconds: int) -> float:
    """Une durée en millisecondes, à la microseconde, bornée."""
    return min(round(max(nanoseconds, 0) / _NS_PER_MS, 3), float(SIGNAL_MAX))


@dataclass(frozen=True)
class CallSignals:
    """Les signaux de qualité de service d'un appel d'outil."""

    latency_ms: float
    added_latency_ms: float
    outcome: CallOutcome
    error_code: int | None = None
    # Absent quand le proxy ne peut pas reconnaître une reprise : jamais zéro par défaut.
    retry_count: int | None = None

    @property
    def error_count(self) -> int:
        return 1 if self.outcome in ERRORS else 0

    def fields(self) -> dict[str, Any]:
        """Les champs de la facette de qualité de service."""
        fields: dict[str, Any] = {
            "latencyMs": self.latency_ms,
            "addedLatencyMs": self.added_latency_ms,
            "outcome": self.outcome,
            "errorCount": self.error_count,
        }
        if self.error_code is not None:
            fields["errorCode"] = self.error_code
        if self.retry_count is not None:
            fields["retryCount"] = self.retry_count
        return fields


@dataclass(frozen=True)
class RetryKey:
    """Ce qui fait d'un appel la reprise d'un autre : portée, outil, empreinte des arguments."""

    scope: str
    tool: str
    arguments: str


class RetryMemory:
    """Les chaînes de reprises en cours, en mémoire bornée.

    Un appel reprend le précédent quand ils ont la même clé (``RetryKey``), que
    le précédent n'a pas réussi, et que l'appel commence après la fin du
    précédent, au plus ``window_ns`` plus tard. Son nombre de reprises est le
    nombre d'appels qui le précèdent dans sa chaîne. Un succès clôt la chaîne.
    """

    def __init__(self, *, window_ns: int = RETRY_WINDOW_NS, capacity: int = RETRY_CHAINS) -> None:
        if window_ns <= 0 or capacity < 1:
            raise ValueError("La fenêtre et la capacité des reprises sont positives.")
        self._window = window_ns
        self._capacity = capacity
        # Clé d'empreinte des identifiants de session, tirée au démarrage : une
        # session n'est jamais gardée telle quelle, même en mémoire.
        self._session_key = secrets.token_bytes(32)
        # Par clé : appels de la chaîne qui ont échoué, fin du dernier.
        self._chains: OrderedDict[RetryKey, tuple[int, int]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._chains)

    def scope(self, sessions: Sequence[bytes], trace: CallTrace | None) -> str:
        """Portée d'une reprise : la session MCP, sinon la trace reçue, sinon le proxy entier.

        La session est celle de l'en-tête ``Mcp-Session-Id``, s'il est unique. La
        trace est celle que le client a transmise ; un contexte créé par le proxy
        est propre à chaque appel et ne relie rien.
        """
        if len(sessions) == 1:
            digest = hashlib.blake2b(sessions[0], key=self._session_key, digest_size=16)
            return "session:" + digest.hexdigest()
        if trace is not None and trace.origin != "proxy":
            return "trace:" + trace.context.trace_id
        return "proxy"

    def record(self, key: RetryKey | None, *, started: int, ended: int, failed: bool) -> int | None:
        """Note un appel terminé et rend son nombre de reprises ; ``None`` sans clé."""
        if key is None:
            return None
        retries = 0
        chain = self._chains.pop(key, None)
        if chain is not None:
            attempts, last_end = chain
            if last_end <= started <= last_end + self._window:
                retries = attempts
        if failed:
            self._chains[key] = (min(retries + 1, SIGNAL_MAX), ended)
        while self._chains:
            oldest_key, (_, oldest_end) = next(iter(self._chains.items()))
            if len(self._chains) <= self._capacity and oldest_end + self._window >= ended:
                break
            del self._chains[oldest_key]
        return retries
