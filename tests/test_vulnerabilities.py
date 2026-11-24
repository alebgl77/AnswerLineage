"""Une vulnérabilité haute ou critique sans exception valable fait échouer l'analyse de l'image."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from scripts.check_vulnerabilities import (
    VulnerabilityException,
    evaluate,
    findings,
    load_exceptions,
    main,
)

TODAY = date(2026, 11, 15)


def match(
    identifier: str = "CVE-2026-0001",
    severity: str = "Critical",
    package: str = "openssl",
    version: str = "3.0.11-1~deb12u2",
    fixed: list[str] | None = None,
    state: str = "fixed",
    related: list[str] | None = None,
) -> dict[str, Any]:
    """Une correspondance telle que Grype l'écrit dans son rapport JSON."""
    return {
        "vulnerability": {
            "id": identifier,
            "severity": severity,
            "fix": {"versions": ["3.0.15-1~deb12u1"] if fixed is None else fixed, "state": state},
        },
        "relatedVulnerabilities": [{"id": alias} for alias in related or []],
        "artifact": {"name": package, "version": version, "type": "deb"},
    }


def report(*matches: dict[str, Any]) -> dict[str, Any]:
    return {"matches": list(matches), "source": {"type": "sbom"}}


def exception(
    vulnerability: str = "CVE-2026-0001",
    package: str | None = None,
    review_by: date = date(2027, 1, 31),
) -> VulnerabilityException:
    return VulnerabilityException(
        vulnerability, package, "Code vulnérable jamais appelé.", review_by
    )


# Exceptions déclarées


def test_a_documented_exception_is_accepted() -> None:
    text = """
[[exception]]
vulnerability = "CVE-2026-0001"
package = "openssl"
reason = "La fonction vulnérable n'est jamais appelée par l'image."
review-by = 2027-01-31
"""
    exceptions, problems = load_exceptions(text, TODAY)
    assert problems == []
    assert exceptions == [
        VulnerabilityException(
            "CVE-2026-0001",
            "openssl",
            "La fonction vulnérable n'est jamais appelée par l'image.",
            date(2027, 1, 31),
        )
    ]


def test_an_empty_file_declares_no_exception() -> None:
    assert load_exceptions("# Aucune exception.\n", TODAY) == ([], [])


@pytest.mark.parametrize(
    ("entry", "problem"),
    [
        ('vulnerability = "CVE-2026-0001"\nreview-by = 2027-01-31', "motif"),
        ('vulnerability = "CVE-2026-0001"\nreason = "  "\nreview-by = 2027-01-31', "motif"),
        ('vulnerability = "CVE-2026-0001"\nreason = 3\nreview-by = 2027-01-31', "motif"),
        ('vulnerability = "CVE-2026-0001"\nreason = "Motif."', "date de revue"),
        ('vulnerability = "CVE-2026-0001"\nreason = "Motif."\nreview-by = "2027-01-31"', "date"),
        (
            'vulnerability = "CVE-2026-0001"\nreason = "Motif."\nreview-by = 2027-01-31T10:00:00',
            "date",
        ),
        ('vulnerability = "CVE-2026-0001"\nreason = "Motif."\nreview-by = 2026-11-14', "passée"),
        ('reason = "Motif."\nreview-by = 2027-01-31', "identifiant"),
        ('vulnerability = "CVE-*"\nreason = "Motif."\nreview-by = 2027-01-31', "identifiant"),
        ('vulnerability = "cve-2026-1"\nreason = "Motif."\nreview-by = 2027-01-31', "identifiant"),
        (
            'vulnerability = "CVE-2026-0001"\npackage = "open ssl*"\nreason = "Motif."\n'
            "review-by = 2027-01-31",
            "paquet",
        ),
        (
            'vulnerability = "CVE-2026-0001"\nreason = "Motif."\nreview-by = 2027-01-31\n'
            'fix-state = "not-fixed"',
            "clé inconnue",
        ),
    ],
)
def test_an_exception_without_reason_or_with_a_lapsed_review_is_refused(
    entry: str, problem: str
) -> None:
    exceptions, problems = load_exceptions(f"[[exception]]\n{entry}\n", TODAY)
    assert exceptions == []
    assert len(problems) == 1
    assert problem in problems[0]
    assert "exception n°1" in problems[0]


def test_the_review_date_may_be_today() -> None:
    text = '[[exception]]\nvulnerability = "CVE-2026-0001"\nreason = "M."\nreview-by = 2026-11-15\n'
    assert load_exceptions(text, TODAY)[1] == []


def test_a_rule_that_would_set_aside_a_whole_class_is_refused() -> None:
    """Seules des exceptions nommées sont admises : pas de règle globale (« sans correctif »…)."""
    _, problems = load_exceptions('only-fixed = true\nignore-states = "not-fixed"\n', TODAY)
    assert problems == ["clé inconnue au premier niveau : ignore-states, only-fixed"]


def test_exceptions_must_be_a_list_of_entries() -> None:
    assert load_exceptions('exception = "CVE-2026-0001"\n', TODAY)[1] == [
        "« exception » doit être une liste d'entrées [[exception]]"
    ]
    assert load_exceptions("exception = [1]\n", TODAY)[1] == [
        "exception n°1 : l'entrée n'est pas une table"
    ]


def test_the_same_vulnerability_cannot_be_excepted_twice() -> None:
    entry = (
        '[[exception]]\nvulnerability = "CVE-2026-0001"\nreason = "M."\nreview-by = 2027-01-31\n'
    )
    exceptions, problems = load_exceptions(entry * 2, TODAY)
    assert len(exceptions) == 1
    assert problems == ["exception n°2 (CVE-2026-0001) : déclarée deux fois"]


def test_an_unreadable_file_is_refused_without_echoing_its_content() -> None:
    _, problems = load_exceptions('secret = [ "jeton\n', TODAY)
    assert problems == ["fichier d'exceptions illisible (TOMLDecodeError)"]


def test_a_hostile_identifier_is_not_echoed() -> None:
    text = '[[exception]]\nvulnerability = "::set-env x"\nreason = "M."\nreview-by = 2027-01-31\n'
    _, problems = load_exceptions(text, TODAY)
    assert problems == ["exception n°1 : identifiant de vulnérabilité absent ou invalide"]


# Rapport de Grype


def test_findings_are_read_from_the_grype_report() -> None:
    (found,) = findings(report(match(related=["CVE-2026-0001"], identifier="GHSA-aaaa-bbbb-cccc")))
    assert found.vulnerability == "GHSA-aaaa-bbbb-cccc"
    assert found.aliases == ("CVE-2026-0001",)
    assert found.severity == "Critical"
    assert (found.package, found.version, found.package_type) == (
        "openssl",
        "3.0.11-1~deb12u2",
        "deb",
    )
    assert found.fixed_in == "3.0.15-1~deb12u1"


@pytest.mark.parametrize(
    ("fixed", "state", "expected"),
    [
        (["2.0", "1.9.3"], "fixed", "2.0, 1.9.3"),
        ([], "not-fixed", "aucune pour l'instant"),
        ([], "wont-fix", "aucune, ne sera pas corrigée"),
        ([], "unknown", "inconnue"),
    ],
)
def test_the_fixed_version_is_said_plainly(fixed: list[str], state: str, expected: str) -> None:
    (found,) = findings(report(match(fixed=fixed, state=state)))
    assert found.fixed_in == expected


@pytest.mark.parametrize(
    ("fix", "state", "versions"),
    [
        ({"versions": ["2.0", "1.9.3", "2.0"], "state": "fixed"}, "fixed", ("2.0", "1.9.3")),
        ({"versions": [], "state": "not-fixed"}, "not-fixed", ()),
        ({"versions": [], "state": "wont-fix"}, "wont-fix", ()),
        ({"state": "corrigée demain"}, "unknown", ()),
        ({"state": 3, "versions": "2.0"}, "unknown", ()),
        (None, "unknown", ()),
    ],
)
def test_the_fix_is_kept_as_grype_describes_it(
    fix: object, state: str, versions: tuple[str, ...]
) -> None:
    item = match()
    item["vulnerability"]["fix"] = fix
    (found,) = findings(report(item))
    assert (found.fix_state, found.fix_versions) == (state, versions)


def test_a_vulnerability_found_twice_in_a_package_is_counted_once() -> None:
    assert len(findings(report(match(), match()))) == 1


@pytest.mark.parametrize(
    "document", [[], {}, {"matches": "x"}, {"matches": [1]}, {"matches": [{"artifact": {}}]}]
)
def test_an_unexpected_report_is_refused(document: object) -> None:
    with pytest.raises(ValueError, match="rapport de Grype"):
        findings(document)


def test_values_from_the_report_cannot_inject_pipeline_commands() -> None:
    (found,) = findings(report(match(package="x\n::set-output name=a::b|`<script>`")))
    assert "\n" not in found.package
    assert "|" not in found.package
    assert "<" not in found.package
    assert "`" not in found.package


# Verdict


def test_high_and_critical_findings_without_exception_block() -> None:
    verdict = evaluate(
        findings(report(match(), match("CVE-2026-0002", "High"), match("CVE-2026-0003", "Medium"))),
        [],
    )
    assert [found.vulnerability for found in verdict.blocking] == ["CVE-2026-0001", "CVE-2026-0002"]
    assert verdict.others == {"Medium": 1}
    assert [found.vulnerability for found in verdict.minor] == ["CVE-2026-0003"]
    assert not verdict.passed


def test_an_exception_sets_aside_its_vulnerability_only() -> None:
    verdict = evaluate(
        findings(report(match(), match("CVE-2026-0002", "High"))), [exception("CVE-2026-0002")]
    )
    assert [found.vulnerability for found in verdict.blocking] == ["CVE-2026-0001"]
    assert [(found.vulnerability, rule.review_by) for found, rule in verdict.excepted] == [
        ("CVE-2026-0002", date(2027, 1, 31))
    ]


def test_an_exception_names_the_vulnerability_or_one_of_its_aliases() -> None:
    found = findings(report(match("GHSA-aaaa-bbbb-cccc", related=["CVE-2026-0001"])))
    assert evaluate(found, [exception("CVE-2026-0001")]).passed


def test_an_exception_restricted_to_a_package_does_not_cover_another() -> None:
    found = findings(report(match(package="openssl"), match(package="libssl3")))
    verdict = evaluate(found, [exception(package="openssl")])
    assert [item.package for item in verdict.blocking] == ["libssl3"]


def test_an_exception_that_matches_nothing_is_reported_as_unused() -> None:
    verdict = evaluate(findings(report()), [exception("CVE-2020-9999")])
    assert verdict.passed
    assert [rule.vulnerability for rule in verdict.unused] == ["CVE-2020-9999"]


# Ligne de commande


@pytest.fixture
def files(tmp_path: Path) -> Path:
    (tmp_path / "exceptions.toml").write_text(
        '[[exception]]\nvulnerability = "CVE-2026-0002"\nreason = "Jamais chargé par l\'image."\n'
        "review-by = 2027-01-31\n",
        encoding="utf-8",
    )
    return tmp_path


def run(arguments: list[str], **environment: str) -> int:
    return main(arguments, today=TODAY, environment=environment)


def test_the_report_fails_and_shows_each_finding(
    files: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    grype = files / "grype.json"
    grype.write_text(
        json.dumps(
            report(
                match(),
                match("CVE-2026-0002", "High", package="zlib1g", fixed=[], state="not-fixed"),
                match("CVE-2026-0003", "Low"),
            )
        ),
        encoding="utf-8",
    )
    summary = files / "summary.md"
    code = run(
        ["report", str(grype), "--exceptions", str(files / "exceptions.toml")],
        GITHUB_STEP_SUMMARY=str(summary),
    )
    output = capsys.readouterr().out
    assert code == 1
    blocking = next(line for line in output.splitlines() if "CVE-2026-0001" in line)
    for value in ("bloquante", "Critical", "openssl", "3.0.11-1~deb12u2", "3.0.15-1~deb12u1"):
        assert value in blocking
    excepted = next(line for line in output.splitlines() if "CVE-2026-0002" in line)
    assert "exception" in excepted
    assert "2027-01-31" in excepted
    assert "Low : 1" in output
    assert "1 vulnérabilité(s) haute(s) ou critique(s) sans exception valable" in output
    written = summary.read_text(encoding="utf-8")
    assert "| Critical | openssl | 3.0.11-1~deb12u2 | deb | CVE-2026-0001 |" in written
    assert "Jamais chargé par l'image." in written


def test_the_report_passes_when_every_blocking_finding_is_excepted(
    files: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    grype = files / "grype.json"
    grype.write_text(json.dumps(report(match("CVE-2026-0002", "High"))), encoding="utf-8")
    assert run(["report", str(grype), "--exceptions", str(files / "exceptions.toml")]) == 0
    assert "Aucune vulnérabilité haute ou critique sans exception valable" in (
        capsys.readouterr().out
    )


def test_a_lapsed_exception_no_longer_applies_and_fails_the_report(
    files: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    grype = files / "grype.json"
    grype.write_text(json.dumps(report(match("CVE-2026-0002", "High"))), encoding="utf-8")
    code = main(
        ["report", str(grype), "--exceptions", str(files / "exceptions.toml")],
        today=date(2027, 2, 1),
        environment={},
    )
    output = capsys.readouterr().out
    assert code == 1
    assert "date de revue passée (2027-01-31)" in output
    assert "bloquante" in next(line for line in output.splitlines() if "CVE-2026-0002" in line)


def test_the_exceptions_alone_are_checked(files: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["exceptions", "--file", str(files / "exceptions.toml")]) == 0
    assert "1 exception(s) valable(s)" in capsys.readouterr().out
    assert (
        main(
            ["exceptions", "--file", str(files / "exceptions.toml")],
            today=date(2027, 2, 1),
            environment={},
        )
        == 1
    )


def test_missing_or_unreadable_files_are_errors(
    files: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["exceptions", "--file", str(files / "absent.toml")]) == 2
    grype = files / "grype.json"
    grype.write_text("pas du json", encoding="utf-8")
    assert run(["report", str(grype), "--exceptions", str(files / "exceptions.toml")]) == 2
    grype.write_text("[]", encoding="utf-8")
    assert run(["report", str(grype), "--exceptions", str(files / "exceptions.toml")]) == 2
    assert run(["report", str(files / "absent.json")]) == 2
    output = capsys.readouterr().out
    assert "pas du json" not in output
