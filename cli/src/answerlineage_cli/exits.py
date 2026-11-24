"""Codes de sortie de ``answerlineage``, communs aux sous-commandes.

Un script distingue ainsi « rien n'est touché » (1) de « je ne sais pas » (3),
et une erreur d'usage (2) d'une API injoignable (4) ou fautive (5). Une analyse
d'incident pas encore faite (6) se distingue d'une analyse en échec (7).
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
    # dead-letters list : des événements attendent au rebut.
    WAITING = 1
    # dead-letters replay : aucun événement à rejouer sous ces filtres.
    NOTHING_TO_REPLAY = 1
    USAGE = 2
    # impact : résultat tronqué, ou couverture du lignage non mesurée, partielle ou trouée.
    INCOMPLETE = 3
    # dead-letters replay : la borne est atteinte, d'autres événements restent au rebut.
    REMAINING = 3
    UNREACHABLE = 4
    # L'API a refusé la requête, ou rendu une réponse invalide.
    API_ERROR = 5
    # incident show : l'analyse attend un worker ; relancer plus tard.
    PENDING = 6
    # incident show : l'analyse est en échec après plusieurs essais.
    ANALYSIS_FAILED = 7
