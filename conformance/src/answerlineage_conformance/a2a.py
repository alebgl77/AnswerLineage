"""Suite de conformité de l'extension A2A d'AnswerLineage, version 1.

Elle juge deux fichiers : l'Agent Card de l'agent appelé, et un échange
capturé, la requête de l'appelant et la réponse de l'appelé, sous leur forme
JSON d'A2A 1.0. Rien d'autre n'est lu : ni réseau, ni base de données.

L'échange se juge des deux côtés. L'appelant active l'extension et envoie son
contexte ; l'appelé, s'il l'a reçue, la nomme dans sa réponse et rend un
résumé dont le rattachement est celui que le contexte reçu commande : la suite
le recalcule elle-même, par la dérivation publiée.

Chaque problème a un code stable et un pointeur JSON (RFC 6901) dans le
fichier où il est. Aucune valeur reçue n'est recopiée : un motif est un code
fixe, une clé douteuse est masquée.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any, Final, Literal

from jsonschema import Draft202012Validator

from answerlineage_api import identifiers
from answerlineage_conformance.findings import items, mapping
from answerlineage_conformance.pointers import Location, Masker
from answerlineage_conformance.runid import derive_run_id
from answerlineage_conformance.schemas import rule_violations

# URI de l'extension, version 1, et le préfixe que partagent toutes ses versions.
URI: Final = "https://github.com/alebgl77/AnswerLineage/tree/main/spec/a2a/v1"
FAMILY: Final = "https://github.com/alebgl77/AnswerLineage/tree/main/spec/a2a/"
# Paramètre de service d'A2A qui active les extensions, nom lu sans égard à la casse.
EXTENSIONS: Final = "a2a-extensions"
# Un texte de l'échange plus court ne se cherche pas : il se retrouverait par hasard.
MIN_CONTENT: Final = 12
# Un échange fautif peut l'être à chaque jeu de données : la liste est bornée, pas le compte.
MAX_PROBLEMS: Final = 1000

Group = Literal["version", "declaration", "call", "response", "content"]
FileKind = Literal["card", "exchange"]

GROUPS: Final[tuple[Group, ...]] = ("version", "declaration", "call", "response", "content")
_FILES: Final[tuple[FileKind, ...]] = ("card", "exchange")


@dataclass(frozen=True)
class Control:
    code: str
    group: Group
    message: str


CONTROLS: Final[tuple[Control, ...]] = (
    Control(
        "other_extension_version",
        "version",
        "URI d'une autre version de l'extension, ou mal écrite : elle n'est pas celle de la "
        "version 1, et un agent ne se rabat pas sur une autre version.",
    ),
    Control(
        "extension_not_declared",
        "declaration",
        "L'Agent Card ne déclare pas l'extension dans capabilities.extensions.",
    ),
    Control(
        "extension_declared_twice",
        "declaration",
        "L'Agent Card déclare l'extension plusieurs fois : ses paramètres seraient ambigus.",
    ),
    Control(
        "declaration_schema_violation",
        "declaration",
        "La déclaration ne respecte pas le schéma publié ; le motif est la règle violée.",
    ),
    Control(
        "extension_not_activated",
        "call",
        "La requête n'active pas l'extension : le paramètre de service A2A-Extensions ne nomme "
        "pas son URI.",
    ),
    Control(
        "context_missing",
        "call",
        "L'extension est activée, mais le message ne porte pas le contexte de l'appel.",
    ),
    Control(
        "context_schema_violation",
        "call",
        "Le contexte de l'appel ne respecte pas le schéma publié ; le motif est la règle violée.",
    ),
    Control(
        "context_run_id_not_derived",
        "call",
        "Le runId envoyé n'est pas celui que la dérivation publiée donne pour le traceparent.",
    ),
    Control(
        "context_not_listed",
        "call",
        "Le message porte le contexte, mais ne nomme pas l'URI dans ses extensions.",
    ),
    Control(
        "extension_not_echoed",
        "response",
        "La réponse ne nomme pas l'extension activée dans son paramètre A2A-Extensions.",
    ),
    Control(
        "summary_missing",
        "response",
        "L'extension est activée, mais la tâche ou le message rendu ne porte pas de résumé.",
    ),
    Control(
        "summary_schema_violation",
        "response",
        "Le résumé ne respecte pas le schéma publié ; le motif est la règle violée.",
    ),
    Control(
        "summary_attachment_wrong",
        "response",
        "Le rattachement que dit le résumé n'est pas celui que le contexte reçu commande ; "
        "le motif dit ce qui était attendu.",
    ),
    Control(
        "summary_inconsistent",
        "response",
        "Le résumé se contredit : jeu de données répété, compte inférieur à la liste, ou "
        "troncature mal dite.",
    ),
    Control(
        "summary_not_listed",
        "response",
        "Le message rendu porte le résumé, mais ne nomme pas l'URI dans ses extensions.",
    ),
    Control(
        "identifier_rejected",
        "content",
        "Un nom de l'extension n'a pas la forme d'un nom : le motif est le code de l'ingestion. "
        "Une phrase, une balise ou un caractère invisible n'ont rien à faire dans un nom.",
    ),
    Control(
        "content_in_extension",
        "content",
        "Un texte échangé, question ou réponse, se retrouve tel quel dans les données de "
        "l'extension.",
    ),
)

BY_CODE: Final[dict[str, Control]] = {control.code: control for control in CONTROLS}


@cache
def validator(name: str) -> Draft202012Validator:
    """Le validateur d'un schéma publié de la version 1, lu dans la copie embarquée."""
    path = resources.files("answerlineage_conformance") / "schemas" / "a2a" / "v1" / f"{name}.json"
    return Draft202012Validator(json.loads(path.read_text(encoding="utf-8")))


@dataclass(frozen=True)
class _Finding:
    """Un problème relevé : son code, le fichier, l'emplacement dans le document, le motif."""

    code: str
    file: FileKind
    location: Location
    reason: str | None = None


@dataclass(frozen=True)
class Problem:
    """Un problème du rapport : où, quoi, jamais la valeur reçue."""

    code: str
    group: Group
    message: str
    file: str
    pointer: str
    reason: str | None = None


@dataclass(frozen=True)
class Judgement:
    # Les problèmes listés, ``MAX_PROBLEMS`` au plus, dans un ordre fixe.
    problems: tuple[Problem, ...]
    # Le compte de tous les problèmes, par code de contrôle.
    counts: Mapping[str, int]
    # Les contrôles exécutés : tous, à chaque fois.
    controls: tuple[Control, ...] = CONTROLS

    @property
    def problem_count(self) -> int:
        return sum(self.counts.values())

    @property
    def truncated(self) -> bool:
        return self.problem_count > len(self.problems)

    @property
    def conformant(self) -> bool:
        return self.problem_count == 0


def _other_version(value: object) -> bool:
    """Une URI de la famille de l'extension qui n'est pas celle de la version 1."""
    return isinstance(value, str) and value.startswith(FAMILY) and value != URI


def _named(parameters: object) -> Iterator[tuple[str, list[str]]]:
    """Les paramètres A2A-Extensions, quelle que soit leur casse : la clé, puis les URI nommées."""
    for key, value in mapping(parameters).items():
        if key.lower() == EXTENSIONS and isinstance(value, str):
            yield key, [item.strip(" \t") for item in value.split(",") if item.strip(" \t")]


def _violations(
    code: str, schema: str, document: object, file: FileKind, at: Location
) -> list[_Finding]:
    return [
        _Finding(code, file, (*at, *inside), rule)
        for inside, rule in rule_violations(validator(schema), document)
    ]


def _identifier(value: object, max_length: int, file: FileKind, at: Location) -> list[_Finding]:
    """Un nom qui n'a pas la forme d'un nom. Sa longueur et son absence relèvent du schéma."""
    if not isinstance(value, str):
        return []
    fault = identifiers.identifier_fault(value, max_length=max_length)
    if fault is None or fault.code in ("identifier_empty", "identifier_too_long"):
        return []
    return [_Finding("identifier_rejected", file, at, fault.code)]


def _versions(listed: list[Any], metadata: Mapping[str, Any], at: Location) -> list[_Finding]:
    """Les URI d'une autre version dans les extensions et les métadonnées d'un message."""
    findings = [
        _Finding("other_extension_version", "exchange", (*at, "extensions", index))
        for index, uri in enumerate(listed)
        if _other_version(uri)
    ]
    findings.extend(
        _Finding("other_extension_version", "exchange", (*at, "metadata", key))
        for key in metadata
        if _other_version(key)
    )
    return findings


def _parameters(owner: Mapping[str, Any], side: str) -> tuple[list[_Finding], bool]:
    """Les paramètres de service d'un côté : une autre version nommée, et l'URI nommée ou non."""
    findings: list[_Finding] = []
    named = False
    for key, uris in _named(owner.get("serviceParameters")):
        named = named or URI in uris
        if any(_other_version(uri) for uri in uris):
            findings.append(
                _Finding("other_extension_version", "exchange", (side, "serviceParameters", key))
            )
    return findings, named


def _card(card: object) -> list[_Finding]:
    """La déclaration de l'extension dans l'Agent Card."""
    where: Location = ("capabilities", "extensions")
    entries = items(mapping(mapping(card).get("capabilities")).get("extensions"))
    findings = [
        _Finding("other_extension_version", "card", (*where, index, "uri"))
        for index, entry in enumerate(entries)
        if _other_version(mapping(entry).get("uri"))
    ]
    declared = [index for index, entry in enumerate(entries) if mapping(entry).get("uri") == URI]
    if not declared:
        return [*findings, _Finding("extension_not_declared", "card", where)]
    findings.extend(
        _Finding("extension_declared_twice", "card", (*where, index)) for index in declared[1:]
    )
    first: Location = (*where, declared[0])
    entry = entries[declared[0]]
    findings.extend(
        _violations("declaration_schema_violation", "AgentExtension", entry, "card", first)
    )
    job = mapping(mapping(mapping(entry).get("params")).get("callJob"))
    for name in ("namespace", "name"):
        at = (*first, "params", "callJob", name)
        findings.extend(_identifier(job.get(name), identifiers.JOB_NAME_MAX, "card", at))
    return findings


@dataclass(frozen=True)
class _Call:
    """Ce que dit la requête, et ce que le contexte reçu commande à l'appelé."""

    findings: list[_Finding]
    activated: bool
    # Rattachement attendu (``attached``, ``missing``, ``rejected``), parent ou motif du refus.
    attachment: str
    parent: str | None = None
    rejection: str | None = None
    # Le runId envoyé, s'il est un texte : l'appelé ne le prend jamais pour le sien.
    caller_run: str | None = None
    # Le contexte envoyé, et son emplacement, quand le message en porte un.
    context: object = None
    at: Location | None = None


def _call(exchange: Mapping[str, Any]) -> _Call:
    """L'appel : activation, contexte, dérivation de l'identifiant de run."""
    request = mapping(exchange.get("request"))
    findings, activated = _parameters(request, "request")
    at: Location = ("request", "params", "message")
    message = mapping(mapping(request.get("params")).get("message"))
    listed = items(message.get("extensions"))
    metadata = mapping(message.get("metadata"))
    findings.extend(_versions(listed, metadata, at))
    if not activated:
        findings.append(
            _Finding("extension_not_activated", "exchange", ("request", "serviceParameters"))
        )
    if URI not in metadata:
        if activated:
            findings.append(_Finding("context_missing", "exchange", (*at, "metadata")))
        return _Call(findings, activated, "missing")
    context = metadata[URI]
    where: Location = (*at, "metadata", URI)
    if URI not in listed:
        findings.append(_Finding("context_not_listed", "exchange", (*at, "extensions")))
    sent = mapping(context).get("runId")
    caller_run = sent if isinstance(sent, str) else None
    malformed = _violations("context_schema_violation", "CallerContext", context, "exchange", where)
    if malformed:
        findings.extend(malformed)
        return _Call(
            findings, activated, "rejected", None, "context_malformed", caller_run, context, where
        )
    _, trace_id, parent_id, _ = context["traceparent"].split("-")
    if str(derive_run_id(trace_id, parent_id)) != sent:
        findings.append(_Finding("context_run_id_not_derived", "exchange", (*where, "runId")))
        return _Call(
            findings, activated, "rejected", None, "run_id_not_derived", caller_run, context, where
        )
    return _Call(findings, activated, "attached", caller_run, None, caller_run, context, where)


def _attachment(summary: Mapping[str, Any], call: _Call, at: Location) -> list[_Finding]:
    """Le rattachement que dit le résumé, comparé à celui que le contexte reçu commande."""
    findings: list[_Finding] = []
    if call.caller_run is not None and summary.get("runId") == call.caller_run:
        findings.append(
            _Finding("summary_attachment_wrong", "exchange", (*at, "runId"), "run_id_reused")
        )
    said = summary.get("attachment")
    if said not in ("attached", "missing", "rejected"):
        # Une valeur hors de la liste relève du schéma.
        return findings
    if said != call.attachment:
        expected = f"expected_{call.attachment}"
        findings.append(
            _Finding("summary_attachment_wrong", "exchange", (*at, "attachment"), expected)
        )
        return findings
    parent, rejection = summary.get("parentRunId"), summary.get("rejection")
    if said == "attached" and isinstance(parent, str) and parent != call.parent:
        findings.append(
            _Finding("summary_attachment_wrong", "exchange", (*at, "parentRunId"), "wrong_parent")
        )
    if said == "rejected" and isinstance(rejection, str) and rejection != call.rejection:
        findings.append(
            _Finding("summary_attachment_wrong", "exchange", (*at, "rejection"), "wrong_rejection")
        )
    return findings


def _whole(value: object) -> int | None:
    """Un entier JSON, écrit ``3`` ou ``3.0`` : JSON Schema ne les distingue pas."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value) if float(value).is_integer() else None


def _consistency(summary: Mapping[str, Any], at: Location) -> list[_Finding]:
    """Ce que le résumé dit de lui-même : chaque jeu une fois, compte et troncature justes."""
    findings: list[_Finding] = []
    datasets = items(summary.get("datasets"))
    seen: set[tuple[str, str, str | None]] = set()
    for index, dataset in enumerate(datasets):
        fields = mapping(dataset)
        namespace, name, version = (
            fields.get("namespace"),
            fields.get("name"),
            fields.get("version"),
        )
        if not (isinstance(namespace, str) and isinstance(name, str)):
            continue
        identity = (namespace, name, version if isinstance(version, str) else None)
        if identity in seen:
            where = (*at, "datasets", index)
            findings.append(_Finding("summary_inconsistent", "exchange", where, "dataset_repeated"))
        seen.add(identity)
    count = _whole(summary.get("datasetCount"))
    if count is None:
        return findings
    if count < len(datasets):
        where = (*at, "datasetCount")
        findings.append(_Finding("summary_inconsistent", "exchange", where, "count_below_listed"))
    truncated = summary.get("truncated")
    if isinstance(truncated, bool) and truncated != (count > len(datasets)):
        where = (*at, "truncated")
        findings.append(_Finding("summary_inconsistent", "exchange", where, "truncated_mismatch"))
    return findings


@dataclass(frozen=True)
class _Response:
    findings: list[_Finding]
    # Le résumé, et son emplacement, quand la réponse en porte un.
    summary: object = None
    at: Location | None = None


def _datasets(summary: Mapping[str, Any], at: Location) -> list[_Finding]:
    """Les noms des jeux de données du résumé, à la règle des identifiants de l'ingestion."""
    bounds = (
        ("namespace", identifiers.NAMESPACE_MAX),
        ("name", identifiers.DATASET_NAME_MAX),
        ("version", identifiers.VERSION_MAX),
    )
    return [
        finding
        for index, dataset in enumerate(items(summary.get("datasets")))
        for name, maximum in bounds
        for finding in _identifier(
            mapping(dataset).get(name), maximum, "exchange", (*at, "datasets", index, name)
        )
    ]


def _response(exchange: Mapping[str, Any], call: _Call) -> _Response:
    """La réponse : écho de l'extension, résumé, et ce qu'il dit."""
    response = mapping(exchange.get("response"))
    findings, echoed = _parameters(response, "response")
    result = mapping(response.get("result"))
    kind = next((name for name in ("task", "message") if isinstance(result.get(name), Mapping)), "")
    holder = mapping(result.get(kind))
    holder_at: Location = ("response", "result", kind) if kind else ("response", "result")
    metadata = mapping(holder.get("metadata"))
    listed = items(holder.get("extensions"))
    findings.extend(_versions(listed, metadata, holder_at))
    if not call.activated:
        # Une extension que l'appelant n'a pas activée, l'appelé n'en dit rien.
        return _Response(findings)
    if not echoed:
        findings.append(
            _Finding("extension_not_echoed", "exchange", ("response", "serviceParameters"))
        )
    if URI not in metadata:
        where = (*holder_at, "metadata") if kind else holder_at
        findings.append(_Finding("summary_missing", "exchange", where))
        return _Response(findings)
    at: Location = (*holder_at, "metadata", URI)
    summary = metadata[URI]
    if kind == "message" and URI not in listed:
        findings.append(_Finding("summary_not_listed", "exchange", (*holder_at, "extensions")))
    findings.extend(
        _violations("summary_schema_violation", "LineageSummary", summary, "exchange", at)
    )
    fields = mapping(summary)
    findings.extend(_datasets(fields, at))
    findings.extend(_attachment(fields, call, at))
    findings.extend(_consistency(fields, at))
    return _Response(findings, summary, at)


def _leaves(value: object, at: Location) -> Iterator[tuple[Location, str]]:
    """Les textes d'un document, clés comprises, avec leur emplacement.

    Le parcours tient sa propre pile : un document très imbriqué ne l'arrête pas.
    """
    pending: list[tuple[Location, object]] = [(at, value)]
    while pending:
        location, current = pending.pop()
        if isinstance(current, dict):
            for key, inside in current.items():
                yield (*location, key), key
                pending.append(((*location, key), inside))
        elif isinstance(current, list):
            pending.extend(((*location, index), inside) for index, inside in enumerate(current))
        elif isinstance(current, str):
            yield location, current


def _exchanged(exchange: Mapping[str, Any]) -> frozenset[str]:
    """Les textes que l'échange porte dans ses parties : la question, la réponse, les artefacts."""
    result = mapping(mapping(exchange.get("response")).get("result"))
    task = mapping(result.get("task"))
    holders = [
        mapping(mapping(mapping(exchange.get("request")).get("params")).get("message")),
        mapping(result.get("message")),
        mapping(mapping(task.get("status")).get("message")),
        *(mapping(message) for message in items(task.get("history"))),
        *(mapping(artifact) for artifact in items(task.get("artifacts"))),
    ]
    texts = set()
    for holder in holders:
        for part in items(holder.get("parts")):
            fields = mapping(part)
            for name in ("text", "data"):
                for _, text in _leaves(fields.get(name), ()):
                    if len(text.strip()) >= MIN_CONTENT:
                        texts.add(text.strip())
    return frozenset(texts)


def _content(texts: frozenset[str], documents: list[tuple[Location, object]]) -> list[_Finding]:
    """Un texte échangé retrouvé tel quel dans les données de l'extension."""
    return [
        _Finding("content_in_extension", "exchange", location)
        for at, document in documents
        for location, text in _leaves(document, at)
        if any(exchanged in text for exchanged in texts)
    ]


def judge(
    card: object,
    exchange: Mapping[str, Any],
    *,
    card_file: str = "card",
    exchange_file: str = "exchange",
) -> Judgement:
    """Juge une Agent Card et un échange capturé. Les mêmes entrées donnent le même jugement."""
    call = _call(exchange)
    answered = _response(exchange, call)
    texts = _exchanged(exchange)
    documents = [
        (at, document)
        for at, document in ((call.at, call.context), (answered.at, answered.summary))
        if at is not None
    ]
    findings = [
        *_card(card),
        *call.findings,
        *answered.findings,
        *_content(texts, documents),
    ]
    names: dict[FileKind, str] = {"card": card_file, "exchange": exchange_file}
    masker = Masker(hidden=lambda key: any(text in key for text in texts))
    problems = {
        (
            _FILES.index(finding.file),
            Problem(
                code=finding.code,
                group=BY_CODE[finding.code].group,
                message=BY_CODE[finding.code].message,
                file=names[finding.file],
                pointer=masker.pointer(finding.location),
                reason=finding.reason,
            ),
        )
        for finding in findings
    }
    ordered = [
        problem
        for _, problem in sorted(
            problems,
            key=lambda item: (
                GROUPS.index(item[1].group),
                item[0],
                item[1].pointer,
                item[1].code,
                item[1].reason or "",
            ),
        )
    ]
    return Judgement(
        problems=tuple(ordered[:MAX_PROBLEMS]),
        counts=dict(Counter(problem.code for problem in ordered)),
    )
