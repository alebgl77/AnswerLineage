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

`ingest_duplicate` compte les événements reçus alors qu'ils étaient déjà connus. Sa clé est l'émetteur (l'espace de noms du job, sous sa forme normalisée) et le jour UTC de réception ; `duplicates` est strictement positif : une ligne n'existe qu'à partir du premier doublon. L'API l'écrit dans la transaction qui reçoit l'événement, par un ajout atomique (`INSERT … ON CONFLICT DO UPDATE`), et le lit par `GET /api/v1/ingestion/duplicates` (voir [API d'ingestion](ingestion.md)). Un index range les jours du plus récent au plus ancien. La table n'est pas purgée.

## Couverture du lignage

Les [relevés de couverture](ingestion.md#relevés-de-couverture) des émetteurs se rangent dans `coverage_report`, un par émetteur (sous sa forme normalisée) et par fenêtre. Deux relevés de la même fenêtre se fusionnent par un ajout atomique (`INSERT … ON CONFLICT DO UPDATE`), compteur par compteur, au plus grand : l'écriture est idempotente et commutative. Les compteurs sont positifs et la fenêtre non vide, contraintes nommées comprises.

`coverage_emitter` tient un émetteur par ligne, dès son premier relevé : `last_report_at`, la réception de son dernier relevé qui a changé quelque chose, et, posés par le [contrôle des émetteurs muets](workers.md#émetteurs-muets), `silent_since` (l'heure de ce dernier relevé quand le silence a été signalé) et `signalled_at`. Un émetteur est muet tant que `silent_since` égale `last_report_at`.

Pour comparer ces relevés au graphe, `event_raw` porte l'émetteur de chaque événement (`emitter`, l'espace de noms de son job sous sa forme NFC) et `tool_call`, vrai pour l'événement d'un appel d'outil. Un index partiel range les appels par émetteur et par heure. La migration `0006` remplit ces colonnes pour les événements reçus avant elle. Un événement est dans le graphe quand il n'est plus dans `ingest_queue`.

Ces tables ne sont pas purgées : un relevé par minute et par émetteur fait 1 440 lignes par jour.

## Migrations

```bash
ANSWERLINEAGE_DATABASE_URL=postgresql://utilisateur:motdepasse@hote/base uv run python -m answerlineage_api.store.migrate upgrade
```

```bash
ANSWERLINEAGE_DATABASE_URL=postgresql://utilisateur:motdepasse@hote/base uv run python -m answerlineage_api.store.migrate downgrade base
```

Une migration nomme ses contraintes explicitement et fournit sa descente. Les tables ne sont pas encore partitionnées ni purgées.
