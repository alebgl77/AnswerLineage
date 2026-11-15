"""La ligne de commande du corpus."""

from __future__ import annotations

import json
from typing import Any

import pytest

from answerlineage_corpus import cli
from answerlineage_corpus.enterprise import generate
from answerlineage_corpus.events import render
from answerlineage_corpus.loader import LoadError, LoadResult
from answerlineage_corpus.truth import truth

SMALL = ["--seed", "3", "--requests", "8", "--days", "2", "--incidents", "1"]


def small() -> Any:
    return generate(3, requests=8, days=2, incidents=1)


def test_events_are_written_as_one_json_array(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["events", *SMALL]) == 0
    assert json.loads(capsys.readouterr().out) == render(small())


def test_truth_is_written_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["truth", *SMALL]) == 0
    assert json.loads(capsys.readouterr().out) == truth(small())


def test_default_enterprise_is_the_one_of_seed_one(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["truth"]) == 0
    assert json.loads(capsys.readouterr().out) == truth(generate(1))


def test_load_sends_the_events_and_reports(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[Any, ...]] = []

    def fake_send(events: Any, url: str, *, batch_size: int) -> LoadResult:
        calls.append((events, url, batch_size))
        return LoadResult(accepted=len(events), duplicates=2, rejected=0)

    monkeypatch.setattr(cli, "send", fake_send)
    assert cli.main(["load", "--url", "http://127.0.0.1:8000", "--batch-size", "50", *SMALL]) == 0
    assert calls == [(render(small()), "http://127.0.0.1:8000", 50)]
    assert capsys.readouterr().out == (
        f"{len(render(small()))} événements acceptés, dont 2 déjà connus ; 0 rejetés.\n"
    )


def test_load_fails_when_an_event_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "send", lambda *_, **__: LoadResult(accepted=1, rejected=1))
    assert cli.main(["load", "--url", "http://127.0.0.1:8000", *SMALL]) == 1


def test_load_reports_an_unreachable_api(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*_: Any, **__: Any) -> LoadResult:
        raise LoadError("L'API est injoignable.")

    monkeypatch.setattr(cli, "send", refuse)
    assert cli.main(["load", "--url", "http://127.0.0.1:8000", *SMALL]) == 1
    assert capsys.readouterr().err == "Envoi interrompu : L'API est injoignable.\n"


def test_invalid_sizes_are_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["events", "--requests", "0"]) == 2
    assert capsys.readouterr().err.startswith("Paramètres invalides")


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main([])
    assert stopped.value.code == 2
