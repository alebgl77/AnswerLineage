"""Signature de la diffusion des périmètres en quarantaine.

Les proxys apprennent les périmètres en quarantaine par ``GET /api/v1/quarantine``.
Avec un secret partagé avec eux, l'API signe et date chaque réponse, ``200``
comme ``304``, dans un en-tête :

    AnswerLineage-Signature: t=1794819310,v1=af95…

- ``t`` est l'heure de signature, en secondes depuis le 1er janvier 1970 (UTC),
  écrite en décimal ;
- ``v1`` est HMAC-SHA-256, en hexadécimal minuscule, sous le secret partagé, de
  l'étiquette ``answerlineage/quarantine/v1``, d'un octet nul, de ``t``, d'un
  point, puis de la charge : le corps d'un ``200`` tel qu'il part, ou, pour un
  ``304``, qui n'a pas de corps, la version que le proxy tient déjà. Le ``304``
  ne vaut ainsi que pour cette version, et seulement pendant la tolérance du
  proxy : rejoué, il ne prolonge pas un état levé. L'étiquette sépare ce domaine
  de celui des webhooks ;
- pendant une rotation, l'API signe sous le secret, puis sous le précédent : un
  proxy qui connaît l'un ou l'autre accepte.

Les secrets se règlent par ``ANSWERLINEAGE_QUARANTINE_SECRET`` et, pendant une
rotation, ``ANSWERLINEAGE_QUARANTINE_PREVIOUS_SECRET`` ; sans eux, la diffusion
n'est pas signée. Un secret refusé arrête l'API au démarrage, avec un message
qui nomme la variable, jamais sa valeur.

Le proxy vérifie de son côté (``answerlineage_proxy.quarantine_signature``) :
aucun des deux ne dépend de l'autre, et le test de contrat les tient d'accord
sur les vecteurs publiés (``spec/quarantine/v1/vectors.json``).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

HEADER: Final = "AnswerLineage-Signature"
SCHEME: Final = "v1"
LABEL: Final = b"answerlineage/quarantine/v1"

# Les noms des variables, pas des secrets.
ENV_SECRET: Final = "ANSWERLINEAGE_QUARANTINE_SECRET"  # noqa: S105
ENV_PREVIOUS_SECRET: Final = "ANSWERLINEAGE_QUARANTINE_PREVIOUS_SECRET"  # noqa: S105
# Un secret partagé : de l'ASCII imprimable sans espace, de 32 à 512 caractères.
MIN_SECRET_LENGTH: Final = 32
MAX_SECRET_LENGTH: Final = 512
_SECRET = re.compile(rf"[\x21-\x7e]{{{MIN_SECRET_LENGTH},{MAX_SECRET_LENGTH}}}")


class QuarantineSecretError(ValueError):
    """Un secret de la diffusion refusé. Le message nomme la variable, jamais sa valeur."""


def _checked(value: str, variable: str) -> str:
    if not _SECRET.fullmatch(value):
        raise QuarantineSecretError(
            f"{variable} doit être de l'ASCII imprimable sans espace, de {MIN_SECRET_LENGTH} à"
            f" {MAX_SECRET_LENGTH} caractères."
        )
    return value


def sign(secret: str, timestamp: int, payload: bytes) -> str:
    """La signature ``v1`` de la charge signée à ``timestamp``, sous ``secret``."""
    message = LABEL + b"\x00" + str(timestamp).encode("ascii") + b"." + payload
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def signature_header(secrets: Sequence[str], timestamp: int, payload: bytes) -> str:
    """La valeur de l'en-tête : l'heure de signature, puis une signature par secret."""
    if not 1 <= len(secrets) <= 2 or timestamp < 0:
        raise ValueError("Un ou deux secrets, et une heure de signature positive.")
    signatures = ",".join(f"{SCHEME}={sign(secret, timestamp, payload)}" for secret in secrets)
    return f"t={timestamp},{signatures}"


@dataclass(frozen=True)
class QuarantineSigning:
    """Les secrets sous lesquels la diffusion est signée : le secret, puis le précédent
    pendant une rotation. Jamais affichés."""

    secrets: tuple[str, ...] = field(repr=False)
    # L'horloge de la signature, en secondes depuis 1970.
    clock: Callable[[], float] = field(default=time.time, repr=False, compare=False)

    @classmethod
    def create(
        cls,
        secret: str,
        previous: str | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> QuarantineSigning:
        _checked(secret, ENV_SECRET)
        if previous is None:
            return cls((secret,), clock)
        return cls((secret, _checked(previous, ENV_PREVIOUS_SECRET)), clock)

    def header(self, payload: bytes) -> str:
        """L'en-tête d'une réponse dont la charge est ``payload``, signée maintenant."""
        return signature_header(self.secrets, int(self.clock()), payload)


def signing_from_settings(
    signing: QuarantineSigning | None, environment: Mapping[str, str]
) -> QuarantineSigning | None:
    """La signature de la diffusion : celle de l'appelant, sinon celle de l'environnement ;
    ``None`` sans secret."""
    if signing is not None:
        return signing
    secret = environment.get(ENV_SECRET) or None
    previous = environment.get(ENV_PREVIOUS_SECRET) or None
    if secret is None:
        if previous is not None:
            raise QuarantineSecretError(
                f"{ENV_PREVIOUS_SECRET} ne sert que pendant une rotation, avec {ENV_SECRET}."
            )
        return None
    return QuarantineSigning.create(secret, previous)
