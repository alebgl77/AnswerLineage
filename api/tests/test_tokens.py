"""La vérification d'un jeton porteur : l'émetteur configuré seul, ses clés en cache.

L'émetteur de test génère ses clés ici et sert ses documents en mémoire : aucun
réseau. Chaque jeton forgé est refusé par un code stable, sans que la réponse
ne recopie rien du jeton.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from answerlineage_api.app import create_app
from answerlineage_api.auth.access import MAX_TOKEN_LENGTH, Authentication
from answerlineage_api.auth.fetch import IssuerUnavailableError
from answerlineage_api.auth.oidc import (
    KEYS_TTL,
    LEEWAY_SECONDS,
    REFRESH_INTERVAL,
    OidcVerifier,
    TokenRefusedError,
)
from answerlineage_api.auth.roles import Role
from api.tests.issuer import (
    EC_KEY,
    EMITTER_CLIENT,
    EXPLORER_CLIENT,
    FORGER_KEY,
    KIDS,
    RSA_KEY,
    TEST_ISSUER,
    LocalIssuer,
    b64,
    public_pem,
)

pytestmark = pytest.mark.anyio

# Une marque inventée, glissée dans chaque jeton forgé : aucune réponse ni aucun
# journal ne doit la porter.
CANARY = "canari-jeton-forge-61e0"
NOWHERE = "postgresql+psycopg://personne@127.0.0.1:1/aucune"
HEALTH = "/api/v1/health"


@dataclass
class Clock:
    now: float = 1000.0

    def __call__(self) -> float:
        return self.now


def verifier(issuer: LocalIssuer | None = None, clock: Clock | None = None, **changes: Any) -> Any:
    issuer = issuer or LocalIssuer()
    return OidcVerifier(issuer.settings(**changes), fetch=issuer.fetch, clock=clock or Clock())


def claims(**changes: Any) -> dict[str, Any]:
    # Valables un jour : les jetons que le module forge à son import le restent toute la
    # session, et chacun n'est refusé que pour ce qu'on y a changé.
    return {**TEST_ISSUER.claims(("auditor",), lifetime=86_400), "sub": CANARY, **changes}


def unsigned(header: dict[str, Any], payload: dict[str, Any], signature: bytes = b"") -> str:
    encode = json.dumps
    return ".".join((b64(encode(header).encode()), b64(encode(payload).encode()), b64(signature)))


def hs256_with_public_key() -> str:
    """La confusion d'algorithme : un HMAC dont la clé est la clé publique de l'émetteur."""
    header = {"alg": "HS256", "typ": "JWT", "kid": KIDS["RS256"]}
    signing = ".".join((b64(json.dumps(header).encode()), b64(json.dumps(claims()).encode())))
    signature = hmac.new(public_pem(), signing.encode(), hashlib.sha256).digest()
    return f"{signing}.{b64(signature)}"


def altered_signature() -> str:
    head, body, signature = TEST_ISSUER.sign(claims()).split(".")
    flipped = bytearray(base64.urlsafe_b64decode(signature + "=="))
    flipped[0] ^= 0x01
    return f"{head}.{body}.{b64(bytes(flipped))}"


def altered_payload() -> str:
    head, _, signature = TEST_ISSUER.sign(claims()).split(".")
    body = b64(json.dumps(claims(roles=["administrator"])).encode())
    return f"{head}.{body}.{signature}"


def now() -> int:
    return int(time.time())


FORGED: dict[str, str] = {
    "alg none": unsigned({"alg": "none", "typ": "JWT", "kid": KIDS["RS256"]}, claims()),
    "alg None": unsigned({"alg": "None", "kid": KIDS["RS256"]}, claims()),
    "HS256 signé avec la clé publique": hs256_with_public_key(),
    "autre émetteur": TEST_ISSUER.sign(claims(iss="https://attaquant.example/realms/lignage")),
    "autre audience": TEST_ISSUER.sign(claims(aud="une-autre-api")),
    "sans audience": TEST_ISSUER.sign(
        {key: value for key, value in claims().items() if key != "aud"}
    ),
    "sans expiration": TEST_ISSUER.sign(
        {key: value for key, value in claims().items() if key != "exp"}
    ),
    "pas encore valide": TEST_ISSUER.sign(claims(nbf=now() + 86_400)),
    "émis dans le futur": TEST_ISSUER.sign(claims(iat=now() + 86_400)),
    "kid inconnu": TEST_ISSUER.sign(claims(), kid="cle-inconnue"),
    "sans kid": jwt.encode(claims(), RSA_KEY, algorithm="RS256"),
    "clé d'un faussaire sous le kid de l'émetteur": TEST_ISSUER.sign(claims(), key=FORGER_KEY),
    "clé d'un faussaire désignée par jku": TEST_ISSUER.sign(
        claims(), key=FORGER_KEY, headers={"jku": "https://attaquant.example/jwks.json"}
    ),
    "kid d'une clé EC pour un jeton RS256": TEST_ISSUER.sign(claims(), kid=KIDS["ES256"]),
    "kid d'une clé RSA pour un jeton ES256": TEST_ISSUER.sign(
        claims(), algorithm="ES256", kid=KIDS["RS256"], key=EC_KEY
    ),
    "extension critique": TEST_ISSUER.sign(claims(), headers={"crit": ["exp"], "exp": 1}),
    "signature altérée": altered_signature(),
    "charge altérée": altered_payload(),
    "deux parties": ".".join(TEST_ISSUER.sign(claims()).split(".")[:2]),
    "quatre parties": TEST_ISSUER.sign(claims()) + ".x",
    "pas du JSON": "aW5jb25udQ.e30.c2ln",
    "RS512": TEST_ISSUER.sign(claims(), algorithm="RS512", key=RSA_KEY, kid=KIDS["RS256"]),
}


@pytest.mark.parametrize("token", FORGED.values(), ids=FORGED.keys())
async def test_a_forged_token_is_refused(token: str) -> None:
    with pytest.raises(TokenRefusedError) as refused:
        await verifier().roles(token)
    assert refused.value.code == "invalid_token"
    assert CANARY not in str(refused.value)


async def test_an_expired_token_says_so() -> None:
    expired = TEST_ISSUER.sign(claims(exp=now() - LEEWAY_SECONDS - 30))
    with pytest.raises(TokenRefusedError) as refused:
        await verifier().roles(expired)
    assert refused.value.code == "expired_token"


async def test_an_expired_token_whose_signature_is_wrong_is_invalid_not_expired() -> None:
    forged = TEST_ISSUER.sign(claims(exp=now() - 3600), key=FORGER_KEY)
    with pytest.raises(TokenRefusedError) as refused:
        await verifier().roles(forged)
    assert refused.value.code == "invalid_token"


async def test_the_clock_tolerance_is_of_one_minute() -> None:
    check = verifier()
    just_expired = TEST_ISSUER.sign(claims(exp=now() - LEEWAY_SECONDS + 10))
    almost_valid = TEST_ISSUER.sign(claims(nbf=now() + LEEWAY_SECONDS - 10))
    assert await check.roles(just_expired) == {Role.AUDITOR}
    assert await check.roles(almost_valid) == {Role.AUDITOR}


@pytest.mark.parametrize("algorithm", ["RS256", "ES256", "EdDSA"])
async def test_each_asymmetric_algorithm_of_the_issuer_is_accepted(algorithm: str) -> None:
    token = TEST_ISSUER.sign(claims(), algorithm=algorithm)
    assert await verifier().roles(token) == {Role.AUDITOR}


# Les rôles.


async def test_without_table_a_claim_value_that_names_a_role_gives_it() -> None:
    token = TEST_ISSUER.sign(claims(roles=["reader", "auditor", "emitter", "inconnu"]))
    # Le rôle d'émetteur ne se lit jamais d'une revendication de rôles.
    assert await verifier().roles(token) == {Role.READER, Role.AUDITOR}


async def test_the_table_maps_the_values_of_the_issuer_to_roles() -> None:
    check = verifier(
        role_map={
            "lignage-lecteurs": "reader",
            "lignage-admins": ["administrator", "auditor"],
        }
    )
    token = TEST_ISSUER.sign(claims(roles=["lignage-admins", "reader", "autre"]))
    # Avec une table, seules les valeurs qu'elle nomme comptent.
    assert await check.roles(token) == {Role.ADMINISTRATOR, Role.AUDITOR}


async def test_the_roles_claim_may_be_nested_or_a_single_text() -> None:
    check = verifier(roles_claim="realm_access.roles")
    nested = TEST_ISSUER.sign(claims(roles=[], realm_access={"roles": ["incident_manager"]}))
    assert await check.roles(nested) == {Role.INCIDENT_MANAGER}
    single = verifier(roles_claim="groups")
    alone = TEST_ISSUER.sign(claims(roles=[], groups="data_steward"))
    assert await single.roles(alone) == {Role.DATA_STEWARD}


async def test_a_malformed_roles_claim_gives_no_role() -> None:
    check = verifier(roles_claim="realm_access.roles")
    for value in ({"realm_access": "reader"}, {"realm_access": {"roles": {"reader": True}}}):
        token = TEST_ISSUER.sign(claims(roles=[], **value))
        assert await check.roles(token) == frozenset()
    too_long = TEST_ISSUER.sign(claims(roles=["x" * 300, 42, None, "reader"]))
    assert await verifier().roles(too_long) == {Role.READER}


@pytest.mark.parametrize("claim", ["azp", "client_id"])
async def test_the_emitter_role_comes_from_the_client_identifier(claim: str) -> None:
    token = TEST_ISSUER.sign(claims(roles=[], **{claim: EMITTER_CLIENT}))
    assert await verifier().roles(token) == {Role.EMITTER}
    other = TEST_ISSUER.sign(claims(roles=[], **{claim: "un-autre-client"}))
    assert await verifier().roles(other) == frozenset()


# Les clés de l'émetteur, en cache.


async def test_the_keys_are_read_once_for_many_tokens() -> None:
    issuer = LocalIssuer()
    check = verifier(issuer)
    for algorithm in ("RS256", "ES256", "EdDSA", "RS256"):
        await check.roles(issuer.sign(claims(), algorithm=algorithm))
    assert check.keys.reads == 1
    assert len(issuer.reads) == 2


async def test_an_unknown_kid_reads_the_keys_again_at_most_once_per_interval() -> None:
    issuer, clock = LocalIssuer(), Clock()
    check = verifier(issuer, clock)
    await check.roles(issuer.sign(claims()))
    for _ in range(20):
        with pytest.raises(TokenRefusedError):
            await check.roles(issuer.sign(claims(), kid="cle-inconnue"))
    assert check.keys.reads == 1
    clock.now += REFRESH_INTERVAL
    with pytest.raises(TokenRefusedError):
        await check.roles(issuer.sign(claims(), kid="cle-inconnue"))
    assert check.keys.reads == 2


async def test_a_rotated_key_is_learnt_on_its_first_token() -> None:
    issuer, clock = LocalIssuer(published=("RS256",)), Clock()
    check = verifier(issuer, clock)
    await check.roles(issuer.sign(claims()))
    issuer.published = ("RS256", "ES256")
    clock.now += REFRESH_INTERVAL
    assert await check.roles(issuer.sign(claims(), algorithm="ES256")) == {Role.AUDITOR}


async def test_without_any_key_the_issuer_is_unavailable() -> None:
    issuer = LocalIssuer(unavailable=True)
    with pytest.raises(IssuerUnavailableError):
        await verifier(issuer).roles(issuer.sign(claims()))


async def test_known_keys_still_serve_while_the_issuer_is_down() -> None:
    issuer, clock = LocalIssuer(), Clock()
    check = verifier(issuer, clock)
    await check.roles(issuer.sign(claims()))
    issuer.unavailable = True
    clock.now += KEYS_TTL + 1
    assert await check.roles(issuer.sign(claims())) == {Role.AUDITOR}
    clock.now += REFRESH_INTERVAL
    with pytest.raises(TokenRefusedError):
        await check.roles(issuer.sign(claims(), kid="cle-inconnue"))


class Served(LocalIssuer):
    """Un émetteur dont les documents sont remplacés : ce qu'un attaquant y mettrait."""

    def __init__(self, discovery: Any = None, jwks: Any = None) -> None:
        super().__init__()
        self._discovery, self._jwks = discovery, jwks

    def discovery(self) -> dict[str, Any]:
        return super().discovery() if self._discovery is None else self._discovery

    def jwks(self) -> dict[str, Any]:
        return super().jwks() if self._jwks is None else self._jwks


def jwk(algorithm: str = "RS256", **changes: Any) -> dict[str, Any]:
    (found,) = [key for key in LocalIssuer().jwks()["keys"] if key["alg"] == algorithm]
    return {**found, **changes}


@pytest.mark.parametrize(
    ("discovery", "jwks"),
    [
        ({"issuer": "https://attaquant.example", "jwks_uri": "https://idp/certs"}, None),
        ({"issuer": TEST_ISSUER.url, "jwks_uri": "http://attaquant.example/certs"}, None),
        ({"issuer": TEST_ISSUER.url, "jwks_uri": "file:///etc/passwd"}, None),
        ({"issuer": TEST_ISSUER.url}, None),
        (["pas un objet"], None),
        (None, {"keys": "pas une liste"}),
        (None, {"keys": [jwk()] * 65}),
    ],
    ids=[
        "découverte d'un autre émetteur",
        "JWKS en http",
        "JWKS en fichier",
        "sans JWKS",
        "découverte illisible",
        "JWKS illisible",
        "JWKS démesuré",
    ],
)
async def test_a_poisoned_document_of_the_issuer_gives_no_key(discovery: Any, jwks: Any) -> None:
    issuer = Served(discovery, jwks)
    with pytest.raises(IssuerUnavailableError):
        await verifier(issuer).roles(TEST_ISSUER.sign(claims()))


@pytest.mark.parametrize(
    "key",
    [
        {"kty": "oct", "k": b64(public_pem()), "kid": KIDS["RS256"]},
        jwk(use="enc"),
        jwk(alg="HS256"),
        jwk(d="secret"),
        jwk(kid=""),
        jwk("ES256", crv="P-384"),
        jwk(n=b64(b"\x01" * 64)),
    ],
    ids=[
        "clé symétrique",
        "clé de chiffrement",
        "algorithme HMAC",
        "clé privée publiée",
        "kid vide",
        "courbe P-384 pour ES256",
        "RSA de 512 bits",
    ],
)
async def test_a_key_that_is_not_an_admitted_public_signing_key_is_ignored(
    key: dict[str, Any],
) -> None:
    issuer = Served(jwks={"keys": [key]})
    with pytest.raises(TokenRefusedError):
        await verifier(issuer).roles(TEST_ISSUER.sign(claims(), kid=key["kid"] or None))


async def test_a_kid_published_twice_is_not_trusted() -> None:
    issuer = Served(jwks={"keys": [jwk(), jwk("ES256", kid=KIDS["RS256"])]})
    with pytest.raises(TokenRefusedError):
        await verifier(issuer).roles(TEST_ISSUER.sign(claims()))


# Les réglages.


@pytest.mark.parametrize(
    ("changes", "named"),
    [
        ({"roles_claim": "rôles"}, "revendication"),
        ({"roles_claim": "a..b"}, "revendication"),
        ({"role_map": {"admins": "emitter"}}, "table des rôles"),
        ({"role_map": {"admins": ["reader", "inconnu"]}}, "table des rôles"),
        ({"emitter_clients": [EXPLORER_CLIENT]}, "client de l'explorateur"),
        ({"explorer_client_id": TEST_ISSUER.audience}, "audience"),
        ({"emitter_clients": ["client\nfaux"]}, "client émetteur"),
        ({"explorer_scope": "openid\n"}, "portée"),
    ],
)
def test_an_unusable_setting_is_refused_and_named(changes: dict[str, Any], named: str) -> None:
    with pytest.raises(ValueError, match=named):
        TEST_ISSUER.settings(**changes)


@pytest.mark.parametrize(
    "issuer",
    ["http://idp.exemple.test", "https://u:p@idp.exemple.test", "ftp://idp", "pas une adresse"],
)
def test_the_issuer_is_reached_in_https_or_on_the_loopback(issuer: str) -> None:
    with pytest.raises(ValueError, match="https"):
        LocalIssuer(url=issuer).settings()
    assert LocalIssuer(url="http://127.0.0.1:8080/realms/x").settings().issuer.startswith("http")


# Par l'application : l'en-tête, les réponses, rien de recopié.


async def call(headers: list[tuple[str, str]], issuer: LocalIssuer | None = None) -> httpx.Response:
    engine = create_async_engine(NOWHERE)
    try:
        app = create_app(engine, authentication=(issuer or TEST_ISSUER).authentication())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.get(HEALTH, headers=headers)
    finally:
        await engine.dispose()


async def test_a_sound_token_opens_the_route(caplog: pytest.LogCaptureFixture) -> None:
    token = TEST_ISSUER.sign(claims())
    with caplog.at_level(logging.DEBUG):
        answered = await call([("authorization", f"Bearer {token}")])
    assert answered.status_code == 200
    assert CANARY not in caplog.text


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (None, "missing_token"),
        ("Basic dXRpbGlzYXRldXI6bW90", "invalid_token"),
        ("Bearer", "invalid_token"),
        ("Bearer ", "invalid_token"),
        (f"Bearer {CANARY} {CANARY}", "invalid_token"),
        (f'Bearer {CANARY}"<script>', "invalid_token"),
        ("Bearer " + "a" * (MAX_TOKEN_LENGTH + 1), "invalid_token"),
        ("Bearer " + FORGED["alg none"], "invalid_token"),
        ("Bearer " + FORGED["HS256 signé avec la clé publique"], "invalid_token"),
        ("Bearer " + FORGED["kid inconnu"], "invalid_token"),
    ],
    ids=[
        "sans en-tête",
        "autre schéma",
        "sans jeton",
        "jeton vide",
        "deux jetons",
        "hors du jeu de caractères",
        "jeton démesuré",
        "alg none",
        "confusion d'algorithme",
        "kid inconnu",
    ],
)
async def test_a_refused_header_says_its_code_and_echoes_nothing(
    value: str | None, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    headers = [] if value is None else [("authorization", value)]
    with caplog.at_level(logging.DEBUG):
        answered = await call(headers)
    assert answered.status_code == 401
    assert answered.json() == {"error": code}
    expected = "Bearer" if code == "missing_token" else 'Bearer error="invalid_token"'
    assert answered.headers["www-authenticate"] == expected
    assert CANARY not in answered.text + caplog.text
    if value is not None:
        assert value not in answered.text + caplog.text


async def test_two_authorization_headers_are_refused() -> None:
    token = TEST_ISSUER.sign(claims())
    answered = await call([("authorization", f"Bearer {token}"), ("authorization", "Bearer x")])
    assert answered.json() == {"error": "invalid_token"}


async def test_the_scheme_is_read_without_regard_to_case() -> None:
    answered = await call([("authorization", f"bEaReR {TEST_ISSUER.sign(claims())}")])
    assert answered.status_code == 200


async def test_an_expired_token_is_refused_by_its_code() -> None:
    expired = TEST_ISSUER.sign(claims(exp=now() - 3600))
    answered = await call([("authorization", f"Bearer {expired}")])
    assert (answered.status_code, answered.json()) == (401, {"error": "expired_token"})


async def test_an_issuer_out_of_reach_makes_the_route_unavailable() -> None:
    issuer = LocalIssuer(unavailable=True)
    answered = await call([("authorization", f"Bearer {issuer.sign(claims())}")], issuer)
    assert (answered.status_code, answered.json()) == (503, {"error": "authentication_unavailable"})
    assert "www-authenticate" not in answered.headers


async def test_a_disabled_authentication_opens_every_route_with_every_role() -> None:
    engine = create_async_engine(NOWHERE)
    try:
        app = create_app(engine, authentication=Authentication.disabled())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            assert (await client.get(HEALTH)).status_code == 200
            mode = (await client.get("/api/v1/authentication")).json()
    finally:
        await engine.dispose()
    assert mode == {"mode": "disabled", "issuer": None, "client_id": None, "scope": None}
