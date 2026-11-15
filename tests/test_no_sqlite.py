"""L'image d'AnswerLineage n'embarque pas SQLite : aucun service ne doit en avoir besoin.

``deploy/Dockerfile`` retire le module ``sqlite3`` de Python et la bibliothèque
SQLite du système, pour ne pas avoir à suivre leurs vulnérabilités. Un service
qui se mettrait à charger ``sqlite3``, lui-même ou par une dépendance,
fonctionnerait sur le poste et tomberait dans l'image : ce test le dit avant.
"""

from __future__ import annotations

import os
import subprocess
import sys

# Ce que lancent les services de l'image et leurs commandes.
ENTRY_POINTS = [
    "answerlineage_api.app",
    "answerlineage_api.store.migrate",
    "answerlineage_workers.cli",
    "answerlineage_proxy.cli",
    "answerlineage_proxy.analyzer",
    "answerlineage_corpus.cli",
    "answerlineage_conformance.cli",
    "alembic.command",
    "uvicorn",
]

_LOADED = """
import importlib, sys
for name in sys.argv[1:]:
    importlib.import_module(name)
print(sorted(name for name in sys.modules if name in ("sqlite3", "_sqlite3")))
"""


def test_no_service_loads_sqlite() -> None:
    # Un interpréteur neuf, sans la mesure de couverture, qui se sert elle-même de SQLite.
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("COV_", "COVERAGE_"))
    }
    loaded = subprocess.run(  # noqa: S603 — l'interpréteur courant, des noms de modules fixes.
        [sys.executable, "-c", _LOADED, *ENTRY_POINTS],
        capture_output=True,
        text=True,
        check=True,
        env=environment,
    )
    assert loaded.stdout.strip() == "[]"
