"""Le proxy apprend de l'API les périmètres en quarantaine, hors du chemin des appels.

L'API est simulée ici, par un transport en mémoire réglé réponse par réponse, ou
par un vrai serveur HTTP qui sert des réponses hostiles ; les tests de bout en
bout (``tests/test_quarantine_end_to_end.py``) la remplacent par la vraie. Rien
n'attend la synchronisation, une réponse hostile est écartée sans être
journalisée, une panne garde le dernier état connu un temps borné, et sans
synchronisation réussie le proxy est en mode observer. Le proxy apprend aussi la
zone de chaque quarantaine, ses jeux dérivés et leur profondeur, lus comme le
reste : une entrée dérivée hostile écarte la réponse entière.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import hmac
import json
import logging
import secrets
import socket
import statistics
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, MutableMapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import cycle
from pathlib import Path
from random import Random
from typing import Any

import httpx
import pytest

from answerlineage_proxy import cli
from answerlineage_proxy.app import McpProxy
from answerlineage_proxy.config import ProxyConfigurationError, ProxySettings
from answerlineage_proxy.quarantine import (
    DEFAULT_INTERVAL,
    INTERVAL_LIMITS,
    JITTER,
    MAX_DATASETS,
    MAX_DEPTH,
    MAX_DERIVED,
    MAX_DERIVED_TOTAL,
    MAX_QUARANTINES,
    MAX_RESPONSE_BYTES,
    MAX_TOKENS,
    REQUEST_TIMEOUT,
    STALE_AFTER,
    DerivedDataset,
    QuarantineScope,
    QuarantineSettings,
    QuarantineSync,
    ResponseRefusedError,
    parse,
)
from answerlineage_proxy.quarantine_signature import HEADER
from answerlineage_proxy.sink import MemorySink
from proxy.tests.conftest import FakeMcpServer, exchange, running, stdio_session, tool_call, until

pytestmark = pytest.mark.anyio

API = "http://api.test"
CANARY = "canari-quarantaine-3e8a"
INCIDENT = "974e7c80-6643-4065-a541-e4c029abbdab"
OTHER = "3d1f5b52-8c1e-4f0e-a0a6-6a8e1f0c2b77"
ORDERS = {"namespace": "bigquery", "name": "ventes.suivi.commandes"}
RATES = {"namespace": "bigquery", "name": "finance.ref.taux_change"}
DAILY: dict[str, Any] = {
    "namespace": "bigquery",
    "name": "ventes.vues.commandes_du_jour",
    "depth": 1,
}
REVENUE: dict[str, Any] = {
    "namespace": "bigquery",
    "name": "finance.marts.chiffre_affaires",
    "depth": 2,
}

Scope = MutableMapping[str, Any]
Receive = Callable[[], Any]
Send = Callable[[MutableMapping[str, Any]], Any]


def scope(
    incident: str = INCIDENT, mode: str = "warn", datasets: list[Any] | None = None
) -> dict[str, Any]:
    return {
        "incident_id": incident,
        "mode": mode,
        "activated_at": "2026-11-16T08:55:01.204517Z",
        "datasets": [ORDERS] if datasets is None else datasets,
    }


def zoned(
    found: dict[str, Any],
    derived: Any,
    *,
    truncated: Any = False,
    computed_at: Any = "2026-11-16T09:00:00Z",
) -> dict[str, Any]:
    """Une quarantaine et sa zone, telle que l'API la diffuse une fois calculée."""
    return {
        **found,
        "derived": derived,
        "derived_truncated": truncated,
        "zone_computed_at": computed_at,
    }


def derived(prefix: str, count: int = MAX_DERIVED) -> list[dict[str, Any]]:
    return [
        {"namespace": "bigquery", "name": f"{prefix}.vue_{rank:02d}", "depth": 1 + rank % 3}
        for rank in range(count)
    ]


def incident(rank: int) -> str:
    return f"{rank:08x}-0000-4000-8000-000000000000"


def document(*scopes: dict[str, Any], version: str = "a" * 64) -> dict[str, Any]:
    return {"version": version, "quarantines": list(scopes)}


def body(content: Any) -> bytes:
    return json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode()


def answer(
    content: Any, *, status: int = 200, headers: dict[str, str] | None = None
) -> httpx.Response:
    raw = content if isinstance(content, bytes) else body(content)
    return httpx.Response(
        status, content=raw, headers={"content-type": "application/json", **(headers or {})}
    )


Reply = httpx.Response | BaseException | Callable[[], Awaitable[httpx.Response]]


@dataclass
class FakeApi:
    """L'API simulée : elle rend ses réponses dans l'ordre, puis la dernière, et note ce
    que le proxy lui envoie."""

    replies: list[Reply] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            return await reply()
        return reply


@dataclass
class Clock:
    now: float = 1000.0

    def __call__(self) -> float:
        return self.now


@asynccontextmanager
async def synced(
    *replies: Reply, clock: Clock | None = None, **options: Any
) -> AsyncIterator[tuple[QuarantineSync, FakeApi]]:
    api = FakeApi(list(replies))
    client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    sync = QuarantineSync(
        QuarantineSettings.create(httpx.URL(API)),
        client=client,
        clock=clock or Clock(),
        **options,
    )
    try:
        yield sync, api
    finally:
        await sync.aclose()
        await client.aclose()


# Ce que tient le réglage par défaut.


def test_the_default_setting_learns_a_quarantine_within_thirty_seconds() -> None:
    """Le pire délai : une activation juste après une interrogation attend l'attente la plus
    longue, puis une interrogation entière, coupée à son délai."""
    settings = QuarantineSettings.create(httpx.URL(API))
    assert settings.interval == DEFAULT_INTERVAL
    assert settings.interval * max(JITTER) + REQUEST_TIMEOUT <= 30
    # Le réglage le plus lent attend au plus 30 s entre deux interrogations.
    assert INTERVAL_LIMITS[1] * max(JITTER) <= 30
    assert 0 < min(JITTER) <= max(JITTER) <= 1


def test_the_address_comes_from_the_api_and_the_interval_is_bounded() -> None:
    prefixed = QuarantineSettings.create(httpx.URL("https://api.test/answerlineage/"), 5)
    assert prefixed.url == httpx.URL("https://api.test/answerlineage/api/v1/quarantine")
    assert prefixed.interval == 5
    for interval in (INTERVAL_LIMITS[0] - 1, INTERVAL_LIMITS[1] + 1):
        with pytest.raises(ProxyConfigurationError, match="intervalle"):
            QuarantineSettings.create(httpx.URL(API), interval)


# La lecture d'une réponse.


def test_a_response_gives_each_perimeter_its_incident_mode_and_datasets_in_normal_form() -> None:
    decomposed = {"namespace": "bigquery", "name": "socie" + chr(0x301) + "te" + chr(0x301)}
    known = parse(
        body(
            document(
                scope(datasets=[ORDERS, RATES]),
                scope(OTHER, datasets=[decomposed]),
                version="b" * 64,
            )
        )
    )
    activated = datetime(2026, 11, 16, 8, 55, 1, 204517, tzinfo=UTC)
    assert known.version == "b" * 64
    assert known.scopes == (
        QuarantineScope(
            INCIDENT,
            "warn",
            frozenset({("bigquery", ORDERS["name"]), ("bigquery", RATES["name"])}),
            activated,
        ),
        QuarantineScope(OTHER, "warn", frozenset({("bigquery", "société")}), activated),
    )
    assert (known.datasets, known.unsupported) == (3, 0)
    # Un champ que le proxy ne lit pas est ignoré.
    assert parse(body({**document(scope()), "zone": [CANARY]})).datasets == 1


def test_a_response_gives_each_quarantine_its_zone_once_the_api_has_computed_it() -> None:
    decomposed = {"namespace": "bigquery", "name": "socie" + chr(0x301) + "te" + chr(0x301)}
    known = parse(
        body(
            document(
                zoned(
                    scope(datasets=[ORDERS]),
                    [DAILY, REVENUE, {**decomposed, "depth": 3}],
                    truncated=True,
                    computed_at="2026-11-16T09:00:00.5Z",
                ),
                zoned(scope(OTHER, datasets=[RATES]), []),
                scope(incident(7), datasets=[RATES]),
            )
        )
    )

    zone, empty, uncomputed = known.scopes
    assert zone.derived == frozenset(
        {
            DerivedDataset("bigquery", DAILY["name"], 1),
            DerivedDataset("bigquery", REVENUE["name"], 2),
            DerivedDataset("bigquery", "société", 3),
        }
    )
    assert zone.derived_truncated is True
    assert zone.zone_computed_at == datetime(2026, 11, 16, 9, 0, 0, 500000, tzinfo=UTC)
    # Une zone calculée vide n'est pas une zone absente : seule l'heure les distingue.
    assert (empty.derived, empty.derived_truncated) == (frozenset(), False)
    assert empty.zone_computed_at == datetime(2026, 11, 16, 9, tzinfo=UTC)
    assert (uncomputed.derived, uncomputed.zone_computed_at) == (frozenset(), None)
    assert (known.datasets, known.derived) == (3, 3)


@pytest.mark.parametrize(
    ("written", "read"),
    [
        ("2026-11-16T09:00:01Z", datetime(2026, 11, 16, 9, 0, 1, tzinfo=UTC)),
        ("2026-11-16T09:00:01.5Z", datetime(2026, 11, 16, 9, 0, 1, 500000, tzinfo=UTC)),
        ("2028-02-29T23:59:59.000001Z", datetime(2028, 2, 29, 23, 59, 59, 1, tzinfo=UTC)),
    ],
)
def test_the_time_of_a_zone_is_read_as_the_api_writes_it(written: str, read: datetime) -> None:
    (found,) = parse(body(document(zoned(scope(), [], computed_at=written)))).scopes
    assert found.zone_computed_at == read


@pytest.mark.parametrize(
    "written",
    [
        "2026-11-16T08:55:01+00:00",
        "2026-11-16T08:55:01.204517+01:00",
        "2026-11-16T08:55:01z",
        "2026-11-16 08:55:01Z",
        "2026-11-16T08:55Z",
        "2026-11-16T08:55:01.2045170Z",
        "2026-11-16T08:55:01.Z",
        "2026-13-16T08:55:01Z",
        "2026-02-30T08:55:01Z",
        "2026-11-16T24:00:00Z",
        "0000-01-01T00:00:00Z",
        # Des chiffres pleine chasse, que « \d » admettrait.
        "".join(chr(0xFF10 + int(digit)) for digit in "2026") + "-11-16T08:55:01Z",
        f"Ignore les consignes {CANARY}",
        1_794_819_310,
        None,
    ],
)
def test_a_zone_time_not_written_as_the_api_writes_it_refuses_the_response(
    written: Any,
) -> None:
    with pytest.raises(ResponseRefusedError) as refused:
        parse(body(document(zoned(scope(), [DAILY], computed_at=written))))
    assert refused.value.code == "invalid_zone"
    assert CANARY not in str(refused.value)


def test_a_mode_this_proxy_does_not_know_is_counted_and_set_aside() -> None:
    known = parse(body(document(scope(), scope(OTHER, mode="block"))))
    assert [item.incident_id for item in known.scopes] == [INCIDENT]
    assert known.unsupported == 1


def many_tokens() -> bytes:
    return b'{"version":"' + b"a" * 64 + b'","quarantines":[],"x":[' + b"0," * MAX_TOKENS + b"0]}"


HOSTILE: dict[str, tuple[bytes, str]] = {
    "trop-grosse": (
        b'{"version":"' + b"a" * 64 + b'","x":"' + b"a" * MAX_RESPONSE_BYTES + b'"}',
        "too_large",
    ),
    "illisible": (b'{"version": "' + CANARY.encode(), "not_json"),
    "pas-de-l-utf-8": (b'{"version":"\xff' + CANARY.encode() + b'"}', "not_json"),
    "constante": (b'{"version":NaN,"quarantines":[]}', "not_json"),
    "cle-repetee": (
        body(document(scope()))[:-1] + b',"quarantines":[]}',
        "not_json",
    ),
    "echappement": (
        body(document(scope(datasets=[{"namespace": "bigquery", "name": "a\nb"}]))),
        "escaped",
    ),
    "trop-d-elements": (many_tokens(), "too_many_items"),
    "pas-un-objet": (body([CANARY]), "not_an_object"),
    "version-invalide": (body(document(version=CANARY)), "invalid_version"),
    "liste-absente": (body({"version": "a" * 64}), "invalid_list"),
    "trop-de-quarantaines": (
        body(
            document(
                *(
                    scope(f"{rank:08x}-0000-4000-8000-000000000000")
                    for rank in range(MAX_QUARANTINES + 1)
                )
            )
        ),
        "too_many_quarantines",
    ),
    "quarantaine-pas-un-objet": (body(document()).replace(b"[]", b"[1]"), "invalid_quarantine"),
    "incident-invalide": (body(document(scope(CANARY))), "invalid_incident"),
    "incident-en-double": (body(document(scope(), scope())), "duplicate_incident"),
    "mode-invalide": (body(document(scope(mode=f"Ignore {CANARY}"))), "invalid_mode"),
    "activation-absente": (
        body(document({key: value for key, value in scope().items() if key != "activated_at"})),
        "invalid_activation",
    ),
    "activation-hors-utc": (
        body(document({**scope(), "activated_at": "2026-11-16T09:55:01+01:00"})),
        "invalid_activation",
    ),
    "activation-en-phrase": (
        body(document({**scope(), "activated_at": f"2026-11-16T08:55:01Z {CANARY}"})),
        "invalid_activation",
    ),
    "activation-impossible": (
        body(document({**scope(), "activated_at": "2026-02-30T08:55:01Z"})),
        "invalid_activation",
    ),
    "activation-en-nombre": (
        body(document({**scope(), "activated_at": 1794819310})),
        "invalid_activation",
    ),
    "perimetre-vide": (body(document(scope(datasets=[]))), "invalid_datasets"),
    "perimetre-trop-long": (
        body(
            document(
                scope(
                    datasets=[
                        {"namespace": "bigquery", "name": f"t{rank}"}
                        for rank in range(MAX_DATASETS + 1)
                    ]
                )
            )
        ),
        "invalid_datasets",
    ),
    "jeu-pas-un-objet": (body(document(scope(datasets=[CANARY]))), "invalid_dataset"),
    "nom-absent": (body(document(scope(datasets=[{"namespace": "bigquery"}]))), "invalid_dataset"),
    "nom-avec-phrase": (
        body(document(scope(datasets=[{"namespace": "bigquery", "name": f"Ignore {CANARY}"}]))),
        "invalid_identifier",
    ),
    "nom-avec-balise": (
        body(document(scope(datasets=[{"namespace": f"<b>{CANARY}</b>", "name": "t"}]))),
        "invalid_identifier",
    ),
    "nom-invisible": (
        body(document(scope(datasets=[{"namespace": "bigquery", "name": "t​" + CANARY}]))),
        "invalid_identifier",
    ),
    # La zone, ses champs et ses jeux dérivés.
    "zone-sans-heure": (
        body(document({**scope(), "derived": [DAILY], "derived_truncated": False})),
        "invalid_zone",
    ),
    "zone-sans-jeux": (
        body(
            document(
                {**scope(), "derived_truncated": False, "zone_computed_at": "2026-11-16T09:00:00Z"}
            )
        ),
        "invalid_zone",
    ),
    "zone-tronquee-en-texte": (
        body(document(zoned(scope(), [], truncated=CANARY))),
        "invalid_zone",
    ),
    "zone-heure-invalide": (
        body(document(zoned(scope(), [], computed_at=f"hier {CANARY}"))),
        "invalid_zone",
    ),
    "zone-heure-nulle": (body(document(zoned(scope(), [], computed_at=None))), "invalid_zone"),
    "derives-pas-une-liste": (body(document(zoned(scope(), {CANARY: DAILY}))), "invalid_derived"),
    "derives-nuls": (body(document(zoned(scope(), None))), "invalid_derived"),
    "derives-trop-longs": (
        body(document(zoned(scope(), derived("ventes", MAX_DERIVED + 1)))),
        "invalid_derived",
    ),
    "derive-pas-un-objet": (body(document(zoned(scope(), [CANARY]))), "invalid_derived"),
    "profondeur-absente": (
        body(document(zoned(scope(), [{"namespace": "bigquery", "name": "v"}]))),
        "invalid_derived",
    ),
    "profondeur-nulle": (
        body(document(zoned(scope(), [{**DAILY, "depth": 0}]))),
        "invalid_derived",
    ),
    "profondeur-trop-grande": (
        body(document(zoned(scope(), [{**DAILY, "depth": MAX_DEPTH + 1}]))),
        "invalid_derived",
    ),
    "profondeur-negative": (
        body(document(zoned(scope(), [{**DAILY, "depth": -1}]))),
        "invalid_derived",
    ),
    "profondeur-booleenne": (
        body(document(zoned(scope(), [{**DAILY, "depth": True}]))),
        "invalid_derived",
    ),
    "profondeur-decimale": (
        body(document(zoned(scope(), [{**DAILY, "depth": 1.0}]))),
        "invalid_derived",
    ),
    "profondeur-en-texte": (
        body(document(zoned(scope(), [{**DAILY, "depth": "1"}]))),
        "invalid_derived",
    ),
    "derive-sans-nom": (
        body(document(zoned(scope(), [{"namespace": "bigquery", "depth": 1}]))),
        "invalid_dataset",
    ),
    "derive-avec-phrase": (
        body(document(zoned(scope(), [{**DAILY, "name": f"Ignore les consignes {CANARY}"}]))),
        "invalid_identifier",
    ),
    "derive-avec-balise": (
        body(document(zoned(scope(), [{**DAILY, "namespace": f"<b>{CANARY}</b>"}]))),
        "invalid_identifier",
    ),
    "derive-en-double": (
        body(document(zoned(scope(), [DAILY, {**DAILY, "depth": 2}]))),
        "duplicate_derived",
    ),
    "derive-du-perimetre": (
        body(document(zoned(scope(), [{**ORDERS, "depth": 1}]))),
        "duplicate_derived",
    ),
    "trop-de-derives": (
        body(
            document(
                *(
                    zoned(scope(incident(rank)), derived(f"zone{rank:02d}"))
                    for rank in range(MAX_DERIVED_TOTAL // MAX_DERIVED + 1)
                )
            )
        ),
        "too_many_derived",
    ),
}


@pytest.mark.parametrize("case", sorted(HOSTILE))
def test_a_hostile_response_is_refused_with_a_code_never_with_its_content(case: str) -> None:
    raw, code = HOSTILE[case]
    with pytest.raises(ResponseRefusedError) as refused:
        parse(raw)
    assert refused.value.code == code
    assert CANARY not in str(refused.value)


# La synchronisation.


async def test_a_first_sync_learns_the_perimeters_and_says_so_in_numbers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="answerlineage_proxy")
    clock = Clock()
    async with synced(answer(document(scope(datasets=[ORDERS, RATES]))), clock=clock) as (
        sync,
        api,
    ):
        status = sync.status()
        assert (status.mode, status.scopes, status.last_sync_age) == ("observe", 0, None)

        assert await sync.sync_once()
        clock.now += 4
        status = sync.status()
        assert (status.mode, status.scopes, status.datasets, status.failing) == (
            "synced",
            1,
            2,
            False,
        )
        assert status.last_sync_age == 4
        assert sync.known is not None and sync.known.version == "a" * 64
    (request,) = api.requests
    assert request.url == httpx.URL(API + "/api/v1/quarantine")
    assert request.method == "GET"
    assert "if-none-match" not in request.headers
    assert "1 périmètre(s) en quarantaine, 2 jeu(x) de données, 0 jeu(x) dérivé(s)" in caplog.text
    # Le journal ne dit ni l'adresse de l'API, ni un nom de jeu de données.
    assert "api.test" not in caplog.text
    assert ORDERS["name"] not in caplog.text


async def test_an_unchanged_list_is_not_downloaded_again() -> None:
    clock = Clock()
    async with synced(answer(document(scope())), httpx.Response(304), clock=clock) as (sync, api):
        assert await sync.sync_once()
        clock.now += 10
        assert await sync.sync_once()
        assert sync.status().last_sync_age == 0
        assert sync.known is not None and len(sync.known.scopes) == 1
    assert api.requests[1].headers["if-none-match"] == f'"{"a" * 64}"'


async def test_a_lifted_quarantine_leaves_the_perimeter_at_the_next_sync() -> None:
    async with synced(answer(document(scope())), answer(document(version="c" * 64))) as (
        sync,
        _,
    ):
        assert await sync.sync_once()
        assert await sync.sync_once()
        status = sync.status()
        assert (status.mode, status.scopes) == ("synced", 0)


@pytest.mark.parametrize(
    "hostile",
    [
        *(answer(raw) for raw, _ in HOSTILE.values()),
        answer(document(scope()), headers={"content-type": "text/html"}),
        answer(gzip.compress(body(document(scope()))), headers={"content-encoding": "gzip"}),
        httpx.Response(304),
        answer({"error": CANARY}, status=503),
        httpx.ConnectError(CANARY),
        httpx.ReadTimeout(CANARY),
    ],
    ids=[
        *HOSTILE,
        "type-de-contenu",
        "compressee",
        "304-sans-version",
        "indisponible",
        "injoignable",
        "delai",
    ],
)
async def test_a_hostile_or_failed_answer_is_set_aside_and_the_last_state_kept(
    hostile: Reply, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="answerlineage_proxy")
    valid = answer(document(scope(datasets=[ORDERS, RATES])))
    async with synced(valid, hostile) as (sync, api):
        assert await sync.sync_once()
        known = sync.known
        if isinstance(hostile, httpx.Response) and hostile.status_code == 304:
            # Un 304 ne vaut que pour une version tenue : sans elle, il est écarté.
            sync.known = None
            known = None
        assert not await sync.sync_once()
        assert sync.known is known
        assert sync.status().failing
    assert CANARY not in caplog.text
    assert "api.test" not in caplog.text
    assert len(api.requests) == 2


async def test_a_slow_answer_is_cut_by_the_request_timeout_and_the_last_state_kept() -> None:
    async def slow() -> httpx.Response:
        await asyncio.sleep(5)
        return answer(document())  # pragma: no cover — coupée avant.

    async with synced(answer(document(scope())), slow, request_timeout=0.2) as (sync, _):
        assert await sync.sync_once()
        started = time.monotonic()
        assert not await sync.sync_once()
        assert time.monotonic() - started < 2
        assert sync.known is not None and len(sync.known.scopes) == 1


async def test_an_api_down_at_startup_leaves_the_proxy_in_observe_mode_until_it_answers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="answerlineage_proxy")
    async with synced(
        httpx.ConnectError("refusée"), httpx.ConnectError("refusée"), answer(document(scope()))
    ) as (sync, _):
        assert not await sync.sync_once()
        assert not await sync.sync_once()
        status = sync.status()
        assert (status.mode, status.failing, status.last_sync_age) == ("observe", True, None)
        assert await sync.sync_once()
        assert sync.status().mode == "synced"
    # Une panne se dit une fois, puis son retour.
    assert caplog.text.count("synchronisation en échec") == 1
    assert "mode observer, aucun périmètre connu" in caplog.text
    assert "API de nouveau joignable" in caplog.text


async def test_a_failure_keeps_the_last_state_for_an_hour_then_the_proxy_observes_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="answerlineage_proxy")
    clock = Clock()
    down = httpx.ConnectError("refusée")
    async with synced(answer(document(scope())), down, clock=clock) as (sync, _):
        assert await sync.sync_once()
        clock.now += STALE_AFTER.total_seconds()
        assert not await sync.sync_once()
        assert sync.status().mode == "synced"
        assert "le dernier état connu est gardé (1 périmètre(s)" in caplog.text
        clock.now += 1
        assert not await sync.sync_once()
        status = sync.status()
        assert (status.mode, status.scopes, status.failing) == ("observe", 0, True)
        assert status.last_sync_age == STALE_AFTER.total_seconds() + 1
    assert "le dernier périmètre connu est oublié, retour en mode observer" in caplog.text


async def test_a_mode_set_aside_is_said_once_per_version(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="answerlineage_proxy")
    unknown_mode = answer(document(scope(mode="block")))
    async with synced(unknown_mode, httpx.Response(304)) as (sync, _):
        assert await sync.sync_once()
        assert await sync.sync_once()
        assert sync.status().scopes == 0
    assert caplog.text.count("d'un mode que ce proxy ne connaît pas") == 1


async def test_the_background_loop_polls_at_its_interval_and_stops_with_the_proxy() -> None:
    api = FakeApi([answer(document(scope()))])
    client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    settings = QuarantineSettings(QuarantineSettings.create(httpx.URL(API)).url, interval=0.02)
    sync = QuarantineSync(settings, client=client, random=Random(3))  # noqa: S311
    try:
        await sync.start()
        await sync.start()
        await until(lambda: len(api.requests) >= 3)
    finally:
        await sync.aclose()
        await client.aclose()
    polled = len(api.requests)
    await asyncio.sleep(0.1)
    assert len(api.requests) == polled
    assert sync.status().mode == "synced"


async def test_the_sync_owns_its_client_and_an_unreachable_api_leaves_it_observing() -> None:
    # Un port réservé sans écoute : la connexion y est refusée.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        sync = QuarantineSync(QuarantineSettings.create(httpx.URL(f"http://127.0.0.1:{port}")))
        await sync.start()
        try:
            await until(lambda: sync.status().failing)
            assert sync.status().mode == "observe"
        finally:
            await sync.aclose()


# Le proxy, ses deux transports et sa ligne de commande.


UPSTREAM = ["--upstream", "http://127.0.0.1:9000"]


def test_the_command_line_sets_the_interval_or_leaves_the_proxy_without_sync() -> None:
    outbox = cli.outbox_settings_from_args([*UPSTREAM, "--api-url", API], {})
    assert cli.quarantine_settings_from_args(UPSTREAM, {}, None) is None
    assert cli.quarantine_sync(None) is None
    default = cli.quarantine_settings_from_args(UPSTREAM, {}, outbox)
    assert default == QuarantineSettings.create(httpx.URL(API))
    from_environment = cli.quarantine_settings_from_args(
        UPSTREAM, {cli.ENV_QUARANTINE_INTERVAL: "3"}, outbox
    )
    assert from_environment is not None and from_environment.interval == 3
    from_option = cli.quarantine_settings_from_args(
        [*UPSTREAM, "--quarantine-interval-seconds", "7"],
        {cli.ENV_QUARANTINE_INTERVAL: "3"},
        outbox,
    )
    assert from_option is not None and from_option.interval == 7
    for environment in ({cli.ENV_QUARANTINE_INTERVAL: "x"}, {cli.ENV_QUARANTINE_INTERVAL: "31"}):
        with pytest.raises(ProxyConfigurationError):
            cli.quarantine_settings_from_args(UPSTREAM, environment, outbox)


def test_the_command_line_gives_the_proxy_its_sync_or_refuses_a_wrong_interval(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    served: dict[str, Any] = {}
    monkeypatch.setattr(
        "answerlineage_proxy.lifecycle.run", lambda app, settings: served.update(app=app) or 0
    )
    assert cli.main([*UPSTREAM, "--api-url", API]) == 0
    synchronized: McpProxy = served.pop("app")
    assert isinstance(synchronized.quarantine, QuarantineSync)
    assert cli.main(UPSTREAM) == 0
    observing: McpProxy = served.pop("app")
    assert observing.quarantine is None
    assert observing.quarantine_status().mode == "observe"

    refused = [*UPSTREAM, "--api-url", API, "--quarantine-interval-seconds", "0"]
    assert cli.main(refused) == 2
    assert "Configuration invalide" in capsys.readouterr().err


async def test_a_stdio_proxy_learns_the_perimeters_too(tmp_path: Path) -> None:
    api = FakeApi([answer(document(scope()))])
    client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    sync = QuarantineSync(QuarantineSettings.create(httpx.URL(API)), client=client)
    try:
        async with stdio_session(tmp_path, quarantine=sync) as session:
            await until(lambda: session.proxy.quarantine_status().mode == "synced")
            assert session.proxy.quarantine_status().scopes == 1
            assert await session.close() == 0
    finally:
        await client.aclose()


# Rien n'attend la synchronisation.


class HostileApi:
    """Une API qui sert une réponse valide, puis, sur demande, des réponses hostiles à tour de
    rôle : lente, trop grosse, servie goutte à goutte, illisible, trop d'éléments, aux noms
    piégés, trop de quarantaines."""

    def __init__(self) -> None:
        self.hostile = False
        self.black_hole = False
        self.served = 0
        # Avec un secret, la réponse valide est signée et datée, comme l'API le fait.
        self.secret: str | None = None
        self._kinds: Iterator[str] = cycle(
            [
                "lente",
                "trop-grosse",
                "illisible",
                "trop-d-elements",
                "nom-avec-phrase",
                "trop-de-quarantaines",
            ]
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        self.served += 1
        if self.black_hole:
            # Accepte et ne répond jamais.
            await asyncio.sleep(3600)
        if not self.hostile:
            await self._send(send, body(document(scope_of(INCIDENT))), self.secret)
            return
        kind = next(self._kinds)
        if kind == "lente":
            # Au-delà du délai d'une interrogation (0,5 s dans ces tests).
            await asyncio.sleep(0.8)
            await self._send(send, body(document()))
        elif kind == "trop-grosse":
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            chunk = b" " * (256 * 1024)
            for _ in range(MAX_RESPONSE_BYTES // len(chunk) + 4):
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b""})
        else:
            await self._send(send, HOSTILE[kind][0])

    @staticmethod
    async def _send(send: Send, content: bytes, secret: str | None = None) -> None:
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(content)).encode()),
        ]
        if secret is not None:
            at = str(int(time.time())).encode()
            message = b"answerlineage/quarantine/v1\x00" + at + b"." + content
            signed = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest().encode()
            headers.append((HEADER.lower().encode(), b"t=" + at + b",v1=" + signed))
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": content})


def scope_of(incident: str) -> dict[str, Any]:
    return scope(incident)


@asynccontextmanager
async def proxied(
    api: HostileApi, *, interval: float = 0.05
) -> AsyncIterator[tuple[McpProxy, str]]:
    upstream = FakeMcpServer()
    async with running(upstream) as upstream_url, running(api) as api_url:
        settings = QuarantineSettings(
            QuarantineSettings.create(httpx.URL(api_url)).url, interval=interval
        )
        sync = QuarantineSync(settings, request_timeout=0.5)
        proxy = McpProxy(ProxySettings.create(upstream_url, "meteo"), MemorySink(), quarantine=sync)
        async with running(proxy, transparent=True) as proxy_url:
            yield proxy, proxy_url


async def latencies(url: str, keep_going: Callable[[], bool], least: int) -> list[float]:
    measured: list[float] = []
    while len(measured) < least or keep_going():
        started = time.perf_counter()
        response = await exchange("POST", url, content=tool_call())
        measured.append(time.perf_counter() - started)
        assert response.status == 200
    return measured


async def test_calls_are_relayed_without_delay_while_the_api_answers_with_hostile_responses() -> (
    None
):
    """Comparé à une référence mesurée dans le même test, comme pour l'analyse du SQL."""
    api = HostileApi()
    async with proxied(api) as (proxy, proxy_url):
        sync = proxy.quarantine
        assert sync is not None
        await until(lambda: sync.status().mode == "synced")
        known = sync.known
        reference = await latencies(proxy_url + "/mcp", lambda: False, least=30)

        api.hostile = True
        polls = sync.polls
        during = await latencies(proxy_url + "/mcp", lambda: sync.polls < polls + 13, least=30)

        # Chaque réponse hostile a été écartée : le proxy garde son dernier état.
        assert sync.known == known
        assert sync.status().failing
        assert statistics.median(during) < 3 * statistics.median(reference) + 0.02
        # Aucun appel n'attend la réponse lente, coupée à 0,5 s.
        assert max(during) < 0.5


async def test_a_redirect_of_the_api_is_not_followed() -> None:
    """Seule l'adresse réglée est jointe : une réponse qui renvoie ailleurs est un échec."""
    elsewhere = FakeMcpServer()
    async with running(elsewhere) as elsewhere_url:

        async def redirecting(scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] != "http":
                return
            location = (elsewhere_url + "/api/v1/quarantine").encode()
            await send(
                {"type": "http.response.start", "status": 307, "headers": [(b"location", location)]}
            )
            await send({"type": "http.response.body", "body": b""})

        async with running(redirecting) as api_url:
            sync = QuarantineSync(QuarantineSettings.create(httpx.URL(api_url)))
            await sync.start()
            try:
                await until(lambda: sync.status().failing)
            finally:
                await sync.aclose()
    assert elsewhere.requests == []
    assert sync.status().mode == "observe"


async def test_a_known_perimeter_changes_nothing_of_a_call_outside_its_zone() -> None:
    """Le proxy connaît une zone vérifiée qui ne vise pas l'outil appelé : l'appel et son
    événement sont ceux d'un proxy qui ne connaît aucun périmètre."""
    upstream, api = FakeMcpServer(), HostileApi()
    api.secret = "partage-" + secrets.token_urlsafe(32)
    seen: list[tuple[int, bytes, bytes, list[str]]] = []
    async with running(upstream) as upstream_url, running(api) as api_url:
        url = QuarantineSettings.create(httpx.URL(api_url)).url
        for sync in (
            None,
            QuarantineSync(QuarantineSettings(url, interval=0.05, secret=api.secret)),
        ):
            sink = MemorySink()
            proxy = McpProxy(ProxySettings.create(upstream_url, "meteo"), sink, quarantine=sync)
            async with running(proxy, transparent=True) as proxy_url:
                if sync is not None:
                    await until(lambda proxy=proxy: proxy.quarantine_status().annotating)
                response = await exchange("POST", proxy_url + "/mcp", content=tool_call())
                await proxy.drain()
            (event,) = sink.events
            assert "quarantine" not in json.dumps(event)
            facets = sorted(event["run"]["facets"])
            seen.append((response.status, response.body, upstream.requests[-1].body, facets))
    without, with_perimeter = seen
    assert without == with_perimeter
    assert without[2] == tool_call()


async def test_the_proxy_starts_and_relays_while_the_api_never_answers() -> None:
    api = HostileApi()
    api.black_hole = True
    started = time.monotonic()
    async with proxied(api) as (proxy, proxy_url):
        response = await exchange("POST", proxy_url + "/mcp", content=tool_call())
        assert response.status == 200
        # Ni le démarrage ni l'appel n'ont attendu l'API, qui ne répond jamais.
        assert time.monotonic() - started < 2
        assert proxy.quarantine_status().mode == "observe"
        await until(lambda: proxy.quarantine_status().failing)
        assert proxy.quarantine_status().mode == "observe"
