"""Contrat entre le proxy et l'API : les périmètres en quarantaine que l'une diffuse, l'autre
les lit, et la signature que l'une pose, l'autre la vérifie.

Le proxy ne dépend pas de l'API : les modes qu'il connaît, les bornes de la
réponse et la vérification de la signature y sont écrits une seconde fois. Ce
test les tient d'accord : une réponse que l'API rend à ses bornes, cent
quarantaines de vingt jeux, tient dans ce que le proxy accepte de lire ; une
diffusion que l'API signe, le proxy la vérifie, sur les vecteurs publiés
(``spec/quarantine/v1/vectors.json``) comme sur une diffusion faite ici. Les
vecteurs eux-mêmes sont recalculés ici, en bibliothèque standard, sans l'une ni
l'autre écriture.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from starlette.responses import JSONResponse

from answerlineage_api.incidents import signature as api_signature
from answerlineage_api.incidents.models import (
    MAX_DATASETS,
    ActiveQuarantine,
    ActiveQuarantines,
    QuarantinedDataset,
)
from answerlineage_api.incidents.quarantine import version_of
from answerlineage_api.store.schema import MAX_ACTIVE_QUARANTINES, QUARANTINE_MODES
from answerlineage_proxy import quarantine as proxy
from answerlineage_proxy import quarantine_signature as proxy_signature
from answerlineage_proxy.config import ProxyConfigurationError

ROOT = Path(__file__).resolve().parents[1]
VECTORS: dict[str, Any] = json.loads(
    (ROOT / "spec" / "quarantine" / "v1" / "vectors.json").read_text(encoding="utf-8")
)
NOW = 1_794_819_310


def test_the_proxy_knows_the_modes_and_the_bounds_of_the_api() -> None:
    assert proxy.MODES == QUARANTINE_MODES
    assert proxy.MAX_QUARANTINES == MAX_ACTIVE_QUARANTINES
    assert proxy.MAX_DATASETS == MAX_DATASETS


def at_bounds() -> list[ActiveQuarantine]:
    return [
        ActiveQuarantine(
            incident_id=UUID(int=rank),
            mode="warn",
            activated_at=datetime(2026, 11, 16, 8, 55, rank % 60, 204517, tzinfo=UTC),
            datasets=[
                QuarantinedDataset(
                    namespace="bigquery://projet-données-référence",
                    name=f"entrepôt.ventes_{rank:03d}.commandes_détaillées_{index:02d}_"
                    + "x" * 160,
                )
                for index in range(MAX_DATASETS)
            ],
        )
        for rank in range(MAX_ACTIVE_QUARANTINES)
    ]


def rendered(quarantines: list[ActiveQuarantine]) -> tuple[ActiveQuarantines, bytes]:
    """La diffusion, et son corps tel que la route le rend."""
    document = ActiveQuarantines(version=version_of(quarantines), quarantines=quarantines)
    return document, bytes(JSONResponse(document.model_dump(mode="json")).body)


def test_a_response_of_the_api_at_its_bounds_is_read_whole_by_the_proxy() -> None:
    quarantines = at_bounds()
    document, body = rendered(quarantines)

    known = proxy.parse(body)
    assert len(body) <= proxy.MAX_RESPONSE_BYTES
    assert known.version == document.version
    assert len(known.scopes) == MAX_ACTIVE_QUARANTINES
    assert known.datasets == MAX_ACTIVE_QUARANTINES * MAX_DATASETS
    assert {scope.incident_id for scope in known.scopes} == {
        str(item.incident_id) for item in quarantines
    }


# La signature de la diffusion.


def independent(secret: str, timestamp: int, payload: bytes) -> str:
    """La signature telle que le format publié la décrit, sans aucune fonction d'AnswerLineage."""
    message = (
        VECTORS["label"].encode("ascii") + b"\x00" + str(timestamp).encode("ascii") + b"." + payload
    )
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def test_the_api_and_the_proxy_sign_and_read_the_same_header_label_and_secrets() -> None:
    assert api_signature.HEADER == proxy_signature.HEADER == VECTORS["header"]
    assert api_signature.SCHEME == proxy_signature.SCHEME == "v1"
    assert api_signature.LABEL == proxy_signature.LABEL == VECTORS["label"].encode("ascii")
    assert (api_signature.MIN_SECRET_LENGTH, api_signature.MAX_SECRET_LENGTH) == (
        proxy_signature.MIN_SECRET_LENGTH,
        proxy_signature.MAX_SECRET_LENGTH,
    )


@pytest.mark.parametrize(
    "value",
    [
        "x" * api_signature.MIN_SECRET_LENGTH,
        "x" * (api_signature.MIN_SECRET_LENGTH - 1),
        "x" * api_signature.MAX_SECRET_LENGTH,
        "x" * (api_signature.MAX_SECRET_LENGTH + 1),
        "!~" * 20,
        "un secret avec des espaces, assez long pour le reste",
        "un-secret-accentué-assez-long-pour-être-admis",
        "un-secret-avec-une-tabulation\t-assez-long",
    ],
)
def test_a_secret_the_api_admits_the_proxy_admits_and_no_other(value: str) -> None:
    try:
        api_signature.QuarantineSigning.create(value)
        api_admits = True
    except api_signature.QuarantineSecretError:
        api_admits = False
    try:
        proxy_signature.checked_secret(value)
        proxy_admits = True
    except ProxyConfigurationError:
        proxy_admits = False
    assert api_admits == proxy_admits


@pytest.mark.parametrize("case", VECTORS["signing"], ids=lambda case: case["name"])
def test_the_vectors_are_recomputed_alike_without_either_implementation(
    case: dict[str, Any],
) -> None:
    payload = case["payload"].encode("utf-8")
    found = re.fullmatch(r"t=([0-9]+)((?:,v1=[0-9a-f]{64})+)", case["header"])
    assert found is not None
    assert int(found.group(1)) == case["timestamp"]
    signatures = found.group(2).split(",v1=")[1:]
    expected = [
        independent(VECTORS["secrets"][name], case["timestamp"], payload)
        for name in case["secrets"]
    ]
    assert signatures == expected


@pytest.mark.parametrize("case", VECTORS["signing"], ids=lambda case: case["name"])
def test_the_api_signs_as_the_vectors_say_and_the_proxy_verifies_it(
    case: dict[str, Any],
) -> None:
    keys = [VECTORS["secrets"][name] for name in case["secrets"]]
    payload = case["payload"].encode("utf-8")
    signing = api_signature.QuarantineSigning.create(*keys, clock=lambda: case["timestamp"])
    header = signing.header(payload)
    assert header == case["header"]
    for key in keys:
        assert (
            proxy_signature.verify(header, payload, key, now=case["timestamp"])
            == (case["timestamp"])
        )


@pytest.mark.parametrize(
    "case",
    [case for case in VECTORS["signing"] if case["status"] == 200],
    ids=lambda case: case["name"],
)
def test_every_body_of_the_vectors_is_a_diffusion_of_the_api_that_the_proxy_reads(
    case: dict[str, Any],
) -> None:
    document = ActiveQuarantines.model_validate_json(case["payload"])
    assert document.version == version_of(document.quarantines)
    assert rendered(document.quarantines)[1] == case["payload"].encode("utf-8")
    assert proxy.parse(case["payload"].encode("utf-8")).version == document.version


def test_a_diffusion_signed_by_the_api_is_verified_by_the_proxy_200_and_304_alike() -> None:
    current, previous = (
        "courant-" + secrets.token_urlsafe(32),
        "precedent-" + secrets.token_urlsafe(32),
    )
    signing = api_signature.QuarantineSigning.create(current, previous, clock=lambda: NOW)
    document, body = rendered(at_bounds())
    version = document.version.encode("ascii")
    for payload in (body, version):
        header = signing.header(payload)
        # Pendant la rotation, un proxy qui connaît l'un ou l'autre secret accepte.
        for key in (current, previous):
            assert proxy_signature.verify(header, payload, key, now=NOW + 1) == NOW
        # Un autre secret, une autre charge, une heure hors de la tolérance : refusés.
        refusals = {
            proxy_signature.MISMATCH: (header, payload + b" ", current, NOW),
            proxy_signature.EXPIRED: (
                header,
                payload,
                current,
                NOW + proxy_signature.TOLERANCE + 1,
            ),
            proxy_signature.FROM_FUTURE: (
                header,
                payload,
                current,
                NOW - proxy_signature.TOLERANCE - 1,
            ),
        }
        for code, (value, charge, key, now) in refusals.items():
            with pytest.raises(proxy_signature.SignatureRefusedError) as refused:
                proxy_signature.verify(value, charge, key, now=now)
            assert refused.value.code == code
    # Ce qui est signé est aussi ce que le proxy lit.
    assert proxy.parse(body).version == document.version
