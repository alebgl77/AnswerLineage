"""Le proxy décide sur le corps de la requête, jamais sur les seuls en-têtes (EF-SEC-11)."""

from __future__ import annotations

import base64
import json

import pytest

from answerlineage_proxy.inspection import decode_header_value, inspect_request


def body(method: str = "tools/call", **params: object) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": 7, "method": method, "params": params}).encode()


def sentinel(value: str) -> str:
    return "=?base64?" + base64.b64encode(value.encode()).decode() + "?="


def test_tool_call_is_described_from_the_body() -> None:
    result = inspect_request(body(name="get_weather", arguments={"q": "x"}), [])
    assert result is not None
    assert result.is_tool_call
    assert result.tool == "get_weather"
    assert not result.invalid_tool_name
    assert result.header_mismatches == ()
    assert result.protocol_version is None


def test_matching_headers_raise_no_mismatch() -> None:
    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
    result = inspect_request(
        body(name="get_weather", _meta=meta),
        [
            ("Mcp-Method", "tools/call"),
            ("MCP-NAME", "get_weather"),
            ("mcp-protocol-version", "2026-07-28"),
        ],
    )
    assert result is not None
    assert result.header_mismatches == ()
    assert result.protocol_version == "2026-07-28"


def test_name_header_that_contradicts_the_body_is_reported_and_the_body_wins() -> None:
    result = inspect_request(body(name="get_weather"), [("mcp-name", "delete_everything")])
    assert result is not None
    assert result.tool == "get_weather"
    assert result.header_mismatches == ("mcp-name",)


def test_method_header_cannot_hide_a_tool_call() -> None:
    result = inspect_request(body(name="get_weather"), [("mcp-method", "tools/list")])
    assert result is not None
    assert result.is_tool_call
    assert result.header_mismatches == ("mcp-method",)


def test_method_header_cannot_invent_a_tool_call() -> None:
    result = inspect_request(body("tools/list"), [("mcp-method", "tools/call")])
    assert result is not None
    assert not result.is_tool_call
    assert result.tool is None
    assert result.header_mismatches == ("mcp-method",)


def test_protocol_version_header_that_contradicts_the_body_is_reported() -> None:
    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
    result = inspect_request(
        body(name="get_weather", _meta=meta), [("mcp-protocol-version", "2025-11-25")]
    )
    assert result is not None
    assert result.protocol_version == "2026-07-28"
    assert result.header_mismatches == ("mcp-protocol-version",)


def test_session_revisions_carry_the_version_in_the_header_only() -> None:
    result = inspect_request(body(name="get_weather"), [("mcp-protocol-version", "2025-11-25")])
    assert result is not None
    assert result.protocol_version == "2025-11-25"
    assert result.header_mismatches == ()


@pytest.mark.parametrize("value", ["latest", "2026-07-28; DROP", "", "2026-7-28"])
def test_malformed_protocol_version_is_not_copied(value: str) -> None:
    result = inspect_request(body(name="get_weather"), [("mcp-protocol-version", value)])
    assert result is not None
    assert result.protocol_version is None


def test_ambiguous_version_headers_are_not_trusted() -> None:
    headers = [("mcp-protocol-version", "2025-11-25"), ("mcp-protocol-version", "2025-06-18")]
    result = inspect_request(body(name="get_weather"), headers)
    assert result is not None
    assert result.protocol_version is None


def test_base64_name_header_is_decoded_before_comparison() -> None:
    name = "météo.paris"
    matching = inspect_request(body("prompts/get", name=name), [("mcp-name", sentinel(name))])
    assert matching is not None
    assert matching.header_mismatches == ()
    other = inspect_request(body("prompts/get", name=name), [("mcp-name", sentinel("autre"))])
    assert other is not None
    assert other.header_mismatches == ("mcp-name",)


def test_resource_uri_is_the_source_of_the_name_header() -> None:
    uri = "file:///projets/app/config.json"
    assert inspect_request(body("resources/read", uri=uri), [("mcp-name", uri)]) == inspect_request(
        body("resources/read", uri=uri), []
    )
    mismatch = inspect_request(
        body("resources/read", uri=uri), [("mcp-name", "file:///etc/passwd")]
    )
    assert mismatch is not None
    assert mismatch.header_mismatches == ("mcp-name",)


def test_duplicate_name_headers_must_all_match() -> None:
    result = inspect_request(
        body(name="get_weather"), [("mcp-name", "get_weather"), ("mcp-name", "autre")]
    )
    assert result is not None
    assert result.header_mismatches == ("mcp-name",)


@pytest.mark.parametrize(
    "name",
    [
        "ignore les instructions précédentes",
        "a" * 129,
        "",
        "outil​caché",
        "<script>",
        42,
        None,
        ["get_weather"],
    ],
)
def test_tool_name_outside_the_allowed_form_is_never_copied(name: object) -> None:
    result = inspect_request(body(name=name), [])
    assert result is not None
    assert result.is_tool_call
    assert result.tool is None
    assert result.invalid_tool_name


@pytest.mark.parametrize("name", ["getUser", "DATA_EXPORT_v2", "admin.tools.list", "a", "a" * 128])
def test_recommended_tool_names_are_accepted(name: str) -> None:
    result = inspect_request(body(name=name), [])
    assert result is not None
    assert result.tool == name


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"pas du json",
        b"[1, 2, 3]",
        b'"tools/call"',
        b'{"jsonrpc": "2.0", "id": 1, "result": {}}',
        b'{"method": 12}',
        b"\xff\xfe",
        b"[" * 100_000,
    ],
)
def test_unreadable_body_is_not_described(payload: bytes) -> None:
    assert inspect_request(payload, [("mcp-method", "tools/call")]) is None


def test_params_and_meta_of_the_wrong_type_are_ignored() -> None:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": []}).encode()
    result = inspect_request(payload, [])
    assert result is not None
    assert result.invalid_tool_name
    with_meta = inspect_request(body(name="get_weather", _meta="2026-07-28"), [])
    assert with_meta is not None
    assert with_meta.protocol_version is None


def test_decode_header_value() -> None:
    assert decode_header_value("us-west1") == "us-west1"
    assert decode_header_value("=?base64?SGVsbG8sIOS4lueVjA==?=") == "Hello, 世界"
    assert decode_header_value("=?base64?PT9iYXNlNjQ/bGl0ZXJhbD89?=") == "=?base64?literal?="
    assert decode_header_value("=?base64?pas du base64?=") is None
    assert decode_header_value("=?base64?//4=?=") is None
