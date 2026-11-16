"""La fonction de référence d'un serveur MCP : l'identifiant de run de l'appel, en label BigQuery,
en QUERY_TAG Snowflake et en commentaire SQL, depuis le contexte de trace reçu."""

from __future__ import annotations

import ast
import json
import re
import secrets
import sys
from pathlib import Path
from typing import Any

import pytest

from answerlineage_proxy import warehouse
from answerlineage_proxy.tracecontext import parse_traceparent, resolve_trace
from answerlineage_proxy.warehouse import LABEL_KEY, WarehouseTags, run_id, warehouse_tags

VECTORS: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[2] / "spec" / "run-id" / "vectors-v1.json").read_text(
        encoding="utf-8"
    )
)
# Vecteur publié dans spec/run-id/vectors-v1.json, et ce qu'en tire un serveur MCP.
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
RUN_ID = "a7e19836-59d9-8033-bd72-8a4bccb7271f"
OTHER = "00-feedfacefeedfacefeedfacefeedface-0123456789abcdef-01"

# Ce que BigQuery admet dans un label : une clé de 1 à 63 caractères qui commence par une
# lettre minuscule, une valeur de 0 à 63 caractères, des minuscules, chiffres, « _ » et « - ».
BIGQUERY_KEY = re.compile(r"[a-z][a-z0-9_-]{0,62}")
BIGQUERY_VALUE = re.compile(r"[a-z0-9_-]{0,63}")


def test_a_server_derives_the_run_of_the_published_vector_and_its_three_forms() -> None:
    tags = warehouse_tags({"traceparent": TRACEPARENT})
    assert tags is not None
    assert tags.run_id == RUN_ID
    assert tags.bigquery_labels == {"answerlineage_run_id": RUN_ID}
    assert tags.snowflake_query_tag == '{"answerlineage_run_id":"' + RUN_ID + '"}'
    assert tags.sql_comment == "/* answerlineage_run_id=" + RUN_ID + " */ "


@pytest.mark.parametrize("vector", VECTORS["valid"], ids=lambda vector: vector["traceparent"])
def test_every_published_context_gives_its_run_from_meta_or_from_the_header(
    vector: dict[str, str],
) -> None:
    assert run_id({"traceparent": vector["traceparent"]}) == vector["runId"]
    assert run_id(None, [vector["traceparent"]]) == vector["runId"]
    assert run_id({}, vector["traceparent"]) == vector["runId"]


@pytest.mark.parametrize("vector", VECTORS["invalid"], ids=lambda vector: vector["reason"])
def test_every_refused_context_gives_no_run_and_nothing_to_set(vector: dict[str, str]) -> None:
    assert warehouse_tags({"traceparent": vector["traceparent"]}) is None
    assert warehouse_tags(None, [vector["traceparent"]]) is None


@pytest.mark.parametrize("value", [7, None, ["00-" + "1" * 32 + "-" + "2" * 16 + "-01"], "0" * 600])
def test_a_context_that_is_not_a_bounded_text_gives_no_run(value: object) -> None:
    assert warehouse_tags({"traceparent": value}) is None


@pytest.mark.parametrize(
    ("meta", "headers", "expected"),
    [
        # _meta l'emporte sur l'en-tête, comme pour le proxy.
        ({"traceparent": TRACEPARENT}, [OTHER], RUN_ID),
        # Un _meta mal formé ou sans contexte laisse l'en-tête décider.
        ({"traceparent": "ff" + TRACEPARENT[2:]}, [TRACEPARENT], RUN_ID),
        ("pas un objet", [TRACEPARENT], RUN_ID),
        # Deux en-têtes ne se départagent pas ; un en-tête mal formé ne compte pas.
        (None, [TRACEPARENT, TRACEPARENT], None),
        (None, ["00-PAS-UN-CONTEXTE"], None),
        # En stdio, sans _meta, le serveur ne voit aucun contexte : il ne pose rien.
        ({"io.modelcontextprotocol/protocolVersion": "2026-07-28"}, [], None),
    ],
)
def test_the_context_is_chosen_as_the_proxy_chooses_it(
    meta: object, headers: list[str], expected: str | None
) -> None:
    assert run_id(meta, headers) == expected


def test_a_server_and_the_proxy_agree_on_the_run_of_every_context_it_can_read() -> None:
    """Le proxy relaie le contexte qu'il crée ; un contexte qu'il ne relaie pas, le serveur ne
    peut pas le connaître, et ne pose rien."""
    for _ in range(200):
        meta_context = f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"
        header_context = f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-00"
        for meta in (meta_context, "00-mal-forme", None):
            for headers in ([], [header_context], ["mal-forme"], [header_context] * 2):
                trace = resolve_trace(parse_traceparent(meta), headers)
                relayed = headers if trace.forwarded is None else [*headers, trace.forwarded]
                found = run_id({"traceparent": meta}, relayed)
                if trace.origin == "proxy" and trace.forwarded is None:
                    assert found is None
                else:
                    assert found == str(trace.run_id)


def test_every_value_is_one_that_bigquery_and_snowflake_admit() -> None:
    for vector in VECTORS["valid"]:
        tags = WarehouseTags(vector["runId"])
        ((key, value),) = tags.bigquery_labels.items()
        assert BIGQUERY_KEY.fullmatch(key)
        assert BIGQUERY_VALUE.fullmatch(value)
        assert json.loads(tags.snowflake_query_tag) == {LABEL_KEY: vector["runId"]}
        # Une valeur sans apostrophe s'écrit entre apostrophes dans ALTER SESSION.
        assert "'" not in tags.snowflake_query_tag


@pytest.mark.parametrize(
    "value",
    [
        "A7E19836-59D9-8033-BD72-8A4BCCB7271F",
        "{a7e19836-59d9-8033-bd72-8a4bccb7271f}",
        "a7e1983659d98033bd728a4bccb7271f",
        "a7e19836-59d9-8033-bd72-8a4bccb7271f */ DROP TABLE clients; /*",
        "a7e19836-59d9-8033-bd72-8a4bccb7271f\n",
        "",
    ],
)
def test_a_run_id_outside_the_canonical_form_is_refused(value: str) -> None:
    with pytest.raises(ValueError) as refused:
        WarehouseTags(value)
    # Le message est fixe : il ne recopie pas la valeur reçue.
    assert str(refused.value) == "L'identifiant de run n'a pas la forme canonique d'un UUID."


def test_the_comment_closes_only_at_its_end_and_opens_only_at_its_start() -> None:
    for _ in range(1_000):
        comment = WarehouseTags(str(resolve_trace(None, []).run_id)).sql_comment
        assert re.fullmatch(r"/\* answerlineage_run_id=[0-9a-f-]{36} \*/ ", comment)
        assert comment.index("*/") == len(comment) - 3
        assert comment.rindex("/*") == 0


def test_the_reference_imports_only_the_standard_library() -> None:
    """Le module se recopie tel quel dans un serveur MCP : rien d'AnswerLineage, aucun paquet."""
    tree = ast.parse(Path(warehouse.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert imported
    assert imported <= {*sys.stdlib_module_names, "__future__"}
