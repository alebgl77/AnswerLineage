# Workers

Un worker lit la file de traitement et projette chaque événement reçu dans le graphe de lignage. La file est une table de PostgreSQL : AnswerLineage n'exploite aucune autre dépendance.

```bash
ANSWERLINEAGE_DATABASE_URL=postgresql://utilisateur:motdepasse@hote/base uv run answerlineage-worker
```

Plusieurs workers peuvent tourner en même temps. Un worker s'arrête proprement sur `SIGINT` ou `SIGTERM`, à la fin du lot en cours.

## Garanties

L'API d'ingestion écrit une ligne dans la file dans la transaction qui enregistre l'événement. Le worker réclame un lot, le traite et le retire de la file dans une seule transaction :

- **aucun double traitement** : les lignes réclamées par un worker sont verrouillées, les autres workers les sautent (`FOR UPDATE SKIP LOCKED`) ;
- **aucune perte** : si un worker s'arrête en plein lot, sa transaction est annulée, les lignes redeviennent visibles et aucune écriture partielle ne reste dans le graphe ;
- **un événement fautif ne bloque pas le lot** : il est remis à plus tard, avec un délai qui double à chaque essai (5 s, 10 s, 20 s, jusqu'à une heure). La file garde le nombre d'essais et la classe de la dernière erreur, jamais son message.

Les événements bruts restent dans `event_raw` : le graphe peut être reconstruit en les rejouant.

## Ce que la projection lit

| Dans l'événement | Dans le graphe |
| --- | --- |
| Facette `answerlineage_toolCall` | Run d'outil, rattaché à son serveur et à son outil ; `traceId` et `spanId`, bien formés et donnés ensemble, sont gardés avec le run, pour passer du graphe à la trace |
| Facette `answerlineage_agent` | Run d'agent, rattaché à l'agent que nomme le job |
| Facette standard `parent` | Run parent |
| `inputs` | Lectures ; les champs de la facette `schema` sont les colonnes lues, la facette `version` donne la version |
| `outputs` avec la facette `answerlineage_delivery` | Réponse (son empreinte) et sa diffusion : canal, référence du message, destinataires pseudonymisés |
| Autres `outputs` | Écritures |

Un événement venu d'un autre émetteur OpenLineage, sans ces facettes, est enregistré comme un job ordinaire avec ses lectures et ses écritures. Une facette propre mal formée est ignorée, sans empêcher d'enregistrer le reste de l'événement. Les schémas de ces facettes sont décrits dans [Facettes OpenLineage](facettes.md).

Chaque identifiant recopié dans le graphe (agent, outil, jeu de données, version, colonne, empreinte, canal, référence de message, destinataire) l'est sous sa forme normalisée NFC : deux écritures d'un même nom, composée et décomposée, donnent un seul objet. Les identifiants ont été contrôlés à l'[ingestion](ingestion.md#identifiants) ; un événement enregistré avant ce contrôle et rejoué n'en fait pas entrer de mal formé : une valeur de facette fautive est ignorée, et un espace de noms ou un nom fautif laisse l'événement dans la file.

L'ordre d'arrivée ne compte pas : deux événements d'un même run reçus dans le désordre donnent le même graphe.

## Limites connues

- Un événement toujours fautif reste dans la file, de plus en plus espacé : il n'y a pas encore de file de rebut à consulter et à rejouer.
