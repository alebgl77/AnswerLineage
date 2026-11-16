# Proxy MCP

Le proxy se place devant un serveur MCP en HTTP, sans toucher à l'agent ni au serveur. Il relaie chaque requête à l'identique et émet un événement OpenLineage par appel d'outil.

```bash
uv run answerlineage-proxy --upstream http://127.0.0.1:9000 --server-name entrepot --sql-tool execute_sql=query:bigquery
```

| Réglage | Option | Variable | Défaut |
| --- | --- | --- | --- |
| Adresse du serveur MCP | `--upstream` | `ANSWERLINEAGE_PROXY_UPSTREAM` | obligatoire |
| Nom du serveur dans le lignage | `--server-name` | `ANSWERLINEAGE_PROXY_SERVER_NAME` | hôte du serveur |
| Adresse d'écoute | `--host` | `ANSWERLINEAGE_PROXY_HOST` | `127.0.0.1` |
| Port d'écoute | `--port` | `ANSWERLINEAGE_PROXY_PORT` | `8717` |
| Hôtes attendus dans `Host` | `--allow-host` | `ANSWERLINEAGE_PROXY_ALLOWED_HOSTS` | boucle locale et serveur MCP |
| Origines attendues dans `Origin` | `--allow-origin` | `ANSWERLINEAGE_PROXY_ALLOWED_ORIGINS` | celles des hôtes attendus |
| En-tête `Host` du serveur MCP | `--no-preserve-host` | | celui du client |
| Taille de corps lue | `--max-inspected-bytes` | | 1 Mo |
| Outil dont un argument porte du SQL | `--sql-tool` | | aucun |
| Budget de calcul de l'analyse d'une requête SQL | `--sql-budget-ms` | `ANSWERLINEAGE_PROXY_SQL_BUDGET_MS` | 50 ms (1 à 60 000) |
| Analyses du SQL en attente, au plus | `--sql-queue-size` | `ANSWERLINEAGE_PROXY_SQL_QUEUE_SIZE` | 32 (1 à 10 000) |
| Résultats d'analyse gardés en cache | `--sql-cache-size` | `ANSWERLINEAGE_PROXY_SQL_CACHE_SIZE` | 1 024 (0 pour aucun, jusqu'à 1 000 000) |
| Fichier de la clé d'empreinte | `--fingerprint-key-file` | `ANSWERLINEAGE_PROXY_FINGERPRINT_KEY_FILE` | aucun |
| Clé d'empreinte elle-même, en hexadécimal | | `ANSWERLINEAGE_PROXY_FINGERPRINT_KEY` | aucune : pas d'empreinte |
| Durée d'une fenêtre des relevés de couverture | `--coverage-window-seconds` | `ANSWERLINEAGE_PROXY_COVERAGE_WINDOW_SECONDS` | 60 s (1 à 86 400) |
| Fichier des règles de lecture des outils non SQL | `--read-rules-file` | `ANSWERLINEAGE_PROXY_READ_RULES_FILE` | aucun : aucun outil non SQL n'est décrit |

L'adresse du serveur MCP ne porte ni identifiant ni paramètre : elle peut apparaître dans la configuration. Son hôte peut être une adresse IPv6, entre crochets (`http://[2001:db8::1]:9000`) ; une adresse IPv6 ne faisant pas un nom de serveur, `--server-name` est alors obligatoire. Un réglage invalide arrête le proxy avec le message « Configuration invalide : … » et le code de sortie 2.

## Ce qui est relayé

- Toutes les méthodes HTTP, le chemin et les paramètres tels que reçus, le chemin à la suite de celui de l'adresse du serveur MCP. Un chemin qui contient un segment `.` ou `..`, même encodé (`%2e`), sortirait de ce préfixe une fois résolu : il est refusé en `400` avec `{"error":"invalid_request_target"}`, sans être relayé. Les révisions 2026-07-28 (sans session) et 2025-11-25 (session, flux `GET`, clôture par `DELETE`) de MCP fonctionnent sans réglage.
- Tous les en-têtes, dans leur ordre, sauf ceux qui sont propres à une connexion (RFC 9110, section 7.6.1). Le proxy n'en ajoute aucun (ni `Server`, ni `Date`, ni `Forwarded`), à une exception près : le `traceparent` qu'il crée pour un appel d'outil arrivé sans contexte de trace (voir [Identifiant de run et contexte de trace](#identifiant-de-run-et-contexte-de-trace)).
- Le corps de la requête, octet pour octet : le proxy ne le modifie jamais.
- Le corps de la réponse, octet pour octet et au fil de l'eau : rien n'est décompressé ni retenu, un flux SSE arrive événement par événement.
- La fermeture du flux par le client : le proxy ferme aussitôt sa connexion au serveur MCP, ce qui annule l'appel.

Si le serveur MCP est injoignable, le proxy répond `502` avec un corps fixe. Si sa réponse s'interrompt, la connexion au client est fermée sans être terminée : une réponse tronquée ne passe pas pour complète.

## Hôtes et origines attendus

Le proxy écoute par défaut sur la boucle locale. Une page web ouverte sur le même poste peut pourtant l'atteindre : par réassociation DNS, un nom d'hôte de l'attaquant finit par désigner `127.0.0.1`. Le navigateur envoie alors ce nom dans l'en-tête `Host`, et l'adresse de la page dans `Origin`.

Une autre page peut aussi le viser par une balise `<img>` ou `<script>`, qui n'envoie pas `Origin` : elle ne lit pas la réponse, mais en apprend le statut. Le navigateur dit d'où vient une requête (`Sec-Fetch-Site`) et ce qu'elle charge (`Sec-Fetch-Mode`, `Sec-Fetch-Dest`).

Le proxy refuse donc, avant tout relais, une requête dont l'un de ces en-têtes n'est pas attendu. Rien n'atteint le serveur MCP et aucun événement n'est émis.

| En-tête | Valeurs acceptées |
| --- | --- |
| `Host` | `127.0.0.1`, `localhost`, `[::1]`, l'hôte du serveur MCP (entre crochets pour une adresse IPv6) et les noms ajoutés par `--allow-host`. Le port n'est pas comparé. |
| `Origin` | Absent, ou une origine dont l'hôte est accepté, ou une origine ajoutée par `--allow-origin` |
| `Sec-Fetch-Site` | Absent, `same-origin` ou `none`. Toute autre valeur (`cross-site`, `same-site`) désigne une autre page : sans origine acceptée, elle n'est admise que pour la navigation d'un lien suivi (`Sec-Fetch-Mode: navigate`, `Sec-Fetch-Dest: document`). |

Un client qui n'est pas un navigateur n'envoie ni `Origin` ni ces en-têtes : rien ne change pour lui.

Une requête refusée reçoit `403` avec `{"error":"host_not_allowed"}`, `{"error":"origin_not_allowed"}` ou `{"error":"cross_site_not_allowed"}`. Le journal cite le nom de l'en-tête, jamais sa valeur.

Un proxy joint sous un autre nom, par exemple derrière un service Kubernetes, déclare ce nom :

```bash
uv run answerlineage-proxy --upstream http://mcp.interne:9000 --host 0.0.0.0 --allow-host proxy.interne
```

`--allow-host '*'` accepte tout hôte, toute origine et toute requête inter-sites : à réserver à un proxy placé derrière un autre contrôle. Les variables d'environnement prennent une liste séparée par des virgules.

## Ce qui est lu

Le proxy lit le corps des requêtes `POST` pour reconnaître un appel d'outil. Il décide sur le corps, jamais sur les seuls en-têtes :

- un en-tête `Mcp-Method`, `Mcp-Name` ou `MCP-Protocol-Version` contredit par le corps est signalé dans l'événement (`headerMismatch`) et dans le journal, par son nom seulement ;
- l'événement décrit l'outil du corps ;
- la requête est relayée telle quelle : c'est le serveur MCP qui la refuse, comme la révision 2026-07-28 l'y oblige.

De la réponse à un appel d'outil, le proxy ne lit qu'une copie bornée, une fois la réponse rendue, pour y trouver une erreur JSON-RPC et son code ([Qualité de service](#lissue)).

Un nom d'outil hors de la forme recommandée par MCP (1 à 128 lettres, chiffres, `_`, `-`, `.`) n'est pas recopié : l'événement porte `invalidToolName`.

### Ce que le proxy ne peut pas inspecter

Le proxy ne décrit que ce qu'il lit sans ambiguïté. Une requête qu'il ne peut pas lire ainsi est relayée telle quelle, parce que la capture ne bloque jamais un agent, mais elle se compte (`uninspected`) : un appel d'outil peut s'y cacher, que le serveur exécutera peut-être sans qu'aucun événement n'en sorte.

| Requête | Pourquoi elle n'est pas décrite |
| --- | --- |
| Corps qui n'est pas du JSON, ou pas une requête JSON-RPC | Rien à lire |
| Clé répétée dans un objet, à n'importe quel niveau | Deux analyseurs JSON ne retiennent pas la même valeur : le proxy pourrait lire `ping` là où le serveur exécute `tools/call` |
| Lot JSON-RPC (un tableau de requêtes) | Les révisions prises en charge n'en admettent plus ; un serveur plus ancien exécuterait chaque appel du lot |
| Corps compressé (`Content-Encoding`) | Le proxy ne le déploie pas |
| Corps plus grand que la taille lue, dont le début ne nomme pas l'outil | Voir ci-dessous |

Le proxy ne devine pas : décrire un appel à partir d'une lecture douteuse ferait entrer dans le graphe un outil ou une lecture que le serveur n'a pas vus. Ce compteur dit ce que le lignage ignore : il est publié, fenêtre par fenêtre, dans les [relevés de couverture](#relevés-de-couverture).

### Corps plus grand que la taille lue

Au-delà de `--max-inspected-bytes`, le corps est relayé en flux, sans attendre sa fin : le serveur MCP en reçoit le début pendant que le client envoie la suite. Le proxy ne lit alors que ses 16 premiers Ko, où les clients MCP écrivent la méthode et le nom de l'outil avant les arguments. Un lecteur JSON strict y cherche `method` au premier niveau et `params.name` :

- il saute les autres valeurs (chaînes et leurs échappements, nombres, objets et tableaux imbriqués) : une clé `method` dans une chaîne ou dans un objet imbriqué ne compte pas ;
- il ne rend une valeur que s'il en est certain : un début qui s'achève avant la clé, ou qui s'écarte de la syntaxe JSON, n'établit rien ; une clé vue deux fois non plus ;
- son temps de lecture est linéaire, sans retour en arrière.

Si ce début établit un appel d'outil et son nom, l'événement est émis comme pour tout appel, compté dans `inspected_from_prefix` ; pour un outil SQL, la lecture est « non résolue », motif `request_too_large`, sans que la requête soit analysée. Sinon, la requête est relayée sans être décrite et comptée dans `uninspected`. C'est toujours le corps qui décide, jamais les en-têtes.

## Analyse du SQL

`--sql-tool outil=argument:dialecte[:espace-de-noms]` déclare un outil dont un argument porte une requête SQL. Les dialectes sont `bigquery`, `snowflake` et `postgres`. L'espace de noms est celui des jeux de données dans le lignage ; par défaut, le nom du dialecte.

Quand un appel à cet outil a réussi, le proxy analyse la requête après avoir rendu la réponse, hors du chemin de l'appel, et ajoute à l'événement les tables lues. Chaque table porte la liste des colonnes lues, colonnes de filtre et de jointure comprises.

L'analyse est prudente : elle ne liste des colonnes que si la liste est sûre d'être complète.

| Cas | Résultat |
| --- | --- |
| Colonnes préfixées, ou une seule table dans la requête | Table et colonnes lues |
| `SELECT *`, `t.*`, ligne entière, jointure naturelle, pivot | Table, sans liste de colonnes |
| Colonne sans préfixe quand plusieurs tables sont possibles | Tables concernées, sans liste de colonnes |
| Syntaxe inconnue, SQL dynamique, procédure, fonction de table, `UPDATE`, `DELETE`, `MERGE`, requête de plus de 100 Ko | Lecture « non résolue » (`readResolution`), l'appel passe |

Une vue est rapportée sous son nom : les tables qu'elle lit ne sont pas connues du proxy. La requête et ses littéraux n'atteignent ni l'événement ni les journaux : l'événement n'en garde que la forme sans littéraux.

### Le SQL conservé sans ses littéraux

L'événement porte la requête sous une forme qui permet d'en reconnaître la structure et de relire ce que l'agent a demandé, sans aucune donnée métier : la facette de job standard d'OpenLineage `sql` ([`SQLJobFacet` 1-1-0](../spec/openlineage/facets/1-1-0/SQLJobFacet.json)), champs `query` (la forme) et `dialect` (celui de l'outil).

```text
reçu   : SELECT c.nom FROM crm.clients c WHERE c.nom = 'Jeanne Dupont' AND c.ville IN ('Lyon', 'Lille') -- demande de Paul
gardé  : SELECT c.nom FROM crm.clients AS c WHERE c.nom = ? AND c.ville IN (?)
```

La forme se construit sur l'arbre syntaxique de la requête, lu par sqlglot dans le dialecte de l'outil, jamais par des expressions régulières sur le texte :

- toute constante devient le marqueur `?`, quelle que soit son écriture : chaîne (entre apostrophes ou guillemets selon le dialecte, triple, brute, échappée, Unicode, nationale, délimitée par dollars de PostgreSQL), nombre, booléen, hexadécimal, binaire, octets, date ou heure typée (`DATE '…'`), intervalle entier (`INTERVAL '3 days'`), paramètre numéroté (`$1`), motif `LIKE`, `ILIKE`, `SIMILAR TO` ou d'expression régulière, chaîne et chemin JSON (`j ->> 'cle'`, `v:cle`) ;
- une liste `IN` de constantes devient `IN (?)` : la forme ne dépend pas de sa longueur ;
- les commentaires disparaissent ;
- les noms restent : tables, colonnes, fonctions, alias, paramètres nommés (`@p`), quand ils ont la forme d'un identifiant (lettres, chiffres, `_`, `$`, `*`, `-`). Un nom entre guillemets est un texte libre : s'il porte un espace, une ponctuation, une balise ou un caractère sans rendu, la requête n'a pas de forme.

La forme se calcule avec l'analyse des lectures, dans le même processus, à partir du même découpage et dans le même budget : elle ne retarde aucun appel, et une analyse abandonnée ou en panne n'en donne pas. Elle franchit la même frontière que les lectures : le proxy ne la croit que bornée (32 768 caractères), imprimable et sans marque de commentaire. Elle est gardée en cache avec le reste du résultat.

Au moindre doute, aucune forme n'est gardée, et l'événement n'a pas de facette `sql` : mieux vaut pas de forme qu'une fuite.

| Cas | Pourquoi |
| --- | --- |
| Requête qui ne se lit pas en entier (syntaxe inconnue, repli de l'analyseur sur du texte brut : `CALL`, `EXECUTE IMMEDIATE`) | Un fragment non lu pourrait porter une constante |
| Requête de plus de 100 Ko, ou forme de plus de 32 768 caractères | Taille maximale |
| Analyse abandonnée au budget, file pleine, processus en panne | L'analyse n'a pas abouti |
| Imbrication trop profonde | L'arbre ne se réécrit pas |
| Forme qui contient encore une chaîne ou un nombre, ou un mot égal au texte d'une constante de la requête qui n'y figurait pas aussi comme nom | Une constante que l'analyseur aurait changée en nom (`DATE_TRUNC('dupont', d)` lu comme une unité) serait sinon recopiée |

Seule exception au contrôle : une unité de temps (`DAY`, `MONTH`, `YEAR`…, un vocabulaire clos) que le dialecte écrit entre apostrophes, comme `DATE_TRUNC('MONTH', d)` en PostgreSQL. Elle ne dit rien des données.

Une instruction dont les lectures ne sont pas connues (`UPDATE`, `INSERT … VALUES`) garde sa forme : elle se lit en entier, la lecture reste « non résolue ».

La limite qui reste : un nom d'un seul mot choisi par l'auteur de la requête (alias, nom de colonne, de table ou de paramètre) est conservé tel quel. `SELECT nom AS "Dupont"` garde `"Dupont"` ; `SELECT nom AS "Jeanne Dupont"`, dont le nom n'a pas la forme d'un identifiant, n'a pas de forme.

### Un processus à part, un budget borné

Une requête pathologique de quelques dizaines de Ko peut occuper sqlglot plusieurs secondes. Un fil Python ne s'interrompt pas, et il disputerait l'interpréteur à la boucle qui relaie les appels : l'analyse tourne donc dans un processus à part, un interpréteur démarré à neuf (`python -I -m answerlineage_proxy.analyzer`) au premier appel à analyser. Le proxy lui confie les requêtes une à une, sur son entrée standard, et lit ses réponses sans jamais bloquer la boucle.

- **Budget.** Chaque requête dispose de `--sql-budget-ms` (50 ms par défaut), comptées à partir du moment où le processus annonce qu'il commence le calcul : ni son démarrage ni l'attente dans la file n'en consomment. Passé le budget, le processus est tué, le calcul s'arrête donc réellement, et un nouveau processus est démarré à la requête suivante.
- **File.** Les requêtes qui attendent leur tour sont au plus `--sql-queue-size`. File pleine, la lecture est « non résolue » sur-le-champ : ni mémoire sans borne, ni attente sans fin.
- **Pannes.** Toute attente a un délai, et une réponse du processus n'est crue que si elle a exactement la forme attendue (noms de tables et de colonnes compris). Un processus qui meurt, refuse de démarrer ou répond n'importe quoi donne une lecture « non résolue » : l'événement de l'appel est toujours émis.
- **Arrêt.** `drain()` attend les analyses en cours ; `aclose()`, appelé aussi à l'arrêt du serveur HTTP, arrête ensuite le processus. Si le proxy disparaît sans l'arrêter, le processus s'arrête sur la fin de son entrée, ou, en plein calcul, par une alarme du système une seconde après le budget.
- **Silence.** La sortie d'erreur du processus est fermée : un message d'erreur pourrait citer la requête.

### Cache

Deux requêtes identiques, avec les mêmes réglages d'analyse (dialecte, catalogue et base par défaut), ne s'analysent qu'une fois, y compris quand elles arrivent ensemble. Le cache garde au plus `--sql-cache-size` résultats et 4 millions de caractères de noms ; les moins récemment servis sortent les premiers. Sa clé est une empreinte BLAKE2b, à clé tirée au démarrage du proxy, du texte et des réglages : le texte de la requête n'est pas conservé, même en mémoire, au-delà de l'appel. Seul le résultat de l'analyse, des noms de tables et de colonnes et la forme sans littéraux, est gardé.

Une analyse abandonnée au budget, ou perdue par un processus mort en plein calcul, est mise en cache comme les autres. L'analyse est déterministe : la relancer coûterait de nouveau le budget, un processus tué et un redémarrage, et répéter une requête pathologique deviendrait un moyen d'occuper l'analyse aux dépens des autres appels. Une requête refusée faute de place, ou qu'un processus n'a pas pu commencer, n'est pas mise en cache : l'échec ne tient pas à elle.

### Motifs d'une lecture non résolue

| Motif (`unresolvedReason`) | Cause |
| --- | --- |
| `not_understood` | Syntaxe inconnue, ou forme que l'analyse ne couvre pas |
| `unsupported_statement` | `UPDATE`, `DELETE`, `MERGE`, procédure, SQL dynamique |
| `table_function` | Fonction de table, qui peut lire n'importe quoi |
| `unsupported_identifier` | Nom de table ou de colonne hors de la forme admise |
| `too_deep` | Imbrication trop profonde pour l'analyseur |
| `too_large` | Requête de plus de 100 Ko |
| `empty` | Requête vide |
| `missing_argument` | L'argument déclaré manque ou n'est pas une chaîne |
| `request_too_large` | Corps de la requête plus grand que la taille lue : la requête n'est pas lue |
| `budget_exceeded` | Analyse abandonnée au budget |
| `queue_full` | File des analyses pleine |
| `analyzer_unavailable` | Processus d'analyse impossible à démarrer, ou analyse interrompue par l'arrêt du proxy |
| `analyzer_failed` | Processus mort en plein calcul, ou réponse illisible |

### Compteurs

`ProxyStats` compte, sans aucune valeur issue des requêtes :

| Compteur | Ce qu'il compte |
| --- | --- |
| `inspected_from_prefix` | Appels décrits d'après le seul début d'un corps trop grand |
| `sql_from_cache` | Analyses servies par le cache, ou par une analyse identique en cours |
| `sql_over_budget` | Analyses abandonnées au budget, processus tué |
| `sql_refused` | Analyses refusées, file pleine |
| `sql_analyzer_failures` | Analyses perdues par un processus d'analyse en panne |

## Règles de lecture des outils non SQL

Un outil d'API, de fichiers ou de recherche documentaire ne dit pas ce qu'il lit, et aucun SQL ne le trahit. Une règle de lecture le déclare : quel outil, éventuellement sur quel serveur ; quels jeux de données il lit, et éventuellement quelles colonnes ; quel argument porte la clé lue. Les règles sont dans un fichier YAML, donné par `--read-rules-file` :

```yaml
version: 1
rules:
  - tool: get_price
    server: catalogue
    datasets:
      - namespace: postgres://referentiel:5432
        name: ref.tarifs
        columns: [produit, prix]
    key: produit
  - tool: search_docs
    datasets:
      - { namespace: "gs://documents", name: "politique%20voyages.pdf" }
    key: [filtre, 0, id]
```

| Champ | Contenu |
| --- | --- |
| `version` | `1`, la version du format |
| `rules[].tool` | Le nom de l'outil, sous la forme que MCP recommande (1 à 128 lettres, chiffres, `_`, `-`, `.`) |
| `rules[].server` | Facultatif : le nom du serveur (`--server-name`) pour lequel la règle vaut. Une règle sans serveur vaut pour tous ; celle qui nomme le serveur l'emporte. Un même fichier peut ainsi servir plusieurs proxys |
| `rules[].datasets` | 1 à 32 jeux de données lus, chacun par son espace de noms (`namespace`) et son nom (`name`), qui ont la forme qu'exige l'[ingestion](ingestion.md#identifiants) |
| `rules[].datasets[].columns` | Facultatif : 1 à 1 000 colonnes lues, sans doublon. Sans elles, la lecture porte sur tout le jeu de données |
| `rules[].key` | Facultatif : l'argument qui porte la clé lue, par son nom, ou un chemin de 1 à 8 étapes dans les arguments, noms d'objets et rangs de listes (0 à 9 999) |

### Le fichier, une entrée hostile

Le fichier est lu une fois, au démarrage, et une règle invalide empêche le proxy de démarrer, avec « Configuration invalide : Règles de lecture invalides : … » et le code de sortie 2 : on ne capte pas à moitié en silence. Il ne se relit pas à chaud.

- **Bornes.** 256 Kio au plus, vérifiés avant toute lecture ; 1 000 règles, 8 niveaux d'imbrication. Le fichier est en UTF-8.
- **Chargeur sûr.** PyYAML lit le fichier avec son chargeur sûr, qui refuse en plus ancres (`&`), alias (`*`) et étiquettes explicites (`!!str`, `!python/…`, `!`) avant de rien composer : pas d'inclusion, pas de référence, pas de bombe d'alias. Le document n'est jamais construit en objets Python : il est parcouru nœud par nœud.
- **Types.** Une valeur qui n'a pas le type attendu est refusée, pas convertie : une date, un nombre ou un booléen là où un nom est attendu (`name: 2026-11-02`, `tool: yes`), un texte là où la version est attendue.
- **Champs.** Un champ inconnu, répété ou absent est refusé : une faute de frappe (`colums`) ne fait pas perdre des colonnes en silence. Deux règles pour le même outil sur le même serveur aussi.
- **Noms.** Un nom de jeu de données, d'espace de noms ou de colonne suit la forme des [identifiants de l'ingestion](ingestion.md#identifiants), écrite une seconde fois dans le proxy pour qu'il ne dépende pas de l'API (`tests/test_contract_identifiers.py` les tient d'accord) ; il est gardé sous sa forme normalisée NFC.
- **Erreurs.** Le message nomme la règle, le champ et leur position, jamais une valeur du fichier ; d'une erreur de syntaxe YAML, dont le message citerait le texte fautif, seuls la position et le genre sont gardés :

```text
Configuration invalide : Règles de lecture invalides : règle 2, champ datasets[0].name (ligne 9, colonne 15) : un nom ne contient ni espace ni blanc (identifier_whitespace).
```

### Ce que dit l'événement

Un appel réussi d'un outil décrit porte en entrée les jeux de données de sa règle, leurs colonnes dans la facette standard `schema`, et sa lecture est résolue. Un appel en échec n'a rien fait lire. Un outil déclaré par `--sql-tool` garde l'analyse de sa requête : sa règle est ignorée, et le proxy le dit au démarrage.

**La clé lue** est une valeur d'argument : elle n'est jamais conservée telle quelle. Elle devient une empreinte à clé, la même fonction que les autres [empreintes](#empreintes-à-clé) du proxy, sur l'étiquette `answerlineage/fingerprint/key/v1` et la forme canonique JSON de la valeur (`"P-1042"` et `1042` n'ont donc pas la même empreinte). Une liste de valeurs simples donne une empreinte par valeur distincte, 100 au plus. Les empreintes sont posées sur chaque jeu de données lu, dans la facette `answerlineage_keys` ([`KeysInputDatasetFacet`](../spec/facets/1-0-0/KeysInputDatasetFacet.json)), champ `hmacs`. Qui détient la clé du locataire retrouve ainsi les appels qui ont lu la ligne d'une donnée reconnue fausse, sans que la valeur entre dans AnswerLineage.

Quand la clé ne peut pas être gardée, la lecture est déclarée pour tout le jeu de données, et `readKeys`, dans la facette `answerlineage_toolCall` ([version 1-1-0](../spec/facets/1-1-0/ToolCallRunFacet.json)), dit pourquoi :

| `readKeys` | Cas |
| --- | --- |
| `fingerprinted` | Les empreintes des clés lues sont sur chaque jeu de données |
| `no_fingerprint_key` | Pas de clé de locataire : aucune empreinte n'est calculée |
| `request_too_large` | Corps plus grand que la taille lue : les arguments ne sont pas lus |
| `missing_argument` | L'argument manque, est nul ou une liste vide |
| `unsupported_argument` | La valeur n'est ni un scalaire ni une liste de scalaires, ou n'a pas de forme canonique |
| `too_many_keys` | Plus de 100 clés : en garder une partie ferait écarter à tort un appel qui a lu une clé oubliée |

Sans `key` dans la règle, `readKeys` est absent. La facette ne se réclame de la version 1-1-0 que lorsqu'elle porte `readKeys`.

### Les outils à décrire

Un appel d'un outil qui n'a ni règle de lecture ni SQL déclaré est marqué : sa lecture est « non résolue », motif `undescribed_tool`, et il n'a pas d'entrée. Le proxy le compte (`ProxyStats.undescribed_calls`) et le journalise une fois par nom d'outil, un nom validé, jamais un argument ; au-delà de 1 000 noms, il ne journalise plus. Un nom d'outil refusé n'est pas marqué : il porte déjà `invalidToolName`.

La liste des outils à décrire se lit par l'API, à partir des événements reçus : [`GET /api/v1/ingestion/undescribed-tools`](ingestion.md#outils-à-décrire) rend, par émetteur, chaque outil marqué, le nombre de ses appels et le dernier reçu. Ce choix ne touche pas au schéma du graphe, garde le proxy sans état, et rassemble en un lieu ce que voient tous les proxys, qu'une liste en mémoire de chacun perdrait à chaque redémarrage. Tant que les événements ne partent pas encore du proxy vers l'API, la liste est vide pour lui : son journal est alors la seule trace.

## Relevés de couverture

Le proxy publie ce qu'il a vu, pour que l'on sache quelle part du trafic le lignage a réellement captée. Il tient ses compteurs par fenêtre de temps, une minute par défaut (`--coverage-window-seconds`), et remet chaque fenêtre close à sa destination d'événements, au format du [relevé de couverture](ingestion.md#relevés-de-couverture) de l'API :

```json
{
  "emitter": "mcp://entrepot",
  "window": { "from": "2026-11-11T09:00:00.000000Z", "to": "2026-11-11T09:01:00.000000Z" },
  "calls_seen": 42,
  "events_emitted": 42,
  "events_lost": 0,
  "uninspected": 1
}
```

| Champ | Ce que le proxy compte |
| --- | --- |
| `emitter` | Son identifiant d'émetteur : `mcp://<serveur>`, l'espace de noms des jobs de ses événements |
| `calls_seen` | Appels d'outils décrits, y compris d'après le seul début d'un corps trop grand |
| `events_emitted` | Événements que la destination a pris en charge |
| `events_lost` | Événements que la destination a refusés, et, à l'arrêt, ceux d'appels dont l'analyse n'avait pas abouti |
| `uninspected` | Requêtes relayées sans pouvoir être inspectées ([ci-dessus](#ce-que-le-proxy-ne-peut-pas-inspecter)) |

- **Un appel compte dans la fenêtre où tombe l'heure de son événement.** L'événement d'un outil SQL part une fois la requête analysée, parfois après la fin de sa fenêtre : la fenêtre ne se clôt qu'une fois tous ses appels réglés, et les relevés partent dans l'ordre du temps.
- **Une fenêtre sans appel est publiée aussi** : c'est le battement qui prouve que le proxy n'est pas muet. Un proxy arrêté, bloqué ou coupé de l'API ne publie plus, et le [contrôle des émetteurs muets](workers.md#émetteurs-muets) le signale.
- **Les fenêtres sont alignées** sur des multiples de leur durée depuis l'origine des temps Unix. La première commence au démarrage du proxy ; à l'arrêt, la fenêtre en cours s'achève à l'heure de l'arrêt. Deux vies successives du proxy publient donc des fenêtres disjointes, et l'intervalle où il était arrêté se lit comme un silence.
- **Rien ne retarde un appel.** Compter est une addition faite là où l'appel est déjà décrit ; la clôture des fenêtres tourne à part, réveillée à la fin de chaque fenêtre, et un échec de remise est journalisé par sa classe, sans atteindre un appel.

Les relevés ne portent que des nombres, des heures et l'identifiant de l'émetteur : rien d'une requête.

**L'envoi réseau des relevés vers l'API d'ingestion ne fait pas encore partie du proxy.** Il viendra avec sa boîte d'envoi, comme celui des événements : d'ici là, la destination par défaut les compte sans les envoyer, et une destination fournie au proxy (`EventSink.report_coverage`) les reçoit.

## Identifiant de run et contexte de trace

L'identifiant de run d'un appel d'outil vient de son contexte de trace W3C ([Trace Context](https://www.w3.org/TR/trace-context/), en-tête `traceparent`). Lignage et traces se corrèlent sans nouvel en-tête, et tout émetteur qui voit le même contexte (deux proxys en série, un proxy et un adaptateur d'agent) émet pour le même run : les écritures du graphe, idempotentes, fusionnent leurs événements.

### D'où vient le contexte

Dans cet ordre, pour une requête `tools/call` :

1. `params._meta.traceparent` du corps. Il désigne l'appel d'outil lui-même et survit aux reprises HTTP : c'est lui qui l'emporte.
2. Sinon, l'en-tête HTTP `traceparent`, s'il est unique et bien formé. Il diffère légitimement de celui de `_meta` (un client HTTP instrumenté ouvre son propre span) : ce n'est pas un écart signalé.
3. Sinon, le proxy crée un contexte : `trace-id` et `parent-id` tirés d'une source cryptographique (`secrets`), jamais nuls, version `00`, drapeaux `00` (« non échantillonné » : le proxy ne décide pas de l'échantillonnage des traces d'autrui).

Une valeur est bien formée quand elle suit le format W3C : version (2 hexadécimaux, pas `ff`), `trace-id` (32), `parent-id` (16), drapeaux (2), séparés par des tirets, en hexadécimal minuscule, sans identifiant nul. Une version `00` s'arrête après les drapeaux ; une version future peut être suivie d'un tiret et de champs qui ne sont pas lus. Au-delà de 512 caractères, la valeur n'est pas lue. Une valeur mal formée est une entrée hostile : elle est ignorée comme si elle manquait, sans être recopiée ni journalisée.

### Ce que le proxy ajoute à la requête relayée

| Appel reçu | Identifiant de run dérivé de | Requête relayée |
| --- | --- | --- |
| `_meta.traceparent` bien formé | `_meta` | inchangée, en-tête `traceparent` compris |
| en-tête `traceparent` unique et bien formé | l'en-tête | inchangée |
| aucun contexte | contexte créé | un en-tête `traceparent` ajouté, à la fin, qui porte le contexte créé |
| en-tête `traceparent` mal formé ou en double, sans `_meta` valable | contexte créé | inchangée : l'en-tête reçu est relayé tel quel, rien n'est ajouté |
| appel d'outil au corps plus grand que la taille lue | l'en-tête `traceparent` s'il est unique et bien formé, sinon un contexte créé | inchangée : `_meta` n'a pas été lu, rien n'est ajouté |
| requête qui n'est pas un appel d'outil, corps illisible, ou corps trop grand dont le début n'établit pas un appel d'outil | aucun (pas d'événement) | inchangée |

Un en-tête `traceparent` déjà présent n'est donc jamais remplacé ni doublé. Quand il est mal formé, le contexte créé ne sert qu'à l'identifiant de run : le transmettre ferait deux en-têtes `traceparent`, que le serveur ne saurait départager. Le corps n'est jamais modifié.

Rien ne retarde l'appel : `_meta` est lu par l'inspection déjà faite du corps, et la création du contexte ne fait ni entrée-sortie ni attente.

### La dérivation, à l'octet près

L'identifiant de run est un UUID version 8 (RFC 9562), fonction pure du `trace-id` et du `parent-id` :

1. `entrée` = les 23 octets ASCII de `answerlineage/run-id/v1`, suivis des 16 octets du `trace-id` puis des 8 octets du `parent-id`, décodés de l'hexadécimal (47 octets en tout).
2. `empreinte` = les 16 premiers octets de SHA-256(`entrée`).
3. L'octet 6 (en partant de 0) devient `(octet & 0x0f) | 0x80` : version 8.
4. L'octet 8 devient `(octet & 0x3f) | 0x80` : variante RFC 9562.
5. L'identifiant est l'écriture canonique de ces 16 octets : `8-4-4-4-12` hexadécimaux minuscules.

La version et les drapeaux du `traceparent` n'entrent pas dans le calcul : le même appel, échantillonné ou non, a le même run. En Python, sans rien d'AnswerLineage :

```python
import hashlib
import uuid

octets = bytearray(
    hashlib.sha256(
        b"answerlineage/run-id/v1" + bytes.fromhex(trace_id) + bytes.fromhex(parent_id)
    ).digest()[:16]
)
octets[6] = (octets[6] & 0x0F) | 0x80
octets[8] = (octets[8] & 0x3F) | 0x80
run_id = uuid.UUID(bytes=bytes(octets))
```

Exemple : `00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01` donne `a7e19836-59d9-8033-bd72-8a4bccb7271f`. Les vecteurs de test, valeurs acceptées avec leur identifiant et valeurs refusées, sont publiés dans [`spec/run-id/vectors-v1.json`](../spec/run-id/vectors-v1.json) pour vérifier tout autre émetteur. Le [kit de conformité](conformite.md) refait ce calcul pour juger un point de capture, et cherche par des canaris ce qu'un émetteur laisserait sortir : le proxy le passe sous le profil `capture`.

Un UUID version 5 aurait aussi convenu. La version 8 est retenue parce que la version 5 impose SHA-1, que l'on n'emploie plus dans une construction nouvelle, et un UUID d'espace de noms à publier en plus ; la RFC 9562 recommande la version 8 pour un nom haché par un autre algorithme, et en donne un exemple avec SHA-256. Tout langage a SHA-256 ; le calcul tient en cinq étapes. L'API d'ingestion accepte tout UUID de forme canonique. Une autre dérivation, demain, changerait d'étiquette (`…/v2`) et de fichier de vecteurs.

## Empreintes à clé

Des arguments et du résultat d'un appel, l'événement ne garde que des empreintes : deux appels aux mêmes arguments, ou deux résultats identiques, se reconnaissent sans que leur contenu entre dans AnswerLineage.

### La clé

Une empreinte est un HMAC-SHA-256 calculé avec une clé secrète propre au locataire. Deux locataires ont deux clés : la même valeur donne chez eux deux empreintes différentes, personne ne peut rapprocher leurs événements, et qui ne détient pas la clé ne retrouve pas une valeur peu variée (un code client, un montant) en essayant des candidates.

- **Fourniture.** La clé s'écrit en hexadécimal, 32 à 64 octets (64 à 128 caractères). Elle se donne dans un fichier (`--fingerprint-key-file`, ou `ANSWERLINEAGE_PROXY_FINGERPRINT_KEY_FILE`), qui ne contient qu'elle, ou dans la variable `ANSWERLINEAGE_PROXY_FINGERPRINT_KEY` ; pas les deux. Jamais sur la ligne de commande, que la liste des processus montre. Pour en tirer une : `python -c "import secrets; print(secrets.token_hex(32))"`.
- **Refus.** Une clé trop courte, trop longue ou qui n'est pas de l'hexadécimal, un fichier illisible : le proxy s'arrête avec « Configuration invalide : … » et le code 2. Le message ne cite ni la clé ni le chemin du fichier.
- **Discrétion.** La clé n'apparaît dans aucun journal, aucun événement, aucune représentation des réglages ni aucun message d'erreur.
- **Sans clé**, aucune empreinte n'est émise : jamais de repli sur un hachage sans clé, qu'une énumération suffirait à inverser. Le proxy le dit une fois, au démarrage.
- **Rotation.** Changer de clé rompt le rapprochement : les empreintes calculées avec la nouvelle clé ne se comparent plus à celles des événements déjà reçus. Une rotation se fait donc à une date connue, et les comparaisons s'arrêtent à cette date. La même clé, partagée par plusieurs proxys du même locataire, leur donne les mêmes empreintes.

### Ce qui est empreint

| Champ de `answerlineage_toolCall` | Octets empreints | Absent quand |
| --- | --- | --- |
| `argumentsFingerprint` | Forme canonique JSON ([RFC 8785](https://www.rfc-editor.org/rfc/rfc8785)) de `params.arguments`, en UTF-8 ; `{}` pour un appel sans arguments | sans clé ; corps plus grand que la taille lue ; arguments sans forme canonique (nombre non fini, texte qui n'est pas de l'Unicode valide) |
| `resultFingerprint` | Corps de la réponse, octet pour octet, tel que relayé : compressé s'il l'est, flux SSE compris | sans clé ; réponse interrompue ; client parti avant la fin |
| `resultSize` | Taille de ces mêmes octets ; ce n'est pas une empreinte, elle est émise même sans clé | réponse interrompue ; client parti avant la fin |

La forme canonique ne dépend ni de l'ordre des clés ni des blancs du JSON reçu : clés triées sur leurs unités de code UTF-16, aucun blanc, chaînes avec les seuls échappements obligatoires, nombres écrits comme en ECMAScript (`2.50` et `2.5` donnent `2.5`, `1.0` donne `1`, `1E-7` donne `1e-7`).

### Le calcul, à l'octet près

1. `message` = l'étiquette de l'usage en ASCII, un octet nul, puis les octets empreints. Les étiquettes : `answerlineage/fingerprint/arguments/v1` et `answerlineage/fingerprint/result/v1`. Des arguments et un résultat identiques ont donc des empreintes différentes.
2. `empreinte` = `hmac-sha256:` suivi de HMAC-SHA-256(`clé`, `message`) en hexadécimal minuscule.

En Python, sans rien d'AnswerLineage :

```python
import hashlib
import hmac

empreinte = (
    "hmac-sha256:"
    + hmac.new(
        cle, b"answerlineage/fingerprint/result/v1" + b"\x00" + corps, hashlib.sha256
    ).hexdigest()
)
```

Les vecteurs de test, avec deux clés de locataire, sont publiés dans [`spec/fingerprint/vectors-v1.json`](../spec/fingerprint/vectors-v1.json) ; ils ont été établis par une implémentation indépendante, en JavaScript, et les tests du proxy les relisent. Un autre émetteur qui retrouve ces valeurs calcule les mêmes empreintes avec la même clé. Un autre calcul, demain, changerait d'étiquette (`…/v2`) et de fichier de vecteurs.

### Ce que coûte le calcul

L'empreinte du résultat se calcule au fil du relais, morceau par morceau, après l'envoi de chaque morceau au client : la réponse n'est ni retenue ni attendue. Mesuré sur la boucle locale, HMAC-SHA-256 passe environ 1,5 Go/s, soit une quarantaine de microsecondes par morceau de 64 Ko : sur une réponse de 8 Mo relayée en 40 ms environ, quelques millisecondes de plus ; sur un appel ordinaire, rien de mesurable. Sans clé, seule la taille est comptée.

L'empreinte des arguments se calcule après l'échange, quand l'événement est construit : quelques microsecondes pour des arguments ordinaires, de l'ordre de 7 ms pour 1 Mo, la taille lue par défaut, pendant lesquelles la boucle ne relaie rien d'autre.

## Qualité de service

Chaque appel porte, dans la facette `answerlineage_qos` ([`QosRunFacet` 1-1-0](../spec/facets/1-1-0/QosRunFacet.json)), ce que le proxy voit de l'échange : sa latence, la latence que le proxy y ajoute, son issue et ses reprises. Le proxy ne voit ni modèle, ni jetons, ni coût : ces champs de la facette restent absents.

```json
"answerlineage_qos": {
  "_producer": "https://github.com/alebgl77/AnswerLineage/tree/0.0.0/proxy",
  "_schemaURL": "https://raw.githubusercontent.com/alebgl77/AnswerLineage/main/spec/facets/1-1-0/QosRunFacet.json#/$defs/QosRunFacet",
  "latencyMs": 3.217,
  "addedLatencyMs": 0.243,
  "outcome": "jsonrpc_error",
  "errorCode": -32602,
  "errorCount": 1,
  "retryCount": 1
}
```

| Champ | Ce que le proxy mesure |
| --- | --- |
| `latencyMs` | De l'arrivée de la requête à la réponse rendue au client, ou à l'échec de l'échange : la latence de l'appel vue du proxy |
| `addedLatencyMs` | Ce que le proxy dépense lui-même avant de relayer la requête et avant de rendre chaque partie de la réponse : les attentes du client et du serveur MCP n'y comptent pas |
| `outcome` | L'issue de l'échange, d'une liste fermée (ci-dessous) |
| `errorCode` | Le code numérique de l'erreur : celui de l'erreur JSON-RPC, ou le statut HTTP. Absent quand l'issue n'a pas de code |
| `errorCount` | 1 pour une erreur, 0 sinon ; un abandon par le client n'est pas une erreur |
| `retryCount` | Le nombre d'appels qui précèdent celui-ci dans sa chaîne de reprises. Absent quand une reprise ne peut pas être reconnue, jamais zéro par défaut |

Les durées sont en millisecondes, à la microseconde, prises sur une horloge monotone : un réglage de l'horloge du système ne les fausse pas.

### L'issue

Dans cet ordre, la première qui s'applique :

| `outcome` | Cas | `errorCode` |
| --- | --- | --- |
| `client_abort` | Le client est parti avant la fin de la réponse | absent |
| `timeout` | Un délai du client HTTP du proxy est dépassé : connexion au serveur MCP (10 s), ou, quand le client HTTP en a un, lecture de la réponse | absent |
| `transport_error` | Serveur MCP injoignable, réponse interrompue | absent |
| `http_error` | Statut HTTP 400 ou plus, rendu par le serveur MCP ou par le proxy lui-même (chemin refusé) | le statut |
| `jsonrpc_error` | Statut de succès, mais la réponse JSON-RPC porte une erreur (`error`) | le code JSON-RPC, s'il est un entier sur 32 bits |
| `success` | Tout le reste | absent |

Une erreur se dit par sa catégorie et son code, jamais par son message : un message d'erreur peut citer les arguments de l'appel. Un code reçu qui n'est pas un entier signé sur 32 bits n'est pas recopié.

Pour y lire une erreur JSON-RPC, le proxy garde de la réponse une copie bornée à 16 Ko, au fil du relais, après l'envoi de chaque morceau au client : le début d'un corps `application/json`, la fin d'un flux `text/event-stream`, où le serveur écrit la réponse avant de le fermer. Il la lit une fois la réponse rendue, puis l'oublie :

- un corps JSON qui tient dans la copie est lu en entier, strictement : une clé répétée, un document illisible n'établissent aucune erreur ;
- un corps plus grand est lu par son début, avec le lecteur strict des corps trop grands ([ci-dessus](#corps-plus-grand-que-la-taille-lue)) : `error` au premier niveau, puis `error.code` ;
- dans un flux SSE, c'est la dernière réponse JSON-RPC complète qui compte ; une requête ou une notification du serveur n'en est pas une, et le premier événement d'une fin tronquée n'est pas lu.

Un corps compressé, ou d'un autre type, n'est pas lu : son issue tient au seul statut HTTP. L'issue ne change pas le type de l'événement : une erreur JSON-RPC rendue avec un statut de succès reste un événement `COMPLETE`, et l'état du run dans le graphe ne change pas.

### Les reprises

MCP ne marque pas une reprise. Le proxy la reconnaît ainsi : un appel reprend le précédent quand

- il nomme le même outil, avec la même empreinte des arguments ;
- il vient de la même portée : la même session MCP (en-tête `Mcp-Session-Id`, s'il est unique), sinon la même trace transmise par le client (`trace-id` de `_meta` ou de l'en-tête `traceparent`), sinon le même proxy ;
- le précédent n'a pas réussi (toute issue autre que `success`, abandon compris : un client qui renonce puis recommence reprend son appel) ;
- il commence après la fin du précédent, et au plus 60 secondes après.

`retryCount` compte les appels qui le précèdent dans cette chaîne : 0 pour un premier appel, 1 pour la première reprise, 2 pour la deuxième. Un succès clôt la chaîne : le même appel ensuite n'est pas une reprise. Deux appels identiques menés en même temps ne se reprennent pas l'un l'autre.

Sans clé d'empreinte, ou sans empreinte des arguments (corps plus grand que la taille lue, arguments sans forme canonique), une reprise ne peut pas être reconnue : `retryCount` est absent, et non nul. Un nom d'outil refusé fait de même.

La mémoire des chaînes est bornée : 10 000 chaînes ouvertes au plus, oubliées 60 secondes après leur dernier échec ; au-delà, la plus ancienne est oubliée, et sa reprise compte pour un premier appel. L'identifiant de session n'y est gardé que sous une empreinte BLAKE2b à clé tirée au démarrage : il n'est conservé ni dans la mémoire, ni dans l'événement, ni dans le journal. Une session étant celle que le client déclare, un client qui en usurpe une autre ne fausse que le compte de ses reprises.

### Rien ne retarde l'appel

Sur le chemin de l'appel, le proxy ne prend que des horodatages et garde la copie bornée de la réponse, après l'envoi de chaque morceau. L'issue, les reprises et l'événement se calculent une fois la réponse rendue, dans l'ordre où les appels s'achèvent. Un calcul de signaux qui échoue est journalisé par sa classe ; l'événement part sans la facette.

La latence ajoutée compte le travail du proxy entre deux attentes : contrôle des en-têtes, lecture du corps, contexte de trace, préparation de la requête relayée, puis, pour chaque morceau de la réponse, son envoi préparé, l'empreinte et la copie qui le suivent. Elle ne compte pas ce que font, pendant ces attentes, le serveur HTTP qui lit la requête du client et la bibliothèque HTTP qui écrit vers le serveur MCP : c'est une borne basse de ce qu'ajoute le saut par le proxy. Le banc mesure aussi le saut entier, par comparaison.

### Le banc de latence

`make bench` lance un serveur MCP de test et le vrai proxy devant lui, chacun dans son processus, servis comme la ligne de commande sert le proxy, avec une clé d'empreinte. Il appelle l'outil 100 000 fois directement et 100 000 fois à travers le proxy, en alternance, après 50 appels de mise en route, et rend les p50, p99 et maximum de deux mesures de la latence ajoutée :

- **par comparaison** : chaque percentile des appels à travers le proxy, moins le même percentile des appels directs, vus du client ;
- **par la mesure interne** : `addedLatencyMs` de l'événement de chaque appel.

Il échoue quand le p99 de l'une ou de l'autre dépasse 5 ms, et quand la mesure interne n'est pas cohérente : un événement réussi par appel mesuré, une latence ajoutée qui ne dépasse pas la latence de l'échange, une latence vue par le proxy qui ne dépasse jamais celle vue par le client. `BENCH_ARGS` règle le nombre d'appels, les appels menés à la fois et le seuil (`make bench BENCH_ARGS='--calls 20000 --concurrency 4'`).

Le banc tourne chaque nuit dans son propre workflow (`.github/workflows/bench.yml`), sur une machine qui ne fait que lui. Dans `make check`, un test de 2 000 appels vérifie que le banc fonctionne et que la mesure interne est cohérente, sans seuil de temps : sur une machine partagée, la charge des autres tests le ferait échouer à tort.

Mesuré sur un poste à 4 cœurs, 100 000 appels un à la fois :

| Mesure | p50 | p99 | max |
| --- | --- | --- | --- |
| Appel direct, vu du client | 1,32 ms | 2,76 ms | 77,96 ms |
| À travers le proxy, vu du client | 2,75 ms | 5,31 ms | 53,56 ms |
| Latence ajoutée, par comparaison | 1,43 ms | 2,55 ms | — |
| Latence ajoutée, mesure interne | 0,24 ms | 0,49 ms | 14,13 ms |

L'écart entre les deux mesures est le saut lui-même : un serveur HTTP qui lit la requête et une bibliothèque HTTP qui la réécrit vers le serveur MCP, que la mesure interne ne voit pas. Un maximum est une valeur isolée, une pause du ramasse-miettes ou de l'ordonnanceur ; par comparaison, la différence de deux maxima n'a pas de sens, et le banc ne la juge pas.

## Ce qui n'est jamais conservé

Aucun argument, résultat, en-tête ni jeton n'est stocké ou journalisé : des arguments et du résultat, l'événement ne garde que des empreintes à clé et une taille ; d'une requête SQL, sa forme sans littéraux ; d'une erreur, sa catégorie et son code ; d'une clé lue d'après une règle, son empreinte à clé. Seuls le `trace-id` et le `parent-id` d'un contexte de trace bien formé, qui ne disent rien du contenu, sont recopiés dans l'événement ; ils ne sont jamais journalisés. Les en-têtes `Mcp-Param-*`, qui recopient des arguments d'outil, ne sont pas lus. L'en-tête `Mcp-Session-Id` n'est lu que pour reconnaître une reprise, et oublié après l'appel, sauf son empreinte à clé. Le journal d'accès du serveur HTTP est coupé. Les journaux des bibliothèques qui citeraient une URL complète, des en-têtes ou une requête SQL (`httpx`, `httpcore`, `sqlglot`) sont coupés pour tout le processus.

## Autorisation

Le proxy ne prend aucune part à l'autorisation. Le défi `401` d'un serveur MCP protégé, ses métadonnées OAuth (`/.well-known/…`) et les échanges d'un client avec le serveur d'autorisation passent tels quels : le proxy n'y répond jamais lui-même, ne s'authentifie pas à la place du client et ne garde aucun jeton d'un appel au suivant.

Un test fait traverser un jeton, un cookie, un paramètre recopié en en-tête et un argument d'outil sur tout le parcours, du proxy au graphe, puis les cherche dans les événements, la base entière, les réponses de l'API, les compteurs et tous les journaux : il n'en reste rien.

## L'événement émis

Un `RunEvent` OpenLineage par appel d'outil, une fois l'échange terminé : `COMPLETE` si le serveur a répondu sans erreur HTTP, `FAIL` sinon, `ABORT` si le client est parti. Le job est `mcp://<serveur>` / `<outil>`, l'identifiant de run celui que dérive le contexte de trace. La facette `answerlineage_toolCall` porte le serveur, l'outil, le transport, la révision du protocole, le statut HTTP, les écarts relevés, la résolution de la lecture, le contexte de trace (`traceId`, `spanId`, `traceOrigin`), les empreintes à clé des arguments et du résultat et la taille du résultat ; son schéma est publié (voir [Facettes OpenLineage](facettes.md)). La facette `answerlineage_qos` porte la latence, la latence ajoutée, l'issue et les reprises ([Qualité de service](#qualité-de-service)). Les tables lues, ou les jeux de données que déclare la règle de lecture de l'outil, sont les jeux de données d'entrée ; leurs colonnes sont dans la facette standard `schema`, les empreintes des clés lues dans la facette `answerlineage_keys`. La forme sans littéraux d'une requête SQL est dans la facette de job standard `sql`.

## Limites connues

- Les événements et les relevés de couverture sont comptés mais ne partent pas encore vers l'API d'ingestion.
- Plusieurs proxys devant un même serveur publient sous le même identifiant d'émetteur : l'API fusionne leurs relevés d'une même fenêtre au plus grand au lieu de les additionner. Un seul proxy par serveur, en attendant un identifiant d'instance.
- Un client qui réutilise le même span pour deux appels d'outil leur donne le même identifiant de run : le graphe fusionne alors les deux appels en un seul run, qui porte les lectures des deux mais l'outil du premier événement projeté, donc selon l'ordre d'arrivée (`tests/test_trace_context_graph.py` le constate).
- Sans authentification des émetteurs, un identifiant dérivé n'est pas une preuve d'origine : qui connaît le contexte d'un appel peut émettre un événement pour son run.
- Une reprise HTTP garde son run quand le contexte vient de `_meta`. Quand il vient de l'en-tête d'un client HTTP qui ouvre un span par tentative, ou quand le proxy le crée, chaque tentative est un run distinct.
- Le proxy ne rattache pas un appel au run de l'agent : la facette standard `parent` exige le job de l'agent, que le proxy ne connaît pas. Un émetteur qui connaît l'agent peut le faire ; `traceId` relie déjà les appels d'une même trace.
- Le résultat de l'appel n'est pas lu, sauf pour y trouver une erreur JSON-RPC : une erreur d'outil rendue comme un résultat (`isError`) compte comme un succès, et une erreur JSON-RPC rendue en `200` reste un événement `COMPLETE`, dont l'issue dit l'erreur.
- L'issue d'une réponse compressée tient à son seul statut HTTP : son corps n'est pas lu.
- Le proxy ne pose lui-même aucun délai de lecture : seul le délai de connexion au serveur MCP (10 s) fait une issue `timeout`, sauf avec un client HTTP qui en a un autre.
- Sans session MCP ni contexte de trace transmis par le client, la portée d'une reprise est le proxy entier : deux clients qui font le même appel, aux mêmes arguments, dans la minute qui suit un échec, sont comptés comme une reprise.
- Le transport stdio n'est pas pris en charge.
- Une règle de lecture se fie à l'outil : si l'outil lit autre chose que ce qu'elle déclare, le lignage le croit. Elle ne dit pas non plus quelles lignes d'un jeu de données correspondent à une clé : l'empreinte rapproche des appels, pas des lignes.
- Les règles de lecture ne se rechargent pas à chaud : un changement demande de relancer le proxy.
- Dans un corps plus grand que la taille lue, une clé `method` ou `name` répétée au-delà des 16 premiers Ko ne peut pas être vue : l'événement décrit la première.
- Le budget se mesure en temps écoulé : sur une machine très chargée, une requête qui tient d'ordinaire dans le budget peut le dépasser et rester « non résolue » tant qu'elle est en cache.
- Les signaux de qualité de service restent dans l'événement reçu : le graphe ne les stocke pas encore.
- La forme d'une requête garde les noms d'un seul mot choisis par son auteur : un alias qui serait une donnée métier (`AS "Dupont"`) est conservé tel quel. Un nom qui n'a pas la forme d'un identifiant fait renoncer à la forme.
- L'empreinte du résultat porte sur les octets relayés : le même résultat, compressé ou non, ou découpé autrement en événements SSE, a deux empreintes différentes.
- Les arguments d'un appel dont le corps dépasse la taille lue ne sont pas lus : pas d'empreinte des arguments.
