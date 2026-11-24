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

Tous les services partagent une image, construite depuis les sources et les versions verrouillées du dépôt (`deploy/Dockerfile`). Aucun ne tourne en administrateur. L'image part d'une base Alpine et ne garde que l'interpréteur et l'environnement installé : ni les sources, ni l'installateur de paquets, ni SQLite, dont aucun service ne se sert (`import sqlite3` y échoue, et un test vérifie qu'aucun service n'en a besoin). Les dépendances Python viennent du verrou ; les paquets système d'Alpine reçoivent à chaque construction les correctifs de sécurité de leur branche stable, qui ne sont donc pas figés. Moins elle contient de paquets système, moins elle a de vulnérabilités à suivre ; le pipeline l'analyse à chaque merge request ([Image : SBOM et vulnérabilités](ci.md)).

## Ce que l'on peut y faire

- Suivre une donnée jusqu'aux personnes dans la [vue d'ensemble](explorateur.md).
- Remonter d'une réponse jusqu'aux données qu'elle a lues, en mode Remonter, avec la couverture de la période de son run.
- Propager un incident : l'adresse ci-dessous ouvre le premier incident de l'entreprise, 16 réponses et 13 destinataires touchés.

```
http://127.0.0.1:8000/#/propager?namespace=bigquery&name=ventes.suivi.commandes&from=2026-11-11T06:00:00Z&to=2026-11-12T12:00:00Z
```

- Interroger l'[API de lecture](lecture.md) avec `curl`, sur `http://127.0.0.1:8000/api/v1/`.
- Tout faire depuis le terminal avec la [ligne de commande](cli.md) : `answerlineage demo` lance la démonstration depuis un clone et attend qu'elle réponde, `answerlineage doctor` vérifie chacun de ses composants, `answerlineage impact` propage un incident.
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

L'authentification OIDC de l'API est désactivée, par un réglage nommé que `compose.yaml` pose sur le service `api` : `ANSWERLINEAGE_AUTH=disabled`. Hors de ce réglage, l'API ne démarre pas sans émetteur OIDC. Désactivée, elle refuse de démarrer si elle accepte un autre nom ou une autre origine que la boucle locale, et l'explorateur le dit dans un bandeau ([Authentification et rôles](securite/authentification.md#la-démonstration)). Le contrôle de santé du service `api` interroge la sonde de vie, `GET /api/v1/live`, ouverte sans jeton.

Le pipeline rejoue la démonstration à chaque merge request :

- les commandes du [guide de démarrage](demarrage.md) se jouent dans l'ordre depuis un clone neuf, et le premier lignage est là dans le budget de cinq minutes ([ci-dessous](#le-guide-de-démarrage-rejoué)) ;
- depuis un clone neuf, le premier graphe est là en moins de cinq minutes, et la propagation du premier incident donne la vérité de l'entreprise ;
- aucun port n'est publié hors de la boucle locale, et aucune connexion n'aboutit sur l'adresse du réseau, quelle que soit la réponse HTTP ;
- une requête sous un autre nom d'hôte reçoit `403`, et le journal de l'API ne garde aucune adresse demandée ;
- les parcours principaux de l'explorateur passent dans Chromium, comparés à l'entreprise générée : **Remonter**, la réponse la plus récente choisie en mode Remonter, dont seul le chemin jusqu'aux données reste en évidence, couverte à 100 %, puis sa page, avec ce qu'elle a lu, par quels outils, pour qui ; **Propager**, le premier incident saisi dans le formulaire, avec ses compteurs et ses nœuds allumés ;
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
| `guide` | Joue les blocs `bash` de `docs/demarrage.md`, dans l'ordre, et mesure le premier lignage ; arrête la démonstration et efface ses données à la fin du guide |
| `up` | Construit et lance la démonstration, attend le premier graphe et mesure son délai |
| `checks` | Ports sur la boucle locale seulement, rien sur l'adresse du réseau, nom d'hôte inattendu refusé, journal sans adresse |
| `browser` | Parcours Remonter et Propager dans Chromium (`tests/test_demonstration.py`) |
| `restart` | Arrêt, relance sans `seed`, même graphe ; puis renvoi de l'entreprise, sans effet |
| `down` | Arrête la démonstration et efface ses données |

`guide` lance et arrête lui-même la démonstration, et demande `uv` et `make setup` ; `checks`, `browser` et `restart` visent une démonstration déjà lancée. Sans argument, le script les enchaîne toutes, `guide` d'abord. Après un échec, la démonstration reste lancée pour l'examiner ; `scripts/check_demo.sh down` l'arrête.

Les parcours se jouent aussi seuls, contre toute démonstration chargée de l'entreprise par défaut ; sans `ANSWERLINEAGE_DEMO_URL`, ils sont sautés :

```bash
ANSWERLINEAGE_DEMO_URL=http://127.0.0.1:8000 uv run pytest -m demo
```

## Le guide de démarrage rejoué

Le [guide de démarrage](demarrage.md) est joué par `scripts/check_demo.sh guide`, qui appelle `python -m scripts.play_guide docs/demarrage.md --budget …`. Le budget est celui de `up` (`BUDGET_SECONDS`, 300 s) : il n'est écrit qu'à un endroit.

- **Tout bloc `bash` est joué**, dans l'ordre, chacun comme un script à lui seul (`bash -euo pipefail`), depuis la racine du dépôt. Les blocs d'une autre langue (`text`, pour un exemple de sortie) ne le sont jamais.
- **Un bloc qui ne doit pas l'être se marque**, par un commentaire sur la ligne qui précède sa clôture, avec sa raison : `<!-- guide : non joué, le pipeline est déjà dans un clone du dépôt -->`. Une marque inconnue, sans raison, ou qui ne précède pas un bloc `bash` est refusée.
- **Le premier lignage se marque aussi** : `<!-- guide : premier lignage -->`, sur le bloc dont la réussite le montre. Le temps écoulé depuis le départ du guide est comparé au budget, et aucune commande ne dure plus que lui : une commande qui l'excède est tuée et nommée.
- **Une commande qui échoue arrête le guide et se nomme** par son fichier, sa ligne et sa première ligne : `docs/demarrage.md, ligne 30 : la commande échoue (code 1) : docker compose up --build --detach`.

L'extraction se teste dans `make check` (`tests/test_play_guide.py`) : un bloc ajouté est joué, un bloc marqué ne l'est pas, une commande qui échoue est nommée. `tests/test_documentation.py` vérifie que le guide a son premier lignage, que les cibles `make` et les services `compose` qu'il cite existent, et que ses exemples sont ceux de l'entreprise synthétique.

## Politique des fichiers compose

La politique du dépôt (`make policy`) contrôle tout fichier YAML du dépôt qui décrit des services compose, quel que soit son nom : `compose.yaml`, mais aussi `compose.override.yaml` ou `docker-compose.override.yml`, que Compose fusionne d'office, et tout fichier inclus ou étendu. Elle refuse :

- un port publié ailleurs que sur la boucle locale ;
- un service sur le réseau de l'hôte, par `network_mode: host` ou par un réseau externe nommé `host` ;
- une image sans empreinte ;
- une directive `include` ou `extends` vers un fichier absent du dépôt, distant ou désigné par une variable : le contrôle ne pourrait pas le lire.

## Limites connues

- La démonstration n'a ni authentification ni chiffrement : elle est faite pour un poste, pas pour un serveur partagé. L'API ne voit pas l'adresse sur laquelle elle écoute : c'est la publication du port sur `127.0.0.1`, que la politique des fichiers compose et `scripts/check_demo.sh` vérifient, qui la garde sur la boucle locale.
- Le mot de passe de la base est écrit dans `compose.yaml`. Il ne protège rien d'autre que cette base, qui n'est pas publiée.
- Le proxy MCP n'y tourne pas : les événements viennent de l'entreprise synthétique, pas d'agents réels.
