"""Codes de sortie de ``answerlineage``, communs aux sous-commandes.

Un script distingue ainsi « rien n'est touché » (1) de « je ne sais pas » (3),
et une erreur d'usage (2) d'une API injoignable (4) ou fautive (5).
"""

from __future__ import annotations

from enum import IntEnum


class Exit(IntEnum):
    OK = 0
    # impact : aucune réponse touchée, et le résultat est complet.
    NOTHING_TOUCHED = 1
    # doctor : au moins une anomalie.
    ANOMALY = 1
    # demo : Docker absent, ou docker compose en échec.
    FAILED = 1
    USAGE = 2
    # impact : résultat tronqué, ou couverture du lignage non mesurée, partielle ou trouée.
    INCOMPLETE = 3
    UNREACHABLE = 4
    # L'API a refusé la requête, ou rendu une réponse invalide.
    API_ERROR = 5
