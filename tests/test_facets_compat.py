"""Un schéma de facette publié ne change plus ; une nouvelle version ne casse aucun émetteur."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts.check_facets_compat import (
    BASE_URL,
    breaking_changes,
    layout_problems,
    local_problems,
    main,
    version_changes,
)

OPENLINEAGE = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/"


def facet(version: str = "1-0-0") -> dict[str, Any]:
    """Une facette de run complète : un champ obligatoire, des champs facultatifs, des imbriqués."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"{BASE_URL}{version}/ToolCallRunFacet.json",
        "type": "object",
        "$defs": {
            "ToolCallRunFacet": {
                "allOf": [
                    {"$ref": OPENLINEAGE + "RunFacet"},
                    {
                        "type": "object",
                        "required": ["server"],
                        "properties": {
                            "server": {"type": "string"},
                            "tool": {"type": "string"},
                            "httpStatus": {"type": "integer"},
                            "headerMismatch": {"type": "array", "items": {"type": "string"}},
                            "limits": {"$ref": "#/$defs/Limits"},
                            "reason": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        },
                    },
                ],
                "type": "object",
            },
            "Limits": {
                "type": "object",
                "required": ["calls"],
                "properties": {"calls": {"type": "integer"}, "window": {"type": "string"}},
            },
        },
        "properties": {"answerlineage_toolCall": {"$ref": "#/$defs/ToolCallRunFacet"}},
    }


def members(schema: dict[str, Any]) -> dict[str, Any]:
    """Le membre du ``allOf`` qui décrit les champs propres de la facette."""
    member: dict[str, Any] = schema["$defs"]["ToolCallRunFacet"]["allOf"][1]
    return member


def changed(edit: Any, version: str = "1-0-0") -> dict[str, Any]:
    schema = facet(version)
    edit(schema)
    return schema


PATH = "spec/facets/1-0-0/ToolCallRunFacet.json"


def test_a_well_formed_facet_has_no_layout_problem() -> None:
    assert layout_problems(PATH, facet()) == []


@pytest.mark.parametrize(
    ("path", "edit", "expected"),
    [
        (PATH, lambda s: s.update({"$id": "https://exemple.invalid/x.json"}), "$id"),
        (
            "spec/facets/1-1-0/ToolCallRunFacet.json",
            lambda s: None,
            "$id",
        ),
        (
            PATH,
            lambda s: s.update({"$schema": "http://json-schema.org/draft-07/schema#"}),
            "2020-12",
        ),
        (PATH, lambda s: s["$defs"].pop("ToolCallRunFacet"), "$defs/ToolCallRunFacet"),
        (
            PATH,
            lambda s: s["$defs"]["ToolCallRunFacet"].update(
                allOf=[s["$defs"]["ToolCallRunFacet"]["allOf"][1]]
            ),
            "OpenLineage",
        ),
        (
            PATH,
            lambda s: s["$defs"]["ToolCallRunFacet"]["allOf"][0].update(
                {"$ref": OPENLINEAGE + "OutputDatasetFacet"}
            ),
            "se termine par OutputDatasetFacet",
        ),
        (
            PATH,
            lambda s: s["$defs"]["ToolCallRunFacet"]["allOf"][0].update(
                {"$ref": "https://exemple.invalid/OpenLineage.json#/$defs/RunFacet"}
            ),
            "OpenLineage",
        ),
        ("spec/facets/v1/ToolCallRunFacet.json", lambda s: None, "chemin"),
        ("spec/facets/1-0-0/outil.json", lambda s: None, "chemin"),
    ],
)
def test_layout_problem_is_reported(path: str, edit: Any, expected: str) -> None:
    problems = layout_problems(path, changed(edit))
    assert problems, "aucun écart relevé"
    assert any(expected in problem for problem in problems), problems


def test_a_document_that_is_not_an_object_is_a_layout_problem() -> None:
    assert layout_problems(PATH, []) != []


def test_a_new_version_may_add_fields_and_keep_the_rest() -> None:
    def add(schema: dict[str, Any]) -> None:
        members(schema)["properties"]["resultSize"] = {"type": "integer"}
        members(schema)["properties"]["readOnly"] = {"type": "boolean"}
        schema["$defs"]["Limits"]["properties"]["burst"] = {"type": "integer"}

    assert breaking_changes(facet(), changed(add, "1-1-0")) == []


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        (
            lambda s: members(s)["properties"].pop("tool"),
            "tool : le champ est retiré",
        ),
        (
            # Retirer un champ obligatoire : critère d'acceptation de la compatibilité ascendante.
            lambda s: (members(s)["properties"].pop("server"), members(s).update(required=[])),
            "server : le champ est retiré",
        ),
        (
            lambda s: members(s).update(required=[]),
            "server : le champ n'est plus obligatoire",
        ),
        (
            lambda s: members(s)["required"].append("tool"),
            "tool : le champ devient obligatoire",
        ),
        (
            lambda s: (
                members(s)["properties"].update(resultSize={"type": "integer"}),
                members(s)["required"].append("resultSize"),
            ),
            "resultSize : le champ devient obligatoire",
        ),
        (
            lambda s: members(s)["properties"]["httpStatus"].update(type="string"),
            "httpStatus : le type change (integer → string)",
        ),
        (
            lambda s: members(s)["properties"]["httpStatus"].update(type=["integer", "string"]),
            "httpStatus : le type change (integer → integer, string)",
        ),
        (
            lambda s: members(s)["properties"]["reason"].update(anyOf=[{"type": "string"}]),
            "reason : le type change (null, string → string)",
        ),
        (
            lambda s: members(s)["properties"]["headerMismatch"]["items"].update(type="integer"),
            "headerMismatch[] : le type change (string → integer)",
        ),
        (
            lambda s: s["$defs"]["Limits"]["properties"]["calls"].update(type="string"),
            "limits/calls : le type change (integer → string)",
        ),
        (
            lambda s: s["$defs"]["Limits"]["properties"].pop("window"),
            "limits/window : le champ est retiré",
        ),
        (
            lambda s: s["$defs"]["Limits"].update(required=[]),
            "limits/calls : le champ n'est plus obligatoire",
        ),
    ],
)
def test_incompatible_change_is_reported(edit: Any, expected: str) -> None:
    changes = breaking_changes(facet(), changed(edit, "1-1-0"))
    assert len(changes) == 1, changes
    assert expected in changes[0]


def test_self_referencing_definition_terminates() -> None:
    schema = facet()
    schema["$defs"]["Limits"]["properties"]["parent"] = {"$ref": "#/$defs/Limits"}
    assert breaking_changes(schema, copy.deepcopy(schema)) == []


def test_an_unresolvable_local_reference_is_read_as_an_untyped_field() -> None:
    schema = facet()
    members(schema)["properties"]["limits"] = {"$ref": "#/$defs/Absent"}
    assert breaking_changes(schema, copy.deepcopy(schema)) == []


def test_versions_are_chained_in_numeric_order() -> None:
    def drop_tool(schema: dict[str, Any]) -> None:
        members(schema)["properties"].pop("tool")

    path = "spec/facets/{}/ToolCallRunFacet.json"
    files = {
        path.format("1-10-0"): changed(drop_tool, "1-10-0"),
        path.format("1-2-0"): facet("1-2-0"),
        path.format("1-0-0"): facet("1-0-0"),
    }
    # 1-10-0 vient après 1-2-0 : c'est lui qui retire le champ, et lui seul.
    problems = version_changes(files)
    assert len(problems) == 1, problems
    assert "1-2-0 → 1-10-0" in problems[0]
    assert "tool : le champ est retiré" in problems[0]


def test_a_single_version_has_no_predecessor() -> None:
    assert version_changes({PATH: facet()}) == []


def test_each_facet_is_compared_with_its_own_earlier_versions() -> None:
    other = facet("1-1-0")
    other["$id"] = f"{BASE_URL}1-1-0/AgentRunFacet.json"
    other["$defs"] = {"AgentRunFacet": other["$defs"]["ToolCallRunFacet"]}
    other["properties"] = {"answerlineage_agent": {"$ref": "#/$defs/AgentRunFacet"}}
    files = {PATH: facet(), "spec/facets/1-1-0/AgentRunFacet.json": other}
    assert version_changes(files) == []


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(repository), *arguments],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "HOME": str(repository),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )
    return result.stdout.strip()


def write(repository: Path, version: str, schema: dict[str, Any]) -> Path:
    path = repository / "spec" / "facets" / version / "ToolCallRunFacet.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schema, indent=2) + "\n", "utf-8")
    return path


def commit(repository: Path, message: str) -> str:
    git(repository, "add", "-A")
    git(repository, "commit", "--quiet", "--no-gpg-sign", "-m", message)
    return git(repository, "rev-parse", "HEAD")


@pytest.fixture
def published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Un dépôt dont la branche cible a publié la version 1-0-0 de la facette."""
    git(tmp_path, "init", "--quiet", "--initial-branch", "main")
    (tmp_path / "README.md").write_text("projet\n", "utf-8")
    without_facets = commit(tmp_path, "base")
    write(tmp_path, "1-0-0", facet())
    base = commit(tmp_path, "facette")
    monkeypatch.chdir(tmp_path)
    return {"without": without_facets, "base": base}


def test_the_target_branch_without_published_facet_has_nothing_to_compare(
    published: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main({"BASE_SHA": published["without"]}) == 0
    assert "rien à comparer" in capsys.readouterr().out


def test_an_unchanged_publication_is_compatible(
    published: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main({"BASE_SHA": published["base"]}) == 0
    assert "compatibles" in capsys.readouterr().out


def test_without_a_target_branch_there_is_nothing_to_compare(
    published: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main({}) == 0
    assert "origin/main" in capsys.readouterr().out


def test_removing_a_required_field_of_a_published_facet_fails(
    tmp_path: Path, published: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    def remove_required(schema: dict[str, Any]) -> None:
        members(schema)["properties"].pop("server")
        members(schema).update(required=[])

    write(tmp_path, "1-0-0", changed(remove_required))
    assert main({"BASE_SHA": published["base"]}) == 1
    output = capsys.readouterr().out
    assert "spec/facets/1-0-0/ToolCallRunFacet.json" in output
    assert "ne change plus" in output
    assert "nouvelle version" in output


def test_a_published_file_that_changes_in_any_way_fails(
    tmp_path: Path, published: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(tmp_path, "1-0-0", facet())
    path.write_text(path.read_text("utf-8") + "\n", "utf-8")
    assert main({"BASE_SHA": published["base"]}) == 1
    assert "publié et modifié" in capsys.readouterr().out


def test_a_published_file_that_disappears_fails(
    tmp_path: Path, published: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / PATH).unlink()
    assert main({"BASE_SHA": published["base"]}) == 1
    assert "publié et retiré" in capsys.readouterr().out


def test_a_compatible_new_version_is_accepted_and_an_incompatible_one_fails(
    tmp_path: Path, published: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    def add(schema: dict[str, Any]) -> None:
        members(schema)["properties"]["resultSize"] = {"type": "integer"}

    write(tmp_path, "1-1-0", changed(add, "1-1-0"))
    assert main({"BASE_SHA": published["base"]}) == 0

    def remove_required(schema: dict[str, Any]) -> None:
        members(schema)["properties"].pop("server")
        members(schema).update(required=[])

    write(tmp_path, "1-1-0", changed(remove_required, "1-1-0"))
    assert main({"BASE_SHA": published["base"]}) == 1
    output = capsys.readouterr().out
    assert "1-0-0 → 1-1-0" in output
    assert "server : le champ est retiré" in output


def test_a_new_file_with_a_wrong_address_fails_even_when_nothing_is_published(
    tmp_path: Path, published: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    write(tmp_path, "1-1-0", facet("1-0-0"))
    assert main({"BASE_SHA": published["without"]}) == 1
    assert "$id" in capsys.readouterr().out


def test_a_malformed_target_reference_is_refused(
    published: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main({"BASE_SHA": "--upload-pack=x"}) == 2
    assert "illisible" in capsys.readouterr().out


def test_a_file_that_is_not_json_fails(
    tmp_path: Path, published: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "spec" / "facets" / "1-1-0" / "ToolCallRunFacet.json"
    path.parent.mkdir(parents=True)
    path.write_text("{", "utf-8")
    assert main({"BASE_SHA": published["base"]}) == 1
    assert "JSON" in capsys.readouterr().out


def write_extension(repository: Path, version: str, name: str, content: str) -> Path:
    path = repository / "spec" / "a2a" / version / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, "utf-8")
    return path


@pytest.mark.parametrize("name", ["LineageSummary.json", "README.md"])
def test_a_published_file_of_the_a2a_extension_never_changes(
    tmp_path: Path, published: dict[str, str], capsys: pytest.CaptureFixture[str], name: str
) -> None:
    """Une URI d'extension désigne pour toujours les mêmes fichiers, texte compris."""
    path = write_extension(tmp_path, "v1", name, '{"type": "object"}\n')
    base = commit(tmp_path, "extension")
    assert main({"BASE_SHA": base}) == 0

    path.write_text('{"type": "object", "maxItems": 1}\n', "utf-8")
    assert main({"BASE_SHA": base}) == 1
    assert f"spec/a2a/v1/{name} : publié et modifié" in capsys.readouterr().out

    path.unlink()
    assert main({"BASE_SHA": base}) == 1
    assert f"spec/a2a/v1/{name} : publié et retiré" in capsys.readouterr().out


def test_a_new_version_of_the_a2a_extension_is_a_new_folder(
    tmp_path: Path, published: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    write_extension(tmp_path, "v1", "LineageSummary.json", '{"type": "object"}\n')
    base = commit(tmp_path, "extension")
    write_extension(tmp_path, "v2", "LineageSummary.json", '{"type": "array"}\n')
    assert main({"BASE_SHA": base}) == 0
    assert "compatibles" in capsys.readouterr().out


def test_an_extension_published_without_facets_is_compared(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    git(tmp_path, "init", "--quiet", "--initial-branch", "main")
    path = write_extension(tmp_path, "v1", "CallerContext.json", "{}\n")
    base = commit(tmp_path, "extension seule")
    monkeypatch.chdir(tmp_path)
    assert main({"BASE_SHA": base}) == 0
    assert "compatibles" in capsys.readouterr().out
    path.write_text("[]\n", "utf-8")
    assert main({"BASE_SHA": base}) == 1


@pytest.mark.parametrize("name", ["Message.json", "vectors.json", "README.md"])
def test_a_published_file_of_the_webhook_format_never_changes(
    tmp_path: Path, published: dict[str, str], capsys: pytest.CaptureFixture[str], name: str
) -> None:
    """Un récepteur écrit contre une version du format la garde : schéma, vecteurs et texte."""
    path = tmp_path / "spec" / "webhooks" / "v1" / name
    path.parent.mkdir(parents=True)
    path.write_text('{"type": "object"}\n', "utf-8")
    base = commit(tmp_path, "webhooks")
    assert main({"BASE_SHA": base}) == 0

    path.write_text('{"type": "object", "maxProperties": 1}\n', "utf-8")
    assert main({"BASE_SHA": base}) == 1
    assert f"spec/webhooks/v1/{name} : publié et modifié" in capsys.readouterr().out

    path.unlink()
    assert main({"BASE_SHA": base}) == 1
    assert f"spec/webhooks/v1/{name} : publié et retiré" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["vectors.json", "README.md"])
def test_a_published_file_of_the_quarantine_signature_never_changes(
    tmp_path: Path, published: dict[str, str], capsys: pytest.CaptureFixture[str], name: str
) -> None:
    """Un proxy écrit contre une version de la signature des périmètres la garde."""
    path = tmp_path / "spec" / "quarantine" / "v1" / name
    path.parent.mkdir(parents=True)
    path.write_text('{"tolerance_seconds": 300}\n', "utf-8")
    base = commit(tmp_path, "quarantaine")
    assert main({"BASE_SHA": base}) == 0

    path.write_text('{"tolerance_seconds": 600}\n', "utf-8")
    assert main({"BASE_SHA": base}) == 1
    assert f"spec/quarantine/v1/{name} : publié et modifié" in capsys.readouterr().out

    path.unlink()
    assert main({"BASE_SHA": base}) == 1
    assert f"spec/quarantine/v1/{name} : publié et retiré" in capsys.readouterr().out


def test_the_repository_facets_respect_the_layout_and_chain_without_a_break() -> None:
    root = Path(__file__).resolve().parents[1] / "spec" / "facets"
    assert any(root.glob("*/*.json"))
    assert local_problems(root) == []
