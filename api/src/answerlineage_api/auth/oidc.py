"""Vérification d'un jeton porteur JWT de l'émetteur OIDC configuré.

Seul l'émetteur réglé est cru. Ses clés se lisent de sa découverte, puis du
JWKS qu'elle désigne, et se gardent en cache (``KeySet``). Un jeton est
accepté quand :

- **il est un JWS compact** de trois parties, dont la taille est bornée par
  l'appelant (``access.py``) ;
- **son algorithme est asymétrique** : RS256, ES256 ou EdDSA, jamais ``none``
  ni HMAC ; l'en-tête n'impose rien d'autre (``crit`` refusé), et une clé
  n'est jamais prise de l'en-tête du jeton (``jwk``, ``jku``, ``x5u`` ignorés) ;
- **sa clé est connue** : le ``kid`` de l'en-tête désigne une clé publique du
  JWKS, du type de l'algorithme (RSA de 2048 bits au moins, EC P-256,
  Ed25519 ou Ed448), et qui n'en déclare pas un autre ;
- **sa signature est juste**, puis ses revendications : ``iss`` est
  l'émetteur, ``aud`` nomme l'audience de l'API, ``exp`` est à venir, ``nbf``
  et ``iat`` ne sont pas à venir, à une tolérance d'horloge de 60 s près.

Les rôles se lisent ensuite dans la revendication réglée, à travers la table de
correspondance ; le rôle d'émetteur, de l'identifiant du client (``azp``, à
défaut ``client_id``). Aucun refus ne dit pourquoi au-delà de son code, et rien
du jeton n'est gardé ni journalisé.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

import jwt

from answerlineage_api.auth.fetch import Fetch, IssuerUnavailableError, fetch_document, secure_url
from answerlineage_api.auth.roles import PERSON_ROLES, Role

ALGORITHMS: Final = ("RS256", "ES256", "EdDSA")
# Tolérance d'horloge entre l'émetteur et l'API, pour exp, nbf et iat.
LEEWAY_SECONDS: Final = 60
DISCOVERY_PATH: Final = "/.well-known/openid-configuration"
# Les clés se relisent au plus une fois par intervalle, quel que soit le nombre de
# jetons au kid inconnu : un appelant ne fait pas marteler l'émetteur.
REFRESH_INTERVAL: Final = 10.0
# Au-delà, les clés gardées se relisent à la prochaine vérification ; tant que
# l'émetteur ne répond pas, elles servent encore.
KEYS_TTL: Final = 3600.0
MAX_KEYS: Final = 64
MIN_RSA_BITS: Final = 2048
MAX_KID_LENGTH: Final = 256
# Valeurs de la revendication des rôles lues au plus, et leur longueur.
MAX_ROLE_VALUES: Final = 256
MAX_ROLE_VALUE_LENGTH: Final = 256
MAX_SETTING_LENGTH: Final = 1024

_CURVES: Final = {"EC": ("P-256",), "OKP": ("Ed25519", "Ed448")}
_KEY_TYPES: Final = {"RS256": "RSA", "ES256": "EC", "EdDSA": "OKP"}
_PRIVATE_MEMBERS: Final = ("d", "p", "q", "dp", "dq", "qi", "k")
_CLAIM_PATH: Final = re.compile(r"[A-Za-z0-9_:/-]{1,64}(\.[A-Za-z0-9_:/-]{1,64}){0,7}")
_PRINTABLE: Final = re.compile(r"[\x21-\x7e](?:[\x20-\x7e]*[\x21-\x7e])?")


class TokenRefusedError(Exception):
    """Le jeton est refusé : ``code`` est un code stable, jamais un extrait du jeton."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class OidcSettings:
    """L'émetteur cru, l'audience de l'API, et la lecture des rôles."""

    issuer: str
    audience: str
    # Chemin de la revendication des rôles, membre par membre : « realm_access.roles ».
    roles_claim: tuple[str, ...] = ("roles",)
    # Valeur de l'émetteur, et les rôles qu'elle donne ; sans table, une valeur qui est
    # le nom d'un rôle de personne donne ce rôle.
    role_map: Mapping[str, frozenset[Role]] | None = None
    # Identifiants des clients qui ont le rôle d'émetteur (azp ou client_id).
    emitter_clients: frozenset[str] = frozenset()
    # Le client public de l'explorateur, et la portée qu'il demande ; sans client,
    # l'explorateur ne se connecte pas.
    explorer_client_id: str | None = None
    explorer_scope: str = "openid"

    @classmethod
    def create(
        cls,
        issuer: str,
        audience: str,
        *,
        roles_claim: str = "roles",
        role_map: Mapping[str, Iterable[str]] | None = None,
        emitter_clients: Iterable[str] = (),
        explorer_client_id: str | None = None,
        explorer_scope: str = "openid",
    ) -> OidcSettings:
        """Contrôle les réglages. Un message nomme le réglage fautif, jamais sa valeur."""
        if len(issuer) > MAX_SETTING_LENGTH or not secure_url(issuer):
            raise ValueError(
                "L'émetteur OIDC est une adresse https, sans identifiant ni fragment (en http, "
                "sur la boucle locale seulement)."
            )
        for name, value in (
            ("l'audience", audience),
            ("le client de l'explorateur", explorer_client_id),
            ("la portée de l'explorateur", explorer_scope),
        ):
            if value is not None and (
                len(value) > MAX_SETTING_LENGTH or not _PRINTABLE.fullmatch(value)
            ):
                raise ValueError(f"Réglage OIDC illisible : {name}.")
        if not _CLAIM_PATH.fullmatch(roles_claim):
            raise ValueError(
                "La revendication des rôles est un nom, ou un chemin de noms séparés par des"
                " points (realm_access.roles)."
            )
        clients = frozenset(emitter_clients)
        if any(
            len(client) > MAX_SETTING_LENGTH or not _PRINTABLE.fullmatch(client)
            for client in clients
        ):
            raise ValueError("Réglage OIDC illisible : un client émetteur.")
        if explorer_client_id is not None and explorer_client_id in clients:
            raise ValueError(
                "Le client de l'explorateur ne peut pas être un émetteur : toute personne"
                " connectée en aurait le rôle."
            )
        if explorer_client_id is not None and explorer_client_id == audience:
            raise ValueError(
                "L'audience de l'API ne peut pas être le client de l'explorateur : le jeton"
                " d'identité de l'explorateur passerait pour un jeton d'accès."
            )
        return cls(
            # Tel que l'émetteur l'écrit dans « iss » : la comparaison est exacte.
            issuer=issuer,
            audience=audience,
            roles_claim=tuple(roles_claim.split(".")),
            role_map=None if role_map is None else _role_map(role_map),
            emitter_clients=clients,
            explorer_client_id=explorer_client_id,
            explorer_scope=explorer_scope,
        )


def _role_map(raw: Mapping[str, Iterable[str]]) -> dict[str, frozenset[Role]]:
    found: dict[str, frozenset[Role]] = {}
    known = {role.value for role in PERSON_ROLES}
    for value, roles in raw.items():
        names = [roles] if isinstance(roles, str) else list(roles)
        if (
            not isinstance(value, str)
            or not value
            or len(value) > MAX_ROLE_VALUE_LENGTH
            or not all(isinstance(name, str) and name in known for name in names)
        ):
            raise ValueError(
                "La table des rôles associe une valeur de l'émetteur à des rôles de personne : "
                + ", ".join(sorted(known))
                + " (l'émetteur se reconnaît à son client, pas à un rôle)."
            )
        found[value] = frozenset(Role(name) for name in names)
    return found


@dataclass
class KeySet:
    """Les clés publiques de l'émetteur, lues de sa découverte puis de son JWKS, en cache.

    Une seule lecture à la fois, au plus une par ``REFRESH_INTERVAL`` : un jeton au
    ``kid`` inconnu relit les clés si l'intervalle est passé, et sinon est refusé. Des
    clés gardées servent tant que l'émetteur ne répond pas.
    """

    issuer: str
    fetch: Fetch = fetch_document
    clock: Callable[[], float] = time.monotonic
    keys: dict[str, jwt.PyJWK] = field(default_factory=dict)
    loaded_at: float | None = None
    # Lectures de l'émetteur, réussies ou non : un nombre, pour les tests et le diagnostic.
    reads: int = 0
    _next_read: float = 0.0
    _jwks_uri: str | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def key(self, kid: str) -> jwt.PyJWK | None:
        """La clé de ce ``kid`` ; ``None`` si l'émetteur ne la publie pas.

        ``IssuerUnavailableError`` quand aucune clé n'a jamais pu être lue."""
        found = self.keys.get(kid)
        if found is not None and not self._stale():
            return found
        async with self._lock:
            found = self.keys.get(kid)
            if found is not None and not self._stale():
                return found
            if self.clock() >= self._next_read:
                await self._refresh()
            if self.loaded_at is None:
                raise IssuerUnavailableError("aucune clé lue")
            return self.keys.get(kid)

    def _stale(self) -> bool:
        return self.loaded_at is None or self.clock() - self.loaded_at > KEYS_TTL

    async def _refresh(self) -> None:
        self._next_read = self.clock() + REFRESH_INTERVAL
        self.reads += 1
        try:
            if self._jwks_uri is None:
                discovery = await self.fetch(self.issuer.rstrip("/") + DISCOVERY_PATH)
                self._jwks_uri = _jwks_uri(discovery, self.issuer)
            keys = _keys(await self.fetch(self._jwks_uri))
        except IssuerUnavailableError:
            # La découverte se refait à la lecture suivante : l'émetteur a pu changer.
            self._jwks_uri = None
            if self.loaded_at is None:
                raise
            return
        self.keys = keys
        self.loaded_at = self.clock()


def _document(body: bytes) -> Mapping[str, Any]:
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise IssuerUnavailableError("document illisible") from None
    if not isinstance(document, dict):
        raise IssuerUnavailableError("document illisible")
    return document


def _jwks_uri(body: bytes, issuer: str) -> str:
    """L'adresse du JWKS, d'une découverte qui nomme bien l'émetteur réglé."""
    document = _document(body)
    if document.get("issuer") != issuer:
        raise IssuerUnavailableError("découverte d'un autre émetteur")
    uri = document.get("jwks_uri")
    if not isinstance(uri, str) or len(uri) > MAX_SETTING_LENGTH or not secure_url(uri):
        raise IssuerUnavailableError("adresse du JWKS refusée")
    return uri


def _usable(raw: object) -> jwt.PyJWK | None:
    """Une clé publique de signature, d'un type et d'une taille admis ; ``None`` sinon."""
    if not isinstance(raw, dict):
        return None
    kid, kty, use, alg = raw.get("kid"), raw.get("kty"), raw.get("use"), raw.get("alg")
    if not isinstance(kid, str) or not 0 < len(kid) <= MAX_KID_LENGTH:
        return None
    if kty not in ("RSA", "EC", "OKP") or use not in (None, "sig"):
        return None
    if alg is not None and alg not in ALGORITHMS:
        return None
    if any(member in raw for member in _PRIVATE_MEMBERS):
        return None
    if kty in _CURVES and raw.get("crv") not in _CURVES[kty]:
        return None
    try:
        key = jwt.PyJWK(raw)
    except (jwt.PyJWKError, jwt.InvalidKeyError, ValueError, TypeError, KeyError):
        return None
    if key.algorithm_name not in ALGORITHMS:
        return None
    if kty == "RSA" and getattr(key.key, "key_size", 0) < MIN_RSA_BITS:
        return None
    return key


def _keys(body: bytes) -> dict[str, jwt.PyJWK]:
    """Les clés admises du JWKS, par ``kid`` ; un ``kid`` en double n'est pas cru."""
    listed = _document(body).get("keys")
    if not isinstance(listed, list) or len(listed) > MAX_KEYS:
        raise IssuerUnavailableError("JWKS illisible")
    found: dict[str, jwt.PyJWK] = {}
    doubled: set[str] = set()
    for raw in listed:
        key = _usable(raw)
        if key is None:
            continue
        kid = raw["kid"]
        if kid in found:
            doubled.add(kid)
        found[kid] = key
    return {kid: key for kid, key in found.items() if kid not in doubled}


def _claim(claims: Mapping[str, Any], path: tuple[str, ...]) -> object:
    value: object = claims
    for name in path:
        value = value.get(name) if isinstance(value, dict) else None
    return value


def _values(claim: object) -> list[str]:
    """Les valeurs d'une revendication de rôles : une liste de textes, ou un texte."""
    items = [claim] if isinstance(claim, str) else claim if isinstance(claim, list) else []
    return [
        item
        for item in items[:MAX_ROLE_VALUES]
        if isinstance(item, str) and 0 < len(item) <= MAX_ROLE_VALUE_LENGTH
    ]


class OidcVerifier:
    """Vérifie un jeton de l'émetteur réglé et rend les rôles de son porteur."""

    def __init__(
        self,
        settings: OidcSettings,
        *,
        fetch: Fetch = fetch_document,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.keys = KeySet(settings.issuer, fetch, clock)

    async def roles(self, token: str) -> frozenset[Role]:
        """Les rôles du porteur ; ``TokenRefusedError`` pour un jeton refusé,
        ``IssuerUnavailableError`` quand aucune clé de l'émetteur n'est connue."""
        if token.count(".") != 2:
            raise TokenRefusedError("invalid_token")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise TokenRefusedError("invalid_token") from None
        algorithm, kid = header.get("alg"), header.get("kid")
        if algorithm not in ALGORITHMS or "crit" in header:
            raise TokenRefusedError("invalid_token")
        if not isinstance(kid, str) or not 0 < len(kid) <= MAX_KID_LENGTH:
            raise TokenRefusedError("invalid_token")
        key = await self.keys.key(kid)
        if key is None or key.algorithm_name != algorithm:
            raise TokenRefusedError("invalid_token")
        try:
            claims = jwt.decode(
                token,
                key.key,
                algorithms=[algorithm],
                audience=self.settings.audience,
                issuer=self.settings.issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError:
            raise TokenRefusedError("expired_token") from None
        except (jwt.PyJWTError, ValueError, TypeError):
            raise TokenRefusedError("invalid_token") from None
        return self._roles_of(claims)

    def _roles_of(self, claims: Mapping[str, Any]) -> frozenset[Role]:
        values = _values(_claim(claims, self.settings.roles_claim))
        table = self.settings.role_map
        roles: set[Role] = set()
        for value in values:
            if table is not None:
                roles.update(table.get(value, ()))
            elif value in {role.value for role in PERSON_ROLES}:
                roles.add(Role(value))
        client = claims.get("azp", claims.get("client_id"))
        if isinstance(client, str) and client in self.settings.emitter_clients:
            roles.add(Role.EMITTER)
        return frozenset(roles)
