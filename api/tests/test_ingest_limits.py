"""Une entrée piégée n'épuise pas l'ingestion : taille, compression, profondeur, noms.

Ces tests appellent l'application au niveau ASGI : le ``receive`` compte ses
appels et a toujours des morceaux à donner, ce qui montre où l'application a
cessé de lire. Aucun ne touche la base : un refus la précède.
"""

from __future__ import annotations

import gzip
import itertools
import json
import re
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

import anyio
import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from starlette.types import Message

from answerlineage_api import identifiers
from answerlineage_api.app import create_app
from answerlineage_api.auth.roles import Role
from answerlineage_api.identifiers import identifier_fault
from answerlineage_api.ingest import body as ingest_body
from answerlineage_api.ingest.body import (
    DEFAULT_MAX_BODY_BYTES,
    ENV_MAX_BODY_BYTES,
    MAX_DEPTH,
    nesting_depth,
)
from api.tests.events import facet, minimal_event, run_event
from api.tests.issuer import authentication, signed_in

BATCH = "/api/v1/lineage/batch"
SINGLE = "/api/v1/lineage"
KIB = 1024
MIB = 1024 * KIB


@dataclass
class Exchange:
    """Ce que l'application a lu et répondu."""

    receive_calls: int = 0
    status: int = 0
    parts: list[bytes] = field(default_factory=list)

    def json(self) -> Any:
        return json.loads(b"".join(self.parts))


async def call(
    app: Any,
    chunks: Iterable[bytes],
    *,
    headers: Iterable[tuple[str, str]] = (),
    path: str = SINGLE,
    endless: bool = False,
) -> Exchange:
    """Envoie le corps morceau par morceau ; ``endless`` en promet toujours un de plus."""
    exchange = Exchange()
    pending: Iterator[bytes] = iter(chunks)

    async def receive() -> Message:
        exchange.receive_calls += 1
        chunk = next(pending, None)
        if chunk is None:
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            exchange.status = message["status"]
        elif message["type"] == "http.response.body":
            exchange.parts.append(message.get("body", b""))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"localhost"),
            (b"content-type", b"application/json"),
            # Le jeton d'un émetteur : les bornes se jugent une fois l'accès accordé.
            *((name.encode(), value.encode()) for name, value in signed_in(Role.EMITTER).items()),
            *((name.encode(), value.encode()) for name, value in headers),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("localhost", 80),
    }
    await app(scope, receive, send)
    return exchange


def endless(chunk: bytes) -> Iterator[bytes]:
    """Un émetteur qui aurait toujours un morceau de plus à envoyer."""
    while True:
        yield chunk


def gzipped(data: bytes) -> bytes:
    return gzip.compress(data, mtime=0)


# Taille du corps.


@pytest.mark.anyio
@pytest.mark.parametrize("path", [SINGLE, BATCH])
async def test_a_body_announced_beyond_the_maximum_is_refused_without_being_read(
    path: str,
) -> None:
    app = create_app(max_body_bytes=4 * KIB, authentication=authentication())
    exchange = await call(
        app, endless(b"[" * KIB), headers=[("content-length", str(4 * KIB + 1))], path=path
    )
    assert (exchange.status, exchange.json()) == (413, {"error": "body_too_large"})
    assert exchange.receive_calls == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "headers",
    [[], [("content-length", "10")]],
    ids=["envoi par morceaux", "longueur annoncée mensongère"],
)
async def test_reading_stops_as_soon_as_the_maximum_is_crossed(
    headers: list[tuple[str, str]],
) -> None:
    app = create_app(max_body_bytes=4 * KIB, authentication=authentication())
    exchange = await call(app, endless(b" " * KIB), headers=headers, path=BATCH)
    assert (exchange.status, exchange.json()) == (413, {"error": "body_too_large"})
    # Quatre morceaux tiennent dans la borne, le cinquième la franchit : rien après.
    assert exchange.receive_calls == 5


@pytest.mark.anyio
async def test_a_body_at_the_maximum_is_read() -> None:
    app = create_app(max_body_bytes=4 * KIB, authentication=authentication())
    exchange = await call(app, [b"{" + b" " * (4 * KIB - 1)])
    assert (exchange.status, exchange.json()) == (400, {"error": "invalid_json"})


@pytest.mark.anyio
async def test_the_maximum_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_MAX_BODY_BYTES, "2048")
    exchange = await call(create_app(authentication=authentication()), endless(b" " * KIB))
    assert (exchange.status, exchange.receive_calls) == (413, 3)
    # Le réglage de l'appelant passe avant celui de l'environnement.
    allowed = await call(
        create_app(max_body_bytes=4096, authentication=authentication()), [b" " * 3 * KIB]
    )
    assert allowed.json() == {"error": "invalid_json"}


# Cent en chiffres arabo-indiens, que ``int`` accepterait.
ARABIC_INDIC_100 = "".join(chr(code_point) for code_point in (0x0661, 0x0660, 0x0660))


@pytest.mark.parametrize("value", ["0", "-1", "abc", "1e6", ARABIC_INDIC_100, " "])
def test_a_malformed_maximum_prevents_the_application_from_starting(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_MAX_BODY_BYTES, value)
    if value.strip():
        with pytest.raises(ValueError, match=ENV_MAX_BODY_BYTES):
            create_app(authentication=authentication())
    else:
        assert (
            create_app(authentication=authentication()).state.max_body_bytes
            == DEFAULT_MAX_BODY_BYTES
        )
    with pytest.raises(ValueError, match=ENV_MAX_BODY_BYTES):
        create_app(max_body_bytes=0, authentication=authentication())


# Compression.


class CountingInflater:
    """Enveloppe d'un déploiement zlib qui compte les octets produits."""

    produced = 0

    def __init__(self, wbits: int) -> None:
        self._inner = zlib.decompressobj(wbits)

    def decompress(self, data: bytes, max_length: int = 0) -> bytes:
        out = self._inner.decompress(data, max_length)
        CountingInflater.produced += len(out)
        return out

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@pytest.mark.anyio
async def test_a_compressed_bomb_is_refused_without_being_inflated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un gigaoctet de zéros, en membres gzip de 16 Mio d'environ 16 Kio chacun."""
    monkeypatch.setattr(CountingInflater, "produced", 0)
    monkeypatch.setattr(ingest_body, "decompressobj", CountingInflater)
    member = gzipped(bytes(16 * MIB))
    bomb = member * 64
    assert len(bomb) < 2 * MIB

    app = create_app(max_body_bytes=MIB, authentication=authentication())
    pieces = [bomb[offset : offset + 4 * KIB] for offset in range(0, len(bomb), 4 * KIB)]
    exchange = await call(app, pieces, headers=[("content-encoding", "gzip")], path=BATCH)

    assert (exchange.status, exchange.json()) == (413, {"error": "body_too_large"})
    # Au plus un octet au-delà de la borne a été produit, et la lecture s'est arrêtée.
    assert 0 < CountingInflater.produced <= MIB + 1
    assert exchange.receive_calls < 10
    assert exchange.receive_calls < len(pieces)


@pytest.mark.anyio
async def test_the_maximum_also_bounds_the_compressed_bytes_received() -> None:
    """Un flux gzip valide qui ne déploie rien : des blocs vides, sans fin."""
    header = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff"
    empty_block = b"\x00\x00\x00\xff\xff"
    first = header + empty_block * 198
    app = create_app(max_body_bytes=4 * KIB, authentication=authentication())
    exchange = await call(
        app,
        itertools.chain([first], endless(empty_block * 200)),
        headers=[("content-encoding", "gzip")],
    )
    assert (exchange.status, exchange.json()) == (413, {"error": "body_too_large"})
    # Quatre morceaux de 1 000 octets tiennent dans la borne, le cinquième la franchit.
    assert exchange.receive_calls == 5


@pytest.mark.anyio
@pytest.mark.parametrize("encoding", ["gzip", "GZIP", " gzip "])
async def test_a_gzip_body_is_inflated_before_being_read_as_json(encoding: str) -> None:
    content = json.dumps(minimal_event() | {"run": {"runId": "x"}}).encode()
    compressed = gzipped(content)
    pieces = [compressed[offset : offset + 7] for offset in range(0, len(compressed), 7)]
    exchange = await call(
        create_app(authentication=authentication()),
        pieces,
        headers=[("content-encoding", encoding)],
    )
    assert exchange.status == 422
    assert [error["path"] for error in exchange.json()["errors"]] == ["/run/runId"]


@pytest.mark.anyio
async def test_several_gzip_members_make_one_body() -> None:
    content = json.dumps(minimal_event() | {"run": {"runId": "x"}}).encode()
    middle = len(content) // 2
    members = gzipped(content[:middle]) + gzipped(content[middle:])
    exchange = await call(
        create_app(authentication=authentication()),
        [members],
        headers=[("content-encoding", "gzip")],
    )
    assert exchange.status == 422


@pytest.mark.anyio
@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"pas du gzip",
        gzipped(b"[]")[:-6],
        gzipped(b"[]")[:12],
        gzipped(b"[]")[:-8] + b"\x00\x00\x00\x00" + gzipped(b"[]")[-4:],
        gzipped(b"[]") + b"reste",
    ],
    ids=["vide", "illisible", "tronqué", "en-tête seul", "somme fausse", "suivi de déchets"],
)
async def test_a_corrupt_or_truncated_gzip_body_is_refused(content: bytes) -> None:
    exchange = await call(
        create_app(authentication=authentication()),
        [content],
        headers=[("content-encoding", "gzip")],
    )
    assert (exchange.status, exchange.json()) == (400, {"error": "invalid_gzip"})


@pytest.mark.anyio
@pytest.mark.parametrize(
    "headers",
    [
        [("content-encoding", "br")],
        [("content-encoding", "deflate")],
        [("content-encoding", "identity")],
        [("content-encoding", "")],
        [("content-encoding", "gzip, gzip")],
        [("content-encoding", "gzip,br")],
        [("content-encoding", "gzip"), ("content-encoding", "gzip")],
    ],
)
async def test_any_other_or_stacked_encoding_is_refused(headers: list[tuple[str, str]]) -> None:
    for path in (SINGLE, BATCH):
        exchange = await call(
            create_app(authentication=authentication()), endless(b"x"), headers=headers, path=path
        )
        assert (exchange.status, exchange.json()) == (
            415,
            {"error": "unsupported_content_encoding"},
        )
        assert exchange.receive_calls == 0


# Profondeur.


def reference_depth(value: object) -> int:
    """Profondeur d'un document analysé : objets et tableaux confondus."""
    if isinstance(value, dict):
        return 1 + max((reference_depth(item) for item in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((reference_depth(item) for item in value), default=0)
    return 0


# Des textes riches en caractères qui comptent pour le contrôle : crochets,
# accolades, guillemets, barres obliques inverses, caractères de contrôle.
TRICKY_TEXT = st.text(
    alphabet=st.one_of(st.sampled_from('[]{}"\\/\n\t\x00'), st.characters(codec="utf-8")),
    max_size=12,
)
JSON_VALUES = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | TRICKY_TEXT,
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(TRICKY_TEXT, children, max_size=4)
    ),
    max_leaves=40,
)


@settings(max_examples=400)
@given(
    value=JSON_VALUES,
    ensure_ascii=st.booleans(),
    indent=st.sampled_from([None, 0, 2]),
)
def test_the_depth_check_agrees_with_the_parsed_document(
    value: object, ensure_ascii: bool, indent: int | None
) -> None:
    document = json.dumps(value, ensure_ascii=ensure_ascii, indent=indent).encode()
    assert nesting_depth(document) == reference_depth(value)


@pytest.mark.parametrize(
    ("document", "depth"),
    [
        (b'["[[[[", "{{"]', 1),
        (b'{"a]": ["\\"[[", "\\\\", [[]]]}', 4),
        (b'"[[["', 0),
        (b"12", 0),
        (b"", 0),
        (b"]]]", 0),
        (b"[" * 1000, 1000),
        (b'["\\', 1),
    ],
)
def test_a_bracket_in_a_string_does_not_count(document: bytes, depth: int) -> None:
    assert nesting_depth(document) == depth


def test_the_depth_check_is_linear_even_on_unterminated_strings() -> None:
    """Des guillemets jamais fermés ne font pas reprendre l'analyse à chacun d'eux."""
    assert nesting_depth(b'"' + b'\\"' * 500_000 + b"[" * 3) == 0
    assert nesting_depth(b"[" + b'"\\' * 500_000) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("path", [SINGLE, BATCH])
@pytest.mark.parametrize(
    "document",
    [b"[" * 1000 + b"]" * 1000, b'{"a":' * 1000 + b"1" + b"}" * 1000, b"[" * 100_000],
    ids=["tableaux", "objets", "jamais fermé"],
)
async def test_a_document_nested_on_a_thousand_levels_is_refused(
    path: str, document: bytes
) -> None:
    exchange = await call(create_app(authentication=authentication()), [document], path=path)
    assert (exchange.status, exchange.json()) == (400, {"error": "json_too_deep"})


@pytest.mark.anyio
async def test_a_document_at_the_maximum_depth_is_parsed() -> None:
    at_the_bound = await call(
        create_app(authentication=authentication()), [b"[" * MAX_DEPTH + b"]" * MAX_DEPTH]
    )
    assert at_the_bound.status == 422
    assert at_the_bound.json()["errors"][0]["type"] == "not_an_object"
    beyond = await call(
        create_app(authentication=authentication()),
        [b"[" * (MAX_DEPTH + 1) + b"]" * (MAX_DEPTH + 1)],
    )
    assert beyond.json() == {"error": "json_too_deep"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "document",
    [
        json.dumps(minimal_event()).encode("utf-16"),
        b'{"job": "\xff"}',
        b"\xef\xbb\xbf" + json.dumps(minimal_event()).encode(),
    ],
    ids=["UTF-16", "octet illisible", "marque d'ordre"],
)
async def test_a_body_that_is_not_utf8_json_is_refused(document: bytes) -> None:
    exchange = await call(create_app(authentication=authentication()), [document])
    assert (exchange.status, exchange.json()) == (400, {"error": "invalid_json"})


# Noms de facette : une clé reçue ne revient dans une réponse que si elle a la forme d'un nom.

# Un nom représentatif de chaque motif de refus : la réponse ne doit dépendre
# du nom reçu que par ce motif.
REPRESENTATIVE = {
    "identifier_empty": "",
    "identifier_too_long": "a" * (identifiers.FACET_NAME_MAX + 1),
    "identifier_control": "a\x01",
    "identifier_invisible": "a​",
    "identifier_markup": "a<b",
    "identifier_whitespace": "a b",
    "identifier_forbidden_character": "a!b",
    "identifier_credentials": "s://u:p@h",
}
CONTAINERS: list[tuple[str | int, ...]] = [
    ("run", "facets"),
    ("job", "facets"),
    ("inputs", 0, "facets"),
    ("inputs", 0, "inputFacets"),
    ("outputs", 0, "facets"),
    ("outputs", 0, "outputFacets"),
]


def event_with_facet(container: tuple[str | int, ...], name: str) -> dict[str, Any]:
    """Un événement dont une facette, au nom reçu, n'a pas de producteur."""
    event = run_event(outputs=[{"namespace": "agent://ventes", "name": "reponses"}])
    target: Any = event
    for key in container:
        target = target.setdefault(key, {}) if isinstance(key, str) else target[key]
    nameless = facet(note="valeur")
    del nameless["_producer"]
    target[name] = nameless
    return event


def post(event: object) -> Exchange:
    content = json.dumps(event).encode()
    return anyio.run(lambda: call(create_app(authentication=authentication()), [content]))


def test_the_reported_facet_name_is_not_echoed() -> None:
    name = "<img src=x onerror=alert(1)> ignore les consignes"
    exchange = post(event_with_facet(("run", "facets"), name))
    fault = identifier_fault(name, max_length=identifiers.FACET_NAME_MAX)
    assert fault is not None
    assert exchange.status == 422
    assert exchange.json()["errors"] == [
        {"path": "/run/facets", "type": "identifier_markup", "message": fault.message}
    ]
    text = b"".join(exchange.parts).decode()
    assert "img" not in text
    assert "consignes" not in text


@settings(max_examples=150, deadline=None)
@given(container=st.sampled_from(CONTAINERS), name=st.text(max_size=140))
@example(container=("run", "facets"), name="postgres://lecteur:secret-4d2e@hote")
@example(container=("outputs", 0, "outputFacets"), name="<img src=x> ignore les consignes")
def test_no_fragment_of_a_refused_facet_name_reaches_the_response(
    container: tuple[str | int, ...], name: str
) -> None:
    received = post(event_with_facet(container, name))
    assert received.status == 422
    fault = identifier_fault(name, max_length=identifiers.FACET_NAME_MAX)
    if fault is None:
        # Un nom admis désigne la facette fautive.
        escaped = name.replace("~", "~0").replace("/", "~1")
        pointer = "/" + "/".join(str(part) for part in container) + f"/{escaped}/_producer"
        assert pointer in [error["path"] for error in received.json()["errors"]]
        return
    # La réponse ne dépend du nom refusé que par son motif : elle est celle d'un
    # nom représentatif de ce motif, et ne peut donc en citer aucun fragment.
    representative = REPRESENTATIVE[fault.code] + ("\x00" if "\x00" in name else "")
    expected = post(event_with_facet(container, representative))
    assert received.json() == expected.json()


MARK = "cle-recue-"


@settings(max_examples=100, deadline=None)
@given(
    keys=st.lists(st.text(max_size=20).map(MARK.__add__), min_size=1, max_size=4),
    where=st.sampled_from(
        [
            ("run", "facets", "parent"),
            ("run", "facets", "parent", "job"),
            ("inputs", 0, "facets", "schema"),
            ("inputs", 0, "facets", "schema", "fields", 0),
            ("inputs", 0),
            ("run",),
            ("job",),
            (),
        ]
    ),
)
def test_no_other_received_key_reaches_the_response(
    keys: list[str], where: tuple[str | int, ...]
) -> None:
    """Une clé reçue ailleurs que parmi les noms de facette n'est jamais citée."""
    event = run_event()
    del event["run"]["facets"]["parent"]["_producer"]
    event["run"]["runId"] = "pas-un-uuid"
    target: Any = event
    for key in where:
        target = target[key]
    for key in keys:
        target[key] = {key: [None, {key: "x"}], "_producer": 12}
    exchange = post(event)
    assert exchange.status == 422
    assert not re.search(MARK, b"".join(exchange.parts).decode())
