"""Réglages communs aux émetteurs : l'erreur d'un réglage, la lecture d'une clé.

Un réglage absent ou invalide arrête l'émetteur au démarrage, avec un message
qui nomme le réglage et jamais sa valeur : une clé ou un secret peut avoir été
donné par erreur à la place d'un autre réglage.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Final

# Fichier d'une clé lu au plus : une clé en hexadécimal, ses blancs et sa fin de ligne.
KEY_FILE_BYTES: Final = 4096


class ConfigurationError(ValueError):
    """Un réglage d'un émetteur est absent ou invalide."""


def key_text(
    environment: Mapping[str, str],
    key_file: str | None = None,
    *,
    variable: str,
    file_variable: str,
    what: str,
) -> str | None:
    """Le texte d'une clé : dans un fichier (``key_file``, sinon celui que nomme la variable
    ``file_variable``) ou dans la variable ``variable``, pas des deux. ``None`` sans clé.

    ``what`` nomme la clé dans les messages (« la clé d'empreinte »). Aucun message ne
    cite la clé, ni le chemin du fichier, qui pourrait être une clé passée par erreur.
    """
    path = key_file or environment.get(file_variable) or None
    text = environment.get(variable) or None
    if path is not None and text is not None:
        raise ConfigurationError(
            f"{what[:1].upper()}{what[1:]} vient d'un fichier ou de {variable}, pas des deux."
        )
    if path is None:
        return text
    try:
        with Path(path).open("rb") as source:
            content = source.read(KEY_FILE_BYTES + 1)
    except OSError:
        raise ConfigurationError(f"Le fichier de {what} est illisible.") from None
    if len(content) > KEY_FILE_BYTES:
        raise ConfigurationError(f"Le fichier de {what} est trop grand.")
    return content.decode("ascii", errors="replace")
