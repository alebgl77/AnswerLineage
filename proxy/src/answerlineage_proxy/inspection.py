"""Lecture d'une requête MCP.

Le proxy décide sur le corps de la requête, jamais sur les seuls en-têtes
(EF-SEC-11). Les en-têtes ``Mcp-Method``, ``Mcp-Name`` et
``MCP-Protocol-Version`` de la révision 2026-07-28 ne servent qu'à être
comparés au corps : un écart est signalé, et c'est le corps qui est décrit.

Rien de ce module ne conserve ni ne journalise une valeur reçue. Les en-têtes
``Mcp-Param-*`` recopient des arguments d'outil : ils ne sont jamais lus.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass

TOOLS_CALL = "tools/call"

# Nom d'outil tel que la spécification MCP le recommande : seul un nom de
# cette forme est recopié dans un événement.
_TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_PROTOCOL_VERSION = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_PROTOCOL_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
_BASE64_PREFIX = "=?base64?"
_BASE64_SUFFIX = "?="

# Champ du corps que l'en-tête Mcp-Name recopie, selon la méthode.
_NAME_SOURCE = {TOOLS_CALL: "name", "prompts/get": "name", "resources/read": "uri"}

HEADER_METHOD = "mcp-method"
HEADER_NAME = "mcp-name"
HEADER_PROTOCOL_VERSION = "mcp-protocol-version"


@dataclass(frozen=True)
class Inspection:
    """Ce que le corps d'une requête dit de l'appel."""

    is_tool_call: bool
    # Nom de l'outil lu dans le corps, s'il a une forme admise.
    tool: str | None = None
    invalid_tool_name: bool = False
    protocol_version: str | None = None
    # Noms des en-têtes MCP dont la valeur contredit le corps.
    header_mismatches: tuple[str, ...] = ()


def decode_header_value(value: str) -> str | None:
    """Décode une valeur d'en-tête MCP, forme ``=?base64?…?=`` comprise.

    Renvoie ``None`` si la valeur encodée est illisible.
    """
    if not (value.startswith(_BASE64_PREFIX) and value.endswith(_BASE64_SUFFIX)):
        return value
    encoded = value[len(_BASE64_PREFIX) : -len(_BASE64_SUFFIX)]
    try:
        return base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None


def _protocol_version(value: object) -> str | None:
    if isinstance(value, str) and _PROTOCOL_VERSION.fullmatch(value):
        return value
    return None


def inspect_request(body: bytes, headers: Iterable[tuple[str, str]]) -> Inspection | None:
    """Analyse le corps d'un POST MCP.

    Renvoie ``None`` si le corps n'est pas une requête JSON-RPC lisible : la
    requête est alors relayée sans être décrite.
    """
    try:
        message = json.loads(body)
    except (ValueError, RecursionError):
        return None
    if not isinstance(message, dict):
        return None
    method = message.get("method")
    if not isinstance(method, str):
        return None
    params = message.get("params")
    if not isinstance(params, dict):
        params = {}
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        meta = {}

    by_name: dict[str, list[str]] = {}
    for name, value in headers:
        by_name.setdefault(name.lower(), []).append(value)

    mismatches: list[str] = []
    if any(value != method for value in by_name.get(HEADER_METHOD, ())):
        mismatches.append(HEADER_METHOD)

    source = _NAME_SOURCE.get(method)
    if source is not None:
        expected = params.get(source)
        if any(decode_header_value(value) != expected for value in by_name.get(HEADER_NAME, ())):
            mismatches.append(HEADER_NAME)

    body_version = _protocol_version(meta.get(_PROTOCOL_VERSION_KEY))
    header_versions = by_name.get(HEADER_PROTOCOL_VERSION, [])
    if body_version is not None and any(value != body_version for value in header_versions):
        mismatches.append(HEADER_PROTOCOL_VERSION)
    # Les révisions à session ne portent la version que dans l'en-tête : elle
    # sert alors à décrire l'appel, jamais à décider.
    version = body_version
    if version is None and len(header_versions) == 1:
        version = _protocol_version(header_versions[0])

    if method != TOOLS_CALL:
        return Inspection(
            is_tool_call=False, protocol_version=version, header_mismatches=tuple(mismatches)
        )
    tool = params.get("name")
    if not (isinstance(tool, str) and _TOOL_NAME.fullmatch(tool)):
        tool = None
    return Inspection(
        is_tool_call=True,
        tool=tool,
        invalid_tool_name=tool is None,
        protocol_version=version,
        header_mismatches=tuple(mismatches),
    )
