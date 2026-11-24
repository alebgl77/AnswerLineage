"""Canari : rien de ce que la commande demande ou reçoit n'atteint un journal.

httpx journalise l'adresse complète de chaque requête, paramètres compris :
le nom d'un jeu de données, une fenêtre d'incident. La commande relève le
niveau de ces journaux, même sous un journal racine réglé au plus bavard.
"""

from __future__ import annotations

import logging

import pytest

from cli.tests.fakes import FakeApi, impact_result, run

CANARY = "canari-journal-4e8b"


def test_neither_the_request_nor_the_response_reaches_a_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    for name in ("httpx", "httpcore"):
        monkeypatch.setattr(logging.getLogger(name), "level", logging.NOTSET)
    caplog.set_level(logging.DEBUG)
    document = impact_result(1)
    document["dataset"]["name"] = CANARY
    api = FakeApi.sound().answer("/api/v1/impact", document)

    impact = [
        "impact",
        "--namespace",
        CANARY,
        "--name",
        CANARY,
        "--from",
        "2026-11-11T06:00:00Z",
        "--to",
        "2026-11-12T12:00:00Z",
    ]
    assert run(impact, api, monkeypatch).code == 0
    assert run(["doctor"], api, monkeypatch).code == 0

    assert len(api.requests) >= 3
    for record in caplog.records:
        assert CANARY not in record.getMessage()
        assert CANARY not in str(record.args)
    for name in ("httpx", "httpcore", "httpcore.http11", "httpcore.connection"):
        assert not logging.getLogger(name).isEnabledFor(logging.INFO)
