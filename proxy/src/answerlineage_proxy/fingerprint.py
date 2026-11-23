"""Empreintes à clé des arguments et du résultat d'un appel d'outil.

Le calcul est celui de tout émetteur (``answerlineage_emitter.fingerprint``) :
HMAC-SHA-256 avec la clé du locataire, sur l'étiquette de l'usage, un octet nul
et les octets empreints. Le proxy empreint la forme canonique JSON (RFC 8785)
de ``params.arguments``, et les octets du corps de la réponse tels qu'il les
relaie.
"""

from __future__ import annotations

from answerlineage_emitter.fingerprint import (
    ARGUMENTS_LABEL,
    FINGERPRINT_PREFIX,
    MAX_KEY_BYTES,
    MIN_KEY_BYTES,
    RESULT_LABEL,
    Digest,
    FingerprintKey,
    arguments_fingerprint,
    canonical_json,
)

__all__ = [
    "ARGUMENTS_LABEL",
    "FINGERPRINT_PREFIX",
    "MAX_KEY_BYTES",
    "MIN_KEY_BYTES",
    "RESULT_LABEL",
    "Digest",
    "FingerprintKey",
    "arguments_fingerprint",
    "canonical_json",
]
