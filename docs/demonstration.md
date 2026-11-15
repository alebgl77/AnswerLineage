# Démonstration

Une commande lance tout AnswerLineage sur une entreprise inventée, avec Docker pour seul prérequis :

```bash
docker compose up
```

L'explorateur est ensuite sur [http://127.0.0.1:8000/](http://127.0.0.1:8000/). La première fois, l'image se construit ; le premier graphe arrive en quelques minutes, puis en quelques secondes aux lancements suivants.

## Ce qui tourne

| Service | Rôle |
| --- | --- |
| `postgres` | La base, seule dépendance d'AnswerLineage. Elle n'est joignable que des autres services |
| `migrate` | Crée ou met à jour le schéma, puis s'arrête |
| `api` | L'API d'ingestion et de lecture, et l'explorateur qu'elle sert |
| `worker` | Projette les événements reçus dans le graphe de lignage |
| `seed` | Envoie l'[entreprise synthétique](corpus.md) à l'API d'ingestion, puis s'arrête. Il partage l'espace réseau de l'API et la joint par la boucle locale |

Les données empruntent le chemin réel : ingestion, file de traitement, worker, graphe. Rien n'est écrit directement dans la base.

Tous les services partagent une image, construite depuis les sources et les versions verrouillées du dépôt (`deploy/Dockerfile`). Aucun ne tourne en administrateur. L'image part d'une base Alpine et ne garde que l'interpréteur et l'environnement installé : ni les sources, ni l'installateur de paquets. Moins elle contient de paquets système, moins elle a de vulnérabilités à suivre ; le pipeline l'analyse à chaque merge request ([Image : SBOM et vulnérabilités](ci.md)).

## Ce que l'on peut y faire

- Suivre une donnée jusqu'aux personnes dans la [vue d'ensemble](explorateur.md).
- Propager un incident : l'adresse ci-dessous ouvre le premier incident de l'entreprise, 16 réponses et 13 destinataires touchés.

```
http://127.0.0.1:8000/#/propager?namespace=bigquery&name=ventes.suivi.commandes&from=2026-11-11T06:00:00Z&to=2026-11-12T12:00:00Z
```

- Interroger l'[API de lecture](lecture.md) avec `curl`, sur `http://127.0.0.1:8000/api/v1/`.
- Comparer avec la vérité de l'entreprise, établie sans passer par AnswerLineage, pendant que la démonstration tourne :

```bash
docker compose exec api answerlineage-corpus truth
```

## Arrêter, relancer, effacer

```bash
docker compose down
```

Les données restent dans un volume. Relancée, la démonstration les retrouve à l'identique. Le service `seed` renvoie alors l'entreprise à l'API, qui n'enregistre un événement qu'une fois : rien ne s'ajoute.

```bash
docker compose down --volumes
```

efface aussi les données.

## Ce qui est garanti

Rien n'écoute hors de la boucle locale : seul le port de l'API est publié, sur `127.0.0.1`. L'API refuse en plus toute requête dont l'en-tête `Host` ou `Origin` n'est pas attendu, ce qui ferme la porte à une page web ouverte sur le même poste. Elle n'accepte aucun nom en plus de la boucle locale : `seed` la joint par `127.0.0.1`, depuis son espace réseau, et non par un nom de service que le réseau pourrait résoudre ailleurs.

Le journal d'accès de l'API est coupé : il écrirait les paramètres des adresses demandées, qui peuvent porter une donnée.

Le pipeline rejoue la démonstration à chaque merge request :

- depuis un clone neuf, le premier graphe est là en moins de cinq minutes, et la propagation du premier incident donne la vérité de l'entreprise ;
- aucun port n'est publié hors de la boucle locale, et aucune connexion n'aboutit sur l'adresse du réseau, quelle que soit la réponse HTTP ;
- une requête sous un autre nom d'hôte reçoit `403`, et le journal de l'API ne garde aucune adresse demandée ;
- les parcours principaux de l'explorateur passent dans Chromium, comparés à l'entreprise générée : **Remonter**, de la vue d'ensemble à la page d'une réponse, avec ce qu'elle a lu, par quels outils, pour qui ; **Propager**, le premier incident saisi dans le formulaire, avec ses compteurs et ses nœuds allumés ;
- arrêtée puis relancée sans le service `seed`, la démonstration retrouve un graphe identique : les données viennent du volume ;
- renvoyée une seconde fois, l'entreprise n'apporte aucun événement nouveau et ne change pas le graphe.

## Rejouer la vérification sur le poste

Il faut Docker, l'environnement du dépôt (`make setup`) et Chromium (`uv run playwright install chromium`). Le port 8000 doit être libre.

```bash
scripts/check_demo.sh
```

Le script lance la démonstration, la vérifie, puis l'arrête et efface ses données. Chaque étape est aussi une sous-commande, celles que le pipeline enchaîne :

| Sous-commande | Ce qu'elle fait |
| --- | --- |
| `up` | Construit et lance la démonstration, attend le premier graphe et mesure son délai |
| `checks` | Ports sur la boucle locale seulement, rien sur l'adresse du réseau, nom d'hôte inattendu refusé, journal sans adresse |
| `browser` | Parcours Remonter et Propager dans Chromium (`tests/test_demonstration.py`) |
| `restart` | Arrêt, relance sans `seed`, même graphe ; puis renvoi de l'entreprise, sans effet |
| `down` | Arrête la démonstration et efface ses données |

`checks`, `browser` et `restart` visent une démonstration déjà lancée. Après un échec, la démonstration reste lancée pour l'examiner ; `scripts/check_demo.sh down` l'arrête.

Les parcours se jouent aussi seuls, contre toute démonstration chargée de l'entreprise par défaut ; sans `ANSWERLINEAGE_DEMO_URL`, ils sont sautés :

```bash
ANSWERLINEAGE_DEMO_URL=http://127.0.0.1:8000 uv run pytest -m demo
```

## Politique des fichiers compose

La politique du dépôt (`make policy`) contrôle tout fichier YAML du dépôt qui décrit des services compose, quel que soit son nom : `compose.yaml`, mais aussi `compose.override.yaml` ou `docker-compose.override.yml`, que Compose fusionne d'office, et tout fichier inclus ou étendu. Elle refuse :

- un port publié ailleurs que sur la boucle locale ;
- un service sur le réseau de l'hôte, par `network_mode: host` ou par un réseau externe nommé `host` ;
- une image sans empreinte ;
- une directive `include` ou `extends` vers un fichier absent du dépôt, distant ou désigné par une variable : le contrôle ne pourrait pas le lire.

## Limites connues

- La démonstration n'a ni authentification ni chiffrement : elle est faite pour un poste, pas pour un serveur partagé.
- Le mot de passe de la base est écrit dans `compose.yaml`. Il ne protège rien d'autre que cette base, qui n'est pas publiée.
- Le proxy MCP n'y tourne pas : les événements viennent de l'entreprise synthétique, pas d'agents réels.
