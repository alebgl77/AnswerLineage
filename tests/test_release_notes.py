"""Les notes de version se tirent des commits conventionnels, sans référence interne."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import release_notes
from scripts.check_release import Version, declared_version, parse_tag
from scripts.release_notes import Change, clean, main, notes, parse, previous_version, render

TRAILERS = "Co-Authored-By: Quelqu'un <quelquun@exemple.org>\nRefs: #12\n"


# Un message


def test_a_conventional_subject_gives_its_type_scope_and_description() -> None:
    message = (
        "feat(api): liste des jeux de données\n\nLe corps n'entre pas dans les notes.\n\n"
        + TRAILERS
    )
    assert parse(message) == Change("feat", "api", "liste des jeux de données")


def test_the_type_is_read_whatever_its_case_and_the_scope_is_optional() -> None:
    assert parse("Fix: une correction") == Change("fix", "", "une correction")


def test_an_exclamation_mark_flags_a_breaking_change() -> None:
    assert parse("feat(api)!: la route renvoie une liste") == Change(
        "feat", "api", "la route renvoie une liste", "la route renvoie une liste"
    )


def test_a_breaking_change_footer_gives_its_text_up_to_the_next_footer() -> None:
    message = (
        "refactor(api): schéma des événements\n\n"
        "Le corps.\n\n"
        "BREAKING CHANGE: le champ answer devient answers,\n"
        "une liste.\n" + TRAILERS
    )
    change = parse(message)
    assert change == Change(
        "refactor", "api", "schéma des événements", "le champ answer devient answers, une liste."
    )


def test_the_hyphenated_footer_is_a_breaking_change_too() -> None:
    change = parse("fix: x\n\nBREAKING-CHANGE: plus de y")
    assert change is not None
    assert change.breaking == "plus de y"


def test_a_subject_that_is_not_conventional_is_kept_without_a_type() -> None:
    assert parse("Mise à jour du guide") == Change("", "", "Mise à jour du guide")


@pytest.mark.parametrize("message", ["", "\n\n", "feat: (ABC-1)"])
def test_a_message_without_description_is_left_out(message: str) -> None:
    assert parse(message) is None


@pytest.mark.parametrize(
    ("subject", "description"),
    [
        ("feat(ci): pipeline d'intégration continue (ABC-101)", "pipeline d'intégration continue"),
        ("fix(ci): nettoyage du canari (ABC-1, DEF-23)", "nettoyage du canari"),
        ("fix: une faille (CVE-2026-1234) corrigée", "une faille (CVE-2026-1234) corrigée"),
        ("fix: une faiblesse (CWE-79)", "une faiblesse (CWE-79)"),
        ("docs: empreintes SHA-256 et UTF-8", "empreintes SHA-256 et UTF-8"),
    ],
)
def test_ticket_keys_are_removed_and_public_identifiers_kept(
    subject: str, description: str
) -> None:
    change = parse(subject)
    assert change is not None
    assert change.description == description


def test_a_scope_that_is_a_ticket_key_is_removed() -> None:
    assert parse("feat(ABC-12): une nouveauté") == Change("feat", "", "une nouveauté")


def test_a_breaking_change_loses_its_ticket_keys_too() -> None:
    change = parse("feat!: x\n\nBREAKING CHANGE: plus de y (ABC-7)")
    assert change is not None
    assert change.breaking == "plus de y"


@pytest.mark.parametrize(
    "message",
    [
        "docs: CLAUDE.md tiré du PRD",
        "docs: aligne le guide sur le backlog",
        "chore: suit le cahier des charges",
        "feat!: x\n\nBREAKING CHANGE: voir le Backlog",
    ],
)
def test_a_message_that_refers_to_the_private_planning_documents_is_left_out(message: str) -> None:
    assert parse(message) is None


def test_invisible_characters_are_removed_and_a_long_entry_is_bounded() -> None:
    hidden = "une\N{ZERO WIDTH SPACE} nouveauté\N{RIGHT-TO-LEFT OVERRIDE}\x07 cachée"
    assert clean(hidden) == "une nouveauté cachée"
    long = clean("x" * 400)
    assert len(long) == 300
    assert long.endswith("…")


# Les notes


def test_the_notes_group_by_type_with_breaking_changes_first() -> None:
    changes = [
        Change("fix", "proxy", "une correction"),
        Change("feat", "api", "une nouveauté", "une rupture"),
        Change("docs", "", "un guide"),
        Change("wip", "", "un essai"),
        Change("", "", "Mise à jour"),
    ]
    assert render(changes, parse_tag("v1.2.2")) == (
        "Changements depuis v1.2.2.\n"
        "\n## Ruptures\n\n- **api** : une rupture\n"
        "\n## Nouveautés\n\n- **api** : une nouveauté\n"
        "\n## Corrections\n\n- **proxy** : une correction\n"
        "\n## Documentation\n\n- un guide\n"
        "\n## Autres changements\n\n- Mise à jour\n- un essai\n"
    )


def test_entries_are_sorted_scope_first_and_duplicates_dropped() -> None:
    changes = [
        Change("feat", "", "b"),
        Change("feat", "workers", "a"),
        Change("feat", "api", "c"),
        Change("feat", "api", "c"),
        Change("feat", "", "a"),
    ]
    assert render(changes, None) == (
        "Première version.\n\n## Nouveautés\n\n- **api** : c\n- **workers** : a\n- a\n- b\n"
    )


def test_the_notes_do_not_depend_on_the_order_of_the_commits() -> None:
    changes = [
        Change(kind, scope, f"{kind}-{scope}-{number}")
        for kind in ("feat", "fix", "ci")
        for scope in ("", "api")
        for number in range(3)
    ]
    expected = render(changes, None)
    assert render(changes[::-1], None) == expected
    assert render(changes[7:] + changes[:7], None) == expected
    assert render(changes[::2] + changes[1::2], None) == expected


def test_a_version_without_change_says_so() -> None:
    assert render([], parse_tag("v1.2.2")) == "Changements depuis v1.2.2.\n\nAucun changement.\n"


# L'étiquette précédente

TAGS = ["v1.1.0", "v1.2.0", "v1.3.0-rc.1", "v1.3.0-rc.2", "v1.3.0", "v2.0.0", "outil-v9", "v1.2"]


@pytest.mark.parametrize(
    ("current", "previous"),
    [
        ("v1.3.0", "v1.2.0"),
        ("v1.3.0-rc.2", "v1.3.0-rc.1"),
        ("v1.3.0-rc.1", "v1.2.0"),
        ("v1.3.1", "v1.3.0"),
        ("v1.1.0", None),
        ("v1.0.0", None),
    ],
)
def test_the_previous_tag_is_the_highest_earlier_version_and_skips_prereleases_for_a_final(
    current: str, previous: str | None
) -> None:
    version = parse_tag(current)
    assert version is not None
    found = previous_version(version, TAGS)
    assert (found.tag if found else None) == previous


# Sur un historique Git


def git(directory: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=directory, check=True, capture_output=True)  # noqa: S603, S607


def commit(directory: Path, message: str) -> None:
    git(directory, "commit", "--allow-empty", "--quiet", "--message", message)


@pytest.fixture
def history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Un dépôt : v1.0.0, une préversion, une branche fusionnée, puis v1.1.0."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for variable in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(variable, "Exemple")
    for variable in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(variable, "exemple@exemple.org")
    directory = tmp_path / "depot"
    directory.mkdir()
    git(directory, "init", "--quiet", "--initial-branch", "principale")
    commit(directory, "feat: première nouveauté")
    git(directory, "tag", "v1.0.0")
    commit(directory, "fix(api): avant la préversion\n\n" + TRAILERS)
    git(directory, "tag", "--annotate", "--message", "préversion", "v1.1.0-rc.1")
    git(directory, "checkout", "--quiet", "-b", "travail/branche-secrete")
    commit(directory, "feat(proxy)!: sur la branche (ABC-42)")
    git(directory, "checkout", "--quiet", "principale")
    commit(directory, "docs: sur la branche principale")
    merge = "Merge branch 'travail/branche-secrete'"
    git(directory, "merge", "--no-ff", "--quiet", "--message", merge, "travail/branche-secrete")
    git(directory, "tag", "v1.1.0")
    commit(directory, "ci: après la version")
    monkeypatch.chdir(directory)
    return directory


def test_the_notes_of_a_version_cover_the_commits_since_the_previous_final_version(
    history: Path,
) -> None:
    version = parse_tag("v1.1.0")
    assert version is not None
    assert notes(version, "v1.1.0") == (
        "Changements depuis v1.0.0.\n"
        "\n## Ruptures\n\n- **proxy** : sur la branche\n"
        "\n## Nouveautés\n\n- **proxy** : sur la branche\n"
        "\n## Corrections\n\n- **api** : avant la préversion\n"
        "\n## Documentation\n\n- sur la branche principale\n"
    )


def test_the_notes_never_name_a_branch_nor_a_footer(history: Path) -> None:
    text = notes(Version(1, 1, 0), "v1.1.0")
    for hidden in (
        "branche-secrete",
        "travail/",
        "Merge",
        "Co-Authored-By",
        "Refs",
        "ABC-42",
        "exemple.org",
    ):
        assert hidden not in text


def test_the_first_version_covers_the_whole_history(history: Path) -> None:
    assert (
        notes(Version(1, 0, 0), "v1.0.0")
        == "Première version.\n\n## Nouveautés\n\n- première nouveauté\n"
    )


def test_the_command_writes_the_notes_of_a_tag(
    history: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["v1.1.0-rc.1"]) == 0
    assert capsys.readouterr().out == (
        "Changements depuis v1.0.0.\n\n## Corrections\n\n- **api** : avant la préversion\n"
    )


def test_the_command_defaults_to_the_declared_version_on_head(
    history: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([]) == 0
    previous = previous_version(declared_version(), ["v1.0.0", "v1.1.0-rc.1", "v1.1.0"])
    first = "Première version." if previous is None else f"Changements depuis {previous.tag}."
    assert capsys.readouterr().out.splitlines()[0] == first


@pytest.mark.parametrize(
    "arguments",
    [["v1.2"], ["v1.1.0", "--to=--output=/tmp/x"], ["v1.1.0", "--to", "HEAD~1"]],
)
def test_the_command_refuses_a_malformed_tag_or_revision(
    history: Path, arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(arguments) == 2
    assert "refusée" in capsys.readouterr().err


def test_the_command_says_when_the_history_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    assert main(["v1.1.0"]) == 2
    assert "Historique illisible" in capsys.readouterr().err


def test_the_command_says_when_no_version_is_declared(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def disagree() -> Version:
        raise ValueError("les composants publiables déclarent plusieurs versions")

    monkeypatch.setattr(release_notes, "declared_version", disagree)
    assert main([]) == 2
    assert "plusieurs versions" in capsys.readouterr().err
