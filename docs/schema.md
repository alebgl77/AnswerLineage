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
| `recipient` | pseudonyme | Destinataire pseudonymisé, sans table de correspondance |
| `event_raw` | empreinte de l'événement | Événement OpenLineage reçu tel quel, rejouable |
| `ingest_queue` | événement | File de traitement : événements à projeter dans le graphe |
| `ingest_duplicate` | émetteur et jour | Nombre d'événements reçus en double, par émetteur et par jour UTC |
| `coverage_report` | émetteur, début et fin de fenêtre | Relevé de couverture : appels vus, événements émis et perdus, requêtes non inspectées |
| `coverage_emitter` | émetteur | Émetteur connu : réception de son dernier relevé, silence signalé |
| `partition_alert` | table et mois | Partition manquante signalée par le contrôle des workers |

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

`answerlineage_api.store.graph.record_run` enregistre un run et ses arêtes dans la transaction en cours, avec la réception de l'événement qui les fait connaître (`RunRecord.recorded_at` ; sans elle, l'heure de la transaction). L'écriture est idempotente et commutative : rejouer un enregistrement, ou recevoir deux événements d'un même run dans le désordre, donne le même graphe, heures comprises. Deux lectures d'un jeu de données par un même run se fusionnent en une arête ; la précision la plus fine l'emporte, les colonnes s'additionnent et l'intervalle des heures s'étend.

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
- **La file de traitement n'a pas de clé étrangère vers `event_raw`** : une clé étrangère vers une table partitionnée empêcherait d'en supprimer une partition. `ingest_queue` désigne l'événement par son identifiant et son mois de réception (`recorded_month`) ; la purge retire de la file les événements du mois qu'elle supprime, jamais projetés.

### Le graphe n'est pas partitionné

Les tables du graphe gardent toutes leurs lignes : elles sont hors de la conservation. Leurs écritures fusionnent les événements d'un run sur des clés naturelles (run, jeu de données, version ; run et empreinte ; diffusion et destinataire) qui ne contiennent aucune heure de réception. Partitionner par heure d'enregistrement obligerait à l'ajouter à ces clés : deux événements d'une même lecture, reçus deux mois différents, donneraient deux arêtes au lieu d'une, et la fusion cesserait d'être idempotente et commutative. Partitionner par heure d'événement est exclu : elle vient de l'émetteur.

Une purge ligne à ligne du graphe demanderait de dire quand une réponse est trop ancienne pour être rappelée, alors que ses lectures, ses diffusions et ses jeux de données sont partagés avec d'autres : c'est une décision du produit, pas de l'exploitation. Le graphe ne garde que des noms, des empreintes et des heures, bien moins volumineux que les événements bruts. Son heure d'enregistrement, la réception de l'événement qui a fait connaître chaque ligne, permettra de le purger le jour où cette règle sera fixée. En attendant, il se reconstruit à partir des événements conservés.

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

## Migrations

```bash
ANSWERLINEAGE_DATABASE_URL=postgresql://utilisateur:motdepasse@hote/base uv run python -m answerlineage_api.store.migrate upgrade
```

```bash
ANSWERLINEAGE_DATABASE_URL=postgresql://utilisateur:motdepasse@hote/base uv run python -m answerlineage_api.store.migrate downgrade base
```

Une migration nomme ses contraintes explicitement et fournit sa descente.
