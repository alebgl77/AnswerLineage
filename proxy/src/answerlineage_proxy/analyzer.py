"""Analyse du SQL dans un processus à part, avec un budget, une file et un cache bornés.

Un fil Python ne s'interrompt pas, et un calcul qui s'y prolonge dispute
l'interpréteur à la boucle qui relaie les appels. L'analyse tourne donc dans un
interpréteur distinct, démarré à neuf (``python -I -m answerlineage_proxy.analyzer``),
à qui le proxy confie les requêtes une à une :

- le budget se compte à partir du moment où le processus annonce qu'il commence
  le calcul : ni son démarrage ni l'attente dans la file n'en consomment. Passé
  le budget, le processus est tué, puis relancé à la requête suivante ;
- la file des analyses en attente est bornée : pleine, la lecture est « non
  résolue » sur-le-champ ;
- toute attente a un délai, et une réponse du processus n'est crue que si elle
  a la forme attendue : un processus qui meurt, ne démarre pas ou répond
  n'importe quoi donne une lecture « non résolue », jamais une exception.

La forme de la requête sans ses littéraux se calcule dans le même processus,
pendant la même analyse et dans le même budget : elle traverse la même
frontière, où elle n'est crue que bornée, imprimable et sans commentaire.

Le cache est indexé par une empreinte à clé secrète du texte et des réglages
d'analyse, jamais par le texte : seul le résultat (des noms de tables et de
colonnes, la forme sans littéraux) est gardé. Une analyse abandonnée au budget
est mise en cache comme les autres : l'analyse est déterministe, la relancer coûterait de nouveau le
budget, un processus tué et un redémarrage, et une requête pathologique répétée
deviendrait un moyen d'occuper l'analyse au détriment des autres appels.

Le protocole entre le proxy et le processus est une ligne JSON par message,
sur l'entrée et la sortie standard. La sortie d'erreur du processus est
fermée : un message d'erreur pourrait citer la requête.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import re
import secrets
import signal
import sys
from collections import OrderedDict
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Literal, cast

from answerlineage_proxy.config import (
    DEFAULT_SQL_BUDGET_MS,
    DEFAULT_SQL_CACHE_SIZE,
    DEFAULT_SQL_QUEUE_SIZE,
)
from answerlineage_proxy.sql import (
    DIALECTS,
    IDENTIFIER,
    MAX_SQL_BYTES,
    Dialect,
    SqlAnalysis,
    TableRead,
    analyze_sql,
    is_plain_shape,
)

logger = logging.getLogger("answerlineage_proxy")

# Délai pour démarrer le processus, et pour qu'il accuse réception d'une requête.
DEFAULT_START_TIMEOUT = 10.0
# Somme des longueurs des noms gardés en cache, toutes entrées confondues.
CACHE_CHARACTERS = 4 * 1024 * 1024
# Longueur maximale d'une ligne rendue par le processus.
_LINE_LIMIT = 1024 * 1024
# Au-delà du budget, le processus s'arrête de lui-même après ce délai : le proxy
# l'aura tué avant, sauf s'il a disparu sans le faire.
_ORPHAN_GRACE = 1.0
# Délai laissé à un processus inoccupé pour s'arrêter sur la fin de son entrée.
_STOP_GRACE = 0.5
_STOP_TIMEOUT = 5.0

_TABLE = re.compile(rf"{IDENTIFIER.pattern}(?:\.{IDENTIFIER.pattern}){{0,2}}")
_REASON = re.compile(r"[a-z_]{1,64}")

# Requêtes analysées au démarrage du processus, dans chaque dialecte : les
# modules que sqlglot charge à la demande sont chargés avant le premier budget.
_WARM_UP = (
    "WITH c AS (SELECT a, b FROM s.t WHERE x IN (SELECT y FROM s.v)) "
    "SELECT c.a, u.*, ROW_NUMBER() OVER (PARTITION BY c.b ORDER BY u.k) AS n "
    "FROM c JOIN s.u AS u ON u.k = c.b WHERE EXISTS (SELECT 1 FROM s.w AS w WHERE w.k = u.k) "
    "UNION ALL SELECT a, b, c, 1 FROM s.x NATURAL JOIN s.y",
    "INSERT INTO s.z SELECT a FROM s.t GROUP BY a HAVING COUNT(*) > 1 ORDER BY a",
    "UPDATE s.t SET a = 1",
    "SELEC a FRM",
)

Origin = Literal["analyzed", "cache", "over_budget", "refused", "failed"]


@dataclass(frozen=True)
class AnalyzerResult:
    """Lecture d'une requête, et d'où elle vient."""

    analysis: SqlAnalysis
    origin: Origin


@dataclass(frozen=True)
class _Job:
    key: bytes
    # Gardée le temps de l'analyse, jamais affichée.
    sql: str = field(default="", repr=False)
    dialect: str = ""
    catalog: str | None = None
    db: str | None = None
    # Préchauffage : démarrer le processus, sans rien analyser.
    warm: bool = False


_Outcome = tuple[SqlAnalysis, Origin]


def _unresolved(reason: str) -> SqlAnalysis:
    return SqlAnalysis(resolved=False, reason=reason)


def _weight(analysis: SqlAnalysis) -> int:
    names = sum(len(table.name) + sum(map(len, table.columns)) for table in analysis.tables)
    return 1 + names + len(analysis.shape or "")


def _is_message(line: bytes, key: str, value: object) -> bool:
    try:
        message = json.loads(line)
    except ValueError:
        return False
    return message == {key: value} and type(message[key]) is type(value)


def _encode(number: int, analysis: SqlAnalysis) -> dict[str, Any]:
    return {
        "id": number,
        "resolved": analysis.resolved,
        "reason": analysis.reason,
        "tables": [[table.name, table.precision, list(table.columns)] for table in analysis.tables],
        "shape": analysis.shape,
    }


def _decode_table(item: object) -> TableRead | None:
    if not (isinstance(item, list) and len(item) == 3):
        return None
    name, precision, columns = item
    if not (isinstance(name, str) and _TABLE.fullmatch(name) and isinstance(columns, list)):
        return None
    if not all(isinstance(column, str) and IDENTIFIER.fullmatch(column) for column in columns):
        return None
    if precision == "table" and not columns:
        return TableRead(name, "table")
    if precision == "columns" and columns:
        return TableRead(name, "columns", tuple(columns))
    return None


def _decode(line: bytes, number: int) -> SqlAnalysis | None:
    """Résultat rendu par le processus, s'il a exactement la forme attendue."""
    try:
        reply = json.loads(line)
    except ValueError:
        return None
    if not (isinstance(reply, dict) and type(reply.get("id")) is int and reply["id"] == number):
        return None
    resolved, reason, tables = reply.get("resolved"), reply.get("reason"), reply.get("tables")
    if not (isinstance(resolved, bool) and isinstance(tables, list)):
        return None
    if resolved and reason is not None:
        return None
    if not resolved and (tables or not (isinstance(reason, str) and _REASON.fullmatch(reason))):
        return None
    shape = reply.get("shape")
    if shape is not None and not (isinstance(shape, str) and is_plain_shape(shape)):
        return None
    reads = [_decode_table(item) for item in tables]
    if any(read is None for read in reads):
        return None
    return SqlAnalysis(
        resolved=resolved,
        tables=tuple(read for read in reads if read is not None),
        reason=reason,
        shape=shape,
    )


class SqlAnalyzer:
    """Confie l'analyse du SQL à un processus à part."""

    def __init__(
        self,
        *,
        budget: float = DEFAULT_SQL_BUDGET_MS / 1000,
        queue_size: int = DEFAULT_SQL_QUEUE_SIZE,
        cache_size: int = DEFAULT_SQL_CACHE_SIZE,
        command: Sequence[str] | None = None,
        start_timeout: float = DEFAULT_START_TIMEOUT,
    ) -> None:
        # Budget de calcul d'une requête, en secondes.
        self.budget = budget
        self.queue_size = queue_size
        self.cache_size = cache_size
        # Commande du processus d'analyse ; les tests en substituent une.
        self.command: tuple[str, ...] = (
            tuple(command)
            if command is not None
            else (sys.executable, "-I", "-m", "answerlineage_proxy.analyzer")
        )
        self._start_timeout = start_timeout
        # Clé de l'empreinte, propre à ce proxy : une empreinte gardée ne se
        # compare pas à celle d'un texte deviné.
        self._salt = secrets.token_bytes(32)
        self._cache: OrderedDict[bytes, SqlAnalysis] = OrderedDict()
        self._cache_weight = 0
        self._inflight: dict[bytes, asyncio.Future[_Outcome]] = {}
        self._queue: asyncio.Queue[tuple[_Job, asyncio.Future[_Outcome]]] | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._numbers = itertools.count(1)
        self._analyzing = False

    @property
    def process_id(self) -> int | None:
        """Identifiant du processus d'analyse en cours, s'il y en a un."""
        return self._process.pid if self._process is not None else None

    @property
    def analyzing(self) -> bool:
        """Vrai pendant qu'une requête est en calcul, budget en cours."""
        return self._analyzing

    async def analyze(
        self,
        sql: str,
        dialect: Dialect,
        *,
        default_catalog: str | None = None,
        default_db: str | None = None,
    ) -> AnalyzerResult:
        """Lit les tables et colonnes d'une requête. Ne lève pas, n'attend jamais sans fin."""
        if dialect not in DIALECTS:
            return AnalyzerResult(_unresolved("unsupported_dialect"), "analyzed")
        if len(sql.encode("utf-8", errors="replace")) > MAX_SQL_BYTES:
            return AnalyzerResult(_unresolved("too_large"), "analyzed")
        key = self._key(sql, dialect, default_catalog, default_db)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return AnalyzerResult(cached, "cache")
        pending = self._inflight.get(key)
        if pending is not None:
            # La même requête est déjà en attente ou en calcul : son résultat sert deux fois.
            analysis, origin = await asyncio.shield(pending)
            return AnalyzerResult(analysis, "failed" if origin == "failed" else "cache")
        queue = self._running_queue()
        future: asyncio.Future[_Outcome] = asyncio.get_running_loop().create_future()
        try:
            queue.put_nowait((_Job(key, sql, dialect, default_catalog, default_db), future))
        except asyncio.QueueFull:
            return AnalyzerResult(_unresolved("queue_full"), "refused")
        self._inflight[key] = future
        analysis, origin = await asyncio.shield(future)
        return AnalyzerResult(analysis, origin)

    async def warm(self) -> None:
        """Démarre le processus d'analyse s'il ne tourne pas, et le laisse se préchauffer (il
        charge chaque dialecte à son démarrage) : la première requête n'attend pas ce temps-là.
        Passe par la file, comme une analyse ; pleine, rien n'est fait. Ne lève pas."""
        queue = self._running_queue()
        future: asyncio.Future[_Outcome] = asyncio.get_running_loop().create_future()
        try:
            queue.put_nowait((_Job(b"", warm=True), future))
        except asyncio.QueueFull:
            return
        await asyncio.shield(future)

    async def aclose(self) -> None:
        """Arrête le processus d'analyse. Les analyses en attente sont « non résolues »."""
        supervisor, self._supervisor = self._supervisor, None
        queue, self._queue = self._queue, None
        if supervisor is not None:
            supervisor.cancel()
            await asyncio.gather(supervisor, return_exceptions=True)
        while queue is not None and not queue.empty():
            job, future = queue.get_nowait()
            self._settle(job, future, _unresolved("analyzer_unavailable"), "failed")
        await self._stop(grace=_STOP_GRACE)

    def _key(self, sql: str, dialect: str, catalog: str | None, db: str | None) -> bytes:
        payload = json.dumps([dialect, catalog, db, sql]).encode("ascii")
        return hashlib.blake2b(payload, key=self._salt, digest_size=32).digest()

    def _remember(self, key: bytes, analysis: SqlAnalysis) -> None:
        """Met un résultat en cache ; les moins récemment servis sortent les premiers."""
        weight = _weight(analysis)
        if self.cache_size == 0 or weight > CACHE_CHARACTERS:
            return
        previous = self._cache.pop(key, None)
        if previous is not None:
            self._cache_weight -= _weight(previous)
        self._cache[key] = analysis
        self._cache_weight += weight
        while len(self._cache) > self.cache_size or self._cache_weight > CACHE_CHARACTERS:
            _, dropped = self._cache.popitem(last=False)
            self._cache_weight -= _weight(dropped)

    def _running_queue(self) -> asyncio.Queue[tuple[_Job, asyncio.Future[_Outcome]]]:
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self.queue_size)
        if self._supervisor is None or self._supervisor.done():
            self._supervisor = asyncio.ensure_future(self._supervise(self._queue))
        return self._queue

    def _settle(
        self, job: _Job, future: asyncio.Future[_Outcome], analysis: SqlAnalysis, origin: Origin
    ) -> None:
        if self._inflight.get(job.key) is future:
            del self._inflight[job.key]
        if not future.done():
            future.set_result((analysis, origin))

    async def _supervise(self, queue: asyncio.Queue[tuple[_Job, asyncio.Future[_Outcome]]]) -> None:
        """Traite la file, une requête à la fois."""
        while True:
            job, future = await queue.get()
            if job.warm:
                origin: Origin = "failed"
                try:
                    origin = "analyzed" if await self._ready() is not None else "failed"
                finally:
                    self._settle(job, future, _unresolved("analyzer_warm"), origin)
                continue
            try:
                analysis, origin, cacheable = await self._compute(job)
            except asyncio.CancelledError:
                self._settle(job, future, _unresolved("analyzer_unavailable"), "failed")
                raise
            except Exception as error:
                logger.error("Analyse du SQL en échec (%s).", type(error).__name__)
                await self._stop(grace=0)
                analysis, origin, cacheable = _unresolved("analyzer_failed"), "failed", False
            if cacheable:
                self._remember(job.key, analysis)
            self._settle(job, future, analysis, origin)

    async def _compute(self, job: _Job) -> tuple[SqlAnalysis, Origin, bool]:
        """Analyse une requête : résultat, origine, et s'il tient à la requête seule."""
        process = await self._ready()
        if process is None or process.stdin is None or process.stdout is None:
            return _unresolved("analyzer_unavailable"), "failed", False
        number = next(self._numbers)
        request = {
            "id": number,
            "sql": job.sql,
            "dialect": job.dialect,
            "catalog": job.catalog,
            "db": job.db,
            "limit": self.budget + _ORPHAN_GRACE,
        }
        try:
            process.stdin.write(json.dumps(request).encode("ascii") + b"\n")
            await asyncio.wait_for(process.stdin.drain(), self._start_timeout)
            started = await asyncio.wait_for(process.stdout.readline(), self._start_timeout)
        except (OSError, ValueError, TimeoutError):
            started = b""
        if not _is_message(started, "started", number):
            # Le calcul n'a pas commencé : l'échec ne tient pas à la requête.
            await self._stop(grace=0)
            return _unresolved("analyzer_unavailable"), "failed", False
        self._analyzing = True
        try:
            line = await asyncio.wait_for(process.stdout.readline(), self.budget)
        except TimeoutError:
            # Le calcul s'arrête réellement : son processus est tué.
            await self._stop(grace=0)
            return _unresolved("budget_exceeded"), "over_budget", True
        except (OSError, ValueError):
            line = b""
        finally:
            self._analyzing = False
        analysis = _decode(line, number)
        if analysis is None:
            # Mort ou réponse illisible en plein calcul : la requête en est
            # peut-être la cause, elle ne sera pas recalculée.
            logger.warning("Processus d'analyse du SQL en échec pendant un calcul.")
            await self._stop(grace=0)
            return _unresolved("analyzer_failed"), "failed", True
        return analysis, "analyzed", True

    async def _ready(self) -> asyncio.subprocess.Process | None:
        """Le processus d'analyse, démarré et prêt ; ``None`` s'il ne démarre pas."""
        process = self._process
        if process is not None and process.returncode is None:
            return process
        await self._stop(grace=0)
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=_LINE_LIMIT,
            )
        except (OSError, ValueError) as error:
            logger.warning("Processus d'analyse du SQL non démarré (%s).", type(error).__name__)
            return None
        self._process = process
        line = b""
        if process.stdout is not None:
            with suppress(OSError, ValueError, TimeoutError):
                line = await asyncio.wait_for(process.stdout.readline(), self._start_timeout)
        if not _is_message(line, "ready", True):
            logger.warning("Processus d'analyse du SQL non prêt.")
            await self._stop(grace=0)
            return None
        return process

    async def _stop(self, *, grace: float) -> None:
        """Arrête le processus : fin de son entrée, puis, passé ``grace``, il est tué."""
        process, self._process = self._process, None
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        if grace > 0:
            with suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), grace)
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        with suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), _STOP_TIMEOUT)


def _read_job(line: bytes) -> tuple[int, str, str, str | None, str | None, float | None] | None:
    try:
        job = json.loads(line)
    except ValueError:
        return None
    if not isinstance(job, dict):
        return None
    number, sql, dialect = job.get("id"), job.get("sql"), job.get("dialect")
    catalog, db, limit = job.get("catalog"), job.get("db"), job.get("limit")
    if not (type(number) is int and isinstance(sql, str) and isinstance(dialect, str)):
        return None
    if not all(value is None or isinstance(value, str) for value in (catalog, db)):
        return None
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int | float)):
        return None
    return number, sql, dialect, catalog, db, limit


def _write(replies: BinaryIO, message: dict[str, Any]) -> None:
    replies.write(json.dumps(message).encode("ascii") + b"\n")
    replies.flush()


def serve(
    requests: BinaryIO,
    replies: BinaryIO,
    analyze: Callable[..., SqlAnalysis] = analyze_sql,
    *,
    alarm: bool = False,
) -> None:
    """Boucle du processus d'analyse : une requête par ligne, une réponse par ligne.

    Avec ``alarm``, chaque calcul est borné par le système : passé la limite
    qu'indique la requête, le signal SIGALRM, laissé à son action par défaut,
    termine le processus, même au milieu d'un calcul qui ne rend pas la main.
    """
    _write(replies, {"ready": True})
    for line in iter(requests.readline, b""):
        job = _read_job(line)
        if job is None:
            return
        number, sql, dialect, catalog, db, limit = job
        _write(replies, {"started": number})
        if alarm and limit is not None:
            signal.setitimer(signal.ITIMER_REAL, limit)
        try:
            analysis = analyze(sql, cast(Dialect, dialect), default_catalog=catalog, default_db=db)
        finally:
            if alarm:
                signal.setitimer(signal.ITIMER_REAL, 0)
        _write(replies, _encode(number, analysis))


def main() -> None:  # pragma: no cover - exécuté dans le processus d'analyse.
    # Le processus s'arrête sur la fin de son entrée, pas sur un Ctrl+C adressé au proxy.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    replies = sys.stdout.buffer
    # Rien d'autre ne doit écrire sur le canal des réponses.
    sys.stdout = sys.stderr
    for dialect in DIALECTS:
        for sql in _WARM_UP:
            analyze_sql(sql, cast(Dialect, dialect))
    serve(sys.stdin.buffer, replies, alarm=True)


if __name__ == "__main__":  # pragma: no cover
    main()
