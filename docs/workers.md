# Workers

Un worker lit la file de traitement et projette chaque événement reçu dans le graphe de lignage. La file est une table de PostgreSQL : AnswerLineage n'exploite aucune autre dépendance.

```bash
ANSWERLINEAGE_DATABASE_URL=postgresql://utilisateur:motdepasse@hote/base uv run answerlineage-worker
```

Plusieurs workers peuvent tourner en même temps. Un worker s'arrête proprement sur `SIGINT` ou `SIGTERM`, à la fin du lot en cours. Un événement qu'il ne parvient pas à projeter va à la [file de rebut](#file-de-rebut), où il se consulte et se rejoue. Il fait aussi l'[analyse des incidents](#analyse-des-incidents) déclarés, contrôle les [émetteurs muets](#émetteurs-muets) et entretient les [partitions mensuelles](#partitions-mensuelles). La même commande [reconstruit le graphe](#reconstruire-le-graphe) depuis les événements bruts, quand il est corrompu.

## Garanties

L'API d'ingestion écrit une ligne dans la file dans la transaction qui enregistre l'événement. Le worker réclame un lot, le traite et le retire de la file dans une seule transaction :

- **aucun double traitement** : les lignes réclamées par un worker sont verrouillées, les autres workers les sautent (`FOR UPDATE SKIP LOCKED`) ;
- **aucune perte** : si un worker s'arrête en plein lot, sa transaction est annulée, les lignes redeviennent visibles et aucune écriture partielle ne reste dans le graphe ;
- **un événement fautif ne bloque pas le lot** : il est remis à plus tard, avec un délai qui double à chaque essai (5 s, 10 s, 20 s, jusqu'à une heure). La file garde le nombre d'essais, l'heure du premier en échec et la classe de la dernière erreur, jamais son message ;
- **un événement fautif ne tourne pas sans fin** : il va à la [file de rebut](#file-de-rebut), au premier échec ou au dernier essai selon son motif, dans la transaction qui le retire de la file ;
- **des workers concurrents ne s'interbloquent pas** : chaque lot pose d'avance, dans un ordre global, les lignes du graphe qu'il partage avec les autres. Un interblocage qui survient malgré tout reprend le lot sans compter d'essai : un pic de trafic n'envoie pas au rebut un événement sain ([plusieurs workers](#plusieurs-workers)).

Les événements bruts restent dans `event_raw` : le graphe peut être [reconstruit](#reconstruire-le-graphe) en les rejouant.

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

Chaque identifiant recopié dans le graphe (agent, outil, jeu de données, version, colonne, empreinte, canal, référence de message, destinataire) l'est sous sa forme normalisée NFC : deux écritures d'un même nom, composée et décomposée, donnent un seul objet. Les identifiants ont été contrôlés à l'[ingestion](ingestion.md#identifiants) ; un événement enregistré avant ce contrôle et rejoué n'en fait pas entrer de mal formé : une valeur de facette fautive est ignorée, et un espace de noms ou un nom fautif envoie l'événement au [rebut](#file-de-rebut) (`invalid_identifier`).

L'ordre d'arrivée ne compte pas : deux événements d'un même run reçus dans le désordre donnent le même graphe.

Chaque ligne que l'événement fait connaître prend pour heure d'enregistrement sa réception par l'API (`event_raw.recorded_at`), pas l'heure du passage du worker : un événement projeté plus tard, après une panne des workers ou parce qu'il a été remis dans la file, garde sa réception d'origine ([deux heures](schema.md#deux-heures)).

## File de rebut

Un événement que la projection ne parvient pas à intégrer au graphe quitte la file pour la file de rebut (`dead_letter`), où il attend d'être consulté puis rejoué. Sans elle, il tournerait sans fin, au plus toutes les heures, sans que rien ne le montre, et son lignage manquerait au graphe.

Il y va dans la transaction du lot qui le retire de la file : ni perte, ni double traitement, même avec plusieurs workers. L'événement brut reste dans `event_raw` : le rebut le désigne, il ne le recopie pas. Il garde :

| Colonne | Contenu |
| --- | --- |
| `event_id`, `recorded_month` | L'événement brut, par son identifiant et son mois de réception |
| `emitter` | Son émetteur : l'espace de noms normalisé de son job, la définition du [compteur de doublons](ingestion.md#compteur-de-doublons) et de la couverture |
| `reason` | Le motif du dernier échec, un code de la liste ci-dessous : la classe de l'erreur, jamais son message, qui peut citer l'événement |
| `attempts` | Les essais en échec |
| `first_failed_at`, `last_failed_at` | Le premier essai en échec, et le dernier : la mise au rebut |

Aucun contenu : ni l'événement, ni le message d'une exception.

### Motifs

| Motif | Erreur | Au rebut |
| --- | --- | --- |
| `invalid_identifier` | Un identifiant obligatoire (espace de noms ou nom du job, d'un jeu de données) n'a pas la forme d'un nom : un événement enregistré avant le contrôle des identifiants | Au premier échec |
| `invalid_event` | L'événement n'a pas la forme que la projection lit : champ obligatoire absent ou d'un autre type, identifiant de run ou heure illisible (`KeyError`, `TypeError`, `ValueError`) | Au premier échec |
| `constraint_violation` | L'écriture du graphe viole une contrainte de la base (`IntegrityError`) : la projection et le schéma du graphe ne s'accordent pas | Au premier échec |
| `invalid_value` | La base refuse une valeur (`DataError`) : hors de portée, texte qu'elle ne sait pas conserver | Au premier échec |
| `database_error` | Une autre erreur de la base : connexion perdue, schéma pas encore migré, interblocage au-delà des [reprises du lot](#erreurs-passagères) | Au dernier essai |
| `projection_error` | Toute autre erreur | Au dernier essai |

Les quatre premiers motifs ne se corrigent pas en réessayant : réessayer ne ferait que retarder le signalement. Les deux derniers peuvent passer : l'événement est remis à plus tard, et ne va au rebut qu'au dernier essai, le dixième par défaut, 42 minutes après le premier. Une panne qui fait échouer le lot entier, base injoignable par exemple, ne compte d'essai à aucun événement : le lot reste dans la file, tel quel. Un interblocage n'en compte pas non plus : le lot est repris ([erreurs passagères](#erreurs-passagères)).

| Réglage | Option | Défaut |
| --- | --- | --- |
| Essais en échec au-delà desquels un événement va au rebut | `--max-attempts` | 10 (1 à 100) |

Le journal nomme l'événement par son identifiant, une empreinte, avec le motif et la classe de l'erreur, jamais son message :

```text
WARNING answerlineage_workers Événement 2a8b9477e2742f3eaeb18f4910603feb800eff1bc80f577f9d0e9cc4851eab10 remis à plus tard : database_error (OperationalError), essai 3 sur 10.
ERROR answerlineage_workers Événement 2a8b9477e2742f3eaeb18f4910603feb800eff1bc80f577f9d0e9cc4851eab10 mis au rebut : constraint_violation (IntegrityError), après 1 essai(s).
```

### Consulter et rejouer

`GET /api/v1/ingestion/dead-letters` liste le rebut, `POST /api/v1/ingestion/dead-letters/replay` le remet en file ([API d'ingestion](ingestion.md#file-de-rebut)) ; depuis le terminal, [`answerlineage dead-letters`](cli.md#answerlineage-dead-letters) fait l'un et l'autre.

Un rebut qui se remplit ne passe pas inaperçu : la [route de santé](cli.md#la-route-de-santé) compte ses événements et donne l'âge du plus ancien, et [`answerlineage doctor`](cli.md#answerlineage-doctor) signale un rebut non vide comme une anomalie (`dead_letters_waiting`), avec son remède : lire les motifs, corriger la cause, puis rejouer.

Le rejeu retire les événements du rebut et les remet en file dans une seule transaction ; un worker les projette comme les autres. Rejouer est sans risque : la projection est idempotente, et un événement intégré n'entre au graphe qu'une fois, avec sa réception d'origine pour heure d'enregistrement. Un événement rejoué repart sans essai : s'il échoue encore, il revient au rebut avec son nouveau motif, ses essais et ses heures d'échec comptés depuis le rejeu.

Le chemin d'une erreur de schéma :

1. une version de la projection écrit dans le graphe une valeur que son schéma refuse : les événements touchés vont au rebut, motif `constraint_violation`, et le journal le dit ;
2. `answerlineage dead-letters list` les montre, par émetteur et par motif ;
3. le correctif déployé, `answerlineage dead-letters replay --reason constraint_violation` les remet en file, et les workers les intègrent au graphe.

Un événement sort du rebut par un rejeu, ou par la [purge](#partitions-mensuelles) du mois où il a été reçu.

## Plusieurs workers

Les workers se partagent la file sans se coordonner : chacun réclame son lot (`FOR UPDATE SKIP LOCKED`) et le projette dans sa transaction. Les événements de lots différents écrivent pourtant les mêmes lignes du graphe : les jeux de données et leurs versions du jour, les outils, les agents et les destinataires qu'ils nomment tous, et les runs dont les événements tombent dans deux lots.

### Un ordre pour les lignes partagées

Une transaction garde, jusqu'à sa validation, chaque ligne qu'elle a créée ou dont elle a avancé l'heure d'enregistrement, et chaque run qu'elle a écrit. Un lot qui projette ses événements l'un après l'autre prend ces lignes dans l'ordre de ses événements ; deux lots qui les prennent dans des ordres différents s'attendent l'un l'autre, et PostgreSQL rompt l'interblocage en annulant l'un d'eux. Son journal le montrait, à chaque interblocage : une insertion dans `agent`, `tool`, `dataset_version`, `recipient` ou `run`, ou l'heure d'enregistrement d'un outil, d'un jeu de données ou d'une version avancée, attendait la fin d'une transaction qui attendait elle-même la sienne. Chaque interblocage remettait un événement à plus tard et lui comptait un essai : au dixième, un événement sain allait au rebut (`database_error`).

Avant de projeter ses événements, un lot écrit donc ces lignes, table par table (agents, outils, jeux de données, versions, destinataires, puis runs) et, dans chaque table, dans l'ordre de leur clé naturelle (`prepare_runs`, dans `answerlineage_api.store.graph`). Toutes les transactions prennent leurs verrous dans le même ordre : aucune n'attend une ligne que tient une transaction qui attend la sienne. Les arêtes d'un run ne sont écrites que par la transaction qui tient ce run.

L'[écriture du graphe](schema.md#écrire-dans-le-graphe) reste idempotente et commutative, et le graphe est celui que donne la projection des événements un à un : chaque ligne partagée reçoit la plus ancienne réception du lot qui la nomme, un jeu de données la nature du premier événement du lot qui le nomme, chaque run l'écriture de chacun de ses événements dans l'ordre du lot ; la projection de chaque événement retrouve ensuite ces lignes telles quelles.

Rien d'un événement en échec ne reste dans le graphe. Quand un événement échoue après que le lot a posé ses lignes, le lot est refait sans lui, dans la même transaction, et son échec est gardé. Quand la base refuse l'une des lignes posées d'avance (une contrainte, une valeur), le lot est projeté événement par événement, sans ordre global, et l'échec revient à l'événement fautif.

### Erreurs passagères

Un interblocage (`40P01`) ou un échec de sérialisation (`40001`) qui survient malgré tout ne dit rien des événements : PostgreSQL a annulé la transaction pour en laisser passer une autre. Le lot est repris en entier, dans une nouvelle transaction, après une courte attente : 0,05 s, puis 0,1 s, jusqu'à 0,25 s. Pas dans la même : une transaction qui attendait une ligne du lot n'est libérée qu'à la fin de la sienne, et la reprise l'attendrait à son tour.

| Erreur | Un essai compté | Ce qui se passe |
| --- | --- | --- |
| Interblocage, échec de sérialisation | Non, pendant cinq reprises | Le lot entier est repris dans une nouvelle transaction |
| Les mêmes, au-delà de cinq reprises | Oui, à l'événement touché | Le lot est projeté une dernière fois, événement par événement : l'erreur compte comme toute erreur de la base (`database_error`) |
| Une autre erreur de la base pendant la projection d'un événement : connexion perdue, base injoignable, schéma pas encore migré | Oui | L'événement est remis à plus tard, et va au rebut au dernier essai |
| Une panne qui fait échouer le lot entier, hors de la projection d'un événement : base injoignable à la réclamation | Non | Le lot reste dans la file, tel quel ; le worker réessaie cinq secondes plus tard |

La borne de cinq reprises évite qu'un lot tourne sans fin : un événement qu'une erreur passagère toucherait à chaque reprise finit au rebut au dernier essai, comme les autres.

Chaque reprise est journalisée, en nombres et en codes : les événements repris, le code SQLSTATE, la classe de l'erreur, jamais son message. Le résultat d'un lot (`BatchResult.retried`) compte ses reprises.

```text
WARNING answerlineage_workers Lot de 100 événement(s) repris sans compter d'essai : 40P01 (OperationalError), reprise 1 sur 5.
```

### Taille des lots

| Réglage | Option | Défaut |
| --- | --- | --- |
| Événements réclamés et projetés par lot, dans une transaction | `--batch-size` | 100 (1 à 10 000) |

Un lot est une transaction, qui tient jusqu'à sa validation les lignes qu'elle écrit. Une taille hors de ces bornes empêche le worker de démarrer, comme les autres réglages.

### Débit

Mesuré sur la machine de test : 4 processeurs virtuels Intel Xeon à 2,10 GHz, 15 Gio de mémoire, PostgreSQL 18 dans Docker sur la même machine, réglages par défaut. La file tient les 11 903 événements de l'entreprise synthétique à 3 000 demandes, reçus en envois de 50 ; le graphe est vide au départ, chaque worker tourne dans son processus, avec des lots de 100. La durée va du départ des workers à la file vide, attente des événements remis à plus tard comprise. Le code d'avant et celui d'après ont été mesurés en alternance ; la machine faisait tourner d'autres charges en même temps : ce sont des ordres de grandeur.

| Workers | Avant l'ordre global | Après |
| --- | --- | --- |
| 1 | 207 à 218 événements par seconde | 296 à 308 événements par seconde |
| 4 | 43 à 161 événements par seconde ; 83 à 279 interblocages par passage, autant d'événements remis à plus tard avec un essai compté | 593 à 649 événements par seconde ; aucun interblocage, aucune reprise, aucun événement remis à plus tard |

Avant, le débit de quatre workers tombait sous celui d'un seul : chaque interblocage attendait une seconde que PostgreSQL le détecte, puis remettait un événement à plus tard de 5 s, 10 s, 20 s. Un seul worker va aussi plus vite : la projection de chaque événement reprend les lignes que son lot a posées d'avance, sans les relire.

## Analyse des incidents

La déclaration d'un [incident](incidents.md) écrit une demande d'analyse dans sa transaction (`incident_analysis`, état `pending`). Après chaque lot d'événements, la boucle du worker en réclame une, si une attend, et la traite :

1. elle lance la [propagation](lecture.md#propager-un-incident) sur chaque jeu de données du périmètre et sur la fenêtre de validité, telle que le graphe était connu à l'heure de l'analyse (`known_at`) ;
2. elle réunit les runs touchés, et garde les réponses de ces runs, par leur run et leur empreinte, et les pseudonymes de leurs destinataires ;
3. elle mesure la [couverture du lignage](lecture.md#couverture-du-lignage) sur la fenêtre, et note les jeux du périmètre que le graphe ne connaît pas ;
4. elle enregistre le résultat et passe l'analyse à l'état `done`.

Les garanties sont celles de la file d'ingestion :

- **Sur un graphe entier.** Pendant une [reconstruction du graphe](#reconstruire-le-graphe), et tant que la file n'a pas rendu au graphe ce que la reconstruction y a remis, aucune analyse ne part : la demande attend, puis lit le graphe reconstruit.
- **Une analyse, un worker.** La demande est réclamée, traitée et close dans une seule transaction ; les workers sautent une demande verrouillée par un autre (`FOR UPDATE SKIP LOCKED`).
- **Aucune perte.** Un worker arrêté en cours d'analyse n'écrit rien : la demande redevient visible, telle quelle.
- **Sans doublon.** Le résultat remplace celui d'une analyse précédente du même incident : rejouer une analyse ne duplique ni réponse ni destinataire.
- **Le même graphe à chaque essai.** `known_at` est l'heure de la base au premier essai, gardée pour les suivants.
- **Un échec se dit par un code.** Une analyse en échec est remise à plus tard, avec un délai qui double à chaque essai (5 s, 10 s, 20 s…) ; au huitième essai en échec, elle passe à l'état `failed`. La demande garde le nombre d'essais et le code du dernier échec, d'une liste fermée : `query_timeout` quand une lecture dépasse le délai de l'analyse, `analysis_error` pour toute autre erreur. Le journal en donne la classe, jamais le message :

  ```text
  WARNING answerlineage_workers Analyse de l'incident 974e7c80-6643-4065-a541-e4c029abbdab remise à plus tard (OperationalError).
  ```

Une analyse dispose d'une minute : chaque lecture part sous le temps qui en reste, et PostgreSQL l'annule au-delà, comme les [lectures de l'API](lecture.md#délai-maximal). Le journal ne nomme un incident que par son identifiant, tiré par l'API.

## Émetteurs muets

Un émetteur publie ses [relevés de couverture](ingestion.md#relevés-de-couverture) fenêtre par fenêtre, y compris une fenêtre sans appel : c'est le battement qui prouve qu'il n'est pas muet. Un émetteur qui se tait ne capte plus rien, et rien d'autre ne le dirait.

Au démarrage puis chaque minute, la boucle du worker contrôle les émetteurs connus, ceux qui ont publié au moins un relevé. Un émetteur dont le dernier relevé, de quelque instance qu'il vienne, a été reçu il y a plus de dix minutes est signalé :

- une ligne de journal qui le nomme, par son identifiant contrôlé à la réception, jamais par un contenu :

  ```text
  WARNING answerlineage_workers Émetteur muet : mcp://entrepot, aucun relevé depuis 2026-11-11T09:01:02+00:00.
  ```

- un état que lit l'API : `muted_since`, l'heure de son dernier relevé, dans `GET /api/v1/coverage` ([API de lecture](lecture.md#émetteurs)).

Le silence se suit par émetteur, pas par instance : un émetteur est muet quand aucune de ses instances ne publie. Une instance arrêtée par une mise à l'échelle, tant qu'une autre publie, n'est pas une alerte ; la [couverture](lecture.md#instances) dit combien d'instances ont publié sur une période.

Un épisode de silence est signalé une fois, pas à chaque passage : l'état retient le relevé après lequel le silence a commencé. Un émetteur qui republie cesse d'être muet, aussitôt pour l'API ; s'il se tait de nouveau, c'est un nouvel épisode, signalé à son tour. Un relevé rejoué ne ranime pas un émetteur.

Plusieurs workers contrôlent en même temps sans signaler deux fois le même épisode ni s'attendre : le contrôle prend un verrou consultatif de PostgreSQL sans l'attendre, et un worker qui le trouve pris passe son tour. La mise à jour ne retient d'ailleurs qu'un épisode pas encore signalé.

| Réglage | Option | Défaut |
| --- | --- | --- |
| Silence au-delà duquel un émetteur est signalé | `--silence-minutes` | 10 minutes (1 à 10 080) |

L'horloge du contrôle est celle du worker, comparée à l'heure de réception posée par la base : des horloges très décalées avancent ou retardent le signalement d'autant. Un contrôle en échec, base injoignable par exemple, est journalisé par la classe de l'erreur et repris au passage suivant ; la file n'attend pas.

## Partitions mensuelles

Les événements bruts, les relevés de couverture et le compteur de doublons sont [partitionnés par mois de réception](schema.md#partitions-et-conservation). Au démarrage, puis toutes les heures, la boucle du worker les entretient, sans intervention :

1. **Création à l'avance.** Le mois courant et les deux suivants ont leur partition, pour chaque table. Une écriture trouve toujours la sienne : une partition manque au plus tôt deux mois après le dernier passage réussi.
2. **Purge.** Une partition entièrement plus ancienne que la conservation est supprimée : avec 13 mois, la maintenance du 15 mars 2026 supprime janvier 2025, qui finit avant le 15 février 2025, et garde février 2025. Les événements d'un mois purgé encore dans la file, jamais projetés après tant de mois, en sont retirés, comme ceux qui attendent au rebut, et une ligne de journal le dit.
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

## Reconstruire le graphe

Le graphe se déduit tout entier des événements bruts. Quand il est corrompu, par un défaut de la projection corrigé depuis ou une écriture malheureuse à la main, on le vide et on rejoue les événements, sans restaurer toute la base :

```bash
# L'aperçu : ce que la reconstruction trouverait. Rien ne change.
ANSWERLINEAGE_DATABASE_URL=postgresql://utilisateur:motdepasse@hote/base uv run answerlineage-worker rebuild
# La reconstruction.
ANSWERLINEAGE_DATABASE_URL=postgresql://utilisateur:motdepasse@hote/base uv run answerlineage-worker rebuild --yes
```

C'est un geste d'exploitation : aucune route de l'API ne le déclenche.

| Option | Rôle | Défaut |
| --- | --- | --- |
| `--yes` | Confirme : vide le graphe et rejoue les événements. Sans elle, la commande donne l'aperçu et ne change rien | absente |
| `--accept-loss` | Accepte de perdre les lignes du graphe que la reconstruction ne peut pas rendre, enregistrées avant le premier événement conservé ([ce qu'elle ne rend pas](#ce-que-la-reconstruction-ne-rend-pas)) | absente |
| `--batch-size` | Événements projetés par lot | 100 (1 à 10 000) |

La commande sort avec le code `0` quand le graphe est reconstruit, `1` quand elle n'a rien fait (aperçu, refus) ou qu'elle a été interrompue, `2` quand la configuration est invalide.

### Pas à pas

1. **L'aperçu.** Sans `--yes`, la commande compte et s'arrête :

   ```text
   INFO answerlineage_workers Le graphe compte 2359 lignes.
   INFO answerlineage_workers 955 événements bruts conservés, reçus du 2026-11-16T12:13:07.400791+00:00 au 2026-11-16T12:13:09.102214+00:00. La reconstruction les remet en file, sauf 1 au rebut, laissés tels quels ; 0 y attendent déjà.
   INFO answerlineage_workers Rien n'est fait sans --yes.
   ```

2. **Une à la fois.** La reconstruction prend un verrou consultatif de PostgreSQL sans l'attendre : une seconde reconstruction ne démarre pas (« Une autre reconstruction du graphe tourne : celle-ci ne démarre pas. Rien n'a changé. »).
3. **Les workers s'écartent.** Elle prend la barrière du graphe, un second verrou consultatif : elle laisse finir les lots et les analyses en cours, puis les workers ne réclament plus ni lot ni analyse tant qu'elle la tient. Leur boucle continue : émetteurs muets, partitions. La reconstruction projette ensuite seule, sans partager la file avec les workers ([limites connues](#limites-connues)).
4. **Le départ, en une transaction.** Quand des lignes du graphe ne seraient pas rendues, elle s'arrête là sans `--accept-loss`, et rien ne change ([ce qu'elle ne rend pas](#ce-que-la-reconstruction-ne-rend-pas)). Sinon, elle remet en file tous les événements bruts conservés, dans l'ordre de leur réception, sauf ceux au rebut, qui y restent avec leur motif ; un événement qui attendait déjà dans la file y garde sa place et ses essais. Elle s'inscrit dans `graph_rebuild` ([la trace](#la-trace)), puis vide les tables du graphe, et elles seules. Une panne avant la fin de cette transaction ne change rien.
5. **La projection, par lots.** Elle projette la file elle-même, lot par lot, avec la projection des workers et dans l'ordre de la file. Un événement que la projection refuse aujourd'hui va au [rebut](#file-de-rebut), comme avec un worker. Toutes les dix secondes, le journal dit où elle en est.
6. **La fin.** Quand la file a rendu au graphe tout ce qui y était au départ, la reconstruction est close, la barrière rendue, et le journal donne le résultat.

```text
INFO answerlineage_workers Reconstruction du graphe commencée : 954 événement(s) à rejouer, 1 laissé(s) au rebut.
INFO answerlineage_workers Reconstruction du graphe : 954 événement(s) projeté(s), 0 à projeter dans la file, 0 mis au rebut.
INFO answerlineage_workers Graphe reconstruit en 3,3 s : 954 événements rejoués, 1 au rebut laissés tels quels, 0 mis au rebut pendant la reconstruction.
INFO answerlineage_workers Événements bruts conservés reçus du 2026-11-16T12:13:07.400791+00:00 au 2026-11-16T12:13:09.102214+00:00 : ce que la conservation a purgé avant n'est pas rendu.
```

Les événements rejoués sont ceux que la reconstruction a remis en file et ceux qui y attendaient ; les événements mis au rebut pendant la reconstruction, ceux que la projection refuse depuis son départ, rejoués ou reçus entre-temps. La durée va du départ à la fin, interruptions comprises. Le journal ne dit que des nombres et des heures, et d'une erreur seulement sa classe.

### Pendant la reconstruction

- **L'ingestion continue.** L'API reçoit et enregistre comme d'habitude. Un événement reçu entre en file derrière ceux de la reconstruction et il est projeté à son tour, par elle ou, après elle, par les workers ; un renvoi est compté comme doublon. La file suivant l'ordre de réception, les événements arrivent au graphe dans l'ordre où AnswerLineage les a reçus.
- **Les lectures voient un graphe partiel.** À tout instant, un événement est dans la file ou dans le graphe : la [couverture du lignage](lecture.md#couverture-du-lignage) compte comme trous `missing_from_graph` les appels d'outil encore en file. Elle ne compte pas les événements des agents : une réponse dont l'événement attend encore manque sans trou. Attendre la fin de la reconstruction avant de conclure.
- **Les analyses d'incident attendent.** Une demande d'analyse attend que la file ait rendu au graphe tout ce que la reconstruction y a mis ; elle lit ensuite le graphe reconstruit.

### Interruption et reprise

Le point de reprise est dans la base : la file, que chaque lot projeté quitte dans sa transaction, et la ligne inachevée de `graph_rebuild`. Sur `SIGINT` ou `SIGTERM`, la reconstruction finit son lot et s'arrête ; arrêtée brutalement, processus tué ou base perdue, son lot en cours est annulé et reste dans la file. Dans les deux cas, la barrière tombe avec sa session : les workers projettent ce qui reste.

```text
INFO answerlineage_workers Reconstruction interrompue : 412 à projeter dans la file. Les workers les projettent ; « answerlineage-worker rebuild --yes » reprend la reconstruction.
```

Relancée, `answerlineage-worker rebuild --yes` reprend la reconstruction inachevée sans vider le graphe de nouveau, et la clôt quand la file a tout rendu. Les analyses attendent jusque-là.

### Identique à l'original

La reconstruction rend chaque ligne de chacune des onze tables du graphe (`agent`, `tool`, `dataset`, `dataset_version`, `run`, `read_edge`, `write_edge`, `answer`, `delivery`, `recipient`, `delivery_recipient`), avec toutes ses colonnes, heures d'événement et [d'enregistrement](schema.md#deux-heures) comprises : l'heure d'enregistrement est la réception de l'événement, qu'un rejeu ne change pas.

Seuls changent les identifiants numériques que la base tire à l'écriture : la colonne `id` de neuf de ces tables, qui repart de un. Rien hors du graphe ne les cite, les incidents désignent une réponse par son run et son empreinte, un jeu de données par son nom, et l'API ne les rend jamais : l'[identifiant d'une réponse](lecture.md#identifiant-dune-réponse) se déduit de son run et de son empreinte, et une adresse de l'explorateur reste valable. La comparaison des tests remplace donc chaque clé étrangère par la clé naturelle de la ligne qu'elle vise.

L'ordre n'y fait rien. Les workers avaient projeté les événements dans l'ordre de leur file, avec ses reprises, ses rejeux et ses workers concurrents ; la reconstruction les rejoue dans l'ordre de leur réception, puis de leur identifiant. L'[écriture du graphe](schema.md#écrire-dans-le-graphe) est idempotente et commutative : projeter deux fois un événement, ou deux événements dans un autre ordre, donne le même graphe. `tests/test_graph_rebuild.py` le vérifie ligne à ligne sur l'entreprise synthétique, reçue d'un seul envoi puis rejouée dans un autre ordre, sur chaque scénario du corpus de vérité, avec des événements reçus pendant la reconstruction, et avec deux workers qui finissent une reconstruction interrompue.

### Ce que la reconstruction ne rend pas

- **Ce que la conservation a purgé.** Les [partitions](#partitions-mensuelles) d'événements bruts plus anciennes que la conservation sont supprimées ; le graphe, lui, garde toutes ses lignes. Une ligne enregistrée avant le premier événement conservé revient autrement, ou pas du tout : son heure d'enregistrement devient celle d'un événement plus récent, ou plus rien ne la fait connaître. L'aperçu les compte, la reconstruction refuse de démarrer sans `--accept-loss` quand il y en a, et son résultat dit la période de réception des événements conservés. Une ligne enregistrée depuis le premier événement conservé revient à l'identique : tous les événements qui l'ont fait connaître sont conservés.
- **Les événements au rebut.** Ils n'étaient pas dans le graphe d'origine et n'entrent pas dans le graphe reconstruit : ils restent au rebut avec leur motif, leurs essais et leurs heures. Le correctif déployé, [ils se rejouent](#consulter-et-rejouer).
- **Ce qu'aucun événement ne dit.** Une ligne écrite dans le graphe à la main, sans événement, ne revient pas.

### Ce qu'elle ne touche pas

Seules les tables du graphe sont vidées. Les événements bruts, la file, où elle remet les événements, le rebut, le compteur de doublons, les relevés de couverture et les émetteurs connus, les alertes de partition, les incidents et leurs analyses restent tels quels.

De tout cela, seul le résultat d'une analyse d'incident dérive du graphe : ses réponses touchées, leurs destinataires, ses décomptes et sa couverture, lus tels que le graphe était connu à l'heure de l'analyse (`known_at`). La reconstruction ne les recalcule pas. Elle rend chaque ligne avec son heure d'enregistrement : le graphe tel que connu à `known_at` est celui d'avant la corruption, et une analyse faite avant elle reste juste. Une analyse faite sur le graphe corrompu se rejoue en gardant son `known_at` ([rejouer une analyse](incidents.md#rejouer-une-analyse)) : elle lit alors ce qu'elle aurait dû lire. La couverture du lignage se calcule à la lecture, des événements bruts et de la file : rien n'est à recalculer.

### Durée

Mesurée sur la machine de test : 4 processeurs virtuels Intel Xeon à 2,10 GHz, 15 Gio de mémoire, PostgreSQL 18 dans Docker sur la même machine, réglages par défaut, lots de 100 événements. La machine faisait tourner d'autres charges en même temps : ce sont des ordres de grandeur.

| Événements conservés | Lignes du graphe | Départ | Reconstruction entière | Débit |
| --- | --- | --- | --- | --- |
| 954, l'entreprise synthétique de la démonstration | 2 359 | négligeable | 3,3 s | 292 événements par seconde |
| 99 954, la même entreprise avec 25 000 demandes | 232 389 | moins d'une seconde | 5 min 51 s | 285 événements par seconde |
| 1 000 000, estimés | environ 2,3 millions | 17 s mesurées | environ 59 min | |

La durée est celle de la projection, un événement après l'autre, environ 3,5 ms chacun : le départ, la remise en file d'un million d'événements comprise, prend 17 secondes, pendant lesquelles le graphe reste lisible ; le vidage, quelques millisecondes. Une reconstruction croît donc avec les événements conservés, de l'ordre d'une heure par million sur cette machine.

Pour un délai de reprise d'une heure au plus, la reconstruction suffit tant que la base conserve moins d'un million d'événements environ, sur une machine comparable. Au-delà, elle ne tient pas l'objectif à elle seule : la restauration d'une sauvegarde de la base, ou une machine plus rapide, doit le tenir. Le journal de progression, toutes les dix secondes, permet d'en estimer la fin.

### La trace

`graph_rebuild` garde une ligne par reconstruction : son départ et sa fin, la dernière ligne de la file qu'elle attend, les événements rejoués et laissés au rebut, la période de réception des événements conservés et les lignes qu'elle n'a pas pu rendre ([schéma](schema.md#reconstructions-du-graphe)).

```sql
SELECT started_at, finished_at, replayed, dead_letters, first_received, last_received, unrecoverable
FROM graph_rebuild ORDER BY started_at DESC;
```

## Limites connues

- Une analyse est un instantané : elle ne voit que les événements déjà projetés. Un événement reçu avant elle mais encore dans la file lui échappe, et la couverture le signale par un trou `missing_from_graph` ; aucune route ne relance encore une analyse ([rejouer une analyse](incidents.md#rejouer-une-analyse)).

- Le motif suit la classe de l'erreur : un défaut de la projection qui lève `KeyError`, `TypeError` ou `ValueError` met l'événement au rebut dès le premier échec, comme un événement mal formé. Le correctif déployé, il se rejoue.
- Les essais d'un événement remis à plus tard avant la migration `0011` n'avaient pas d'heure : sa mise en file tient lieu de premier échec.
- La reconstruction projette seule, un événement après l'autre : sa durée croît avec les événements conservés ([durée](#durée)), et des workers de plus ne l'accélèrent pas, alors que quatre workers vident une file chargée deux fois plus vite qu'un seul ([débit](#débit)).
- Pendant une reconstruction, l'API lit un graphe partiel ; la couverture ne signale que les appels d'outil encore en file ([pendant la reconstruction](#pendant-la-reconstruction)). Une analyse faite sur un graphe corrompu n'est pas rejouée d'elle-même.
- Un run garde l'agent, l'outil, le run parent et le contexte de trace du premier événement projeté qui les donne, et un jeu de données la nature du premier qui le nomme. Tant que les événements d'un run s'accordent, comme ceux des émetteurs d'AnswerLineage, l'ordre n'y fait rien ; deux événements qui se contredisent peuvent donner l'autre valeur, quand ils ont été reçus dans le même envoi ou que leur première projection n'a pas suivi leur réception (un événement remis à plus tard, puis rejoué du rebut).
- Un émetteur retiré pour de bon est signalé muet une fois, puis le reste : rien ne permet encore de le déclarer retiré, ni de l'oublier. Sa dernière couverture reste lisible, et son silence rend partielle la couverture des périodes qui suivent.
