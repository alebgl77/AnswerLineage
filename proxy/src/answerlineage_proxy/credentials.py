"""Le jeton du proxy envers l'API d'AnswerLineage : octroi ``client_credentials``.

L'octroi est celui de tout émetteur (``answerlineage_emitter.credentials``) :
point de jeton découvert, jeton gardé en mémoire et renouvelé avant d'expirer,
demandé par les seules tâches de fond. Ce module dit où le proxy lit ses
réglages : ses variables d'environnement, et le fichier ou la variable de son
secret, jamais sa ligne de commande. Le secret n'est pas transmis au serveur
MCP lancé en stdio.

Ce jeton n'a rien à voir avec ceux des agents, que le proxy relaie sans les
lire ni les garder : c'est l'identité du proxy lui-même, émetteur
d'événements, auprès de l'API.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from answerlineage_emitter import credentials as _emitter
from answerlineage_emitter.credentials import (
    COOL_DOWN,
    MAX_RESPONSE_BYTES,
    ClientCredentials,
    ClientCredentialsTokens,
    Tokens,
    TokenUnavailableError,
    secure_url,
)

__all__ = [
    "COOL_DOWN",
    "ENV_AUDIENCE",
    "ENV_CLIENT_ID",
    "ENV_CLIENT_SECRET",
    "ENV_CLIENT_SECRET_FILE",
    "ENV_ISSUER",
    "ENV_SCOPE",
    "MAX_RESPONSE_BYTES",
    "ClientCredentials",
    "ClientCredentialsTokens",
    "TokenUnavailableError",
    "Tokens",
    "client_secret",
    "secure_url",
]

# Réglages du jeton : l'émetteur, l'identifiant du client, son secret, et ce
# que certains émetteurs demandent en plus (portée, audience).
ENV_ISSUER: Final = "ANSWERLINEAGE_PROXY_OIDC_ISSUER"
ENV_CLIENT_ID: Final = "ANSWERLINEAGE_PROXY_OIDC_CLIENT_ID"
# Le secret : jamais sur la ligne de commande, que la liste des processus montre.
ENV_CLIENT_SECRET: Final = "ANSWERLINEAGE_PROXY_OIDC_CLIENT_SECRET"  # noqa: S105
ENV_CLIENT_SECRET_FILE: Final = "ANSWERLINEAGE_PROXY_OIDC_CLIENT_SECRET_FILE"  # noqa: S105
ENV_SCOPE: Final = "ANSWERLINEAGE_PROXY_OIDC_SCOPE"
ENV_AUDIENCE: Final = "ANSWERLINEAGE_PROXY_OIDC_AUDIENCE"


def client_secret(environment: Mapping[str, str], secret_file: str | None = None) -> str | None:
    """Le secret du client du proxy : dans un fichier (``secret_file``, sinon la variable
    ``ANSWERLINEAGE_PROXY_OIDC_CLIENT_SECRET_FILE``) ou dans la variable
    ``ANSWERLINEAGE_PROXY_OIDC_CLIENT_SECRET``. ``None`` sans secret.

    Aucun message ne cite le secret, ni le chemin du fichier.
    """
    return _emitter.client_secret(
        environment,
        secret_file,
        variable=ENV_CLIENT_SECRET,
        file_variable=ENV_CLIENT_SECRET_FILE,
    )
