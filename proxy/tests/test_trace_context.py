"""Contexte de trace W3C : lecture, création et identifiant de run qui en dérive."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from answerlineage_proxy.tracecontext import (
    CallTrace,
    TraceContext,
    derive_run_id,
    new_trace_context,
    parse_traceparent,
    resolve_trace,
)

VECTORS: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[2] / "spec" / "run-id" / "vectors-v1.json").read_text(
        encoding="utf-8"
    )
)
VALID = VECTORS["valid"]
INVALID = VECTORS["invalid"]

IN_META = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
IN_HEADER = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
# Forme d'un traceparent que le proxy crée : version 00, drapeau « non échantillonné ».
CREATED = re.compile(r"00-(?!0{32})[0-9a-f]{32}-(?!0{16})[0-9a-f]{16}-00")


def context(value: str) -> TraceContext:
    parsed = parse_traceparent(value)
    assert parsed is not None
    return parsed


@pytest.mark.parametrize("vector", VALID, ids=lambda vector: vector["traceparent"])
def test_a_published_vector_gives_its_run_id(vector: dict[str, str]) -> None:
    parsed = context(vector["traceparent"])
    assert (parsed.trace_id, parsed.parent_id) == (vector["traceId"], vector["parentId"])
    run_id = derive_run_id(parsed)
    assert str(run_id) == vector["runId"]
    assert (run_id.version, run_id.variant) == (8, "specified in RFC 4122")


@pytest.mark.parametrize("vector", INVALID, ids=lambda vector: vector["reason"])
def test_a_published_invalid_value_is_ignored(vector: dict[str, str]) -> None:
    assert parse_traceparent(vector["traceparent"]) is None


@pytest.mark.parametrize("value", [None, 42, ["00-..."], {"a": 1}, b"00-", "0" * 10_000])
def test_a_value_that_is_not_a_bounded_text_is_ignored(value: object) -> None:
    assert parse_traceparent(value) is None


@given(st.text())
def test_any_text_is_either_ignored_or_a_well_formed_context(value: str) -> None:
    parsed = parse_traceparent(value)
    if parsed is not None:
        assert value.startswith(f"{value[:2]}-{parsed.trace_id}-{parsed.parent_id}-")
        assert int(parsed.trace_id, 16) and int(parsed.parent_id, 16)


@given(
    st.integers(min_value=0, max_value=len(IN_HEADER) - 1),
    st.sampled_from("0123456789abcdefABCDEF-_ gz\n\x00é"),
)
def test_a_corrupted_traceparent_is_ignored_unless_it_is_still_valid(
    position: int, character: str
) -> None:
    corrupted = IN_HEADER[:position] + character + IN_HEADER[position + 1 :]
    parsed = parse_traceparent(corrupted)
    still_valid = re.fullmatch(
        r"(?!ff)[0-9a-f]{2}-(?!0{32})[0-9a-f]{32}-(?!0{16})[0-9a-f]{16}-[0-9a-f]{2}", corrupted
    )
    assert (parsed is not None) == (still_valid is not None)


def test_the_run_id_depends_on_the_trace_and_the_parent_only() -> None:
    first = context(IN_HEADER)
    assert derive_run_id(first) == derive_run_id(context(IN_HEADER[:-2] + "00"))
    other_parent = context(IN_HEADER[:36] + "b7ad6b7169203331-01")
    other_trace = context("00-" + "1" * 32 + IN_HEADER[35:])
    assert len({derive_run_id(first), derive_run_id(other_parent), derive_run_id(other_trace)}) == 3


def test_the_context_of_the_tool_call_in_meta_wins_over_the_http_header() -> None:
    trace = resolve_trace(context(IN_META), [IN_HEADER])
    assert trace == CallTrace(context(IN_META), "meta")
    assert trace.forwarded is None
    assert trace.run_id == derive_run_id(context(IN_META))


def test_the_http_header_alone_gives_the_context() -> None:
    trace = resolve_trace(None, [IN_HEADER])
    assert (trace.context, trace.origin, trace.forwarded) == (context(IN_HEADER), "header", None)


def test_without_context_the_proxy_creates_one_and_forwards_it() -> None:
    trace = resolve_trace(None, [])
    assert trace.origin == "proxy"
    assert trace.forwarded is not None
    assert CREATED.fullmatch(trace.forwarded)
    assert context(trace.forwarded) == trace.context
    assert trace.run_id == derive_run_id(trace.context)
    assert resolve_trace(None, []).context != trace.context


@pytest.mark.parametrize(
    "headers",
    [["pas-un-traceparent"], [IN_HEADER, IN_HEADER], [IN_HEADER + ", " + IN_HEADER]],
    ids=["mal formé", "en double", "deux valeurs jointes"],
)
def test_a_header_that_cannot_be_read_is_never_replaced_nor_doubled(headers: list[str]) -> None:
    trace = resolve_trace(None, headers)
    assert trace.origin == "proxy"
    assert trace.forwarded is None
    assert CREATED.fullmatch(trace.context.traceparent())


def test_an_invalid_meta_context_falls_back_on_the_header() -> None:
    assert resolve_trace(parse_traceparent("ff-" + IN_META[3:]), [IN_HEADER]).origin == "header"


def test_a_created_context_is_never_null() -> None:
    draws = iter(["0" * 32, "1" * 16, "2" * 32, "0" * 16, "3" * 32, "4" * 16])
    created = new_trace_context(token=lambda size: next(draws))
    assert created == TraceContext("3" * 32, "4" * 16)
    assert created.traceparent() == f"00-{'3' * 32}-{'4' * 16}-00"
