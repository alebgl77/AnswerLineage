"""Le serveur PostgreSQL des tests démarre malgré un refus passager du registre public."""

from __future__ import annotations

from collections.abc import Callable

import pytest

import conftest


class RateLimitedError(Exception):
    """Comme l'erreur que rend Docker quand le registre limite le débit."""


def starter(failures: list[Exception]) -> tuple[list[int], Callable[[], None]]:
    calls: list[int] = []

    def start() -> None:
        calls.append(1)
        if failures:
            raise failures.pop(0)

    return calls, start


def test_a_refusal_by_the_registry_is_retried_after_a_pause() -> None:
    calls, start = starter(
        [
            RateLimitedError('500 Server Error: ("toomanyrequests: Data limit exceeded")'),
            RateLimitedError("429 Too Many Requests"),
        ]
    )
    pauses: list[float] = []
    conftest.start_despite_rate_limits(start, sleep=pauses.append)
    assert len(calls) == 3
    assert pauses == list(conftest.REGISTRY_RETRY_DELAYS)


def test_a_lasting_refusal_ends_with_the_last_error() -> None:
    errors = [RateLimitedError(f"toomanyrequests {n}") for n in range(3)]
    _, start = starter(list(errors))
    with pytest.raises(RateLimitedError) as raised:
        conftest.start_despite_rate_limits(start, sleep=lambda _: None)
    assert raised.value is errors[-1]


def test_any_other_error_is_not_retried() -> None:
    calls, start = starter([RuntimeError("image introuvable")])
    with pytest.raises(RuntimeError):
        conftest.start_despite_rate_limits(start, sleep=lambda _: None)
    assert len(calls) == 1


DIGEST = "sha256:" + "ab" * 32


def test_the_same_digest_is_offered_by_a_second_public_registry() -> None:
    image = f"public.ecr.aws/docker/library/postgres:18.6@{DIGEST}"
    assert conftest.registry_mirrors(image) == [image, f"docker.io/library/postgres:18.6@{DIGEST}"]


@pytest.mark.parametrize(
    "image",
    [
        "public.ecr.aws/docker/library/postgres:18.6",
        f"ghcr.io/exemple/postgres@{DIGEST}",
        "postgres:18",
    ],
)
def test_an_image_without_digest_or_from_elsewhere_has_no_mirror(image: str) -> None:
    """Une étiquette peut désigner d'autres octets d'un registre à l'autre : seule l'empreinte."""
    assert conftest.registry_mirrors(image) == [image]


def test_a_registry_that_refuses_hands_over_to_the_mirror_without_a_pause() -> None:
    tried: list[str] = []

    def start(image: str) -> str:
        tried.append(image)
        if image == "premier":
            raise RateLimitedError("toomanyrequests: Data limit exceeded")
        return image

    pauses: list[float] = []
    assert conftest.start_from_mirrors(["premier", "second"], start, sleep=pauses.append) == (
        "second"
    )
    assert (tried, pauses) == (["premier", "second"], [])


def test_when_every_registry_refuses_all_are_tried_again_after_a_pause() -> None:
    tried: list[str] = []

    def start(image: str) -> str:
        tried.append(image)
        if len(tried) <= 2:
            raise RateLimitedError("429 Too Many Requests")
        return image

    pauses: list[float] = []
    assert conftest.start_from_mirrors(["premier", "second"], start, sleep=pauses.append) == (
        "premier"
    )
    assert (tried, pauses) == (
        ["premier", "second", "premier"],
        [conftest.REGISTRY_RETRY_DELAYS[0]],
    )


def test_another_error_from_the_first_registry_is_not_hidden_by_the_mirror() -> None:
    def start(image: str) -> str:
        raise RuntimeError(f"image introuvable : {image}")

    with pytest.raises(RuntimeError, match="premier"):
        conftest.start_from_mirrors(["premier", "second"], start, sleep=lambda _: None)
