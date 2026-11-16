"""Explorateur : les fichiers statiques de l'interface, servis par l'API.

L'interface est construite à part (``ui/``). L'API sert le dossier construit
tel quel, sous une politique de sécurité du contenu qui n'autorise que ses
propres fichiers : aucun script ni style en ligne, aucune ressource tierce.
L'application pose cette politique sur toute réponse (``headers.py``) ; les
fichiers n'ajoutent que leur durée de conservation.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

ENV_UI_DIRECTORY = "ANSWERLINEAGE_UI_DIR"

# Les fichiers construits portent une empreinte dans leur nom : ils ne changent jamais.
_ASSET_CACHE = "public, max-age=31536000, immutable"


class ExplorerFiles(StaticFiles):
    """Fichiers de l'explorateur : les fichiers à empreinte sont gardés, la page jamais."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        asset = Path(path).parts[:1] == ("assets",)
        response.headers["cache-control"] = _ASSET_CACHE if asset else "no-cache"
        return response


def explorer_files(
    directory: str | os.PathLike[str] | None, environment: Mapping[str, str]
) -> ExplorerFiles | None:
    """Le dossier construit de l'explorateur, celui de l'appelant ou de l'environnement."""
    chosen = directory if directory is not None else environment.get(ENV_UI_DIRECTORY)
    if not chosen:
        return None
    if not (Path(chosen) / "index.html").is_file():
        raise ValueError(
            f"Le dossier de l'explorateur ne contient pas index.html ({ENV_UI_DIRECTORY})."
        )
    return ExplorerFiles(directory=chosen, html=True)
