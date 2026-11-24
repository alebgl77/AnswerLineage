"""Les réglages de l'adaptateur : l'agent, l'API, la clé d'empreinte, le jeton du client.

Ils se lisent de l'environnement, sous des variables propres à l'adaptateur ;
un réglage refusé ne cite jamais sa valeur.
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path

import pytest

from answerlineage_adapters.settings import (
    ENV_API_URL,
    ENV_FINGERPRINT_KEY,
    ENV_FINGERPRINT_KEY_FILE,
    ENV_OIDC_AUDIENCE,
    ENV_OIDC_CLIENT_ID,
    ENV_OIDC_CLIENT_SECRET,
    ENV_OIDC_CLIENT_SECRET_FILE,
    ENV_OIDC_ISSUER,
    ENV_OIDC_SCOPE,
    ENV_PSEUDONYM_KEY,
    ENV_PSEUDONYM_KEY_FILE,
    AdapterSettings,
    AgentJob,
)
from answerlineage_emitter.config import ConfigurationError
from answerlineage_emitter.credentials import ClientCredentialsTokens
from answerlineage_emitter.fingerprint import FingerprintKey
from answerlineage_emitter.outbox import Outbox
from answerlineage_emitter.pseudonym import PseudonymKey
from answerlineage_emitter.sink import NullSink

AGENT = AgentJob.create("agent://finance", "analyste-change")
API = "https://lignage.exemple.fr"
ISSUER = "https://idp.exemple.fr/realms/lignage"


def identity(value: str) -> dict[str, str]:
    return {
        ENV_API_URL: API,
        ENV_OIDC_ISSUER: ISSUER,
        ENV_OIDC_CLIENT_ID: "adaptateur-finance",
        ENV_OIDC_CLIENT_SECRET: value,
    }


@pytest.mark.parametrize(
    ("namespace", "name"),
    [
        ("agent://finance", ""),
        ("", "analyste"),
        ("agent://finance", "analyste change"),
        ("agent://finance", "<script>"),
        ("agent://finance", "a" * 300),
    ],
)
def test_an_agent_without_the_form_of_a_name_is_refused_without_being_cited(
    namespace: str, name: str
) -> None:
    with pytest.raises(ConfigurationError) as refused:
        AgentJob.create(namespace, name)
    for value in (namespace, name):
        if value:
            assert value not in str(refused.value)


def test_without_an_api_nothing_is_sent_and_the_journal_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = AdapterSettings.from_environment({}, agent=AGENT)
    assert (settings.outbox, settings.credentials, settings.fingerprint_key) == (None, None, None)
    assert settings.pseudonym_key is None
    with caplog.at_level(logging.WARNING, logger="answerlineage_adapters"):
        assert isinstance(settings.sink(), NullSink)
    assert "Aucune adresse d'API" in caplog.text


def test_the_api_the_key_and_the_identity_come_from_the_variables_of_the_adapter(
    tmp_path: Path,
) -> None:
    value = "canari-" + secrets.token_hex(6)
    written = secrets.token_hex(32)
    held = tmp_path / "secret"
    held.write_text(value, encoding="utf-8")
    environment = {
        **identity(value),
        ENV_FINGERPRINT_KEY: written,
        ENV_OIDC_SCOPE: "api://lignage/.default",
        ENV_OIDC_AUDIENCE: "answerlineage-api",
    }
    settings = AdapterSettings.from_environment(environment, agent=AGENT, flush_seconds=2)
    assert settings.outbox is not None
    assert str(settings.outbox.api_url) == API
    assert settings.outbox.flush_seconds == 2
    assert settings.fingerprint_key == FingerprintKey.from_hex(written)
    assert settings.credentials is not None
    assert settings.credentials.scope == "api://lignage/.default"
    assert settings.credentials.audience == "answerlineage-api"
    assert value not in repr(settings)
    assert written not in repr(settings)

    from_file = {**identity(value), ENV_OIDC_CLIENT_SECRET_FILE: str(held)}
    del from_file[ENV_OIDC_CLIENT_SECRET]
    assert AdapterSettings.from_environment(from_file, agent=AGENT).credentials is not None
    key_file = tmp_path / "cle"
    key_file.write_text(written, encoding="ascii")
    keyed = AdapterSettings.from_environment({ENV_FINGERPRINT_KEY_FILE: str(key_file)}, agent=AGENT)
    assert keyed.fingerprint_key == FingerprintKey.from_hex(written)


def test_the_key_of_the_pseudonyms_comes_from_its_own_variable_or_file(tmp_path: Path) -> None:
    written = secrets.token_hex(32)
    settings = AdapterSettings.from_environment({ENV_PSEUDONYM_KEY: written}, agent=AGENT)
    assert settings.pseudonym_key == PseudonymKey.from_hex(written)
    assert settings.fingerprint_key is None
    assert written not in repr(settings)
    held = tmp_path / "cle-des-pseudonymes"
    held.write_text(written + "\n", encoding="ascii")
    from_file = AdapterSettings.from_environment({ENV_PSEUDONYM_KEY_FILE: str(held)}, agent=AGENT)
    assert from_file.pseudonym_key == settings.pseudonym_key


def test_the_key_of_the_pseudonyms_is_not_the_key_of_the_fingerprints() -> None:
    """L'API tient la clé des pseudonymes : la même que celle des empreintes lui permettrait
    d'éprouver les arguments des appels d'outils."""
    written = secrets.token_hex(32)
    environment = {ENV_FINGERPRINT_KEY: written, ENV_PSEUDONYM_KEY: written.upper()}
    with pytest.raises(ConfigurationError) as refused:
        AdapterSettings.from_environment(environment, agent=AGENT)
    assert ENV_PSEUDONYM_KEY in str(refused.value)
    assert written not in str(refused.value).lower()
    other = {ENV_FINGERPRINT_KEY: written, ENV_PSEUDONYM_KEY: secrets.token_hex(32)}
    settings = AdapterSettings.from_environment(other, agent=AGENT)
    assert settings.fingerprint_key is not None
    assert settings.pseudonym_key is not None


@pytest.mark.anyio
async def test_with_an_api_the_events_leave_by_the_outbox_with_the_token() -> None:
    settings = AdapterSettings.from_environment(identity("s"), agent=AGENT)
    sink = settings.sink()
    assert isinstance(sink, Outbox)
    assert isinstance(sink._tokens, ClientCredentialsTokens)
    await sink.aclose()


@pytest.mark.parametrize(
    "environment",
    [
        {ENV_OIDC_ISSUER: ISSUER},
        {ENV_API_URL: API, ENV_OIDC_ISSUER: ISSUER, ENV_OIDC_CLIENT_ID: "adaptateur"},
        {key: value for key, value in identity("s").items() if key != ENV_API_URL},
        {**identity("s"), ENV_API_URL: "http://lignage.exemple.fr"},
        {ENV_API_URL: "ftp://lignage.exemple.fr"},
        {ENV_FINGERPRINT_KEY: "abc"},
        {ENV_PSEUDONYM_KEY: "abc"},
    ],
    ids=[
        "émetteur seul",
        "sans secret",
        "jeton sans API",
        "jeton en clair",
        "adresse d'un autre schéma",
        "clé trop courte",
        "clé des pseudonymes trop courte",
    ],
)
def test_an_incomplete_or_unsafe_setting_is_refused(environment: dict[str, str]) -> None:
    with pytest.raises(ConfigurationError) as refused:
        AdapterSettings.from_environment(environment, agent=AGENT)
    assert "abc" not in str(refused.value)
