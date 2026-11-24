"""Lecture du début d'un message JSON-RPC.

Un corps trop grand pour être lu en entier est relayé en flux ; le proxy n'en
garde que le début. Ce module y cherche la méthode (``method``, au premier
niveau) et le nom de l'outil (``params.name``), qui précèdent d'ordinaire les
arguments volumineux. Dans le début d'une réponse, il cherche de même une
erreur (``error``, au premier niveau) et son code (``error.code``), ou la place
où un bloc s'insère en tête du contenu d'un résultat d'outil
(``result.content``).

Le lecteur est strict : il ne rend une valeur que s'il en est certain. Il saute
les autres valeurs (chaînes, nombres, objets et tableaux imbriqués) sans les
interpréter, et s'arrête sans conclure dès que le préfixe s'achève ou s'écarte
de la syntaxe JSON. Une clé vue deux fois rend sa valeur incertaine : selon
l'analyseur, c'est la première ou la dernière qui compte. Une clé en double
au-delà du préfixe, en revanche, ne peut pas être vue.

Le temps de lecture est linéaire : les chaînes et les suites sans crochet ni
accolade sont sautées par des expressions sans retour en arrière.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

# Début gardé d'un message trop grand pour être lu en entier : les clients et les
# serveurs MCP y écrivent d'ordinaire la méthode, le nom de l'outil et l'identifiant.
PREFIX_BYTES = 16 * 1024

_WHITESPACE = re.compile(rb"[ \t\n\r]*+")
_STRING = re.compile(rb'"[^"\\]*+(?:\\[\s\S][^"\\]*+)*+"')
# Nombre, true, false, null : tout ce qui n'est ni séparateur ni délimiteur.
_SCALAR = re.compile(rb'[^ \t\n\r\[\]{},:"]++')
# Dans une valeur sautée : chaînes complètes et caractères hors crochets et accolades.
_FLAT = re.compile(rb'(?:[^"\[\]{}]++|"[^"\\]*+(?:\\[\s\S][^"\\]*+)*+")*+')
_OPENERS = re.compile(rb"[\[{]++")
_CLOSERS = re.compile(rb"[\]}]++")
_QUOTE = ord('"')
# L'ouvrant qui répond à chaque fermant.
_OPENER_OF = {ord("]"): ord("["), ord("}"): ord("{")}
# Un entier JSON : le code d'une erreur JSON-RPC.
_INTEGER = re.compile(rb"-?(?:0|[1-9][0-9]{0,18})")
# Ce qui peut suivre le « [ » d'un tableau JSON : le début d'une valeur, ou sa fin.
_ARRAY_STARTS = frozenset(b'{["-0123456789tfn]')

# Reçoit une clé et la position de sa valeur ; rend la position qui suit la valeur.
_OnMember = Callable[[str, int], int | None]


@dataclass(frozen=True)
class CallPrefix:
    """Ce que le début d'un message établit avec certitude."""

    method: str | None = None
    # ``params.name`` tel qu'écrit, sans contrôle de forme.
    name: str | None = None


@dataclass(frozen=True)
class ReplyPrefix:
    """Ce que le début d'une réponse établit avec certitude : une erreur, et son code."""

    error: bool = False
    # ``error.code`` quand c'est un entier écrit sans fraction ni exposant.
    code: int | None = None


@dataclass(frozen=True)
class MessagePrefix:
    """Ce que le début d'un message établit sur sa nature et son identifiant."""

    # ``id`` au premier niveau, vu une seule fois : une chaîne, ou un entier écrit
    # sans fraction ni exposant. Jamais affiché : il est choisi par l'émetteur du message.
    identifier: str | int | None = field(default=None, repr=False)
    # Membres du premier niveau vus dans le début : ``method`` fait une requête ou une
    # notification, ``result`` ou ``error`` une réponse.
    method: bool = False
    reply: bool = False


# Pourquoi rien ne s'insère dans un résultat : une erreur JSON-RPC ; un résultat sans tableau
# ``content`` (absent, autre chose qu'un tableau, résultat d'un autre type que ``complete``) ;
# un début qui ne se lit pas entier et sans ambiguïté (syntaxe, clé répétée, autre identifiant).
Refusal = Literal["error_response", "no_content", "not_whole"]


@dataclass(frozen=True)
class ResultPrefix:
    """Ce que le début d'une réponse dit de la place d'un bloc en tête de ``result.content``.

    ``found`` : la place est ``position``, juste après le ``[`` du tableau, vide ou non.
    ``refused`` : rien ne s'insère, pour ``reason``. ``not_reply`` : le message est une
    requête ou une notification. ``pending`` : le début s'achève avant de conclure.
    """

    state: Literal["found", "refused", "not_reply", "pending"]
    position: int | None = None
    empty: bool = False
    reason: Refusal | None = None


class _Concluded(Exception):  # noqa: N818 — un arrêt de la lecture, pas une erreur.
    """La lecture conclut avant la fin du document : elle s'arrête là."""

    def __init__(self, prefix: ResultPrefix) -> None:
        super().__init__(prefix.state)
        self.prefix = prefix


def _refused(reason: Refusal) -> _Concluded:
    return _Concluded(ResultPrefix("refused", reason=reason))


class _MalformedError(Exception):
    """Le préfixe s'écarte de la syntaxe JSON : le document entier serait illisible."""


def _end(pattern: re.Pattern[bytes], data: bytes, pos: int) -> int:
    """Fin d'une suite qui peut être vide : elle se trouve toujours."""
    match = pattern.match(data, pos)
    return match.end() if match is not None else pos


def _blank(data: bytes, pos: int) -> int:
    return _end(_WHITESPACE, data, pos)


def _at(data: bytes, pos: int, token: bytes) -> bool:
    """Vrai si ``token`` est en ``pos`` ; faux si le préfixe s'achève avant."""
    if data[pos : pos + 1] == token:
        return True
    if pos < len(data):
        raise _MalformedError
    return False


def _string(data: bytes, pos: int) -> tuple[str | None, int | None]:
    """Décode la chaîne qui commence en ``pos``. ``(None, None)`` si elle est incomplète."""
    match = _STRING.match(data, pos)
    if match is None:
        return None, None
    try:
        value = json.loads(match.group())
    except ValueError as error:
        # Échappement invalide, caractère de contrôle, UTF-8 mal formé.
        raise _MalformedError from error
    return value, match.end()


def _container(data: bytes, pos: int) -> int | None:
    """Saute l'objet ou le tableau qui commence en ``pos``.

    Chaque crochet ou accolade fermant doit répondre au dernier ouvert : un ``]``
    qui fermerait un ``{`` écarte le préfixe de la syntaxe JSON.
    """
    opened: list[int] = []
    while True:
        openers = _OPENERS.match(data, pos)
        if openers is not None:
            opened.extend(data[pos : openers.end()])
            pos = openers.end()
        pos = _end(_FLAT, data, pos)
        closers = _CLOSERS.match(data, pos)
        if closers is not None:
            for index in range(pos, closers.end()):
                if opened.pop() != _OPENER_OF[data[index]]:
                    raise _MalformedError
                if not opened:
                    return index + 1
            pos = closers.end()
        elif pos >= len(data) or data[pos] == _QUOTE:
            # Fin du préfixe, ou chaîne inachevée.
            return None


def _skip(data: bytes, pos: int) -> int | None:
    """Saute la valeur qui commence en ``pos`` ; ``None`` si le préfixe s'achève avant sa fin."""
    first = data[pos : pos + 1]
    if first == b'"':
        return _string(data, pos)[1]
    if first in (b"[", b"{"):
        return _container(data, pos)
    scalar = _SCALAR.match(data, pos)
    if scalar is None:
        if pos < len(data):
            raise _MalformedError
        return None
    # Un nombre au bord du préfixe peut continuer au-delà.
    return scalar.end() if scalar.end() < len(data) else None


def _text(data: bytes, pos: int) -> tuple[str | None, int | None]:
    """Valeur en ``pos`` si c'est une chaîne ; sinon ``None``, et la valeur est sautée."""
    if data[pos : pos + 1] == b'"':
        return _string(data, pos)
    return None, _skip(data, pos)


def _object(data: bytes, pos: int, on_member: _OnMember) -> int | None:
    """Parcourt les membres de l'objet qui commence en ``pos``."""
    pos = _blank(data, pos + 1)
    if data[pos : pos + 1] == b"}":
        return pos + 1
    while True:
        if not _at(data, pos, b'"'):
            return None
        key, end = _string(data, pos)
        if key is None or end is None:
            return None
        pos = _blank(data, end)
        if not _at(data, pos, b":"):
            return None
        end = on_member(key, _blank(data, pos + 1))
        if end is None:
            return None
        pos = _blank(data, end)
        if data[pos : pos + 1] == b"}":
            return pos + 1
        if not _at(data, pos, b","):
            return None
        pos = _blank(data, pos + 1)


def read_call_prefix(data: bytes) -> CallPrefix:
    """Lit ``method`` et ``params.name`` au début d'un message JSON-RPC."""
    counts = {"method": 0, "params": 0, "name": 0}
    found: dict[str, str | None] = {"method": None, "name": None}

    def on_param(key: str, pos: int) -> int | None:
        if key != "name":
            return _skip(data, pos)
        counts["name"] += 1
        found["name"], end = _text(data, pos)
        return end

    def on_member(key: str, pos: int) -> int | None:
        if key == "method":
            counts["method"] += 1
            found["method"], end = _text(data, pos)
            return end
        if key == "params":
            counts["params"] += 1
            if data[pos : pos + 1] == b"{":
                return _object(data, pos, on_param)
        return _skip(data, pos)

    start = _blank(data, 0)
    if data[start : start + 1] != b"{":
        return CallPrefix()
    try:
        _object(data, start, on_member)
    except _MalformedError:
        # Un document illisible n'établit rien, pas même ce qui précède l'erreur.
        return CallPrefix()
    certain_name = counts["params"] == 1 and counts["name"] == 1
    return CallPrefix(
        method=found["method"] if counts["method"] == 1 else None,
        name=found["name"] if certain_name else None,
    )


def read_reply_prefix(data: bytes) -> ReplyPrefix:
    """Lit ``error`` et ``error.code`` au début d'une réponse JSON-RPC.

    L'erreur n'est établie que si la clé ``error`` est vue une seule fois au
    premier niveau, avec une valeur autre que ``null`` ; son code, que si
    ``code`` y est vu une seule fois, écrit comme un entier, en entier dans le
    préfixe. Le code n'est pas borné ici : c'est à l'appelant de le faire.
    """
    counts = {"error": 0, "code": 0}
    found: dict[str, int | None] = {"code": None}
    present = [False]

    def on_error(key: str, pos: int) -> int | None:
        end = _skip(data, pos)
        if key == "code":
            counts["code"] += 1
            number = _INTEGER.fullmatch(data, pos, end) if end is not None else None
            found["code"] = int(number.group()) if number is not None else None
        return end

    def on_member(key: str, pos: int) -> int | None:
        if key != "error":
            return _skip(data, pos)
        counts["error"] += 1
        if data[pos : pos + 1] == b"{":
            present[0] = True
            return _object(data, pos, on_error)
        end = _skip(data, pos)
        present[0] = end is not None and data[pos:end] != b"null"
        return end

    start = _blank(data, 0)
    if data[start : start + 1] != b"{":
        return ReplyPrefix()
    try:
        _object(data, start, on_member)
    except _MalformedError:
        return ReplyPrefix()
    if counts["error"] != 1 or not present[0]:
        return ReplyPrefix()
    return ReplyPrefix(error=True, code=found["code"] if counts["code"] == 1 else None)


def read_message_prefix(data: bytes) -> MessagePrefix:
    """Lit, au début d'un message JSON-RPC, son identifiant et sa nature.

    L'identifiant n'est établi que si la clé ``id`` est vue une seule fois au
    premier niveau, avec pour valeur une chaîne, ou un entier écrit en entier dans
    le début. Un début illisible n'établit rien.
    """
    counts = {"id": 0}
    found: dict[str, str | int | None] = {"id": None}
    seen = {"method": False, "reply": False}

    def on_member(key: str, pos: int) -> int | None:
        if key == "method":
            seen["method"] = True
        elif key in ("result", "error"):
            seen["reply"] = True
        elif key == "id":
            counts["id"] += 1
            if data[pos : pos + 1] == b'"':
                found["id"], end = _string(data, pos)
                return end
            end = _skip(data, pos)
            number = _INTEGER.fullmatch(data, pos, end) if end is not None else None
            found["id"] = int(number.group()) if number is not None else None
            return end
        return _skip(data, pos)

    start = _blank(data, 0)
    if data[start : start + 1] != b"{":
        return MessagePrefix()
    try:
        _object(data, start, on_member)
    except _MalformedError:
        return MessagePrefix()
    return MessagePrefix(
        identifier=found["id"] if counts["id"] == 1 else None,
        method=seen["method"],
        reply=seen["reply"],
    )


def _same_identifier(data: bytes, pos: int, expected: str | int) -> int | None:
    """Saute l'identifiant en ``pos`` ; refuse la réponse s'il diffère de ``expected``, de
    valeur ou de type, ou s'il n'est ni une chaîne ni un entier écrit en entier."""
    if data[pos : pos + 1] == b'"':
        value, end = _string(data, pos)
        if end is not None and (not isinstance(expected, str) or value != expected):
            raise _refused("not_whole")
        return end
    end = _skip(data, pos)
    if end is None:
        return None
    number = _INTEGER.fullmatch(data, pos, end)
    if number is None or isinstance(expected, str) or int(number.group()) != expected:
        raise _refused("not_whole")
    return end


def read_result_prefix(data: bytes, identifier: str | int | None = None) -> ResultPrefix:
    """Cherche, au début d'une réponse JSON-RPC, la place d'un bloc en tête de ``result.content``.

    Le lecteur est strict. Il refuse un début qui s'écarte de la syntaxe JSON, une clé
    répétée au premier niveau ou dans ``result`` avant ``content``, une erreur, un
    ``resultType`` autre que ``complete`` vu avant ``content``, et, quand
    ``identifier`` est donné, un ``id`` qui en diffère. Seul ``content`` au premier
    niveau de ``result`` compte : un ``content`` imbriqué, dans ``structuredContent``
    par exemple, est sauté sans être lu. Ce qui suit la place trouvée n'est pas lu :
    une clé répétée au-delà ne peut pas être vue.
    """
    top: set[str] = set()
    inner: set[str] = set()

    def on_content(pos: int) -> int | None:
        if pos >= len(data):
            return None
        if data[pos] != ord("["):
            raise _refused("no_content")
        after = _blank(data, pos + 1)
        if after >= len(data):
            return None
        if data[after] not in _ARRAY_STARTS:
            raise _MalformedError
        empty = data[after] == ord("]")
        raise _Concluded(ResultPrefix("found", position=pos + 1, empty=empty))

    def on_result(key: str, pos: int) -> int | None:
        if key in inner:
            raise _refused("not_whole")
        inner.add(key)
        if key == "content":
            return on_content(pos)
        if key == "resultType":
            value, end = _text(data, pos)
            if end is not None and value != "complete":
                raise _refused("no_content")
            return end
        return _skip(data, pos)

    def on_member(key: str, pos: int) -> int | None:
        if key in top:
            raise _refused("not_whole")
        top.add(key)
        if key == "method":
            raise _Concluded(ResultPrefix("not_reply"))
        if key == "id" and identifier is not None:
            return _same_identifier(data, pos, identifier)
        if key == "error":
            end = _skip(data, pos)
            if end is not None and data[pos:end] != b"null":
                raise _refused("error_response")
            return end
        if key == "result":
            if pos >= len(data):
                return None
            if data[pos] != ord("{"):
                raise _refused("no_content")
            if _object(data, pos, on_result) is not None:
                # Le résultat s'est refermé sans tableau ``content``.
                raise _refused("no_content")
            return None
        return _skip(data, pos)

    start = _blank(data, 0)
    if start >= len(data):
        return ResultPrefix("pending")
    if data[start] != ord("{"):
        # Un lot, ou autre chose qu'un objet : pas une réponse unique.
        return ResultPrefix("refused", reason="not_whole")
    try:
        end = _object(data, start, on_member)
    except _Concluded as concluded:
        return concluded.prefix
    except _MalformedError:
        return ResultPrefix("refused", reason="not_whole")
    # Le message lu en entier n'a ni résultat, ni erreur, ni méthode.
    return ResultPrefix("pending") if end is None else ResultPrefix("not_reply")
