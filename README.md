# AnswerLineage

**Quand une donnée est fausse, quelles réponses d'agents IA l'ont utilisée, et qui les a reçues ?**

Un tableau de bord faux se corrige au rechargement suivant. Une réponse d'agent fausse a déjà été lue, copiée dans un e-mail ou utilisée pour décider, et rien ne permet de la retrouver. AnswerLineage relie chaque réponse d'agent aux données qu'elle a lues et aux personnes qui l'ont reçue, pour :

- **remonter** d'une réponse à ses sources : tables, colonnes, outils, agents ;
- **propager** un incident de données jusqu'aux réponses et aux destinataires touchés ;
- **rappeler** ces destinataires, après validation humaine.

Le lignage est capté sans modifier les agents, normalisé au format [OpenLineage](https://openlineage.io) et rangé dans PostgreSQL.

> Projet en cours de construction. Le tableau « État du projet » dit ce qui fonctionne aujourd'hui. Les interfaces peuvent changer avant la première version.

## Comment ça marche

```mermaid
flowchart LR
    agent[Agent IA] -->|appel d'outil| proxy[Proxy AnswerLineage]
    proxy -->|relayé à l'identique| serveur[Serveur MCP]
    serveur --> donnees[(Données de l'entreprise)]
    proxy -.->|événement OpenLineage| api[API d'ingestion]
    api --> graphe[(PostgreSQL : graphe de lignage)]
```

Le proxy se place devant un serveur [MCP](https://modelcontextprotocol.io) existant. Il relaie chaque appel d'outil octet pour octet et décrit, à côté de l'appel, ce que l'agent a lu : quel outil, quelles tables, quelles colonnes. Ces événements alimentent un graphe qui va du jeu de données jusqu'au destinataire de la réponse.

## Principes

- **Aucun contenu conservé.** Ni prompt, ni résultat d'outil, ni réponse, ni jeton : des empreintes et des noms de jeux de données seulement.
- **Aucun modèle embarqué.** Un lignage qui sert de preuve doit être déterministe.
- **La capture ne bloque jamais un agent.** Le proxy décrit l'appel après l'avoir relayé ; une panne d'AnswerLineage n'arrête rien.
- **Standards ouverts.** OpenLineage pour les événements, MCP pour la capture : les catalogues de données en place restent utilisables.
- **Une seule dépendance : PostgreSQL.** Aucun service cloud propriétaire, installation possible hors ligne.

## État du projet

| Fonction | État |
| --- | --- |
| Proxy MCP transparent en HTTP, révisions 2026-07-28 et 2025-11-25 du protocole | Disponible |
| Tables et colonnes lues, déduites des requêtes SQL (BigQuery, Snowflake, PostgreSQL) | Disponible |
| API d'ingestion OpenLineage, par événement ou par lot | Disponible |
| Schéma du graphe de lignage dans PostgreSQL, avec ses migrations | Disponible |
| Envoi des événements du proxy vers l'API, alimentation du graphe | À venir |
| Remontée d'une réponse à ses sources, propagation d'un incident | À venir |
| Explorateur visuel, rappel des destinataires | À venir |

## Essayer

Prérequis : [uv](https://docs.astral.sh/uv/), Python 3.13 ou 3.14, Docker.

```bash
git clone https://github.com/alebgl77/AnswerLineage.git && cd AnswerLineage && make setup
```

### L'API d'ingestion

Un PostgreSQL jetable, le schéma, puis l'API sur `http://127.0.0.1:8000` :

```bash
docker run --rm -d --name answerlineage-postgres -e POSTGRES_PASSWORD=answerlineage -p 127.0.0.1:5432:5432 postgres:18
```

```bash
export ANSWERLINEAGE_DATABASE_URL=postgresql://postgres:answerlineage@127.0.0.1:5432/postgres
```

```bash
uv run python -m answerlineage_api.store.migrate upgrade
```

```bash
uv run uvicorn --factory answerlineage_api.app:create_app
```

Dans un autre terminal, un événement OpenLineage :

```bash
curl -X POST http://127.0.0.1:8000/api/v1/lineage -H 'content-type: application/json' -d '{
  "eventType": "COMPLETE",
  "eventTime": "2026-11-02T09:00:00Z",
  "producer": "https://example.com/mon-emetteur",
  "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent",
  "run": {"runId": "0193a9c5-7a10-7c3e-9d5e-2f1b6c8a4e77"},
  "job": {"namespace": "mcp://entrepot", "name": "execute_sql"},
  "inputs": [{"namespace": "bigquery", "name": "finance.ref.taux_change"}]
}'
```

L'API répond `201` avec l'identifiant de l'événement. Le même envoi répété répond `"duplicate": true` : un événement rejoué n'est enregistré qu'une fois. Un événement invalide est rejeté avec le chemin de chaque champ fautif. La description complète de l'API est servie sur `/api/v1/openapi.json`.

### Le proxy MCP

Devant un serveur MCP qui écoute sur le port 9000 et expose un outil `execute_sql` dont l'argument `query` porte du SQL BigQuery :

```bash
uv run answerlineage-proxy --upstream http://127.0.0.1:9000 --server-name entrepot --sql-tool execute_sql=query:bigquery
```

L'agent se connecte alors à `http://127.0.0.1:8717` au lieu du serveur : rien d'autre ne change pour lui.

## Documentation

- [Proxy MCP](docs/proxy.md) : ce qui est relayé, ce qui est lu, analyse du SQL, limites connues
- [API d'ingestion](docs/ingestion.md) : routes, lots, validation, rejeu
- [Schéma du graphe](docs/schema.md) : tables, écritures, migrations
- [Pipeline d'intégration continue](docs/ci.md) : contrôles, chaîne d'approvisionnement

## Développement

```bash
make check
```

`make check` enchaîne le contrôle de politique des workflows, le format, le lint, le typage strict, les tests et la porte de couverture. Les tests d'intégration démarrent un PostgreSQL réel avec Testcontainers.
