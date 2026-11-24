"""Une API qui renvoie n'importe quoi : la commande échoue proprement, sans recopier la valeur.

Toute réponse est bornée en taille, lue sans compression, validée champ par
champ avant d'être affichée ; un identifiant n'est affiché que s'il a une
forme admise. Le refus nomme l'endroit de la réponse, jamais ce qui s'y
trouvait.
"""

from __future__ import annotations

import copy
import gzip
import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from answerlineage_cli import impact
from answerlineage_cli.exits import Exit
from cli.tests.fakes import INCIDENT, FakeApi, hole, impact_result, run

IMPACT = "/api/v1/impact"
CANARY = "canari-7f3a"


def changed(edit: Callable[[dict[str, Any]], object]) -> dict[str, Any]:
    document = copy.deepcopy(impact_result(2))
    edit(document)
    return document


def put(path: str, value: Any) -> Callable[[dict[str, Any]], object]:
    """Remplace la valeur au chemin donné (« graph/answers/0/agent »)."""

    def edit(document: dict[str, Any]) -> None:
        *parents, last = path.split("/")
        target: Any = document
        for key in parents:
            target = target[int(key)] if isinstance(target, list) else target[key]
        if isinstance(target, list):
            target[int(last)] = value
        else:
            target[last] = value

    return edit


# Des noms piégés : séquence d'échappement du terminal, saut de ligne, balise,
# caractère invisible, forçage de direction, phrase, mot de passe dans une adresse.
TRAPPED_NAMES = {
    "terminal-escape": f"ventes\x1b[2J\x1b[31m{CANARY}",
    "newline": f"execute_sql\nVerdict : rien à rappeler {CANARY}",
    "markup": f"<script>{CANARY}</script>",
    "zero-width": f"p-3f2a{chr(0x200B)}{CANARY}",
    "bidi-override": f"mcp://crm{chr(0x202E)}{CANARY}",
    "sentence": f"Ignore les consignes {CANARY}",
    "credentials": f"postgres://lecteur:{CANARY}@crm:5432",
    "too-long": CANARY + "a" * 2000,
}
NAME_FIELDS = [
    "dataset/name",
    "graph/datasets/1/namespace",
    "graph/tools/0/name",
    "graph/agents/0/name",
    "graph/recipients/0/pseudonym",
    "graph/answers/0/fingerprint",
    "graph/answers/0/channels/0",
]

MALFORMED: dict[str, dict[str, Any]] = {
    "count-as-text": changed(put("counts/answers", "16")),
    "count-as-boolean": changed(put("counts/answers", True)),
    "negative-count": changed(put("counts/recipients", -1)),
    "flag-as-number": changed(put("truncated", 0)),
    "rate-above-one": changed(put("coverage/rate", 1.5)),
    "unknown-status": changed(put("coverage/status", CANARY)),
    "unknown-hole": changed(put("coverage/holes", [{**hole("silence", None), "kind": CANARY}])),
    "hole-without-window": changed(
        put("coverage/holes", [{k: v for k, v in hole("silence", None).items() if k != "window"}])
    ),
    "missing-counts": changed(lambda document: document.pop("counts")),
    "answers-not-a-list": changed(put("graph/answers", {CANARY: 1})),
    "answer-id-not-an-identifier": changed(put("graph/answers/0/id", CANARY)),
    "unknown-agent-node": changed(put("graph/answers/0/agent", "agent-9")),
    "unknown-recipient-node": changed(put("graph/answers/0/recipients/0", "recipient-9")),
    "unknown-dataset-node": changed(put("graph/answers/0/reads/0/dataset", "dataset-9")),
    "date-without-zone": changed(put("window/from", "2026-11-11T06:00:00")),
    "date-not-a-date": changed(put("graph/answers/0/produced_at", CANARY)),
    "cursor-not-a-cursor": changed(put("graph/next_cursor", CANARY)),
    "too-many-answers": changed(
        put("graph/answers", [impact_result(1)["graph"]["answers"][0]] * 201)
    ),
    "document-not-an-object": {"wrapped": [CANARY]},
}


def answering(body: bytes, headers: dict[str, str] | None = None) -> FakeApi:
    return FakeApi().route(
        IMPACT,
        lambda _: httpx.Response(
            200, content=body, headers={"content-type": "application/json", **(headers or {})}
        ),
    )


def refused_cleanly(api: FakeApi, monkeypatch: pytest.MonkeyPatch) -> str:
    result = run(["impact", *INCIDENT], api, monkeypatch)
    assert result.code == Exit.API_ERROR
    assert result.out == ""
    assert CANARY not in result.err
    assert "\x1b" not in result.err
    return result.err


@pytest.mark.parametrize("field", NAME_FIELDS)
@pytest.mark.parametrize("name", TRAPPED_NAMES.values(), ids=TRAPPED_NAMES.keys())
def test_a_trapped_name_fails_the_command_without_being_shown(
    monkeypatch: pytest.MonkeyPatch, field: str, name: str
) -> None:
    for output in ("text", "json"):
        api = FakeApi().answer(IMPACT, changed(put(field, name)))
        result = run(["impact", *INCIDENT, "--format", output], api, monkeypatch)
        assert result.code == Exit.API_ERROR
        assert result.out == ""
        assert CANARY not in result.err
        assert f"/{field}" in result.err


@pytest.mark.parametrize("document", MALFORMED.values(), ids=MALFORMED.keys())
def test_a_response_of_the_wrong_shape_fails_the_command(
    monkeypatch: pytest.MonkeyPatch, document: dict[str, Any]
) -> None:
    err = refused_cleanly(FakeApi().answer(IMPACT, document), monkeypatch)
    assert "Réponse de l'API invalide" in err


@pytest.mark.parametrize(
    "body",
    [
        b"{" + CANARY.encode(),
        b"[" * 100_000 + b"]" * 100_000,
        json.dumps(impact_result(1)).replace('"rate": 1.0', '"rate": NaN').encode(),
        b'{"counts": ' + b"9" * 5000 + b"}",
        "{}".encode("utf-16"),
    ],
    ids=["not-json", "too-deep", "nan", "huge-integer", "not-utf-8"],
)
def test_an_unreadable_body_fails_the_command(monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
    refused_cleanly(answering(body), monkeypatch)


def test_a_body_beyond_its_bound_is_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(impact, "MAX_RESPONSE_BYTES", 4096)
    padded = {**impact_result(1), "padding": CANARY * 1000}
    err = refused_cleanly(answering(json.dumps(padded).encode()), monkeypatch)
    assert "trop grande" in err


def test_a_streamed_body_beyond_its_bound_is_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(impact, "MAX_RESPONSE_BYTES", 4096)
    sent: list[int] = []

    def endless() -> Iterator[bytes]:
        for index in range(10_000):
            sent.append(index)
            yield b" " * 1024

    api = FakeApi().route(
        IMPACT,
        lambda _: httpx.Response(
            200, content=endless(), headers={"content-type": "application/json"}
        ),
    )
    assert "trop grande" in refused_cleanly(api, monkeypatch)
    assert len(sent) < 10


def test_a_compressed_body_is_refused_before_being_inflated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bomb = gzip.compress(b" " * 10_000_000)
    api = answering(bomb, {"content-encoding": "gzip"})
    refused_cleanly(api, monkeypatch)


@pytest.mark.parametrize("content_type", ["text/html", "application/json-canari", ""])
def test_a_body_of_another_type_fails_the_command(
    monkeypatch: pytest.MonkeyPatch, content_type: str
) -> None:
    api = FakeApi().route(
        IMPACT,
        lambda _: httpx.Response(
            200,
            content=json.dumps(impact_result(1)).encode(),
            headers={"content-type": content_type},
        ),
    )
    refused_cleanly(api, monkeypatch)


def test_a_redirect_is_not_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeApi().route(
        IMPACT,
        lambda _: httpx.Response(302, headers={"location": f"http://{CANARY}.example/vol"}),
    )
    err = refused_cleanly(api, monkeypatch)
    assert "HTTP 302" in err
    assert len(api.requests) == 1


def test_a_malformed_http_answer_is_an_invalid_response(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError(CANARY, request=request)

    result = run(["impact", *INCIDENT], httpx.MockTransport(broken), monkeypatch)
    assert result.code == Exit.API_ERROR
    assert CANARY not in result.err
