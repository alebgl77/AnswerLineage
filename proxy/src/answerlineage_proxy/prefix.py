"""Lecture du début d'un message JSON-RPC.

Un corps trop grand pour être lu en entier est relayé en flux ; le proxy n'en
garde que le début. Ce module y cherche la méthode (``method``, au premier
niveau) et le nom de l'outil (``params.name``), qui précèdent d'ordinaire les
arguments volumineux.

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
from dataclasses import dataclass

_WHITESPACE = re.compile(rb"[ \t\n\r]*+")
_STRING = re.compile(rb'"[^"\\]*+(?:\\[\s\S][^"\\]*+)*+"')
# Nombre, true, false, null : tout ce qui n'est ni séparateur ni délimiteur.
_SCALAR = re.compile(rb'[^ \t\n\r\[\]{},:"]++')
# Dans une valeur sautée : chaînes complètes et caractères hors crochets et accolades.
_FLAT = re.compile(rb'(?:[^"\[\]{}]++|"[^"\\]*+(?:\\[\s\S][^"\\]*+)*+")*+')
_OPENERS = re.compile(rb"[\[{]++")
_CLOSERS = re.compile(rb"[\]}]++")
_QUOTE = ord('"')

# Reçoit une clé et la position de sa valeur ; rend la position qui suit la valeur.
_OnMember = Callable[[str, int], int | None]


@dataclass(frozen=True)
class CallPrefix:
    """Ce que le début d'un message établit avec certitude."""

    method: str | None = None
    # ``params.name`` tel qu'écrit, sans contrôle de forme.
    name: str | None = None


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
    """Saute l'objet ou le tableau qui commence en ``pos``."""
    depth = 0
    while True:
        openers = _OPENERS.match(data, pos)
        if openers is not None:
            depth += openers.end() - pos
            pos = openers.end()
        pos = _end(_FLAT, data, pos)
        closers = _CLOSERS.match(data, pos)
        if closers is not None:
            count = closers.end() - pos
            if count >= depth:
                return pos + depth
            depth -= count
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
