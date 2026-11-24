"""Migrations du schéma, pilotées par Alembic.

Usage : ``python -m answerlineage_api.store.migrate upgrade [révision]``
ou ``downgrade <révision>``. L'URL vient de ``ANSWERLINEAGE_DATABASE_URL``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL

from answerlineage_api.store.engine import database_url_from_environment, normalize_url

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def alembic_config(url: str | URL) -> Config:
    resolved = normalize_url(url) if isinstance(url, str) else url
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.attributes["database_url"] = resolved
    return config


def upgrade(url: str | URL, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str | URL, revision: str) -> None:
    command.downgrade(alembic_config(url), revision)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrations du schéma AnswerLineage")
    actions = parser.add_subparsers(dest="action", required=True)
    up = actions.add_parser("upgrade", help="applique les migrations montantes")
    up.add_argument("revision", nargs="?", default="head")
    down = actions.add_parser("downgrade", help="applique les migrations descendantes")
    down.add_argument("revision", help="révision cible, ou base pour tout retirer")
    args = parser.parse_args(argv)

    url = database_url_from_environment()
    if args.action == "upgrade":
        upgrade(url, args.revision)
    else:
        downgrade(url, args.revision)
    return 0


if __name__ == "__main__":
    sys.exit(main())
