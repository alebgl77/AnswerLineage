"""Le pseudonyme d'un destinataire, calculé par l'émetteur là où il connaît l'identité.

Le calcul est écrit ici sans rien de l'API : les vecteurs publiés dans
``spec/pseudonym/vectors-v1.json`` le fixent à l'octet près, et un test de
contrat le tient d'accord avec celui de l'API.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import pickle
import secrets
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from answerlineage_emitter.config import ConfigurationError
from answerlineage_emitter.fingerprint import FingerprintKey
from answerlineage_emitter.pseudonym import (
    IDENTITY_MAX,
    RECIPIENT_LABEL,
    PseudonymKey,
    canonical_identity,
    read_pseudonym_key,
)

VECTORS: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[2] / "spec" / "pseudonym" / "vectors-v1.json").read_text(
        encoding="utf-8"
    )
)
TENANTS = sorted(VECTORS["keys"])
VARIABLE, FILE_VARIABLE = "EMETTEUR_CLE_PSEUDONYMES", "EMETTEUR_CLE_PSEUDONYMES_FILE"


@pytest.mark.parametrize("tenant", TENANTS)
@pytest.mark.parametrize("vector", VECTORS["identities"], ids=lambda vector: vector["name"])
def test_the_published_vectors_give_the_published_pseudonyms(
    vector: dict[str, Any], tenant: str
) -> None:
    key = PseudonymKey.from_hex(VECTORS["keys"][tenant])
    assert VECTORS["label"].encode("ascii") == RECIPIENT_LABEL
    assert canonical_identity(vector["identity"]) == vector["canonical"].encode("utf-8")
    assert key.pseudonym(vector["identity"]) == vector["pseudonyms"][tenant]


@pytest.mark.parametrize("vector", VECTORS["refused"], ids=lambda vector: vector["name"])
def test_an_identity_with_a_blank_a_control_or_an_excess_has_no_pseudonym(
    vector: dict[str, Any],
) -> None:
    key = PseudonymKey.from_hex(VECTORS["keys"][TENANTS[0]])
    assert canonical_identity(vector["identity"]) is None
    assert key.pseudonym(vector["identity"]) is None


@pytest.mark.parametrize("value", [None, 42, b"camille@exemple.fr", ["camille@exemple.fr"]])
def test_what_is_not_a_text_has_no_pseudonym(value: object) -> None:
    assert canonical_identity(value) is None


def test_the_bound_counts_characters_of_the_canonical_form() -> None:
    """La normalisation réunit au plus quatre caractères en un : une identité écrite en
    caractères décomposés a sa forme canonique comptée après réunion."""
    # « ᾂ » s'écrit en quatre caractères décomposés, et en un seul une fois normalisé.
    decomposed = unicodedata.normalize("NFD", "ᾂ")
    assert len(decomposed) == 4
    at_the_bound = decomposed * IDENTITY_MAX
    assert canonical_identity(at_the_bound) == ("ᾂ" * IDENTITY_MAX).encode("utf-8")
    assert canonical_identity(decomposed * (IDENTITY_MAX + 1)) is None
    assert canonical_identity("a" * IDENTITY_MAX) is not None
    assert canonical_identity("a" * (IDENTITY_MAX + 1)) is None
    assert canonical_identity("a" * 10_000_000) is None


def test_the_calculation_is_a_plain_hmac_of_the_label_and_the_canonical_form() -> None:
    secret = secrets.token_bytes(32)
    expected = hmac.new(
        secret, RECIPIENT_LABEL + b"\x00" + b"camille.martin@exemple.fr", hashlib.sha256
    ).hexdigest()
    assert PseudonymKey(secret).pseudonym("Camille.Martin@Exemple.FR") == "hmac-sha256:" + expected


def test_a_key_never_shows_and_never_serializes() -> None:
    written = secrets.token_hex(32)
    key = PseudonymKey.from_hex(written)
    assert written not in repr(key)
    assert written not in str(key)
    assert key == PseudonymKey.from_hex(written.upper())
    assert key != PseudonymKey.from_hex(secrets.token_hex(32))
    assert key != written
    assert hash(key) == hash(PseudonymKey.from_hex(secrets.token_hex(32)))
    with pytest.raises(TypeError):
        pickle.dumps(key)


def test_a_key_tells_whether_a_fingerprint_key_shares_its_secret() -> None:
    """La clé des pseudonymes n'est pas celle des empreintes : l'émetteur le vérifie sans
    qu'aucune des deux livre son secret."""
    secret = secrets.token_bytes(32)
    key = PseudonymKey(secret)
    assert key.shares_secret_with(FingerprintKey(secret))
    assert not key.shares_secret_with(FingerprintKey(secrets.token_bytes(32)))


@pytest.mark.parametrize(
    "written",
    ["zz" * 32, "ab" * 31, "ab" * 65, ""],
    ids=["pas hexadécimal", "courte", "longue", "vide"],
)
def test_a_wrong_key_is_refused_without_being_cited(written: str) -> None:
    with pytest.raises(ValueError) as refused:
        PseudonymKey.from_hex(written)
    if written:
        assert written not in str(refused.value)


@pytest.mark.parametrize("size", [0, 31, 65])
def test_a_secret_of_the_wrong_size_is_refused(size: int) -> None:
    with pytest.raises(ValueError, match="octets"):
        PseudonymKey(bytes(size))


def test_the_key_comes_from_the_variable_or_from_a_file(tmp_path: Path) -> None:
    written = secrets.token_hex(32)
    assert read_pseudonym_key({}, variable=VARIABLE, file_variable=FILE_VARIABLE) is None
    from_variable = read_pseudonym_key(
        {VARIABLE: written}, variable=VARIABLE, file_variable=FILE_VARIABLE
    )
    assert from_variable == PseudonymKey.from_hex(written)
    held = tmp_path / "cle"
    held.write_text(written + "\n", encoding="ascii")
    from_file = read_pseudonym_key(
        {FILE_VARIABLE: str(held)}, variable=VARIABLE, file_variable=FILE_VARIABLE
    )
    assert from_file == from_variable


def test_an_unusable_key_source_is_refused_without_being_cited(tmp_path: Path) -> None:
    written = secrets.token_hex(32)
    held = tmp_path / "cle"
    held.write_text(written, encoding="ascii")
    too_large = tmp_path / "grand"
    too_large.write_text("ab" * 4096, encoding="ascii")
    for environment in (
        {VARIABLE: written, FILE_VARIABLE: str(held)},
        {FILE_VARIABLE: str(tmp_path / "absent")},
        {FILE_VARIABLE: str(too_large)},
        {VARIABLE: written[:10]},
    ):
        with pytest.raises(ConfigurationError) as refused:
            read_pseudonym_key(environment, variable=VARIABLE, file_variable=FILE_VARIABLE)
        message = str(refused.value)
        assert "pseudonymes" in message
        assert written[:10] not in message
        assert str(tmp_path) not in message
