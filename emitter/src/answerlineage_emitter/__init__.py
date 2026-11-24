"""Émetteur d'AnswerLineage : ce que partagent le proxy MCP et les adaptateurs d'agents.

Décrire un appel d'outil sans en garder le contenu (empreintes à clé de
locataire), en tirer l'identifiant de run du contexte de trace W3C, construire
l'événement OpenLineage et ses facettes, puis le remettre à l'API d'ingestion
par une boîte d'envoi bornée, hors du chemin des appels, avec le jeton du client.
"""

__version__ = "0.0.0"
