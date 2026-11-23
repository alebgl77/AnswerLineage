"""Une fausse API pour les tests de la ligne de commande : réponses écrites à la main.

La forme des réponses est celle du contrat publié ; les tests d'intégration
(``tests/test_cli_*.py``) jouent la commande contre la vraie API.
"""

from __future__ import annotations

import copy
import io
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from answerlineage_cli.cli import main

DATASET = {"namespace": "bigquery", "name": "ventes.suivi.commandes", "kind": "table"}
WINDOW = {"from": "2026-11-11T06:00:00Z", "to": "2026-11-12T12:00:00Z"}
INCIDENT = [
    "--namespace",
    "bigquery",
    "--name",
    "ventes.suivi.commandes",
    "--from",
    "2026-11-11T06:00:00Z",
    "--to",
    "2026-11-12T12:00:00Z",
]
ANSWER_ID = "01a1c9e63a187c5cb8b5fce292591000424d79573035c170"
CURSOR = "1794386287000000.01a1c9e63a187c5cb8b5fce292591000424d79573035c170"
HTML = '<!doctype html><html><body><div id="root"></div></body></html>'


def coverage(
    status: str = "measured",
    rate: float | None = 1.0,
    holes: Sequence[dict[str, Any]] = (),
    *,
    truncated: bool = False,
) -> dict[str, Any]:
    return {
        "status": status,
        "rate": rate,
        "calls_seen": 12,
        "events_in_graph": 12,
        "events_lost": 0,
        "uninspected": 0,
        "holes": list(holes),
        "truncated": truncated,
    }


def hole(kind: str, count: int | None = 3, emitter: str = "mcp://crm") -> dict[str, Any]:
    return {
        "emitter": emitter,
        "kind": kind,
        "window": {"from": "2026-11-11T00:00:00Z", "to": "2026-11-12T00:00:00Z"},
        "count": count,
    }


def answer(index: int = 0) -> dict[str, Any]:
    return {
        "id": f"{index:02d}" + ANSWER_ID[2:],
        "fingerprint": f"sha256:{index:064x}",
        "produced_at": f"2026-11-11T09:{index:02d}:36Z",
        "agent": "agent-0",
        "channels": ["slack"],
        "recipients": ["recipient-0", "recipient-1"],
        "tools": ["tool-0", "tool-1"],
        "reads": [
            {"dataset": "dataset-0", "tool": "tool-0"},
            {"dataset": "dataset-1", "tool": None},
        ],
    }


def impact_result(
    answers: int = 2,
    *,
    recipients: int | None = None,
    cover: dict[str, Any] | None = None,
    truncated: bool = False,
    more: bool = False,
    known_at: str | None = None,
) -> dict[str, Any]:
    shown = [answer(index) for index in range(answers)]
    graph: dict[str, Any] = {
        "datasets": [
            {"id": "dataset-0", **DATASET},
            {"id": "dataset-1", "namespace": "postgres://crm:5432", "name": "crm.public.comptes"}
            | {"kind": "table"},
        ]
        if answers
        else [],
        "tools": [
            {"id": "tool-0", "server": "entrepot", "name": "execute_sql"},
            {"id": "tool-1", "server": "crm", "name": "lister_opportunites"},
        ]
        if answers
        else [],
        "agents": [{"id": "agent-0", "namespace": "agent://ventes", "name": "assistant-commercial"}]
        if answers
        else [],
        "answers": shown,
        "recipients": [
            {"id": "recipient-0", "pseudonym": "p-3f2a9c"},
            {"id": "recipient-1", "pseudonym": "p-81d0e4"},
        ]
        if answers
        else [],
        "more": more,
        "next_cursor": CURSOR if more else None,
        "truncated": False,
    }
    result: dict[str, Any] = {
        "dataset": dict(DATASET),
        "window": dict(WINDOW),
        "counts": {
            "answers": answers,
            "recipients": (2 if answers else 0) if recipients is None else recipients,
            "lower_bound": truncated,
        },
        "coverage": coverage() if cover is None else cover,
        "graph": graph,
        "truncated": truncated,
    }
    if known_at is not None:
        result["known_at"] = known_at
        graph["known_at"] = known_at
    return result


EXPOSURE_CURSOR = "638974224000000000.1954320.42"


def exposure_result(
    *,
    truncated: bool = False,
    more: bool = False,
    known_at: str | None = None,
) -> dict[str, Any]:
    """Un classement tel que l'API le rend : deux jeux de données, des colonnes pour l'un."""
    result: dict[str, Any] = {
        "at": "2026-11-01T12:00:00Z",
        "datasets": [
            {
                **{key: DATASET[key] for key in ("namespace", "kind")},
                "name": "finance.ref.taux_change",
                "score": 10.192078,
                "answers": 5,
                "recipients": 6,
                "columns": [
                    {"name": "date_valeur", "score": 6.423125, "answers": 2},
                    {"name": "devise", "score": 5.315637, "answers": 4},
                ],
                "more_columns": True,
            },
            {
                "namespace": "postgres://crm:5432",
                "name": "crm.public.comptes",
                "kind": "table",
                "score": 4,
                "answers": 1,
                "recipients": 3,
                "columns": [],
                "more_columns": False,
            },
        ],
        "more": more,
        "next_cursor": EXPOSURE_CURSOR if more else None,
        "truncated": truncated,
    }
    if known_at is not None:
        result["known_at"] = known_at
    return result


INCIDENT_ID = "3f0c6a52-9d1e-4b7a-8c2f-5e6d7a8b9c0d"


def incident(
    state: str = "pending",
    *,
    answers: int = 2,
    recipients: int = 3,
    truncated: bool = False,
    error: str | None = None,
    attempts: int = 0,
) -> dict[str, Any]:
    """Un incident tel que l'API le rend, son analyse dans l'état demandé."""
    done = state == "done"
    return {
        "id": INCIDENT_ID,
        "state": "qualified",
        "severity": "high",
        "scope": {"datasets": [{"namespace": "bigquery", "name": "ventes.suivi.commandes"}]},
        "window": dict(WINDOW),
        "declared_at": "2026-11-12T13:00:00Z",
        "analysis": {
            "state": state,
            "requested_at": "2026-11-12T13:00:00Z",
            "attempts": attempts,
            "error": error,
            "known_at": "2026-11-12T13:00:01Z" if state != "pending" or attempts else None,
            "finished_at": None if state == "pending" else "2026-11-12T13:00:02Z",
            "counts": {"answers": answers, "recipients": recipients, "lower_bound": truncated}
            if done
            else None,
            "truncated": truncated if done else None,
        },
    }


def incident_detail(
    state: str = "done",
    *,
    answers: int = 2,
    cover: dict[str, Any] | None = None,
    unknown: Sequence[dict[str, str]] = (),
    truncated: bool = False,
    more: bool = False,
    **changes: Any,
) -> dict[str, Any]:
    """Un incident et le résultat de son analyse, une page de réponses touchées."""
    shown = [
        {
            "id": f"{index:02d}" + ANSWER_ID[2:],
            "fingerprint": f"sha256:{index:064x}",
            "produced_at": f"2026-11-11T09:{index:02d}:36Z",
            "recipients": ["p-3f2a9c", f"p-81d0e{index}"],
        }
        for index in range(answers)
    ]
    document = incident(state, answers=answers, truncated=truncated, **changes)
    document["result"] = (
        {
            "unknown_datasets": list(unknown),
            "coverage": coverage() if cover is None else cover,
            "answers": shown,
            "more": more,
            "next_cursor": CURSOR if more else None,
        }
        if state == "done"
        else None
    )
    return document


def health(**changes: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "api_version": "1.0.0",
        "database": "available",
        "migrations": "current",
        "queue": {"pending": 0, "lower_bound": False, "oldest_age_seconds": None},
        "dead_letters": {"events": 0, "lower_bound": False, "oldest_age_seconds": None},
        "emitters": {"known": 2, "muted": 0, "lower_bound": False},
        "partitions": {"missing": 0, "lower_bound": False},
        "reemission": {
            "queued": 0,
            "retrying": 0,
            "lower_bound": False,
            "oldest_age_seconds": None,
            "sent": 40,
            "lost": 0,
            "rejected": 0,
        },
    }
    state.update(changes)
    return state


Handler = Callable[[httpx.Request], httpx.Response]


def json_response(document: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=document)


@dataclass
class FakeApi:
    """Une API qui répond par chemin, et retient ce qu'elle reçoit."""

    routes: dict[str, Handler] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    @classmethod
    def sound(cls) -> FakeApi:
        """Une installation saine, qui sert l'explorateur."""
        return cls(
            {
                "/api/v1/health": lambda _: json_response(health()),
                "/": lambda _: httpx.Response(
                    200, text=HTML, headers={"content-type": "text/html; charset=utf-8"}
                ),
            }
        )

    def route(self, path: str, handler: Handler) -> FakeApi:
        self.routes[path] = handler
        return self

    def answer(self, path: str, document: Any, status: int = 200) -> FakeApi:
        return self.route(path, lambda _: json_response(copy.deepcopy(document), status))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.routes.get(request.url.path)
        if handler is None:
            return httpx.Response(404, json={"detail": "Not Found"})
        return handler(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("Connexion refusée", request=request)


@dataclass(frozen=True)
class Run:
    code: int
    out: str
    err: str


def run(
    argv: Sequence[str],
    api: FakeApi | httpx.BaseTransport | None,
    monkeypatch: pytest.MonkeyPatch,
) -> Run:
    """Lance la commande ; sa sortie est lue en octets, comme un terminal la recevrait.

    Sans fausse API ni transport, la commande joint le réseau pour de bon.
    """
    out, err = io.BytesIO(), io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(out, encoding="utf-8"))
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(err, encoding="utf-8"))
    transport = api.transport if isinstance(api, FakeApi) else api
    code = main(list(argv), transport=transport)
    sys.stdout.flush()
    sys.stderr.flush()
    return Run(code, out.getvalue().decode("utf-8"), err.getvalue().decode("utf-8"))
