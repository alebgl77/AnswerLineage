"""La destination de la réémission : réglée par l'exploitant, contrôlée, jointe sous des délais.

Une adresse de métadonnées, de lien local ou nulle est refusée, écrite ou
désignée par un nom ; ``http`` ne sort pas de la boucle locale ; aucun
identifiant, paramètre ni fragment. Aucun refus ne cite l'adresse ni l'en-tête
d'authentification. Le client vérifie TLS, ne suit aucune redirection, et
n'attend pas une destination qui ne répond pas.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from ipaddress import ip_address
from pathlib import Path

import pytest

from answerlineage_workers.destination import (
    Address,
    Delivery,
    Destination,
    DestinationError,
    Sender,
    Timeouts,
    check_resolution,
    parse_destination,
    resolve,
)
from workers.tests.catalog import LINEAGE, Answer, FakeCatalog, certificate, serving

pytestmark = pytest.mark.anyio

CANARY = "canari5e0b"
FAST = Timeouts(connect=1.0, io=1.0, request=1.0)


def resolver(*addresses: str) -> object:
    def found(host: str, port: int) -> Sequence[Address]:
        return [ip_address(address) for address in addresses]

    return found


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://catalogue.interne/api/v1/lineage",
            Destination("https", "catalogue.interne", 443, "/api/v1/lineage"),
        ),
        (
            "https://10.2.0.7:5000/api/v1/lineage",
            Destination("https", "10.2.0.7", 5000, "/api/v1/lineage"),
        ),
        ("HTTPS://[fd12::7]/lineage", Destination("https", "fd12::7", 443, "/lineage")),
        (
            "http://127.0.0.1:5000/api/v1/lineage",
            Destination("http", "127.0.0.1", 5000, "/api/v1/lineage"),
        ),
        ("http://localhost:5000", Destination("http", "localhost", 5000, "/")),
        ("http://[::1]:5000/x", Destination("http", "::1", 5000, "/x")),
        ("https://localhost/x", Destination("https", "localhost", 443, "/x")),
    ],
)
def test_a_destination_is_https_or_http_on_the_loopback(url: str, expected: Destination) -> None:
    assert parse_destination(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        # Les métadonnées des clouds, écrites ou enveloppées.
        "https://169.254.169.254/latest/meta-data",
        "https://169.254.170.2/v2/credentials",
        "https://[fd00:ec2::254]/latest",
        "https://[::ffff:169.254.169.254]/latest",
        "https://[::ffff:a9fe:a9fe]/latest",
        "https://[64:ff9b::a9fe:a9fe]/latest",
        "https://100.100.100.200/latest",
        "https://168.63.129.16/machine",
        "https://[fe80::1]/x",
        "https://0.0.0.0/x",
        "https://[::]/x",
        "https://224.0.0.1/x",
        "https://255.255.255.255/x",
        # En clair hors de la boucle locale.
        "http://10.0.0.5/api/v1/lineage",
        "http://catalogue.interne/api/v1/lineage",
        "http://169.254.169.254/latest",
        # Un identifiant, des paramètres, un fragment.
        "https://utilisateur:canari5e0b@catalogue.interne/x",
        "https://canari5e0b@catalogue.interne/x",
        "https://catalogue.interne/x?api_key=canari5e0b",
        "https://catalogue.interne/x#canari5e0b",
        # Ce qui n'est pas une adresse web lisible.
        "ftp://catalogue.interne/x",
        "file:///etc/canari5e0b",
        "https:///x",
        "https://catalogue.interne:99999/x",
        "https://catalogue.interne:canari5e0b/x",
        "https://catalogue_canari5e0b!.interne/x",
        "https://catalogue.interne/x canari5e0b",
        "https://catalogue.interne/x\ncanari5e0b",
        "https://catalogué.interne/canari5e0b",
        "https://[canari5e0b]/x",
        "https://catalogue.interne/" + "canari5e0b" * 300,
    ],
)
def test_a_refused_destination_is_never_cited(url: str) -> None:
    with pytest.raises(DestinationError) as refused:
        parse_destination(url)
    message = str(refused.value)
    assert CANARY not in message
    assert "169.254" not in message and "catalogue" not in message


@pytest.mark.parametrize(
    "authorization",
    [
        "",
        " Bearer canari5e0b",
        "Bearer canari5e0b\r\nX-Injecte: 1",
        "Bearer é" + CANARY,
        "x" * 8193,
    ],
)
def test_an_authorization_header_that_is_not_printable_ascii_is_refused_without_being_cited(
    authorization: str,
) -> None:
    with pytest.raises(DestinationError) as refused:
        parse_destination("https://catalogue.interne/x", authorization)
    assert CANARY not in str(refused.value)


def test_the_authorization_header_is_never_shown() -> None:
    destination = parse_destination("https://catalogue.interne/x", f"Bearer {CANARY}")
    assert destination.authorization == f"Bearer {CANARY}"
    assert CANARY not in repr(destination)


def test_a_name_that_leads_to_a_forbidden_address_is_refused_at_startup() -> None:
    https = parse_destination("https://metadata.interne/x")
    with pytest.raises(DestinationError) as refused:
        check_resolution(https, resolver("10.0.0.4", "169.254.169.254"))  # type: ignore[arg-type]
    assert "metadata" not in str(refused.value)
    check_resolution(https, resolver("10.0.0.4", "fd12::4"))  # type: ignore[arg-type]
    # En clair, la boucle locale seulement : « localhost » qui mènerait ailleurs est refusé.
    with pytest.raises(DestinationError):
        check_resolution(parse_destination("http://localhost/x"), resolver("10.0.0.4"))  # type: ignore[arg-type]


def test_a_name_that_does_not_resolve_yet_does_not_stop_the_worker() -> None:
    def unknown(host: str, port: int) -> Sequence[Address]:
        raise OSError("nom inconnu")

    check_resolution(parse_destination("https://catalogue.interne/x"), unknown)


def test_the_resolver_gives_the_addresses_of_a_name_once_each() -> None:
    found = resolve("localhost", 80)
    assert found
    assert all(address.is_loopback for address in found)
    assert len(found) == len(set(found))


async def test_each_connection_checks_what_the_name_leads_to_and_goes_there() -> None:
    catalog = FakeCatalog()
    async with serving(catalog) as base:
        port = int(base.rpartition(":")[2])
        destination = parse_destination(f"http://localhost:{port}{LINEAGE}")
        # « localhost » mène ici à l'adresse de métadonnées : rien ne part.
        trapped = Sender(destination, resolver=resolver("169.254.169.254"), timeouts=FAST)  # type: ignore[arg-type]
        assert await trapped.send(b"{}") == Delivery("failed", "ForbiddenAddressError")
        trapped.close()
        # La connexion va à l'adresse contrôlée, pas à une seconde résolution du nom.
        sender = Sender(destination, resolver=resolver("127.0.0.1"), timeouts=FAST)  # type: ignore[arg-type]
        assert await sender.send(b"{}") == Delivery("sent", "HTTP 201")
        sender.close()
    assert [request.body for request in catalog.received] == [b"{}"]
    assert catalog.received[0].headers["host"] == f"localhost:{port}"


@pytest.mark.parametrize(
    ("status", "verdict"),
    [
        (200, "sent"),
        (201, "sent"),
        (202, "sent"),
        (400, "rejected"),
        (413, "rejected"),
        (422, "rejected"),
        (401, "failed"),
        (403, "failed"),
        (404, "failed"),
        (429, "failed"),
        (500, "failed"),
        (503, "failed"),
        (307, "failed"),
    ],
)
async def test_each_status_has_its_verdict(status: int, verdict: str) -> None:
    catalog = FakeCatalog(answer=Answer(status))
    async with serving(catalog) as base:
        sender = Sender(parse_destination(base + LINEAGE), timeouts=FAST)
        assert await sender.send(b"{}") == Delivery(verdict, f"HTTP {status}")  # type: ignore[arg-type]
        sender.close()


async def test_a_redirect_is_not_followed() -> None:
    elsewhere = FakeCatalog()
    async with serving(elsewhere) as other:
        catalog = FakeCatalog(answer=Answer(307), location=other + LINEAGE)
        async with serving(catalog) as base:
            sender = Sender(parse_destination(base + LINEAGE), timeouts=FAST)
            assert await sender.send(b"{}") == Delivery("failed", "HTTP 307")
            sender.close()
    assert len(catalog.received) == 1
    assert elsewhere.received == []


async def test_the_event_and_the_authorization_header_are_sent_on_one_kept_connection() -> None:
    catalog = FakeCatalog()
    async with serving(catalog) as base:
        sender = Sender(parse_destination(base + LINEAGE, f"Bearer {CANARY}"), timeouts=FAST)
        for index in range(3):
            assert (await sender.send(b'{"n":%d}' % index)).verdict == "sent"
        sender.close()
    assert [request.body for request in catalog.received] == [b'{"n":0}', b'{"n":1}', b'{"n":2}']
    for request in catalog.received:
        assert request.path == LINEAGE
        assert request.headers["authorization"] == f"Bearer {CANARY}"
        assert request.headers["content-type"] == "application/json"
    assert len({request.peer for request in catalog.received}) == 1


async def test_a_destination_that_does_not_answer_is_given_up_at_the_deadline() -> None:
    catalog = FakeCatalog().then(Answer(delay=30))
    async with serving(catalog) as base:
        sender = Sender(parse_destination(base + LINEAGE), timeouts=Timeouts(1.0, 5.0, 0.5))
        started = time.monotonic()
        assert await sender.send(b"{}") == Delivery("failed", "TimeoutError")
        assert time.monotonic() - started < 2
        # La connexion coupée, l'envoi suivant en ouvre une autre.
        assert await sender.send(b"{}") == Delivery("sent", "HTTP 201")
        sender.close()


async def test_a_stop_interrupts_the_sending_in_progress() -> None:
    catalog = FakeCatalog().then(Answer(delay=30))
    stop = asyncio.Event()
    async with serving(catalog) as base:
        sender = Sender(parse_destination(base + LINEAGE), timeouts=Timeouts(1.0, 30.0, 30.0))
        asyncio.get_running_loop().call_later(0.2, stop.set)
        started = time.monotonic()
        assert await sender.send(b"{}", stop) == Delivery("interrupted", "stop")
        assert time.monotonic() - started < 2
        sender.close()


async def test_an_unreachable_destination_is_a_failure_by_its_class() -> None:
    async with serving(FakeCatalog()) as base:
        closed = base
    sender = Sender(parse_destination(closed + LINEAGE), timeouts=FAST)
    assert await sender.send(b"{}") == Delivery("failed", "ConnectionRefusedError")
    sender.close()


async def test_tls_is_verified_and_an_internal_authority_is_trusted_through_the_system_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = certificate(tmp_path, "IP:127.0.0.1")
    catalog = FakeCatalog()
    async with serving(catalog, certificate=trusted) as base:
        destination = parse_destination(base + LINEAGE)
        unknown = Sender(destination, timeouts=FAST)
        assert await unknown.send(b"{}") == Delivery("failed", "SSLCertVerificationError")
        unknown.close()
        assert catalog.received == []

        monkeypatch.setenv("SSL_CERT_FILE", str(trusted[0]))
        sender = Sender(destination, timeouts=FAST)
        assert await sender.send(b"{}") == Delivery("sent", "HTTP 201")
        sender.close()
    assert catalog.bodies() == [b"{}"]


async def test_tls_checks_the_name_of_the_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = certificate(tmp_path, "DNS:autre.interne")
    monkeypatch.setenv("SSL_CERT_FILE", str(elsewhere[0]))
    catalog = FakeCatalog()
    async with serving(catalog, certificate=elsewhere) as base:
        sender = Sender(parse_destination(base + LINEAGE), timeouts=FAST)
        assert await sender.send(b"{}") == Delivery("failed", "SSLCertVerificationError")
        sender.close()
    assert catalog.received == []
