"""Contrat entre composants : le proxy et l'API jugent Host, Origin et Sec-Fetch-Site pareil.

Le contrôle est écrit deux fois, une par service, pour qu'aucun ne dépende de
l'autre. Ce test les tient d'accord.
"""

from __future__ import annotations

import pytest

from answerlineage_api.guard import RequestGuard as ApiGuard
from answerlineage_proxy.guard import RequestGuard as ProxyGuard

HOSTS = ["127.0.0.1", "localhost", "[::1]", "lignage.exemple.fr"]
ORIGINS = ["https://console.exemple.fr"]

# Hôte, origine, Sec-Fetch-Site, Sec-Fetch-Mode, Sec-Fetch-Dest, verdict attendu.
Case = tuple[str | None, str | None, str | None, str | None, str | None, str | None]
CASES: list[Case] = [
    ("127.0.0.1:8000", None, None, None, None, None),
    ("LocalHost.:8000", "http://localhost:3000", None, None, None, None),
    ("[::1]:8000", "http://[::1]:8000", None, None, None, None),
    ("lignage.exemple.fr", "https://console.exemple.fr", None, None, None, None),
    ("lignage.exemple.fr", "HTTPS://LIGNAGE.EXEMPLE.FR", None, None, None, None),
    ("attaquant.example:8000", None, None, None, None, "host_not_allowed"),
    ("127.0.0.1.attaquant.example", None, None, None, None, "host_not_allowed"),
    ("127.0.0.1:8000@attaquant.example", None, None, None, None, "host_not_allowed"),
    ("", None, None, None, None, "host_not_allowed"),
    (None, None, None, None, None, "host_not_allowed"),
    ("attaquant.example", "https://attaquant.example", None, None, None, "host_not_allowed"),
    ("127.0.0.1:8000", "https://attaquant.example", None, None, None, "origin_not_allowed"),
    ("127.0.0.1:8000", "null", None, None, None, "origin_not_allowed"),
    ("127.0.0.1:8000", "https://console.exemple.fr:8443", None, None, None, "origin_not_allowed"),
    ("127.0.0.1:8000", "http://127.0.0.1:8000/chemin", None, None, None, "origin_not_allowed"),
    ("127.0.0.1:8000", "", None, None, None, "origin_not_allowed"),
    # Une autre page, sans origine : ce qu'elle charge par une balise est refusé.
    ("127.0.0.1:8000", None, "cross-site", None, None, "cross_site_not_allowed"),
    ("127.0.0.1:8000", None, "Cross-Site", None, None, "cross_site_not_allowed"),
    ("127.0.0.1:8000", None, "cross-site", "no-cors", "image", "cross_site_not_allowed"),
    ("127.0.0.1:8000", None, "cross-site", "no-cors", "script", "cross_site_not_allowed"),
    ("127.0.0.1:8000", None, "cross-site", "navigate", "iframe", "cross_site_not_allowed"),
    ("127.0.0.1:8000", None, "cross-site", "no-cors", "document", "cross_site_not_allowed"),
    ("127.0.0.1:8000", None, "cross-site", "navigate", None, "cross_site_not_allowed"),
    # Un autre nom du même site, un sous-domaine voisin par exemple, est une autre page.
    ("127.0.0.1:8000", None, "same-site", None, None, "cross_site_not_allowed"),
    ("127.0.0.1:8000", None, "same-site", "no-cors", "image", "cross_site_not_allowed"),
    ("127.0.0.1:8000", None, "valeur-inconnue", None, None, "cross_site_not_allowed"),
    # Un lien suivi depuis une autre page ouvre la page : c'est une navigation.
    ("127.0.0.1:8000", None, "cross-site", "navigate", "document", None),
    ("127.0.0.1:8000", None, "same-site", "Navigate", "Document", None),
    ("127.0.0.1:8000", "https://console.exemple.fr", "cross-site", "cors", "empty", None),
    (
        "127.0.0.1:8000",
        "https://attaquant.example",
        "cross-site",
        "cors",
        "empty",
        "origin_not_allowed",
    ),
    (
        "127.0.0.1:8000",
        "https://attaquant.example",
        "cross-site",
        "navigate",
        "document",
        "origin_not_allowed",
    ),
    ("attaquant.example", None, "cross-site", "navigate", "document", "host_not_allowed"),
    # La page elle-même, ou son utilisateur qui saisit l'adresse.
    ("127.0.0.1:8000", None, "same-origin", "cors", "empty", None),
    ("127.0.0.1:8000", None, "none", "navigate", "document", None),
]


def _headers(
    host: str | None,
    origin: str | None,
    fetch_site: str | None,
    fetch_mode: str | None,
    fetch_dest: str | None,
) -> list[tuple[bytes, bytes]]:
    headers = [(b"accept", b"*/*")]
    for name, value in (
        (b"host", host),
        (b"origin", origin),
        (b"sec-fetch-site", fetch_site),
        (b"sec-fetch-mode", fetch_mode),
        (b"sec-fetch-dest", fetch_dest),
    ):
        if value is not None:
            headers.append((name, value.encode("latin-1")))
    return headers


@pytest.mark.parametrize(
    ("host", "origin", "fetch_site", "fetch_mode", "fetch_dest", "expected"), CASES
)
def test_both_services_give_the_same_verdict(
    host: str | None,
    origin: str | None,
    fetch_site: str | None,
    fetch_mode: str | None,
    fetch_dest: str | None,
    expected: str | None,
) -> None:
    headers = _headers(host, origin, fetch_site, fetch_mode, fetch_dest)
    assert ApiGuard.create(HOSTS, ORIGINS).refusal(headers) == expected
    assert ProxyGuard.create(HOSTS, ORIGINS).refusal(headers) == expected


@pytest.mark.parametrize(
    "headers",
    [
        # Une balise qui se dit aussi de la page elle-même.
        [*_headers("127.0.0.1:8000", None, "cross-site", None, None), (b"sec-fetch-site", b"none")],
        [
            *_headers("127.0.0.1:8000", None, "same-origin", "no-cors", "image"),
            (b"Sec-Fetch-Site", b"cross-site"),
        ],
        # Une navigation qui charge aussi autre chose n'en est pas une.
        [
            *_headers("127.0.0.1:8000", None, "cross-site", "navigate", "document"),
            (b"sec-fetch-dest", b"image"),
        ],
        [
            *_headers("127.0.0.1:8000", None, "cross-site", "navigate", "document"),
            (b"sec-fetch-mode", b"no-cors"),
        ],
    ],
)
def test_both_services_refuse_a_request_that_says_two_things(
    headers: list[tuple[bytes, bytes]],
) -> None:
    """Un en-tête répété ne fait passer une balise ni pour la page, ni pour une navigation."""
    assert ApiGuard.create(HOSTS, ORIGINS).refusal(headers) == "cross_site_not_allowed"
    assert ProxyGuard.create(HOSTS, ORIGINS).refusal(headers) == "cross_site_not_allowed"


def test_a_star_accepts_everything_on_both_sides() -> None:
    headers = [(b"host", b"attaquant.example"), (b"sec-fetch-site", b"cross-site")]
    assert ApiGuard.create(["*"]).refusal(headers) is None
    assert ProxyGuard.create(["*"]).refusal(headers) is None


@pytest.mark.parametrize("settings", [(["exemple.fr:80"], []), (HOSTS, ["exemple.fr"])])
def test_both_services_refuse_the_same_malformed_settings(
    settings: tuple[list[str], list[str]],
) -> None:
    for guard in (ApiGuard, ProxyGuard):
        with pytest.raises(ValueError, match=r"hôte|origine"):
            guard.create(*settings)
