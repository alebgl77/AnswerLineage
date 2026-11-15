"""Les relevés de couverture de l'entreprise synthétique : chaque serveur, chaque jour."""

from __future__ import annotations

import itertools
from collections import Counter
from dataclasses import replace
from datetime import datetime

from answerlineage_corpus.coverage import coverage_reports
from answerlineage_corpus.enterprise import generate
from answerlineage_corpus.events import render


def test_each_server_reports_every_day_of_the_enterprise_without_gap_or_loss() -> None:
    enterprise = generate(3, requests=40, days=4, incidents=1)
    reports = coverage_reports(enterprise)

    emitters = {f"mcp://{tool.server}" for tool in enterprise.tools}
    assert {report["emitter"] for report in reports} == emitters
    for emitter in emitters:
        windows = [
            (report["window"]["from"], report["window"]["to"])
            for report in reports
            if report["emitter"] == emitter
        ]
        # Des jours entiers qui se suivent, sans trou.
        assert all(start.endswith("T00:00:00Z") for start, _ in windows)
        assert all(end == start for (_, end), (start, _) in itertools.pairwise(windows))
    assert all(report["events_lost"] == report["uninspected"] == 0 for report in reports)
    assert all(report["events_emitted"] == report["calls_seen"] for report in reports)


def test_the_calls_seen_are_the_tool_call_events_of_the_enterprise() -> None:
    enterprise = generate(3, requests=40, days=4, incidents=1)
    reports = coverage_reports(enterprise)
    events = Counter(
        (event["job"]["namespace"], event["eventTime"][:10])
        for event in render(enterprise)
        if "answerlineage_toolCall" in event["run"]["facets"]
    )
    seen = Counter(
        {
            (report["emitter"], report["window"]["from"][:10]): report["calls_seen"]
            for report in reports
            if report["calls_seen"]
        }
    )
    assert seen == events
    # Chaque événement tombe dans une fenêtre de son émetteur.
    first = min(report["window"]["from"] for report in reports)
    last = max(report["window"]["to"] for report in reports)
    assert all(first <= event["eventTime"] < last for event in render(enterprise))


def test_the_reports_never_end_in_the_future_of_the_enterprise() -> None:
    enterprise = generate(3, requests=40, days=4, incidents=1)
    latest = max(request.ended_at for request in enterprise.requests)
    last = max(report["window"]["to"] for report in coverage_reports(enterprise))
    assert (datetime.fromisoformat(last) - latest).days == 0


def test_an_enterprise_without_request_reports_nothing() -> None:
    enterprise = generate(3, requests=40, days=4, incidents=1)
    assert coverage_reports(replace(enterprise, requests=())) == []
