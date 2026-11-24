"""Empreintes à clé : HMAC-SHA-256, une étiquette par usage, une forme canonique des arguments."""

from __future__ import annotations

import base64
import itertools
import json
import pickle
import random
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from answerlineage_proxy.fingerprint import (
    ARGUMENTS_LABEL,
    RESULT_LABEL,
    FingerprintKey,
    arguments_fingerprint,
    canonical_json,
)

VECTORS: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[2] / "spec" / "fingerprint" / "vectors-v1.json").read_text(
        encoding="utf-8"
    )
)
KEYS = {name: FingerprintKey.from_hex(value) for name, value in VECTORS["keys"].items()}
KEY = KEYS["locataireA"]
FORM = r"hmac-sha256:[0-9a-f]{64}"


def test_the_published_labels_are_those_of_the_code() -> None:
    assert VECTORS["labels"] == {
        "arguments": ARGUMENTS_LABEL.decode("ascii"),
        "result": RESULT_LABEL.decode("ascii"),
    }


@pytest.mark.parametrize("case", VECTORS["arguments"], ids=lambda case: case["name"])
def test_arguments_fingerprints_match_the_published_vectors(case: dict[str, Any]) -> None:
    arguments = {} if case["received"] is None else json.loads(case["received"])
    assert canonical_json(arguments) == case["canonical"].encode("utf-8")
    for name, key in KEYS.items():
        assert arguments_fingerprint(key, arguments) == case["fingerprints"][name]


@pytest.mark.parametrize("case", VECTORS["results"], ids=lambda case: case["name"])
def test_result_fingerprints_match_the_published_vectors(case: dict[str, Any]) -> None:
    body = base64.b64decode(case["base64"])
    assert len(body) == case["size"]
    for name, key in KEYS.items():
        assert key.fingerprint(RESULT_LABEL, body) == case["fingerprints"][name]


def test_the_fingerprint_carries_its_algorithm() -> None:
    assert re.fullmatch(FORM, KEY.fingerprint(RESULT_LABEL, b"Dupont"))


def test_two_tenants_get_two_fingerprints_for_the_same_value() -> None:
    """Critère d'acceptation : deux locataires, la même valeur, deux empreintes."""
    value = {"client": "Jeanne Dupont"}
    first = arguments_fingerprint(KEYS["locataireA"], value)
    second = arguments_fingerprint(KEYS["locataireB"], value)
    assert first is not None
    assert second is not None
    assert first != second


def test_identical_arguments_and_result_have_different_fingerprints() -> None:
    """Une étiquette par usage : les mêmes octets ne se rapprochent pas d'un usage à l'autre."""
    arguments = arguments_fingerprint(KEY, "Dupont")
    assert canonical_json("Dupont") == b'"Dupont"'
    assert arguments != KEY.fingerprint(RESULT_LABEL, b'"Dupont"')
    assert arguments == KEY.fingerprint(ARGUMENTS_LABEL, b'"Dupont"')


def test_same_key_and_same_value_give_the_same_fingerprint_in_another_process() -> None:
    script = (
        "import sys\n"
        "from answerlineage_proxy.fingerprint import FingerprintKey, arguments_fingerprint\n"
        "key = FingerprintKey.from_hex(sys.stdin.readline())\n"
        "print(arguments_fingerprint(key, {'client': 'Jeanne Dupont', 'n': [1, 2.0]}))\n"
    )
    other = subprocess.run(  # noqa: S603 — l'interpréteur des tests, un script fixe.
        [sys.executable, "-I", "-c", script],
        input=VECTORS["keys"]["locataireA"] + "\n",
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    here = arguments_fingerprint(KEY, {"n": [1, 2], "client": "Jeanne Dupont"})
    assert other.stdout.strip() == here


JSON_VALUES = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4)
    ),
    max_leaves=20,
)


def shuffled(value: object, seed: random.Random) -> object:
    """La même valeur, clés dans un autre ordre."""
    if isinstance(value, dict):
        items = list(value.items())
        seed.shuffle(items)
        return {key: shuffled(item, seed) for key, item in items}
    if isinstance(value, list):
        return [shuffled(item, seed) for item in value]
    return value


@settings(max_examples=200, deadline=None)
@given(value=JSON_VALUES, seed=st.integers(), indent=st.sampled_from([None, 0, 2, "\t"]))
def test_the_canonical_form_ignores_key_order_and_whitespace(
    value: object, seed: int, indent: int | str | None
) -> None:
    """Deux écritures JSON de la même valeur donnent la même empreinte."""
    order = random.Random(seed)  # noqa: S311 — un ordre de clés, pas un secret.
    written = json.dumps(shuffled(value, order), indent=indent, ensure_ascii=bool(seed % 2))
    canonical = canonical_json(value)
    assert canonical is not None
    assert canonical_json(json.loads(written)) == canonical
    # La forme canonique est du JSON qui redonne la valeur, nombres en double précision.
    assert json.loads(canonical, parse_int=float) == json.loads(json.dumps(value), parse_int=float)


@settings(max_examples=100, deadline=None)
@given(body=st.binary(max_size=4096), cuts=st.lists(st.integers(min_value=0, max_value=4096)))
def test_a_result_fingerprinted_piece_by_piece_equals_the_whole(
    body: bytes, cuts: list[int]
) -> None:
    digest = KEY.digest(RESULT_LABEL)
    bounds = sorted({0, len(body), *(cut for cut in cuts if cut <= len(body))})
    for start, end in itertools.pairwise(bounds):
        digest.update(body[start:end])
    assert digest.fingerprint() == KEY.fingerprint(RESULT_LABEL, body)


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        {"a": [float("-inf")]},
        "\ud800",
        {"\udc00": 1},
        10**400,
        object(),
    ],
)
def test_a_value_without_canonical_form_has_no_fingerprint(value: object) -> None:
    """Pas d'empreinte qu'un autre émetteur ne retrouverait pas."""
    assert canonical_json(value) is None
    assert arguments_fingerprint(KEY, value) is None


def test_too_deep_a_value_has_no_fingerprint() -> None:
    value: object = 1
    for _ in range(100_000):
        value = [value]
    assert arguments_fingerprint(KEY, value) is None


CANARY = "ca11ab1e"


@pytest.mark.parametrize(
    "text",
    [
        CANARY * 7,  # 28 octets : trop courte
        CANARY * 17,  # 68 octets : trop longue
        CANARY * 8 + "zz",  # pas de l'hexadécimal
        CANARY * 7 + "ca11ab1",  # nombre impair de chiffres
        "",
    ],
)
def test_an_invalid_key_is_refused_without_being_quoted(text: str) -> None:
    with pytest.raises(ValueError, match="clé d'empreinte") as refused:
        FingerprintKey.from_hex(text)
    assert CANARY not in str(refused.value)
    assert refused.value.__cause__ is None
    assert refused.value.__context__ is None or CANARY not in str(refused.value.__context__)


def test_a_key_is_32_to_64_bytes_long() -> None:
    assert FingerprintKey(bytes(32)) == FingerprintKey.from_hex("00" * 32)
    assert FingerprintKey(bytes(64)) != FingerprintKey(bytes(32))
    for size in (31, 65):
        with pytest.raises(ValueError, match="32 à 64 octets"):
            FingerprintKey(bytes(size))


def test_a_key_never_shows_itself() -> None:
    key = FingerprintKey.from_hex(CANARY * 8)
    for shown in (repr(key), str(key), f"{key}", repr([key]), repr({"cle": key})):
        assert CANARY not in shown
        assert "secrète" in shown
    assert hash(key) == hash(FingerprintKey(bytes(32)))
    with pytest.raises(TypeError):
        pickle.dumps(key)
    assert key != CANARY * 8
