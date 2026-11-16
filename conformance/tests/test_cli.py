"""La commande ``answerlineage-conformance`` : ses codes de sortie et ses deux rapports."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from answerlineage_conformance import __version__, cli
from answerlineage_conformance.canaries import parse_canaries
from answerlineage_conformance.controls import CONTROLS
from conformance.tests.captures import (
    ANSWER,
    CANARIES,
    PROMPT,
    TOOL_RESULT,
    captured_call,
    mutated,
    request,
    witness,
)

REFERENCE = "2026-11-03T00:00:00Z"


class Capture:
    """Les fichiers qu'un intégrateur garde d'une demande jouée avec les canaris."""

    def __init__(self, folder: Path, events: Any = None) -> None:
        self.folder = folder
        self.events = self.write("evenements.json", json.dumps(events or request()))
        document = {canary.role: canary.value for canary in CANARIES}
        self.canaries = self.write("canaris.json", json.dumps(document))
        self.witness = self.write("echange.txt", witness().data)
        self.logs = self.write("agent.log", "INFO appel relayé\nINFO réponse diffusée\n")
        self.metrics = self.write("metriques.txt", "appels_total 1\n")

    def write(self, name: str, content: str | bytes) -> Path:
        path = self.folder / name
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        return path

    def arguments(self, *more: str) -> list[str]:
        return [
            "check",
            str(self.events),
            "--canaries",
            str(self.canaries),
            "--witness",
            str(self.witness),
            "--logs",
            str(self.logs),
            "--metrics",
            str(self.metrics),
            "--received-at",
            REFERENCE,
            *more,
        ]


@pytest.fixture
def capture(tmp_path: Path) -> Capture:
    return Capture(tmp_path)


def test_canaries_writes_three_canaries_that_check_reads_back(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["canaries"]) == 0
    written = json.loads(capsys.readouterr().out)
    assert list(written) == ["prompt", "tool_result", "answer"]
    assert [canary.value for canary in parse_canaries(written)] == list(written.values())


def test_a_conformant_capture_exits_with_zero_and_a_readable_report(
    capture: Capture, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(capture.arguments()) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert out.startswith(f"Kit de conformité AnswerLineage {__version__}\n")
    assert "Verdict : CONFORME\n" in out
    assert "Profil : adapter\n" in out
    assert f"Heure de référence : {REFERENCE}\n" in out
    assert "Capture : 3 événements, 2 runs\n" in out
    assert "Problèmes : aucun\n" in out
    # Chaque fichier fouillé, avec sa taille et son empreinte : le rapport sert de preuve.
    for path in (capture.events, capture.canaries, capture.witness, capture.logs, capture.metrics):
        data = path.read_bytes()
        assert f"{path}  {len(data)} octets  sha256:{hashlib.sha256(data).hexdigest()}" in out


def test_a_tool_call_without_parent_exits_with_one_and_names_the_missing_field(
    capture: Capture, capsys: pytest.CaptureFixture[str]
) -> None:
    """Un émetteur qui oublie la facette parent : le kit échoue en nommant le champ manquant."""
    capture.write("evenements.json", json.dumps(mutated(request(), 1, ["run", "facets", "parent"])))
    assert cli.main(capture.arguments()) == 1
    out = capsys.readouterr().out
    assert "Verdict : NON CONFORME\n" in out
    assert "[contexte] parent_missing : événement 1, /run/facets/parent\n" in out


def test_a_leak_exits_with_one_and_is_located_without_being_quoted(
    capture: Capture, capsys: pytest.CaptureFixture[str]
) -> None:
    capture.write("agent.log", f"INFO début\nDEBUG résultat reçu : {TOOL_RESULT}\n")
    assert cli.main(capture.arguments()) == 1
    out = capsys.readouterr().out
    assert f"[contenu] canary_in_logs : {capture.logs}, ligne 2" in out
    assert "canari du résultat d'outil, tel quel" in out
    for canary in (PROMPT, TOOL_RESULT, ANSWER):
        assert canary not in out


def test_the_json_report_has_a_stable_shape(
    capture: Capture, capsys: pytest.CaptureFixture[str]
) -> None:
    capture.write("evenements.json", json.dumps(mutated(request(), 1, ["run", "facets", "parent"])))
    capture.write("metriques.txt", f"reponses_total {ANSWER.encode().hex()}\n")
    assert cli.main(capture.arguments("--format", "json")) == 1
    report = json.loads(capsys.readouterr().out)

    assert list(report) == [
        "report_version",
        "kit",
        "profile",
        "verdict",
        "received_at",
        "capture",
        "files",
        "controls",
        "problem_count",
        "truncated",
        "problems",
    ]
    assert report["report_version"] == 1
    assert report["kit"] == {"name": "answerlineage-conformance", "version": __version__}
    assert (report["profile"], report["verdict"]) == ("adapter", "non_conformant")
    assert report["received_at"] == REFERENCE
    assert report["capture"] == {"events": 3, "runs": 2}

    assert [(file["role"], file["path"]) for file in report["files"]] == [
        ("events", str(capture.events)),
        ("canaries", str(capture.canaries)),
        ("witness", str(capture.witness)),
        ("logs", str(capture.logs)),
        ("metrics", str(capture.metrics)),
    ]
    for file in report["files"]:
        data = Path(file["path"]).read_bytes()
        assert (file["bytes"], file["sha256"]) == (len(data), hashlib.sha256(data).hexdigest())

    # Les contrôles exécutés : ceux du profil, avec le nombre de problèmes de chacun.
    expected = [control for control in CONTROLS if "adapter" in control.profiles]
    assert [(control["code"], control["group"]) for control in report["controls"]] == [
        (control.code, control.group) for control in expected
    ]
    counted = {control["code"]: control["problems"] for control in report["controls"]}
    assert {code for code, count in counted.items() if count} == {
        "parent_missing",
        "no_complete_chain",
        "canary_in_metrics",
    }

    assert (report["problem_count"], report["truncated"]) == (3, False)
    nowhere = {"event": None, "pointer": None, "file": None, "line": None}
    nothing = {"reason": None, "canary": None, "form": None}
    messages = {control.code: control.message for control in CONTROLS}
    assert report["problems"] == [
        {
            "code": "parent_missing",
            "group": "context",
            "message": messages["parent_missing"],
            **nowhere,
            "event": 1,
            "pointer": "/run/facets/parent",
            **nothing,
        },
        {
            "code": "no_complete_chain",
            "group": "context",
            "message": messages["no_complete_chain"],
            **nowhere,
            **nothing,
        },
        {
            "code": "canary_in_metrics",
            "group": "content",
            "message": messages["canary_in_metrics"],
            **nowhere,
            "file": str(capture.metrics),
            "line": 1,
            **nothing,
            "canary": "answer",
            "form": "hex",
        },
    ]
    assert list(report["problems"][0]) == [
        "code",
        "group",
        "message",
        "event",
        "pointer",
        "file",
        "line",
        "reason",
        "canary",
        "form",
    ]


@pytest.mark.parametrize("layout", ["text", "json"])
def test_the_same_files_give_the_same_report_to_the_byte(
    capture: Capture, capsys: pytest.CaptureFixture[str], layout: str
) -> None:
    capture.write("agent.log", f"DEBUG {PROMPT}\n")
    reports = []
    for _ in range(2):
        assert cli.main(capture.arguments("--format", layout)) == 1
        reports.append(capsys.readouterr().out)
    assert reports[0] == reports[1]
    assert reports[0].endswith("\n")


def test_the_report_is_written_in_utf_8_whatever_the_terminal(
    capture: Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sur un terminal qui n'écrit pas l'UTF-8, le rapport garde les mêmes octets."""
    terminal = io.TextIOWrapper(io.BytesIO(), encoding="ascii", newline="\r\n")
    monkeypatch.setattr(sys, "stdout", terminal)
    assert cli.main(capture.arguments()) == 0
    written = terminal.buffer.getvalue()
    assert written.decode("utf-8").startswith("Kit de conformité AnswerLineage")
    assert b"\r" not in written

    replaced = io.StringIO()
    monkeypatch.setattr(sys, "stdout", replaced)
    assert cli.main(capture.arguments()) == 0
    assert replaced.getvalue().encode("utf-8") == written


def test_without_a_reference_time_the_current_time_is_used_and_written(
    capture: Capture, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = [argument for argument in capture.arguments() if argument != "--received-at"]
    arguments.remove(REFERENCE)

    monkeypatch.setattr(cli, "_now", lambda: datetime(2026, 11, 20, 8, 30, tzinfo=UTC))
    assert cli.main(arguments) == 0
    assert "Heure de référence : 2026-11-20T08:30:00Z\n" in capsys.readouterr().out

    # L'ingestion refuse un événement daté de plus d'un jour après sa réception.
    monkeypatch.setattr(cli, "_now", lambda: datetime(2026, 10, 1, tzinfo=UTC))
    assert cli.main(arguments) == 1
    assert "ingestion_rejected : événement 0, /eventTime" in capsys.readouterr().out


def test_the_capture_profile_judges_a_capture_point(
    capture: Capture, capsys: pytest.CaptureFixture[str]
) -> None:
    capture.write("evenements.json", json.dumps([captured_call()]))
    assert cli.main(capture.arguments("--profile", "capture")) == 0
    assert "Profil : capture\n" in capsys.readouterr().out
    # La même capture ne porte pas la chaîne complète qu'un adaptateur doit.
    assert cli.main(capture.arguments()) == 1


def test_events_are_read_one_per_line_as_well(
    capture: Capture, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = "\n".join(json.dumps(event) for event in request())
    capture.write("evenements.json", f"\n{lines}\n\n")
    assert cli.main(capture.arguments()) == 0
    assert "Capture : 3 événements, 2 runs\n" in capsys.readouterr().out


def test_an_empty_capture_is_not_conformant(
    capture: Capture, capsys: pytest.CaptureFixture[str]
) -> None:
    capture.write("evenements.json", "\n")
    assert cli.main(capture.arguments()) == 1
    out = capsys.readouterr().out
    assert "Capture : 0 événement, 0 run\n" in out
    assert "[contexte] no_complete_chain : capture entière\n" in out


def test_a_single_event_is_a_capture_of_one_event(
    capture: Capture, capsys: pytest.CaptureFixture[str]
) -> None:
    capture.write("evenements.json", json.dumps(captured_call(), indent=2))
    assert cli.main(capture.arguments("--profile", "capture")) == 0
    assert "Capture : 1 événement, 1 run\n" in capsys.readouterr().out


def test_several_logs_and_no_metrics_are_accepted(
    capture: Capture, capsys: pytest.CaptureFixture[str]
) -> None:
    second = capture.write("proxy.log", f"DEBUG {ANSWER}\n")
    arguments = capture.arguments("--logs", str(second))
    del arguments[arguments.index("--metrics") : arguments.index("--metrics") + 2]
    assert cli.main(arguments) == 1
    out = capsys.readouterr().out
    assert f"[contenu] canary_in_logs : {second}, ligne 1" in out
    assert "Métriques : aucun fichier fourni, rien n'a été fouillé." in out


UNREADABLE: dict[str, tuple[str, bytes, str]] = {
    "événements qui ne sont pas du JSON": (
        "evenements.json",
        b'{"eventType": "START"}\n{"eventType": \n',
        "ligne 2",
    ),
    "événements qui ne sont pas de l'UTF-8": ("evenements.json", b'["\xff\xfe\xfa"]', "UTF-8"),
    "nombre qui n'est pas du JSON": ("evenements.json", b'[{"a": NaN}]', "JSON"),
    "clé en double": ("evenements.json", b'[{"a": 1, "a": 2}]', "deux fois"),
    "clé en double sur une ligne": (
        "evenements.json",
        b'{"a": 1}\n{"a": 1, "a": 2}',
        "deux fois",
    ),
    "événements trop imbriqués": ("evenements.json", b"[" * 100_000, "JSON"),
    "canaris qui ne sont pas du JSON": ("canaris.json", b"canari", "JSON"),
    "canaris d'une autre forme": ("canaris.json", b'{"prompt": "x"}', "canaris"),
    "canaris ambigus": ("canaris.json", b'{"prompt": "x", "prompt": "y"}', "deux fois"),
    "journal compressé": ("agent.log", gzip.compress(b"INFO\n", mtime=0), "compressé"),
}


@pytest.mark.parametrize("case", list(UNREADABLE))
def test_an_unreadable_input_exits_with_two(
    case: str, capture: Capture, capsys: pytest.CaptureFixture[str]
) -> None:
    name, content, said = UNREADABLE[case]
    capture.write(name, content)
    assert cli.main(capture.arguments()) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("Entrée illisible : ")
    assert said in err
    assert str(capture.folder / name) in err


def test_a_missing_file_exits_with_two(
    capture: Capture, capsys: pytest.CaptureFixture[str]
) -> None:
    capture.metrics.unlink()
    assert cli.main(capture.arguments()) == 2
    assert (
        capsys.readouterr().err == f"Entrée illisible : {capture.metrics} : fichier introuvable.\n"
    )


def test_a_folder_given_for_a_file_exits_with_two(
    capture: Capture, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = capture.arguments("--logs", str(capture.folder))
    assert cli.main(arguments) == 2
    assert capsys.readouterr().err == (
        f"Entrée illisible : {capture.folder} : fichier illisible (IsADirectoryError).\n"
    )


def test_events_too_deeply_nested_to_be_judged_exit_with_two(
    capture: Capture, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Une erreur du kit ne doit jamais passer pour un verdict : elle sortirait avec 1."""

    def exhausted(*_: Any, **__: Any) -> Any:
        raise RecursionError

    monkeypatch.setattr(cli, "judge", exhausted)
    assert cli.main(capture.arguments()) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err == f"Entrée illisible : {capture.events} : événements trop imbriqués.\n"


def test_a_file_beyond_the_size_limit_exits_with_two(
    capture: Capture, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("answerlineage_conformance.sources.MAX_FILE_BYTES", 16)
    assert cli.main(capture.arguments()) == 2
    assert "trop volumineux" in capsys.readouterr().err


def test_a_file_name_that_is_not_plain_is_never_printed(
    capture: Capture, capsys: pytest.CaptureFixture[str]
) -> None:
    hostile = capture.write("journal\x1b[2J.log", "INFO\n")
    arguments = capture.arguments("--logs", str(hostile), "--format", "json")
    assert cli.main(arguments) == 0
    report = json.loads(capsys.readouterr().out)
    assert [file["path"] for file in report["files"] if file["role"] == "logs"] == [
        str(capture.logs),
        "<nom de fichier non affichable>",
    ]


@pytest.mark.parametrize("reference", ["hier", "2026-11-03", "2026-11-03T00:00:00"])
def test_a_reference_time_that_is_not_rfc_3339_exits_with_two(
    reference: str, capture: Capture, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = capture.arguments()
    arguments[arguments.index(REFERENCE)] = reference
    assert cli.main(arguments) == 2
    assert capsys.readouterr().err.startswith("Usage fautif : --received-at")


@pytest.mark.parametrize(
    "arguments",
    [[], ["check"], ["check", "evenements.json"], ["check", "e.json", "--canaries", "c.json"]],
)
def test_a_faulty_usage_exits_with_two(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(arguments)
    assert stopped.value.code == 2


def test_the_listing_of_problems_is_bounded(
    capture: Capture, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("answerlineage_conformance.judgement.MAX_PROBLEMS", 2)
    capture.write("agent.log", f"{PROMPT}\n" * 5)
    assert cli.main(capture.arguments("--format", "json")) == 1
    report = json.loads(capsys.readouterr().out)
    assert (report["problem_count"], report["truncated"], len(report["problems"])) == (5, True, 2)
    counted = {control["code"]: control["problems"] for control in report["controls"]}
    assert counted["canary_in_logs"] == 5

    assert cli.main(capture.arguments()) == 1
    assert "Problèmes : 5, dont les 2 premiers sont listés\n" in capsys.readouterr().out
