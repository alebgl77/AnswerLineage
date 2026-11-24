"""Contrat entre composants : la diffusion que l'émetteur construit, l'ingestion l'admet.

Le pseudonyme d'un destinataire se calcule deux fois : dans l'émetteur, là où
l'identité est connue, et dans l'API, pour révéler. Chacun est écrit de son
côté, pour qu'aucun composant ne dépende de l'autre : les vecteurs publiés et
ce test les tiennent d'accord, identité par identité. De même, un canal ou une
référence de message que l'émetteur garde a la forme qu'exige l'ingestion :
une diffusion acceptée par l'émetteur ne fait jamais rejeter son événement.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from answerlineage_api import identifiers
from answerlineage_api.ingest import models
from answerlineage_api.pseudonyms import vault as api
from answerlineage_emitter import delivery, pseudonym
from answerlineage_emitter.fingerprint import FINGERPRINT_PREFIX

VECTORS: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[1] / "spec" / "pseudonym" / "vectors-v1.json").read_text(
        encoding="utf-8"
    )
)
SECRET = secrets.token_bytes(32)
FROM_EMITTER = pseudonym.PseudonymKey(SECRET)
FROM_API = api.PseudonymKey(SECRET)
# Des identités qui éprouvent la casse, la normalisation et les caractères refusés.
TRICKY = st.lists(
    st.sampled_from(
        [
            "a",
            "Z",
            "é",
            "e\u0301",
            "ß",
            "İ",
            "Σ",
            "ᾂ",
            "@",
            ".",
            "+",
            " ",
            "\u00a0",
            "\u200b",
            "\u2028",
            "\u0000",
            "\ud800",
            "\U0001f642",
        ]
    ),
    max_size=40,
).map("".join)


def test_the_label_the_prefix_the_bound_and_the_key_sizes_are_those_of_the_api() -> None:
    assert pseudonym.RECIPIENT_LABEL == api.RECIPIENT_LABEL
    assert pseudonym.PSEUDONYM_PREFIX == api.PSEUDONYM_PREFIX == FINGERPRINT_PREFIX
    assert pseudonym.IDENTITY_MAX == api.IDENTITY_MAX
    assert (pseudonym.MIN_KEY_BYTES, pseudonym.MAX_KEY_BYTES) == (
        api.MIN_KEY_BYTES,
        api.MAX_KEY_BYTES,
    )
    assert delivery.CHANNEL_MAX == identifiers.CHANNEL_MAX
    assert delivery.MESSAGE_REF_MAX == identifiers.MESSAGE_REF_MAX
    assert delivery.RECIPIENTS_MAX == models.RECIPIENTS_MAX


@pytest.mark.parametrize("tenant", sorted(VECTORS["keys"]))
def test_both_calculations_give_the_published_vectors(tenant: str) -> None:
    from_emitter = pseudonym.PseudonymKey.from_hex(VECTORS["keys"][tenant])
    from_api = api.PseudonymKey.from_hex(VECTORS["keys"][tenant])
    for vector in VECTORS["identities"]:
        expected = vector["pseudonyms"][tenant]
        assert from_emitter.pseudonym(vector["identity"]) == expected
        assert api.pseudonym_of(from_api, vector["identity"]) == expected
    for vector in VECTORS["refused"]:
        assert from_emitter.pseudonym(vector["identity"]) is None
        assert api.pseudonym_of(from_api, vector["identity"]) is None


@settings(max_examples=500)
@given(identity=st.one_of(st.text(max_size=40), TRICKY))
def test_on_any_identity_the_emitter_and_the_api_agree(identity: str) -> None:
    from_emitter = FROM_EMITTER.pseudonym(identity)
    assert from_emitter == api.pseudonym_of(FROM_API, identity)
    if from_emitter is not None:
        assert api.is_pseudonym(from_emitter)


@pytest.mark.parametrize("repeat", [api.IDENTITY_MAX, api.IDENTITY_MAX + 1])
def test_at_the_bound_the_emitter_and_the_api_agree(repeat: int) -> None:
    """Le calcul de l'émetteur écarte d'emblée une identité trop longue pour avoir une forme
    canonique admise : quatre caractères décomposés par caractère canonique, au pire."""
    for unit in ("a", "İ", "e\u0301", "\u03b1\u0313\u0300\u0345"):
        identity = unit * repeat
        assert FROM_EMITTER.pseudonym(identity) == api.pseudonym_of(FROM_API, identity)


# Des canaux et des références qui éprouvent la forme d'un nom pour l'ingestion.
NAMES = st.lists(
    st.sampled_from(
        [*"aZ09._-:/@=+~%,#$*[]<>\"'` ", "é", "\u200b", "\u0000", "\uff1c", "://", "a:b@"]
    ),
    max_size=30,
).map("".join)


@settings(max_examples=500)
@given(value=NAMES)
def test_a_channel_the_emitter_keeps_is_a_name_for_the_ingestion(value: str) -> None:
    kept = delivery.channel_name(value)
    if kept is not None:
        assert identifiers.identifier_fault(kept, max_length=identifiers.CHANNEL_MAX) is None


@settings(max_examples=500)
@given(value=NAMES, channel=st.sampled_from([delivery.SLACK, "email", "teams"]))
def test_a_reference_the_emitter_keeps_is_a_name_for_the_ingestion(
    value: str, channel: str
) -> None:
    kept = delivery.message_ref(channel, value)
    if kept is not None:
        assert identifiers.identifier_fault(kept, max_length=identifiers.MESSAGE_REF_MAX) is None


def test_a_slack_reference_is_a_name_for_the_ingestion() -> None:
    reference = delivery.slack_message_ref("C" + "0" * 20, "9999999999.999999")
    assert delivery.message_ref(delivery.SLACK, reference) == reference
    assert identifiers.identifier_fault(reference, max_length=identifiers.MESSAGE_REF_MAX) is None
