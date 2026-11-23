"""Périmètres en quarantaine : le proxy les apprend de l'API, sans encore s'en servir.

Un responsable de données met un incident en quarantaine par l'API ; chaque
proxy apprend le périmètre des quarantaines actives en interrogeant l'API qu'il
connaît déjà pour sa boîte d'envoi (``GET /api/v1/quarantine``), à intervalle
réglable : 10 s par défaut, 30 s au plus. Il ne fait encore rien des lectures
de ce périmètre : il le connaît, et le dit.

Avec le secret qu'il partage avec l'API, le proxy n'accorde foi qu'à une
diffusion signée et datée par elle (``quarantine_signature.py``) : chaque
réponse, ``200`` comme ``304``, est vérifiée avant que son contenu soit lu, et
une réponse non signée, mal signée, trop ancienne ou venue du futur est écartée
comme une réponse hostile. Seul un proxy qui vérifie la diffusion pourra agir
sur les lectures du périmètre (``annotating``) ; sans secret, il l'apprend sans
la vérifier, et ne sera jamais autorisé à annoter.

Les invariants de la capture tiennent :

- **rien n'attend la synchronisation.** Elle tourne dans une tâche de fond,
  démarrée sans attendre la première réponse : ni le démarrage ni un appel
  relayé n'attendent l'API. La lecture de la réponse se fait dans un fil, après
  un premier passage qui borne ce que l'analyseur JSON aura à lire ;
- **la réponse est hostile.** Elle est lue au plus sur 2 Mio, sans
  décompression ; elle doit être du JSON strict, sans clé répétée ni
  échappement ; les identifiants d'incident ont la forme d'un UUID, les noms de
  jeux de données celle que l'ingestion admet (``names.py``), et le nombre de
  quarantaines et de jeux est borné. Une réponse qui s'en écarte est écartée en
  entier : le proxy garde son dernier état, et le journal ne dit que le code du
  motif, jamais la réponse ;
- **une panne garde le dernier état connu**, une heure au plus après la
  dernière synchronisation réussie (``STALE_AFTER``) : assez pour traverser un
  redémarrage ou une bascule de l'API sans perdre une quarantaine en cours,
  assez peu pour qu'une quarantaine levée pendant une panne longue ne survive
  pas indéfiniment chez le proxy. Au-delà, le proxy oublie ce périmètre et
  revient en mode observer : la capture reste ouverte en cas de panne ;
- **le mode observer.** Tant qu'aucune synchronisation n'a réussi, le proxy
  relaie et décrit les appels comme toujours, et le dit (``QuarantineStatus``,
  et le journal) ;
- **le journal ne dit que des nombres**, des statuts et des codes : ni
  l'adresse de l'API, ni la réponse, ni un nom de jeu de données.

Avec un jeton (``credentials.py``), chaque interrogation le porte : l'API ne
livre les périmètres qu'à un appelant authentifié. Sans jeton disponible,
l'interrogation échoue comme une API en panne, et le dernier état connu est
gardé ; un refus ``401`` fait oublier le jeton.

Le modèle de menaces (``proxy/perimetre-usurpe``) dit ce que la signature ne
couvre pas : le secret est symétrique, une API compromise signe ce qu'elle
pousse, et qui bloque le chemin fait garder le dernier état valide une heure.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from random import Random
from typing import Any, Final, Literal

import httpx

from answerlineage_proxy.config import ProxyConfigurationError
from answerlineage_proxy.credentials import Tokens, TokenUnavailableError
from answerlineage_proxy.names import (
    DATASET_NAME_MAX,
    NAMESPACE_MAX,
    name_fault,
    normalize_name,
)
from answerlineage_proxy.quarantine_signature import (
    ENV_SECRET,
    HEADER,
    MALFORMED,
    SignatureRefusedError,
    checked_secret,
    verify,
)

logger = logging.getLogger("answerlineage_proxy")

# Intervalle entre deux interrogations, en secondes : 10 par défaut, 30 au plus,
# pour qu'un proxy connaisse une quarantaine moins de 30 s après son activation.
DEFAULT_INTERVAL: Final = 10
INTERVAL_LIMITS: Final = (1, 30)
# Chaque attente est tirée entre ces parts de l'intervalle, jamais au-delà : des proxys
# démarrés ensemble ne reviennent pas ensemble.
JITTER: Final = (0.8, 1.0)
# Délais d'une interrogation : connexion, chaque lecture et écriture, puis
# l'interrogation entière, qu'une réponse servie goutte à goutte ne prolonge pas.
CONNECT_TIMEOUT: Final = 5.0
READ_TIMEOUT: Final = 5.0
REQUEST_TIMEOUT: Final = 10.0
# Une panne garde le dernier état connu une heure après la dernière synchronisation.
STALE_AFTER: Final = timedelta(hours=1)
# Bornes de la réponse : celles de l'API (cent quarantaines actives, vingt jeux par
# incident), la taille lue, et les éléments JSON qu'elle peut compter.
MAX_QUARANTINES: Final = 100
MAX_DATASETS: Final = 20
MAX_RESPONSE_BYTES: Final = 2 * 1024 * 1024
MAX_TOKENS: Final = 20_000
# Les modes que ce proxy connaît. Une quarantaine d'un autre mode, que l'API
# connaîtrait après une mise à jour, est comptée et laissée de côté.
MODES: Final = ("warn",)
_PATH: Final = b"/api/v1/quarantine"
_VERSION: Final = re.compile(r"[0-9a-f]{64}")
_INCIDENT_ID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_MODE: Final = re.compile(r"[a-z][a-z_]{0,31}")
# Un élément JSON : une chaîne, un délimiteur ouvrant, ou une valeur littérale.
_TOKEN: Final = re.compile(rb'"[^"]*"|[{\[]|[^\s"{}\[\],:]+')
_CLIENT_LOGGERS: Final = ("httpx", "httpcore")

Mode = Literal["observe", "synced"]


class ResponseRefusedError(Exception):
    """Une réponse de l'API écartée. ``code`` est un motif d'une liste fermée, jamais un
    extrait de la réponse."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class QuarantineSettings:
    """L'adresse des périmètres en quarantaine, l'intervalle de leur interrogation, et le secret
    qui vérifie leur diffusion."""

    url: httpx.URL
    # Secondes entre deux interrogations.
    interval: float = DEFAULT_INTERVAL
    # Le secret partagé avec l'API ; sans lui, la diffusion n'est pas vérifiée. Jamais affiché.
    secret: str | None = field(default=None, repr=False)

    @classmethod
    def create(
        cls, api_url: httpx.URL, interval: int = DEFAULT_INTERVAL, *, secret: str | None = None
    ) -> QuarantineSettings:
        """L'adresse vient de celle de l'API, déjà contrôlée pour la boîte d'envoi."""
        minimum, maximum = INTERVAL_LIMITS
        if not minimum <= interval <= maximum:
            raise ProxyConfigurationError(
                f"L'intervalle de synchronisation des quarantaines va de {minimum} à {maximum}"
                " secondes."
            )
        url = api_url.copy_with(raw_path=api_url.raw_path.rstrip(b"/") + _PATH)
        return cls(url, interval, None if secret is None else checked_secret(secret))


@dataclass(frozen=True)
class QuarantineScope:
    """Le périmètre d'une quarantaine active : son incident, son mode, ses jeux de données."""

    incident_id: str
    mode: str
    # Espace de noms et nom, sous leur forme NFC.
    datasets: frozenset[tuple[str, str]]


@dataclass(frozen=True)
class KnownQuarantines:
    """Les périmètres en quarantaine active tels que l'API les a donnés, et leur version."""

    version: str
    scopes: tuple[QuarantineScope, ...]
    # Quarantaines d'un mode que ce proxy ne connaît pas, laissées de côté.
    unsupported: int = 0

    @property
    def datasets(self) -> int:
        return sum(len(scope.datasets) for scope in self.scopes)


@dataclass(frozen=True)
class QuarantineStatus:
    """L'état lisible de la synchronisation : des nombres, aucun nom."""

    # « observe » : aucun périmètre connu, jamais synchronisé ou oublié après une panne
    # trop longue. « synced » : un périmètre connu, peut-être vide.
    mode: Mode
    scopes: int
    datasets: int
    # Secondes depuis la dernière synchronisation réussie ; nul sans aucune.
    last_sync_age: float | None
    # Vrai tant que les interrogations échouent, depuis la dernière réussie.
    failing: bool
    # Vrai quand le périmètre connu vient d'une diffusion vérifiée par sa signature : seul
    # un tel périmètre pourra agir sur les lectures. Jamais vrai sans secret.
    annotating: bool = False


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for key, value in pairs:
        if key in found:
            raise ValueError
        found[key] = value
    return found


def _refuse_constant(_: str) -> Any:
    raise ValueError


def _tokens_within_bounds(body: bytes) -> bool:
    """Vrai quand la réponse compte au plus ``MAX_TOKENS`` éléments JSON.

    Ce passage est fait de courtes recherches, que l'interpréteur interrompt pour
    rendre la main à la boucle des appels ; l'analyseur JSON, qui ne la rend pas,
    ne lit ensuite qu'une réponse bornée.
    """
    return all(count <= MAX_TOKENS for count, _ in enumerate(_TOKEN.finditer(body), start=1))


def _dataset(value: object) -> tuple[str, str]:
    if not isinstance(value, dict):
        raise ResponseRefusedError("invalid_dataset")
    namespace, name = value.get("namespace"), value.get("name")
    if not isinstance(namespace, str) or not isinstance(name, str):
        raise ResponseRefusedError("invalid_dataset")
    if name_fault(namespace, max_length=NAMESPACE_MAX) or name_fault(
        name, max_length=DATASET_NAME_MAX
    ):
        raise ResponseRefusedError("invalid_identifier")
    return normalize_name(namespace), normalize_name(name)


def parse(body: bytes) -> KnownQuarantines:
    """Les périmètres d'une réponse de l'API, contrôlés ; ``ResponseRefusedError`` sinon.

    Tout ou rien : une seule faute écarte la réponse entière. Les champs que le
    proxy ne lit pas sont ignorés ; un mode qu'il ne connaît pas fait laisser de
    côté sa quarantaine, qui est comptée.
    """
    if len(body) > MAX_RESPONSE_BYTES:
        raise ResponseRefusedError("too_large")
    # Ni l'API ni un nom admis n'en écrivent : une chaîne se lit alors d'un guillemet à l'autre.
    if b"\\" in body:
        raise ResponseRefusedError("escaped")
    if not _tokens_within_bounds(body):
        raise ResponseRefusedError("too_many_items")
    try:
        document = json.loads(
            body.decode("utf-8"), object_pairs_hook=_unique_keys, parse_constant=_refuse_constant
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ResponseRefusedError("not_json") from None
    if not isinstance(document, dict):
        raise ResponseRefusedError("not_an_object")
    version, listed = document.get("version"), document.get("quarantines")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise ResponseRefusedError("invalid_version")
    if not isinstance(listed, list):
        raise ResponseRefusedError("invalid_list")
    if len(listed) > MAX_QUARANTINES:
        raise ResponseRefusedError("too_many_quarantines")
    scopes: list[QuarantineScope] = []
    seen: set[str] = set()
    unsupported = 0
    for item in listed:
        if not isinstance(item, dict):
            raise ResponseRefusedError("invalid_quarantine")
        incident_id, mode, datasets = (
            item.get("incident_id"),
            item.get("mode"),
            item.get("datasets"),
        )
        if not isinstance(incident_id, str) or not _INCIDENT_ID.fullmatch(incident_id):
            raise ResponseRefusedError("invalid_incident")
        if incident_id in seen:
            raise ResponseRefusedError("duplicate_incident")
        seen.add(incident_id)
        if not isinstance(mode, str) or not _MODE.fullmatch(mode):
            raise ResponseRefusedError("invalid_mode")
        if not isinstance(datasets, list) or not 1 <= len(datasets) <= MAX_DATASETS:
            raise ResponseRefusedError("invalid_datasets")
        names = frozenset(_dataset(dataset) for dataset in datasets)
        if mode not in MODES:
            unsupported += 1
            continue
        scopes.append(QuarantineScope(incident_id, mode, names))
    return KnownQuarantines(version, tuple(scopes), unsupported)


def _quiet_client_logs() -> None:
    for name in _CLIENT_LOGGERS:
        client_logger = logging.getLogger(name)
        if client_logger.level == logging.NOTSET or client_logger.level < logging.WARNING:
            client_logger.setLevel(logging.WARNING)


@dataclass(frozen=True)
class _Reply:
    status: int
    content_type: str
    encoded: bool
    # Le corps lu, ou ``None`` au-delà de la taille lue.
    body: bytes | None
    # Chaque valeur reçue de l'en-tête de signature : une au plus est admise.
    signatures: tuple[str, ...] = ()


class QuarantineSync:
    """Interroge l'API à intervalle régulier et garde les périmètres en quarantaine active."""

    def __init__(
        self,
        settings: QuarantineSettings,
        *,
        client: httpx.AsyncClient | None = None,
        request_timeout: float = REQUEST_TIMEOUT,
        stale_after: timedelta = STALE_AFTER,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        random: Random | None = None,
        tokens: Tokens | None = None,
    ) -> None:
        _quiet_client_logs()
        self.settings = settings
        self._tokens = tokens
        # L'heure du proxy, en secondes depuis 1970 : celle qui juge l'heure d'une signature.
        self._wall_clock = wall_clock
        self._client = client
        self._owns_client = client is None
        self._request_timeout = request_timeout
        self._stale_after = stale_after.total_seconds()
        self._clock = clock
        self._random = random or Random()  # noqa: S311 — une gigue, pas un secret.
        # Ce que le proxy sait : rien d'abord, puis la dernière réponse acceptée.
        self.known: KnownQuarantines | None = None
        self._synced_at: float | None = None
        self._failing = False
        self._task: asyncio.Task[None] | None = None
        self.polls = 0

    @property
    def annotating(self) -> bool:
        """Vrai quand le périmètre connu vient d'une diffusion vérifiée : avec un secret, le
        proxy n'en adopte pas d'autre ; sans secret, jamais."""
        return self.known is not None and self.settings.secret is not None

    def status(self) -> QuarantineStatus:
        known = self.known
        return QuarantineStatus(
            mode="observe" if known is None else "synced",
            scopes=0 if known is None else len(known.scopes),
            datasets=0 if known is None else known.datasets,
            last_sync_age=None if self._synced_at is None else self._clock() - self._synced_at,
            failing=self._failing,
            annotating=self.annotating,
        )

    async def start(self) -> None:
        """Démarre la synchronisation, sans attendre l'API : rend la main aussitôt."""
        if self._task is not None:
            return
        if self._client is None:
            # Certificat vérifié, aucune redirection suivie, rien de l'environnement :
            # seule l'adresse réglée est jointe, et rien n'est annoncé de plus.
            self._client = httpx.AsyncClient(
                verify=True,
                follow_redirects=False,
                trust_env=False,
                timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            )
            self._client.headers.clear()
        logger.info(
            "Quarantaine : mode observer jusqu'à la première synchronisation avec l'API,"
            " interrogée toutes les %g s.",
            self.settings.interval,
        )
        if self.settings.secret is None:
            logger.warning(
                "Quarantaine : aucun secret de diffusion (%s ou --quarantine-secret-file) ; les"
                " périmètres sont appris sans être vérifiés, et ce proxy ne sera jamais autorisé"
                " à annoter un résultat.",
                ENV_SECRET,
            )
        else:
            logger.info(
                "Quarantaine : seule une diffusion signée par l'API, et datée de moins de 5 min,"
                " est adoptée."
            )
        self._task = asyncio.ensure_future(self._loop())

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.sync_once()
            except Exception as error:  # pragma: no cover — garde-fou de la tâche de fond.
                logger.error(
                    "Synchronisation des quarantaines en échec (%s).", type(error).__name__
                )
            await asyncio.sleep(self.settings.interval * self._random.uniform(*JITTER))

    async def sync_once(self) -> bool:
        """Une interrogation de l'API ; vrai quand elle a réussi."""
        self.polls += 1
        headers = {"accept": "application/json"}
        if self.known is not None:
            headers["if-none-match"] = f'"{self.known.version}"'
        try:
            if self._tokens is not None:
                headers["authorization"] = f"Bearer {await self._tokens.token()}"
        except TokenUnavailableError:
            self._failed("jeton indisponible")
            return False
        try:
            async with asyncio.timeout(self._request_timeout):
                reply = await self._get(headers)
        except (httpx.HTTPError, TimeoutError) as error:
            self._failed(type(error).__name__)
            return False
        now = self._wall_clock()
        if reply.status == 401 and self._tokens is not None:
            self._tokens.invalidate()
        if reply.status == 304 and self.known is not None:
            # Un 304 n'a pas de corps : sa signature porte sur la version tenue.
            try:
                self._verify(reply.signatures, self.known.version.encode("ascii"), now)
            except ResponseRefusedError as refusal:
                self._failed(f"réponse écartée, {refusal.code}")
                return False
            self._succeeded(self.known)
            return True
        if reply.status != 200:
            self._failed(f"HTTP {reply.status}")
            return False
        try:
            if reply.encoded:
                raise ResponseRefusedError("encoded")
            if reply.content_type != "application/json":
                raise ResponseRefusedError("not_json")
            if reply.body is None:
                raise ResponseRefusedError("too_large")
            known = await asyncio.to_thread(self._read, reply.body, reply.signatures, now)
        except ResponseRefusedError as refusal:
            self._failed(f"réponse écartée, {refusal.code}")
            return False
        self._succeeded(known)
        return True

    def _verify(self, signatures: tuple[str, ...], payload: bytes, now: float) -> None:
        """Avec un secret, la diffusion doit être signée par l'API, et récente ; sans secret,
        rien n'est vérifié."""
        secret = self.settings.secret
        if secret is None:
            return
        if len(signatures) > 1:
            raise ResponseRefusedError(MALFORMED)
        try:
            verify(signatures[0] if signatures else None, payload, secret, now=now)
        except SignatureRefusedError as refusal:
            raise ResponseRefusedError(refusal.code) from None

    def _read(self, body: bytes, signatures: tuple[str, ...], now: float) -> KnownQuarantines:
        """Le corps d'un 200, authentifié avant d'être lu : l'analyseur JSON ne lit jamais ce
        qu'un tiers a écrit."""
        self._verify(signatures, body, now)
        return parse(body)

    async def _get(self, headers: dict[str, str]) -> _Reply:
        if self._client is None:  # pragma: no cover — démarrée avant toute interrogation.
            raise RuntimeError("synchronisation non démarrée")
        async with self._client.stream("GET", self.settings.url, headers=headers) as reply:
            encoded = (
                reply.headers.get("content-encoding", "identity").strip().lower() != "identity"
            )
            content_type = reply.headers.get("content-type", "").split(";")[0].strip().lower()
            signatures = tuple(reply.headers.get_list(HEADER))
            body = bytearray()
            # Un corps compressé n'est pas lu : une bombe n'est jamais déployée.
            if reply.status_code == 200 and not encoded:
                async for chunk in reply.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_RESPONSE_BYTES:
                        return _Reply(reply.status_code, content_type, encoded, None, signatures)
            return _Reply(reply.status_code, content_type, encoded, bytes(body), signatures)

    def _succeeded(self, known: KnownQuarantines) -> None:
        changed = self.known is None or known.version != self.known.version
        recovered = self._failing
        self.known = known
        self._synced_at = self._clock()
        self._failing = False
        if recovered:
            logger.info("Quarantaine : API de nouveau joignable.")
        if changed or recovered:
            logger.info(
                "Quarantaine synchronisée : %d périmètre(s) en quarantaine, %d jeu(x) de données.",
                len(known.scopes),
                known.datasets,
            )
        if changed and known.unsupported:
            logger.warning(
                "Quarantaine : %d périmètre(s) d'un mode que ce proxy ne connaît pas, laissés de"
                " côté : mettre le proxy à jour.",
                known.unsupported,
            )

    def _failed(self, reason: str) -> None:
        if not self._failing:
            self._failing = True
            if self.known is None:
                logger.warning(
                    "Quarantaine : synchronisation en échec (%s) ; mode observer, aucun périmètre"
                    " connu.",
                    reason,
                )
            else:
                logger.warning(
                    "Quarantaine : synchronisation en échec (%s) ; le dernier état connu est"
                    " gardé (%d périmètre(s), synchronisé il y a %d s), %s au plus.",
                    reason,
                    len(self.known.scopes),
                    self._age(),
                    _duration(self._stale_after),
                )
        if self.known is not None and self._age() > self._stale_after:
            logger.warning(
                "Quarantaine : aucune synchronisation depuis %s ; le dernier périmètre connu est"
                " oublié, retour en mode observer.",
                _duration(self._stale_after),
            )
            self.known = None

    def _age(self) -> float:
        return 0.0 if self._synced_at is None else self._clock() - self._synced_at


def _duration(seconds: float) -> str:
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{int(seconds // 3600)} h"
    return f"{seconds:g} s"
