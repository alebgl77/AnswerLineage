"""Un changement incompatible sur une route publiée fait échouer le pipeline."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts.check_openapi_compat import breaking_changes, main

PUBLISHED: dict[str, Any] = {
    "openapi": "3.1.0",
    "paths": {
        "/api/v1/answers/{answer_id}/sources": {
            "get": {
                "parameters": [
                    {
                        "name": "answer_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "integer"},
                    },
                    {
                        "name": "limit",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "integer"},
                    },
                ],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Sources"}}
                        }
                    },
                    "404": {"description": "Réponse inconnue"},
                },
            }
        },
        "/api/v1/lineage": {
            "post": {
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Event"}}
                    },
                },
                "responses": {"201": {"description": "Créé"}},
            }
        },
    },
    "components": {
        "schemas": {
            "Sources": {
                "type": "object",
                "required": ["datasets"],
                "properties": {
                    "datasets": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/Dataset"},
                    },
                    "note": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                },
            },
            "Dataset": {
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}, "columns": {"type": "array"}},
            },
            "Event": {
                "type": "object",
                "required": ["runId"],
                "properties": {"runId": {"type": "string"}, "eventType": {"type": "string"}},
            },
        }
    },
}
SOURCES = "/api/v1/answers/{answer_id}/sources"


def changed(edit: Any) -> dict[str, Any]:
    current = copy.deepcopy(PUBLISHED)
    edit(current)
    return current


def test_identical_contract_is_compatible() -> None:
    assert breaking_changes(PUBLISHED, copy.deepcopy(PUBLISHED)) == []


def test_additions_are_compatible() -> None:
    def add(document: dict[str, Any]) -> None:
        document["paths"]["/api/v1/impact"] = {"get": {"responses": {"200": {}}}}
        document["paths"][SOURCES]["get"]["responses"]["429"] = {"description": "Trop de requêtes"}
        document["paths"][SOURCES]["get"]["parameters"].append(
            {"name": "depth", "in": "query", "required": False, "schema": {"type": "integer"}}
        )
        document["components"]["schemas"]["Dataset"]["properties"]["kind"] = {"type": "string"}
        document["components"]["schemas"]["Event"]["properties"]["facets"] = {"type": "object"}

    assert breaking_changes(PUBLISHED, changed(add)) == []


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        (lambda d: d["paths"].pop(SOURCES), "la route est retirée"),
        (lambda d: d["paths"]["/api/v1/lineage"].pop("post"), "l'opération est retirée"),
        (
            lambda d: d["paths"][SOURCES]["get"]["responses"].pop("404"),
            "la réponse 404 est retirée",
        ),
        (
            lambda d: d["components"]["schemas"]["Sources"]["properties"].pop("note"),
            "réponse 200/note : le champ est retiré de la réponse",
        ),
        (
            lambda d: d["components"]["schemas"]["Dataset"]["properties"].pop("name"),
            "réponse 200/datasets[]/name : le champ est retiré de la réponse",
        ),
        (
            lambda d: d["components"]["schemas"]["Dataset"]["properties"]["name"].update(
                type="integer"
            ),
            "réponse 200/datasets[]/name : le type change (string → integer)",
        ),
        (
            lambda d: d["components"]["schemas"]["Sources"].update(required=[]),
            "réponse 200/datasets : le champ n'est plus garanti dans la réponse",
        ),
        (
            lambda d: d["components"]["schemas"]["Event"]["required"].append("eventType"),
            "requête/eventType : le champ devient obligatoire",
        ),
        (
            lambda d: d["components"]["schemas"]["Event"]["properties"]["runId"].update(
                type="integer"
            ),
            "requête/runId : le type change (string → integer)",
        ),
        (
            lambda d: d["paths"][SOURCES]["get"]["parameters"][1].update(required=True),
            "paramètre limit : le paramètre devient obligatoire",
        ),
        (
            lambda d: d["paths"][SOURCES]["get"]["parameters"].append(
                {"name": "tenant", "in": "query", "required": True, "schema": {"type": "string"}}
            ),
            "paramètre tenant : le paramètre devient obligatoire",
        ),
        (
            lambda d: d["paths"][SOURCES]["get"]["parameters"][0]["schema"].update(type="string"),
            "paramètre answer_id : le type change (integer → string)",
        ),
        (
            lambda d: d["paths"][SOURCES]["get"].update(requestBody={"required": True}),
            "le corps de la requête devient obligatoire",
        ),
    ],
)
def test_incompatible_change_is_reported(edit: Any, expected: str) -> None:
    changes = breaking_changes(PUBLISHED, changed(edit))
    assert len(changes) == 1, changes
    assert expected in changes[0]


def test_response_may_become_narrower_and_request_wider() -> None:
    """Rendre toujours une chaîne là où null était possible, ou accepter un type de plus."""

    def widen_request_narrow_response(document: dict[str, Any]) -> None:
        document["components"]["schemas"]["Sources"]["properties"]["note"] = {"type": "string"}
        document["components"]["schemas"]["Event"]["properties"]["eventType"] = {
            "anyOf": [{"type": "string"}, {"type": "null"}]
        }

    assert breaking_changes(PUBLISHED, changed(widen_request_narrow_response)) == []

    def widen_response(document: dict[str, Any]) -> None:
        document["components"]["schemas"]["Dataset"]["properties"]["name"] = {
            "type": ["string", "null"]
        }

    assert len(breaking_changes(PUBLISHED, changed(widen_response))) == 1


def test_self_referencing_schema_terminates() -> None:
    document = copy.deepcopy(PUBLISHED)
    document["components"]["schemas"]["Dataset"]["properties"]["parent"] = {
        "$ref": "#/components/schemas/Dataset"
    }
    assert breaking_changes(document, copy.deepcopy(document)) == []


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


def test_pipeline_fails_on_a_breaking_change_against_the_target_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    contract = tmp_path / "spec" / "openapi" / "answerlineage-v1.json"
    contract.parent.mkdir(parents=True)
    git(tmp_path, "init", "--quiet", "--initial-branch", "main")
    (tmp_path / "README.md").write_text("projet\n", "utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "--quiet", "--no-gpg-sign", "-m", "base")
    without_contract = git(tmp_path, "rev-parse", "HEAD")
    contract.write_text(json.dumps(PUBLISHED), "utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "--quiet", "--no-gpg-sign", "-m", "contrat")
    base = git(tmp_path, "rev-parse", "HEAD")
    monkeypatch.chdir(tmp_path)
    relative = Path("spec/openapi/answerlineage-v1.json")

    assert main({"BASE_SHA": without_contract}, relative) == 0
    assert "rien à comparer" in capsys.readouterr().out
    assert main({"BASE_SHA": base}, relative) == 0

    contract.write_text(json.dumps(changed(lambda d: d["paths"].pop(SOURCES))), "utf-8")
    assert main({"BASE_SHA": base}, relative) == 1
    output = capsys.readouterr().out
    assert "la route est retirée" in output
    assert "nouvelle version de l'API" in output

    assert main({"BASE_SHA": "--upload-pack=x"}, relative) == 2
    assert main({}, relative) == 0
