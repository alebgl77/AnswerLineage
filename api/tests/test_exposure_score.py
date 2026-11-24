"""Le poids d'une réponse dans le score d'exposition : sans base, sans horloge.

Une réponse pèse 1 plus un par destinataire distinct, et ce poids diminue de
moitié tous les 30 jours depuis l'heure de la réponse. Les poids se comptent
en billionièmes entiers, le score s'arrondit au millionième : la somme ne
dépend pas de l'ordre des réponses, et le même graphe à la même heure de
référence donne le même score.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from answerlineage_api.lineage.exposure import (
    HALF_LIFE,
    WEIGHT_SCALE,
    ExposureCursor,
    answer_weight,
    decay,
    score,
    units,
)
from answerlineage_api.lineage.paging import CursorError

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


def test_an_answer_of_the_reference_time_weighs_one_per_recipient_plus_one() -> None:
    assert timedelta(days=30) == HALF_LIFE
    assert decay(T0, T0) == WEIGHT_SCALE
    assert answer_weight(T0, T0, recipients=0) == WEIGHT_SCALE
    assert answer_weight(T0, T0, recipients=3) == 4 * WEIGHT_SCALE


def test_an_answer_of_exactly_thirty_days_weighs_half_and_of_sixty_days_a_quarter() -> None:
    today = answer_weight(T0, T0, recipients=2)
    assert answer_weight(T0, T0 + timedelta(days=30), recipients=2) * 2 == today
    assert answer_weight(T0, T0 + timedelta(days=60), recipients=2) * 4 == today
    # Exactement : la moitié de 3 est 1,5, sans arrondi.
    assert score(answer_weight(T0, T0 + timedelta(days=30), recipients=2)) == 1.5


def test_the_weight_decreases_with_age_and_never_increases() -> None:
    ages = [timedelta(seconds=seconds) for seconds in (0, 1, 3600, 86_400, 2_592_000, 31_536_000)]
    weights = [decay(T0, T0 + age) for age in ages]
    assert weights == sorted(weights, reverse=True)
    assert len(set(weights)) == len(weights)
    # Au-delà de quelques années, le poids d'une réponse ne compte plus au millionième.
    assert units(decay(T0, T0 + timedelta(days=3 * 365))) == 0


def test_an_answer_after_the_reference_time_weighs_as_an_answer_of_that_time() -> None:
    assert decay(T0 + timedelta(days=3), T0) == decay(T0, T0)
    assert decay(datetime(9999, 12, 31, tzinfo=UTC), datetime(1970, 1, 1, tzinfo=UTC)) == (
        WEIGHT_SCALE
    )
    assert decay(datetime(1970, 1, 1, tzinfo=UTC), datetime(9999, 12, 30, tzinfo=UTC)) == 0


def test_the_score_is_rounded_to_the_millionth_half_up() -> None:
    millionth = WEIGHT_SCALE // 1_000_000
    assert units(0) == 0
    assert units(millionth // 2 - 1) == 0
    assert units(millionth // 2) == 1
    assert score(3 * WEIGHT_SCALE + millionth // 2) == 3.000001
    assert score(1_954_321_987_654) == 1.954322


def test_the_sum_of_weights_does_not_depend_on_their_order() -> None:
    weights = [
        answer_weight(T0 - timedelta(days=days, minutes=7), T0, recipients=days % 4)
        for days in range(90)
    ]
    assert sum(weights) == sum(reversed(weights)) == sum(sorted(weights))


def test_a_cursor_names_the_reference_time_the_score_and_the_dataset() -> None:
    cursor = ExposureCursor(T0, 1_954_322, 42)
    assert ExposureCursor.decode(cursor.encode()) == cursor
    assert cursor.encode().endswith(".1954322.42")


@pytest.mark.parametrize(
    "value",
    [
        f"{'9' * 18}.1.1",  # au-delà de l'an 9999
        "1.1.1",  # avant 1970
    ],
)
def test_a_cursor_out_of_the_admitted_times_is_no_cursor(value: str) -> None:
    with pytest.raises(CursorError):
        ExposureCursor.decode(value)
