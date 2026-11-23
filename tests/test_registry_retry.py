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
