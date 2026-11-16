"""L'adresse de l'API : ``http`` ou ``https``, sans identifiant, sans paramètre ni fragment.

Elle se règle par ``--url`` ou par ``ANSWERLINEAGE_API_URL`` ; une adresse
refusée n'est pas recopiée dans le message.
"""

from __future__ import annotations

import pytest

from answerlineage_cli.client import DEFAULT_URL, ENV_URL, api_base
from answerlineage_cli.exits import Exit
from cli.tests.fakes import FakeApi, run

CANARY = "canari-91b2"


@pytest.mark.parametrize(
    ("given", "base"),
    [
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("http://127.0.0.1:8000/", "http://127.0.0.1:8000"),
        ("HTTPS://Lignage.Exemple.fr/al/", "https://lignage.exemple.fr/al"),
        ("http://[::1]:8000", "http://[::1]:8000"),
        ("http://localhost", "http://localhost"),
    ],
)
def test_an_admitted_address_is_the_base_of_every_route(given: str, base: str) -> None:
    assert api_base(given) == base


@pytest.mark.parametrize(
    "given",
    [
        f"ftp://{CANARY}.example",
        f"file:///{CANARY}",
        f"http://lecteur:{CANARY}@127.0.0.1:8000",
        f"http://{CANARY}@127.0.0.1:8000",
        f"http://127.0.0.1:8000/?jeton={CANARY}",
        f"http://127.0.0.1:8000/#{CANARY}",
        "http://:8000",
        f"http://127.0.0.1:99999/{CANARY}",
        f"http://127.0.0.1:port/{CANARY}",
        f"http://exemple .fr/{CANARY}",
        f"http://127.0.0.1:8000/{CANARY}\n",
        f"http://127.0.0.1:8000/{CANARY}<script>",
        f"{CANARY}",
        "http://" + "a" * 2100,
    ],
)
def test_a_refused_address_is_a_usage_error_that_does_not_cite_it(
    monkeypatch: pytest.MonkeyPatch, given: str
) -> None:
    for argv, environment in ((["--url", given], None), ([], given)):
        if environment is not None:
            monkeypatch.setenv(ENV_URL, environment)
        api = FakeApi.sound()
        result = run(["doctor", *argv], api, monkeypatch)
        assert result.code == Exit.USAGE
        assert api.requests == []
        assert CANARY not in result.err + result.out
        assert ENV_URL in result.err


def test_the_default_address_is_the_loopback_of_the_demonstration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeApi.sound()
    assert run(["doctor"], api, monkeypatch).code == Exit.OK
    assert {str(request.url.copy_with(path="/")) for request in api.requests} == {DEFAULT_URL + "/"}


def test_help_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run(["--help"], FakeApi(), monkeypatch)
    assert result.code == Exit.OK
    for command in ("impact", "incident", "doctor", "demo"):
        assert command in result.out
    assert run([], FakeApi(), monkeypatch).code == Exit.USAGE


def test_the_exit_codes_are_those_a_script_reads_in_the_documentation() -> None:
    documented = {
        "OK": 0,
        "NOTHING_TOUCHED": 1,
        "ANOMALY": 1,
        "FAILED": 1,
        "USAGE": 2,
        "INCOMPLETE": 3,
        "UNREACHABLE": 4,
        "API_ERROR": 5,
        "PENDING": 6,
        "ANALYSIS_FAILED": 7,
    }
    assert {name: int(code) for name, code in Exit.__members__.items()} == documented
