"""Le proxy n'accorde foi qu'à une diffusion des périmètres signée et datée par l'API.

Avec le secret qu'il partage avec l'API, le proxy vérifie la signature et l'heure
de chaque réponse, ``200`` comme ``304``, avant d'en lire le contenu : une
réponse non signée, mal signée, trop ancienne ou venue du futur est écartée avec
un code, jamais avec son contenu, et le dernier état valide est gardé, une heure
au plus. Pendant une rotation, l'API signe sous deux secrets : un proxy qui
connaît l'un ou l'autre accepte. Sans secret, le proxy apprend les périmètres
mais ne sera jamais autorisé à annoter, et le dit une fois. Le secret ne paraît
ni au journal, ni dans les réglages, ni dans l'environnement du serveur MCP
lancé en stdio.

Les signatures de ces tests sont calculées ici, en bibliothèque standard, comme
le dit le format publié (``spec/quarantine/v1``), sans la fonction du proxy.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from answerlineage_proxy import cli
from answerlineage_proxy.config import ProxyConfigurationError
from answerlineage_proxy.quarantine import STALE_AFTER, QuarantineSettings, QuarantineSync
from answerlineage_proxy.quarantine_signature import (
    ENV_SECRET,
    ENV_SECRET_FILE,
    EXPIRED,
    FROM_FUTURE,
    HEADER,
    LABEL,
    MALFORMED,
    MAX_SECRET_LENGTH,
    MIN_SECRET_LENGTH,
    MISMATCH,
    TOLERANCE,
    UNSIGNED,
    SignatureRefusedError,
    read_secret,
    verify,
)
from proxy.tests.test_quarantine import (
    API,
    INCIDENT,
    ORDERS,
    OTHER,
    RATES,
    Clock,
    FakeApi,
    Reply,
    answer,
    body,
    document,
    scope,
)
from proxy.tests.test_stdio import present_in_the_server

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).resolve().parents[2]
VECTORS: dict[str, Any] = json.loads(
    (ROOT / "spec" / "quarantine" / "v1" / "vectors.json").read_text(encoding="utf-8")
)
# L'heure des tests : le 16 novembre 2026, peu avant 9 h UTC.
NOW = 1_794_819_310
# Un nom de jeu admis, que le journal ne doit jamais citer.
CANARY_NAME = "ventes.canari_diffusion_5c1e"
UPSTREAM = ["--upstream", "http://127.0.0.1:9000"]


def a_secret() -> str:
    return "partage-" + secrets.token_urlsafe(32)


def signature(content: bytes, keys: Sequence[str], at: int) -> str:
    """L'en-tête que l'API poserait, calculé ici comme le format publié le dit."""
    message = b"answerlineage/quarantine/v1\x00" + str(at).encode("ascii") + b"." + content
    signed = (hmac.new(key.encode(), message, hashlib.sha256).hexdigest() for key in keys)
    return f"t={at}," + ",".join(f"v1={value}" for value in signed)


def signed_answer(content: Any, keys: Sequence[str], at: int = NOW) -> httpx.Response:
    raw = body(content)
    return answer(raw, headers={HEADER: signature(raw, keys, at)})


def signed_304(version: str, keys: Sequence[str], at: int = NOW) -> httpx.Response:
    return httpx.Response(304, headers={HEADER: signature(version.encode(), keys, at)})


@dataclass
class WallClock:
    now: float = NOW

    def __call__(self) -> float:
        return self.now


@asynccontextmanager
async def verifying(
    *replies: Reply,
    secret: str | None,
    wall_clock: WallClock | None = None,
    clock: Clock | None = None,
) -> AsyncIterator[tuple[QuarantineSync, FakeApi]]:
    api = FakeApi(list(replies))
    client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    sync = QuarantineSync(
        QuarantineSettings.create(httpx.URL(API), secret=secret),
        client=client,
        clock=clock or Clock(),
        wall_clock=wall_clock or WallClock(),
    )
    try:
        yield sync, api
    finally:
        await sync.aclose()
        await client.aclose()


# Les vecteurs publiés.


def test_the_vectors_describe_the_header_label_and_tolerance_of_the_proxy() -> None:
    assert VECTORS["header"] == HEADER
    assert VECTORS["label"].encode("ascii") == LABEL
    assert VECTORS["tolerance_seconds"] == TOLERANCE
    assert VECTORS["algorithm"] == "HMAC-SHA-256"
    for secret in VECTORS["secrets"].values():
        assert MIN_SECRET_LENGTH <= len(secret) <= MAX_SECRET_LENGTH


@pytest.mark.parametrize("case", VECTORS["verification"], ids=lambda case: case["name"])
def test_the_proxy_decides_as_the_vectors_say(case: dict[str, Any]) -> None:
    secret = VECTORS["secrets"][case["secret"]]
    payload = case["payload"].encode("utf-8")
    if case["valid"]:
        assert verify(case["header"], payload, secret, now=case["now"]) > 0
    else:
        with pytest.raises(SignatureRefusedError) as refused:
            verify(case["header"], payload, secret, now=case["now"])
        assert refused.value.code == case["error"]


@pytest.mark.parametrize("case", VECTORS["signing"], ids=lambda case: case["name"])
def test_what_the_api_signs_the_proxy_accepts_under_each_of_its_secrets(
    case: dict[str, Any],
) -> None:
    for name in case["secrets"]:
        secret = VECTORS["secrets"][name]
        found = verify(case["header"], case["payload"].encode(), secret, now=case["timestamp"])
        assert found == case["timestamp"]


def test_the_vectors_refuse_each_situation_with_its_own_code() -> None:
    refused = {case["error"] for case in VECTORS["verification"] if not case["valid"]}
    assert refused == {UNSIGNED, MALFORMED, MISMATCH, EXPIRED, FROM_FUTURE}


def test_a_signature_is_checked_before_the_time_and_every_code_is_a_closed_word() -> None:
    secret = a_secret()
    forged = signature(b"{}", [a_secret()], NOW - 10 * TOLERANCE)
    with pytest.raises(SignatureRefusedError) as refused:
        verify(forged, b"{}", secret, now=NOW)
    # Une signature fausse ne dit rien de l'heure : seule une diffusion authentique est datée.
    assert refused.value.code == MISMATCH
    assert str(refused.value) == MISMATCH
    with pytest.raises(ValueError, match="tolérance"):
        verify(signature(b"{}", [secret], NOW), b"{}", secret, now=NOW, tolerance=-1)


# La synchronisation, avec un secret.


async def test_a_signed_diffusion_is_adopted_and_a_signed_304_keeps_it() -> None:
    secret = a_secret()
    wall = WallClock()
    clock = Clock()
    content = document(scope(datasets=[ORDERS, RATES]))
    async with verifying(
        signed_answer(content, [secret]),
        signed_304("a" * 64, [secret], NOW + 10),
        secret=secret,
        wall_clock=wall,
        clock=clock,
    ) as (sync, _):
        assert await sync.sync_once()
        assert sync.status().annotating
        clock.now += 10
        wall.now += 10
        assert await sync.sync_once()
        status = sync.status()
        assert (status.mode, status.scopes, status.datasets, status.last_sync_age) == (
            "synced",
            1,
            2,
            0,
        )
        assert status.annotating


def hostile_answers(secret: str) -> dict[str, tuple[httpx.Response, str]]:
    """Des réponses bien formées, qui font croire à une autre quarantaine, mais sans une
    diffusion valide de l'API ; et le code de chaque refus."""
    other = body(document(scope(OTHER, datasets=[{"namespace": "bigquery", "name": CANARY_NAME}])))
    stranger = a_secret()
    return {
        "non-signee": (answer(other), UNSIGNED),
        "en-tete-illisible": (answer(other, headers={HEADER: "t=hier"}), MALFORMED),
        "deux-en-tetes": (
            httpx.Response(
                200,
                content=other,
                headers=[
                    ("content-type", "application/json"),
                    (HEADER, signature(other, [secret], NOW)),
                    (HEADER, signature(other, [secret], NOW)),
                ],
            ),
            MALFORMED,
        ),
        "signee-par-un-tiers": (
            answer(other, headers={HEADER: signature(other, [stranger], NOW)}),
            MISMATCH,
        ),
        "corps-altere": (
            answer(other, headers={HEADER: signature(body(document()), [secret], NOW)}),
            MISMATCH,
        ),
        "hors-tolerance": (
            answer(other, headers={HEADER: signature(other, [secret], NOW - TOLERANCE - 1)}),
            EXPIRED,
        ),
        "venue-du-futur": (
            answer(other, headers={HEADER: signature(other, [secret], NOW + TOLERANCE + 1)}),
            FROM_FUTURE,
        ),
        "contenu-hostile-non-signe": (answer(b'{"version":"' + CANARY_NAME.encode()), UNSIGNED),
    }


@pytest.mark.parametrize("case", sorted(hostile_answers(a_secret())))
async def test_an_unsigned_badly_signed_stale_or_future_diffusion_is_set_aside_with_a_code(
    case: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="answerlineage_proxy")
    secret = a_secret()
    hostile, code = hostile_answers(secret)[case]
    async with verifying(signed_answer(document(scope()), [secret]), hostile, secret=secret) as (
        sync,
        api,
    ):
        assert await sync.sync_once()
        known = sync.known
        assert not await sync.sync_once()
        # Le dernier état valide est gardé : la quarantaine annoncée par un tiers n'est pas crue.
        assert sync.known is known
        assert sync.known is not None and sync.known.scopes[0].incident_id == INCIDENT
        assert sync.status().failing
        assert len(api.requests) == 2
    assert f"réponse écartée, {code}" in caplog.text
    assert CANARY_NAME not in caplog.text
    assert OTHER not in caplog.text


STALE_304: dict[str, tuple[Callable[[str], httpx.Response], str]] = {
    "non-signe": (lambda _: httpx.Response(304), UNSIGNED),
    "signe-par-un-tiers": (lambda _: signed_304("a" * 64, [a_secret()]), MISMATCH),
    # Le secret du proxy, mais sur une autre version que celle qu'il tient.
    "autre-version": (lambda secret: signed_304("c" * 64, [secret]), MISMATCH),
    "rejoue": (lambda secret: signed_304("a" * 64, [secret], NOW - TOLERANCE - 1), EXPIRED),
    "venu-du-futur": (
        lambda secret: signed_304("a" * 64, [secret], NOW + TOLERANCE + 1),
        FROM_FUTURE,
    ),
}


@pytest.mark.parametrize("case", sorted(STALE_304))
async def test_a_304_holds_only_signed_for_the_version_held_and_recently(
    case: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Des ``304`` rejoués à l'infini ne prolongent pas un état levé."""
    caplog.set_level(logging.INFO, logger="answerlineage_proxy")
    secret = a_secret()
    build, code = STALE_304[case]
    reply = build(secret)
    clock = Clock()
    async with verifying(
        signed_answer(document(scope()), [secret]), reply, secret=secret, clock=clock
    ) as (sync, _):
        assert await sync.sync_once()
        clock.now += 30
        assert not await sync.sync_once()
        assert sync.status().last_sync_age == 30
    assert f"réponse écartée, {code}" in caplog.text


async def test_without_a_valid_diffusion_for_an_hour_the_proxy_observes_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="answerlineage_proxy")
    secret = a_secret()
    clock, wall = Clock(), WallClock()
    replayed = signed_answer(document(scope()), [secret])
    async with verifying(replayed, secret=secret, clock=clock, wall_clock=wall) as (sync, _):
        assert await sync.sync_once()
        # La même réponse, rejouée : bonne tant qu'elle est dans la tolérance, plus ensuite.
        wall.now += TOLERANCE + 1
        clock.now += STALE_AFTER.total_seconds()
        assert not await sync.sync_once()
        assert sync.status().mode == "synced"
        clock.now += 1
        assert not await sync.sync_once()
        status = sync.status()
        assert (status.mode, status.scopes, status.annotating) == ("observe", 0, False)
    assert "réponse écartée, signature_expired" in caplog.text
    assert "le dernier périmètre connu est oublié, retour en mode observer" in caplog.text


async def test_during_a_rotation_a_proxy_that_knows_either_secret_accepts() -> None:
    old, new = a_secret(), a_secret()
    for known in (old, new):
        async with verifying(
            signed_answer(document(scope()), [new, old]),
            signed_304("a" * 64, [new, old]),
            secret=known,
        ) as (sync, _):
            assert await sync.sync_once()
            assert await sync.sync_once()
            assert sync.status().annotating
    # La rotation finie, l'API ne signe plus que sous le nouveau : l'ancien n'est plus cru.
    async with verifying(signed_answer(document(scope()), [new]), secret=old) as (sync, _):
        assert not await sync.sync_once()
        assert sync.status().mode == "observe"


# Sans secret.


async def test_without_a_secret_the_proxy_learns_the_perimeters_but_never_annotates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="answerlineage_proxy")
    # Une diffusion que l'API n'a pas signée, rendue à chaque interrogation.
    unsigned = answer(document(scope(), scope(OTHER), version="b" * 64))
    async with verifying(unsigned, secret=None) as (sync, api):
        await sync.start()
        await sync.start()
        for _ in range(3):
            assert await sync.sync_once()
        status = sync.status()
        assert (status.mode, status.scopes, status.annotating) == ("synced", 2, False)
        assert not sync.annotating
        assert len(api.requests) >= 3
    # Une fois, au démarrage, quel que soit le nombre d'interrogations.
    assert caplog.text.count("aucun secret de diffusion") == 1
    assert ENV_SECRET in caplog.text


async def test_with_a_secret_the_proxy_says_once_that_it_verifies_the_diffusion(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="answerlineage_proxy")
    secret = a_secret()
    async with verifying(signed_answer(document(), [secret]), secret=secret) as (sync, _):
        # Rien n'est encore connu : rien ne pourrait agir.
        assert not sync.status().annotating
        await sync.start()
        await sync.start()
        assert await sync.sync_once()
        assert sync.annotating
    assert caplog.text.count("diffusion signée par l'API") == 1
    assert "aucun secret de diffusion" not in caplog.text


# Le secret, ses sources, et ce qu'il ne traverse pas.


def test_the_secret_comes_from_a_file_or_a_variable_never_from_the_command_line(
    tmp_path: Path,
) -> None:
    secret = a_secret()
    held = tmp_path / "secret-diffusion"
    held.write_text(secret + "\n", encoding="utf-8")
    assert read_secret({}) is None
    assert read_secret({ENV_SECRET: secret}) == secret
    assert read_secret({ENV_SECRET_FILE: str(held)}) == secret
    assert read_secret({}, str(held)) == secret
    outbox = cli.outbox_settings_from_args([*UPSTREAM, "--api-url", API], {})
    from_file = cli.quarantine_settings_from_args(
        [*UPSTREAM, "--quarantine-secret-file", str(held)], {}, outbox
    )
    from_variable = cli.quarantine_settings_from_args(UPSTREAM, {ENV_SECRET: secret}, outbox)
    assert from_file is not None and from_file.secret == secret
    assert from_variable is not None and from_variable.secret == secret
    without = cli.quarantine_settings_from_args(UPSTREAM, {}, outbox)
    assert without is not None and without.secret is None
    # Aucune option ne prend le secret lui-même : la liste des processus le montrerait.
    options = {option for action in cli.build_parser()._actions for option in action.option_strings}
    assert "--quarantine-secret-file" in options
    assert "--quarantine-secret" not in options


def test_an_unusable_secret_is_refused_without_being_cited(tmp_path: Path) -> None:
    secret = a_secret()
    big, held = tmp_path / "grand", tmp_path / "secret"
    big.write_bytes(b"x" * 5000)
    held.write_text(secret, encoding="utf-8")
    short = secret[: MIN_SECRET_LENGTH - 1]
    spaced = secret[:20] + " " + secret[20:]
    invalid = tmp_path / "invalide"
    invalid.write_bytes(b"\xff" + secret.encode())
    for environment, secret_file in (
        ({ENV_SECRET: secret, ENV_SECRET_FILE: str(held)}, None),
        ({}, str(tmp_path / "absent")),
        ({}, str(big)),
        ({}, str(invalid)),
        ({ENV_SECRET: short}, None),
        ({ENV_SECRET: spaced}, None),
        ({ENV_SECRET: secret + "é"}, None),
        ({ENV_SECRET: "x" * (MAX_SECRET_LENGTH + 1)}, None),
    ):
        with pytest.raises(ProxyConfigurationError) as refused:
            read_secret(environment, secret_file)
        message = str(refused.value)
        for fragment in (secret, short, secret[:20], str(tmp_path)):
            assert fragment not in message
    with pytest.raises(ProxyConfigurationError, match="adresse d'API"):
        cli.quarantine_settings_from_args(UPSTREAM, {ENV_SECRET: secret}, None)


async def test_the_secret_is_neither_in_the_journal_nor_in_the_displayed_settings(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    secret, short = secrets.token_urlsafe(32), secrets.token_hex(8)
    settings = QuarantineSettings.create(httpx.URL(API), secret=secret)
    assert secret not in repr(settings)
    assert secret not in str(settings)
    async with verifying(
        signed_answer(document(scope()), [secret]),
        answer(document(), headers={HEADER: signature(b"{}", [secret], NOW)}),
        secret=secret,
    ) as (sync, _):
        assert await sync.sync_once()
        assert not await sync.sync_once()
        await sync.start()
        assert secret not in repr(sync.status())
    # Un secret refusé ne se cite pas, ni au journal ni sur la sortie d'erreur.
    with pytest.MonkeyPatch.context() as patched:
        patched.setenv(ENV_SECRET, short)
        assert cli.main([*UPSTREAM, "--api-url", API]) == 2
    captured = capsys.readouterr()
    assert "Configuration invalide" in captured.err
    for text in (caplog.text, captured.out, captured.err):
        assert secret not in text
        assert short not in text


async def test_the_stdio_server_does_not_get_the_secret_of_the_diffusion(tmp_path: Path) -> None:
    present = await present_in_the_server(
        tmp_path,
        {
            ENV_SECRET: a_secret(),
            ENV_SECRET_FILE: str(tmp_path / "secret-diffusion"),
            "ANSWERLINEAGE_ESSAI": "1",
        },
    )
    assert present == {ENV_SECRET: False, ENV_SECRET_FILE: False, "ANSWERLINEAGE_ESSAI": True}
