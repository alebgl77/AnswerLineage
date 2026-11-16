"""Des règles de lecture décrivent les outils non SQL ; un outil sans règle est à décrire."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from answerlineage_proxy import cli
from answerlineage_proxy.app import McpProxy
from answerlineage_proxy.config import ProxyConfigurationError, ProxySettings, SqlTool
from answerlineage_proxy.events import KEYS_FACET_SCHEMA, TOOL_CALL_FACET_SCHEMA_READ_KEYS
from answerlineage_proxy.fingerprint import FingerprintKey
from answerlineage_proxy.rules import KEY_LABEL, ReadRule, parse_read_rules
from proxy.tests.conftest import BenchFactory, exchange, tool_call
from proxy.tests.test_relay import eventually

pytestmark = pytest.mark.anyio

KEY = FingerprintKey(bytes(range(32)))
CANARY = "canari-produit-4b8d"
RULES_FILE = """\
version: 1
rules:
  - tool: get_price
    datasets:
      - namespace: postgres://referentiel:5432
        name: ref.tarifs
        columns: [produit, prix]
      - {namespace: postgres://referentiel:5432, name: ref.devises}
    key: produit
  - tool: get_weather
    server: autre-serveur
    datasets: [{namespace: api://meteo, name: previsions}]
"""
RULES = parse_read_rules(RULES_FILE)


def call_facet(event: Mapping[str, Any]) -> dict[str, Any]:
    facet: dict[str, Any] = event["run"]["facets"]["answerlineage_toolCall"]
    return facet


def keys_of(dataset: Mapping[str, Any]) -> list[str]:
    facet = dataset.get("inputFacets", {}).get("answerlineage_keys")
    if facet is None:
        return []
    assert facet["_schemaURL"] == KEYS_FACET_SCHEMA
    hmacs: list[str] = facet["hmacs"]
    return hmacs


async def test_a_rule_records_the_read_of_its_datasets_with_the_key_read(
    bench: BenchFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """La règle associe l'argument produit à ref.tarifs : la lecture porte la clé lue."""
    body = tool_call("get_price", {"produit": CANARY, "devise": "EUR"})
    async with bench(fingerprint_key=KEY, read_rules=RULES) as b:
        with caplog.at_level(logging.DEBUG):
            proxied = await exchange("POST", b.proxy_url + "/mcp", content=body)
        (event,) = b.sink.events
    assert proxied.status == 200
    facet = call_facet(event)
    assert (facet["readResolution"], facet["readKeys"]) == ("resolved", "fingerprinted")
    assert facet["_schemaURL"] == TOOL_CALL_FACET_SCHEMA_READ_KEYS
    tarifs, devises = event["inputs"]
    assert (tarifs["namespace"], tarifs["name"]) == ("postgres://referentiel:5432", "ref.tarifs")
    assert [field["name"] for field in tarifs["facets"]["schema"]["fields"]] == ["produit", "prix"]
    expected = KEY.fingerprint(KEY_LABEL, json.dumps(CANARY).encode())
    assert keys_of(tarifs) == keys_of(devises) == [expected]
    assert "facets" not in devises
    # La clé n'est que dans son empreinte : ni l'événement ni le journal ne la citent.
    assert CANARY not in json.dumps(event) + caplog.text
    assert b.proxy.stats.undescribed_calls == 0


async def test_without_a_tenant_key_the_read_is_recorded_for_the_dataset_and_said(
    bench: BenchFactory,
) -> None:
    async with bench(read_rules=RULES) as b:
        await exchange("POST", b.proxy_url + "/mcp", content=tool_call("get_price", {"produit": 1}))
        (event,) = b.sink.events
    assert call_facet(event)["readKeys"] == "no_fingerprint_key"
    assert [dataset["name"] for dataset in event["inputs"]] == ["ref.tarifs", "ref.devises"]
    assert all(keys_of(dataset) == [] for dataset in event["inputs"])


async def test_a_call_whose_body_was_not_read_has_no_key(bench: BenchFactory) -> None:
    body = tool_call("get_price", {"produit": "P-1"}) + b" " * 2000
    async with bench(fingerprint_key=KEY, read_rules=RULES, max_inspected_bytes=100) as b:
        await exchange("POST", b.proxy_url + "/mcp", content=body)
        (event,) = b.sink.events
    assert call_facet(event)["readKeys"] == "request_too_large"
    assert len(event["inputs"]) == 2


async def test_a_failed_call_read_nothing(bench: BenchFactory) -> None:
    async with bench(fingerprint_key=KEY, read_rules=RULES) as b:
        await exchange("POST", b.proxy_url + "/fail", content=tool_call("get_price", {"p": 1}))
        (event,) = b.sink.events
    assert event["eventType"] == "FAIL"
    assert event["inputs"] == []
    assert "readResolution" not in call_facet(event)


async def test_a_tool_without_rule_is_marked_counted_and_logged_once_by_its_name(
    bench: BenchFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """La règle de get_weather vaut pour un autre serveur : ici, l'outil est à décrire."""
    async with bench(fingerprint_key=KEY, read_rules=RULES) as b:
        with caplog.at_level(logging.WARNING, logger="answerlineage_proxy"):
            for location in ("Lyon", CANARY, "Lille"):
                body = tool_call(arguments={"location": location})
                await exchange("POST", b.proxy_url + "/mcp", content=body)
            await exchange("POST", b.proxy_url + "/mcp", content=tool_call("get_time", {}))
        events = b.sink.events
        stats = b.proxy.stats
    assert [
        (call_facet(event)["readResolution"], call_facet(event)["unresolvedReason"])
        for event in events
    ] == [("unresolved", "undescribed_tool")] * 4
    assert all(event["inputs"] == [] for event in events)
    assert stats.undescribed_calls == 4
    logged = [record.getMessage() for record in caplog.records if "sans règle" in record.message]
    assert logged == [
        "Outil « get_weather » sans règle de lecture : ses lectures ne sont pas décrites.",
        "Outil « get_time » sans règle de lecture : ses lectures ne sont pas décrites.",
    ]
    assert CANARY not in caplog.text + json.dumps(events)


async def test_a_tool_with_an_invalid_name_is_flagged_not_listed(bench: BenchFactory) -> None:
    async with bench(read_rules=RULES) as b:
        await exchange("POST", b.proxy_url + "/mcp", content=tool_call("nom invalide"))
        (event,) = b.sink.events
    assert call_facet(event)["invalidToolName"] is True
    assert "readResolution" not in call_facet(event)
    assert b.proxy.stats.undescribed_calls == 0


async def test_a_declared_sql_tool_keeps_the_analysis_of_its_query(bench: BenchFactory) -> None:
    rules = parse_read_rules(
        "version: 1\nrules: [{tool: execute_sql, datasets: [{namespace: a, name: b}]}]\n"
    )
    sql = {"execute_sql": SqlTool("query", "bigquery", "bigquery")}
    body = tool_call("execute_sql", {"query": "SELECT c.nom FROM crm.clients c"})
    async with bench(sql_tools=sql, read_rules=rules, sql_budget_ms=60_000) as b:
        await exchange("POST", b.proxy_url + "/mcp", content=body)
        await b.proxy.drain()
        (event,) = b.sink.events
    assert [dataset["name"] for dataset in event["inputs"]] == ["crm.clients"]
    assert "readKeys" not in call_facet(event)


async def test_reads_that_cannot_be_established_never_reach_the_call(
    bench: BenchFactory, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("règle en panne")

    monkeypatch.setattr("answerlineage_proxy.app.declared_reads", broken)
    async with bench(fingerprint_key=KEY, read_rules=RULES) as b:
        with caplog.at_level(logging.ERROR, logger="answerlineage_proxy"):
            proxied = await exchange("POST", b.proxy_url + "/mcp", content=tool_call("get_price"))
        await eventually(lambda: len(b.sink.events) == 1)
        (event,) = b.sink.events
    assert proxied.status == 200
    assert event["inputs"] == []
    assert "Lectures de l'appel non établies (RuntimeError)" in caplog.text


def test_the_rules_that_apply_are_those_of_this_server() -> None:
    here = ProxySettings.create("http://127.0.0.1:9000", "autre-serveur", read_rules=RULES)
    elsewhere = ProxySettings.create("http://127.0.0.1:9000", "meteo", read_rules=RULES)
    assert set(here.read_rules) == {"get_price", "get_weather"}
    assert set(elsewhere.read_rules) == {"get_price"}
    assert isinstance(here.read_rules["get_weather"], ReadRule)


@pytest.fixture
def rules_file(tmp_path: Path) -> Path:
    path = tmp_path / "regles.yaml"
    path.write_text(RULES_FILE, encoding="utf-8")
    return path


def test_rules_come_from_the_command_line_or_the_environment(rules_file: Path) -> None:
    upstream = ["--upstream", "http://127.0.0.1:9000", "--server-name", "meteo"]
    from_option = cli.settings_from_args([*upstream, "--read-rules-file", str(rules_file)], {})
    assert set(from_option.read_rules) == {"get_price"}
    environment = {"ANSWERLINEAGE_PROXY_READ_RULES_FILE": str(rules_file)}
    assert cli.settings_from_args(upstream, environment).read_rules == from_option.read_rules
    environment["ANSWERLINEAGE_PROXY_UPSTREAM"] = "http://127.0.0.1:9000"
    from_environment = ProxySettings.from_environment(environment)
    assert set(from_environment.read_rules) == {"get_price"}
    assert cli.settings_from_args(upstream, {}).read_rules == {}


def test_an_invalid_rule_stops_the_proxy_without_echoing_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "regles.yaml"
    path.write_text(
        f"version: 1\nrules:\n  - tool: t\n    datasets: [{{namespace: a, name: '{CANARY} x'}}]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("uvicorn.run", lambda app, **options: pytest.fail("démarré"))
    with pytest.raises(ProxyConfigurationError, match="Règles de lecture invalides : règle 1"):
        cli.settings_from_args(["--upstream", "http://h", "--read-rules-file", str(path)], {})
    assert cli.main(["--upstream", "http://h", "--read-rules-file", str(path)]) == 2
    error = capsys.readouterr().err
    assert error.startswith("Configuration invalide : Règles de lecture invalides : règle 1, ")
    assert "(identifier_whitespace)" in error
    assert CANARY not in error
    assert str(path) not in error


def test_the_proxy_says_at_start_which_rules_it_ignores_for_a_sql_tool(
    rules_file: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    served: dict[str, Any] = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **options: served.update(app=app))
    arguments = [
        "--upstream",
        "http://127.0.0.1:9000",
        "--read-rules-file",
        str(rules_file),
        "--sql-tool",
        "get_price=query:postgres",
    ]
    with caplog.at_level(logging.INFO, logger="answerlineage_proxy"):
        assert cli.main(arguments) == 0
    assert isinstance(served["app"], McpProxy)
    assert "La règle de lecture de l'outil « get_price » est ignorée" in caplog.text
    assert "Règles de lecture : 1 outils décrits." in caplog.text


async def test_the_log_of_tools_to_describe_is_bounded(
    bench: BenchFactory, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("answerlineage_proxy.app._UNDESCRIBED_LOGGED", 1)
    async with bench() as b:
        with caplog.at_level(logging.WARNING, logger="answerlineage_proxy"):
            for tool in ("outil_a", "outil_b", "outil_a"):
                await exchange("POST", b.proxy_url + "/mcp", content=tool_call(tool))
        assert b.proxy.stats.undescribed_calls == 3
    assert "outil_a" in caplog.text
    assert "outil_b" not in caplog.text
