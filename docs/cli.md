# Ligne de commande

`answerlineage` fait depuis le terminal ce que fait l'explorateur, et un peu plus : propager un incident jusqu'aux réponses et aux destinataires touchés, vérifier une installation, lancer la démonstration. Elle parle à l'API par HTTP, comme l'explorateur, et ne lit jamais la base.

| Sous-commande | Ce qu'elle fait |
| --- | --- |
| [`answerlineage impact`](#answerlineage-impact) | Les réponses et les destinataires touchés par un jeu de données faux pendant une fenêtre ; le code de sortie dit si l'on peut conclure |
| [`answerlineage doctor`](#answerlineage-doctor) | Vérifie chaque composant d'une installation et liste les anomalies, chacune avec son remède |
| [`answerlineage demo`](#answerlineage-demo) | Lance la démonstration d'un clone du dépôt, attend qu'elle réponde et donne l'adresse de l'explorateur |

## Installation

Dans le dépôt, après `make setup` :

```bash
uv run answerlineage --help
```

L'image de démonstration la contient aussi ; pendant que la démonstration tourne :

```bash
docker compose exec api answerlineage doctor
```

Le paquet `answerlineage-cli` sort avec chaque version, comme les autres composants ([Publication](publication.md)). Il ne dépend que de httpx : aucun autre composant d'AnswerLineage, ni pilote de base de données.

## L'adresse de l'API

Toutes les sous-commandes joignent l'API à la même adresse :

| Réglage | Rôle | Défaut |
| --- | --- | --- |
| `--url` | Adresse de l'API, sous-commande par sous-commande | aucune |
| `ANSWERLINEAGE_API_URL` | La même, par l'environnement ; `--url` passe avant | `http://127.0.0.1:8000`, celle de la démonstration |
| `--timeout` | Délai maximal d'une requête, en secondes, de 1 à 600 | 30 |

Une adresse s'écrit `http://hôte[:port][/chemin]` ou `https://…`, sans identifiant (`utilisateur:motdepasse@`), sans paramètre ni fragment. Un chemin est admis, pour une API servie derrière un relais sous un préfixe. Une adresse refusée est une erreur d'usage, et le message ne la recopie pas.

La commande joint l'adresse donnée, directement : elle ne suit aucune redirection, et ignore les variables de mandataire (`HTTPS_PROXY`…) et `.netrc`, pour qu'aucun identifiant ne s'y ajoute à son insu. L'API refuse un nom d'hôte qu'elle n'attend pas ([Hôtes et origines attendus](ingestion.md#hôtes-et-origines-attendus)) : `answerlineage doctor` le dit, avec le réglage à changer.

## `answerlineage impact`

Le premier incident de la démonstration : la table des commandes, fausse du chargement de nuit du 11 novembre jusqu'à sa correction le lendemain midi.

```bash
answerlineage impact --namespace bigquery --name ventes.suivi.commandes --from 2026-11-11T06:00:00Z --to 2026-11-12T12:00:00Z
```

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

Destinataires, pseudonymisés (13 sur cette page) :
  237937de45af1b1cc4811a3b5a1152bd
  …

Liste complète : aucune réponse touchée ne peut y manquer.
```

La sortie lisible dit le jeu de données et la fenêtre, les décomptes de tout l'incident, la [couverture du lignage](lecture.md#couverture-du-lignage) et chacun de ses trous connus, puis la chaîne de chaque réponse : l'agent qui a répondu, les outils appelés, les jeux de données lus, les canaux de diffusion et les destinataires, par leur pseudonyme. Elle finit par ce que le résultat permet de conclure.

| Option | Rôle | Défaut |
| --- | --- | --- |
| `--namespace`, `--name` | Le jeu de données, par son espace de noms et son nom (500 caractères au plus chacun) | obligatoires |
| `--from`, `--to` | La fenêtre de l'incident, début inclus, fin exclue, en RFC 3339 avec fuseau ; `--to` suit `--from` | obligatoires |
| `--limit` | Réponses par page, de 1 à 200 | 200 |
| `--cursor` | La page suivante : la valeur de `next_cursor`, que la sortie lisible rappelle | aucun |
| `--known-at` | Lit le graphe tel qu'il était connu à cette date ([graphe à une date](lecture.md#le-graphe-tel-que-connu-à-une-date)) | aucune |
| `--format` | `text` ou `json` | `text` |

Ce sont les paramètres de `GET /api/v1/impact`. La propagation se fait au niveau table, le seul que la route connaisse : elle renvoie tout lecteur du jeu de données pendant la fenêtre, et la commande n'a pas d'option de niveau ([limites de la propagation](lecture.md#limites-connues)).

### Codes de sortie

Un script doit pouvoir distinguer « rien n'est touché » de « je ne sais pas ». Le code de sortie le dit :

| Code | Sens |
| --- | --- |
| `0` | Des réponses sont touchées, et la liste est complète |
| `1` | Aucune réponse touchée, et le résultat le prouve : complet, sous une couverture mesurée sans trou sur toute la fenêtre |
| `2` | Usage fautif : option absente ou mal formée, fenêtre vide, adresse refusée ; ou paramètre refusé par l'API (`422`) |
| `3` | Résultat incomplet : tronqué, ou couverture du lignage non mesurée, partielle, trouée ou elle-même tronquée. Des réponses touchées peuvent manquer ; zéro réponse ne prouve rien |
| `4` | L'API ne répond pas, ou pas dans le délai |
| `5` | L'API a refusé la requête (jeu de données inconnu, délai de lecture dépassé, adresse refusée) ou rendu une réponse invalide |

Un code `3` dit pourquoi dans la sortie lisible. Par exemple, sur une fenêtre qui commence avant le premier relevé des émetteurs :

```text
Couverture du lignage : partielle, 100 % des appels vus sont dans le graphe, 3 trous connus
  mcp://crm : avant son premier relevé, du 2026-10-30T00:00:00Z au 2026-11-02T00:00:00Z
  …
Résultat incomplet :
  - les relevés des émetteurs ne couvrent qu'une partie de la fenêtre.
  - des trous connus : des appels ont pu échapper au graphe.
D'autres réponses peuvent être touchées que celles-ci.
```

Un code `1` n'est rendu que sur un résultat qui permet de conclure qu'il n'y a rien à rappeler. Les erreurs, codes `2`, `4` et `5`, s'écrivent sur la sortie d'erreur ; la sortie standard reste vide.

### Sortie JSON

`--format json` rend la réponse de `GET /api/v1/impact`, sous la forme que décrit l'[API de lecture](lecture.md#propager-un-incident) et le contrat publié (`ImpactResult` dans [`spec/openapi/answerlineage-v1.json`](../spec/openapi/answerlineage-v1.json)) : `dataset`, `window`, `counts`, `coverage`, `graph`, `truncated`, et `known_at` quand il est demandé. Elle est réduite aux champs de ce contrat : un champ que l'API ajouterait ne passe pas tant que la commande ne le connaît pas. Le code de sortie est le même qu'en sortie lisible.

```bash
answerlineage impact --namespace bigquery --name ventes.suivi.commandes \
  --from 2026-11-11T06:00:00Z --to 2026-11-12T12:00:00Z --format json | jq '.counts'
```

```json
{
  "answers": 16,
  "recipients": 13,
  "lower_bound": false
}
```

### Une réponse de l'API est hostile

Avant d'afficher quoi que ce soit, en texte comme en JSON, la commande valide la réponse entière :

- elle est lue sans compression, et sa lecture s'arrête au-delà de 32 Mio, que sa longueur soit annoncée ou non ; une réponse compressée est refusée avant d'être déployée ;
- c'est du JSON en UTF-8, sans `NaN` ni infini, d'une profondeur que l'interpréteur lit ;
- chaque champ a son type et ses bornes : un décompte est un entier positif, un taux tient entre 0 et 1, un état ou une nature de trou appartient à une liste fermée, une date porte son fuseau ;
- chaque identifiant reçu (jeu de données, outil, agent, canal, empreinte, pseudonyme, émetteur) a la forme d'un nom, celle que l'[ingestion](ingestion.md#identifiants) admet : une séquence d'échappement du terminal, un saut de ligne qui simulerait une autre ligne de la sortie, une balise, un caractère invisible, une phrase ou un mot de passe dans une adresse ne passent pas ;
- chaque nœud cité par une réponse existe dans la vue.

Sinon la commande échoue avec le code `5`, n'affiche rien, et désigne l'endroit fautif par les clés du contrat, sans recopier la valeur :

```text
Réponse de l'API invalide en /graph/tools/0/name : un champ manque ou n'a pas la forme attendue. Rien n'est affiché.
```

Un refus de l'API est dit par un message écrit d'avance, choisi d'après son code (`unknown_dataset`, `query_timeout`, `host_not_allowed`…) ; un code inconnu n'est pas recopié, seul le statut HTTP l'est.

## `answerlineage doctor`

```bash
answerlineage doctor
```

```text
Vérification de l'installation : http://127.0.0.1:8000

  bon          API          L'API répond : API 1.0.0.
  bon          Adresse      L'API accepte cette adresse (en-têtes Host et Origin).
  bon          Base         L'API lit sa base PostgreSQL.
  bon          Migrations   Le schéma de la base est à jour.
  anomalie     File         queue_stalled : La file d'ingestion n'avance pas : 12 événements en attente, le plus ancien depuis 1 h 02 min (seuil : 5 min).
                            Remède : Lancer un worker (answerlineage-worker) ; s'il tourne, lire son journal : un événement fautif y est remis à plus tard, de plus en plus espacé.
  bon          Émetteurs    3 émetteurs connus, aucun muet.
  bon          Partitions   Aucune partition mensuelle ne manque.
  bon          Explorateur  L'explorateur est servi : http://127.0.0.1:8000/
  non vérifié  Workers      Les workers ne publient aucun signal de vie : leur marche se lit dans la file, qui avance, et dans le contrôle des émetteurs muets, qui ne tourne qu'avec eux.

1 anomalie.
```

Chaque vérification rend `bon`, `anomalie` ou `non vérifié`. Une vérification qui dépend d'une autre en échec n'est pas faite, et le dit : quand l'API ne répond pas, rien d'autre n'est vérifié ; quand la base est injoignable, ni les migrations, ni la file, ni les émetteurs, ni les partitions. Ce que la commande ne peut pas savoir n'est jamais donné pour bon.

| Vérification | Ce qui est vérifié | Anomalies |
| --- | --- | --- |
| `api` | L'API répond à sa route de santé, dans une version majeure que la commande sait lire | `api_unreachable`, `api_invalid_response`, `api_version_unsupported` |
| `address` | L'API accepte l'adresse utilisée : son nom d'hôte (`Host`) et son origine (`Origin`), celle d'une page qui serait servie à cette adresse | `host_not_allowed`, `origin_not_allowed`, `address_refused` |
| `database` | L'API lit sa base dans son délai | `database_unavailable` |
| `migrations` | Le schéma de la base est à la dernière révision des migrations | `schema_not_migrated`, `schema_behind`, `schema_unknown_revision` |
| `queue` | La file d'ingestion avance : le plus ancien événement en attente n'attend pas depuis plus que `--queue-max-age` | `queue_stalled` |
| `emitters` | Aucun émetteur n'est signalé [muet](workers.md#émetteurs-muets). Sans émetteur qui publie de relevés, leur silence ne se mesure pas : non vérifié | `emitters_muted` |
| `partitions` | Aucune [partition mensuelle](workers.md#partitions-mensuelles) n'est signalée manquante | `partitions_missing` |
| `explorer` | L'adresse sert la page de l'explorateur | `explorer_missing` |
| `workers` | Jamais vérifié : les workers ne publient aucun signal de vie. La file et le contrôle des émetteurs muets témoignent d'eux | |

| Option | Rôle | Défaut |
| --- | --- | --- |
| `--queue-max-age` | Âge, en secondes, au-delà duquel le plus ancien événement en attente signale une file qui n'avance pas | 300 |
| `--format` | `text` ou `json` | `text` |

Le code de sortie est `0` sans anomalie, `1` avec au moins une, `2` pour un usage fautif. Une API injoignable est une anomalie comme une autre : `1`.

### Anomalies et remèdes

Chaque anomalie a un code stable, un message et un remède écrits d'avance. Aucun texte reçu de l'API n'y est recopié : seulement des nombres, et l'adresse donnée à la commande.

| Code | Ce qu'il dit | Remède |
| --- | --- | --- |
| `api_unreachable` | L'API ne répond pas à cette adresse | La lancer, ou corriger l'adresse (`--url`, `ANSWERLINEAGE_API_URL`) |
| `api_invalid_response` | L'adresse ne répond pas comme une API AnswerLineage que la commande sait lire | Vérifier l'adresse, et que la commande est de la version de l'API |
| `api_version_unsupported` | L'API est d'une autre version majeure | Installer la commande de la version de l'API |
| `host_not_allowed` | L'API refuse le nom d'hôte de l'adresse | L'ajouter à `ANSWERLINEAGE_ALLOWED_HOSTS` sur l'API, ou passer par la boucle locale |
| `origin_not_allowed` | L'API refuse l'origine de l'adresse : une page servie là ne pourrait pas l'appeler | L'ajouter à `ANSWERLINEAGE_ALLOWED_ORIGINS`, ou son hôte à `ANSWERLINEAGE_ALLOWED_HOSTS` |
| `address_refused` | L'API refuse les requêtes faites à cette adresse | Vérifier les hôtes et les origines attendus de l'API |
| `database_unavailable` | L'API ne lit pas sa base, ou pas dans son délai | Vérifier PostgreSQL et `ANSWERLINEAGE_DATABASE_URL` côté API |
| `schema_not_migrated` | Aucune migration n'est appliquée | `python -m answerlineage_api.store.migrate upgrade` ; dans la démonstration, `docker compose run --rm migrate` |
| `schema_behind` | Des migrations restent à appliquer | Le même |
| `schema_unknown_revision` | La base porte une révision que l'API ne connaît pas : l'API est plus ancienne que la base | Mettre l'API à la version qui a migré la base |
| `queue_stalled` | La file n'avance pas : combien attendent, depuis quand, le seuil | Lancer un worker ; s'il tourne, lire son journal |
| `emitters_muted` | Combien d'émetteurs sont muets | Vérifier qu'ils tournent et joignent l'API ; `GET /api/v1/coverage` les nomme |
| `partitions_missing` | Combien de partitions manquent : l'ingestion d'un mois sans partition échouera | Vérifier qu'un worker tourne ; son journal nomme la table et le mois |
| `explorer_missing` | L'adresse ne sert pas l'explorateur | `make ui`, puis l'API avec `ANSWERLINEAGE_UI_DIR=ui/dist` |

### Sortie JSON

`--format json` rend le même rapport, sous une forme stable :

```json
{
  "url": "http://127.0.0.1:8000",
  "checks": [
    {
      "check": "queue",
      "status": "anomaly",
      "code": "queue_stalled",
      "message": "La file d'ingestion n'avance pas : 12 événements en attente, le plus ancien depuis 1 h 02 min (seuil : 5 min).",
      "remedy": "Lancer un worker (answerlineage-worker) ; s'il tourne, lire son journal : un événement fautif y est remis à plus tard, de plus en plus espacé."
    }
  ],
  "anomalies": ["queue_stalled"]
}
```

| Champ | Contenu |
| --- | --- |
| `url` | L'adresse vérifiée |
| `checks` | Les neuf vérifications, toujours dans l'ordre du tableau plus haut |
| `checks[].check` | Le nom de la vérification |
| `checks[].status` | `ok`, `anomaly` ou `not_checked` |
| `checks[].code` | Le code de l'anomalie, nul sinon |
| `checks[].message`, `checks[].remedy` | Les textes écrits d'avance ; `remedy` est nul hors d'une anomalie |
| `anomalies` | Les codes des anomalies trouvées, vide pour une installation saine |

Un script lit `status` et `code` ; `message` et `remedy` sont faits pour être lus.

### La route de santé

`doctor` lit l'état de la base, des migrations, de la file, des émetteurs et des partitions par une route de l'API, `GET /api/v1/health`. Elle est petite et bornée : quelques requêtes indexées, des décomptes plafonnés, sous le [délai maximal des lectures](lecture.md#délai-maximal). Elle ne dit aucun détail interne, ni révision du schéma, ni adresse de la base, ni message d'erreur : seulement des états d'une liste fermée et des nombres.

```json
{
  "api_version": "1.0.0",
  "database": "available",
  "migrations": "current",
  "queue": { "pending": 12, "lower_bound": false, "oldest_age_seconds": 3725 },
  "emitters": { "known": 3, "muted": 0, "lower_bound": false },
  "partitions": { "missing": 0, "lower_bound": false }
}
```

| Champ | Contenu |
| --- | --- |
| `api_version` | La version de l'API |
| `database` | `available` quand l'API lit sa base, `unavailable` sinon. Une base injoignable n'est pas une erreur de la route : elle répond `200` |
| `migrations` | `current`, `behind`, `unknown_revision` ou `not_migrated` ; nul quand la base est indisponible |
| `queue` | Événements en attente de projection, et depuis combien de secondes attend le plus ancien, à l'horloge de la base ; nul quand la file est vide |
| `emitters` | Émetteurs connus, ceux qui ont publié un relevé, et ceux que le contrôle des workers a signalés muets sans relevé reçu depuis |
| `partitions` | Partitions mensuelles signalées manquantes par les workers |

`queue`, `emitters` et `partitions` sont nuls tant que le schéma n'est pas à jour. Chaque décompte s'arrête à 10 000 : au-delà, `lower_bound` vaut `true`. Comme toute route, elle n'accepte que les hôtes et les origines attendus, et une lecture qui dépasse son délai répond `503` (`query_timeout`).

## `answerlineage demo`

Depuis un clone du dépôt, avec Docker :

```bash
answerlineage demo
```

```text
La démonstration répond.
Explorateur : http://127.0.0.1:8000/
Vérifier l'installation : answerlineage doctor
Arrêter : docker compose down, depuis /chemin/vers/AnswerLineage ; avec --volumes, ses données aussi.
```

La commande trouve la racine du clone en remontant depuis le dossier courant, ou depuis `--directory` : le dossier qui porte `deploy/Dockerfile` et le `compose.yaml` du projet `answerlineage`. Elle y lance `docker compose up --build --detach` : la [démonstration](demonstration.md) de `compose.yaml`, que le pipeline lance aussi (`scripts/check_demo.sh`). Elle n'en décrit aucun service elle-même : il n'y a qu'un chemin de démonstration à maintenir. Elle attend ensuite que l'API réponde, que son schéma soit migré et que l'explorateur soit servi. L'entreprise synthétique continue de se charger : le premier graphe arrive en quelques minutes la première fois.

Hors d'un clone, elle dit comment en obtenir un et s'arrête :

```text
Ce dossier n'est pas dans un clone d'AnswerLineage : la démonstration se lance depuis le dépôt. git clone https://github.com/alebgl77/AnswerLineage.git && cd AnswerLineage && answerlineage demo
```

| Option | Rôle | Défaut |
| --- | --- | --- |
| `--directory` | Un dossier du clone | le dossier courant |
| `--wait` | Délai d'attente de la démonstration, en secondes, de 1 à 3 600, après `docker compose up` | 120 |
| `--url` | L'adresse où l'attendre ; la démonstration n'écoute que sur la boucle locale, une autre adresse est refusée | `http://127.0.0.1:8000` |

| Code | Sens |
| --- | --- |
| `0` | La démonstration répond ; l'adresse de l'explorateur est donnée |
| `1` | Docker est absent, ou `docker compose up` a échoué : sa sortie est au-dessus |
| `2` | Hors d'un clone, ou adresse hors de la boucle locale |
| `4` | La démonstration ne répond pas dans le délai : `docker compose logs api` dit pourquoi |

## Tous les codes de sortie

| Code | `impact` | `doctor` | `demo` |
| --- | --- | --- | --- |
| `0` | Réponses touchées, liste complète | Aucune anomalie | La démonstration répond |
| `1` | Aucune réponse touchée, résultat complet | Au moins une anomalie | Docker absent ou en échec |
| `2` | Usage fautif | Usage fautif | Usage fautif, hors d'un clone |
| `3` | Résultat incomplet | | |
| `4` | API injoignable | | La démonstration ne répond pas |
| `5` | Refus de l'API ou réponse invalide | | |

## Journaux

La commande n'écrit dans aucun journal. Le client HTTP qu'elle emploie, httpx, journalise l'adresse complète de chaque requête, paramètres compris : le nom d'un jeu de données et la fenêtre d'un incident y figureraient. La commande relève le niveau de ces journaux au démarrage, même sous un journal racine réglé au plus bavard, et un test canari le vérifie.

## Limites connues

- L'API n'authentifie pas ses appelants : la commande non plus. Elle est faite pour une API sur la boucle locale ou derrière un contrôle d'accès.
- `impact` lit une page de réponses à la fois, 200 au plus ; les décomptes sont ceux de tout l'incident, et `--cursor` donne la suite.
- `doctor` ne voit pas les workers : une file vide ne dit pas qu'un worker tourne, et sans worker, aucun émetteur n'est signalé muet.
- `demo` attend la démonstration sur le port que publie `compose.yaml`, 8000 : il doit être libre.
- La commande ignore les variables de mandataire : une API qu'on ne joint que par un mandataire ne lui est pas accessible.
