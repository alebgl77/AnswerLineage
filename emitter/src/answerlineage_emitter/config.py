"""Erreur de réglage commune aux émetteurs.

Un réglage absent ou invalide arrête l'émetteur au démarrage, avec un message
qui nomme le réglage et jamais sa valeur : une clé ou un secret peut avoir été
donné par erreur à la place d'un autre réglage.
"""

from __future__ import annotations


class ConfigurationError(ValueError):
    """Un réglage d'un émetteur est absent ou invalide."""
