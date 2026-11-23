"""La signature des webhooks et sa vérification de référence, jugées sur les vecteurs publiés.

Le récepteur refuse une signature fausse, un corps altéré, un message rejoué
hors de la tolérance ou venu du futur, et un en-tête illisible. Pendant une
rotation, deux secrets sont acceptés. Le module se copie : il n'importe que la
bibliothèque standard.
"""

from __future__ import annotations

import ast
import json
import secrets
import sys
import time
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from answerlineage_workers import webhook_signature
from answerlineage_workers.webhook_signature import (
    DEFAULT_TOLERANCE,
    HEADER,
    MALFORMED_HEADER,
    MAX_HEADER_LENGTH,
    OUTSIDE_TOLERANCE,
    SIGNATURE_MISMATCH,
    SignatureError,
    sign,
    signature_header,
    verify,
)

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "spec" / "webhooks" / "v1"
VECTORS: dict[str, Any] = json.loads((SPEC / "vectors.json").read_text(encoding="utf-8"))
MESSAGE_SCHEMA: dict[str, Any] = json.loads((SPEC / "Message.json").read_text(encoding="utf-8"))


def named(names: list[str]) -> list[str]:
    known: dict[str, str] = VECTORS["secrets"]
    return [known[name] for name in names]


def test_the_vectors_describe_the_header_and_the_tolerance_of_the_module() -> None:
    assert VECTORS["header"] == HEADER
    assert VECTORS["tolerance_seconds"] == DEFAULT_TOLERANCE
    assert VECTORS["algorithm"] == "HMAC-SHA-256"


@pytest.mark.parametrize("case", VECTORS["signing"], ids=lambda case: case["name"])
def test_the_sender_signs_as_the_vectors_say(case: dict[str, Any]) -> None:
    body = case["body"].encode()
    assert signature_header(named(case["secrets"]), case["timestamp"], body) == case["header"]
    # Ce qui est signé se vérifie, sous chacun des secrets.
    for secret in named(case["secrets"]):
        assert verify(case["header"], body, [secret], now=case["timestamp"]) == case["timestamp"]


@pytest.mark.parametrize("case", VECTORS["verification"], ids=lambda case: case["name"])
def test_the_receiver_decides_as_the_vectors_say(case: dict[str, Any]) -> None:
    body = case["body"].encode()
    secrets_known = named(case["secrets"])
    if case["valid"]:
        assert verify(case["header"], body, secrets_known, now=case["now"]) > 0
    else:
        with pytest.raises(SignatureError) as refused:
            verify(case["header"], body, secrets_known, now=case["now"])
        assert refused.value.code == case["error"]


@pytest.mark.parametrize("case", VECTORS["signing"], ids=lambda case: case["name"])
def test_every_body_of_the_vectors_follows_the_published_format(case: dict[str, Any]) -> None:
    jsonschema.Draft202012Validator(MESSAGE_SCHEMA).validate(json.loads(case["body"]))


def test_the_vectors_refuse_a_replay_a_false_signature_and_an_altered_body() -> None:
    refused = {case["error"] for case in VECTORS["verification"] if not case["valid"]}
    assert refused == {MALFORMED_HEADER, SIGNATURE_MISMATCH, OUTSIDE_TOLERANCE}
    names = " ".join(case["name"] for case in VECTORS["verification"])
    for situation in ("rejouée hors de la tolérance", "signature fausse", "corps altéré"):
        assert situation in names


def message_signed_now(secret: str) -> tuple[bytes, str, int]:
    body = json.dumps({"version": 1, "type": "incident.declared"}).encode()
    timestamp = 1_800_000_000
    return body, signature_header([secret], timestamp, body), timestamp


def test_a_signature_replayed_outside_the_window_is_refused() -> None:
    secret = secrets.token_urlsafe(32)
    body, header, timestamp = message_signed_now(secret)
    assert verify(header, body, [secret], now=timestamp + DEFAULT_TOLERANCE) == timestamp
    with pytest.raises(SignatureError) as refused:
        verify(header, body, [secret], now=timestamp + DEFAULT_TOLERANCE + 1)
    assert refused.value.code == OUTSIDE_TOLERANCE
    # La fenêtre se règle chez le récepteur.
    with pytest.raises(SignatureError):
        verify(header, body, [secret], tolerance=30, now=timestamp + 31)
    assert verify(header, body, [secret], tolerance=3600, now=timestamp + 3000) == timestamp


def test_a_false_signature_and_an_altered_body_are_refused() -> None:
    secret = secrets.token_urlsafe(32)
    body, header, timestamp = message_signed_now(secret)
    for forged_header, forged_body in (
        (header, body.replace(b"declared", b"analysed")),
        (header, body + b" "),
        (signature_header([secrets.token_urlsafe(32)], timestamp, body), body),
        (f"t={timestamp},v1={'0' * 64}", body),
    ):
        with pytest.raises(SignatureError) as refused:
            verify(forged_header, forged_body, [secret], now=timestamp)
        assert refused.value.code == SIGNATURE_MISMATCH


def test_two_secrets_are_accepted_during_a_rotation() -> None:
    old, new = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    body = b'{"version":1}'
    both = signature_header([new, old], 1_800_000_000, body)
    # Un récepteur pas encore passé au nouveau secret, et un récepteur déjà passé.
    assert verify(both, body, [old], now=1_800_000_000)
    assert verify(both, body, [new], now=1_800_000_000)
    # Un récepteur qui accepte les deux, devant un émetteur passé au nouveau seul.
    assert verify(signature_header([new], 1_800_000_000, body), body, [old, new], now=1.8e9)


@pytest.mark.parametrize(
    "header",
    [
        None,
        "t=1800000000," + ",".join(["v1=" + "a" * 64] * 9),
        "t=1800000000,v1=" + "a" * 64 + ",x=" + "y" * MAX_HEADER_LENGTH,
        "t=1800000000,V1=" + "a" * 64,
        "t=-1,v1=" + "a" * 64,
        "t=1800000000,=" + "a" * 64,
        "t=1800000000;v1=" + "a" * 64,
    ],
)
def test_an_unreadable_header_is_refused_before_any_signature_is_computed(
    header: str | None,
) -> None:
    with pytest.raises(SignatureError) as refused:
        verify(header, b"{}", ["un-secret-de-test-assez-long-pour-etre-admis"], now=1.8e9)
    assert refused.value.code == MALFORMED_HEADER


def test_a_signature_is_the_hmac_of_the_timestamp_a_dot_and_the_body() -> None:
    assert sign(b"k", 1, b"x") == sign("k", 1, b"x")
    assert sign("k", 1, b"x") != sign("k", 1, b"y") != sign("k", 2, b"x")
    assert len(sign("k", 1, b"x")) == 64


def test_the_signing_and_the_verification_refuse_a_misuse() -> None:
    with pytest.raises(ValueError, match="secret"):
        verify("t=1,v1=" + "a" * 64, b"", [])
    with pytest.raises(ValueError, match="tolérance"):
        verify("t=1,v1=" + "a" * 64, b"", ["s"], tolerance=-1)
    with pytest.raises(ValueError, match="secrets"):
        signature_header([], 1, b"")
    with pytest.raises(ValueError, match="secrets"):
        signature_header(["s"] * 9, 1, b"")


def test_the_receiver_clock_is_the_default() -> None:
    secret = secrets.token_urlsafe(32)
    body = b"{}"
    now = int(time.time())
    assert verify(signature_header([secret], now, body), body, [secret]) == now
    with pytest.raises(SignatureError):
        verify(signature_header([secret], now - 3600, body), body, [secret])


def test_the_module_can_be_copied_it_imports_only_the_standard_library() -> None:
    tree = ast.parse(Path(webhook_signature.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "aucun import relatif"
            imported.add((node.module or "").partition(".")[0])
    assert imported
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}, imported
