"""La spécification de l'extension A2A reste vraie quand la suite change.

Ses exemples passent la suite, chaque contrôle est décrit dans la section de
son groupe, et le rapport qu'elle montre a la forme d'un vrai.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from answerlineage_conformance.a2a import CONTROLS, GROUPS, URI, judge
from answerlineage_conformance.a2a_report import A2aReport, render_json
from answerlineage_conformance.sources import source
from conformance.tests.a2a_toys import Read, ToyCallee, ToyCaller, exchange

ROOT = Path(__file__).resolve().parents[2]
DOCUMENT = (ROOT / "docs" / "a2a.md").read_text(encoding="utf-8")
TITLES = {
    "version": "Version",
    "declaration": "Déclaration",
    "call": "Appel",
    "response": "Réponse",
    "content": "Contenu",
}


def example(marker: str) -> Any:
    """Le premier exemple JSON qui suit un repère du document."""
    block = re.search(r"```json\n(.*?)```", DOCUMENT[DOCUMENT.index(marker) :], re.DOTALL)
    assert block is not None
    return json.loads(block[1])


def section(title: str) -> str:
    start = DOCUMENT.index(f"#### {title}\n")
    following = re.search(r"^#", DOCUMENT[start + 5 :], re.MULTILINE)
    assert following is not None
    return DOCUMENT[start : start + 5 + following.start()]


def card() -> Any:
    return example("## Déclaration dans l'Agent Card")


def captured() -> Any:
    return example("### L'échange capturé")


def test_every_control_is_documented_in_the_section_of_its_group() -> None:
    assert set(TITLES) == set(GROUPS)
    for group, title in TITLES.items():
        documented = re.findall(r"^\| `([a-z_]+)` \|", section(title), re.MULTILINE)
        assert documented == [control.code for control in CONTROLS if control.group == group]


def test_the_documented_card_and_exchange_pass_the_suite() -> None:
    judgement = judge(card(), captured())
    assert judgement.conformant, judgement.problems


def test_the_documented_exchange_is_what_the_toys_exchange() -> None:
    toy = ToyCallee(
        run_id="0193a9c5-7a10-7c3e-9d5e-2f1b6c8a0d01",
        reads=(Read("bigquery", "finance.ref.taux_change", "2026-11-01", "columns"),),
    )
    assert exchange(ToyCaller(), toy)[1] == captured()


def test_the_documented_request_and_summary_pass_the_suite() -> None:
    documented = captured()
    documented["request"]["params"] = example("## Ce que l'appelant envoie")
    documented["response"]["result"]["task"]["metadata"][URI] = example(
        "## Ce que l'appelé renvoie"
    )
    assert judge(card(), documented).conformant


def test_the_documented_parent_facet_attaches_the_callee_to_the_call() -> None:
    parent = example("Rattacher, c'est porter")["parent"]
    context = captured()["request"]["params"]["message"]["metadata"][URI]
    declared = card()["capabilities"]["extensions"][0]["params"]["callJob"]
    assert parent["run"] == {"runId": context["runId"]}
    assert parent["job"] == declared


def test_the_documented_json_report_has_the_shape_of_a_real_one() -> None:
    documented = example("`--format json` rend")
    broken = copy.deepcopy(captured())
    summary = broken["response"]["result"]["task"]["metadata"][URI]
    summary["datasets"] = summary["datasets"] * 101
    judgement = judge(card(), broken)
    files = (source("card", "carte.json", b"{}"), source("exchange", "echange.json", b"{}"))
    real = json.loads(render_json(A2aReport(judgement, files)))
    assert list(documented) == list(real)
    for part in ("files", "controls", "problems"):
        assert list(documented[part][0]) == list(real[part][0])
    assert documented["extension"] == real["extension"] == URI
    after = DOCUMENT[DOCUMENT.index("`--format json` rend") :]
    fields = re.findall(r"^\| `([a-z_]+)` \|", after, re.MULTILINE)
    assert set(fields) >= set(real)


def test_the_documented_text_report_counts_the_controls() -> None:
    assert f"Contrôles exécutés : {len(CONTROLS)}\n" in DOCUMENT
