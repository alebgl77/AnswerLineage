"""Le guide de démarrage se joue : ses commandes sont extraites du fichier, pas recopiées."""

from __future__ import annotations

import time
from pathlib import Path
from textwrap import dedent

import pytest

from scripts.play_guide import Block, GuideError, extract, main, play

FENCE = "```"


def guide(*parts: str) -> str:
    """Un guide fait de ses parties, séparées par une ligne vide."""
    return "\n\n".join(dedent(part).strip("\n") for part in parts) + "\n"


def bash(code: str, marker: str | None = None) -> str:
    comment = f"<!-- guide : {marker} -->\n" if marker is not None else ""
    return f"{comment}{FENCE}bash\n{dedent(code).strip(chr(10))}\n{FENCE}"


def run(
    text: str,
    directory: Path,
    *,
    budget: float | None = None,
    clock: list[float] | None = None,
) -> list[Block]:
    ticks = iter(clock) if clock is not None else None
    return play(
        extract(text),
        source="guide.md",
        directory=directory,
        budget=budget,
        clock=(lambda: next(ticks)) if ticks is not None else time.monotonic,
    )


# Extraction


def test_every_bash_block_is_extracted_in_order() -> None:
    text = guide(
        "# Titre",
        bash("echo un"),
        "Du texte.",
        bash("echo deux\necho trois"),
    )
    blocks = extract(text)
    assert [block.code for block in blocks] == ["echo un", "echo deux\necho trois"]
    assert [block.line for block in blocks] == [3, 9]
    assert [block.skip_reason for block in blocks] == [None, None]


def test_a_block_of_another_language_is_not_extracted() -> None:
    text = guide(
        f"{FENCE}text\nSortie : 16 réponses touchées\n{FENCE}",
        f"{FENCE}json\n{{}}\n{FENCE}",
        f"{FENCE}\nsans langage\n{FENCE}",
        bash("echo seul"),
    )
    assert [block.code for block in extract(text)] == ["echo seul"]


def test_what_a_block_of_another_language_quotes_is_not_a_block() -> None:
    quoted = f"{FENCE}text\n{FENCE}bash\n{FENCE}"
    assert extract(quoted + "\n") == []


def test_a_block_in_a_list_is_extracted_without_its_indentation() -> None:
    text = "1. Lancer :\n\n   ```bash\n   echo un\n   echo deux\n   ```\n"
    assert [block.code for block in extract(text)] == ["echo un\necho deux"]


def test_a_block_fenced_by_tildes_is_extracted() -> None:
    assert [block.code for block in extract("~~~bash\necho un\n~~~\n")] == ["echo un"]


def test_a_block_that_never_closes_is_refused() -> None:
    with pytest.raises(GuideError, match=r"ligne 1.*jamais fermé"):
        extract(f"{FENCE}bash\necho un\n")


def test_a_marked_block_is_extracted_with_its_reason() -> None:
    text = guide(
        bash(
            "git clone https://exemple.org/depot.git",
            "non joué, le pipeline est déjà dans un clone",
        )
    )
    (block,) = extract(text)
    assert block.skip_reason == "le pipeline est déjà dans un clone"
    assert block.first_lineage is False


def test_the_first_lineage_marker_is_read() -> None:
    (block,) = extract(guide(bash("echo prêt", "premier lignage")))
    assert block.first_lineage is True
    assert block.skip_reason is None


@pytest.mark.parametrize(
    ("marker", "message"),
    [
        ("non joué", "sans raison"),
        ("non joué, ", "sans raison"),
        ("non joue, faute de frappe", "marque inconnue"),
        ("premier lignage, de plus", "marque inconnue"),
        ("", "marque inconnue"),
    ],
)
def test_a_marker_that_is_not_understood_is_refused(marker: str, message: str) -> None:
    with pytest.raises(GuideError, match=message):
        extract(guide(bash("echo un", marker)))


def test_a_marker_before_something_other_than_a_bash_block_is_refused() -> None:
    text = "<!-- guide : non joué, exemple -->\n\n" + f"{FENCE}text\nsortie\n{FENCE}\n"
    with pytest.raises(GuideError, match=r"ligne 1.*ne précède pas un bloc bash"):
        extract(text)


def test_a_marker_alone_at_the_end_is_refused() -> None:
    with pytest.raises(GuideError, match=r"ligne 5.*ne précède pas un bloc bash"):
        extract(guide(bash("echo un"), "<!-- guide : premier lignage -->"))


def test_two_markers_in_a_row_are_refused() -> None:
    text = "<!-- guide : premier lignage -->\n<!-- guide : non joué, deux -->\n"
    text += f"{FENCE}bash\necho un\n{FENCE}\n"
    with pytest.raises(GuideError, match=r"ligne 1.*ne précède pas un bloc bash"):
        extract(text)


def test_a_comment_that_is_not_a_marker_is_left_alone() -> None:
    text = "<!-- une note -->\n\n" + bash("echo un") + "\n"
    assert [block.code for block in extract(text)] == ["echo un"]


# Jeu


def test_an_added_block_is_played(tmp_path: Path) -> None:
    text = guide(bash("touch un"), bash("touch deux"))
    played = run(text, tmp_path)
    assert (tmp_path / "un").exists()
    assert (tmp_path / "deux").exists()
    assert [block.code for block in played] == ["touch un", "touch deux"]


def test_a_marked_block_is_not_played(tmp_path: Path) -> None:
    text = guide(bash("touch un"), bash("touch jamais", "non joué, exemple"), bash("touch trois"))
    played = run(text, tmp_path)
    assert (tmp_path / "un").exists()
    assert not (tmp_path / "jamais").exists()
    assert (tmp_path / "trois").exists()
    assert [block.code for block in played] == ["touch un", "touch trois"]


def test_blocks_are_played_in_the_directory_asked_and_one_after_the_other(tmp_path: Path) -> None:
    text = guide(bash("echo 1 >> journal"), bash("echo 2 >> journal"))
    run(text, tmp_path)
    assert (tmp_path / "journal").read_text() == "1\n2\n"


def test_a_failing_command_is_named_and_stops_the_guide(tmp_path: Path) -> None:
    text = guide(bash("touch un"), bash("exit 3"), bash("touch jamais"))
    with pytest.raises(GuideError) as raised:
        run(text, tmp_path)
    message = str(raised.value)
    assert "guide.md, ligne 5" in message
    assert "code 3" in message
    assert "exit 3" in message
    assert (tmp_path / "un").exists()
    assert not (tmp_path / "jamais").exists()


def test_the_command_named_is_the_first_line_of_a_long_block(tmp_path: Path) -> None:
    text = guide(bash("echo ouverture\nfalse"))
    with pytest.raises(GuideError, match="echo ouverture"):
        run(text, tmp_path)


def test_a_command_in_the_middle_of_a_block_that_fails_fails_the_block(tmp_path: Path) -> None:
    with pytest.raises(GuideError, match="code 1"):
        run(guide(bash("false\ntrue")), tmp_path)


def test_a_failure_inside_a_pipe_fails_the_block(tmp_path: Path) -> None:
    with pytest.raises(GuideError, match="code 1"):
        run(guide(bash("false | cat")), tmp_path)


def test_a_guide_without_a_block_to_play_is_refused(tmp_path: Path) -> None:
    text = guide(bash("touch jamais", "non joué, exemple"), "Du texte.")
    with pytest.raises(GuideError, match="aucun bloc"):
        run(text, tmp_path)


# Budget


def test_a_guide_that_marks_no_first_lineage_cannot_be_timed(tmp_path: Path) -> None:
    with pytest.raises(GuideError, match="ne marque pas son premier lignage"):
        run(guide(bash("touch un")), tmp_path, budget=300)
    assert not (tmp_path / "un").exists()


def test_a_guide_that_marks_two_first_lineages_cannot_be_timed(tmp_path: Path) -> None:
    text = guide(bash("true", "premier lignage"), bash("true", "premier lignage"))
    with pytest.raises(GuideError, match="plus d'un premier lignage"):
        run(text, tmp_path, budget=300)


def test_the_first_lineage_inside_the_budget_passes(tmp_path: Path) -> None:
    text = guide(bash("touch un"), bash("touch deux", "premier lignage"), bash("touch trois"))
    # Départ, avant chacun des deux premiers blocs, puis à la fin du premier lignage.
    played = run(text, tmp_path, budget=300, clock=[0.0, 10.0, 20.0, 130.0])
    assert len(played) == 3
    assert (tmp_path / "trois").exists()


def test_a_marked_guide_plays_without_a_budget_too(tmp_path: Path) -> None:
    played = run(guide(bash("touch un", "premier lignage")), tmp_path)
    assert [block.first_lineage for block in played] == [True]
    assert (tmp_path / "un").exists()


def test_a_first_lineage_after_the_budget_is_named(tmp_path: Path) -> None:
    text = guide(bash("touch un", "premier lignage"), bash("touch jamais"))
    with pytest.raises(GuideError) as raised:
        run(text, tmp_path, budget=300, clock=[0.0, 0.0, 301.0])
    message = str(raised.value)
    assert "premier lignage en 301 s, pour 300 s au plus" in message
    assert "guide.md, ligne 2" in message
    assert not (tmp_path / "jamais").exists()


def test_a_command_still_running_when_the_budget_ends_is_killed_and_named(tmp_path: Path) -> None:
    text = guide(bash("sleep 60", "premier lignage"))
    with pytest.raises(GuideError, match=r"pas fini dans le budget de 1 s.*sleep 60"):
        run(text, tmp_path, budget=1)


def test_a_command_after_the_first_lineage_has_the_same_time_to_finish(tmp_path: Path) -> None:
    text = guide(bash("true", "premier lignage"), bash("sleep 60"))
    with pytest.raises(GuideError, match=r"ne finit pas en 1 s.*sleep 60"):
        run(text, tmp_path, budget=1)


def test_a_budget_already_spent_stops_before_the_next_command(tmp_path: Path) -> None:
    text = guide(bash("touch un"), bash("touch jamais", "premier lignage"))
    with pytest.raises(GuideError, match=r"budget de 5 s.*épuisé"):
        run(text, tmp_path, budget=5, clock=[0.0, 1.0, 6.0])
    assert not (tmp_path / "jamais").exists()


# Ligne de commande


def test_the_command_line_plays_a_guide_and_says_what_it_did(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "guide.md"
    path.write_text(
        guide(
            bash("echo bonjour"),
            bash("echo non", "non joué, exemple"),
            bash("true", "premier lignage"),
        ),
        encoding="utf-8",
    )
    assert main([str(path), "--directory", str(tmp_path), "--budget", "300"]) == 0
    out = capsys.readouterr().out
    assert "bonjour" in out
    assert "2 blocs joués, 1 non joué" in out
    assert "premier lignage en " in out


def test_the_command_line_fails_with_the_command_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "guide.md"
    path.write_text(guide(bash("exit 4")), encoding="utf-8")
    assert main([str(path), "--directory", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "ÉCHEC" in err
    assert "guide.md, ligne 1" in err
    assert "exit 4" in err


def test_the_command_line_refuses_a_guide_it_cannot_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([str(tmp_path / "absent.md")]) == 1
    assert "ÉCHEC" in capsys.readouterr().err


def test_the_command_line_refuses_a_budget_that_is_not_positive(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as raised:
        main([str(tmp_path / "guide.md"), "--budget", "0"])
    assert raised.value.code == 2
