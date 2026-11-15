"""Événements, files, réglages et ligne de commande du proxy."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytest

from answerlineage_proxy import cli
from answerlineage_proxy.app import McpProxy, end_to_end_headers, quiet_transport_logs
from answerlineage_proxy.config import DEFAULT_PORT, ProxyConfigurationError, ProxySettings
from answerlineage_proxy.events import RUN_EVENT_SCHEMA, UNRESOLVED_TOOL, build_tool_call_event
from answerlineage_proxy.inspection import Inspection
from answerlineage_proxy.sink import MemorySink

RUN_ID = UUID("0193a9c5-7a10-7c3e-9d5e-2f1b6c8a4e77")
MOMENT = datetime(2026, 11, 16, 10, 30, 0, 123456, tzinfo=timezone(timedelta(hours=1)))


def test_event_describes_the_tool_call_without_any_content() -> None:
    inspection = Inspection(is_tool_call=True, tool="execute_sql", protocol_version="2026-07-28")
    event = build_tool_call_event(
        run_id=RUN_ID,
        event_time=MOMENT,
        outcome="COMPLETE",
        server="entrepot",
        inspection=inspection,
        http_status=200,
    )
    assert event["eventType"] == "COMPLETE"
    assert event["eventTime"] == "2026-11-16T09:30:00.123456Z"
    assert event["schemaURL"] == RUN_EVENT_SCHEMA
    assert event["run"]["runId"] == str(RUN_ID)
    assert event["job"] == {"namespace": "mcp://entrepot", "name": "execute_sql"}
    assert (event["inputs"], event["outputs"]) == ([], [])
    facet = event["run"]["facets"]["answerlineage_toolCall"]
    assert facet["_producer"] == event["producer"]
    assert facet["_schemaURL"].startswith("https://")
    assert {key: value for key, value in facet.items() if not key.startswith("_")} == {
        "server": "entrepot",
        "tool": "execute_sql",
        "transport": "http",
        "protocolVersion": "2026-07-28",
        "httpStatus": 200,
        "headerMismatch": [],
        "invalidToolName": False,
    }
    json.dumps(event)


def test_event_of_a_failed_call_with_an_invalid_name() -> None:
    inspection = Inspection(
        is_tool_call=True,
        invalid_tool_name=True,
        header_mismatches=("mcp-name", "mcp-method"),
    )
    event = build_tool_call_event(
        run_id=RUN_ID,
        event_time=MOMENT,
        outcome="FAIL",
        server="entrepot",
        inspection=inspection,
        proxy_error="upstream_unreachable",
    )
    facet = event["run"]["facets"]["answerlineage_toolCall"]
    assert event["job"]["name"] == UNRESOLVED_TOOL
    assert "tool" not in facet
    assert "httpStatus" not in facet
    assert "protocolVersion" not in facet
    assert facet["invalidToolName"] is True
    assert facet["headerMismatch"] == ["mcp-method", "mcp-name"]
    assert facet["proxyError"] == "upstream_unreachable"


def test_memory_sink_drops_the_oldest_event_when_full() -> None:
    sink = MemorySink(capacity=2)
    for index in range(5):
        sink.emit({"n": index})
    assert sink.events == [{"n": 3}, {"n": 4}]
    assert sink.dropped == 3
    assert sink.drain() == [{"n": 3}, {"n": 4}]
    assert sink.events == []
    with pytest.raises(ValueError, match="capacité"):
        MemorySink(capacity=0)


def test_hop_by_hop_headers_are_removed_including_those_named_by_connection() -> None:
    headers = [
        (b"Connection", b"keep-alive, X-Prive"),
        (b"x-prive", b"1"),
        (b"Keep-Alive", b"timeout=5"),
        (b"TE", b"trailers"),
        (b"Transfer-Encoding", b"chunked"),
        (b"Upgrade", b"h2c"),
        (b"Proxy-Authorization", b"Basic x"),
        (b"authorization", b"Bearer jeton"),
        (b"mcp-param-region", b"eu"),
        (b"set-cookie", b"a=1"),
        (b"set-cookie", b"b=2"),
    ]
    assert end_to_end_headers(headers) == [
        (b"authorization", b"Bearer jeton"),
        (b"mcp-param-region", b"eu"),
        (b"set-cookie", b"a=1"),
        (b"set-cookie", b"b=2"),
    ]


def test_settings_defaults_listen_on_the_loopback_interface() -> None:
    settings = ProxySettings.create("https://mcp.exemple.fr/")
    assert settings.host == "127.0.0.1"
    assert settings.port == DEFAULT_PORT
    assert settings.server_name == "mcp.exemple.fr"
    assert settings.preserve_host


@pytest.mark.parametrize(
    "upstream",
    [
        "ftp://mcp.exemple.fr",
        "mcp.exemple.fr",
        "https://utilisateur:secret@mcp.exemple.fr",
        "https://mcp.exemple.fr/mcp?cle=secret",
        "https://mcp.exemple.fr/mcp#fragment",
        "http://[::1",
        "",
    ],
)
def test_unusable_upstream_is_refused(upstream: str) -> None:
    with pytest.raises(ProxyConfigurationError):
        ProxySettings.create(upstream, "serveur")


def test_refusal_never_echoes_credentials_found_in_the_upstream_url() -> None:
    with pytest.raises(ProxyConfigurationError) as raised:
        ProxySettings.create("https://utilisateur:canari-secret@mcp.exemple.fr")
    assert "canari-secret" not in str(raised.value)


@pytest.mark.parametrize("name", ["", "nom avec espaces", "a" * 129, "nom/chemin"])
def test_invalid_server_name_is_refused(name: str) -> None:
    with pytest.raises(ProxyConfigurationError):
        ProxySettings.create("https://mcp.exemple.fr", name)


def test_invalid_port_and_size_are_refused() -> None:
    with pytest.raises(ProxyConfigurationError):
        ProxySettings.create("https://mcp.exemple.fr", port=70000)
    with pytest.raises(ProxyConfigurationError):
        ProxySettings.create("https://mcp.exemple.fr", max_inspected_bytes=-1)


def test_settings_from_environment() -> None:
    settings = ProxySettings.from_environment(
        {
            "ANSWERLINEAGE_PROXY_UPSTREAM": "http://127.0.0.1:9000",
            "ANSWERLINEAGE_PROXY_SERVER_NAME": "entrepot",
            "ANSWERLINEAGE_PROXY_HOST": "0.0.0.0",  # noqa: S104 — valeur de test.
            "ANSWERLINEAGE_PROXY_PORT": "9001",
        }
    )
    assert (settings.server_name, settings.host, settings.port) == ("entrepot", "0.0.0.0", 9001)  # noqa: S104
    with pytest.raises(ProxyConfigurationError, match="ANSWERLINEAGE_PROXY_UPSTREAM"):
        ProxySettings.from_environment({})
    with pytest.raises(ProxyConfigurationError, match="entier"):
        ProxySettings.from_environment(
            {"ANSWERLINEAGE_PROXY_UPSTREAM": "http://h", "ANSWERLINEAGE_PROXY_PORT": "x"}
        )


def test_command_line_overrides_the_environment() -> None:
    environment = {
        "ANSWERLINEAGE_PROXY_UPSTREAM": "http://127.0.0.1:9000",
        "ANSWERLINEAGE_PROXY_PORT": "9001",
        "ANSWERLINEAGE_PROXY_SERVER_NAME": "entrepot",
    }
    from_env = cli.settings_from_args([], environment)
    assert (str(from_env.upstream), from_env.port, from_env.server_name) == (
        "http://127.0.0.1:9000",
        9001,
        "entrepot",
    )
    overridden = cli.settings_from_args(
        [
            "--upstream",
            "https://mcp.exemple.fr/base",
            "--port",
            "9002",
            "--server-name",
            "meteo",
            "--host",
            "127.0.0.2",
            "--no-preserve-host",
            "--max-inspected-bytes",
            "10",
        ],
        environment,
    )
    assert str(overridden.upstream) == "https://mcp.exemple.fr/base"
    assert (overridden.port, overridden.server_name, overridden.host) == (
        9002,
        "meteo",
        "127.0.0.2",
    )
    assert not overridden.preserve_host
    assert overridden.max_inspected_bytes == 10
    with pytest.raises(ProxyConfigurationError, match="--upstream"):
        cli.settings_from_args([], {})
    with pytest.raises(ProxyConfigurationError, match="entier"):
        cli.settings_from_args(["--upstream", "http://h"], {"ANSWERLINEAGE_PROXY_PORT": "x"})


def test_main_serves_without_access_log_and_without_added_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served: dict[str, Any] = {}

    def fake_run(app: object, **options: object) -> None:
        served["app"] = app
        served.update(options)

    monkeypatch.setattr("uvicorn.run", fake_run)
    monkeypatch.delenv("ANSWERLINEAGE_PROXY_UPSTREAM", raising=False)
    assert cli.main(["--upstream", "http://127.0.0.1:9000", "--port", "9003"]) == 0
    assert isinstance(served["app"], McpProxy)
    assert (served["host"], served["port"]) == ("127.0.0.1", 9003)
    assert served["access_log"] is False
    assert served["server_header"] is False
    assert served["date_header"] is False


def test_main_reports_an_invalid_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ANSWERLINEAGE_PROXY_UPSTREAM", raising=False)
    assert cli.main([]) == 2
    assert "Configuration invalide" in capsys.readouterr().err


@pytest.mark.parametrize("upstream", ["http://[fe80::1%25eth0]:9000", "http://mcp~interne:9000"])
def test_main_reports_an_mcp_server_that_host_cannot_name(
    upstream: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("uvicorn.run", lambda app, **options: None)
    assert cli.main(["--upstream", upstream, "--server-name", "meteo"]) == 2
    error = capsys.readouterr().err
    assert error.startswith("Configuration invalide : L'hôte du serveur MCP")
    assert "Traceback" not in error


def test_main_serves_an_ipv6_mcp_server(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    served: dict[str, Any] = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **options: served.update(app=app))
    assert cli.main(["--upstream", "http://[::1]:9000", "--server-name", "meteo"]) == 0
    assert isinstance(served["app"], McpProxy)
    # Une adresse IPv6 ne fait pas un nom de serveur : le message dit comment en donner un.
    assert cli.main(["--upstream", "http://[::1]:9000"]) == 2
    assert "--server-name" in capsys.readouterr().err


def test_http_client_logs_cannot_cite_urls_or_headers() -> None:
    """httpx cite l'URL complète au niveau INFO, httpcore les en-têtes au niveau DEBUG."""
    root_level = logging.getLogger().level
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.NOTSET)
    logging.getLogger().setLevel(logging.DEBUG)
    try:
        McpProxy(ProxySettings.create("http://127.0.0.1:1", "meteo"), MemorySink())
        for name in ("httpx", "httpcore", "httpcore.http11", "httpcore.connection"):
            assert not logging.getLogger(name).isEnabledFor(logging.INFO)
            assert logging.getLogger(name).isEnabledFor(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.ERROR)
        quiet_transport_logs()
        assert logging.getLogger("httpx").level == logging.ERROR
    finally:
        logging.getLogger().setLevel(root_level)
        logging.getLogger("httpx").setLevel(logging.WARNING)
