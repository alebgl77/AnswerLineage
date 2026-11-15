"""Connexion à PostgreSQL."""

from __future__ import annotations

import os

from sqlalchemy import make_url
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

DATABASE_URL_VARIABLE = "ANSWERLINEAGE_DATABASE_URL"
_DRIVER = "postgresql+psycopg"


class DatabaseConfigurationError(RuntimeError):
    """La configuration de la base est absente ou inutilisable."""


def normalize_url(url: str) -> URL:
    """Accepte une URL PostgreSQL usuelle et impose le pilote psycopg 3."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql":
        raise DatabaseConfigurationError(
            "AnswerLineage ne fonctionne qu'avec PostgreSQL (ADR-001)."
        )
    return parsed.set(drivername=_DRIVER)


def database_url_from_environment() -> URL:
    value = os.environ.get(DATABASE_URL_VARIABLE)
    if not value:
        raise DatabaseConfigurationError(
            f"La variable {DATABASE_URL_VARIABLE} doit contenir l'URL de la base PostgreSQL."
        )
    return normalize_url(value)


def create_engine(url: str | URL) -> AsyncEngine:
    """Crée le moteur asynchrone. Le mot de passe n'est jamais journalisé."""
    resolved = normalize_url(url) if isinstance(url, str) else url
    return create_async_engine(resolved, pool_pre_ping=True, hide_parameters=True)
