"""Listes de rappel : un message par destinataire et par incident, les plus exposés d'abord.

Les workers font la liste d'un incident de son analyse d'impact
(``answerlineage_workers.recall``) ; l'API la lit.

- ``models.py`` : ce que rend la route ;
- ``store.py`` : la lecture d'une liste, page par page, sous les bornes des lectures ;
- ``routes.py`` : lire la liste de rappel d'un incident.

Des pseudonymes, des identifiants et des nombres : aucune identité, aucun texte libre.
"""
