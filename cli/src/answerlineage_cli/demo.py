"""``answerlineage demo`` : lance la démonstration d'un clone du dépôt et attend qu'elle réponde.

La démonstration est celle de ``compose.yaml``, lancée comme le pipeline la
lance (``scripts/check_demo.sh``) : ``docker compose up``, depuis la racine
du clone. La commande n'en décrit aucun service : il n'y a qu'un chemin de
démonstration à maintenir. Elle attend ensuite que l'API réponde, que son
schéma soit migré et que l'explorateur soit servi, puis en donne l'adresse.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

from answerlineage_cli import doctor
from answerlineage_cli.client import Api, InvalidResponseError, UnreachableError
from answerlineage_cli.exits import Exit

REPOSITORY: Final = "https://github.com/alebgl77/AnswerLineage.git"
COMPOSE_FILE: Final = "compose.yaml"
# Le fichier compose du dépôt tient en quelques kilo-octets.
MAX_COMPOSE_BYTES: Final = 256 * 1024
DEFAULT_WAIT: Final = 120
MAX_WAIT: Final = 3600
POLL_SECONDS = 1.0

# Le nom du projet compose, à la racine du fichier : c'est lui qui désigne la démonstration.
_PROJECT = re.compile(r"^name:\s*answerlineage\s*$", re.MULTILINE)


class DemoError(Exception):
    """La démonstration ne peut pas être lancée ; le message dit quoi faire."""

    def __init__(self, code: Exit, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _is_clone(directory: Path) -> bool:
    compose = directory / COMPOSE_FILE
    if not compose.is_file() or not (directory / "deploy" / "Dockerfile").is_file():
        return False
    with compose.open("rb") as handle:
        content = handle.read(MAX_COMPOSE_BYTES + 1)
    if len(content) > MAX_COMPOSE_BYTES:
        return False
    return _PROJECT.search(content.decode("utf-8", errors="replace")) is not None


def find_clone(start: Path) -> Path | None:
    """La racine du clone qui contient ``start``, ou ``None`` hors d'un clone."""
    for directory in (start, *start.parents):
        if _is_clone(directory):
            return directory
    return None


def _compose_up(root: Path) -> None:
    docker = shutil.which("docker")
    if docker is None:
        raise DemoError(
            Exit.FAILED,
            "Docker est requis pour la démonstration : l'installer (https://docs.docker.com/"
            "get-started/get-docker/), puis relancer answerlineage demo.",
        )
    # Une commande fixe, sans interpréteur de commandes : rien de l'utilisateur n'y entre.
    completed = subprocess.run(  # noqa: S603
        [docker, "compose", "up", "--build", "--detach"], cwd=root, check=False
    )
    if completed.returncode != 0:
        raise DemoError(
            Exit.FAILED,
            f"docker compose up a échoué (code {completed.returncode}) : lire sa sortie ci-dessus, "
            f"et le journal des services avec docker compose logs, depuis {root}.",
        )


def _ready(api: Api) -> bool:
    """Vrai quand l'API répond, lit sa base migrée, et sert l'explorateur."""
    try:
        received = api.get(doctor.HEALTH, max_bytes=doctor.MAX_HEALTH_BYTES)
        if received.status != 200:
            return False
        health = doctor.parse_health(received.json())
        if health.migrations != "current":
            return False
        page = api.get("/", max_bytes=doctor.MAX_PAGE_BYTES, headers={"accept": "text/html"})
    except (UnreachableError, InvalidResponseError):
        return False
    return page.status == 200 and b'id="root"' in page.body


def start(
    api: Api,
    directory: Path,
    wait: int,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> Path:
    """Lance la démonstration du clone qui contient ``directory`` et attend qu'elle réponde."""
    root = find_clone(directory.resolve())
    if root is None:
        raise DemoError(
            Exit.USAGE,
            "Ce dossier n'est pas dans un clone d'AnswerLineage : la démonstration se lance "
            f"depuis le dépôt. git clone {REPOSITORY} && cd AnswerLineage && answerlineage demo",
        )
    _compose_up(root)
    deadline = clock() + wait
    while not _ready(api):
        if clock() >= deadline:
            raise DemoError(
                Exit.UNREACHABLE,
                f"La démonstration ne répond pas à {api.base} après {wait} s. Son journal : "
                f"docker compose logs api, depuis {root}.",
            )
        time.sleep(POLL_SECONDS)
    return root


def ready_message(base: str, root: Path) -> str:
    return (
        "La démonstration répond.\n"
        f"Explorateur : {base}/\n"
        "Vérifier l'installation : answerlineage doctor\n"
        f"Arrêter : docker compose down, depuis {root} ; avec --volumes, ses données aussi.\n"
    )
