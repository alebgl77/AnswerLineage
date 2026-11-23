"""Chaque test part d'un environnement sans adresse d'API ni jeton réglés."""

from __future__ import annotations

import pytest

from answerlineage_cli.client import ENV_TOKEN, ENV_TOKEN_FILE, ENV_URL


@pytest.fixture(autouse=True)
def no_configured_address(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (ENV_URL, ENV_TOKEN, ENV_TOKEN_FILE):
        monkeypatch.delenv(name, raising=False)
