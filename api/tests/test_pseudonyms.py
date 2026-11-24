"""Le pseudonyme d'un destinataire, son calcul à l'octet près, et le coffre à clés."""

from __future__ import annotations

import json
import pickle
import secrets
from pathlib import Path
from typing import Any

import pytest

from answerlineage_api.pseudonyms.vault import (
    ENV_KEYS,
    ENV_KEYS_FILE,
    IDENTITY_MAX,
    MAX_KEYS,
    LocalVault,
    PseudonymKey,
    VaultConfigurationError,
    canonical_identity,
    is_pseudonym,
    pseudonym_of,
    vault_from_settings,
)

pytestmark = pytest.mark.anyio

VECTORS: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[2] / "spec" / "pseudonym" / "vectors-v1.json").read_text(
        encoding="utf-8"
    )
)
TENANTS = sorted(VECTORS["keys"])


def material() -> str:
    """Une clé tirée pour le test, en hexadécimal : née et morte ici."""
    return secrets.token_hex(32)


@pytest.mark.parametrize("vector", VECTORS["identities"], ids=lambda vector: vector["name"])
@pytest.mark.parametrize("tenant", TENANTS)
def test_the_published_vectors_give_the_published_pseudonyms(
    vector: dict[str, Any], tenant: str
) -> None:
    key = PseudonymKey.from_hex(VECTORS["keys"][tenant])
    assert canonical_identity(vector["identity"]) == vector["canonical"].encode()
    assert pseudonym_of(key, vector["identity"]) == vector["pseudonyms"][tenant]


@pytest.mark.parametrize("vector", VECTORS["refused"], ids=lambda vector: vector["name"])
def test_an_identity_with_a_blank_a_control_or_an_excess_has_no_pseudonym(
    vector: dict[str, Any],
) -> None:
    key = PseudonymKey.from_hex(VECTORS["keys"][TENANTS[0]])
    assert canonical_identity(vector["identity"]) is None
    assert pseudonym_of(key, vector["identity"]) is None


def test_the_length_bound_counts_characters_of_the_canonical_form() -> None:
    assert canonical_identity("é" * IDENTITY_MAX) is not None
    assert canonical_identity("é" * (IDENTITY_MAX + 1)) is None
    # Un demi-codet isolé, qu'un JSON peut porter, n'est pas une identité.
    assert canonical_identity("camille" + chr(0xD800)) is None


def test_two_tenants_give_the_same_identity_two_unrelated_pseudonyms() -> None:
    first, second = (
        pseudonym_of(PseudonymKey.from_hex(VECTORS["keys"][tenant]), "camille.martin@exemple.fr")
        for tenant in TENANTS
    )
    assert first != second
    assert first is not None
    assert second is not None
    assert is_pseudonym(first)
    assert is_pseudonym(second)


@pytest.mark.parametrize(
    "value",
    [
        "camille.martin@exemple.fr",
        "hmac-sha256:" + "0" * 63,
        "hmac-sha256:" + "0" * 65,
        "hmac-sha256:" + "A" * 64,
        "sha256:" + "0" * 64,
        "HMAC-SHA256:" + "0" * 64,
        "0" * 64,
        "hmac-sha256:" + "0" * 64 + "\n",
        42,
        None,
    ],
)
def test_only_the_hmac_form_is_a_pseudonym(value: object) -> None:
    assert not is_pseudonym(value)
    assert is_pseudonym("hmac-sha256:" + "0123456789abcdef" * 4)


async def test_the_vault_gives_a_pseudonym_under_each_key_it_holds() -> None:
    current, previous = PseudonymKey.from_hex(material()), PseudonymKey.from_hex(material())
    vault = LocalVault([current, previous])
    canonical = canonical_identity("Camille.Martin@Exemple.FR")
    assert canonical is not None
    assert await vault.pseudonyms(canonical) == (
        pseudonym_of(current, "camille.martin@exemple.fr"),
        pseudonym_of(previous, "camille.martin@exemple.fr"),
    )


def test_a_key_never_shows_and_never_serializes() -> None:
    written = material()
    key = PseudonymKey.from_hex(written)
    vault = LocalVault([key])
    for shown in (repr(key), str(key), repr(vault), str(vault)):
        assert written not in shown
        assert written.upper() not in shown
    with pytest.raises(TypeError):
        pickle.dumps(key)
    with pytest.raises(TypeError):
        pickle.dumps(vault)


@pytest.mark.parametrize(
    "written",
    [
        "zz" * 32,
        "ab" * 31,
        "ab" * 65,
        "abc",
    ],
    ids=["pas de l'hexadécimal", "trop courte", "trop longue", "impaire"],
)
def test_a_wrong_key_is_refused_without_being_cited(written: str) -> None:
    with pytest.raises(VaultConfigurationError) as refused:
        vault_from_settings({ENV_KEYS: written})
    assert written not in str(refused.value)


def test_without_any_setting_there_is_no_vault() -> None:
    assert vault_from_settings({}) is None
    assert vault_from_settings({ENV_KEYS: "  ", ENV_KEYS_FILE: ""}) is None


async def test_the_keys_come_from_the_variable_current_first(tmp_path: Path) -> None:
    current, previous = material(), material()
    vault = vault_from_settings({ENV_KEYS: f"{current}, {previous}"})
    assert vault is not None
    canonical = b"u07ab12cd"
    expected = (
        PseudonymKey.from_hex(current).pseudonym(canonical),
        PseudonymKey.from_hex(previous).pseudonym(canonical),
    )
    assert await vault.pseudonyms(canonical) == expected


async def test_the_keys_come_from_a_file_one_per_line(tmp_path: Path) -> None:
    current, previous = material(), material()
    path = tmp_path / "cles"
    path.write_text(f"{current}\n{previous}\n", encoding="ascii")
    vault = vault_from_settings({ENV_KEYS_FILE: str(path)})
    assert vault is not None
    assert len(await vault.pseudonyms(b"x")) == 2


@pytest.mark.parametrize(
    ("setting", "message"),
    [
        (lambda path: {ENV_KEYS_FILE: str(path), ENV_KEYS: material()}, "pas des deux"),
        (lambda path: {ENV_KEYS_FILE: str(path / "absent")}, "illisible"),
        (lambda path: {ENV_KEYS: ",".join(material() for _ in range(MAX_KEYS + 1))}, "au plus"),
        (lambda path: {ENV_KEYS: " , ,"}, "aucune clé"),
    ],
    ids=["fichier et variable", "fichier illisible", "trop de clés", "aucune clé"],
)
def test_a_wrong_vault_setting_names_the_setting_never_the_value(
    tmp_path: Path, setting: Any, message: str
) -> None:
    given: dict[str, str] = setting(tmp_path)
    with pytest.raises(VaultConfigurationError) as refused:
        vault_from_settings(given)
    assert message in str(refused.value)
    for value in given.values():
        assert value not in str(refused.value)


def test_a_key_file_too_large_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "cles"
    path.write_text("\n".join(material() for _ in range(70)), encoding="ascii")
    with pytest.raises(VaultConfigurationError, match="trop grand"):
        vault_from_settings({ENV_KEYS_FILE: str(path)})


def test_a_vault_holds_one_to_eight_keys() -> None:
    with pytest.raises(VaultConfigurationError):
        LocalVault([])
    with pytest.raises(VaultConfigurationError):
        LocalVault([PseudonymKey.from_hex(material()) for _ in range(MAX_KEYS + 1)])
    with pytest.raises(VaultConfigurationError):
        PseudonymKey(b"courte")
