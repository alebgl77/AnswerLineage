"""Ce que chaque émetteur règle à sa façon : ses variables, son journal.

La clé d'empreinte et le secret du client se lisent d'un fichier ou d'une
variable que l'émetteur nomme ; aucun message ne cite la valeur ni le chemin.
La boîte d'envoi et le jeton parlent dans le journal de l'émetteur qui les emploie.
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path

import httpx
import pytest

from answerlineage_emitter.config import ConfigurationError
from answerlineage_emitter.credentials import (
    ClientCredentials,
    ClientCredentialsTokens,
    TokenUnavailableError,
    client_secret,
)
from answerlineage_emitter.fingerprint import FingerprintKey, read_key
from answerlineage_emitter.outbox import Outbox, OutboxSettings

pytestmark = pytest.mark.anyio

KEY_VARIABLE, KEY_FILE_VARIABLE = "EXEMPLE_CLE", "EXEMPLE_CLE_FICHIER"
SECRET_VARIABLE, SECRET_FILE_VARIABLE = "EXEMPLE_SECRET", "EXEMPLE_SECRET_FICHIER"


def key_from(environment: dict[str, str], key_file: str | None = None) -> FingerprintKey | None:
    return read_key(environment, key_file, variable=KEY_VARIABLE, file_variable=KEY_FILE_VARIABLE)


def secret_from(environment: dict[str, str], secret_file: str | None = None) -> str | None:
    return client_secret(
        environment, secret_file, variable=SECRET_VARIABLE, file_variable=SECRET_FILE_VARIABLE
    )


def test_the_key_comes_from_the_variables_the_emitter_names(tmp_path: Path) -> None:
    written = secrets.token_hex(32)
    held = tmp_path / "cle"
    held.write_text(written + "\n", encoding="ascii")
    expected = FingerprintKey.from_hex(written)
    assert key_from({}) is None
    assert key_from({KEY_VARIABLE: written}) == expected
    assert key_from({KEY_FILE_VARIABLE: str(held)}) == expected
    assert key_from({}, str(held)) == expected
    # Les variables d'un autre émetteur ne sont pas lues.
    assert key_from({"ANSWERLINEAGE_PROXY_FINGERPRINT_KEY": written}) is None


def test_an_unusable_key_source_is_refused_without_being_cited(tmp_path: Path) -> None:
    written = secrets.token_hex(32)
    big, held = tmp_path / "grande", tmp_path / "cle"
    big.write_bytes(b"0" * 5000)
    held.write_text(written, encoding="ascii")
    for environment, key_file in (
        ({KEY_VARIABLE: written, KEY_FILE_VARIABLE: str(held)}, None),
        ({}, str(tmp_path / "absente")),
        ({}, str(big)),
        ({KEY_VARIABLE: written[:10]}, None),
    ):
        with pytest.raises(ConfigurationError) as refused:
            key_from(environment, key_file)
        assert written not in str(refused.value)
        assert written[:10] not in str(refused.value)
        assert str(tmp_path) not in str(refused.value)
    with pytest.raises(ConfigurationError, match=KEY_VARIABLE):
        key_from({KEY_VARIABLE: written, KEY_FILE_VARIABLE: str(held)})


def test_the_secret_comes_from_the_variables_the_emitter_names(tmp_path: Path) -> None:
    value = "canari-" + secrets.token_hex(6)
    held = tmp_path / "secret"
    held.write_text(value + "\n", encoding="utf-8")
    assert secret_from({}) is None
    assert secret_from({SECRET_VARIABLE: value}) == value
    assert secret_from({SECRET_FILE_VARIABLE: str(held)}) == value
    assert secret_from({"ANSWERLINEAGE_PROXY_OIDC_CLIENT_SECRET": value}) is None
    with pytest.raises(ConfigurationError, match=SECRET_VARIABLE) as refused:
        secret_from({SECRET_VARIABLE: value, SECRET_FILE_VARIABLE: str(held)})
    assert value not in str(refused.value)


def test_an_unusable_identity_says_nothing_of_the_proxy() -> None:
    """Le client peut être un proxy ou un adaptateur : le message ne présume pas lequel."""
    for client, value in (("client\nfaux", "s"), ("client", "")):
        with pytest.raises(ConfigurationError) as refused:
            ClientCredentials.create("https://idp.exemple.fr", client, value)
        assert "proxy" not in str(refused.value)


async def test_the_outbox_and_the_token_speak_in_the_journal_of_their_emitter(
    caplog: pytest.LogCaptureFixture,
) -> None:
    journal = logging.getLogger("exemple_emetteur")

    async def refuse(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    tokens = ClientCredentialsTokens(
        ClientCredentials.create("https://idp.exemple.fr", "client", "s"),
        transport=httpx.MockTransport(refuse),
        logger=journal,
    )
    with caplog.at_level(logging.INFO, logger="exemple_emetteur"):
        with pytest.raises(TokenUnavailableError):
            await tokens.token()
        # Sans délai de vidage, l'événement en attente est perdu à l'arrêt, et le journal le dit.
        box = Outbox(OutboxSettings.create("http://127.0.0.1:9", flush_seconds=0), logger=journal)
        box.emit({"eventTime": "2026-11-02T09:00:00.000000Z"})
        await box.aclose()
    assert {record.name for record in caplog.records} == {"exemple_emetteur"}
    assert len(caplog.records) == 2
    assert all("proxy" not in record.getMessage() for record in caplog.records)
