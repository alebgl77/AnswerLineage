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
| `seed` | Envoie l'[entreprise synthétique](corpus.md) à l'API d'ingestion, puis s'arrête |

Les données empruntent le chemin réel : ingestion, file de traitement, worker, graphe. Rien n'est écrit directement dans la base.

Tous les services partagent une image, construite depuis les sources et les versions verrouillées du dépôt (`deploy/Dockerfile`). Aucun ne tourne en administrateur.

## Ce que l'on peut y faire

- Suivre une donnée jusqu'aux personnes dans la [vue d'ensemble](explorateur.md).
- Propager un incident : l'adresse ci-dessous ouvre le premier incident de l'entreprise, 16 réponses et 13 destinataires touchés.

```
http://127.0.0.1:8000/#/propager?namespace=bigquery&name=ventes.suivi.commandes&from=2026-11-11T06:00:00Z&to=2026-11-12T12:00:00Z
```

- Interroger l'[API de lecture](lecture.md) avec `curl`, sur `http://127.0.0.1:8000/api/v1/`.
- Comparer avec la vérité de l'entreprise, établie sans passer par AnswerLineage :

```bash
docker compose run --rm --no-deps seed answerlineage-corpus truth
```

## Arrêter, relancer, effacer

```bash
docker compose down
```

Les données restent dans un volume. Relancée, la démonstration les retrouve à l'identique : l'entreprise est renvoyée à l'API, qui n'enregistre un événement qu'une fois.

```bash
docker compose down --volumes
```

efface aussi les données.

## Ce qui est garanti

Rien n'écoute hors de la boucle locale : seul le port de l'API est publié, sur `127.0.0.1`. L'API refuse en plus toute requête dont l'en-tête `Host` ou `Origin` n'est pas attendu, ce qui ferme la porte à une page web ouverte sur le même poste.

Le pipeline rejoue la démonstration à chaque merge request (`scripts/check_demo.sh`) :

- depuis un clone neuf, le premier graphe est là en moins de cinq minutes, et la propagation du premier incident donne la vérité de l'entreprise ;
- aucun port n'est publié hors de la boucle locale, et l'API ne répond pas sur l'adresse du réseau ;
- une requête sous un autre nom d'hôte reçoit `403` ;
- après un arrêt et une relance, les données sont identiques.

La politique du dépôt (`make policy`) refuse un fichier compose qui publierait un port ailleurs que sur la boucle locale, ou une image sans empreinte.

## Limites connues

- La démonstration n'a ni authentification ni chiffrement : elle est faite pour un poste, pas pour un serveur partagé.
- Le mot de passe de la base est écrit dans `compose.yaml`. Il ne protège rien d'autre que cette base, qui n'est pas publiée.
- Le proxy MCP n'y tourne pas : les événements viennent de l'entreprise synthétique, pas d'agents réels.
