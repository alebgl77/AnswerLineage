"""La documentation du kit reste vraie quand le code change."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, get_args

from answerlineage_conformance.canaries import Form, parse_canaries
from answerlineage_conformance.controls import CONTROLS, GROUPS
from answerlineage_conformance.report import Report, render_json
from conformance.tests.captures import RECEIVED_AT, evidence, judged, mutated, request

ROOT = Path(__file__).resolve().parents[2]
DOCUMENT = (ROOT / "docs" / "conformite.md").read_text(encoding="utf-8")
TITLES = {"shape": "Forme", "context": "Contexte", "content": "Contenu", "projection": "Projection"}
PROFILES = {
    ("adapter",): "`adapter`",
    ("capture",): "`capture`",
    ("adapter", "capture"): "les deux",
}


def section(title: str) -> str:
    """Le texte d'une section, de son titre au titre suivant."""
    start = DOCUMENT.index(f"### {title}\n")
    following = re.search(r"^##", DOCUMENT[start + 4 :], re.MULTILINE)
    assert following is not None
    return DOCUMENT[start : start + 4 + following.start()]


def rows(text: str) -> list[tuple[str, str]]:
    """Les lignes des tableaux dont la première cellule est un code : le code, puis la suivante."""
    return re.findall(r"^\| `([a-z_0-9]+)` \| ([^|]+) \|", text, re.MULTILINE)


def example(marker: str) -> Any:
    """Le premier exemple JSON qui suit un repère du document."""
    block = re.search(r"```json\n(.*?)```", DOCUMENT[DOCUMENT.index(marker) :], re.DOTALL)
    assert block is not None
    return json.loads(block[1])


def test_every_control_is_documented_in_the_section_of_its_group() -> None:
    assert set(TITLES) == set(GROUPS)
    for group, title in TITLES.items():
        expected = [control.code for control in CONTROLS if control.group == group]
        documented = [code for code, _ in rows(section(title))]
        assert documented[: len(expected)] == expected, title


def test_the_profiles_of_a_context_control_are_the_documented_ones() -> None:
    documented = dict(rows(section("Contexte")))
    assert documented == {
        control.code: PROFILES[control.profiles]
        for control in CONTROLS
        if control.group == "context"
    }


def test_every_form_under_which_a_canary_is_searched_is_documented() -> None:
    documented = [code for code, _ in rows(section("Contenu"))]
    assert documented[-len(get_args(Form)) :] == list(get_args(Form))


def test_the_documented_canaries_are_canaries_the_kit_accepts() -> None:
    assert len(parse_canaries(example("**1. Tirer les canaris.**"))) == 3


def test_the_documented_json_report_has_the_shape_of_a_real_one() -> None:
    documented = example("### Rapport JSON")
    proof = evidence(logs="DEBUG\n", metrics="appels_total 1\n")
    judgement = judged(mutated(request(), 1, ["run", "facets", "parent"]), evidence=proof)
    files = (proof.witness, *proof.logs, *proof.metrics)
    real = json.loads(render_json(Report(judgement, RECEIVED_AT, files)))

    assert list(documented) == list(real)
    for part in ("kit", "capture"):
        assert list(documented[part]) == list(real[part])
    for part in ("files", "controls", "problems"):
        assert list(documented[part][0]) == list(real[part][0])
    assert {field for field, _ in rows(DOCUMENT[DOCUMENT.index("### Rapport JSON") :])} >= set(real)


def test_the_documented_text_report_counts_the_controls_of_a_full_capture() -> None:
    proof = evidence(logs="DEBUG\n", metrics="appels_total 1\n")
    executed = len(judged(request(), evidence=proof).controls)
    assert f"Contrôles exécutés : {executed}\n" in DOCUMENT
