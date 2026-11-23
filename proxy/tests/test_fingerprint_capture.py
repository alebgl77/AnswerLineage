"""Un appel qui traverse le proxy laisse des empreintes à clé de ses arguments et de son résultat.

La clé est propre à un locataire, se fournit sans passer par la ligne de commande et ne
se montre nulle part. Sans clé, aucune empreinte.
"""

from __future__ import annotations

import gzip
import json
import logging
import time
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest

from answerlineage_proxy import cli, lifecycle
from answerlineage_proxy.config import ProxyConfigurationError, ProxySettings
from answerlineage_proxy.fingerprint import (
    ARGUMENTS_LABEL,
    RESULT_LABEL,
    FingerprintKey,
)
from proxy.tests.conftest import (
    LARGE_RESULT,
    SSE_FIRST,
    SSE_LAST,
    TOOL_RESULT,
    BenchFactory,
    exchange,
    tool_call,
)

pytestmark = pytest.mark.anyio

TENANT_A = FingerprintKey.from_hex("0a" * 32)
TENANT_B = FingerprintKey.from_hex("0b" * 32)
UPSTREAM = "http://127.0.0.1:9000"
# Clé canari : de l'hexadécimal reconnaissable, cherché partout où il ne doit pas être.
CANARY = "ca11ab1e"
CANARY_KEY = CANARY * 8


def facet(event: Mapping[str, Any]) -> Mapping[str, Any]:
    result: Mapping[str, Any] = event["run"]["facets"]["answerlineage_toolCall"]
    return result


async def call(
    bench: BenchFactory, key: FingerprintKey | None, body: bytes, path: str = "/mcp"
) -> Mapping[str, Any]:
    async with bench(fingerprint_key=key) as b:
        response = await exchange("POST", b.proxy_url + path, content=body)
        assert response.status == 200
        await b.proxy.drain()
    (event,) = b.sink.events
    return event


async def test_two_tenants_get_two_fingerprints_for_the_same_call(bench: BenchFactory) -> None:
    """Critère d'acceptation : deux locataires, la même valeur, deux empreintes."""
    body = tool_call("get_weather", {"location": "Séville, ES"})
    first = facet(await call(bench, TENANT_A, body))
    second = facet(await call(bench, TENANT_B, body))
    again = facet(await call(bench, TENANT_A, body))

    assert first["argumentsFingerprint"] != second["argumentsFingerprint"]
    assert first["resultFingerprint"] != second["resultFingerprint"]
    # Même clé, même valeur : même empreinte, d'un proxy à l'autre.
    assert (again["argumentsFingerprint"], again["resultFingerprint"]) == (
        first["argumentsFingerprint"],
        first["resultFingerprint"],
    )


async def test_fingerprints_are_those_of_the_canonical_arguments_and_the_relayed_bytes(
    bench: BenchFactory,
) -> None:
    event = await call(bench, TENANT_A, tool_call("get_weather", {"location": "Séville, ES"}))
    assert facet(event)["argumentsFingerprint"] == TENANT_A.fingerprint(
        ARGUMENTS_LABEL, '{"location":"Séville, ES"}'.encode()
    )
    assert facet(event)["resultFingerprint"] == TENANT_A.fingerprint(RESULT_LABEL, TOOL_RESULT)
    assert facet(event)["resultSize"] == len(TOOL_RESULT)
    # Le résultat lui-même n'est pas dans l'événement.
    assert "Séville" not in json.dumps(event, ensure_ascii=False)


async def test_the_arguments_fingerprint_ignores_how_the_json_is_written(
    bench: BenchFactory,
) -> None:
    compact = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_weather", "arguments": {"b": [1, 2.0], "a": "é"}},
        },
        separators=(",", ":"),
    ).encode()
    spaced = (
        b'{ "jsonrpc": "2.0", "id": 1, "method": "tools/call",\n'
        b'  "params": { "arguments": { "a": "\\u00e9", "b": [ 1.0, 2 ] }, "name": "get_weather" } }'
    )
    first = facet(await call(bench, TENANT_A, compact))
    second = facet(await call(bench, TENANT_A, spaced))
    assert first["argumentsFingerprint"] == second["argumentsFingerprint"]


async def test_a_call_without_arguments_is_fingerprinted_as_an_empty_object(
    bench: BenchFactory,
) -> None:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_weather"}}
    ).encode()
    event = await call(bench, TENANT_A, body)
    assert facet(event)["argumentsFingerprint"] == TENANT_A.fingerprint(ARGUMENTS_LABEL, b"{}")


async def test_without_a_key_no_fingerprint_is_emitted(bench: BenchFactory) -> None:
    event = await call(bench, None, tool_call("get_weather", {"location": "Séville, ES"}))
    assert "argumentsFingerprint" not in facet(event)
    assert "resultFingerprint" not in facet(event)
    # La taille n'est pas une empreinte : elle est émise sans clé.
    assert facet(event)["resultSize"] == len(TOOL_RESULT)


async def test_a_streamed_result_is_fingerprinted_as_relayed(bench: BenchFactory) -> None:
    async with bench(fingerprint_key=TENANT_A) as b:
        b.upstream.gate.set()
        response = await exchange("POST", b.proxy_url + "/sse", content=tool_call())
        await b.proxy.drain()
    assert response.body == SSE_FIRST + SSE_LAST
    (event,) = b.sink.events
    assert facet(event)["resultFingerprint"] == TENANT_A.fingerprint(
        RESULT_LABEL, SSE_FIRST + SSE_LAST
    )
    assert facet(event)["resultSize"] == len(SSE_FIRST + SSE_LAST)


async def test_a_compressed_result_is_fingerprinted_without_being_inflated(
    bench: BenchFactory,
) -> None:
    event = await call(bench, TENANT_A, tool_call(), path="/gzip")
    relayed = gzip.compress(TOOL_RESULT, mtime=0)
    assert facet(event)["resultFingerprint"] == TENANT_A.fingerprint(RESULT_LABEL, relayed)
    assert facet(event)["resultSize"] == len(relayed)


async def test_an_interrupted_result_has_no_fingerprint(bench: BenchFactory) -> None:
    async with bench(fingerprint_key=TENANT_A) as b:
        with pytest.raises(httpx.HTTPError):
            await exchange("POST", b.proxy_url + "/truncated", content=tool_call())
        await b.proxy.drain()
    (event,) = b.sink.events
    assert facet(event)["proxyError"] == "upstream_interrupted"
    assert "argumentsFingerprint" in facet(event)
    assert "resultFingerprint" not in facet(event)
    assert "resultSize" not in facet(event)


async def test_an_abandoned_call_has_no_result_fingerprint(bench: BenchFactory) -> None:
    async with bench(fingerprint_key=TENANT_A) as b:
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            request = client.build_request("POST", b.proxy_url + "/hang", content=tool_call())
            response = await client.send(request, stream=True)
            await response.aiter_raw().__anext__()
            await response.aclose()
        await b.upstream.cancelled.wait()
        await b.proxy.drain()
    (event,) = b.sink.events
    assert event["eventType"] == "ABORT"
    assert "resultFingerprint" not in facet(event)
    assert "resultSize" not in facet(event)


async def test_arguments_of_a_body_too_large_to_be_read_have_no_fingerprint(
    bench: BenchFactory,
) -> None:
    body = tool_call("get_weather", {"location": "x" * 4096})
    async with bench(fingerprint_key=TENANT_A, max_inspected_bytes=1024) as b:

        async def upload() -> AsyncIterator[bytes]:
            for start in range(0, len(body), 512):
                yield body[start : start + 512]

        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            response = await client.post(b.proxy_url + "/mcp", content=upload())
        assert response.status_code == 200
        await b.proxy.drain()
    (event,) = b.sink.events
    assert "argumentsFingerprint" not in facet(event)
    assert facet(event)["resultFingerprint"] == TENANT_A.fingerprint(RESULT_LABEL, TOOL_RESULT)


async def test_the_result_fingerprint_adds_no_measurable_delay_to_the_relay(
    bench: BenchFactory,
) -> None:
    """Un résultat de 8 Mo relayé avec et sans clé : l'empreinte suit le flux sans l'attendre."""

    async def relay_time(key: FingerprintKey | None) -> float:
        async with bench(fingerprint_key=key) as b:
            timings = []
            for _ in range(5):
                started = time.perf_counter()
                response = await exchange("POST", b.proxy_url + "/large", content=tool_call())
                timings.append(time.perf_counter() - started)
                assert response.body == LARGE_RESULT
            await b.proxy.drain()
        assert facet(b.sink.events[-1])["resultSize"] == len(LARGE_RESULT)
        return min(timings)

    without = await relay_time(None)
    with_key = await relay_time(TENANT_A)
    # Marge large : la machine peut être chargée ; HMAC-SHA-256 passe des centaines de
    # Mo par seconde, bien plus vite que le relais lui-même.
    assert with_key < 2 * without + 0.05


def test_the_key_comes_from_the_environment_or_from_a_file(tmp_path: Path) -> None:
    from_variable = cli.settings_from_args(
        ["--upstream", UPSTREAM], {"ANSWERLINEAGE_PROXY_FINGERPRINT_KEY": "0a" * 32}
    )
    assert from_variable.fingerprint_key == TENANT_A
    key_file = tmp_path / "cle"
    key_file.write_text("0b" * 32 + "\n", encoding="ascii")
    from_option = cli.settings_from_args(
        ["--upstream", UPSTREAM, "--fingerprint-key-file", str(key_file)], {}
    )
    assert from_option.fingerprint_key == TENANT_B
    from_file_variable = ProxySettings.from_environment(
        {
            "ANSWERLINEAGE_PROXY_UPSTREAM": UPSTREAM,
            "ANSWERLINEAGE_PROXY_FINGERPRINT_KEY_FILE": str(key_file),
        }
    )
    assert from_file_variable.fingerprint_key == TENANT_B
    assert cli.settings_from_args(["--upstream", UPSTREAM], {}).fingerprint_key is None


def test_the_key_has_no_command_line_option() -> None:
    options = {option for action in cli.build_parser()._actions for option in action.option_strings}
    assert "--fingerprint-key-file" in options
    assert not any(option.endswith("-key") for option in options)


@pytest.mark.parametrize(
    ("environment", "content", "message"),
    [
        ({"ANSWERLINEAGE_PROXY_FINGERPRINT_KEY": CANARY * 4}, None, "32 à 64 octets"),
        ({"ANSWERLINEAGE_PROXY_FINGERPRINT_KEY": CANARY * 8 + "zz"}, None, "hexadécimal"),
        ({}, (CANARY * 4).encode(), "32 à 64 octets"),
        ({}, b"\xff" + (CANARY * 8).encode(), "hexadécimal"),
        ({}, b"0" * 5000, "trop grand"),
        ({"ANSWERLINEAGE_PROXY_FINGERPRINT_KEY": CANARY_KEY}, CANARY_KEY.encode(), "pas des deux"),
    ],
)
def test_an_invalid_key_stops_the_proxy_without_being_quoted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    environment: dict[str, str],
    content: bytes | None,
    message: str,
) -> None:
    arguments = ["--upstream", UPSTREAM]
    if content is not None:
        key_file = tmp_path / CANARY_KEY
        key_file.write_bytes(content)
        arguments += ["--fingerprint-key-file", str(key_file)]
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(lifecycle, "run", _never_started)

    assert cli.main(arguments) == 2
    printed = capsys.readouterr()
    assert printed.err.startswith("Configuration invalide : ")
    assert message in printed.err
    assert CANARY not in printed.err + printed.out


def test_an_unreadable_key_file_stops_the_proxy_without_being_quoted(tmp_path: Path) -> None:
    missing = tmp_path / CANARY_KEY
    with pytest.raises(ProxyConfigurationError, match="illisible") as refused:
        cli.settings_from_args(["--upstream", UPSTREAM, "--fingerprint-key-file", str(missing)], {})
    assert CANARY not in str(refused.value)


def _never_started(*args: object, **options: object) -> None:
    raise AssertionError("le proxy ne devait pas démarrer")


def test_the_proxy_says_once_at_startup_that_it_has_no_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("ANSWERLINEAGE_PROXY_FINGERPRINT_KEY", raising=False)
    monkeypatch.delenv("ANSWERLINEAGE_PROXY_FINGERPRINT_KEY_FILE", raising=False)
    monkeypatch.setattr(lifecycle, "run", lambda app, settings: 0)
    with caplog.at_level(logging.INFO):
        assert cli.main(["--upstream", UPSTREAM]) == 0
    assert caplog.text.count("Aucune clé d'empreinte") == 1

    caplog.clear()
    monkeypatch.setenv("ANSWERLINEAGE_PROXY_FINGERPRINT_KEY", "0a" * 32)
    with caplog.at_level(logging.INFO):
        assert cli.main(["--upstream", UPSTREAM]) == 0
    assert "Aucune clé d'empreinte" not in caplog.text


async def test_the_key_appears_nowhere(
    bench: BenchFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ni journal, ni événement, ni représentation des réglages, ni compteurs."""
    started: list[Any] = []

    def run(app: Any, settings: Any) -> int:
        started.append(app)
        return 0

    monkeypatch.setenv("ANSWERLINEAGE_PROXY_FINGERPRINT_KEY", CANARY_KEY)
    monkeypatch.setattr(lifecycle, "run", run)
    with caplog.at_level(logging.DEBUG):
        assert cli.main(["--upstream", UPSTREAM]) == 0
        (proxy,) = started
        settings = proxy.settings
        assert settings.fingerprint_key == FingerprintKey.from_hex(CANARY_KEY)
        async with bench(fingerprint_key=settings.fingerprint_key) as b:
            await exchange("POST", b.proxy_url + "/mcp", content=tool_call())
            with pytest.raises(httpx.HTTPError):
                await exchange("POST", b.proxy_url + "/truncated", content=tool_call())
            await b.proxy.drain()
            seen = [
                json.dumps(b.sink.events),
                repr(b.proxy.stats),
                repr(b.proxy.settings),
                str(b.proxy.settings),
                repr(vars(b.proxy)),
            ]
    printed = capsys.readouterr()
    seen += [repr(settings), str(settings), caplog.text, printed.out, printed.err]
    assert len(b.sink.events) == 2
    assert all("argumentsFingerprint" in facet(event) for event in b.sink.events)
    for place in seen:
        assert CANARY not in place


def test_settings_built_in_code_take_a_key() -> None:
    settings = ProxySettings.create(UPSTREAM, "m", fingerprint_key=TENANT_A)
    assert settings.fingerprint_key == TENANT_A
    assert "fingerprint_key" not in repr(settings)
