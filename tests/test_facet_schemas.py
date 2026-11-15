"""Les schémas des facettes propres : publiés à l'adresse que les émetteurs écrivent, et justes.

Chaque ``_schemaURL`` émis désigne un fichier du dépôt, et chaque facette réellement émise
(proxy, entreprise synthétique, scénarios de vérité) respecte le schéma qu'elle annonce. Les
références vers le schéma d'OpenLineage sont résolues depuis la copie locale : aucun accès au
réseau.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from answerlineage_api import identifiers
from answerlineage_api.ingest.models import RECIPIENTS_MAX
from answerlineage_corpus.enterprise import generate
from answerlineage_corpus.events import render
from answerlineage_corpus.scenarios import load_corpus
from answerlineage_proxy.events import build_tool_call_event
from answerlineage_proxy.inspection import Inspection
from answerlineage_proxy.sql import SqlAnalysis, TableRead
from answerlineage_proxy.tracecontext import parse_traceparent, resolve_trace
from api.tests.events import OFFICIAL_SCHEMA

ROOT = Path(__file__).resolve().parents[1]
FACETS = ROOT / "spec" / "facets"
RAW = "https://raw.githubusercontent.com/alebgl77/AnswerLineage/main/"
PUBLISHED = RAW + "spec/facets/"
OWN_PREFIX = "answerlineage_"
PRODUCER = "https://github.com/alebgl77/AnswerLineage/tree/0.0.0/test"

NAMES = {
    "answerlineage_agent": "AgentRunFacet",
    "answerlineage_toolCall": "ToolCallRunFacet",
    "answerlineage_qos": "QosRunFacet",
    "answerlineage_delivery": "DeliveryOutputDatasetFacet",
    "answerlineage_keys": "KeysInputDatasetFacet",
}


def schema_files() -> list[Path]:
    return sorted(FACETS.glob("*/*.json"))


def load(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def registry() -> Registry[Any]:
    """Le schéma d'OpenLineage et les schémas des facettes, indexés par leur ``$id``."""
    documents = [json.loads(OFFICIAL_SCHEMA.read_text(encoding="utf-8"))]
    documents.extend(load(path) for path in schema_files())
    return Registry().with_resources(
        (document["$id"], Resource.from_contents(document, default_specification=DRAFT202012))
        for document in documents
    )


REGISTRY = registry()


def address(name: str, version: str = "1-0-0") -> str:
    return f"{PUBLISHED}{version}/{name}.json#/$defs/{name}"


def validator(name: str, version: str = "1-0-0") -> Draft202012Validator:
    """Valide une facette contre la définition que son ``_schemaURL`` désigne."""
    return Draft202012Validator(
        {"$ref": address(name, version)}, registry=REGISTRY, format_checker=FormatChecker()
    )


def facet(name: str, version: str = "1-0-0", **fields: Any) -> dict[str, Any]:
    return {"_producer": PRODUCER, "_schemaURL": address(name, version), **fields}


def test_there_are_five_facets_published_in_version_1_0_0() -> None:
    assert {path.relative_to(FACETS).as_posix() for path in schema_files()} == {
        f"1-0-0/{name}.json" for name in NAMES.values()
    }


@pytest.mark.parametrize("path", schema_files(), ids=lambda path: path.stem)
def test_a_schema_is_a_valid_json_schema_at_the_address_it_announces(path: Path) -> None:
    document = load(path)
    Draft202012Validator.check_schema(document)
    assert document["$id"] == f"{PUBLISHED}{path.parent.name}/{path.name}"
    ((key, target),) = document["properties"].items()
    assert NAMES[key] == path.stem
    assert target == {"$ref": f"#/$defs/{path.stem}"}


def test_the_names_of_the_properties_never_suggest_content() -> None:
    """Aucune facette ne porte de contenu : seulement des noms, des empreintes, des tailles."""
    content = re.compile(r"prompt|argument|result|response|answer|content|text|query|sql", re.I)
    names = {
        name
        for path in schema_files()
        for definition in load(path)["$defs"].values()
        for part in definition.get("allOf", [])
        for name in part.get("properties", {})
    }
    assert names
    for name in names:
        assert not content.search(name) or name.endswith(("Fingerprint", "Size")), name


EXAMPLES: dict[str, dict[str, Any]] = {
    "AgentRunFacet": {
        "minimal": {},
        "full": {
            "agentId": "analyste-ventes",
            "framework": "langgraph",
            "agentVersion": "2.4.1",
            "owner": "equipe-donnees",
            "taskClass": "analyse",
        },
    },
    "ToolCallRunFacet": {
        "minimal": {"server": "entrepot"},
        "full": {
            "server": "entrepot",
            "tool": "execute_sql",
            "transport": "http",
            "protocolVersion": "2026-07-28",
            "httpStatus": 200,
            "proxyError": "upstream_interrupted",
            "headerMismatch": ["mcp-name"],
            "invalidToolName": False,
            "readResolution": "unresolved",
            "unresolvedReason": "too_large",
            "argumentsFingerprint": "sha256:" + "a" * 64,
            "readOnly": True,
            "resultFingerprint": "sha256:" + "b" * 64,
            "resultSize": 2048,
            "definitionFingerprint": "sha256:" + "c" * 64,
            "traceId": "4bf92f3577b34da6a3ce929d0e0e4736",
            "spanId": "00f067aa0ba902b7",
            "traceOrigin": "meta",
        },
    },
    "QosRunFacet": {
        "minimal": {},
        "full": {
            "model": "modele-a",
            "route": "passerelle-interne",
            "inputTokens": 1200,
            "outputTokens": 340,
            "latencyMs": 812.5,
            "estimatedCost": 0.0123,
            "errorCount": 1,
            "retryCount": 2,
            "skill": "resume-ventes",
        },
    },
    "DeliveryOutputDatasetFacet": {
        "minimal": {"fingerprint": "sha256:" + "d" * 64, "channel": "mail", "messageRef": "m-1"},
        "full": {
            "fingerprint": "sha256:" + "d" * 64,
            "channel": "mail",
            "messageRef": "m-1",
            "recipients": ["0" * 32, "1" * 32],
        },
    },
    "KeysInputDatasetFacet": {
        "minimal": {"hmacs": ["e" * 64]},
        "full": {"hmacs": ["e" * 64], "bloomFilter": "AAECAwQF"},
    },
}

FAULTY: dict[str, dict[str, dict[str, Any]]] = {
    "AgentRunFacet": {
        "identifiant qui n'est pas un texte": {"agentId": 3},
        "responsable en liste": {"owner": ["a"]},
        "framework vide": {"framework": ""},
    },
    "ToolCallRunFacet": {
        "serveur absent": {"tool": "execute_sql"},
        "statut HTTP en texte": {"server": "s", "httpStatus": "200"},
        "statut HTTP hors plage": {"server": "s", "httpStatus": 99},
        "nom d'outil invalide en texte": {"server": "s", "invalidToolName": "oui"},
        "résolution inconnue": {"server": "s", "readResolution": "peut-être"},
        "en-têtes qui ne sont pas des textes": {"server": "s", "headerMismatch": [1]},
        "taille négative": {"server": "s", "resultSize": -1},
        "lecture seule en texte": {"server": "s", "readOnly": "true"},
        "trace en majuscules": {"server": "s", "traceId": "4BF92F3577B34DA6A3CE929D0E0E4736"},
        "trace trop courte": {"server": "s", "traceId": "4bf92f3577b34da6a3ce929d0e0e473"},
        "trace nulle": {"server": "s", "traceId": "0" * 32},
        "span trop long": {"server": "s", "spanId": "00f067aa0ba902b7a"},
        "span nul": {"server": "s", "spanId": "0" * 16},
        "span non hexadécimal": {"server": "s", "spanId": "00f067aa0ba902bz"},
        "origine de la trace inconnue": {"server": "s", "traceOrigin": "devinee"},
    },
    "QosRunFacet": {
        "jetons en texte": {"inputTokens": "12"},
        "jetons négatifs": {"outputTokens": -1},
        "latence en texte": {"latencyMs": "vite"},
        "reprises fractionnaires": {"retryCount": 1.5},
    },
    "DeliveryOutputDatasetFacet": {
        "empreinte absente": {"channel": "mail", "messageRef": "m-1"},
        "canal absent": {"fingerprint": "sha256:x", "messageRef": "m-1"},
        "référence du message absente": {"fingerprint": "sha256:x", "channel": "mail"},
        "destinataires en texte": {
            "fingerprint": "sha256:x",
            "channel": "mail",
            "messageRef": "m-1",
            "recipients": "a",
        },
        "destinataire qui n'est pas un texte": {
            "fingerprint": "sha256:x",
            "channel": "mail",
            "messageRef": "m-1",
            "recipients": [1],
        },
    },
    "KeysInputDatasetFacet": {
        "ni empreintes ni filtre": {},
        "empreintes en texte": {"hmacs": "abc"},
        "empreinte qui n'est pas un texte": {"hmacs": [1]},
        "filtre qui n'est pas un texte": {"bloomFilter": 12},
    },
}


@pytest.mark.parametrize(
    ("name", "kind"),
    [(name, kind) for name, kinds in EXAMPLES.items() for kind in kinds],
)
def test_an_example_the_schema_documents_is_accepted(name: str, kind: str) -> None:
    assert list(validator(name).iter_errors(facet(name, **EXAMPLES[name][kind]))) == []


@pytest.mark.parametrize(
    ("name", "case"),
    [(name, case) for name, cases in FAULTY.items() for case in cases],
)
def test_a_faulty_example_is_refused(name: str, case: str) -> None:
    assert list(validator(name).iter_errors(facet(name, **FAULTY[name][case]))) != []


@pytest.mark.parametrize("name", list(NAMES.values()))
@pytest.mark.parametrize("missing", ["_producer", "_schemaURL"])
def test_the_base_facet_of_openlineage_is_required(name: str, missing: str) -> None:
    complete = facet(name, **EXAMPLES[name]["minimal"])
    assert list(validator(name).iter_errors(complete)) == []
    del complete[missing]
    assert list(validator(name).iter_errors(complete)) != []


BOUNDED = [
    ("ToolCallRunFacet", "server", identifiers.TOOL_MAX),
    ("ToolCallRunFacet", "tool", identifiers.TOOL_MAX),
    ("DeliveryOutputDatasetFacet", "fingerprint", identifiers.FINGERPRINT_MAX),
    ("DeliveryOutputDatasetFacet", "channel", identifiers.CHANNEL_MAX),
    ("DeliveryOutputDatasetFacet", "messageRef", identifiers.MESSAGE_REF_MAX),
]


@pytest.mark.parametrize(("name", "field", "bound"), BOUNDED)
def test_a_schema_bounds_an_identifier_where_ingestion_does(
    name: str, field: str, bound: int
) -> None:
    """Qui respecte le schéma publié n'est pas rejeté pour une longueur, et inversement."""
    minimal = EXAMPLES[name]["minimal"]
    at_the_bound = facet(name, **{**minimal, field: "a" * bound})
    beyond = facet(name, **{**minimal, field: "a" * (bound + 1)})
    assert list(validator(name).iter_errors(at_the_bound)) == []
    assert list(validator(name).iter_errors(beyond)) != []


def test_a_schema_bounds_a_recipient_where_ingestion_does() -> None:
    name = "DeliveryOutputDatasetFacet"
    minimal = EXAMPLES[name]["minimal"]
    at_the_bound = facet(name, **{**minimal, "recipients": ["a" * identifiers.RECIPIENT_MAX]})
    beyond = facet(name, **{**minimal, "recipients": ["a" * (identifiers.RECIPIENT_MAX + 1)]})
    assert list(validator(name).iter_errors(at_the_bound)) == []
    assert list(validator(name).iter_errors(beyond)) != []


def test_a_schema_bounds_the_number_of_recipients_where_ingestion_does() -> None:
    name = "DeliveryOutputDatasetFacet"
    minimal = EXAMPLES[name]["minimal"]
    at_the_bound = facet(name, **{**minimal, "recipients": ["a"] * RECIPIENTS_MAX})
    beyond = facet(name, **{**minimal, "recipients": ["a"] * (RECIPIENTS_MAX + 1)})
    assert list(validator(name).iter_errors(at_the_bound)) == []
    assert list(validator(name).iter_errors(beyond)) != []


def test_a_field_the_schema_does_not_know_is_accepted() -> None:
    """Un schéma s'étend : une version plus récente peut ajouter un champ."""
    assert list(validator("QosRunFacet").iter_errors(facet("QosRunFacet", futur=1))) == []


def own_facets(event: Mapping[str, Any]) -> Iterator[tuple[str, Mapping[str, Any]]]:
    """Les facettes propres d'un événement, avec leur clé, sur le run et sur les jeux de données."""
    owners: list[Mapping[str, Any]] = [event["run"]]
    owners.extend(event.get("inputs", []))
    owners.extend(event.get("outputs", []))
    for owner in owners:
        for container in ("facets", "inputFacets", "outputFacets"):
            for key, value in owner.get(container, {}).items():
                if key.startswith(OWN_PREFIX):
                    yield key, value


def proxy_events() -> list[dict[str, Any]]:
    """Ce que le proxy émet : appels réussis, refusés, en erreur, lectures résolues ou non."""
    inspections = [
        Inspection(is_tool_call=True, tool="execute_sql", protocol_version="2026-07-28"),
        Inspection(is_tool_call=True, invalid_tool_name=True),
        Inspection(
            is_tool_call=True, tool="get_weather", header_mismatches=("mcp-name", "mcp-method")
        ),
    ]
    events = [
        build_tool_call_event(
            run_id=uuid4(),
            event_time=datetime.now(UTC),
            outcome="COMPLETE",
            server="entrepot",
            inspection=inspection,
            http_status=200,
        )
        for inspection in inspections
    ]
    # Le contexte de trace de l'appel : dans _meta, dans l'en-tête, ou créé par le proxy.
    received = parse_traceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
    traces = [
        resolve_trace(received, []),
        resolve_trace(None, ["00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"]),
        resolve_trace(None, []),
    ]
    events.extend(
        build_tool_call_event(
            run_id=trace.run_id,
            event_time=datetime.now(UTC),
            outcome="COMPLETE",
            server="entrepot",
            inspection=inspections[0],
            http_status=200,
            trace=trace,
        )
        for trace in traces
    )
    failed = build_tool_call_event(
        run_id=uuid4(),
        event_time=datetime.now(UTC),
        outcome="FAIL",
        server="entrepot",
        inspection=inspections[0],
        proxy_error="upstream_unreachable",
    )
    resolved = build_tool_call_event(
        run_id=uuid4(),
        event_time=datetime.now(UTC),
        outcome="COMPLETE",
        server="entrepot",
        inspection=inspections[0],
        http_status=200,
        reads=SqlAnalysis(
            resolved=True,
            tables=(TableRead(name="crm.clients", precision="columns", columns=("nom",)),),
        ),
        namespace="bigquery",
    )
    unresolved = build_tool_call_event(
        run_id=uuid4(),
        event_time=datetime.now(UTC),
        outcome="COMPLETE",
        server="entrepot",
        inspection=inspections[0],
        http_status=200,
        reads=SqlAnalysis(resolved=False, reason="too_large"),
        namespace="bigquery",
    )
    return [*events, failed, resolved, unresolved]


def emitted_events() -> list[dict[str, Any]]:
    events = proxy_events()
    events.extend(render(generate(7)))
    events.extend(event for scenario in load_corpus() for event in scenario.events())
    return events


EMITTED = emitted_events()


def emitted_addresses() -> set[str]:
    return {facet["_schemaURL"] for event in EMITTED for _, facet in own_facets(event)}


def test_every_kind_of_facet_that_is_emitted_today_is_covered() -> None:
    keys = {key for event in EMITTED for key, _ in own_facets(event)}
    assert keys == {"answerlineage_agent", "answerlineage_toolCall", "answerlineage_delivery"}


@pytest.mark.parametrize("url", sorted(emitted_addresses()))
def test_a_schema_url_emitted_leads_to_the_exact_file_of_that_version(url: str) -> None:
    assert url.startswith(PUBLISHED), "l'adresse d'un émetteur est celle du fichier brut du dépôt"
    file_address, _, pointer = url.partition("#")
    path = ROOT / file_address.removeprefix(RAW)
    assert path.is_file()
    document = load(path)
    assert document["$id"] == file_address
    name = path.stem
    assert pointer == f"/$defs/{name}"
    assert name in document["$defs"]
    assert path.parent.name in url, "la version est dans l'adresse"


def test_no_emitter_still_points_at_a_web_page_instead_of_a_schema() -> None:
    assert all("github.com" not in url for url in emitted_addresses())


def test_every_emitted_facet_is_announced_under_its_own_key_and_respects_its_schema() -> None:
    checked = 0
    for event in EMITTED:
        for key, emitted in own_facets(event):
            name = NAMES[key]
            document = load(next(path for path in schema_files() if path.stem == name))
            assert emitted["_schemaURL"] == address(name)
            assert key in document["properties"]
            assert list(validator(name).iter_errors(emitted)) == []
            checked += 1
    assert checked > 1000
