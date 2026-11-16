"""Lecture d'une requête MCP.

Le proxy décide sur le corps de la requête, jamais sur les seuls en-têtes.
Les en-têtes ``Mcp-Method``, ``Mcp-Name`` et
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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from answerlineage_proxy.prefix import read_call_prefix
from answerlineage_proxy.tracecontext import TRACEPARENT, TraceContext, parse_traceparent

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
    # Requête SQL d'un outil déclaré : gardée en mémoire le temps de l'analyser,
    # jamais affichée, comparée ni conservée.
    sql: str | None = field(default=None, repr=False, compare=False)
    # Contexte de trace W3C d'un appel d'outil, lu dans params._meta.traceparent.
    # Une valeur mal formée est ignorée comme si elle manquait.
    trace_context: TraceContext | None = None
    # Arguments de l'appel, gardés le temps d'en calculer l'empreinte, quand elle
    # est demandée : jamais affichés, comparés ni conservés.
    arguments: object = field(default=None, repr=False, compare=False)
    has_arguments: bool = field(default=False, repr=False, compare=False)


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


def _unambiguous(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Un objet JSON dont aucune clé n'est répétée.

    Deux analyseurs ne retiennent pas la même valeur d'une clé répétée : le
    serveur pourrait exécuter un appel d'outil là où le proxy lirait autre
    chose. Un tel corps n'est pas décrit ; la requête est comptée comme non
    inspectée.
    """
    members = dict(pairs)
    if len(members) != len(pairs):
        raise ValueError("clé répétée")
    return members


def parse_message(body: bytes) -> object | None:
    """Lit un message JSON sans ambiguïté : ``None`` s'il est illisible ou a une clé répétée."""
    try:
        message: object = json.loads(body, object_pairs_hook=_unambiguous)
    except (ValueError, RecursionError):
        return None
    return message


def inspect_request(
    body: bytes,
    headers: Iterable[tuple[str, str]],
    sql_arguments: Mapping[str, str] | None = None,
    *,
    keep_arguments: bool = False,
) -> Inspection | None:
    """Analyse le corps d'un POST MCP.

    ``sql_arguments`` donne, par outil, le nom de l'argument qui porte une
    requête SQL. Avec ``keep_arguments``, les arguments d'un appel d'outil sont
    gardés pour en calculer l'empreinte ; un appel sans arguments a ``{}``.
    Renvoie ``None`` si le corps n'est pas une requête JSON-RPC lisible sans
    ambiguïté (JSON invalide, lot de requêtes, clé répétée, corps compressé) : la
    requête est alors relayée sans être décrite, et comptée.
    """
    return inspect_message(
        parse_message(body), headers, sql_arguments, keep_arguments=keep_arguments
    )


def inspect_message(
    message: object,
    headers: Iterable[tuple[str, str]],
    sql_arguments: Mapping[str, str] | None = None,
    *,
    keep_arguments: bool = False,
) -> Inspection | None:
    """Décrit un message déjà lu par ``parse_message`` ; ``None`` s'il n'est pas une
    requête JSON-RPC (lot, réponse, méthode absente)."""
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
    sql = None
    if tool is not None and sql_arguments is not None and tool in sql_arguments:
        arguments = params.get("arguments")
        candidate = arguments.get(sql_arguments[tool]) if isinstance(arguments, dict) else None
        sql = candidate if isinstance(candidate, str) else None
    return Inspection(
        is_tool_call=True,
        tool=tool,
        invalid_tool_name=tool is None,
        protocol_version=version,
        header_mismatches=tuple(mismatches),
        sql=sql,
        trace_context=parse_traceparent(meta.get(TRACEPARENT)),
        arguments=params.get("arguments", {}) if keep_arguments else None,
        has_arguments=keep_arguments,
    )


def inspect_prefix(prefix: bytes, headers: Iterable[tuple[str, str]]) -> Inspection | None:
    """Décrit un appel d'outil d'après le seul début de son corps.

    Sert quand le corps dépasse la taille lue. Renvoie ``None`` si ce début
    n'établit pas un appel d'outil et son nom. La révision du protocole n'est
    pas décrite : le début du corps ne garantit pas de contenir ``_meta``.
    """
    call = read_call_prefix(prefix)
    if call.method != TOOLS_CALL or call.name is None:
        return None
    named = [(name.lower(), value) for name, value in headers]
    mismatches: list[str] = []
    if any(value != TOOLS_CALL for name, value in named if name == HEADER_METHOD):
        mismatches.append(HEADER_METHOD)
    if any(decode_header_value(value) != call.name for name, value in named if name == HEADER_NAME):
        mismatches.append(HEADER_NAME)
    tool = call.name if _TOOL_NAME.fullmatch(call.name) else None
    return Inspection(
        is_tool_call=True,
        tool=tool,
        invalid_tool_name=tool is None,
        header_mismatches=tuple(mismatches),
    )
