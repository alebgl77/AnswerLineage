# AnswerLineage

**Quand une donnée est fausse, quelles réponses d'agents IA l'ont utilisée, et qui les a reçues ?**

Un tableau de bord faux se corrige au rechargement suivant. Une réponse d'agent fausse a déjà été lue, copiée dans un e-mail ou utilisée pour décider, et rien ne permet de la retrouver. AnswerLineage relie chaque réponse d'agent aux données qu'elle a lues et aux personnes qui l'ont reçue, pour :

- **remonter** d'une réponse à ses sources : tables, colonnes, outils, agents ;
- **propager** un incident de données jusqu'aux réponses et aux destinataires touchés ;
- **rappeler** ces destinataires, après validation humaine.

Le lignage est capté sans modifier les agents, normalisé au format [OpenLineage](https://openlineage.io) et rangé dans PostgreSQL.

![L'explorateur d'AnswerLineage : une table fausse, les 16 réponses et les 13 destinataires touchés](docs/images/explorateur-propager.png)

> Projet en cours de construction. Le tableau « État du projet » dit ce qui fonctionne aujourd'hui. Les interfaces peuvent changer avant la première version.

## Comment ça marche

```mermaid
flowchart LR
    agent[Agent IA] -->|appel d'outil| proxy[Proxy AnswerLineage]
    proxy -->|relayé à l'identique| serveur[Serveur MCP]
    serveur --> donnees[(Données de l'entreprise)]
    proxy -.->|événement OpenLineage| api[API d'ingestion]
    api --> graphe[(PostgreSQL : graphe de lignage)]
    graphe --> lecture[API de lecture]
    lecture --> explorateur[Explorateur]
```

Le proxy se place devant un serveur [MCP](https://modelcontextprotocol.io) existant. Il relaie chaque appel d'outil octet pour octet et décrit, à côté de l'appel, ce que l'agent a lu : quel outil, quelles tables, quelles colonnes. Ces événements alimentent un graphe qui va du jeu de données jusqu'au destinataire de la réponse.

L'API de lecture interroge ce graphe dans les deux sens : d'une réponse vers ses sources, d'une donnée fausse vers les réponses et les personnes touchées. L'explorateur en donne la vue.

## Principes

- **Aucun contenu conservé.** Ni prompt, ni résultat d'outil, ni réponse, ni jeton : des empreintes et des noms de jeux de données seulement.
- **Aucun modèle embarqué.** Un lignage qui sert de preuve doit être déterministe.
- **La capture ne bloque jamais un agent.** Le proxy décrit l'appel après l'avoir relayé ; une panne d'AnswerLineage n'arrête rien.
- **Standards ouverts.** OpenLineage pour les événements, MCP pour la capture : les catalogues de données en place restent utilisables.
- **Écoute locale par défaut.** Le proxy et l'API refusent une requête dont l'hôte ou l'origine n'est pas attendu : une page web ouverte sur le poste ne les atteint pas.
- **Toute entrée est tenue pour hostile.** Un nom reçu n'entre dans le graphe que s'il a la forme d'un nom ; toute requête est bornée en taille, en profondeur et en durée ; une erreur ne renvoie jamais la valeur reçue.
- **Une seule dépendance : PostgreSQL.** Aucun service cloud propriétaire, installation possible hors ligne.

## État du projet

| Fonction | État |
| --- | --- |
| Proxy MCP transparent en HTTP, révisions 2026-07-28 et 2025-11-25 du protocole | Disponible |
| Tables et colonnes lues, déduites des requêtes SQL (BigQuery, Snowflake, PostgreSQL) | Disponible |
| API d'ingestion OpenLineage, par événement ou par lot | Disponible |
| Schéma du graphe de lignage dans PostgreSQL, avec ses migrations | Disponible |
| Alimentation du graphe à partir des événements reçus, par des workers concurrents | Disponible |
| Remontée d'une réponse à ses sources : jeux de données, versions, outils, agents | Disponible |
| Propagation d'un incident : réponses et destinataires touchés par une table fausse sur une période | Disponible |
| Entreprise synthétique et corpus de vérité, pour essayer et mesurer sans donnée réelle | Disponible |
| Explorateur visuel : graphe en cinq colonnes, mode Propager, adresses partageables | Disponible |
| Démonstration en une commande, sur une entreprise synthétique | Disponible |
| Contrôle de toute entrée : identifiants, taille, compression, profondeur, délai des lectures | Disponible |
| Ingestion idempotente, avec un compteur de doublons par émetteur | Disponible |
| Schémas JSON versionnés des facettes propres, pour écrire un émetteur sans lire le code | Disponible |
| Identifiant de run dérivé du contexte de trace W3C : lignage et traces se rejoignent | Disponible |
| Empreintes à clé des appels, et SQL conservé sans ses littéraux | Disponible |
| Kit de conformité des émetteurs, avec preuve de non-fuite par canari | Disponible |
| Image analysée à chaque merge request : SBOM, vulnérabilités hautes et critiques bloquantes | Disponible |
| Envoi des événements du proxy vers l'API | À venir |
| Propagation fine (colonnes, lignes, versions) et transitive (mémoire, tables dérivées) | À venir |
| Rappel des destinataires après validation humaine | À venir |

## Essayer

### En une commande

Avec Docker pour seul prérequis :

```bash
git clone https://github.com/alebgl77/AnswerLineage.git && cd AnswerLineage && docker compose up
```

L'explorateur est ensuite sur [http://127.0.0.1:8000/](http://127.0.0.1:8000/), devant une entreprise inventée : 5 agents, 4 outils, 13 tables, deux semaines de demandes. Un incident déjà propagé s'ouvre par son adresse :

```
http://127.0.0.1:8000/#/propager?namespace=bigquery&name=ventes.suivi.commandes&from=2026-11-11T06:00:00Z&to=2026-11-12T12:00:00Z
```

Rien n'écoute hors de la boucle locale, et aucune donnée réelle n'est en jeu. [La démonstration](docs/demonstration.md) dit ce qui tourne et comment l'arrêter.

### Pas à pas

Le reste de cette section monte les mêmes pièces une à une, pour voir ce que fait chacune.

Prérequis : [uv](https://docs.astral.sh/uv/), Python 3.13 ou 3.14, Docker, et Node 22 pour l'explorateur.

```bash
make setup
```

#### L'API d'ingestion

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

#### Le graphe

Dans un troisième terminal, avec la même variable `ANSWERLINEAGE_DATABASE_URL`, un worker projette les événements reçus dans le graphe de lignage :

```bash
uv run answerlineage-worker
```

Le run et la table qu'il a lue sont maintenant dans le graphe :

```bash
docker exec answerlineage-postgres psql -U postgres -c "SELECT d.namespace, d.name, e.precision, r.state FROM read_edge e JOIN dataset d ON d.id = e.dataset_id JOIN run r ON r.run_id = e.run_id"
```

#### Une entreprise synthétique

Pour voir un graphe plus riche qu'un seul événement, le dépôt fournit une entreprise inventée : 5 agents, 4 outils, 13 tables, deux semaines de demandes. La commande envoie ses événements à l'API, et le worker les projette dans le graphe :

```bash
uv run answerlineage-corpus load --url http://127.0.0.1:8000
```

Les réponses de chaque agent, et les personnes qui les ont reçues :

```bash
docker exec answerlineage-postgres psql -U postgres -c "SELECT a.name AS agent, count(DISTINCT w.id) AS reponses, count(DISTINCT l.recipient_id) AS destinataires FROM answer w JOIN run r ON r.run_id = w.run_id JOIN agent a ON a.id = r.agent_id JOIN delivery d ON d.answer_id = w.id JOIN delivery_recipient l ON l.delivery_id = d.id GROUP BY a.name ORDER BY reponses DESC"
```

#### Remonter d'une réponse à ses sources

Un destinataire a reçu un message d'un agent et veut savoir sur quoi il s'appuie. Il retrouve la réponse par le canal et la référence du message :

```bash
curl "http://127.0.0.1:8000/api/v1/answers?channel=slack&message_ref=34f209664c2955dd"
```

Puis il demande ses sources, avec l'identifiant obtenu : les tables et les colonnes lues, leur version, les outils appelés, l'agent qui a répondu.

```bash
curl http://127.0.0.1:8000/api/v1/answers/01a1c9e63a187c5cb8b5fce292591000424d79573035c170/sources
```

#### Propager un incident

La table des commandes a été fausse du chargement de nuit du 11 novembre jusqu'à sa correction le lendemain midi. Quelles réponses s'appuient sur elle, et qui les a reçues ?

```bash
curl "http://127.0.0.1:8000/api/v1/impact?namespace=bigquery&name=ventes.suivi.commandes&from=2026-11-11T06:00:00Z&to=2026-11-12T12:00:00Z"
```

La réponse compte 16 réponses touchées et 13 destinataires, et donne la chaîne de chacune. L'entreprise vient avec sa vérité, établie sans passer par AnswerLineage : pour six incidents de données, la liste exacte des réponses et des destinataires touchés. Cet incident est le premier, et les deux listes sont les mêmes.

```bash
uv run answerlineage-corpus truth
```

#### L'explorateur

L'explorateur montre tout cela en cinq colonnes, des données aux personnes. Il se construit une fois :

```bash
make ui
```

Puis l'API se relance avec le dossier construit, à la place de la commande précédente :

```bash
ANSWERLINEAGE_UI_DIR=ui/dist uv run uvicorn --factory answerlineage_api.app:create_app
```

Le graphe est sur [http://127.0.0.1:8000/](http://127.0.0.1:8000/), et l'incident propagé plus haut s'ouvre par la même adresse que dans la démonstration.

#### Le proxy MCP

Devant un serveur MCP qui écoute sur le port 9000 et expose un outil `execute_sql` dont l'argument `query` porte du SQL BigQuery :

```bash
uv run answerlineage-proxy --upstream http://127.0.0.1:9000 --server-name entrepot --sql-tool execute_sql=query:bigquery
```

L'agent se connecte alors à `http://127.0.0.1:8717` au lieu du serveur : rien d'autre ne change pour lui.

## Documentation

- [Proxy MCP](docs/proxy.md) : ce qui est relayé, ce qui est lu, analyse du SQL, limites connues
- [API d'ingestion](docs/ingestion.md) : routes, lots, validation, rejeu
- [API de lecture](docs/lecture.md) : retrouver une réponse, remonter à ses sources, propager un incident
- [Facettes](docs/facettes.md) : les schémas JSON versionnés des facettes propres à AnswerLineage
- [Kit de conformité](docs/conformite.md) : vérifier un émetteur, lire son rapport
- [Démonstration](docs/demonstration.md) : ce que lance `docker compose up`, et comment le vérifier
- [Explorateur](docs/explorateur.md) : le graphe en cinq colonnes, le mode Propager, les adresses partageables
- [Workers](docs/workers.md) : file de traitement, garanties, projection dans le graphe
- [Schéma du graphe](docs/schema.md) : tables, écritures, migrations
- [Entreprise synthétique et corpus de vérité](docs/corpus.md) : ce qu'ils contiennent, leurs événements, la vérité des incidents
- [Pipeline d'intégration continue](docs/ci.md) : contrôles, chaîne d'approvisionnement

## Contribuer

```bash
make check
```

`make check` est la porte de toute contribution : politique du dépôt, compatibilité de l'API, format, lint, typage strict, tests et couverture. Les tests d'intégration démarrent un PostgreSQL réel avec Testcontainers.

Le [guide de contribution](CONTRIBUTING.md) dit comment proposer un changement et ce qui est attendu avant sa fusion. Le dépôt fournit un conteneur de développement (`.devcontainer/`), sans identifiant ni jeton, utilisable avec VS Code ou GitHub Codespaces.

## Sécurité

Une faille se signale en privé, jamais dans une issue : la [politique de sécurité](SECURITY.md) donne le canal et les délais de réponse.

Le projet suit un [code de conduite](CODE_OF_CONDUCT.md).

## Licence

AnswerLineage est distribué sous la licence [Apache 2.0](LICENSE).
