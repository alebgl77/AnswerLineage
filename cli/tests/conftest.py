"""Chaque test part d'un environnement sans adresse d'API réglée."""

from __future__ import annotations

import pytest

from answerlineage_cli.client import ENV_URL


@pytest.fixture(autouse=True)
def no_configured_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_URL, raising=False)
