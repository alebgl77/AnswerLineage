"""Réglages d'un adaptateur : l'agent qu'il décrit, l'API, les clés, le jeton.

L'agent est nommé dans le code de l'application ; le reste se lit de
l'environnement de son processus, sous des variables propres aux adaptateurs :

| Variable | Réglage |
| --- | --- |
| ``ANSWERLINEAGE_ADAPTER_API_URL`` | Adresse de l'API ; sans elle, rien n'est envoyé |
| ``ANSWERLINEAGE_ADAPTER_FINGERPRINT_KEY`` | Clé d'empreinte, en hexadécimal ; ou ``…_FILE`` |
| ``ANSWERLINEAGE_ADAPTER_PSEUDONYM_KEY`` | Clé des pseudonymes, en hexadécimal ; ou ``…_FILE`` |
| ``ANSWERLINEAGE_ADAPTER_OIDC_ISSUER`` | Émetteur OIDC du jeton de l'adaptateur |
| ``ANSWERLINEAGE_ADAPTER_OIDC_CLIENT_ID`` | Identifiant du client de l'adaptateur |
| ``ANSWERLINEAGE_ADAPTER_OIDC_CLIENT_SECRET`` | Son secret ; ou ``…_FILE`` |
| ``ANSWERLINEAGE_ADAPTER_OIDC_SCOPE``, ``…_AUDIENCE`` | Ce que certains émetteurs OIDC exigent |

Un réglage refusé arrête l'adaptateur avec un message qui nomme le réglage,
jamais sa valeur. Le jeton ne part qu'en https, ou en http vers la boucle locale.
La clé des pseudonymes n'est pas celle des empreintes : l'API la tient pour
révéler un destinataire, et ne doit pas pouvoir éprouver les arguments des appels.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from answerlineage_emitter.config import ConfigurationError
from answerlineage_emitter.credentials import (
    ClientCredentials,
    ClientCredentialsTokens,
    client_secret,
    secure_url,
)
from answerlineage_emitter.fingerprint import FingerprintKey, read_key
from answerlineage_emitter.outbox import (
    DEFAULT_FLUSH_SECONDS,
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_EVENTS,
    Outbox,
    OutboxSettings,
)
from answerlineage_emitter.pseudonym import PseudonymKey, read_pseudonym_key
from answerlineage_emitter.sink import EventSink, NullSink

LOGGER: Final = logging.getLogger("answerlineage_adapters")

ENV_API_URL: Final = "ANSWERLINEAGE_ADAPTER_API_URL"
ENV_FINGERPRINT_KEY: Final = "ANSWERLINEAGE_ADAPTER_FINGERPRINT_KEY"
ENV_FINGERPRINT_KEY_FILE: Final = "ANSWERLINEAGE_ADAPTER_FINGERPRINT_KEY_FILE"
ENV_PSEUDONYM_KEY: Final = "ANSWERLINEAGE_ADAPTER_PSEUDONYM_KEY"
ENV_PSEUDONYM_KEY_FILE: Final = "ANSWERLINEAGE_ADAPTER_PSEUDONYM_KEY_FILE"
ENV_OIDC_ISSUER: Final = "ANSWERLINEAGE_ADAPTER_OIDC_ISSUER"
ENV_OIDC_CLIENT_ID: Final = "ANSWERLINEAGE_ADAPTER_OIDC_CLIENT_ID"
# Le secret : d'un fichier ou de l'environnement, jamais du code ni de la ligne de commande.
ENV_OIDC_CLIENT_SECRET: Final = "ANSWERLINEAGE_ADAPTER_OIDC_CLIENT_SECRET"  # noqa: S105
ENV_OIDC_CLIENT_SECRET_FILE: Final = "ANSWERLINEAGE_ADAPTER_OIDC_CLIENT_SECRET_FILE"  # noqa: S105
ENV_OIDC_SCOPE: Final = "ANSWERLINEAGE_ADAPTER_OIDC_SCOPE"
ENV_OIDC_AUDIENCE: Final = "ANSWERLINEAGE_ADAPTER_OIDC_AUDIENCE"

# L'espace de noms et le nom du job de l'agent : une forme que l'ingestion admet, sans
# blanc ni balise, bornée. Le nom d'un sous-agent y ajoute « / » et son type.
_JOB_PART: Final = re.compile(r"[A-Za-z0-9_.:/@-]{1,255}")


@dataclass(frozen=True)
class AgentJob:
    """Le job de l'agent dans le lignage : ``agent://<domaine>`` et le nom de l'agent."""

    namespace: str
    name: str

    @classmethod
    def create(cls, namespace: str, name: str) -> AgentJob:
        """Contrôle la forme des deux noms ; le message d'un refus ne les cite pas."""
        for what, value in (("l'espace de noms", namespace), ("le nom", name)):
            if not _JOB_PART.fullmatch(value):
                raise ConfigurationError(
                    f"Agent : {what} compte 1 à 255 lettres, chiffres et signes . _ : / @ -."
                )
        return cls(namespace, name)

    def job(self) -> dict[str, str]:
        return {"namespace": self.namespace, "name": self.name}


@dataclass(frozen=True)
class AdapterSettings:
    """Ce que l'adaptateur sait de son déploiement : l'agent, l'API, les clés, son identité."""

    agent: AgentJob
    outbox: OutboxSettings | None = None
    credentials: ClientCredentials | None = field(default=None, repr=False)
    fingerprint_key: FingerprintKey | None = field(default=None, repr=False)
    pseudonym_key: PseudonymKey | None = field(default=None, repr=False)

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str],
        *,
        agent: AgentJob,
        max_events: int = DEFAULT_MAX_EVENTS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        flush_seconds: float = DEFAULT_FLUSH_SECONDS,
    ) -> AdapterSettings:
        """Lit les réglages de l'environnement ; les bornes de la boîte d'envoi se donnent ici."""
        api_url = environment.get(ENV_API_URL) or None
        outbox = (
            None
            if api_url is None
            else OutboxSettings.create(
                api_url, max_events=max_events, max_bytes=max_bytes, flush_seconds=flush_seconds
            )
        )
        key = read_key(
            environment, variable=ENV_FINGERPRINT_KEY, file_variable=ENV_FINGERPRINT_KEY_FILE
        )
        pseudonyms = read_pseudonym_key(
            environment, variable=ENV_PSEUDONYM_KEY, file_variable=ENV_PSEUDONYM_KEY_FILE
        )
        if key is not None and pseudonyms is not None and pseudonyms.shares_secret_with(key):
            raise ConfigurationError(
                f"La clé des pseudonymes ({ENV_PSEUDONYM_KEY}) doit différer de la clé "
                f"d'empreinte ({ENV_FINGERPRINT_KEY}) : l'API tient la première, et ne doit pas "
                "pouvoir éprouver les arguments des appels d'outils."
            )
        return cls(agent, outbox, _credentials(environment, outbox), key, pseudonyms)

    def sink(self, logger: logging.Logger = LOGGER) -> EventSink:
        """La destination des événements : la boîte d'envoi vers l'API, avec le jeton du
        client quand il est réglé ; sans adresse d'API, une destination qui compte sans
        envoyer."""
        if self.outbox is None:
            logger.warning(
                "Aucune adresse d'API (%s) : les appels d'outils de l'agent sont comptés sans "
                "être envoyés.",
                ENV_API_URL,
            )
            return NullSink()
        tokens = (
            None
            if self.credentials is None
            else ClientCredentialsTokens(self.credentials, logger=logger)
        )
        return Outbox(self.outbox, tokens=tokens, logger=logger)


def _credentials(
    environment: Mapping[str, str], outbox: OutboxSettings | None
) -> ClientCredentials | None:
    """L'identité de l'adaptateur envers l'API. Aucune sans émetteur ; un réglage partiel est
    refusé."""
    issuer = environment.get(ENV_OIDC_ISSUER) or None
    client_id = environment.get(ENV_OIDC_CLIENT_ID) or None
    secret = client_secret(
        environment, variable=ENV_OIDC_CLIENT_SECRET, file_variable=ENV_OIDC_CLIENT_SECRET_FILE
    )
    if issuer is None and client_id is None and secret is None:
        return None
    if issuer is None or client_id is None or secret is None:
        raise ConfigurationError(
            f"Le jeton de l'adaptateur demande l'émetteur ({ENV_OIDC_ISSUER}), l'identifiant du "
            f"client ({ENV_OIDC_CLIENT_ID}) et son secret ({ENV_OIDC_CLIENT_SECRET_FILE} ou "
            f"{ENV_OIDC_CLIENT_SECRET})."
        )
    if outbox is None:
        raise ConfigurationError(
            f"Un jeton OIDC est réglé sans adresse d'API ({ENV_API_URL}) : il ne servirait à rien."
        )
    if not secure_url(outbox.api_url):
        raise ConfigurationError(
            "Le jeton de l'adaptateur ne part qu'en https, ou en http vers la boucle locale : "
            "l'adresse de l'API doit être en https."
        )
    return ClientCredentials.create(
        issuer,
        client_id,
        secret,
        scope=environment.get(ENV_OIDC_SCOPE) or None,
        audience=environment.get(ENV_OIDC_AUDIENCE) or None,
    )
