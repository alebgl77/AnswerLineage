# Ligne de commande

`answerlineage` fait depuis le terminal ce que fait l'explorateur, et un peu plus : propager un incident jusqu'aux réponses et aux destinataires touchés, classer les jeux de données par exposition, déclarer un incident et lire son analyse, consulter et rejouer la file de rebut, vérifier une installation, lancer la démonstration. Elle parle à l'API par HTTP, comme l'explorateur, et ne lit jamais la base. Elle lance aussi le proxy MCP.

| Sous-commande | Ce qu'elle fait |
| --- | --- |
| [`answerlineage impact`](#answerlineage-impact) | Les réponses et les destinataires touchés par un jeu de données faux pendant une fenêtre ; le code de sortie dit si l'on peut conclure |
| [`answerlineage exposure`](#answerlineage-exposure) | Les jeux de données et leurs colonnes, les plus exposés d'abord, selon les réponses qui en dépendent et leurs destinataires |
| [`answerlineage incident`](#answerlineage-incident) | Déclare un incident de données, puis lit l'analyse d'impact que les workers en font ; le code de sortie de `show` dit si l'on peut conclure |
| [`answerlineage dead-letters`](#answerlineage-dead-letters) | Consulte la file de rebut, les événements que la projection n'a pas pu intégrer au graphe, et les remet en file une fois le correctif déployé |
| [`answerlineage doctor`](#answerlineage-doctor) | Vérifie chaque composant d'une installation et liste les anomalies, chacune avec son remède |
| [`answerlineage demo`](#answerlineage-demo) | Lance la démonstration d'un clone du dépôt, attend qu'elle réponde et donne l'adresse de l'explorateur |
| [`answerlineage proxy`](#answerlineage-proxy) | Le proxy MCP : devant un serveur HTTP, ou qui lance un serveur stdio donné après `--` |

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

## `answerlineage exposure`

Les jeux de données dont dépendent le plus de réponses et de destinataires, et leurs colonnes, les plus exposés d'abord : le [score d'exposition](lecture.md#score-dexposition). L'exemple est un scénario du corpus de vérité, `exposition-colonne-unique` :

```bash
answerlineage exposure --at 2026-11-01T12:00:00Z
```

```text
Exposition des jeux de données au 2026-11-01T12:00:00Z
Score : chaque réponse qui dépend d'un jeu de données y pèse 1 plus un par destinataire, moitié moins tous les 30 jours.

Jeux de données, les plus exposés d'abord (2 sur cette page) :
  10,192078  finance.ref.taux_change (espace de noms bigquery, table)
    5 réponses, 6 destinataires
    colonnes : date_valeur 6,423125 (2 réponses), devise 5,315637 (4 réponses), taux 5,315637 (4 réponses)
  4,380814  finance.compta.factures (espace de noms bigquery, table)
    2 réponses, 3 destinataires
    colonnes : montant 2,604141 (1 réponse), client_id 1,776673 (1 réponse)

Classement complet : chaque réponse qui dépend d'un jeu de données y est comptée.
```

La sortie lisible dit l'heure de référence et la règle du score, puis chaque jeu de données : son score au millionième, sans zéro inutile, ses réponses et ses destinataires distincts, ses colonnes les plus exposées, et s'il en a d'autres. Elle finit par ce que vaut le classement.

| Option | Rôle | Défaut |
| --- | --- | --- |
| `--limit` | Jeux de données par page, de 1 à 200 | 50 |
| `--cursor` | La page suivante : la valeur de `next_cursor`, que la sortie lisible rappelle ; elle garde l'heure de référence | aucun |
| `--at` | L'heure de référence du score, en RFC 3339 avec fuseau | la date de `--known-at`, sinon l'heure de la base |
| `--known-at` | Lit le graphe tel qu'il était connu à cette date ([graphe à une date](lecture.md#le-graphe-tel-que-connu-à-une-date)) | aucune |
| `--format` | `text` ou `json` ; en JSON, la réponse de `GET /api/v1/exposure`, réduite aux champs du contrat (`ExposureList`) | `text` |

Ce sont les paramètres de `GET /api/v1/exposure`. La réponse est validée comme celle d'`impact` ([une réponse de l'API est hostile](#une-réponse-de-lapi-est-hostile)) : un nom de jeu de données ou de colonne a la forme d'un nom, un score est un nombre fini et positif, un décompte un entier positif, une page qui annonce une suite donne son curseur.

| Code | Sens |
| --- | --- |
| `0` | Classement complet, vide compris quand aucune réponse ne dépend d'un jeu de données |
| `2` | Usage fautif : option mal formée, date hors des bornes ; ou paramètre refusé par l'API (`422`), qui nomme l'option |
| `3` | Classement incomplet : une lecture du graphe a dépassé sa borne. Les scores sont des minimums, et l'ordre peut différer de celui du graphe entier |
| `4` | L'API ne répond pas, ou pas dans le délai |
| `5` | L'API a refusé la requête (délai de lecture dépassé, adresse refusée) ou rendu une réponse invalide |

## `answerlineage incident`

Un [incident de données](incidents.md) se déclare avec son périmètre, sa fenêtre de validité et sa gravité ; l'API le crée à l'état « qualifié » et en demande l'analyse d'impact, que les workers font. `declare` déclare, `show` lit l'incident et son analyse, `quarantine` le met en quarantaine ou la lève.

### `answerlineage incident declare`

```bash
answerlineage incident declare --dataset bigquery ventes.suivi.commandes --from 2026-11-11T06:00:00Z --to 2026-11-12T12:00:00Z --severity high
```

```text
Incident déclaré : 974e7c80-6643-4065-a541-e4c029abbdab
  état : qualifié
  gravité : élevée (high)
  périmètre : ventes.suivi.commandes (espace de noms bigquery)
  fenêtre : du 2026-11-11T06:00:00Z au 2026-11-12T12:00:00Z, fin exclue
  déclaré le 2026-11-16T08:53:16.793908Z
Analyse : demandée, en attente d'un worker.
Suivre l'analyse : answerlineage incident show 974e7c80-6643-4065-a541-e4c029abbdab
```

| Option | Rôle | Défaut |
| --- | --- | --- |
| `--dataset ESPACE NOM` | Un jeu de données faux, par son espace de noms et son nom ; à répéter, 20 fois au plus, sans doublon | obligatoire |
| `--from`, `--to` | La fenêtre de validité, début inclus, fin exclue, en RFC 3339 avec fuseau ; `--to` suit `--from`, de 31 jours au plus | obligatoires |
| `--severity` | La gravité : `low`, `medium`, `high` ou `critical` ([gravité](incidents.md#gravité)) | obligatoire |
| `--format` | `text` ou `json` ; en JSON, l'incident que rend `POST /api/v1/incidents` | `text` |

La commande contrôle la déclaration avant de l'envoyer, comme l'API la contrôlera : chaque espace de noms et chaque nom ont la forme d'un nom, celle que l'[ingestion](ingestion.md#identifiants) admet ; la fenêtre n'est ni vide, ni inversée, ni trop longue. Une déclaration fautive est un usage fautif, et le message ne recopie aucune valeur :

```text
Usage fautif : --to doit suivre --from.
```

| Code | Sens |
| --- | --- |
| `0` | L'incident est déclaré, son analyse demandée |
| `2` | Usage fautif : option absente ou mal formée, fenêtre vide, inversée ou trop longue, nom refusé ; ou déclaration refusée par l'API (`422`), qui nomme l'option |
| `4` | L'API ne répond pas, ou pas dans le délai |
| `5` | L'API a refusé la requête ou rendu une réponse invalide |

### `answerlineage incident show`

```bash
answerlineage incident show 974e7c80-6643-4065-a541-e4c029abbdab
```

```text
Incident 974e7c80-6643-4065-a541-e4c029abbdab
  état : qualifié
  gravité : élevée (high)
  périmètre : ventes.suivi.commandes (espace de noms bigquery)
  fenêtre : du 2026-11-11T06:00:00Z au 2026-11-12T12:00:00Z, fin exclue
  déclaré le 2026-11-16T08:53:16.793908Z
  quarantaine : aucune
Analyse : faite le 2026-11-16T08:53:16.849328Z, sur le graphe tel que connu le 2026-11-16T08:53:16.814594Z

16 réponses touchées, 13 destinataires
Couverture du lignage : mesurée, 100 % des appels vus sont dans le graphe, sans trou connu

Réponses touchées, les plus récentes d'abord (16 sur cette page) :
  2026-11-12T10:47:56Z  01a1cf5e5bb078b991a908d485d5acfdb4c70313c6faa097
    destinataires : 821743431def23772bbc6215495e30c7, a71b66a14ed697845fee1dc093de55a0, d3675030f72fc8499edef4ab380c2c44
  …

Destinataires, pseudonymisés (13 sur cette page) :
  237937de45af1b1cc4811a3b5a1152bd
  …

Liste complète : aucune réponse touchée ne peut y manquer.
```

C'est le premier incident de la démonstration : l'analyse trouve exactement sa vérité. Tant que l'analyse attend un worker, la commande le dit (`Analyse : en attente d'un worker.`) et sort avec le code `6` ; une fois faite, la sortie suit celle d'[`impact`](#answerlineage-impact) : les décomptes de tout l'incident, la couverture et ses trous, chaque réponse touchée avec ses destinataires, et ce que le résultat permet de conclure. Elle nomme aussi les jeux du périmètre que le graphe ne connaissait pas.

| Option | Rôle | Défaut |
| --- | --- | --- |
| `IDENTIFIANT` | L'identifiant de l'incident, tel que le rend `declare` | obligatoire |
| `--limit` | Réponses touchées par page, de 1 à 200 | 200 |
| `--cursor` | La page suivante : la valeur de `next_cursor`, que la sortie lisible rappelle | aucun |
| `--format` | `text` ou `json` ; en JSON, la réponse de `GET /api/v1/incidents/{id}`, réduite aux champs du contrat | `text` |

| Code | Sens |
| --- | --- |
| `0` | Analyse faite : des réponses sont touchées, et la liste est complète |
| `1` | Analyse faite : aucune réponse touchée, et le résultat le prouve, complet sous une couverture mesurée sans trou |
| `2` | Usage fautif : identifiant mal formé, option mal formée ; ou paramètre refusé par l'API |
| `3` | Analyse faite, résultat incomplet : tronqué, couverture non mesurée, partielle ou trouée, ou jeu du périmètre inconnu du graphe. Zéro réponse ne prouve rien |
| `4` | L'API ne répond pas, ou pas dans le délai |
| `5` | L'API a refusé la requête (incident inconnu, délai de lecture dépassé, adresse refusée) ou rendu une réponse invalide |
| `6` | L'analyse attend un worker, ou a été remise à plus tard après un échec : relancer plus tard |
| `7` | L'analyse est en échec ; la sortie dit pourquoi, par un message écrit d'avance |

La réponse est validée comme celle d'`impact` ([une réponse de l'API est hostile](#une-réponse-de-lapi-est-hostile)), et un peu plus : l'incident rendu est celui demandé, un état et ce qu'il porte vont ensemble (un résultat pour une analyse faite, et seulement pour elle ; un code d'échec pour une analyse en échec ; une heure de levée pour une quarantaine levée, et seulement pour elle), et chaque état, gravité, mode ou code d'échec appartient à sa liste fermée. La ligne `quarantaine` dit la dernière quarantaine de l'incident : `aucune`, `active depuis le …` avec son mode, ou `levée le …`.

### `answerlineage incident quarantine`

```bash
answerlineage incident quarantine 974e7c80-6643-4065-a541-e4c029abbdab
answerlineage incident quarantine 974e7c80-6643-4065-a541-e4c029abbdab --lift
```

```text
Incident 974e7c80-6643-4065-a541-e4c029abbdab mis en quarantaine le 2026-11-16T08:55:01.204517Z, signaler les lectures (warn).
Les proxys reçoivent son périmètre à leur prochaine synchronisation, 30 s au plus.
Lever la quarantaine : answerlineage incident quarantine 974e7c80-6643-4065-a541-e4c029abbdab --lift
```

Met l'incident en [quarantaine](incidents.md#quarantaine) (`POST /api/v1/incidents/{id}/quarantine`) : chaque proxy reçoit son périmètre, ses jeux de données, à sa synchronisation suivante. `--lift` la lève (`POST …/quarantine/lift`). Les deux sont idempotents : une quarantaine déjà active, ou déjà levée, est rendue telle quelle, et la commande dit que rien n'a changé.

| Option | Rôle | Défaut |
| --- | --- | --- |
| `IDENTIFIANT` | L'identifiant de l'incident, tel que le rend `declare` | obligatoire |
| `--mode` | Ce que les proxys feront des lectures du périmètre, d'une liste fermée : `warn`, les signaler, le seul mode aujourd'hui | `warn` |
| `--lift` | Lève la quarantaine, au lieu de l'activer ; ne se donne pas avec `--mode` | |
| `--format` | `text` ou `json` ; en JSON, la réponse de l'API : l'incident, `changed`, et la quarantaine telle qu'elle est désormais | `text` |

| Code | Sens |
| --- | --- |
| `0` | L'incident est en quarantaine, ou sa quarantaine est levée, par cette commande ou avant elle |
| `2` | Usage fautif : identifiant mal formé, mode hors de la liste, `--mode` avec `--lift` ; ou mode refusé par l'API (`422`) |
| `4` | L'API ne répond pas, ou pas dans le délai |
| `5` | L'API a refusé la requête (incident inconnu, 100 quarantaines déjà actives, levée d'un incident jamais mis en quarantaine) ou rendu une réponse invalide |

La réponse est validée en entier : l'incident est celui demandé, la quarantaine est dans l'état demandé, active ou levée, avec ses heures, et son mode appartient à sa liste.

## `answerlineage dead-letters`

Un événement que les workers ne parviennent pas à intégrer au graphe va à la [file de rebut](workers.md#file-de-rebut), avec son émetteur et le motif de l'échec. `list` la consulte, `replay` la remet en file, une fois le correctif déployé. Ce sont les routes `GET` et `POST` de la file de rebut ([API d'ingestion](ingestion.md#file-de-rebut)).

### `answerlineage dead-letters list`

```bash
answerlineage dead-letters list --emitter mcp://catalogue
```

```text
Rebut de mcp://catalogue : 2 événements
  constraint_violation       1  l'écriture du graphe viole une contrainte de la base
  database_error             1  la base a échoué à chaque essai

Les plus récemment mis au rebut d'abord (2 sur cette page) :
  2026-11-16T09:42:10.031000Z  database_error  mcp://catalogue
    événement 9c1e7d04b2a6f3851e0c9d7a4b3f2e1d0c9b8a7f6e5d4c3b2a1f0e9d8c7b6a5f
    run 0193a9c5-7a10-7c3e-9d5e-2f1b6c8a0002, reçu le 2026-11-16T09:00:02.410000Z
    10 essais en échec, le premier le 2026-11-16T08:59:29.870000Z
  2026-11-16T09:00:01.500000Z  constraint_violation  mcp://catalogue
    événement 2a8b9477e2742f3eaeb18f4910603feb800eff1bc80f577f9d0e9cc4851eab10
    run 0193a9c5-7a10-7c3e-9d5e-2f1b6c8a0001, reçu le 2026-11-16T09:00:00.120000Z
    1 essai en échec, le premier le 2026-11-16T09:00:01.500000Z

Une fois le correctif déployé, les rejouer : answerlineage dead-letters replay --emitter mcp://catalogue
```

La sortie lisible donne les décomptes par motif, chacun avec ce qu'il veut dire, puis chaque événement : l'heure de sa mise au rebut, son motif, son émetteur, son identifiant, son run, sa réception, ses essais en échec. Elle finit par la commande qui rejoue ce que la liste montre. Aucun contenu d'événement : le rebut n'en garde pas.

| Option | Rôle | Défaut |
| --- | --- | --- |
| `--emitter` | Seulement cet émetteur, par l'espace de noms de ses jobs (512 caractères au plus) | aucun |
| `--reason` | Seulement ce motif : `invalid_identifier`, `invalid_event`, `constraint_violation`, `invalid_value`, `database_error` ou `projection_error` ([motifs](workers.md#motifs)) | aucun |
| `--limit` | Événements par page, de 1 à 500 | 100 |
| `--cursor` | La page suivante : la valeur de `next_cursor`, que la sortie lisible rappelle | aucun |
| `--format` | `text` ou `json` ; en JSON, la réponse de `GET /api/v1/ingestion/dead-letters`, réduite aux champs du contrat | `text` |

Les décomptes suivent `--emitter`, pas `--reason` : ils disent la répartition des motifs. Au-delà de 100 000 événements, ce sont des minimums, et la sortie le dit (« au moins »).

| Code | Sens |
| --- | --- |
| `0` | Aucun événement n'attend au rebut, sous ces filtres |
| `1` | Des événements attendent au rebut : de quoi alerter depuis un script |
| `2` | Usage fautif : émetteur, motif ou option mal formés ; ou paramètre refusé par l'API (`422`), qui nomme l'option |
| `4` | L'API ne répond pas, ou pas dans le délai |
| `5` | L'API a refusé la requête (délai de lecture dépassé, adresse refusée) ou rendu une réponse invalide |

### `answerlineage dead-letters replay`

```bash
answerlineage dead-letters replay --reason constraint_violation
```

```text
1 événement remis en file : un worker les projettera dans le graphe ; un événement qui échoue encore reviendra au rebut.
```

| Option | Rôle | Défaut |
| --- | --- | --- |
| `--emitter` | Seulement les événements de cet émetteur | aucun |
| `--reason` | Seulement ceux mis au rebut pour ce motif | aucun |
| `--event-id IDENTIFIANT` | Seulement cet événement, par l'identifiant que donne `list` ; à répéter, 1 000 fois au plus | aucun |
| `--all` | Tout le rebut, sans filtre | non |
| `--limit` | Événements remis en file au plus, les plus anciens échecs d'abord, de 1 à 10 000 | 1 000 |
| `--format` | `text` ou `json` ; en JSON, le bilan que rend `POST /api/v1/ingestion/dead-letters/replay` | `text` |

Les filtres se cumulent. Rejouer tout le rebut se demande par `--all`, et lui seul : une commande sans filtre, ou `--all` avec un filtre, est un usage fautif. Un filtre oublié ne rejoue donc pas tout le rebut. Le rejeu est sans risque : la projection est idempotente, et un événement qui échoue encore revient au rebut avec son nouveau motif.

| Code | Sens |
| --- | --- |
| `0` | Des événements sont remis en file, et aucun ne reste au rebut sous ces filtres |
| `1` | Aucun événement à rejouer sous ces filtres |
| `2` | Usage fautif : aucun filtre ni `--all`, `--all` avec un filtre, identifiant ou option mal formés ; ou paramètre refusé par l'API (`422`) |
| `3` | D'autres événements restent au rebut sous ces filtres : la borne `--limit` est atteinte, ou un autre rejeu les tenait. Relancer la même commande |
| `4` | L'API ne répond pas, ou pas dans le délai |
| `5` | L'API a refusé la requête ou rendu une réponse invalide |

La réponse est validée comme celle d'`impact` ([une réponse de l'API est hostile](#une-réponse-de-lapi-est-hostile)) : un identifiant d'événement ou de run a sa forme, un émetteur celle d'un nom, un motif appartient à sa liste fermée, et une page qui annonce une suite donne son curseur.

Les routes du rebut n'authentifient pas encore leurs appelants : la commande non plus. Le rejeu sera réservé à l'administrateur quand l'API distinguera les rôles de ses appelants.

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
  bon          Rebut        La file de rebut est vide.
  bon          Émetteurs    3 émetteurs connus, aucun muet.
  bon          Partitions   Aucune partition mensuelle ne manque.
  bon          Réémission   La réémission avance : file vide ; 1520 remis, 0 perdu.
  bon          Webhooks     Les webhooks avancent : file vide ; 12 remis, 0 perdu.
  bon          Explorateur  L'explorateur est servi : http://127.0.0.1:8000/
  non vérifié  Workers      Les workers ne publient aucun signal de vie : leur marche se lit dans la file, qui avance, et dans le contrôle des émetteurs muets, qui ne tourne qu'avec eux.

1 anomalie.
```

Chaque vérification rend `bon`, `anomalie` ou `non vérifié`. Une vérification qui dépend d'une autre en échec n'est pas faite, et le dit : quand l'API ne répond pas, rien d'autre n'est vérifié ; quand la base est injoignable, ni les migrations, ni la file, ni le rebut, ni les émetteurs, ni les partitions, ni la réémission, ni les webhooks. Ce que la commande ne peut pas savoir n'est jamais donné pour bon.

| Vérification | Ce qui est vérifié | Anomalies |
| --- | --- | --- |
| `api` | L'API répond à sa route de santé, dans une version majeure que la commande sait lire | `api_unreachable`, `api_invalid_response`, `api_version_unsupported` |
| `address` | L'API accepte l'adresse utilisée : son nom d'hôte (`Host`) et son origine (`Origin`), celle d'une page qui serait servie à cette adresse | `host_not_allowed`, `origin_not_allowed`, `address_refused` |
| `database` | L'API lit sa base dans son délai | `database_unavailable` |
| `migrations` | Le schéma de la base est à la dernière révision des migrations | `schema_not_migrated`, `schema_behind`, `schema_unknown_revision` |
| `queue` | La file d'ingestion avance : le plus ancien événement en attente n'attend pas depuis plus que `--queue-max-age` | `queue_stalled` |
| `dead_letters` | Aucun événement n'attend à la [file de rebut](workers.md#file-de-rebut) : la projection a intégré au graphe tout ce qu'elle a reçu. Un seul suffit à l'anomalie : son lignage manque au graphe tant qu'il n'est pas rejoué | `dead_letters_waiting` |
| `emitters` | Aucun émetteur n'est signalé [muet](workers.md#émetteurs-muets). Sans émetteur qui publie de relevés, leur silence ne se mesure pas : non vérifié | `emitters_muted` |
| `partitions` | Aucune [partition mensuelle](workers.md#partitions-mensuelles) n'est signalée manquante | `partitions_missing` |
| `reemission` | La [réémission](reemission.md) vers un consommateur OpenLineage externe n'est pas en retard : le plus ancien événement à réémettre n'attend pas depuis plus que `--reemission-max-age`. Le message dit aussi combien d'événements ont été remis et perdus depuis la mise en service. Sans destination réglée aux workers, rien n'est jamais mis en file : non vérifié | `reemission_lagging` |
| `webhooks` | Les [webhooks](webhooks.md) qui annoncent les incidents ne sont pas en retard : le plus ancien message à envoyer n'attend pas depuis plus que `--webhook-max-age`. Le message dit aussi combien de messages ont été remis et perdus depuis la mise en service. Sans webhook en service et sans message en file, rien n'est annoncé : non vérifié. Des messages laissés en file par un webhook qui n'est plus en service sont encore jugés | `webhooks_lagging` |
| `explorer` | L'adresse sert la page de l'explorateur | `explorer_missing` |
| `workers` | Jamais vérifié : les workers ne publient aucun signal de vie. La file et le contrôle des émetteurs muets témoignent d'eux | |

| Option | Rôle | Défaut |
| --- | --- | --- |
| `--queue-max-age` | Âge, en secondes, au-delà duquel le plus ancien événement en attente signale une file qui n'avance pas | 300 |
| `--reemission-max-age` | Âge, en secondes, au-delà duquel le plus ancien événement à réémettre signale une réémission en retard | 300 |
| `--webhook-max-age` | Âge, en secondes, au-delà duquel le plus ancien message de webhook à envoyer signale des webhooks en retard | 300 |
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
| `dead_letters_waiting` | Combien d'événements attendent au rebut, et depuis quand le plus ancien | Lire leur motif (`answerlineage dead-letters list`) et corriger sa cause, puis les rejouer (`answerlineage dead-letters replay`) |
| `emitters_muted` | Combien d'émetteurs sont muets | Vérifier qu'ils tournent et joignent l'API ; `GET /api/v1/coverage` les nomme |
| `partitions_missing` | Combien de partitions manquent : l'ingestion d'un mois sans partition échouera | Vérifier qu'un worker tourne ; son journal nomme la table et le mois |
| `reemission_lagging` | La réémission est en retard : combien d'événements attendent, depuis quand le plus ancien, le seuil, et combien se sont perdus | Vérifier que la destination répond et que les workers la joignent (leur journal donne la classe de l'échec ou le statut de la réponse), `ANSWERLINEAGE_REEMIT_URL` et `ANSWERLINEAGE_REEMIT_AUTHORIZATION` dans leur environnement, et qu'un worker tourne avec ce réglage. Au-delà des bornes de la file, les plus anciens sont perdus |
| `webhooks_lagging` | Les webhooks sont en retard : combien de messages attendent, depuis quand le plus ancien, le seuil, et combien se sont perdus | Vérifier que le récepteur répond et que les workers le joignent (leur journal donne la classe de l'échec ou le statut de la réponse ; un récepteur qui refuse la signature répond souvent `401` ou `403` : vérifier alors le secret), `ANSWERLINEAGE_WEBHOOK_URL` et `ANSWERLINEAGE_WEBHOOK_SECRET` dans leur environnement, et qu'un worker tourne avec ce réglage. Au-delà des bornes de la file, les plus anciens sont perdus |
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
| `checks` | Les douze vérifications, toujours dans l'ordre du tableau plus haut |
| `checks[].check` | Le nom de la vérification |
| `checks[].status` | `ok`, `anomaly` ou `not_checked` |
| `checks[].code` | Le code de l'anomalie, nul sinon |
| `checks[].message`, `checks[].remedy` | Les textes écrits d'avance ; `remedy` est nul hors d'une anomalie |
| `anomalies` | Les codes des anomalies trouvées, vide pour une installation saine |

Un script lit `status` et `code` ; `message` et `remedy` sont faits pour être lus.

### La route de santé

`doctor` lit l'état de la base, des migrations, de la file, du rebut, des émetteurs, des partitions, de la réémission et des webhooks par une route de l'API, `GET /api/v1/health`. Elle est petite et bornée : quelques requêtes indexées, des décomptes plafonnés, sous le [délai maximal des lectures](lecture.md#délai-maximal). Elle ne dit aucun détail interne, ni révision du schéma, ni adresse de la base, ni message d'erreur : seulement des états d'une liste fermée et des nombres.

```json
{
  "api_version": "1.0.0",
  "database": "available",
  "migrations": "current",
  "queue": { "pending": 12, "lower_bound": false, "oldest_age_seconds": 3725 },
  "dead_letters": { "events": 0, "lower_bound": false, "oldest_age_seconds": null },
  "emitters": { "known": 3, "muted": 0, "lower_bound": false },
  "partitions": { "missing": 0, "lower_bound": false },
  "reemission": {
    "queued": 0, "retrying": 0, "lower_bound": false, "oldest_age_seconds": null,
    "sent": 1520, "lost": 0, "rejected": 0
  },
  "webhooks": {
    "in_service": true, "queued": 0, "retrying": 0, "lower_bound": false,
    "oldest_age_seconds": null, "sent": 12, "lost": 0, "rejected": 0
  }
}
```

| Champ | Contenu |
| --- | --- |
| `api_version` | La version de l'API |
| `database` | `available` quand l'API lit sa base, `unavailable` sinon. Une base injoignable n'est pas une erreur de la route : elle répond `200` |
| `migrations` | `current`, `behind`, `unknown_revision` ou `not_migrated` ; nul quand la base est indisponible |
| `queue` | Événements en attente de projection, et depuis combien de secondes attend le plus ancien, à l'horloge de la base ; nul quand la file est vide |
| `dead_letters` | Événements au [rebut](workers.md#file-de-rebut), et l'âge du plus ancien en secondes, compté depuis sa mise au rebut à l'horloge de la base ; nul quand le rebut est vide. Le décompte et l'âge sont lus ensemble : ils s'accordent, même pendant un rejeu |
| `emitters` | Émetteurs connus, ceux qui ont publié un relevé, et ceux que le contrôle des workers a signalés muets sans relevé reçu depuis |
| `partitions` | Partitions mensuelles signalées manquantes par les workers |
| `reemission` | La [réémission](reemission.md#ce-qui-se-lit) vers un consommateur OpenLineage externe : événements à réémettre, ceux en reprise, l'âge du plus ancien en secondes (nul quand la file est vide), et depuis la mise en service, les événements remis, perdus, et parmi les perdus ceux que la destination a refusés |
| `webhooks` | Les [webhooks](webhooks.md#ce-qui-se-lit) des incidents : s'ils sont en service, messages à envoyer, ceux en reprise, l'âge du plus ancien en secondes (nul quand la file est vide), et depuis la mise en service, les messages remis, perdus, et parmi les perdus ceux que le récepteur a refusés |

`queue`, `dead_letters`, `emitters`, `partitions`, `reemission` et `webhooks` sont nuls tant que le schéma n'est pas à jour. Chaque décompte s'arrête à 10 000 : au-delà, `lower_bound` vaut `true`. Comme toute route, elle n'accepte que les hôtes et les origines attendus, et une lecture qui dépasse son délai répond `503` (`query_timeout`).

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

## `answerlineage proxy`

Le [proxy MCP](proxy.md), par la commande commune. Sans `--`, il se place devant un serveur MCP en HTTP ; avec, il lance la commande qui suit en stdio et se place entre elle et le client :

```bash
answerlineage proxy --server-name fichiers -- /opt/mcp/serveur-fichiers --racine /srv/partage
```

Dans la configuration d'un client MCP, la commande du serveur devient `answerlineage`, et ses arguments `proxy --server-name fichiers -- /opt/mcp/serveur-fichiers --racine /srv/partage`.

La sous-commande cède la place : le processus devient le proxy installé dans le même environnement, `python -m answerlineage_proxy.cli` avec les mêmes arguments. Rien ne s'intercale donc entre le client et le proxy : entrée, sorties, signaux, réglages (`answerlineage proxy --help` donne ceux du proxy) et codes de sortie sont ceux du proxy, en stdio celui du serveur ([Mode stdio](proxy.md#mode-stdio)). Elle n'emploie ni `--url` ni `--timeout`.

Le paquet `answerlineage-cli` ne dépend pas du proxy. Sans le paquet `answerlineage-proxy` dans le même environnement, la commande le dit et sort avec le code `2`.

## Tous les codes de sortie

| Code | `impact` | `exposure` | `incident declare` | `incident show` | `incident quarantine` | `dead-letters list` | `dead-letters replay` | `doctor` | `demo` |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `0` | Réponses touchées, liste complète | Classement complet | Incident déclaré | Réponses touchées, liste complète | Quarantaine active, ou levée | Rebut vide | Tout est remis en file | Aucune anomalie | La démonstration répond |
| `1` | Aucune réponse touchée, résultat complet | | | Aucune réponse touchée, résultat complet | | Des événements attendent | Rien à rejouer | Au moins une anomalie | Docker absent ou en échec |
| `2` | Usage fautif | Usage fautif | Usage fautif | Usage fautif | Usage fautif | Usage fautif | Usage fautif | Usage fautif | Usage fautif, hors d'un clone |
| `3` | Résultat incomplet | Classement incomplet | | Résultat incomplet | | | D'autres restent, relancer | | |
| `4` | API injoignable | API injoignable | API injoignable | API injoignable | API injoignable | API injoignable | API injoignable | | La démonstration ne répond pas |
| `5` | Refus de l'API ou réponse invalide | Refus de l'API ou réponse invalide | Refus de l'API ou réponse invalide | Refus de l'API ou réponse invalide | Refus de l'API ou réponse invalide | Refus de l'API ou réponse invalide | Refus de l'API ou réponse invalide | | |
| `6` | | | | Analyse en attente | | | | | |
| `7` | | | | Analyse en échec | | | | | |

`answerlineage proxy` rend les codes du proxy, et `2` quand le proxy n'est pas installé.

## Journaux

La commande n'écrit dans aucun journal. Le client HTTP qu'elle emploie, httpx, journalise l'adresse complète de chaque requête, paramètres compris : le nom d'un jeu de données et la fenêtre d'un incident y figureraient. La commande relève le niveau de ces journaux au démarrage, même sous un journal racine réglé au plus bavard, et un test canari le vérifie.

## Limites connues

- L'API n'authentifie pas ses appelants : la commande non plus. Elle est faite pour une API sur la boucle locale ou derrière un contrôle d'accès.
- `impact` lit une page de réponses à la fois, 200 au plus ; les décomptes sont ceux de tout l'incident, et `--cursor` donne la suite. `incident show` de même.
- `exposure` lit une page de jeux de données à la fois, 200 au plus, et 100 colonnes par jeu ; au-delà, la sortie le dit, sans moyen de lire les suivantes.
- `incident declare` n'attend pas l'analyse : `incident show` la lit quand un worker l'a faite.
- `doctor` ne voit pas les workers : une file vide ne dit pas qu'un worker tourne, et sans worker, aucun émetteur n'est signalé muet.
- `demo` attend la démonstration sur le port que publie `compose.yaml`, 8000 : il doit être libre.
- La commande ignore les variables de mandataire : une API qu'on ne joint que par un mandataire ne lui est pas accessible.
