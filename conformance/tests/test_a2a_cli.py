"""La commande ``answerlineage-conformance a2a`` : ses codes de sortie et ses deux rapports."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from answerlineage_conformance import __version__, cli
from answerlineage_conformance.a2a import CONTROLS, URI
from conformance.tests.a2a_toys import Read, ToyCallee, ToyCaller, exchange

CALLEE_RUN = "0193a9c5-7a10-7c3e-9d5e-2f1b6c8a0c01"
TOY = ToyCallee(run_id=CALLEE_RUN, reads=(Read("bigquery", "finance.ref.taux_change"),))


class Files:
    """Ce qu'un intégrateur garde d'un appel entre deux agents : la carte et l'échange."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        card, captured = exchange(ToyCaller(), TOY)
        self.card = self.write("carte.json", card)
        self.exchange = self.write("echange.json", captured)

    def write(self, name: str, document: Any) -> Path:
        path = self.folder / name
        content = document if isinstance(document, str) else json.dumps(document, indent=2)
        path.write_text(content, "utf-8")
        return path

    def arguments(self, *more: str) -> list[str]:
        return ["a2a", "--card", str(self.card), "--exchange", str(self.exchange), *more]

    def summary(self) -> tuple[dict[str, Any], dict[str, Any]]:
        captured = json.loads(self.exchange.read_text("utf-8"))
        return captured, captured["response"]["result"]["task"]["metadata"][URI]


@pytest.fixture
def files(tmp_path: Path) -> Files:
    return Files(tmp_path)


def test_a_conformant_exchange_exits_with_zero_and_a_readable_report(
    files: Files, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(files.arguments()) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert out.startswith(f"Kit de conformité AnswerLineage {__version__}\n")
    assert f"Extension : {URI}\n" in out
    assert "Verdict : CONFORME\n" in out
    assert "Problèmes : aucun\n" in out
    assert f"Contrôles exécutés : {len(CONTROLS)}\n" in out
    for label, path in (("Agent Card", files.card), ("échange", files.exchange)):
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        assert f"  {label}  {path}  {len(data)} octets  sha256:{digest}\n" in out


def test_a_summary_too_long_exits_with_one_and_is_located(
    files: Files, capsys: pytest.CaptureFixture[str]
) -> None:
    captured, summary = files.summary()
    summary["datasets"] = [
        {"namespace": "bigquery", "name": f"t{index}", "precision": "table"} for index in range(101)
    ]
    summary["datasetCount"] = 101
    files.write("echange.json", captured)
    assert cli.main(files.arguments()) == 1
    out = capsys.readouterr().out
    assert "Verdict : NON CONFORME\n" in out
    key = URI.replace("/", "~1")
    assert (
        f"  [réponse] summary_schema_violation : {files.exchange}, "
        f"/response/result/task/metadata/{key}/datasets (maxItems)\n"
    ) in out
    assert "Ce que disent ces codes\n  summary_schema_violation : " in out


def test_the_json_report_is_stable_and_never_quotes_what_it_received(
    files: Files, capsys: pytest.CaptureFixture[str]
) -> None:
    captured, summary = files.summary()
    summary["datasets"][0]["name"] = TOY.answer
    summary[TOY.answer] = "<script>alert(1)</script>"
    files.write("echange.json", captured)
    assert cli.main(files.arguments("--format", "json")) == 1
    first = capsys.readouterr().out
    assert cli.main(files.arguments("--format", "json")) == 1
    assert capsys.readouterr().out == first

    report = json.loads(first)
    assert list(report) == [
        "report_version",
        "kit",
        "suite",
        "extension",
        "verdict",
        "files",
        "controls",
        "problem_count",
        "truncated",
        "problems",
    ]
    assert (report["suite"], report["extension"], report["verdict"]) == (
        "a2a",
        URI,
        "non_conformant",
    )
    assert [file["role"] for file in report["files"]] == ["card", "exchange"]
    assert [control["code"] for control in report["controls"]] == [c.code for c in CONTROLS]
    assert list(report["problems"][0]) == ["code", "group", "message", "file", "pointer", "reason"]
    assert {problem["code"] for problem in report["problems"]} == {
        "summary_schema_violation",
        "identifier_rejected",
        "content_in_extension",
    }
    assert TOY.answer not in first
    assert "script" not in first


@pytest.mark.parametrize(
    ("which", "content", "expected"),
    [
        ("card", "[]", "ce n'est pas une Agent Card"),
        ("card", "{", "JSON illisible"),
        ("exchange", '{"request": {}}', "ce n'est pas un échange capturé"),
        ("exchange", '{"request": {}, "response": []}', "ce n'est pas un échange capturé"),
        ("exchange", '{"request": {}, "request": {}, "response": {}}', "JSON ambigu"),
    ],
)
def test_an_unreadable_input_exits_with_two_and_no_verdict(
    files: Files, capsys: pytest.CaptureFixture[str], which: str, content: str, expected: str
) -> None:
    files.write("carte.json" if which == "card" else "echange.json", content)
    assert cli.main(files.arguments()) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("Entrée illisible : ")
    assert expected in err


def test_a_missing_file_exits_with_two(files: Files, capsys: pytest.CaptureFixture[str]) -> None:
    files.exchange.unlink()
    assert cli.main(files.arguments()) == 2
    assert "fichier introuvable" in capsys.readouterr().err


def test_both_files_are_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(["a2a", "--card", "carte.json"])
    assert stopped.value.code == 2
    assert "--exchange" in capsys.readouterr().err


def test_a_long_list_of_problems_says_how_many_are_listed(
    files: Files, capsys: pytest.CaptureFixture[str]
) -> None:
    captured, summary = files.summary()
    summary["datasets"] = [{"namespace": "b", "name": f"t {index}"} for index in range(1200)]
    files.write("echange.json", captured)
    assert cli.main(files.arguments()) == 1
    assert ", dont les 1000 premiers sont listés\n" in capsys.readouterr().out
