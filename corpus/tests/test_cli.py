"""La ligne de commande du corpus."""

from __future__ import annotations

import json
from typing import Any

import pytest

from answerlineage_corpus import cli
from answerlineage_corpus.coverage import coverage_reports
from answerlineage_corpus.enterprise import generate
from answerlineage_corpus.events import render
from answerlineage_corpus.loader import (
    ENV_TOKEN,
    ENV_TOKEN_FILE,
    LoadError,
    LoadResult,
    ReportResult,
)
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


@pytest.mark.parametrize("token", [None, "jeton.d-emetteur"], ids=["sans jeton", "avec jeton"])
def test_load_sends_the_events_and_reports(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], token: str | None
) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.delenv(ENV_TOKEN_FILE, raising=False)
    if token is None:
        monkeypatch.delenv(ENV_TOKEN, raising=False)
    else:
        monkeypatch.setenv(ENV_TOKEN, token)

    def fake_send(events: Any, url: str, *, batch_size: int, token: str | None) -> LoadResult:
        calls.append((events, url, batch_size, token))
        return LoadResult(accepted=len(events), duplicates=2, rejected=0)

    def fake_send_reports(reports: Any, url: str, *, token: str | None) -> ReportResult:
        calls.append((reports, url, token))
        return ReportResult(accepted=len(reports))

    monkeypatch.setattr(cli, "send", fake_send)
    monkeypatch.setattr(cli, "send_reports", fake_send_reports)
    assert cli.main(["load", "--url", "http://127.0.0.1:8000", "--batch-size", "50", *SMALL]) == 0
    reports = coverage_reports(small())
    assert calls == [
        (render(small()), "http://127.0.0.1:8000", 50, token),
        (reports, "http://127.0.0.1:8000", token),
    ]
    assert capsys.readouterr().out == (
        f"{len(render(small()))} événements acceptés, dont 2 déjà connus ; 0 rejetés.\n"
        f"{len(reports)} relevés de couverture acceptés ; 0 rejetés.\n"
    )


@pytest.mark.parametrize(
    ("events", "reports"),
    [
        (LoadResult(accepted=1, rejected=1), ReportResult(accepted=1)),
        (LoadResult(accepted=1), ReportResult(accepted=1, rejected=1)),
    ],
)
def test_load_fails_when_an_event_or_a_report_is_rejected(
    monkeypatch: pytest.MonkeyPatch, events: LoadResult, reports: ReportResult
) -> None:
    monkeypatch.setattr(cli, "send", lambda *_, **__: events)
    monkeypatch.setattr(cli, "send_reports", lambda *_, **__: reports)
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
