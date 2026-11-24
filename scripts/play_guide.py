"""Joue les commandes d'un guide Markdown, dans l'ordre, et chronomètre le premier lignage.

Un guide de démarrage qui n'est pas rejoué cesse d'être vrai sans que personne le
voie. Ce script en extrait les blocs ``bash`` et les joue : une commande qui
échoue arrête le guide et se nomme, par son fichier et sa ligne.

Tout bloc ``bash`` est joué. Un bloc qui ne doit pas l'être, parce qu'un autre
contrôle le rejoue ou parce qu'il suppose un poste que le pipeline n'est pas, se
marque d'un commentaire sur la ligne qui précède sa clôture, avec sa raison ::

    <!-- guide : non joué, le pipeline est déjà dans un clone -->

Les blocs d'une autre langue (``text`` pour une sortie, ``json``…) ne sont jamais
joués. Le commentaire suivant marque le bloc dont la réussite montre le premier
lignage ; le temps écoulé depuis le départ est alors comparé au budget que donne
``--budget`` ::

    <!-- guide : premier lignage -->

Chaque bloc est un script ``bash`` à lui seul, lancé depuis la racine du dépôt :
aucun état ne passe d'un bloc au suivant, ce qui est aussi vrai d'un lecteur qui
copie les blocs un à un. Une commande qui dure au-delà du budget est tuée, avec
son groupe de processus.

Usage : ``python -m scripts.play_guide docs/demarrage.md [--budget SECONDES]``

Bibliothèque standard seule.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_FENCE = re.compile(r"^(?P<indent> *)(?P<fence>`{3,}|~{3,}) *(?P<info>[^`]*?) *$")
_MARKER = re.compile(r"^\s*<!--\s*guide\s*:\s*(?P<text>.*?)\s*-->\s*$")
_SKIPPED = re.compile(r"non joué\s*(?:[,:;—-]\s*(?P<reason>.*))?")
_FIRST_LINEAGE = "premier lignage"
_TITLE_MAX = 120


class GuideError(Exception):
    """Le guide est mal formé, ou une de ses commandes échoue."""


@dataclass(frozen=True)
class Block:
    """Un bloc ``bash`` du guide."""

    line: int
    """Ligne de sa clôture ouvrante, à partir de 1."""
    code: str
    skip_reason: str | None
    """Pourquoi le bloc n'est pas joué ; ``None`` pour un bloc joué."""
    first_lineage: bool
    """Vrai pour le bloc dont la réussite montre le premier lignage."""

    @property
    def title(self) -> str:
        """Sa première ligne, par laquelle la commande se nomme."""
        first = next((line.strip() for line in self.code.splitlines() if line.strip()), "")
        return first if len(first) <= _TITLE_MAX else first[: _TITLE_MAX - 1] + "…"


def _marker(line: int, text: str) -> tuple[str | None, bool]:
    """La marque d'un commentaire : sa raison de ne pas être joué, ou le premier lignage."""
    if text == _FIRST_LINEAGE:
        return None, True
    skipped = _SKIPPED.fullmatch(text)
    if skipped is None:
        raise GuideError(
            f"ligne {line} : marque inconnue, « non joué, <raison> » ou « {_FIRST_LINEAGE} »"
        )
    reason = (skipped.group("reason") or "").strip()
    if not reason:
        raise GuideError(f"ligne {line} : « non joué » sans raison")
    return reason, False


def extract(text: str) -> list[Block]:
    """Les blocs ``bash`` d'un guide, dans l'ordre, avec leurs marques."""
    lines = text.splitlines()
    blocks: list[Block] = []
    marker: tuple[int, str] | None = None
    index = 0
    while index < len(lines):
        raw = lines[index]
        number = index + 1
        opening = _FENCE.match(raw)
        if opening is None:
            found = _MARKER.match(raw)
            if found is not None:
                if marker is not None:
                    raise GuideError(f"ligne {marker[0]} : la marque ne précède pas un bloc bash")
                marker = (number, found.group("text"))
            elif raw.strip() and marker is not None:
                raise GuideError(f"ligne {marker[0]} : la marque ne précède pas un bloc bash")
            index += 1
            continue

        indent = len(opening.group("indent"))
        fence = opening.group("fence")
        language = opening.group("info").split(maxsplit=1)[0] if opening.group("info") else ""
        body: list[str] = []
        index += 1
        while index < len(lines):
            closing = _FENCE.match(lines[index])
            if (
                closing is not None
                and closing.group("fence")[0] == fence[0]
                and len(closing.group("fence")) >= len(fence)
                and not closing.group("info")
            ):
                break
            line = lines[index]
            body.append(line[indent:] if not line[:indent].strip() else line.lstrip())
            index += 1
        else:
            raise GuideError(f"ligne {number} : bloc jamais fermé")
        index += 1

        if language != "bash":
            if marker is not None:
                raise GuideError(f"ligne {marker[0]} : la marque ne précède pas un bloc bash")
            continue
        reason, first = (None, False)
        if marker is not None:
            reason, first = _marker(*marker)
            marker = None
        blocks.append(
            Block(line=number, code="\n".join(body), skip_reason=reason, first_lineage=first)
        )
    if marker is not None:
        raise GuideError(f"ligne {marker[0]} : la marque ne précède pas un bloc bash")
    return blocks


def _run(code: str, directory: Path, timeout: float | None) -> int | None:
    """Joue un bloc comme un script bash ; ``None`` s'il dépasse son délai, après l'avoir tué."""
    process = subprocess.Popen(  # noqa: S603 — le code est celui du guide, que le dépôt versionne.
        ["bash", "-euo", "pipefail", "-c", code],  # noqa: S607
        cwd=directory,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        return None


def _seconds(value: float) -> str:
    return f"{value:g}"


def play(
    blocks: Sequence[Block],
    *,
    source: str,
    directory: Path,
    budget: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> list[Block]:
    """Joue les blocs non marqués, dans l'ordre ; rend ceux qui l'ont été.

    Avec un budget, le bloc du premier lignage doit réussir avant qu'il soit
    épuisé, et aucune commande ne dure plus que lui.
    """
    played = [block for block in blocks if block.skip_reason is None]
    if not played:
        raise GuideError(f"{source} : aucun bloc à jouer")
    if budget is not None:
        firsts = [block for block in played if block.first_lineage]
        if not firsts:
            raise GuideError(
                f"{source} ne marque pas son premier lignage : le budget n'a rien à mesurer"
            )
        if len(firsts) > 1:
            raise GuideError(f"{source} marque plus d'un premier lignage")

    started = clock()
    reached = False
    for block in played:
        where = f"{source}, ligne {block.line}"
        print(f"[guide] {where} : {block.title}", flush=True)
        timeout = budget
        if budget is not None and not reached:
            timeout = budget - (clock() - started)
            if timeout <= 0:
                raise GuideError(
                    f"{where} : le budget de {_seconds(budget)} s est épuisé "
                    f"avant la commande : {block.title}"
                )
        code = _run(block.code, directory, timeout)
        if code is None:
            when = "n'a pas fini dans le budget de" if not reached else "ne finit pas en"
            raise GuideError(
                f"{where} : la commande {when} {_seconds(budget or 0)} s : {block.title}"
            )
        if code != 0:
            raise GuideError(f"{where} : la commande échoue (code {code}) : {block.title}")
        if not block.first_lineage:
            continue
        reached = True
        if budget is not None:
            elapsed = clock() - started
            if elapsed > budget:
                raise GuideError(
                    f"{where} : premier lignage en {elapsed:.0f} s, "
                    f"pour {_seconds(budget)} s au plus"
                )
            print(
                f"[guide] premier lignage en {elapsed:.0f} s, pour {_seconds(budget)} s au plus.",
                flush=True,
            )
    return played


def _positive(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("le budget est un nombre de secondes positif")
    return number


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.play_guide",
        description="Joue les blocs bash d'un guide Markdown, dans l'ordre.",
    )
    parser.add_argument("guide", type=Path, help="le fichier Markdown du guide")
    parser.add_argument(
        "--budget",
        type=_positive,
        metavar="SECONDES",
        help="délai du premier lignage, et durée maximale d'une commande",
    )
    parser.add_argument(
        "--directory",
        type=Path,
        default=ROOT,
        help="dossier où se jouent les commandes (la racine du dépôt par défaut)",
    )
    arguments = parser.parse_args(argv)
    source = arguments.guide.as_posix()
    try:
        try:
            text = arguments.guide.read_text(encoding="utf-8")
        except OSError as error:
            raise GuideError(f"{source} : lecture impossible ({type(error).__name__})") from None
        blocks = extract(text)
        played = play(blocks, source=source, directory=arguments.directory, budget=arguments.budget)
    except GuideError as error:
        print(f"ÉCHEC : {error}", file=sys.stderr)
        return 1
    skipped = len(blocks) - len(played)
    print(
        f"Guide joué : {len(played)} blocs joués, {skipped} non joué{'s' if skipped > 1 else ''}.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
