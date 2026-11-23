"""Spécification OpenAPI de l'API, telle qu'elle est publiée dans ``spec/openapi/``.

Usage : ``python -m answerlineage_api.openapi`` écrit la spécification sur la
sortie standard. ``make openapi`` met à jour le fichier publié.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from answerlineage_api.app import create_app
from answerlineage_api.auth.access import Authentication


def specification() -> dict[str, Any]:
    """Spécification OpenAPI de l'application, sans connexion à la base.

    Elle ne dépend d'aucun réglage du déploiement : la sécurité qu'elle publie est
    celle de la matrice des droits, quel que soit l'émetteur."""
    app = create_app(allowed_hosts=[], allowed_origins=[], authentication=Authentication.disabled())
    return app.openapi()


def render() -> str:
    """Forme publiée : JSON indenté, clés triées, pour des différences lisibles."""
    return json.dumps(specification(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    sys.stdout.write(render())
