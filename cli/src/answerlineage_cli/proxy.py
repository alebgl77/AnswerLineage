"""``answerlineage proxy`` : le proxy MCP, par la commande commune.

La sous-commande cède la place : le processus devient le proxy MCP installé
dans le même environnement (``python -m answerlineage_proxy.cli``), avec les
mêmes arguments. Sans ``--``, le proxy se place devant un serveur MCP en HTTP ;
avec, il lance la commande qui suit en stdio.

Rien ne s'intercale entre le client MCP et le proxy : entrée, sorties, signaux
et code de sortie sont ceux du proxy, ce qu'exige le mode stdio. La commande ne
dépend pas du proxy ; quand il n'est pas installé, elle le dit.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Callable, Sequence
from typing import Final

from answerlineage_cli.exits import Exit

PROXY_PACKAGE: Final = "answerlineage_proxy"
PROXY_MODULE: Final = "answerlineage_proxy.cli"
NOT_INSTALLED: Final = (
    "Le proxy MCP n'est pas installé avec cette commande : installer le paquet "
    "answerlineage-proxy dans le même environnement."
)

# Remplace le processus : ne rend jamais la main, sauf remplacé par un test.
_execute: Callable[[str, list[str]], object] = os.execv


def installed() -> bool:
    """Le proxy est-il importable par l'interpréteur de la commande ?"""
    return importlib.util.find_spec(PROXY_PACKAGE) is not None


def delegate(arguments: Sequence[str], report: Callable[[str], None]) -> int:
    """Devient le proxy, avec ``arguments`` ; rend 2 si le proxy n'est pas installé."""
    if not installed():
        report(NOT_INSTALLED)
        return Exit.USAGE
    sys.stdout.flush()
    sys.stderr.flush()
    _execute(sys.executable, [sys.executable, "-m", PROXY_MODULE, *arguments])
    return Exit.OK
