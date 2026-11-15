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
| `read_edge` | run, jeu de données, version | Niveau de précision, colonnes lues, origine (analyse ou réconciliation) |
| `write_edge` | run, jeu de données, version | Sortie produite par le run |
| `answer` | run et empreinte | Empreinte de la réponse, jamais son contenu |
| `delivery` | réponse, canal, référence du message | Diffusion de la réponse |
| `recipient` | pseudonyme | Destinataire pseudonymisé, sans table de correspondance |
| `event_raw` | empreinte de l'événement | Événement OpenLineage reçu tel quel, rejouable |
| `ingest_queue` | événement | File de traitement : événements à projeter dans le graphe |

Chaque arête porte `event_time`, l'heure déclarée par l'émetteur, et `recorded_at`, l'heure posée par la base.

## Écrire dans le graphe

`answerlineage_api.store.graph.record_run` enregistre un run et ses arêtes dans la transaction en cours. L'écriture est idempotente et commutative : rejouer un enregistrement, ou recevoir deux événements d'un même run dans le désordre, donne le même graphe. Deux lectures d'un jeu de données par un même run se fusionnent en une arête ; la précision la plus fine l'emporte et les colonnes s'additionnent.

## Migrations

```bash
ANSWERLINEAGE_DATABASE_URL=postgresql://utilisateur:motdepasse@hote/base uv run python -m answerlineage_api.store.migrate upgrade
```

```bash
ANSWERLINEAGE_DATABASE_URL=postgresql://utilisateur:motdepasse@hote/base uv run python -m answerlineage_api.store.migrate downgrade base
```

Une migration nomme ses contraintes explicitement et fournit sa descente. Les tables ne sont pas encore partitionnées ni purgées.
