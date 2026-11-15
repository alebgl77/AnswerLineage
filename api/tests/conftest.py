from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.store.engine import create_engine


@pytest.fixture
async def engine(database: URL) -> AsyncIterator[AsyncEngine]:
    """Moteur asynchrone sur une base migrée propre au test."""
    engine = create_engine(database)
    try:
        yield engine
    finally:
        await engine.dispose()
