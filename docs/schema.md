# Schéma du graphe

Le lignage est stocké dans PostgreSQL, seule dépendance obligatoire d'AnswerLineage. Le schéma est déclaré dans `api/src/answerlineage_api/store/schema.py` et créé par les migrations Alembic de `api/src/answerlineage_api/migrations/`. Un test compare la base migrée au schéma déclaré, colonne par colonne, contrainte par contrainte.

## Du jeu de données au destinataire

```text
dataset ── dataset_version
   │
read_edge ── run ── answer ── delivery ── delivery_recipient ── recipient
   │          │
write_edge   agent, tool
```

Les onze tables d'`agent` à `delivery_recipient` forment le graphe (`schema.GRAPH`) : la projection des événements les écrit, elles seules, et tout ce qu'elles tiennent se déduit des événements bruts. La [reconstruction](workers.md#reconstruire-le-graphe) les vide et les remplit de nouveau en rejouant les événements. Hors d'elles, seul le résultat d'une analyse d'incident dérive du graphe : un instantané, lu tel que le graphe était connu à l'heure de l'analyse ([ce que la reconstruction ne touche pas](workers.md#ce-quelle-ne-touche-pas)).

| Table | Clé naturelle | Contenu |
| --- | --- | --- |
| `agent`, `tool` | espace de noms et nom, serveur et nom | Qui a exécuté le run |
| `dataset`, `dataset_version` | espace de noms et nom, puis version | Table, document, API, mémoire ou réponse |
| `run` | `run_id` | Requête d'un agent ou appel d'outil, run parent, trace, état |
| `read_edge` | run, jeu de données, version | Niveau de précision, colonnes lues, origine (analyse ou réconciliation), première et dernière heure de lecture |
| `write_edge` | run, jeu de données, version | Sortie produite par le run |
| `answer` | run et empreinte | Empreinte de la réponse, jamais son contenu |
| `delivery` | réponse, canal, référence du message | Diffusion de la réponse |
| `delivery_recipient` | diffusion et destinataire | Destinataire d'une diffusion |
| `recipient` | pseudonyme | Destinataire pseudonymisé, sans table de correspondance : `hmac-sha256:` suivi de 64 chiffres hexadécimaux, jamais une identité ([pseudonymes](securite/pseudonymes.md)) |
| `event_raw` | empreinte de l'événement | Événement OpenLineage reçu tel quel, rejouable |
| `ingest_queue` | événement | File de traitement : événements à projeter dans le graphe, leurs essais en échec |
| `dead_letter` | événement | File de rebut : événements que la projection n'a pas pu intégrer, leur émetteur et le motif de l'échec |
| `reemission_queue` | événement | File de réémission : événements projetés qui attendent d'être envoyés au consommateur OpenLineage externe, leurs envois en échec |
| `reemission_stats` | une seule ligne | Événements réémis, refusés par la destination et abandonnés, depuis la mise en service |
| `ingest_duplicate` | émetteur et jour | Nombre d'événements reçus en double, par émetteur et par jour UTC |
| `coverage_report` | émetteur, début et fin de fenêtre | Relevé de couverture : appels vus, événements émis et perdus, requêtes non inspectées |
| `coverage_emitter` | émetteur | Émetteur connu : réception de son dernier relevé, silence signalé |
| `partition_alert` | table et mois | Partition manquante signalée par le contrôle des workers |
| `incident` | identifiant tiré par l'API | Incident déclaré : état, gravité, fenêtre de validité, heure de déclaration |
| `incident_dataset` | incident, espace de noms et nom | Périmètre d'un incident : les jeux de données faux |
| `incident_analysis` | incident | Demande d'analyse d'un incident, son état, et les décomptes, la couverture et les jeux inconnus de son résultat |
| `incident_answer` | incident, run et empreinte | Réponse touchée par un incident |
| `incident_recipient` | incident, réponse et pseudonyme | Destinataire d'une réponse touchée |
| `incident_quarantine` | identifiant tiré par la base | Quarantaine d'un incident, une ligne par activation : mode, état, heures d'activation et de levée |
| `webhook_queue` | identifiant de livraison | File des webhooks : changements d'état des incidents qui attendent d'être annoncés au récepteur, figés à l'heure du changement, leurs envois en échec |
| `webhook_stats` | une seule ligne | Échéance de la mise en service des webhooks ; messages remis, refusés par le récepteur et abandonnés, depuis la mise en service |
| `graph_rebuild` | identifiant tiré par la base | Reconstruction du graphe depuis les événements bruts : départ, fin, événements rejoués, période des événements conservés |
| `recipient_revelation` | identifiant tiré par la base | Journal des révélations de destinataires, en ajout seul : auteur, heure, motif, pseudonyme visé, nombre de candidates, résultat ; jamais l'identité |
| `recall_list` | incident | Liste de rappel d'un incident, faite de son analyse : heure de référence de l'exposition, décomptes |
| `recall_entry` | incident et pseudonyme | Un destinataire de la liste, une fois par incident : rang, exposition, réponses touchées comptées |
| `recall_entry_answer` | incident, pseudonyme, run et empreinte | Une réponse touchée qu'un destinataire de la liste a reçue |
| `recall_decision` | identifiant tiré par la base | Journal des décisions sur les listes de rappel, en ajout seul : décision, auteur, heure, gabarit ou motif ; et leurs remontées, sans auteur |
| `recall_dispatch` | identifiant tiré par la base | Message de rappel mis à l'envoi, un par destinataire et par incident : refusé par la base sans liste validée |

## Deux heures

Chaque arête et chaque nœud daté du graphe porte deux heures : ce qui s'est passé selon l'émetteur, et ce qu'AnswerLineage en savait.

| Table | Heure de l'événement | Heure d'enregistrement |
| --- | --- | --- |
| `run` | `started_at`, `ended_at`, `state_time` | `recorded_at` |
| `read_edge` | `event_time` (première), `last_event_time` (dernière) | `recorded_at` |
| `write_edge`, `answer`, `delivery`, `delivery_recipient` | `event_time` | `recorded_at` |
| `agent`, `tool`, `dataset`, `dataset_version`, `recipient` | aucune | `recorded_at` |

- **L'heure de l'événement** est déclarée par l'émetteur (`eventTime`). Plusieurs événements d'une même ligne se fusionnent : la plus ancienne l'emporte, et une lecture garde aussi la plus récente.
- **L'heure d'enregistrement** est la réception, par AnswerLineage, du premier événement qui a fait connaître la ligne : l'heure que `event_raw.recorded_at` garde pour cet événement. Ce n'est pas l'heure de passage du worker : rejouer un événement, ou le projeter plus tard, ne la change pas. Quand deux événements font connaître la même ligne, elle garde la plus ancienne des deux réceptions.

Elle permet de lire le graphe tel qu'il était connu à une date ([API de lecture](lecture.md#le-graphe-tel-que-connu-à-une-date)) : une ligne enregistrée après cette date n'existait pas encore pour AnswerLineage. Un run porte ses liens vers son parent, son agent et son outil : ils sont connus dès que le run l'est.

### Intervalle d'une lecture

Une lecture garde la première (`event_time`) et la dernière (`last_event_time`) heure d'événement reçues. Un événement rejoué avec une heure antérieure, par un émetteur mal réglé ou malveillant, élargit l'intervalle sans le déplacer : la propagation, qui retient une lecture dont l'intervalle recoupe la fenêtre d'un incident, ne la perd pas. Une contrainte nommée (`ck_read_edge_event_times`) garde la première heure avant la dernière, et un index partiel range les seules lectures étalées sur plusieurs heures.

Les écritures, les réponses et les diffusions gardent une seule heure d'événement, la plus ancienne : aucune lecture ne les retient par une fenêtre.

### Reprise des lignes existantes

La migration `0007` donne leur heure de réception aux lignes enregistrées avant elle. Chaque ligne est rattachée aux événements reçus qui la font connaître, comme le fait la projection : le run, les événements qui portent son identifiant ; une lecture ou une écriture, ceux de son run qui nomment son jeu de données et sa version (sous leur forme NFC) ; une réponse, une diffusion ou un destinataire, ceux de son run dont la facette de diffusion les nomme. Elle prend la plus ancienne de leurs réceptions, et une lecture la dernière de leurs heures d'événement. Un jeu de données, une version ou un destinataire prend la plus ancienne réception de ses arêtes, un agent ou un outil celle des runs qui le nomment.

Une ligne qu'aucun événement reçu ne rattache garde l'heure qu'elle portait, celle du passage du worker : elle suit toujours la réception, et ne fait donc jamais paraître une ligne connue plus tôt qu'elle ne l'a été. Le lien d'une diffusion à un destinataire, sans événement qui le nomme, prend l'heure de sa diffusion. La descente retire les colonnes ajoutées et garde les heures reprises.

## Écrire dans le graphe

`answerlineage_api.store.graph.record_run` enregistre un run et ses arêtes dans la transaction en cours, avec la réception de l'événement qui les fait connaître (`RunRecord.recorded_at` ; sans elle, l'heure de la transaction). L'écriture est idempotente et commutative : rejouer un enregistrement, ou recevoir deux événements d'un même run dans le désordre, donne le même graphe, heures comprises. Deux lectures d'un jeu de données, dans une même version, par un même run se fusionnent en une arête : elle couvre l'une et l'autre ([plus bas](#fusion-de-deux-lectures)), et l'intervalle des heures s'étend.

### Fusion de deux lectures

Une lecture dit quelles lignes et quelles colonnes d'un jeu de données ont été lues :

| `precision` | Lignes | Colonnes |
| --- | --- | --- |
| `table` | Toutes | Toutes ; `columns` est vide |
| `columns` | Toutes | Celles de `columns`, jamais vide |
| `subset` | Un sous-ensemble, que le graphe ne décrit pas | Celles de `columns` ; toutes quand elle est vide |
| `keys` | Les lignes de clés désignées, que le graphe ne décrit pas | Celles de `columns` ; toutes quand elle est vide |

Chaque lecture a une seule forme : une lecture `columns` qui ne nomme aucune colonne est enregistrée comme `table`, une lecture `table` ne garde aucune colonne, et `columns` tient chaque colonne une fois, dans l'ordre de ses points de code.

Deux lectures se fusionnent du côté sûr : la lecture fusionnée est la plus étroite qui couvre l'une et l'autre. Elle ne dit donc jamais moins que l'une d'elles, car une colonne manquante ferait écarter à tort une réponse touchée.

- **Les lignes.** Deux lectures qui ont la même restriction (`subset` et `subset`, `keys` et `keys`) la gardent. Toute autre paire couvre toutes les lignes : le graphe ne garde pas quelles lignes une restriction a lues, et aucune des deux ne décrit leur réunion.
- **Les colonnes.** Toutes dès que l'une des deux lectures les a toutes lues ; sinon, l'union de leurs listes. On note C₁ ⊔ C₂ ce résultat.

| Fusion | `table` | `columns` C₂ | `subset` C₂ | `keys` C₂ |
| --- | --- | --- | --- | --- |
| `table` | `table` | `table` | `table` | `table` |
| `columns` C₁ | `table` | `columns` C₁ ⊔ C₂ | `columns` C₁ ⊔ C₂ | `columns` C₁ ⊔ C₂ |
| `subset` C₁ | `table` | `columns` C₁ ⊔ C₂ | `subset` C₁ ⊔ C₂ | `columns` C₁ ⊔ C₂ |
| `keys` C₁ | `table` | `columns` C₁ ⊔ C₂ | `columns` C₁ ⊔ C₂ | `keys` C₁ ⊔ C₂ |

Une fusion `columns` dont C₁ ⊔ C₂ couvre toutes les colonnes est `table`. Un run qui a lu la table entière, puis nommé une colonne, garde donc la table entière : une autre colonne fausse touche ses réponses.

La fusion est idempotente, commutative et associative : rejouer un événement, recevoir les événements d'un run dans le désordre, ou les fusionner en plusieurs fois, donne la même arête. `api/tests/test_read_merge.py` le vérifie sur toutes les paires de précisions et sur des triplets, et vérifie que la fusion couvre chacune des deux lectures. La [remontée](lecture.md#ce-qui-compte-comme-source) réunit de la même façon les lectures d'un jeu par les runs et les versions de l'arbre d'une réponse, et le [résumé de lignage A2A](a2a.md#ce-que-lappelé-renvoie) en tire la précision d'un jeu lu plusieurs fois par un agent appelé.

Auparavant, la précision la plus fine l'emportait : un run qui avait lu la table entière puis nommé des colonnes ne gardait que ces colonnes. Les arêtes déjà enregistrées ainsi le restent. La [reconstruction du graphe](workers.md#reconstruire-le-graphe) (`answerlineage-worker rebuild --yes`) les fusionne de nouveau, selon cette règle, à partir des événements bruts conservés ; celles dont les événements sont purgés ne peuvent pas être reprises ([ce que la reconstruction ne rend pas](workers.md#ce-que-la-reconstruction-ne-rend-pas)).

## Compteur de doublons

`ingest_duplicate` compte les événements reçus alors qu'ils étaient déjà connus. Sa clé est l'émetteur (l'espace de noms du job, sous sa forme normalisée) et le jour UTC de réception ; `duplicates` est strictement positif : une ligne n'existe qu'à partir du premier doublon. L'API l'écrit dans la transaction qui reçoit l'événement, par un ajout atomique (`INSERT … ON CONFLICT DO UPDATE`), et le lit par `GET /api/v1/ingestion/duplicates` (voir [API d'ingestion](ingestion.md)). Un index range les jours du plus récent au plus ancien. La table est partitionnée par mois du jour de réception et purgée avec la conservation ([partitions](#partitions-et-conservation)).

## Couverture du lignage

Les [relevés de couverture](ingestion.md#relevés-de-couverture) des émetteurs se rangent dans `coverage_report`, un par émetteur (sous sa forme normalisée), instance et fenêtre. L'instance d'un relevé qui n'en porte pas est le texte vide, une instance à part ; une instance compte au plus 255 caractères (`ck_coverage_report_instance`). Deux relevés de la même instance et de la même fenêtre se fusionnent compteur par compteur, au plus grand : l'écriture est idempotente et commutative. La table est partitionnée par mois de première réception de la fenêtre (`recorded_month`) : un relevé republié est d'abord fusionné dans la ligne existante, quelle que soit sa partition, par une mise à jour qui ne la déplace pas ; une fenêtre nouvelle est insérée dans le mois courant, et deux premières réceptions simultanées s'y fusionnent par la clé primaire (`INSERT … ON CONFLICT DO UPDATE`). Les compteurs sont positifs et la fenêtre non vide, contraintes nommées comprises.

`coverage_emitter` tient un émetteur par ligne, dès son premier relevé : `last_report_at`, la réception de son dernier relevé qui a changé quelque chose, et, posés par le [contrôle des émetteurs muets](workers.md#émetteurs-muets), `silent_since` (l'heure de ce dernier relevé quand le silence a été signalé) et `signalled_at`. Un émetteur est muet tant que `silent_since` égale `last_report_at`.

Pour comparer ces relevés au graphe, `event_raw` porte l'émetteur de chaque événement (`emitter`, l'espace de noms de son job sous sa forme NFC) et `tool_call`, vrai pour l'événement d'un appel d'outil. Un index partiel range les appels par émetteur et par heure. La migration `0006` remplit ces colonnes pour les événements reçus avant elle. Un événement est dans le graphe quand il n'est plus dans `ingest_queue`.

La migration `0009` ajoute l'instance : les relevés reçus avant elle prennent l'instance vide et gardent leur place, et la clé primaire devient émetteur, instance, fenêtre et mois de réception. Sa descente réunit les instances d'une même fenêtre, reçues le même mois, en une ligne dont chaque compteur est leur somme.

Un relevé par minute et par instance fait 1 440 lignes par jour : `coverage_report` et `event_raw` sont purgés avec la conservation, au même rythme, pour que la couverture d'une période purgée soit inconnue plutôt que fausse. `coverage_emitter`, une ligne par émetteur, n'est pas purgée.

## Partitions et conservation

Les tables qui grossissent avec le trafic sont partitionnées par mois, créées à l'avance et purgées selon une durée de conservation, sans intervention : la [maintenance des workers](workers.md#partitions-mensuelles) s'en charge.

| Table | Clé de partition | Clé primaire |
| --- | --- | --- |
| `event_raw` | `recorded_month`, mois UTC de réception | empreinte de l'événement, mois |
| `coverage_report` | `recorded_month`, mois UTC de première réception de la fenêtre | émetteur, fenêtre, mois |
| `ingest_duplicate` | `day`, jour UTC de réception | émetteur, jour |

Une partition couvre un mois UTC, du premier jour inclus au premier jour du mois suivant exclu, et porte le nom de sa table et de son mois : `event_raw_2026_11`. Les index et les contraintes de la table valent pour chaque partition.

- **La clé de partition suit la réception, jamais l'heure de l'événement.** Elle est posée par la base à l'insertion, avec `recorded_at`, et une contrainte nommée (`ck_event_raw_recorded_month`, `ck_coverage_report_recorded_month`) la tient égale au mois UTC de `recorded_at`. Un émetteur peut dater un événement de 1970 : il est rangé dans le mois où il a été reçu.
- **Aucune mise à jour ne déplace une ligne.** La clé n'est jamais réécrite : une fusion de relevés ou un doublon compté modifient la ligne dans sa partition.
- **Les écritures restent idempotentes.** PostgreSQL exige que la clé primaire d'une table partitionnée contienne sa clé de partition : un événement déjà reçu est donc cherché dans toutes les partitions avant d'être enregistré, et la clé primaire départage deux réceptions simultanées du même mois. Une fenêtre de relevé se cherche de même.
- **Aucune écriture n'échoue faute de partition.** Les migrations créent le mois courant et les deux suivants ; la maintenance garde ensuite ces mois d'avance, et son contrôle signale, sept jours avant, un mois qui commence sans partition (`partition_alert`).
- **La file de traitement et la file de rebut n'ont pas de clé étrangère vers `event_raw`** : une clé étrangère vers une table partitionnée empêcherait d'en supprimer une partition. `ingest_queue` et `dead_letter` désignent l'événement par son identifiant et son mois de réception (`recorded_month`) ; la purge retire de l'une et de l'autre les événements du mois qu'elle supprime, jamais projetés.

### Le graphe n'est pas partitionné

Les tables du graphe gardent toutes leurs lignes : elles sont hors de la conservation. Leurs écritures fusionnent les événements d'un run sur des clés naturelles (run, jeu de données, version ; run et empreinte ; diffusion et destinataire) qui ne contiennent aucune heure de réception. Partitionner par heure d'enregistrement obligerait à l'ajouter à ces clés : deux événements d'une même lecture, reçus deux mois différents, donneraient deux arêtes au lieu d'une, et la fusion cesserait d'être idempotente et commutative. Partitionner par heure d'événement est exclu : elle vient de l'émetteur.

Une purge ligne à ligne du graphe demanderait de dire quand une réponse est trop ancienne pour être rappelée, alors que ses lectures, ses diffusions et ses jeux de données sont partagés avec d'autres : c'est une décision du produit, pas de l'exploitation. Le graphe ne garde que des noms, des empreintes et des heures, bien moins volumineux que les événements bruts. Son heure d'enregistrement, la réception de l'événement qui a fait connaître chaque ligne, permettra de le purger le jour où cette règle sera fixée. En attendant, il se [reconstruit](workers.md#reconstruire-le-graphe) à partir des événements conservés ; ce que la conservation a purgé n'y revient pas.

### L'état du contrôle

`partition_alert` tient une ligne par partition manquante signalée : sa table, son mois, et l'heure du signalement. La ligne disparaît quand la partition existe, ou quand le mois est passé.

```sql
SELECT table_name, month, signalled_at FROM partition_alert ORDER BY month, table_name;
```

Les partitions existantes se lisent dans le catalogue :

```sql
SELECT p.relname AS table_name, c.relname AS partition, pg_get_expr(c.relpartbound, c.oid) AS bounds
FROM pg_inherits i
JOIN pg_class c ON c.oid = i.inhrelid
JOIN pg_class p ON p.oid = i.inhparent
WHERE p.relname IN ('event_raw', 'coverage_report', 'ingest_duplicate')
ORDER BY 1, 2;
```

### Reprise des tables existantes

La migration `0008` recrée chaque table partitionnée : ses lignes sont mises de côté dans une copie temporaire, la table est recréée, une partition est créée pour chaque mois où une ligne a été reçue jusqu'aux deux mois suivant le mois courant, puis les lignes sont recopiées avec leur mois de réception. La file reçoit le mois de chaque événement. La descente recrée les tables sans partition et la clé étrangère de la file ; deux lignes d'une même clé reçues à cheval sur deux mois s'y réunissent.

### Limites connues

- Deux réceptions simultanées d'un même événement, à cheval sur deux mois à quelques millisecondes près, ne se voient pas l'une l'autre : l'événement est enregistré deux fois, une fois dans chaque partition. Le graphe n'en est pas changé (la projection est idempotente) ; le doublon n'est pas compté, et la couverture de sa fenêtre compte l'appel deux fois, au plus jusqu'aux appels vus. Une fenêtre de relevé reçue ainsi a deux lignes ; la lecture les réunit au plus grand, comme la réception.
- Un relevé reçu juste après un changement de mois survit un mois de plus que les événements de sa fenêtre : pendant ce mois, la couverture de cette fenêtre, la plus ancienne de la conservation, compte ses appels comme manquants.

## File de rebut

Un événement que la projection n'a pas pu intégrer au graphe quitte `ingest_queue` pour `dead_letter`, dans la transaction du lot ; un rejeu fait le chemin inverse, dans une transaction aussi ([workers](workers.md#file-de-rebut)). Un événement est donc dans l'une ou dans l'autre, jamais dans les deux, et sa ligne d'`event_raw` reste : le rebut la désigne, il ne la recopie pas.

| Table | Contraintes |
| --- | --- |
| `ingest_queue` | `first_failed_at`, l'heure du premier essai en échec, est remplie dès le premier échec, et seulement alors (`ck_ingest_queue_first_failure`) |
| `dead_letter` | Un événement une fois (clé primaire, son identifiant). Motif de la liste fermée (`ck_dead_letter_reason`) ; un essai au moins (`ck_dead_letter_attempts`) ; le premier échec ne suit pas le dernier (`ck_dead_letter_failures`) |

`dead_letter.emitter` est l'émetteur d'`event_raw`, sous sa forme normalisée : la même définition que le compteur de doublons et la couverture. `reason` est un code, jamais le message d'une exception ; ajouter un motif demande une révision de la base, qui étend sa contrainte. Un index range le rebut par heure du dernier échec, pour ses pages ; un autre par émetteur et par motif, pour ses décomptes.

La migration `0011` crée la table et ajoute `first_failed_at` à la file. Un événement déjà remis à plus tard n'avait pas d'heure de premier échec : il prend son heure de mise en file, la plus ancienne possible. La descente remet en file les événements du rebut, avec leurs essais et leur motif pour dernière erreur, avant de retirer la table : aucun n'est perdu.

## File de réémission

Quand les workers ont une destination de [réémission](reemission.md), un événement que la projection intègre au graphe quitte `ingest_queue` pour `reemission_queue`, dans la transaction du lot ; il en sort dans la transaction qui enregistre l'issue de son envoi, avec les compteurs de `reemission_stats`. Comme la file de traitement, la file de réémission désigne l'événement brut par son identifiant et son mois de réception, sans clé étrangère ni copie ; la purge retire les événements du mois qu'elle supprime, et les compte perdus.

| Table | Contraintes |
| --- | --- |
| `ingest_queue` | `reemit` : vrai pour un événement jamais entré au graphe, qui partira une fois projeté ; la reconstruction du graphe remet les événements en file à faux |
| `reemission_queue` | Un événement une fois (`uq_reemission_queue_event_id`). Des essais positifs (`ck_reemission_queue_attempts`) ; la classe du dernier échec, ou le statut de la réponse, dès le premier échec, et seulement alors (`ck_reemission_queue_last_error`) |
| `reemission_stats` | Une seule ligne (`ck_reemission_stats_single_row`), des compteurs positifs (`ck_reemission_stats_counts`) |

Un index range la file par heure de mise en file, pour son âge et sa borne d'âge ; un index partiel tient les événements en reprise. `last_error` est un code, `HTTP 503` ou `ConnectionRefusedError`, jamais un message.

La migration `0013` crée les deux tables et la colonne ; sa descente les retire : ce qui attendait d'être réémis ne l'est pas, le graphe et la file de traitement ne changent pas.

## Incidents

Un [incident de données](incidents.md) se range à côté du graphe, sans clé vers lui : le graphe se reconstruit à partir des événements, et ses identifiants internes changeraient. Les réponses touchées se désignent donc comme l'identifiant stable d'une réponse, par leur run et leur empreinte ; un jeu de données du périmètre, par son espace de noms et son nom. Aucune de ces tables ne porte de texte libre : des identifiants, des heures, des décomptes et des codes de listes fermées.

| Table | Contraintes |
| --- | --- |
| `incident` | État (`ck_incident_state`) et gravité (`ck_incident_severity`) de leurs listes fermées ; fenêtre non vide (`ck_incident_window`) de 31 jours au plus (`ck_incident_window_length`) |
| `incident_dataset` | Un jeu de données une fois par incident, clé étrangère vers l'incident |
| `incident_analysis` | Une demande par incident, écrite dans la transaction de la déclaration. État (`ck_incident_analysis_state`) et code d'échec (`ck_incident_analysis_error`) de leurs listes fermées ; une analyse en échec porte son code (`ck_incident_analysis_failure`) ; seule une analyse en attente n'a pas d'heure de fin (`ck_incident_analysis_finished`) ; les colonnes du résultat sont remplies pour une analyse faite, et seulement pour elle (`ck_incident_analysis_result`) |
| `incident_answer` | Une réponse une fois par incident, clé étrangère vers l'incident |
| `incident_recipient` | Un pseudonyme une fois par réponse touchée, clé étrangère vers elle |
| `incident_quarantine` | Clé étrangère vers l'incident. Mode (`ck_incident_quarantine_mode`) et état (`ck_incident_quarantine_state`) de leurs listes fermées ; une quarantaine levée, et elle seule, a son heure de levée (`ck_incident_quarantine_lifted`), qui suit l'activation (`ck_incident_quarantine_lifted_after`) ; une quarantaine active au plus par incident (`uq_incident_quarantine_active`, un index unique partiel) ; les transitions d'une liste fermée (`ck_incident_quarantine_transition`) |

Un index partiel range les demandes en attente par heure de reprise : c'est ce que les [workers](workers.md#analyse-des-incidents) réclament. Un autre range les incidents par heure de déclaration, pour la liste, et un troisième les réponses d'un incident par heure, pour leurs pages. Un dernier range les quarantaines d'un incident, la dernière d'abord.

La [quarantaine](incidents.md#quarantaine) est un objet à part, rattaché à l'incident : une ligne par activation, que sa levée complète et qui ne change plus ensuite. Ses heures sont posées par la base. Ses transitions sont tenues par une contrainte nommée que la base vérifie à chaque écriture, une contrainte de déclencheur (`CREATE CONSTRAINT TRIGGER`) et sa fonction `incident_quarantine_transition()` : une ligne naît active ; seule une ligne active se lève, sans changer d'incident, de mode ni d'heure d'activation ; une ligne levée ne change plus. Toute autre écriture est refusée comme une contrainte de vérification (`check_violation`), sous le nom de la contrainte. L'API n'en active pas plus de 100 à la fois, sous un verrou de la table pris par l'activation.

`incident_analysis.coverage` garde la couverture du lignage de la fenêtre à l'heure de l'analyse, sous la forme que rend l'API (état, taux, compteurs, trous) ; `unknown_datasets`, les jeux du périmètre que le graphe ne connaissait pas. Ce sont des colonnes `jsonb` : un instantané, qu'aucune requête ne parcourt.

Les incidents ne sont ni partitionnés ni purgés : ils sont rares, et un rappel s'appuie sur eux. La migration `0010` crée ces tables ; sa descente les retire, incidents compris. La migration `0015` crée `incident_quarantine`, sa contrainte de transitions et sa fonction ; sa descente les retire, les quarantaines perdues, les incidents inchangés.

## Propositions d'incident et signaux de qualité

Les [propositions d'incident](incidents.md#incidents-proposés-par-un-test-de-qualité) et les signaux de qualité d'où elles naissent se rangent à côté des incidents, hors du graphe : une reconstruction ne les vide pas, et rejoue les événements sans rien relever deux fois. Aucune de ces tables ne porte de texte libre : des identifiants contrôlés, des classes d'assertions et des gravités de listes fermées, des heures et des décomptes.

| Table | Contraintes |
| --- | --- |
| `quality_signal` | Un signal par run, jeu de données et colonne (`''` pour la table entière) : la clé rend l'écriture idempotente, et deux écritures du même signal réunissent leurs classes et gardent la plus forte gravité. Classes d'une à huit, prises dans la liste fermée (`ck_quality_signal_classes`) ; gravité et état de leurs listes fermées (`ck_quality_signal_severity`, `ck_quality_signal_state`) ; un signal rattaché ou en sourdine, et lui seul, désigne sa proposition (`ck_quality_signal_proposal`) |
| `quality_column` | La dernière réussite et le dernier échec connus de chaque colonne testée, écrits au plus grand (`ck_quality_column_known`) |
| `quality_stats` | Une ligne par étape, `projection` et `proposals` (`ck_quality_stats_stage`), pour que l'une n'attende pas l'autre ; des compteurs positifs (`ck_quality_stats_counts`) |
| `incident_proposal` | État, gravité, base de la fenêtre et classes de leurs listes fermées (`ck_incident_proposal_state`, `ck_incident_proposal_severity`, `ck_incident_proposal_window_basis`, `ck_incident_proposal_classes`) ; le premier échec précède le dernier (`ck_incident_proposal_failed`), la fenêtre déduite commence avant lui (`ck_incident_proposal_window`) ; seule une proposition signalée n'a pas d'heure de décision (`ck_incident_proposal_decided`), qui suit la proposition (`ck_incident_proposal_decided_after`) ; une proposition confirmée, et elle seule, désigne son incident (`ck_incident_proposal_confirmed`), un incident au plus une proposition (`uq_incident_proposal_incident_id`) ; une proposition signalée au plus par jeu (`uq_incident_proposal_open`, un index unique partiel) ; les transitions d'une liste fermée (`ck_incident_proposal_transition`) |
| `incident_proposal_column` | Les colonnes en échec d'une proposition, nommées (`ck_incident_proposal_column_named`), clé étrangère vers elle |

Un index partiel range les signaux en attente par heure, pour les workers ; un autre ceux d'une proposition, pour compter ses runs ; un troisième les signaux par heure de réception, pour leur purge. Les propositions se rangent par heure de proposition, pour la liste, et par jeu et heure de décision, pour la sourdine.

Comme la quarantaine, les transitions d'une proposition sont tenues par une contrainte de déclencheur nommée et sa fonction `incident_proposal_transition()` : une ligne naît signalée ; seule une proposition signalée change, sans changer de jeu, d'heure de proposition, de premier échec ni de début de fenêtre ; une décision est définitive. Les colonnes s'appellent `column_name` : `column` est un mot réservé de SQL.

La migration `0016` crée ces tables, leur contrainte de transitions et sa fonction ; sa descente les retire : propositions et signaux perdus, incidents inchangés.

## File des webhooks

Quand un [webhook](webhooks.md) est en service, chaque changement d'état d'un incident écrit son message dans `webhook_queue`, dans la transaction qui le fait : celle de la déclaration, celle qui enregistre la fin de l'analyse, celle qui active ou lève la quarantaine. Le message y est tel qu'il partira, figé à l'heure du changement : la sorte d'événement, l'incident, son état, sa gravité, l'état de son analyse, son code d'échec et ses décomptes, l'état et le mode de sa dernière quarantaine. Il en sort dans la transaction qui enregistre l'issue de son envoi, avec les compteurs de `webhook_stats`.

| Table | Contraintes |
| --- | --- |
| `webhook_queue` | Un identifiant de livraison par message, tiré par la base (`uq_webhook_queue_delivery_id`), clé étrangère vers l'incident. Sorte d'événement, état de l'incident, gravité et code d'échec de leurs listes fermées (`ck_webhook_queue_event_type`, `ck_webhook_queue_incident_state`, `ck_webhook_queue_severity`, `ck_webhook_queue_analysis_error`) ; chaque sorte d'événement de l'analyse annonce l'état d'analyse qui est le sien (`ck_webhook_queue_analysis_state`), et chaque sorte d'événement de la quarantaine l'état de quarantaine qui est le sien (`ck_webhook_queue_quarantine_event`) ; état et mode de la quarantaine de leurs listes fermées (`ck_webhook_queue_quarantine_state`, `ck_webhook_queue_quarantine_mode`), donnés ensemble ou pas du tout (`ck_webhook_queue_quarantine_given`) ; une analyse en échec, et elle seule, porte son code (`ck_webhook_queue_failure`) ; une analyse faite, et elle seule, porte ses décomptes (`ck_webhook_queue_counts_given`), positifs (`ck_webhook_queue_counts`) ; la classe du dernier échec, ou le statut de la réponse, dès le premier échec, et seulement alors (`ck_webhook_queue_last_error`) |
| `webhook_stats` | Une seule ligne (`ck_webhook_stats_single_row`), des compteurs positifs (`ck_webhook_stats_counts`). `in_service_until` : un worker qui a un webhook la repousse à chaque passage ; tant qu'elle n'est pas passée, un changement d'état est mis en file |

Un index range la file par heure de mise en file, pour son âge et sa borne d'âge ; un index partiel tient les messages en reprise. Aucune colonne ne porte de texte libre ni de pseudonyme : des identifiants, des codes, des heures et des nombres.

La migration `0014` crée les deux tables ; sa descente les retire : ce qui attendait d'être annoncé ne l'est pas, les incidents ne changent pas. La migration `0015` y ajoute les événements de la quarantaine et ses deux colonnes ; sa descente retire de la file les annonces de quarantaine qui attendaient, puis les colonnes.

## Reconstructions du graphe

`graph_rebuild` garde une ligne par [reconstruction du graphe](workers.md#reconstruire-le-graphe) : tant qu'elle tourne, puis pour mémoire. Des heures et des décomptes, aucun contenu.

| Colonne | Contenu |
| --- | --- |
| `started_at`, `finished_at` | Le départ, et la fin ; nulle tant que la reconstruction n'est pas close |
| `queue_boundary` | La dernière ligne de la file au départ : la reconstruction est finie quand la file n'a plus aucune ligne jusqu'à elle |
| `replayed` | Événements remis à la projection : remis en file par la reconstruction, ou qui y attendaient |
| `dead_letters` | Événements au rebut au départ, laissés tels quels |
| `first_received`, `last_received` | Réceptions du premier et du dernier événement brut conservé ; nulles quand il n'y en avait aucun |
| `unrecoverable` | Lignes du graphe enregistrées avant le premier événement conservé, que la reconstruction ne rend pas telles quelles |

Les décomptes sont positifs (`ck_graph_rebuild_counts`), la fin suit le départ (`ck_graph_rebuild_finished`), les deux réceptions sont données ensemble et dans l'ordre (`ck_graph_rebuild_received`). Une reconstruction inachevée a une ligne sans fin : la suivante la reprend. Tant que la file garde des lignes jusqu'à sa `queue_boundary`, les workers n'analysent aucun incident.

La migration `0012` crée la table ; sa descente la retire, sans toucher au graphe ni à la file.

## Journal des révélations

La [révélation d'un destinataire](securite/pseudonymes.md#la-révélation) laisse une ligne dans `recipient_revelation`, et rien d'autre : ni l'identité révélée, ni les identités candidates. Aucune table ne relie un pseudonyme à une personne.

| Colonne | Contenu |
| --- | --- |
| `id` | Numéro de la révélation, tiré par la base |
| `revealed_at` | Heure de la révélation, posée par la base à l'insertion |
| `author_issuer`, `author_subject` | L'auteur : l'émetteur OIDC et le sujet (`sub`) de son jeton |
| `reason` | Motif, d'une liste fermée |
| `pseudonym` | Pseudonyme visé |
| `candidates` | Nombre d'identités candidates |
| `result` | Ce qu'il en est sorti |

| Contrainte | Ce qu'elle tient |
| --- | --- |
| `ck_recipient_revelation_reason` | Le motif : `recall`, `data_subject_request`, `legal_obligation`, `incident_investigation` |
| `ck_recipient_revelation_result` | Le résultat : `matched`, `no_match`, `unknown_recipient` |
| `ck_recipient_revelation_pseudonym` | La forme d'un pseudonyme, `^hmac-sha256:[0-9a-f]{64}$` |
| `ck_recipient_revelation_candidates` | De 1 à 100 candidates |
| `ck_recipient_revelation_author` | L'émetteur de 1 à 1 024 caractères, le sujet de 1 à 255 |
| `ck_recipient_revelation_append_only` | L'ajout seul : une modification, une suppression ou une troncature est refusée comme une contrainte (`check_violation`), sous ce nom |

L'ajout seul est tenu par la base, pour toute connexion, propriétaire compris : une fonction, `recipient_revelation_append_only()`, et trois déclencheurs. `recipient_revelation_stamped`, avant chaque insertion, pose l'heure de la transaction, quoi que l'insertion porte ; `recipient_revelation_append_only`, avant chaque modification ou suppression, et `recipient_revelation_not_truncated`, avant une troncature, les refusent. Les droits `UPDATE`, `DELETE` et `TRUNCATE` sur la table sont aussi retirés à tous, propriétaire compris. Un index range les révélations d'un auteur par heure : c'est ce que compte la borne de débit.

Le pseudonyme est une valeur, sans clé étrangère vers `recipient` : le journal survit à la reconstruction du graphe, qui vide `recipient`. Il n'est ni partitionné ni purgé.

La migration `0017` crée la table, sa fonction et ses déclencheurs. Elle retire aussi de la base ce qu'un émetteur avait pu envoyer comme destinataire sous une autre forme qu'un pseudonyme, peut-être une identité en clair : des événements bruts (les destinataires de leurs facettes de diffusion, sous `facets` comme sous `outputFacets` ; une liste qui n'en est pas une est vidée), du graphe (le destinataire et ses liens) et des résultats d'analyse des incidents. Les décomptes des analyses passées restent ceux de leur heure. La descente retire le journal, sa fonction et ses déclencheurs ; les destinataires retirés ne reviennent pas.

## Listes de rappel

La [liste de rappel](rappel.md) d'un incident se range à côté de l'incident, sans clé vers le graphe, comme le résultat de son analyse dont elle est faite. Elle en est un instantané : elle tient ses propres réponses, par leur run et leur empreinte, que rejouer l'analyse refait ou retire. Aucune de ces tables ne porte de texte libre : des pseudonymes, des identifiants, des heures et des nombres.

| Table | Contraintes |
| --- | --- |
| `recall_list` | Une liste par incident, clé étrangère vers lui. `built_at`, l'heure de la transaction qui l'a faite, posée par la base ; `reference_at`, l'heure de référence de l'exposition, celle du graphe lu par l'analyse. Des décomptes positifs (`ck_recall_list_counts`) |
| `recall_entry` | Un pseudonyme une fois par liste (clé primaire), un rang une fois (`uq_recall_entry_incident_id_rank`), clé étrangère vers la liste. Un rang d'au moins 1, une exposition positive en millionièmes, une réponse touchée au moins (`ck_recall_entry_counts`) |
| `recall_entry_answer` | Une réponse une fois par destinataire, clé étrangère vers son entrée |

La migration `0018` crée ces tables ; sa descente les retire, les incidents et leurs analyses inchangés. Une analyse faite avant elle n'a pas de liste : la rejouer en fait une.

### Décisions et envois

La [décision](rappel.md#décider-du-rappel) d'un responsable d'agent sur une liste, et sa [remontée](rappel.md#la-remontée) faute de décision, laissent une ligne dans `recall_decision`. Le journal désigne l'incident, sans clé vers la liste : il survit à une liste qu'une analyse rejouée retire.

| Colonne | Contenu |
| --- | --- |
| `id` | Numéro de la ligne, tiré par la base |
| `incident_id` | L'incident dont la liste est décidée ou remonte, clé étrangère vers lui |
| `decided_at` | L'heure, posée par la base à l'insertion, quoi que l'insertion porte |
| `decision` | `validated`, `modified`, `refused`, ou `escalated` pour une remontée |
| `author_issuer`, `author_subject` | L'auteur : l'émetteur OIDC et le sujet (`sub`) de son jeton ; nuls pour une remontée |
| `template` | Le gabarit de ce qui partira : `standard`, `information`, `decision_review` ; pour une liste validée ou modifiée seulement |
| `reason` | Le motif d'un refus : `not_affected`, `already_informed`, `no_impact`, `duplicate_incident` |

| Contrainte | Ce qu'elle tient |
| --- | --- |
| `ck_recall_decision_decision`, `ck_recall_decision_template`, `ck_recall_decision_reason` | La décision, le gabarit et le motif, de leurs listes fermées |
| `ck_recall_decision_author` | Une décision a son auteur, émetteur et sujet ensemble, de 1 à 1 024 et de 1 à 255 caractères ; une remontée n'en a pas |
| `ck_recall_decision_given` | Un gabarit pour une liste validée ou modifiée, et pour elle seule ; un motif pour un refus, et pour lui seul |
| `uq_recall_decision_decided`, `uq_recall_decision_escalated` | Une décision au plus par liste, une remontée au plus : deux index uniques partiels |
| `ck_recall_decision_append_only` | L'ajout seul : une modification, une suppression ou une troncature est refusée comme une contrainte (`check_violation`), sous ce nom |

L'ajout seul est tenu par la base, comme celui du [journal des révélations](#journal-des-révélations) : une fonction, `recall_decision_append_only()`, et trois déclencheurs, qui posent l'heure à l'insertion et refusent le reste ; les droits `UPDATE`, `DELETE` et `TRUNCATE` sont retirés à tous, propriétaire compris.

`recall_dispatch` tient les messages de rappel mis à l'envoi : un par destinataire et par incident (`uq_recall_dispatch_incident_id_pseudonym`), chacun une entrée de la liste (clé étrangère vers `recall_entry`), et l'heure de sa mise à l'envoi. Rien ne l'écrit encore. Deux règles sont tenues par la base, pour toute connexion, propriétaire compris :

| Contrainte | Ce qu'elle tient |
| --- | --- |
| `ck_recall_dispatch_validated` | Aucun message sans une liste validée ou modifiée : une contrainte de déclencheur (`CREATE CONSTRAINT TRIGGER`) et sa fonction `recall_dispatch_validated()` refusent toute autre écriture, comme une contrainte de vérification (`check_violation`), sous ce nom |
| `ck_recall_list_frozen` | Une liste décidée ne change plus : la fonction `recall_list_frozen()` et un déclencheur sur `recall_list`, `recall_entry` et `recall_entry_answer` refusent toute écriture dans une liste qui porte une décision ; une remontée ne la fige pas |

La migration `0019` crée les deux tables, leurs fonctions et leurs déclencheurs ; sa descente les retire : les décisions journalisées sont perdues, les listes inchangées.

## Migrations

```bash
ANSWERLINEAGE_DATABASE_URL=postgresql://utilisateur:motdepasse@hote/base uv run python -m answerlineage_api.store.migrate upgrade
```

```bash
ANSWERLINEAGE_DATABASE_URL=postgresql://utilisateur:motdepasse@hote/base uv run python -m answerlineage_api.store.migrate downgrade base
```

Une migration nomme ses contraintes explicitement et fournit sa descente.
