# Workers

Un worker lit la file de traitement et projette chaque événement reçu dans le graphe de lignage. La file est une table de PostgreSQL : AnswerLineage n'exploite aucune autre dépendance.

```bash
ANSWERLINEAGE_DATABASE_URL=postgresql://utilisateur:motdepasse@hote/base uv run answerlineage-worker
```

Plusieurs workers peuvent tourner en même temps. Un worker s'arrête proprement sur `SIGINT` ou `SIGTERM`, à la fin du lot en cours. Il contrôle aussi les [émetteurs muets](#émetteurs-muets) et entretient les [partitions mensuelles](#partitions-mensuelles).

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

Chaque ligne que l'événement fait connaître prend pour heure d'enregistrement sa réception par l'API (`event_raw.recorded_at`), pas l'heure du passage du worker : un événement projeté plus tard, après une panne des workers ou parce qu'il a été remis dans la file, garde sa réception d'origine ([deux heures](schema.md#deux-heures)).

## Émetteurs muets

Un émetteur publie ses [relevés de couverture](ingestion.md#relevés-de-couverture) fenêtre par fenêtre, y compris une fenêtre sans appel : c'est le battement qui prouve qu'il n'est pas muet. Un émetteur qui se tait ne capte plus rien, et rien d'autre ne le dirait.

Au démarrage puis chaque minute, la boucle du worker contrôle les émetteurs connus, ceux qui ont publié au moins un relevé. Un émetteur dont le dernier relevé a été reçu il y a plus de dix minutes est signalé :

- une ligne de journal qui le nomme, par son identifiant contrôlé à la réception, jamais par un contenu :

  ```text
  WARNING answerlineage_workers Émetteur muet : mcp://entrepot, aucun relevé depuis 2026-11-11T09:01:02+00:00.
  ```

- un état que lit l'API : `muted_since`, l'heure de son dernier relevé, dans `GET /api/v1/coverage` ([API de lecture](lecture.md#émetteurs)).

Un épisode de silence est signalé une fois, pas à chaque passage : l'état retient le relevé après lequel le silence a commencé. Un émetteur qui republie cesse d'être muet, aussitôt pour l'API ; s'il se tait de nouveau, c'est un nouvel épisode, signalé à son tour. Un relevé rejoué ne ranime pas un émetteur.

Plusieurs workers contrôlent en même temps sans signaler deux fois le même épisode ni s'attendre : le contrôle prend un verrou consultatif de PostgreSQL sans l'attendre, et un worker qui le trouve pris passe son tour. La mise à jour ne retient d'ailleurs qu'un épisode pas encore signalé.

| Réglage | Option | Défaut |
| --- | --- | --- |
| Silence au-delà duquel un émetteur est signalé | `--silence-minutes` | 10 minutes (1 à 10 080) |

L'horloge du contrôle est celle du worker, comparée à l'heure de réception posée par la base : des horloges très décalées avancent ou retardent le signalement d'autant. Un contrôle en échec, base injoignable par exemple, est journalisé par la classe de l'erreur et repris au passage suivant ; la file n'attend pas.

## Partitions mensuelles

Les événements bruts, les relevés de couverture et le compteur de doublons sont [partitionnés par mois de réception](schema.md#partitions-et-conservation). Au démarrage, puis toutes les heures, la boucle du worker les entretient, sans intervention :

1. **Création à l'avance.** Le mois courant et les deux suivants ont leur partition, pour chaque table. Une écriture trouve toujours la sienne : une partition manque au plus tôt deux mois après le dernier passage réussi.
2. **Purge.** Une partition entièrement plus ancienne que la conservation est supprimée : avec 13 mois, la maintenance du 15 mars 2026 supprime janvier 2025, qui finit avant le 15 février 2025, et garde février 2025. Les événements d'un mois purgé encore dans la file, jamais projetés après tant de mois, en sont retirés, et la ligne de journal le dit.
3. **Contrôle.** Une partition qui manque pour un mois qui commence dans les sept jours, ou pour le mois courant, est signalée :

   - une ligne de journal qui nomme la table et le mois, jamais un contenu :

     ```text
     ERROR answerlineage_workers Partition manquante : event_raw, mois 2026-04, à créer avant le 2026-04-01.
     ```

   - une ligne de `partition_alert`, qui reste tant que la partition manque ([lire l'état](schema.md#létat-du-contrôle)).

   Un épisode est signalé une fois, pas à chaque passage. Il se clôt quand la partition existe, et sa ligne disparaît.

La création, la purge et le contrôle sont journalisés : `Partition créée : event_raw_2026_04.`, `Partition supprimée : event_raw_2025_01, hors de la conservation de 13 mois.`

| Réglage | Option | Défaut |
| --- | --- | --- |
| Conservation, en mois | `--retention-months` | 13 (1 à 1 200) |
| Mois créés d'avance après le mois courant | `--partitions-ahead` | 2 (2 à 24) |

Une conservation absurde empêche le worker de démarrer : nulle, négative, ou plus courte que l'avance. La plus courte admise, deux mois, garde toujours le mois courant et le précédent.

### Ce que la purge ne touche jamais

- **Le mois courant.** La limite de la purge est la plus prudente de deux horloges : celle du worker et celle de la base, qui pose la clé de partition. Une horloge de worker très en avance ne fait tomber aucun mois que la base tient encore pour conservé.
- **Une partition partiellement dans la conservation.** Seule une partition qui finit avant la limite est supprimée.
- **Une partition par défaut**, ou de bornes que la maintenance ne lit pas, créée à la main : elle ne couvre aucun mois et n'est jamais supprimée.
- **Le graphe**, hors de la conservation ([pourquoi](schema.md#le-graphe-nest-pas-partitionné)).

### Plusieurs workers, aucune attente

Plusieurs workers entretiennent en même temps sans double travail : la maintenance prend un verrou consultatif de PostgreSQL sans l'attendre, et un worker qui le trouve pris passe son tour. Créer ou supprimer une partition verrouille sa table un instant ; la maintenance n'attend pas ce verrou plus d'une seconde (`lock_timeout`), pour ne jamais faire attendre l'ingestion derrière elle : derrière une longue lecture, elle renonce pour cette table, le journalise par la classe de l'erreur (`Partition event_raw_2026_04 non créée (LockNotAvailable).`), et réessaie au passage suivant. Une maintenance en échec, base injoignable par exemple, est journalisée de même ; la file n'attend pas.

L'horloge de la maintenance est celle du worker, injectée dans les tests : les mois y passent sans attendre.

## Limites connues

- Un événement toujours fautif reste dans la file, de plus en plus espacé : il n'y a pas encore de file de rebut à consulter et à rejouer. Il en sort quand son mois est purgé.
- Un émetteur retiré pour de bon est signalé muet une fois, puis le reste : rien ne permet encore de le déclarer retiré, ni de l'oublier. Sa dernière couverture reste lisible, et son silence rend partielle la couverture des périodes qui suivent.
