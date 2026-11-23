"""Contrat entre le proxy et l'API : les périmètres en quarantaine que l'une diffuse, l'autre
les lit, et la signature que l'une pose, l'autre la vérifie.

Le proxy ne dépend pas de l'API : les modes qu'il connaît, les bornes de la
réponse et la vérification de la signature y sont écrits une seconde fois. Ce
test les tient d'accord : une réponse que l'API rend à ses bornes, cent
quarantaines de vingt jeux, et leurs zones de cinq cents jeux dérivés en tout,
tient dans ce que le proxy accepte de lire, même aux noms les plus longs que
l'API admet, quand ils s'écrivent en ASCII ; une
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
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from starlette.responses import JSONResponse

from answerlineage_api import identifiers
from answerlineage_api.incidents import signature as api_signature
from answerlineage_api.incidents.models import (
    MAX_DATASETS,
    MAX_DIFFUSED_DERIVED,
    ActiveQuarantine,
    ActiveQuarantines,
    DerivedDataset,
    QuarantinedDataset,
)
from answerlineage_api.incidents.quarantine import version_of
from answerlineage_api.store.schema import (
    MAX_ACTIVE_QUARANTINES,
    MAX_ZONE_DEPTH,
    MAX_ZONE_DERIVED,
    QUARANTINE_MODES,
)
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
    assert proxy.MAX_DEPTH == MAX_ZONE_DEPTH
    assert proxy.MAX_DERIVED == MAX_ZONE_DERIVED
    assert proxy.MAX_DERIVED_TOTAL == MAX_DIFFUSED_DERIVED


def realistic(kind: str, rank: int, index: int) -> tuple[str, str]:
    """Des noms longs, accentués, comme en écrit un entrepôt."""
    return (
        "bigquery://projet-données-référence",
        f"entrepôt.{kind}_{rank:03d}.commandes_détaillées_{index:02d}_" + "x" * 160,
    )


def longest(kind: str, rank: int, index: int) -> tuple[str, str]:
    """Les noms les plus longs que l'API admet, en ASCII."""
    namespace = f"{kind}{rank:03d}{index:02d}"
    name = f"{kind}.{rank:03d}.{index:02d}."
    return (
        namespace.ljust(identifiers.NAMESPACE_MAX, "n"),
        name.ljust(identifiers.DATASET_NAME_MAX, "x"),
    )


def at_bounds(
    named: Callable[[str, int, int], tuple[str, str]] = realistic,
) -> list[ActiveQuarantine]:
    """Cent quarantaines de vingt jeux, chacune avec sa zone : les plus anciennes reçoivent
    chacune cinquante jeux dérivés, jusqu'à la borne de toutes ; les suivantes une zone vide,
    tronquée."""
    full = MAX_DIFFUSED_DERIVED // MAX_ZONE_DERIVED
    quarantines = []
    for rank in range(MAX_ACTIVE_QUARANTINES):
        derived = []
        if rank < full:
            for index in range(MAX_ZONE_DERIVED):
                namespace, name = named("vues", rank, index)
                derived.append(
                    DerivedDataset(namespace=namespace, name=name, depth=1 + index % MAX_ZONE_DEPTH)
                )
        quarantines.append(
            ActiveQuarantine(
                incident_id=UUID(int=rank),
                mode="warn",
                activated_at=datetime(2026, 11, 16, 8, 55, rank % 60, 204517, tzinfo=UTC),
                datasets=[
                    QuarantinedDataset(
                        namespace=named("ventes", rank, index)[0],
                        name=named("ventes", rank, index)[1],
                    )
                    for index in range(MAX_DATASETS)
                ],
                derived=derived,
                derived_truncated=rank >= full,
                zone_computed_at=datetime(2026, 11, 16, 9, 0, rank % 60, 125000, tzinfo=UTC),
            )
        )
    return quarantines


def rendered(quarantines: list[ActiveQuarantine]) -> tuple[ActiveQuarantines, bytes]:
    """La diffusion, et son corps tel que la route le rend."""
    document = ActiveQuarantines(version=version_of(quarantines), quarantines=quarantines)
    return document, bytes(JSONResponse(document.model_dump(mode="json")).body)


@pytest.mark.parametrize("named", [realistic, longest], ids=["realistes", "les-plus-longs"])
def test_a_response_of_the_api_at_its_bounds_is_read_whole_by_the_proxy(
    named: Callable[[str, int, int], tuple[str, str]],
) -> None:
    quarantines = at_bounds(named)
    document, body = rendered(quarantines)

    known = proxy.parse(body)
    assert len(body) <= proxy.MAX_RESPONSE_BYTES
    assert known.version == document.version
    assert len(known.scopes) == MAX_ACTIVE_QUARANTINES
    assert known.datasets == MAX_ACTIVE_QUARANTINES * MAX_DATASETS
    assert known.derived == MAX_DIFFUSED_DERIVED
    assert {scope.incident_id for scope in known.scopes} == {
        str(item.incident_id) for item in quarantines
    }
    # Chaque jeu dérivé, sa profondeur et l'heure de sa zone arrivent tels que l'API les rend.
    for scope, item in zip(known.scopes, quarantines, strict=True):
        assert item.derived is not None
        assert {(d.namespace, d.name, d.depth) for d in scope.derived} == {
            (d.namespace, d.name, d.depth) for d in item.derived
        }
        assert scope.derived_truncated is item.derived_truncated
        assert scope.zone_computed_at == item.zone_computed_at
        assert scope.activated_at == item.activated_at


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


def test_the_zone_is_signed_with_the_body_a_derived_dataset_changed_breaks_the_signature() -> None:
    """La zone n'a pas de signature à elle : elle est dans le corps, que la signature couvre
    octet pour octet. Le format publié des vecteurs ne change donc pas."""
    secret = "courant-" + secrets.token_urlsafe(32)
    signing = api_signature.QuarantineSigning.create(secret, clock=lambda: NOW)
    _, body = rendered(at_bounds())
    header = signing.header(body)
    assert b'"depth":2' in body and b'"derived_truncated":true' in body
    assert proxy_signature.verify(header, body, secret, now=NOW) == NOW
    for altered in (
        body.replace(b'"depth":2', b'"depth":1', 1),
        body.replace(b'"derived_truncated":true', b'"derived_truncated":false', 1),
        body.replace(b'"zone_computed_at":"2026', b'"zone_computed_at":"2027', 1),
    ):
        with pytest.raises(proxy_signature.SignatureRefusedError) as refused:
            proxy_signature.verify(header, altered, secret, now=NOW)
        assert refused.value.code == proxy_signature.MISMATCH
