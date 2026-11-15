"""Fichiers d'une capture : événements, canaris, témoin, journaux, métriques.

Chaque fichier est lu une fois, en entier et dans une taille bornée. Le rapport
en garde la taille et l'empreinte SHA-256 : elles disent sur quels octets le
verdict a été rendu.

Un fichier illisible n'est jamais cité : le motif d'un refus est un texte fixe,
qui ne recopie rien de ce que le fichier contient.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

FileRole = Literal["events", "canaries", "witness", "logs", "metrics"]

# Une demande jouée tient en quelques kilo-octets : la borne arrête un fichier pris pour un autre.
MAX_FILE_BYTES: Final = 256 * 1024 * 1024

# Un fichier compressé ne se fouille pas : le canari y serait invisible, et le verdict faux.
_COMPRESSED: Final = (
    b"\x1f\x8b",  # gzip
    b"BZh",  # bzip2
    b"\xfd7zXZ\x00",  # xz
    b"\x28\xb5\x2f\xfd",  # zstd
    b"PK\x03\x04",  # zip
)
# Un nom de fichier vient de la ligne de commande : il n'est affiché que s'il est anodin.
_PRINTABLE: Final = re.compile(r"[\w./ @+=,:~\\-]{1,300}")
UNPRINTABLE: Final = "<nom de fichier non affichable>"
_AMBIGUOUS: Final = "{path} : JSON ambigu, une clé figure deux fois dans un objet."


class InputError(Exception):
    """Une entrée est illisible : la commande s'arrête sans rendre de verdict."""


@dataclass(frozen=True)
class SourceFile:
    """Un fichier de la capture, avec ce que le rapport en retient."""

    role: FileRole
    # Chemin tel qu'il a été donné, ou ``UNPRINTABLE``.
    path: str
    size: int
    sha256: str
    data: bytes = field(repr=False, compare=False)


def printable(path: str) -> str:
    return path if _PRINTABLE.fullmatch(path) else UNPRINTABLE


def source(role: FileRole, path: str, data: bytes) -> SourceFile:
    return SourceFile(role, printable(path), len(data), hashlib.sha256(data).hexdigest(), data)


def read(role: FileRole, path: Path) -> SourceFile:
    """Lit un fichier de la capture. Lève ``InputError`` s'il ne peut pas être fouillé."""
    shown = printable(str(path))
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        raise InputError(f"{shown} : fichier introuvable.") from None
    except OSError as error:
        raise InputError(f"{shown} : fichier illisible ({type(error).__name__}).") from None
    if len(data) > MAX_FILE_BYTES:
        raise InputError(f"{shown} : fichier trop volumineux (plus de {MAX_FILE_BYTES} octets).")
    if data.startswith(_COMPRESSED):
        raise InputError(f"{shown} : fichier compressé, à décompresser avant de le fouiller.")
    return source(role, str(path), data)


class _DuplicateKeyError(ValueError):
    """Une clé figure deux fois dans un objet : ce que l'objet veut dire dépend du lecteur."""


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document = dict(pairs)
    if len(document) != len(pairs):
        raise _DuplicateKeyError
    return document


def _not_a_number(_: str) -> Any:
    raise ValueError("NaN et Infinity ne sont pas du JSON")


def _loads(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_unique_keys, parse_constant=_not_a_number)


def _text(file: SourceFile) -> str:
    try:
        # UTF-8, ou UTF-16 et UTF-32 reconnus comme JSON les reconnaît.
        return file.data.decode(json.detect_encoding(file.data))
    except UnicodeDecodeError:
        raise InputError(f"{file.path} : le texte n'est pas de l'UTF-8.") from None


def parse_json(file: SourceFile) -> Any:
    """Le document JSON d'un fichier, lu strictement. Lève ``InputError`` s'il est illisible."""
    try:
        return _loads(_text(file))
    except _DuplicateKeyError:
        raise InputError(_AMBIGUOUS.format(path=file.path)) from None
    except (ValueError, RecursionError):
        raise InputError(f"{file.path} : JSON illisible.") from None


def parse_events(file: SourceFile) -> list[Any]:
    """Les événements d'une capture : un tableau JSON, ou un événement par ligne.

    Le JSON est lu comme l'API d'ingestion le lit : ``NaN`` et ``Infinity`` sont
    refusés. Une clé en double l'est aussi : un lecteur garderait la première
    valeur, un autre la dernière, et un contenu pourrait se cacher dans celle
    que le kit ne verrait pas.
    """
    text = _text(file)
    if not text.strip():
        return []
    try:
        document = _loads(text)
    except _DuplicateKeyError:
        raise InputError(_AMBIGUOUS.format(path=file.path)) from None
    except (ValueError, RecursionError):
        return _lines(file, text)
    return document if isinstance(document, list) else [document]


def _lines(file: SourceFile, text: str) -> list[Any]:
    events = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            events.append(_loads(line))
        except _DuplicateKeyError:
            raise InputError(_AMBIGUOUS.format(path=file.path)) from None
        except (ValueError, RecursionError):
            raise InputError(f"{file.path} : JSON illisible, ligne {number}.") from None
    return events


def searchable_text(file: SourceFile) -> str:
    """Le texte d'un journal, d'un relevé de métriques ou d'un témoin, prêt à être fouillé.

    Chaque octet devient un caractère (latin-1) : un texte ASCII s'y retrouve tel
    quel, quel que soit l'encodage du reste. Les octets nuls sont retirés, pour
    qu'un fichier en UTF-16 se fouille comme un autre.
    """
    return file.data.decode("latin-1").replace("\x00", "")


def text_lines(file: SourceFile) -> list[str]:
    """Les lignes du même texte, dans l'ordre : la première porte le numéro 1."""
    return searchable_text(file).split("\n")
