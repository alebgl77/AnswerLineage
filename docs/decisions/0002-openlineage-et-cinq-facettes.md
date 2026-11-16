# 0002. OpenLineage comme contrat d'événements, et cinq facettes propres seulement

Statut : en vigueur.

## Contexte

Chaque émetteur (un proxy MCP, un adaptateur d'agent, un serveur d'envoi de messages) doit décrire ce qu'il a vu dans une forme que l'API sait recevoir, et que d'autres outils savent lire. Les entreprises ont déjà des catalogues de données et des outils de lignage : un format de plus qu'ils ne comprennent pas les isolerait.

Le besoin propre à AnswerLineage dépasse ce que décrit un lignage de données ordinaire : l'appel d'outil, l'agent, la réponse, sa diffusion, la lecture d'une clé.

## Décision

Le contrat est le `RunEvent` d'OpenLineage 1.55, schéma 2-0-2, copié dans `spec/openlineage/`. Une requête d'agent est un run, un appel d'outil un run enfant (facette standard `parent`), les données lues sont les `inputs`, les colonnes et la version passent par les facettes standard `schema` et `version`, la requête SQL sans littéraux par la facette de job standard `sql`.

Ce qu'OpenLineage ne dit pas tient en cinq facettes propres, chacune avec un schéma JSON versionné dans `spec/facets/` ([Facettes](../facettes.md)) :

| Facette | Porte |
| --- | --- |
| `answerlineage_agent` | Le run d'un agent |
| `answerlineage_toolCall` | Un appel d'outil MCP : serveur, outil, transport, issue, contexte de trace, empreintes à clé |
| `answerlineage_qos` | Latence, latence ajoutée, issue, reprises |
| `answerlineage_delivery` | La diffusion d'une réponse : empreinte, canal, référence du message, destinataires pseudonymisés |
| `answerlineage_keys` | Les clés lues dans un jeu de données, jamais en clair |

Tout le reste passe par le standard. L'API valide les événements contre le schéma officiel (un test compare ses modèles au schéma, événement par événement) ; `tests/test_contract_proxy_ingestion.py` vérifie que le proxy et l'API s'accordent avec lui.

## Alternative écartée

**Un format d'événement propre à AnswerLineage.**

Un format propre se serait décrit exactement comme le produit en a besoin, sans se plier au vocabulaire d'OpenLineage. Mais chaque émetteur existant, et chaque catalogue qui lit déjà OpenLineage, aurait été à adapter ; un événement d'un autre émetteur OpenLineage n'aurait pas pu entrer. Avec le standard, un émetteur conforme se branche à `POST /api/v1/lineage` sans adaptation, et un événement sans nos facettes est enregistré comme un job ordinaire, avec ses lectures et ses écritures.

Les facettes sont le mécanisme d'extension d'OpenLineage : y mettre ce qui est propre au produit garde l'enveloppe standard. Elles sont limitées à ce qu'aucune facette standard ne porte : un champ de plus se discute contre ce que le standard offre déjà.

## Conséquences

- Les schémas des facettes sont publiés et immuables : un fichier de version publié ne change plus d'un octet, une évolution passe par une nouvelle version qui ne retire rien et ne rend rien obligatoire. `make contract` (`scripts/check_facets_compat.py`) le fait respecter, comme il fait respecter la compatibilité de l'API publiée.
- On écrit un émetteur à partir des schémas, sans lire le code d'AnswerLineage ; le [kit de conformité](../conformite.md) le juge.
- Le produit hérite des limites du contrat : les sous-ensembles et les clés d'une lecture, que le [corpus de vérité](../corpus.md#corpus-de-vérité) sait décrire, n'ont pas encore de forme dans les événements, et la propagation reste au niveau table.
- Les facettes ne ferment pas leurs objets (comme dans OpenLineage) : un champ inconnu est admis, et le contrôle de compatibilité ne compare ni les bornes ni les valeurs admises ([limites](../facettes.md#limites-connues)).
- Un événement d'un autre émetteur, sans `answerlineage_agent` ni `answerlineage_delivery`, nourrit le graphe de lectures mais ne produit ni réponse ni destinataire : le lignage jusqu'aux personnes demande un émetteur qui connaît la diffusion.
