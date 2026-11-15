"""Contrat entre composants : le proxy et l'API jugent Host et Origin de la même façon.

Le contrôle est écrit deux fois, une par service, pour qu'aucun ne dépende de
l'autre. Ce test les tient d'accord.
"""

from __future__ import annotations

import pytest

from answerlineage_api.guard import RequestGuard as ApiGuard
from answerlineage_proxy.guard import RequestGuard as ProxyGuard

HOSTS = ["127.0.0.1", "localhost", "[::1]", "lignage.exemple.fr"]
ORIGINS = ["https://console.exemple.fr"]

CASES: list[tuple[str | None, str | None, str | None]] = [
    ("127.0.0.1:8000", None, None),
    ("LocalHost.:8000", "http://localhost:3000", None),
    ("[::1]:8000", "http://[::1]:8000", None),
    ("lignage.exemple.fr", "https://console.exemple.fr", None),
    ("lignage.exemple.fr", "HTTPS://LIGNAGE.EXEMPLE.FR", None),
    ("attaquant.example:8000", None, "host_not_allowed"),
    ("127.0.0.1.attaquant.example", None, "host_not_allowed"),
    ("127.0.0.1:8000@attaquant.example", None, "host_not_allowed"),
    ("", None, "host_not_allowed"),
    (None, None, "host_not_allowed"),
    ("attaquant.example", "https://attaquant.example", "host_not_allowed"),
    ("127.0.0.1:8000", "https://attaquant.example", "origin_not_allowed"),
    ("127.0.0.1:8000", "null", "origin_not_allowed"),
    ("127.0.0.1:8000", "https://console.exemple.fr:8443", "origin_not_allowed"),
    ("127.0.0.1:8000", "http://127.0.0.1:8000/chemin", "origin_not_allowed"),
    ("127.0.0.1:8000", "", "origin_not_allowed"),
]


@pytest.mark.parametrize(("host", "origin", "expected"), CASES)
def test_both_services_give_the_same_verdict(
    host: str | None, origin: str | None, expected: str | None
) -> None:
    headers = [(b"accept", b"*/*")]
    if host is not None:
        headers.append((b"host", host.encode("latin-1")))
    if origin is not None:
        headers.append((b"origin", origin.encode("latin-1")))
    assert ApiGuard.create(HOSTS, ORIGINS).refusal(headers) == expected
    assert ProxyGuard.create(HOSTS, ORIGINS).refusal(headers) == expected


@pytest.mark.parametrize("settings", [(["exemple.fr:80"], []), (HOSTS, ["exemple.fr"])])
def test_both_services_refuse_the_same_malformed_settings(
    settings: tuple[list[str], list[str]],
) -> None:
    for guard in (ApiGuard, ProxyGuard):
        with pytest.raises(ValueError, match=r"hôte|origine"):
            guard.create(*settings)
