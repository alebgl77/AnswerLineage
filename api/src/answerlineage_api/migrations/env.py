"""Environnement Alembic : l'URL est fournie par ``answerlineage_api.store.migrate``."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

config = context.config


def run_migrations_online() -> None:
    engine = create_engine(
        config.attributes["database_url"], poolclass=pool.NullPool, hide_parameters=True
    )
    try:
        with engine.connect() as connection:
            # Pas de target_metadata : les migrations nomment leurs contraintes
            # elles-mêmes, sans convention appliquée par-dessus.
            context.configure(connection=connection, transaction_per_migration=True)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Le mode hors ligne d'Alembic n'est pas pris en charge.")
run_migrations_online()
