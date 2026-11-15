"""Le début d'un corps trop grand pour être lu suffit à reconnaître un appel d'outil."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from answerlineage_proxy.inspection import inspect_prefix
from answerlineage_proxy.prefix import CallPrefix, read_call_prefix


def whole(document: bytes) -> CallPrefix:
    """Ce que l'analyse du document entier dit de la méthode et de l'outil."""
    message = json.loads(document)
    if not isinstance(message, dict):
        return CallPrefix()
    method = message.get("method")
    params = message.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    return CallPrefix(
        method=method if isinstance(method, str) else None,
        name=name if isinstance(name, str) else None,
    )


CALL = (
    b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
    b'"params":{"name":"execute_sql","arguments":{"query":"SELECT 1"}}}'
)


def test_method_and_tool_are_read_from_the_start_of_the_body() -> None:
    assert read_call_prefix(CALL) == CallPrefix(method="tools/call", name="execute_sql")
    # Le reste du corps n'est pas nécessaire.
    cut = CALL.index(b'"arguments"') + 20
    assert read_call_prefix(CALL[:cut]) == CallPrefix(method="tools/call", name="execute_sql")


def test_prefix_that_ends_before_the_key_concludes_nothing() -> None:
    cut = CALL.index(b'"name"')
    assert read_call_prefix(CALL[:cut]) == CallPrefix(method="tools/call")
    assert read_call_prefix(CALL[: CALL.index(b"execute_sql") + 4]) == CallPrefix(
        method="tools/call"
    )
    assert read_call_prefix(CALL[:10]) == CallPrefix()
    assert read_call_prefix(b"") == CallPrefix()


@pytest.mark.parametrize(
    "document",
    [
        # Une clé « method » dans une chaîne, ou dans un objet imbriqué, ne compte pas.
        b'{"id":"\\"method\\":\\"tools/call\\"","params":{"name":"x"},"method":"ping"}',
        b'{"meta":{"method":"tools/call"},"method":"ping","params":{"name":"x"}}',
        b'{"params":{"arguments":{"name":"leurre"},"name":"vrai"},"method":"tools/call"}',
        b'{"list":[{"method":"tools/call"},"]",{"a":"}"}],"method":"ping"}',
    ],
)
def test_keys_inside_strings_or_nested_objects_do_not_count(document: bytes) -> None:
    assert read_call_prefix(document) == whole(document)


def test_escaped_keys_and_values_are_decoded() -> None:
    document = b'{"m\\u0065thod":"tools\\/call","params":{"n\\u0061me":"ex\\u00e9cute"}}'
    assert read_call_prefix(document) == CallPrefix(method="tools/call", name="exécute")


@pytest.mark.parametrize(
    "document",
    [
        b'{"method":42,"params":{"name":"x"}}',
        b'{"method":"tools/call","params":["name","x"]}',
        b'{"method":"tools/call","params":{"name":null}}',
        b'{"method":["tools/call"]}',
        b'["tools/call"]',
        b'"tools/call"',
    ],
)
def test_values_that_are_not_strings_are_not_read(document: bytes) -> None:
    assert read_call_prefix(document) == whole(document)


def test_a_key_seen_twice_makes_its_value_uncertain() -> None:
    """Avec une clé en double, un analyseur garde la première valeur, un autre la dernière."""
    assert read_call_prefix(b'{"method":"ping","method":"tools/call"') == CallPrefix()
    assert read_call_prefix(
        b'{"method":"tools/call","params":{"name":"a","name":"b"},"x":1'
    ) == CallPrefix(method="tools/call")
    assert read_call_prefix(
        b'{"method":"tools/call","params":{"name":"a"},"params":{"name":"b"}'
    ) == CallPrefix(method="tools/call")


@pytest.mark.parametrize(
    "document",
    [
        b'{"method":"tools/call" "params":{}}',
        b'{"method" "tools/call"}',
        b'{method:"tools/call"}',
        b'{"bad":"\\x","method":"tools/call"}',
        b'{"bad":"\xff\xfe","method":"tools/call"}',
        b'{"bad":"\x01","method":"tools/call"}',
        b'{"a":1 2,"method":"tools/call"}',
        b'{"a":[1}],"method":"tools/call"',
        b"  \r\n\t",
    ],
)
def test_malformed_prefix_stops_without_concluding(document: bytes) -> None:
    assert read_call_prefix(document).method is None


def test_reading_time_is_linear_in_the_prefix() -> None:
    """Chaînes, nombres et imbrications sont sautés sans retour en arrière."""
    size = 2 * 1024 * 1024
    documents = [
        b'{"a":"' + b"\\\\" * (size // 2) + b'","method":"tools/call"}',
        b'{"a":[' + b"0," * (size // 2) + b'0],"method":"tools/call"}',
        b'{"a":' + b"[" * size,
        b'{"a":"' + b"x" * size,
    ]
    for document in documents:
        started = time.perf_counter()
        read_call_prefix(document)
        assert time.perf_counter() - started < 1.0


def test_inspection_of_a_prefix_describes_the_tool_call() -> None:
    headers = [("mcp-method", "tools/call"), ("mcp-name", "autre"), ("mcp-protocol-version", "x")]
    inspection = inspect_prefix(CALL[:80], headers)
    assert inspection is not None
    assert inspection.is_tool_call
    assert inspection.tool == "execute_sql"
    assert inspection.sql is None
    assert inspection.header_mismatches == ("mcp-name",)
    assert inspection.protocol_version is None


def test_inspection_of_a_prefix_flags_an_invalid_tool_name() -> None:
    inspection = inspect_prefix(b'{"method":"tools/call","params":{"name":"a b"}', [])
    assert inspection is not None
    assert (inspection.tool, inspection.invalid_tool_name) == (None, True)


@pytest.mark.parametrize(
    "prefix",
    [
        b'{"method":"tools/list","params":{"name":"x"}}',
        b'{"method":"tools/call","params":{"arguments":{"q":"',
        b"pas du JSON",
    ],
)
def test_prefix_that_does_not_establish_a_tool_call_is_not_described(prefix: bytes) -> None:
    assert inspect_prefix(prefix, [("mcp-method", "tools/call")]) is None


# Clés et chaînes qui ressemblent à celles que le lecteur cherche.
_KEYS = st.sampled_from(["method", "params", "name", "arguments", "_meta", "id", "jsonrpc", "m"])
_TEXT = st.one_of(
    st.text(max_size=12),
    st.sampled_from(['"method":"tools/call"', "tools/call", '\\"', "}", "]", "{", "["]),
)
_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    _TEXT,
)
_VALUES = st.recursive(
    _SCALARS,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.one_of(_KEYS, _TEXT), children, max_size=4),
    ),
    max_leaves=12,
)


@st.composite
def json_rpc_documents(draw: st.DrawFn) -> bytes:
    params: dict[str, Any] = draw(st.dictionaries(st.one_of(_KEYS, _TEXT), _VALUES, max_size=4))
    if draw(st.booleans()):
        params["name"] = draw(st.one_of(st.sampled_from(["execute_sql", "a b"]), _VALUES))
    message: dict[str, Any] = draw(st.dictionaries(st.one_of(_KEYS, _TEXT), _VALUES, max_size=4))
    if draw(st.booleans()):
        message["method"] = draw(st.one_of(st.just("tools/call"), _VALUES))
    message["params"] = draw(st.one_of(st.just(params), _VALUES))
    items = list(message.items())
    order = draw(st.permutations(range(len(items))))
    ordered = {items[index][0]: items[index][1] for index in order}
    text = json.dumps(
        ordered,
        ensure_ascii=draw(st.booleans()),
        indent=draw(st.sampled_from([None, 0, 1])),
        separators=draw(st.sampled_from([None, (",", ":"), (" ,  ", " :\t")])),
    )
    if draw(st.booleans()):
        # Même document, clés et valeurs écrites avec un échappement.
        text = text.replace('"method"', '"\\u006dethod"').replace('"name"', '"n\\u0061me"')
    return text.encode("utf-8")


@settings(max_examples=400, suppress_health_check=[HealthCheck.too_slow])
@given(document=json_rpc_documents(), cut=st.floats(min_value=0, max_value=1))
def test_a_prefix_never_gives_a_value_the_whole_document_does_not(
    document: bytes, cut: float
) -> None:
    expected = whole(document)
    found = read_call_prefix(document[: round(len(document) * cut)])
    assert found.method in (None, expected.method)
    assert found.name in (None, expected.name)
    # Le document entier est lu jusqu'au bout.
    assert read_call_prefix(document) == expected
