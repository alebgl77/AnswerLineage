"""Le commentaire qui porte l'identifiant de run, écrit en tête de la requête : seule la chaîne
de la requête change, et un corps qui ne se lit pas avec certitude n'est jamais modifié."""

from __future__ import annotations

import json
from uuid import UUID

import pytest

from answerlineage_proxy.inspection import parse_message
from answerlineage_proxy.tagging import _string_at, comment_query

RUN = UUID("a7e19836-59d9-8033-bd72-8a4bccb7271f")
COMMENT = b"/* answerlineage_run_id=a7e19836-59d9-8033-bd72-8a4bccb7271f */ "

# Un appel tel qu'un client peut l'écrire : clés dans un ordre quelconque, blancs irréguliers,
# échappements dans les clés comme dans les valeurs, texte non ASCII brut, et des leurres :
# une clé « query » dans un autre argument, « params » dans une chaîne.
IRREGULAR = (
    '{ "jsonrpc" : "2.0",\r\n "id":"a\\"b", "method":"tools\\/call",\n'
    '\t"params" :{"name":"execute_sql","_meta":{"query":"pas celle-ci","traceparent":"x"},'
    '"arguments": {\n  "options":{"query":"leurre","n":[1,2.50,{"query":null}]},'
    '  "q\\u0075ery"\t:  "SELECT c.nom\\n FROM crm.clients c WHERE c.ville = \'S\\u00e9ville\''
    ' AND c.pays = \'España\' -- \\"params\\"",\n'
    '  "region":"eu" } }, "extra":"\\"params\\":{}" }'
).encode()


def tagged_query(body: bytes) -> str:
    message = json.loads(body)
    query: str = message["params"]["arguments"]["query"]
    return query


def test_only_the_query_changes_every_other_byte_is_kept() -> None:
    tagged = comment_query(IRREGULAR, "query", RUN)
    assert isinstance(tagged, bytes)
    # Le commentaire est inséré juste après le guillemet ouvrant de la requête, et nulle part
    # ailleurs : retiré, il rend le corps reçu à l'octet près.
    start = IRREGULAR.index(b'"SELECT') + 1
    assert tagged == IRREGULAR[:start] + COMMENT + IRREGULAR[start:]
    assert tagged.replace(COMMENT, b"", 1) == IRREGULAR
    assert tagged_query(tagged) == COMMENT.decode() + tagged_query(IRREGULAR)
    # Le reste du message, lu, est le même.
    before, after = json.loads(IRREGULAR), json.loads(tagged)
    after["params"]["arguments"]["query"] = before["params"]["arguments"]["query"]
    assert after == before
    # Le corps étiqueté se lit toujours sans ambiguïté.
    assert parse_message(tagged) is not None


@pytest.mark.parametrize(
    "arguments",
    [
        {"q": "SELECT 1"},
        {"query": None},
        {"query": 42},
        {"query": ["SELECT 1"]},
        {"query": {"text": "SELECT 1"}},
    ],
    ids=str,
)
def test_a_missing_argument_or_one_that_is_not_text_leaves_the_body_alone(
    arguments: dict[str, object],
) -> None:
    body = json.dumps({"method": "tools/call", "params": {"name": "s", "arguments": arguments}})
    assert comment_query(body.encode(), "query", RUN) == "missing_argument"


@pytest.mark.parametrize(
    "message",
    [
        {"method": "tools/call"},
        {"method": "tools/call", "params": "texte"},
        {"method": "tools/call", "params": {"name": "s"}},
        {"method": "tools/call", "params": {"name": "s", "arguments": "query"}},
        {"method": "tools/call", "params": {}},
    ],
    ids=str,
)
def test_a_call_without_arguments_object_leaves_the_body_alone(message: object) -> None:
    assert comment_query(json.dumps(message).encode(), "query", RUN) == "missing_argument"


@pytest.mark.parametrize("query", ["", "   ", "\n\t"])
def test_an_empty_query_is_not_made_into_a_comment(query: str) -> None:
    body = json.dumps({"params": {"arguments": {"query": query}}}).encode()
    assert comment_query(body, "query", RUN) == "empty"


@pytest.mark.parametrize(
    "query",
    [
        "#standardSQL\nSELECT 1",
        "  #legacySQL\nSELECT 1 FROM [p:d.t]",
        "/*+ SeqScan(t) */ SELECT * FROM t",
        "\n/*+ IndexScan(t) */ SELECT 1",
    ],
)
def test_a_directive_read_at_the_start_of_the_query_keeps_its_place(query: str) -> None:
    """La directive de dialecte de BigQuery, l'indice d'optimiseur : un commentaire devant eux
    les priverait d'effet."""
    body = json.dumps({"params": {"arguments": {"query": query}}}).encode()
    assert comment_query(body, "query", RUN) == "leading_directive"


@pytest.mark.parametrize(
    "body",
    [
        # Octets qui ne sont pas de l'UTF-8 : le serveur les lirait autrement.
        b'{"params":{"arguments":{"query":"SELECT \xe9"}}}',
        # Marque d'ordre des octets en tête, demi-paire de substitution encodée : un lecteur
        # JSON l'accepte, un autre non.
        b'\xef\xbb\xbf{"params":{"arguments":{"query":"SELECT 1"}}}',
        b'{"params":{"arguments":{"query":"SELECT \xed\xa0\x80"}}}',
        # Un lot, une valeur qui n'est pas un objet, un document illisible.
        b'[{"params":{"arguments":{"query":"SELECT 1"}}}]',
        b'"SELECT 1"',
        b'{"params":{"arguments":{"query":"SELECT 1"',
        b'{"params":{"arguments" {"query":"SELECT 1"}}}',
        b'{"params" , {"arguments":{"query":"SELECT 1"}}}',
        b'{params:{"arguments":{"query":"SELECT 1"}}}',
        b'{"params":{"arguments":{"query":"SELECT \x01"}}}',
    ],
)
def test_a_body_not_read_with_certainty_is_never_modified(body: bytes) -> None:
    assert comment_query(body, "query", RUN) == "unreadable"


@pytest.mark.parametrize(
    "text",
    [
        '﻿{"params":{"arguments":{"query":"SELECT 1"}}}',
        '{"params":{"arguments":{"query":7}}}',
        '{"params":{"arguments":{}}}',
        '{"params":{"arguments":{"autre":"SELECT 1"}}}',
        '{"params":[]}',
        '{"params" {"arguments":{"query":"SELECT 1"}}}',
        '{params:{"arguments":{"query":"SELECT 1"}}}',
        '{"id":1 "params":{"arguments":{"query":"SELECT 1"}}}',
    ],
)
def test_the_path_to_the_query_follows_only_the_exact_structure(text: str) -> None:
    """Le corps est déjà lu en entier quand le chemin se suit : un écart ne se devine pas."""
    with pytest.raises(ValueError):
        _string_at(text, ("params", "arguments", "query"))


def test_the_comment_holds_only_the_run_identifier() -> None:
    body = json.dumps({"params": {"arguments": {"query": "SELECT 1"}}}).encode()
    tagged = comment_query(body, "query", RUN)
    assert isinstance(tagged, bytes)
    assert tagged_query(tagged) == f"/* answerlineage_run_id={RUN} */ SELECT 1"
