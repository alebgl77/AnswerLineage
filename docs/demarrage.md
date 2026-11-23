# Démarrage

Ce guide mène du clone du dépôt au premier lignage visible, vérifie l'installation, propage un incident depuis le terminal puis dans l'explorateur, remonte d'une réponse à ses sources, et arrête tout. Depuis un clone neuf, le premier lignage est là en cinq minutes au plus ; le reste prend quelques minutes de plus.

Pour comprendre ce que l'on regarde : les [concepts](concepts.md), puis l'[architecture](architecture.md) et les [décisions d'architecture](decisions/README.md).

## Chaque commande est jouée par le pipeline

Les blocs `bash` de ce guide sont extraits du fichier et joués, dans l'ordre, par le job `demo` du pipeline (`scripts/check_demo.sh guide`). Une commande qui ne fonctionne plus fait échouer le pipeline, qui la nomme par sa ligne. Le temps qui sépare le départ du premier lignage est comparé à un budget de cinq minutes ([comment le guide est rejoué](demonstration.md#le-guide-de-démarrage-rejoué)).

Chaque bloc se joue seul, depuis la racine du dépôt : il ne suppose rien du bloc précédent, sinon que la démonstration tourne. Les blocs d'une autre langue (`text`) sont des exemples de sortie, jamais joués. Un bloc `bash` qui ne doit pas l'être se marque d'un commentaire qui dit pourquoi ; ceux de ce guide le disent.

Cela vaut aussi pour qui modifie ce guide : un bloc ajouté est joué.

## Prérequis

- Docker, avec Compose v2 : `docker compose version` répond.
- Le port 8000 libre sur la boucle locale : la démonstration l'utilise, et lui seul.
- Pour la dernière étape seulement, [uv](https://docs.astral.sh/uv/) et Python 3.13 ou 3.14.

## 1. Cloner et lancer

<!-- guide : non joué, le pipeline est déjà dans un clone du dépôt -->
```bash
git clone https://github.com/alebgl77/AnswerLineage.git && cd AnswerLineage
```

Les commandes suivantes se lancent depuis la racine du clone.

```bash
docker compose up --build --detach
```

La première fois, Docker construit l'image, qui contient l'API, l'explorateur, le worker, l'entreprise synthétique et la ligne de commande, puis lance cinq services : PostgreSQL, la migration du schéma, l'API, un worker, et `seed`, qui envoie l'[entreprise synthétique](corpus.md) à l'API. La commande rend la main quand ils sont lancés, pas quand le graphe est prêt. [La démonstration](demonstration.md) dit ce que fait chaque service.

```bash
docker compose ps --all
```

`postgres`, `api` et `worker` sont `running` ; `migrate` et `seed` sont `exited (0)` une fois leur travail fini.

## 2. Le premier lignage

Le premier incident de l'entreprise : la table des commandes, `ventes.suivi.commandes`, a été fausse du chargement de nuit du 11 novembre, 6 h, jusqu'à sa correction le lendemain à midi. Quelles réponses d'agents s'appuient sur elle, et qui les a reçues ? La commande `answerlineage impact` pose la question à l'API. Tant que l'entreprise se charge, elle échoue ou dit son résultat incomplet ; elle réussit quand le résultat est complet ([codes de sortie](cli.md#codes-de-sortie)) : la boucle ci-dessous la rejoue toutes les deux secondes jusque-là.

<!-- guide : premier lignage -->
```bash
until docker compose exec -T api answerlineage impact --namespace bigquery --name ventes.suivi.commandes --from 2026-11-11T06:00:00Z --to 2026-11-12T12:00:00Z; do sleep 2; done
```

`-T` évite l'allocation d'un terminal, absent d'un script. La sortie, abrégée :

```text
Jeu de données : ventes.suivi.commandes (espace de noms bigquery, table)
Fenêtre : du 2026-11-11T06:00:00Z au 2026-11-12T12:00:00Z, fin exclue

16 réponses touchées, 13 destinataires
Couverture du lignage : mesurée, 100 % des appels vus sont dans le graphe, sans trou connu

Réponses, les plus récentes d'abord (16 sur cette page) :
  2026-11-12T10:47:56Z  01a1cf5e5bb078b991a908d485d5acfdb4c70313c6faa097
    agent : analyste-tresorerie (agent://finance)
    outils : execute_sql (entrepot)
    lu : ventes.suivi.commandes (bigquery)
    canaux : email
    destinataires : 821743431def23772bbc6215495e30c7, a71b66a14ed697845fee1dc093de55a0, d3675030f72fc8499edef4ab380c2c44
  …

Liste complète : aucune réponse touchée ne peut y manquer.
```

C'est le premier lignage : 16 réponses et 13 destinataires pseudonymisés touchés par une table fausse, chacune avec sa chaîne (l'agent, les outils, les données lues, les canaux). La couverture dit ce que le résultat peut manquer : ici, rien, parce que chaque émetteur a publié ce qu'il a vu et que le graphe contient tout. L'entreprise vient avec sa vérité, établie sans passer par AnswerLineage ; elle donne les mêmes nombres.

```bash
docker compose exec -T api answerlineage-corpus truth
```

## 3. Vérifier l'installation

```bash
docker compose exec -T api answerlineage doctor
```

```text
Vérification de l'installation : http://127.0.0.1:8000

  bon          API          L'API répond : API 1.0.0.
  bon          Adresse      L'API accepte cette adresse (en-têtes Host et Origin).
  bon          Accès        L'API répond sans jeton : son authentification est désactivée, comme dans la démonstration.
  bon          Base         L'API lit sa base PostgreSQL.
  bon          Migrations   Le schéma de la base est à jour.
  bon          File         La file d'ingestion est vide.
  bon          Émetteurs    3 émetteurs connus, aucun muet.
  bon          Partitions   Aucune partition mensuelle ne manque.
  bon          Explorateur  L'explorateur est servi : http://127.0.0.1:8000/
  non vérifié  Workers      Les workers ne publient aucun signal de vie : leur marche se lit dans la file, qui avance, et dans le contrôle des émetteurs muets, qui ne tourne qu'avec eux.

Aucune anomalie.
```

Le code de sortie est `0` sans anomalie. Dix minutes après le chargement, `doctor` signale les trois émetteurs de l'entreprise (`emitters_muted`) : l'entreprise cesse de publier, et un émetteur qui se tait n'est plus vu. C'est le comportement voulu, pas une panne de la démonstration ; [les anomalies et leurs remèdes](cli.md#anomalies-et-remèdes) disent chaque cas.

## 4. Dans l'explorateur

Le même incident s'ouvre dans l'explorateur par son adresse. Elle allume les 16 réponses et les 13 destinataires touchés, avec la couverture du lignage :

```text
http://127.0.0.1:8000/#/propager?namespace=bigquery&name=ventes.suivi.commandes&from=2026-11-11T06:00:00Z&to=2026-11-12T12:00:00Z
```

La page d'accueil, `http://127.0.0.1:8000/`, montre les réponses les plus récentes et tout ce qu'elles touchent, en cinq colonnes : jeux de données, outils, agents, réponses, destinataires. Le serveur la sert :

```bash
curl --silent --fail --output /dev/null http://127.0.0.1:8000/
```

## 5. Remonter d'une réponse à ses sources

Le chemin inverse part d'une réponse. Un destinataire sait le canal et la référence du message qu'il a reçu ; l'API en retrouve la réponse :

```bash
curl --silent --fail "http://127.0.0.1:8000/api/v1/answers?channel=slack&message_ref=34f209664c2955dd"
```

Puis, avec l'identifiant obtenu, ses sources : les tables et les colonnes lues, leur version, les outils appelés, l'agent qui a répondu, et la couverture du lignage sur la période de la réponse.

```bash
curl --silent --fail http://127.0.0.1:8000/api/v1/answers/01a1c9e63a187c5cb8b5fce292591000424d79573035c170/sources
```

Dans l'explorateur, le mode Remonter fait de même : `http://127.0.0.1:8000/#/remonter`. L'[API de lecture](lecture.md) détaille ces routes.

## 6. Depuis votre poste de développement

Les commandes précédentes passent par l'image. Pour travailler sur le code, `make setup` installe l'environnement verrouillé (`uv sync`) et les hooks de pré-commit :

```bash
make setup
```

La ligne de commande de l'environnement joint la démonstration à son adresse par défaut, `http://127.0.0.1:8000` :

```bash
uv run answerlineage doctor
```

Elle dit les mêmes choses que depuis l'image. Les tests d'intégration, eux, démarrent leur propre PostgreSQL avec Testcontainers ; ils ne touchent pas à la démonstration. La porte de toute contribution rejoue tout cela, et prend de l'ordre de quatre minutes :

<!-- guide : non joué, les jobs static et tests du pipeline la jouent, et elle dure plusieurs minutes -->
```bash
make check
```

[Contribuer](../CONTRIBUTING.md) dit comment proposer un changement ; [le pipeline](ci.md) détaille chaque contrôle.

## 7. Arrêter

```bash
docker compose down
```

Les données restent dans un volume : relancée, la démonstration les retrouve à l'identique, et `seed` renvoie l'entreprise, qui n'ajoute rien. Pour tout effacer, données comprises :

```bash
docker compose down --volumes
```

## Et ensuite

- [Concepts](concepts.md) : le vocabulaire du produit, un exemple de l'entreprise synthétique pour chaque mot.
- [Architecture](architecture.md) : les composants, le flux de la capture à la lecture, le modèle de données, les invariants et où chacun est tenu.
- [Décisions d'architecture](decisions/README.md) : pourquoi le code est fait ainsi, et ce qui a été écarté.
- [Ligne de commande](cli.md), [démonstration](demonstration.md) et [explorateur](explorateur.md) pour aller plus loin dans ce qu'on vient de lancer.
