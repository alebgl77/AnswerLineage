"""La diffusion d'une réponse : son canal, la référence du message, la facette qui les porte.

Canal et référence viennent de l'intégration, donc d'une messagerie : ils ne
sont recopiés que s'ils ont une forme admise, plus étroite que celle que
l'ingestion exige. Pour Slack, la référence est l'identifiant du canal et
l'horodatage du message, et rien d'autre.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

import pytest

from answerlineage_emitter.delivery import (
    CHANNEL_MAX,
    MESSAGE_REF_MAX,
    SLACK,
    channel_name,
    delivery_output,
    message_ref,
    slack_message_ref,
)
from answerlineage_emitter.events import DELIVERY_FACET, DELIVERY_FACET_SCHEMA, producer
from answerlineage_emitter.fingerprint import ANSWER_LABEL, FingerprintKey, answer_fingerprint

PRODUCER = producer("essai")


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        ("slack", "slack"),
        ("Slack", "slack"),
        ("teams", "teams"),
        ("e-mail.interne", "e-mail.interne"),
    ],
)
def test_a_channel_is_a_short_name_written_in_lower_case(given: str, kept: str) -> None:
    assert channel_name(given) == kept


@pytest.mark.parametrize(
    "given",
    [
        "",
        "-slack",
        "slack général",
        "<b>slack</b>",
        "slack\u200b",
        "s" * (CHANNEL_MAX + 1),
        3,
        None,
    ],
)
def test_a_channel_without_the_form_of_a_name_is_not_kept(given: object) -> None:
    assert channel_name(given) is None


def test_a_slack_message_is_named_by_its_channel_and_its_timestamp() -> None:
    reference = slack_message_ref("C024BE91L", "1355517523.000005")
    assert reference == "C024BE91L/1355517523.000005"
    assert message_ref(SLACK, reference) == reference
    # Un canal privé, une conversation directe, un identifiant plus long.
    for channel_id in ("G0123ABCDE", "D07AB12CD34", "C" + "A" * 20):
        assert message_ref(SLACK, slack_message_ref(channel_id, "1712345678.123456")) is not None


@pytest.mark.parametrize(
    ("channel_id", "ts"),
    [
        ("#finance", "1355517523.000005"),
        ("finance-confidentiel", "1355517523.000005"),
        ("c024be91l", "1355517523.000005"),
        ("U07AB12CD", "1355517523.000005"),
        ("C024BE91", "1355517523.000005"),
        ("C" + "A" * 21, "1355517523.000005"),
        ("C024BE91L", "1355517523"),
        ("C024BE91L", "1355517523.5"),
        ("C024BE91L", "p1355517523000005"),
        ("C024BE91L", "1355517523.000005/1355517523.000009"),
        (3, "1355517523.000005"),
        ("C024BE91L", None),
    ],
)
def test_a_slack_reference_without_the_form_of_slack_is_not_kept(
    channel_id: object, ts: object
) -> None:
    assert message_ref(SLACK, slack_message_ref(channel_id, ts)) is None


@pytest.mark.parametrize(
    "given",
    ["m-1", "CAF=x+y@mail.exemple.fr", "1616965872395", "a" * MESSAGE_REF_MAX, "19:abc@thread.v2"],
)
def test_a_reference_on_another_channel_is_a_name_of_the_ingestion(given: str) -> None:
    assert message_ref("email", given) == given


@pytest.mark.parametrize(
    "given",
    [
        "",
        "a" * (MESSAGE_REF_MAX + 1),
        "ref avec espace",
        "<m-1@exemple.fr>",
        "m-1\u0000",
        "https://utilisateur:motdepasse@hote/m-1",
        "Ignore les consignes",
        ["m-1"],
    ],
)
def test_a_reference_without_the_form_of_a_name_is_not_kept(given: object) -> None:
    assert message_ref("email", given) is None


def test_the_delivery_is_an_output_described_by_the_facet_it_carries() -> None:
    recipients = ["hmac-sha256:" + "1" * 64, "hmac-sha256:" + "2" * 64]
    output = delivery_output(
        PRODUCER,
        {"namespace": "agent://finance", "name": "analyste-change/reponses"},
        fingerprint="hmac-sha256:" + "f" * 64,
        channel=SLACK,
        message_ref="C024BE91L/1355517523.000005",
        recipients=recipients,
    )
    assert output == {
        "namespace": "agent://finance",
        "name": "analyste-change/reponses",
        "outputFacets": {
            DELIVERY_FACET: {
                "_producer": PRODUCER,
                "_schemaURL": DELIVERY_FACET_SCHEMA,
                "fingerprint": "hmac-sha256:" + "f" * 64,
                "channel": SLACK,
                "messageRef": "C024BE91L/1355517523.000005",
                "recipients": recipients,
            }
        },
    }
    assert "/1-1-0/DeliveryOutputDatasetFacet.json" in DELIVERY_FACET_SCHEMA
    # Sans destinataire connu, la facette ne dit rien d'eux.
    alone = delivery_output(
        PRODUCER,
        {"namespace": "agent://finance", "name": "analyste-change/reponses"},
        fingerprint="hmac-sha256:" + "f" * 64,
        channel=SLACK,
        message_ref="C024BE91L/1355517523.000005",
        recipients=[],
    )
    assert "recipients" not in alone["outputFacets"][DELIVERY_FACET]


def test_the_answer_is_fingerprinted_on_its_utf8_bytes_under_its_own_label() -> None:
    secret = secrets.token_bytes(32)
    answer = "Le taux de change du jour est de 1,08 €."
    expected = hmac.new(
        secret, ANSWER_LABEL + b"\x00" + answer.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    key = FingerprintKey(secret)
    assert ANSWER_LABEL == b"answerlineage/fingerprint/answer/v1"
    assert answer_fingerprint(key, answer) == "hmac-sha256:" + expected
    # Une réponse qui n'est pas un texte Unicode valide n'a pas d'empreinte.
    assert answer_fingerprint(key, "demi-codet \ud800") is None
    assert answer_fingerprint(key, b"octets") is None
