"""Le relevé des vulnérabilités connues d'une version : sources, états, format, exigence."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from scripts import check_release, known_vulnerabilities
from scripts.check_vulnerabilities import VulnerabilityException
from scripts.known_vulnerabilities import (
    DOCUMENT_FIELDS,
    EXCEPTION_FIELDS,
    FINDING_FIELDS,
    SOURCE_FIELDS,
    SOURCES,
    InputError,
    build,
    cyclonedx,
    npm_inventory,
    problems,
    python_inventory,
    read_scan,
    render_markdown,
)

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 23, 1, 30, tzinfo=UTC)
TODAY = NOW.date()
BUILT = "2026-11-22T04:12:33Z"
# Ce que Grype écrit autour des champs que le relevé reprend : rien n'en doit sortir.
LEAK = "FUITE"


# Inventaire des paquets Python, d'après le verrou

PYTHON_LOCK = """
version = 1
revision = 3
requires-python = ">=3.13"

[manifest]
members = ["exemple", "exemple-api", "exemple-workers"]

[[package]]
name = "exemple"
version = "0.0.0"
source = { virtual = "." }

[package.dev-dependencies]
dev = [{ name = "pytest" }]

[[package]]
name = "exemple-api"
version = "1.2.3"
source = { editable = "api" }
dependencies = [
    { name = "fastapi" },
    { name = "psycopg", extra = ["binary"] },
]

[[package]]
name = "exemple-workers"
version = "1.2.3"
source = { editable = "workers" }
dependencies = [{ name = "exemple-api" }, { name = "fastapi" }]

[[package]]
name = "fastapi"
version = "0.162.2"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "starlette" }]

[[package]]
name = "starlette"
version = "1.10.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "psycopg"
version = "3.3.8"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "tzdata", marker = "sys_platform == 'win32'" }]

[package.optional-dependencies]
binary = [{ name = "psycopg-binary", marker = "implementation_name != 'pypy'" }]
pool = [{ name = "psycopg-pool" }]

[[package]]
name = "psycopg-binary"
version = "3.3.8"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "psycopg-pool"
version = "3.3.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "tzdata"
version = "2026.6"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "pytest"
version = "9.0.1"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "pluggy" }]

[[package]]
name = "pluggy"
version = "1.7.0"
source = { registry = "https://pypi.org/simple" }
"""


def test_the_python_inventory_holds_the_runtime_dependencies_of_the_workspace() -> None:
    """Ni le groupe de développement, ni un extra que personne ne demande, ni les membres."""
    assert python_inventory(PYTHON_LOCK) == [
        ("fastapi", "0.162.2"),
        ("psycopg", "3.3.8"),
        ("psycopg-binary", "3.3.8"),
        ("starlette", "1.10.0"),
        # Réservé à Windows : les marqueurs ne sont pas évalués, du côté sûr.
        ("tzdata", "2026.6"),
    ]


def test_a_dependency_pinned_to_one_of_several_locked_versions_takes_that_one() -> None:
    lock = PYTHON_LOCK.replace(
        '{ name = "fastapi" },\n    { name = "psycopg"',
        '{ name = "numpy", version = "2.1.0" },\n    { name = "psycopg"',
    ) + "".join(
        f'\n[[package]]\nname = "numpy"\nversion = "{version}"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        for version in ("1.26.4", "2.1.0")
    )
    found = python_inventory(lock)
    assert ("numpy", "2.1.0") in found
    assert ("numpy", "1.26.4") not in found


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv absent")
def test_the_python_inventory_of_the_repository_is_what_uv_exports_from_the_lock() -> None:
    """Le parcours du verrou donne les paquets qu'uv installe sans le groupe de développement."""
    exported = subprocess.run(
        [  # noqa: S607
            "uv",
            "export",
            "--frozen",
            "--all-packages",
            "--no-dev",
            "--no-emit-workspace",
            "--format",
            "requirements.txt",
            "--no-hashes",
            "--no-annotate",
            "--no-header",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    expected = sorted(
        tuple(line.split(";")[0].strip().split("==")) for line in exported.splitlines() if line
    )
    found = python_inventory((ROOT / "uv.lock").read_text(encoding="utf-8"))
    assert found == expected
    assert "pytest" not in {name for name, _ in found}


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (lambda text: text.replace("version = 1\n", "version = 2\n", 1), "version du verrou"),
        (lambda text: "version = [", "verrou illisible"),
        (
            lambda text: text.replace('package]]\nname = "starlette"', 'package]]\nname = "autre"'),
            "ne décrit pas",
        ),
        (lambda text: text.replace('{ name = "starlette" }', '{ name = "x y" }'), "hors format"),
        (lambda text: text.replace('version = "1.10.0"', 'version = "1|0"'), "hors format"),
        (
            lambda text: text.replace('[[package]]\nname = "exemple"\n', "[[package]]\n"),
            "illisible",
        ),
    ],
)
def test_a_lock_that_cannot_be_read_with_certainty_is_refused(change: Any, problem: str) -> None:
    with pytest.raises(InputError, match=problem) as error:
        python_inventory(change(PYTHON_LOCK))
    assert "x y" not in str(error.value)
    assert "1|0" not in str(error.value)


# Inventaire des paquets npm de l'explorateur, d'après son verrou

NPM_LOCK: dict[str, Any] = {
    "name": "exemple-ui",
    "version": "0.0.0",
    "lockfileVersion": 3,
    "requires": True,
    "packages": {
        "": {
            "name": "exemple-ui",
            "version": "0.0.0",
            "dependencies": {"react": "19.3.1", "@portee/outil": "1.0.0"},
            "devDependencies": {"vite": "8.3.6"},
        },
        "node_modules/react": {
            "version": "19.3.1",
            "resolved": "https://registry.npmjs.org/react/-/react-19.3.1.tgz",
            "license": "MIT",
        },
        "node_modules/@portee/outil": {"version": "1.0.0", "dependencies": {"scheduler": "0.27.0"}},
        "node_modules/@portee/outil/node_modules/scheduler": {"version": "0.27.0"},
        "node_modules/scheduler": {"version": "0.28.0"},
        "node_modules/vite": {"version": "8.3.6", "dev": True},
        "node_modules/esbuild": {"version": "0.25.0", "dev": True, "optional": True},
        "node_modules/fsevents": {"version": "2.3.3", "optional": True},
        "node_modules/lien": {"resolved": "paquets/lien", "link": True},
        "paquets/lien": {"version": "0.1.0"},
    },
}


def test_the_npm_inventory_holds_what_the_built_explorer_embeds() -> None:
    """Ni les outils de construction (dev), ni le projet lui-même, ni un dossier local."""
    assert npm_inventory(json.dumps(NPM_LOCK)) == [
        ("@portee/outil", "1.0.0"),
        ("fsevents", "2.3.3"),
        ("react", "19.3.1"),
        ("scheduler", "0.27.0"),
        ("scheduler", "0.28.0"),
    ]


def test_the_npm_inventory_of_the_explorer_holds_its_dependencies_and_none_of_its_tools() -> None:
    manifest = json.loads((ROOT / "ui" / "package.json").read_text(encoding="utf-8"))
    lock = (ROOT / "ui" / "package-lock.json").read_text(encoding="utf-8")
    found = {name for name, _ in npm_inventory(lock)}
    assert set(manifest["dependencies"]) <= found
    assert not set(manifest["devDependencies"]) & found


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (lambda lock: lock | {"lockfileVersion": 1}, "version du verrou"),
        (lambda lock: lock | {"packages": []}, "illisible"),
        (
            lambda lock: (
                lock
                | {
                    "packages": {
                        **lock["packages"],
                        "node_modules/react": {"version": "19.3.1", "dev": True},
                    }
                }
            ),
            "dépendance directe",
        ),
        (
            lambda lock: (
                lock
                | {"packages": {**lock["packages"], "node_modules/re act": {"version": "1.0.0"}}}
            ),
            "hors format",
        ),
        (
            lambda lock: (
                lock
                | {"packages": {**lock["packages"], "node_modules/react": {"version": "1.0\n|x"}}}
            ),
            "hors format",
        ),
    ],
)
def test_an_npm_lock_that_cannot_be_read_with_certainty_is_refused(
    change: Any, problem: str
) -> None:
    with pytest.raises(InputError, match=problem) as error:
        npm_inventory(json.dumps(change(NPM_LOCK)))
    assert "re act" not in str(error.value)
    assert "|x" not in str(error.value)


def test_an_npm_lock_that_is_not_json_is_refused() -> None:
    with pytest.raises(InputError, match="illisible"):
        npm_inventory("{")


def test_an_inventory_is_a_cyclonedx_document_like_the_canary_of_the_image_analysis() -> None:
    document = cyclonedx("npm", [("@portee/outil", "1.0.0"), ("react", "19.3.1")])
    assert document["bomFormat"] == "CycloneDX"
    assert document["components"] == [
        {
            "type": "library",
            "bom-ref": "pkg:npm/%40portee/outil@1.0.0",
            "name": "@portee/outil",
            "version": "1.0.0",
            "purl": "pkg:npm/%40portee/outil@1.0.0",
        },
        {
            "type": "library",
            "bom-ref": "pkg:npm/react@19.3.1",
            "name": "react",
            "version": "19.3.1",
            "purl": "pkg:npm/react@19.3.1",
        },
    ]
    (component,) = cyclonedx("python", [("pyyaml", "5.3")])["components"]
    assert component["purl"] == "pkg:pypi/pyyaml@5.3"


def test_the_inventory_command_writes_the_sbom(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(PYTHON_LOCK, encoding="utf-8")
    output = tmp_path / "python.cdx.json"
    assert (
        known_vulnerabilities.main(
            ["inventory", "python", "--lock", str(lock), "--output", str(output)]
        )
        == 0
    )
    names = [item["name"] for item in json.loads(output.read_text())["components"]]
    assert names == ["fastapi", "psycopg", "psycopg-binary", "starlette", "tzdata"]

    lock = tmp_path / "package-lock.json"
    lock.write_text(json.dumps(NPM_LOCK), encoding="utf-8")
    output = tmp_path / "npm.cdx.json"
    assert (
        known_vulnerabilities.main(
            ["inventory", "npm", "--lock", str(lock), "--output", str(output)]
        )
        == 0
    )
    assert len(json.loads(output.read_text())["components"]) == 5


def test_the_inventory_command_refuses_an_absent_or_unreadable_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "sortie.json"
    assert (
        known_vulnerabilities.main(
            ["inventory", "npm", "--lock", str(tmp_path / "absent"), "--output", str(output)]
        )
        == 2
    )
    lock = tmp_path / "uv.lock"
    lock.write_text("secret = [", encoding="utf-8")
    assert (
        known_vulnerabilities.main(
            ["inventory", "python", "--lock", str(lock), "--output", str(output)]
        )
        == 2
    )
    assert not output.exists()
    assert "secret" not in capsys.readouterr().out


def test_an_oversized_lock_is_not_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(known_vulnerabilities, "_FILE_LIMIT", 100)
    lock = tmp_path / "uv.lock"
    lock.write_text(PYTHON_LOCK, encoding="utf-8")
    output = tmp_path / "python.cdx.json"
    assert (
        known_vulnerabilities.main(
            ["inventory", "python", "--lock", str(lock), "--output", str(output)]
        )
        == 2
    )


# Rapports de Grype


def match(
    identifier: str = "CVE-2026-0001",
    severity: str = "Critical",
    package: str = "libssl3",
    version: str = "3.5.9-r0",
    package_type: str = "apk",
    fixed: list[str] | None = None,
    state: str = "not-fixed",
    related: list[str] | None = None,
) -> dict[str, Any]:
    """Une correspondance comme Grype l'écrit, avec ce que le relevé ne reprend pas."""
    return {
        "vulnerability": {
            "id": identifier,
            "dataSource": f"https://{LEAK}.example/{identifier}",
            "namespace": f"{LEAK}:distro:alpine:3.23",
            "severity": severity,
            "urls": [f"https://{LEAK}.example"],
            "description": f"{LEAK} : description de la vulnérabilité.",
            "cvss": [{"vector": f"CVSS:3.1/{LEAK}", "metrics": {"baseScore": 9.8}}],
            "fix": {"versions": fixed or [], "state": state},
            "advisories": [{"id": LEAK, "link": LEAK}],
            "risk": 12.5,
        },
        "relatedVulnerabilities": [
            {"id": alias, "description": LEAK, "urls": [LEAK]} for alias in related or []
        ],
        "matchDetails": [{"type": "exact-direct-match", "matcher": LEAK, "searchedBy": LEAK}],
        "artifact": {
            "id": LEAK,
            "name": package,
            "version": version,
            "type": package_type,
            "locations": [{"path": f"/{LEAK}/lib/apk/db/installed", "layerID": LEAK}],
            "language": LEAK,
            "licenses": [LEAK],
            "cpes": [f"cpe:2.3:a:{LEAK}:*"],
            "purl": f"pkg:apk/alpine/{package}@{version}?{LEAK}",
            "upstreams": [{"name": LEAK}],
            "metadata": {"note": LEAK},
        },
    }


def grype(*matches: dict[str, Any], built: str = BUILT, valid: object = True) -> dict[str, Any]:
    """Un rapport JSON de Grype, avec la description de sa base de vulnérabilités."""
    return {
        "matches": list(matches),
        "ignoredMatches": [match(f"CVE-2026-9{index:03d}") for index in range(2)],
        "source": {"type": "sbom", "target": f"/{LEAK}/sbom.json"},
        "distro": {"name": LEAK, "version": "3.23.0", "idLike": []},
        "descriptor": {
            "name": "grype",
            "version": "0.121.2",
            "configuration": {"db": {"cache-dir": f"/cache/{LEAK}"}, "search": {"scope": LEAK}},
            "db": {
                "status": {
                    "schemaVersion": "v6.1.3",
                    "from": f"https://{LEAK}.example/vulnerability-db.tar.zst",
                    "built": built,
                    "path": f"/cache/{LEAK}/6/vulnerability.db",
                    "valid": valid,
                },
                "providers": {LEAK: {"captured": BUILT, "input": f"xxh64:{LEAK}"}},
            },
            "timestamp": f"2026-11-23T01:29:00Z{LEAK}",
        },
    }


EXCEPTION = VulnerabilityException(
    "CVE-2026-0002", None, "La fonction vulnérable n'est jamais appelée.", date(2027, 1, 31)
)


def statement(
    reports: dict[str, dict[str, Any]],
    exceptions: list[VulnerabilityException] | None = None,
    refused: list[str] | None = None,
    version: str = "1.2.3",
) -> dict[str, Any]:
    scans = [read_scan(source, report) for source, report in reports.items()]
    return build(scans, exceptions or [], refused or [], version, NOW)


def blocked_image() -> dict[str, Any]:
    """L'image d'aujourd'hui : OpenSSL sans correctif Alpine, et d'autres trouvailles."""
    return grype(
        match(),
        match(
            "CVE-2026-0002",
            "High",
            package="busybox",
            version="1.37.0-r20",
            state="fixed",
            fixed=["1.37.0-r21"],
        ),
        match("CVE-2026-0003", "Medium", package="zlib", version="1.3.1-r2"),
        match("CVE-2026-0004", "Low", package="zlib", version="1.3.1-r2"),
        match("CVE-2026-0005", "Negligible", package="musl", version="1.2.5-r10"),
        match("CVE-2026-0006", "Unknown", package="musl", version="1.2.5-r10", state="wont-fix"),
    )


# Les états


def test_each_vulnerability_found_appears_with_its_state_whatever_its_severity() -> None:
    document = statement({"image": blocked_image()}, [EXCEPTION])
    rows = {row["vulnerability"]: row for row in document["findings"]}
    assert {identifier: row["state"] for identifier, row in rows.items()} == {
        "CVE-2026-0001": "blocking",
        "CVE-2026-0002": "excepted",
        "CVE-2026-0003": "non_blocking",
        "CVE-2026-0004": "non_blocking",
        "CVE-2026-0005": "non_blocking",
        "CVE-2026-0006": "non_blocking",
    }
    assert document["summary"] == {"blocking": 1, "excepted": 1, "non_blocking": 4}
    assert rows["CVE-2026-0002"]["exception"] == {
        "reason": "La fonction vulnérable n'est jamais appelée.",
        "review_by": "2027-01-31",
    }
    assert rows["CVE-2026-0001"]["exception"] is None
    assert problems(document) == []


def test_a_blocking_vulnerability_without_known_fix_is_shown_as_such() -> None:
    """Le cas d'OpenSSL dans l'image Alpine : critique, et Alpine n'a pas de correctif."""
    document = statement({"image": blocked_image()})
    blocking = document["findings"][0]
    assert blocking == {
        "state": "blocking",
        "source": "image",
        "severity": "Critical",
        "vulnerability": "CVE-2026-0001",
        "aliases": [],
        "package": "libssl3",
        "package_version": "3.5.9-r0",
        "package_type": "apk",
        "fix_state": "not-fixed",
        "fixed_in": [],
        "exception": None,
    }
    text = render_markdown(document)
    assert (
        "| bloquante | critique | Image | libssl3 | 3.5.9-r0 | apk | CVE-2026-0001 "
        "| aucune pour l'instant |  |"
    ) in text
    assert "| bloquante | 2 |" in text


def test_an_excepted_vulnerability_shows_its_reason_and_its_review_date() -> None:
    text = render_markdown(statement({"image": blocked_image()}, [EXCEPTION]))
    assert (
        "| écartée par une exception | haute | Image | busybox | 1.37.0-r20 | apk | CVE-2026-0002 "
        "| 1.37.0-r21 | revue le 2027-01-31 : La fonction vulnérable n'est jamais appelée. |"
    ) in text


def test_an_empty_statement_says_so() -> None:
    document = statement({source: grype() for source in SOURCES})
    assert document["findings"] == []
    assert document["summary"] == {"blocking": 0, "excepted": 0, "non_blocking": 0}
    text = render_markdown(document)
    assert "Aucune vulnérabilité connue" in text
    assert "| État | Gravité |" not in text
    assert problems(document) == []


def test_a_lapsed_exception_is_refused_as_today_and_its_vulnerability_blocks(
    tmp_path: Path,
) -> None:
    exceptions = tmp_path / "exceptions.toml"
    exceptions.write_text(
        '[[exception]]\nvulnerability = "CVE-2026-0002"\nreason = "Motif."\n'
        "review-by = 2026-11-01\n",
        encoding="utf-8",
    )
    paths = write_reports(tmp_path, {"image": blocked_image(), "python": grype(), "npm": grype()})
    output = run_build(tmp_path, paths, exceptions)
    document = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
    states = {row["vulnerability"]: row["state"] for row in document["findings"]}
    assert states["CVE-2026-0002"] == "blocking"
    assert document["refused_exceptions"] == [
        "exception n°1 (CVE-2026-0002) : date de revue passée (2026-11-01)"
    ]
    text = output.with_suffix(".md").read_text(encoding="utf-8")
    assert "## Exceptions refusées" in text
    assert "exception n°1 (CVE-2026-0002) : date de revue passée (2026-11-01)" in text


# Les sources


def test_each_source_is_named_with_its_grype_and_its_database() -> None:
    document = statement(
        {
            "npm": grype(match("GHSA-aaaa-bbbb-cccc", "High", "react", "19.3.1", "npm")),
            "image": grype(match()),
            "python": grype(
                match(
                    "PYSEC-2026-1",
                    "Medium",
                    "pyyaml",
                    "6.0.3",
                    "python",
                    related=["CVE-2026-0010", "PYSEC-2026-1"],
                ),
                built="2026-11-22T05:00:00Z",
            ),
        }
    )
    assert [source["name"] for source in document["sources"]] == ["image", "python", "npm"]
    assert document["sources"][1] == {
        "name": "python",
        "scanner": "grype",
        "scanner_version": "0.121.2",
        "database_schema": "v6.1.3",
        "database_built_at": "2026-11-22T05:00:00Z",
    }
    assert [(row["source"], row["vulnerability"]) for row in document["findings"]] == [
        ("image", "CVE-2026-0001"),
        ("npm", "GHSA-aaaa-bbbb-cccc"),
        ("python", "PYSEC-2026-1"),
    ]
    assert document["findings"][2]["aliases"] == ["CVE-2026-0010"]
    text = render_markdown(document)
    assert "| Paquets Python |" in text
    assert "| Explorateur |" in text
    assert "schéma v6.1.3, construite le 2026-11-22T05:00:00Z" in text
    assert "PYSEC-2026-1 (CVE-2026-0010)" in text


def test_a_vulnerability_found_in_two_sources_appears_in_each() -> None:
    same = match("CVE-2026-0020", "Medium", "pyyaml", "6.0.3", "python")
    document = statement({"image": grype(same), "python": grype(same)})
    assert [row["source"] for row in document["findings"]] == ["image", "python"]


def test_the_order_is_stable_whatever_the_order_of_the_reports() -> None:
    reports = {
        "image": blocked_image(),
        "python": grype(
            *(match(f"CVE-2026-01{i:02d}", "High", f"p{i % 3}", "1.0", "python") for i in range(12))
        ),
        "npm": grype(match("CVE-2026-0300", "Low", "react", "19.3.1", "npm")),
    }
    first = statement(reports, [EXCEPTION])
    shuffled = {}
    for source in ("npm", "image", "python"):
        report = json.loads(json.dumps(reports[source]))
        report["matches"] = report["matches"][3:][::-1] + report["matches"][:3]
        shuffled[source] = report
    second = statement(shuffled, [EXCEPTION])
    assert json.dumps(first) == json.dumps(second)
    assert render_markdown(first) == render_markdown(second)
    states = [row["state"] for row in first["findings"]]
    assert states == sorted(states, key=["blocking", "excepted", "non_blocking"].index)


# Ce que le relevé recopie


def test_nothing_of_the_grype_report_is_copied_outside_the_expected_fields() -> None:
    document = statement(
        {"image": blocked_image(), "python": grype(match(related=["GHSA-xxxx-yyyy-zzzz"]))},
        [EXCEPTION],
    )
    assert LEAK not in json.dumps(document)
    assert LEAK not in render_markdown(document)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("identifier", "CVE-2026-0001\n| injecté |"),
        ("identifier", "CVE 2026"),
        ("package", "x\n::set-output name=a::b"),
        ("package", "<script>"),
        ("package", "p" * 300),
        ("version", "1.0`injecté`"),
        ("package_type", "APK|injecté"),
    ],
)
def test_a_value_outside_its_form_is_not_copied(field: str, value: str) -> None:
    values: dict[str, Any] = {field: value}
    document = statement({"image": grype(match(**values))})
    (row,) = document["findings"]
    assert "?" in (
        row["vulnerability"],
        row["package"],
        row["package_version"],
        row["package_type"],
    )
    text = render_markdown(document)
    assert "injecté" not in text
    assert "set-output" not in text
    assert "<" not in text
    assert "p" * 200 not in text
    assert problems(document) == []


def test_a_finding_keeps_its_state_when_its_identifier_is_not_copied() -> None:
    """Un identifiant hors format ne fait pas disparaître une vulnérabilité bloquante."""
    document = statement({"image": grype(match("CVE 2026"))})
    assert document["summary"]["blocking"] == 1


def test_lists_are_bounded() -> None:
    aliases = [f"CVE-2026-{index:04d}" for index in range(100, 140)]
    fixed = [f"1.{index}" for index in range(40)]
    document = statement({"image": grype(match(related=aliases, fixed=fixed, state="fixed"))})
    (row,) = document["findings"]
    assert len(row["aliases"]) == known_vulnerabilities.LIST_LIMIT
    assert len(row["fixed_in"]) == known_vulnerabilities.LIST_LIMIT


def test_too_many_findings_are_refused_rather_than_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(known_vulnerabilities, "FINDINGS_LIMIT", 3)
    with pytest.raises(InputError, match="plus de 3"):
        statement({"image": blocked_image()})


@pytest.mark.parametrize(
    "report",
    [
        grype(built="lundi"),
        grype(valid=False),
        grype(valid="true"),
        {**grype(), "descriptor": {"name": "grype", "version": "0.121.2"}},
        {**grype(), "descriptor": {**grype()["descriptor"], "version": "dernière"}},
        {**grype(), "descriptor": []},
        [],
        {"matches": "x", "descriptor": grype()["descriptor"]},
    ],
)
def test_a_report_without_its_grype_and_database_is_refused(report: object) -> None:
    with pytest.raises(InputError) as error:
        read_scan("image", report)
    assert "lundi" not in str(error.value)
    assert "dernière" not in str(error.value)


def test_the_version_of_the_statement_must_have_the_form_of_a_version() -> None:
    with pytest.raises(InputError, match="version"):
        statement({"image": grype()}, version="1.2.3\n# injecté")


# Le format


def valid_document() -> dict[str, Any]:
    return statement(
        {
            "image": blocked_image(),
            "python": grype(match("CVE-2026-0050", "Low", "pyyaml", "6.0.3", "python")),
            "npm": grype(),
        },
        [EXCEPTION],
    )


def finding_change(index: int, **values: object) -> Any:
    def change(document: dict[str, Any]) -> None:
        document["findings"][index].update(values)

    return change


@pytest.mark.parametrize(
    "change",
    [
        lambda document: document.update(format="autre"),
        lambda document: document.update(format_version=2),
        lambda document: document.update(format_version=True),
        lambda document: document.update(version="1.2.3 x"),
        lambda document: document.update(generated_at="2026-11-23"),
        lambda document: document.update(extra=1),
        lambda document: document.pop("summary"),
        lambda document: document.update(sources={}),
        lambda document: document["sources"].append(dict(document["sources"][0])),
        lambda document: document["sources"][0].update(name="pypi"),
        lambda document: document["sources"][0].update(scanner="trivy"),
        lambda document: document["sources"][0].update(database_built_at="hier"),
        lambda document: document["sources"][0].update(extra=1),
        lambda document: document["sources"].__setitem__(0, "image"),
        lambda document: document["summary"].update(blocking=0),
        lambda document: document["summary"].update(blocking=True),
        lambda document: document.update(refused_exceptions="aucune"),
        lambda document: document.update(refused_exceptions=[3]),
        lambda document: document.update(findings={}),
        lambda document: document["findings"].__setitem__(0, []),
        finding_change(0, state="ignorée"),
        finding_change(0, source="pypi"),
        finding_change(0, severity="Grave"),
        finding_change(0, vulnerability="CVE 1"),
        finding_change(0, aliases="CVE-1"),
        finding_change(0, aliases=["CVE 1"]),
        finding_change(0, package="a|b"),
        finding_change(0, package_version=""),
        finding_change(0, package_type="APK"),
        finding_change(0, fix_state="demain"),
        finding_change(0, fixed_in=["1.0"] * 17),
        finding_change(0, exception={"reason": "M.", "review_by": "2027-01-31"}),
        finding_change(1, exception=None),
        finding_change(1, exception={"reason": "", "review_by": "2027-01-31"}),
        finding_change(1, exception={"reason": "M.", "review_by": "bientôt"}),
        finding_change(1, exception={"reason": "M."}),
        finding_change(0, extra=1),
        lambda document: document["findings"].reverse(),
        lambda document: document.update(findings=[document["findings"][0]] * 2),
    ],
)
def test_a_statement_outside_its_format_is_refused(change: Any) -> None:
    document = valid_document()
    assert problems(document) == []
    change(document)
    found = problems(document)
    assert found
    assert "injecté" not in " ".join(found)


def test_a_statement_that_is_not_an_object_is_refused() -> None:
    assert problems([]) == ["le relevé n'est pas un objet JSON"]


def test_every_field_of_the_statement_is_documented() -> None:
    text = (ROOT / "docs" / "publication.md").read_text(encoding="utf-8")
    for field in (*DOCUMENT_FIELDS, *SOURCE_FIELDS, *FINDING_FIELDS, *EXCEPTION_FIELDS):
        assert f"| `{field}` |" in text, field


# La ligne de commande


def write_reports(directory: Path, reports: dict[str, Any]) -> list[str]:
    arguments = []
    for source, report in reports.items():
        path = directory / f"grype-{source}.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        arguments += ["--source", f"{source}={path}"]
    return arguments


def run_build(directory: Path, sources: list[str], exceptions: Path) -> Path:
    output = directory / "known-vulnerabilities"
    code = known_vulnerabilities.main(
        [
            "build",
            "--version",
            "1.2.3",
            *sources,
            "--exceptions",
            str(exceptions),
            "--json",
            str(output.with_suffix(".json")),
            "--markdown",
            str(output.with_suffix(".md")),
        ],
        now=NOW,
    )
    assert code == 0
    return output


def test_the_build_command_writes_the_statement_in_json_and_in_markdown(tmp_path: Path) -> None:
    exceptions = tmp_path / "exceptions.toml"
    exceptions.write_text(
        '[[exception]]\nvulnerability = "CVE-2026-0002"\nreason = "Jamais appelée."\n'
        "review-by = 2027-01-31\n",
        encoding="utf-8",
    )
    paths = write_reports(tmp_path, {"image": blocked_image(), "python": grype(), "npm": grype()})
    output = run_build(tmp_path, paths, exceptions)
    raw = output.with_suffix(".json").read_text(encoding="utf-8")
    document = json.loads(raw)
    assert problems(document) == []
    assert document["generated_at"] == "2026-11-23T01:30:00Z"
    assert raw.endswith("}\n")
    text = output.with_suffix(".md").read_text(encoding="utf-8")
    assert text.startswith("# Vulnérabilités connues d'AnswerLineage 1.2.3\n")
    assert "Relevé du 2026-11-23T01:30:00Z" in text
    # Deux exécutions sur les mêmes rapports écrivent les mêmes octets.
    again = run_build(tmp_path, paths, exceptions)
    assert again.with_suffix(".json").read_text(encoding="utf-8") == raw


@pytest.mark.parametrize(
    "sources",
    [
        ["--source", "pypi=x.json"],
        ["--source", "image"],
        ["--source", "image=a.json", "--source", "image=b.json"],
        [],
    ],
)
def test_the_build_command_refuses_an_unknown_or_repeated_source(
    tmp_path: Path, sources: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "releve"
    code = known_vulnerabilities.main(
        [
            "build",
            "--version",
            "1.2.3",
            *sources,
            "--json",
            str(output.with_suffix(".json")),
            "--markdown",
            str(output.with_suffix(".md")),
        ],
        now=NOW,
    )
    assert code == 2
    assert not output.with_suffix(".json").exists()
    assert "pypi" not in capsys.readouterr().out


def test_the_build_command_refuses_an_unreadable_report_without_echoing_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    exceptions = tmp_path / "exceptions.toml"
    exceptions.write_text("", encoding="utf-8")
    report = tmp_path / "grype.json"
    report.write_text("secret {", encoding="utf-8")
    output = tmp_path / "releve"
    arguments = [
        "build",
        "--version",
        "1.2.3",
        "--source",
        f"image={report}",
        "--exceptions",
        str(exceptions),
        "--json",
        str(output.with_suffix(".json")),
        "--markdown",
        str(output.with_suffix(".md")),
    ]
    assert known_vulnerabilities.main(arguments, now=NOW) == 2
    report.write_text(json.dumps(grype(built="lundi")), encoding="utf-8")
    assert known_vulnerabilities.main(arguments, now=NOW) == 2
    report.unlink()
    assert known_vulnerabilities.main(arguments, now=NOW) == 2
    report.write_text(json.dumps(grype()), encoding="utf-8")
    exceptions.unlink()
    assert known_vulnerabilities.main(arguments, now=NOW) == 2
    exceptions.write_text("", encoding="utf-8")
    monkeypatch.setattr(known_vulnerabilities, "_FILE_LIMIT", 10)
    assert known_vulnerabilities.main(arguments, now=NOW) == 2
    output_text = capsys.readouterr().out
    assert "secret" not in output_text
    assert "lundi" not in output_text
    assert not output.with_suffix(".json").exists()


def test_a_statement_that_would_leave_its_format_is_not_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un changement de format casse dès la merge request, pas à la publication."""
    monkeypatch.setattr(known_vulnerabilities, "problems", lambda document: ["écart"])
    exceptions = tmp_path / "exceptions.toml"
    exceptions.write_text("", encoding="utf-8")
    paths = write_reports(tmp_path, {"image": grype()})
    output = tmp_path / "known-vulnerabilities"
    code = known_vulnerabilities.main(
        [
            "build",
            "--version",
            "1.2.3",
            *paths,
            "--exceptions",
            str(exceptions),
            "--json",
            str(output.with_suffix(".json")),
            "--markdown",
            str(output.with_suffix(".md")),
        ],
        now=NOW,
    )
    assert code == 2
    assert not output.with_suffix(".json").exists()


def test_an_unreadable_exceptions_file_is_refused_as_today(tmp_path: Path) -> None:
    """Aucune exception ne s'applique : chaque vulnérabilité haute ou critique est bloquante."""
    exceptions = tmp_path / "exceptions.toml"
    exceptions.write_text("secret = [", encoding="utf-8")
    paths = write_reports(tmp_path, {"image": blocked_image()})
    output = run_build(tmp_path, paths, exceptions)
    document = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
    assert document["refused_exceptions"] == ["fichier d'exceptions illisible (TOMLDecodeError)"]
    assert document["summary"]["blocking"] == 2


# La publication exige le relevé


def write_statement(directory: Path, document: object) -> Path:
    path = directory / "known-vulnerabilities.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def clean_document() -> dict[str, Any]:
    return statement(
        {
            "image": grype(match("CVE-2026-0002", "High", state="fixed", fixed=["3.5.10-r0"])),
            "python": grype(match("CVE-2026-0050", "Low", "pyyaml", "6.0.3", "python")),
            "npm": grype(),
        },
        [EXCEPTION],
    )


def test_the_publication_accepts_a_statement_without_blocking_vulnerability(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_statement(tmp_path, clean_document())
    assert check_release.main(["vulnerabilities", str(path), "--tag", "v1.2.3"]) == 0
    out = capsys.readouterr().out
    assert "aucune bloquante" in out
    assert "1 écartée(s) par une exception, 1 non bloquante(s)" in out
    assert BUILT in out
    assert check_release.main(["vulnerabilities", str(path)]) == 0


def test_the_publication_refuses_a_blocking_vulnerability(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document = statement({"image": blocked_image(), "python": grype(), "npm": grype()}, [EXCEPTION])
    path = write_statement(tmp_path, document)
    assert check_release.main(["vulnerabilities", str(path), "--tag", "v1.2.3"]) == 1
    out = capsys.readouterr().out
    assert (
        "vulnérabilité bloquante : CVE-2026-0001, libssl3 3.5.9-r0 (apk, source image), "
        "corrigée dans : aucune pour l'instant"
    ) in out
    assert "1 écart(s) : rien ne doit être publié." in out


@pytest.mark.parametrize(
    ("document", "tag", "problem"),
    [
        (
            statement({"image": grype(), "python": grype()}),
            "v1.2.3",
            "relevé : source npm absente",
        ),
        (
            clean_document(),
            "v1.2.4",
            "relevé de la version 1.2.3, l'étiquette v1.2.4 demande 1.2.4",
        ),
        (
            statement(
                {source: grype() for source in SOURCES},
                refused=["exception n°1 (CVE-2026-0002) : date de revue passée (2026-11-01)"],
            ),
            "v1.2.3",
            "exception refusée : exception n°1 (CVE-2026-0002)",
        ),
        ({"format": "autre"}, "v1.2.3", "relevé : "),
    ],
)
def test_the_publication_refuses_an_incomplete_or_foreign_statement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], document: object, tag: str, problem: str
) -> None:
    path = write_statement(tmp_path, document)
    assert check_release.main(["vulnerabilities", str(path), "--tag", tag]) == 1
    assert problem in capsys.readouterr().out


def test_the_publication_requires_the_statement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert check_release.main(["vulnerabilities", str(tmp_path / "absent.json")]) == 2
    path = tmp_path / "known-vulnerabilities.json"
    path.write_text("secret {", encoding="utf-8")
    assert check_release.main(["vulnerabilities", str(path)]) == 2
    path.write_text(json.dumps(clean_document()), encoding="utf-8")
    assert check_release.main(["vulnerabilities", str(path), "--tag", "v1.2"]) == 2
    out = capsys.readouterr().out
    assert "relevé absent ou illisible" in out
    assert "relevé illisible" in out
    assert "secret" not in out
