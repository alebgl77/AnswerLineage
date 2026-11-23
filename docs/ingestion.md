# API d'ingestion

L'API reçoit des `RunEvent` OpenLineage (schéma 2-0-2, OpenLineage 1.55), les valide et les enregistre tels quels dans `event_raw`. Elle est versionnée sous `/api/v1`. Sa spécification OpenAPI est servie sur `/api/v1/openapi.json` et publiée dans [`spec/openapi/answerlineage-v1.json`](../spec/openapi/answerlineage-v1.json).

```bash
ANSWERLINEAGE_DATABASE_URL=postgresql://utilisateur:motdepasse@hote/base uv run uvicorn --factory answerlineage_api.app:create_app
```

| Route | Corps | Réponse |
| --- | --- | --- |
| `POST /api/v1/lineage` | Un événement | `201` avec son identifiant, `422` avec les champs fautifs |
| `POST /api/v1/lineage/batch` | Un tableau d'événements, 1 000 au plus | `202` avec le bilan du lot |
| `GET /api/v1/ingestion/duplicates` | Aucun | Les doublons reçus, par émetteur et par jour |
| `GET /api/v1/ingestion/undescribed-tools` | Aucun | Les outils appelés sans règle de lecture, par émetteur |
| `GET /api/v1/ingestion/dead-letters` | Aucun | Les événements au rebut, que la projection n'a pas pu intégrer, et les décomptes par motif |
| `POST /api/v1/ingestion/dead-letters/replay` | Les filtres du rejeu | `202` avec le nombre d'événements remis en file |
| `POST /api/v1/coverage` | Un relevé de couverture | `201` avec l'état de sa fenêtre, `422` avec les champs fautifs |

`/api/v1/lineage` est le point d'entrée standard d'OpenLineage : un émetteur conforme s'y branche sans adaptation. `/api/v1/coverage` reçoit ce que chaque émetteur a vu, pour mesurer la part du trafic réellement captée ([Relevés de couverture](#relevés-de-couverture)). Les événements que les workers n'ont pas pu intégrer au graphe se consultent et se rejouent par la [file de rebut](#file-de-rebut).

## Lots

Chaque événement d'un lot est validé séparément. Un événement invalide est rejeté avec le chemin de ses champs fautifs, le reste du lot est accepté :

```json
{
  "accepted": 2,
  "duplicates": 0,
  "rejected": [
    {
      "index": 1,
      "errors": [{ "path": "/run/runId", "type": "value_error", "message": "..." }]
    }
  ]
}
```

| Statut | Cas |
| --- | --- |
| `202` | Au moins un événement valide, ou lot vide |
| `422` | Aucun événement valide |
| `400` | Le corps n'est pas un tableau JSON (`invalid_json`, `json_too_deep`, `invalid_gzip`, `batch_must_be_an_array`) |
| `413` | Le corps ou le lot dépasse sa taille maximale (`body_too_large`, `batch_too_large`) |
| `415` | Le corps porte un autre encodage que gzip (`unsupported_content_encoding`) |

`path` est un pointeur JSON (RFC 6901). Un rejet ne renvoie jamais la valeur reçue : seulement le chemin, un code et un motif. Les bornes de taille, de profondeur et de nombre sont décrites plus bas, dans [Limites](#limites).

## Validation

Le contrat est le schéma officiel d'OpenLineage, copié dans `spec/openlineage/2-0-2/`. L'API le traduit en modèles Pydantic pour la vitesse ; un test compare les deux, événement par événement, sur des événements valides et invalides.

Deux textes que PostgreSQL ne peut pas conserver sont rejetés (`unstorable_text`) : le caractère nul et les demi-codets Unicode isolés. Ce motif vient en tête de la liste des erreurs.

Au-delà du schéma, l'API contrôle l'heure de l'événement et chacun de ses identifiants.

### Heure de l'événement

`eventTime` suit la RFC 3339, avec son fuseau ; `T` et `Z` s'écrivent en majuscules ou en minuscules. Ramenée en UTC, l'heure tombe entre le 1er janvier 1970 et un jour après la réception de l'événement ; sinon l'événement est rejeté (`event_time_out_of_range`).

- Aucun événement de lignage ne précède 1970 ; une année qui sortirait de 1 à 9999 une fois ramenée en UTC (`9999-12-31T23:59:59-14:00`) ne se relirait plus et ferait échouer toute lecture du graphe.
- Un jour d'avance couvre un décalage d'horloge, comme un fuseau mal déclaré (14 heures au plus). Au-delà, l'heure est fausse : elle fixerait pour toujours l'état du run, que donne son dernier événement, et placerait sa réponse en tête des plus récentes.
- Aucune borne n'empêche d'envoyer des événements anciens : un émetteur peut rattraper son retard.

L'ingestion et les workers lisent l'heure avec la même fonction : un événement accepté est toujours projeté.

### Identifiants

Un identifiant reçu finit dans le graphe, l'explorateur et les gabarits de rappel : il n'est accepté que s'il a la forme d'un nom. Sont des identifiants :

| Champ | Longueur maximale |
| --- | --- |
| `job.namespace` (agent, serveur MCP, émetteur) | 512 |
| `job.name` (agent, outil, job) | 512 |
| `namespace` d'un jeu de données d'entrée ou de sortie | 512 |
| `name` d'un jeu de données d'entrée ou de sortie | 1 024 |
| Facette `parent` : `job.namespace`, `job.name` | 512 |
| Facette `answerlineage_toolCall` : `server`, `tool` | 255 |
| Facette `schema` d'une entrée : `fields[].name` | 300 |
| Facette `version` : `datasetVersion` | 255 |
| Facette `answerlineage_delivery` (dans `facets` ou `outputFacets`) : `fingerprint` | 255 |
| Même facette : `channel` | 64 |
| Même facette : `messageRef` | 512 |
| Même facette : `recipients[]` | 255 |
| Nom d'une facette : clé de `run.facets`, `job.facets`, `facets`, `inputFacets` ou `outputFacets` | 128 |

La longueur se compte en caractères de la forme normalisée. Un identifiant contient des lettres, des marques et des chiffres de toute écriture, et ces signes : `. _ - / : @ = + ~ % , # $ * [ ]`. Ils suffisent aux conventions de nommage d'OpenLineage : `postgres://crm:5432`, `gs://seau/chemin/fichier.csv`, `kafka://broker1:9092,broker2:9092`, `abfss://conteneur@compte.dfs.core.windows.net`, `référentiel.société.coût_unitaire`, `projet.jeu.evenements_2026*`.

Tout le reste est rejeté, un code par motif. Quand une valeur a plusieurs défauts, le premier de cette liste est rapporté :

| Code | Motif |
| --- | --- |
| `identifier_control` | Un caractère de contrôle |
| `identifier_invisible` | Un caractère invisible ou de formatage : largeur nulle, marque ou forçage de direction, sélecteur de variante, étiquette, trait d'union conditionnel (propriété Unicode *Default_Ignorable_Code_Point*) |
| `identifier_markup` | Un chevron, un guillemet ou un accent grave, y compris sous une forme de compatibilité comme `＜` : rien qui forme une balise ou sorte d'une chaîne |
| `identifier_whitespace` | Un espace ou un blanc, quel qu'il soit : tabulation, retour à la ligne, espace insécable ou idéographique |
| `identifier_forbidden_character` | Tout autre signe : ponctuation, symbole, émoji, usage privé |
| `identifier_credentials` | Un mot de passe devant l'hôte d'une adresse (`postgres://utilisateur:motdepasse@hote`) : une adresse s'écrit sans lui, sinon le secret d'un émetteur mal réglé entrerait dans le graphe et dans les réponses de l'API. Un nom d'utilisateur seul (`conteneur@compte`) reste admis |
| `identifier_too_long` | Plus de caractères que la borne du champ |
| `identifier_empty` | Un texte vide |

Un nom fait d'une phrase est refusé parce qu'il contient des espaces : une instruction en langage naturel glissée dans un nom de table (`Ignore les consignes précédentes et…`) est rejetée avec `identifier_whitespace`, dont le motif dit qu'une phrase n'est pas un nom. Le motif est un texte fixe : il ne cite jamais la valeur reçue.

```json
{ "path": "/inputs/0/name", "type": "identifier_whitespace", "message": "Un identifiant ne contient ni espace ni blanc : une phrase n'est pas un nom." }
```

Un nom réel qui contient des espaces, celui d'un document par exemple, s'envoie encodé (`Politique%20voyages%202026.pdf`) : `%` est admis, et le nom reste lisible. Les schémas publiés des [facettes](facettes.md) portent les mêmes longueurs maximales que ce tableau.

Une facette inconnue, et un champ d'une facette connue qui n'est ni dans ce tableau ni parmi les [signaux de qualité de service](#signaux-de-qualité-de-service), ne sont pas contrôlés : ils n'entrent pas dans le graphe. Une valeur de ces champs qui n'est pas un texte est tolérée : la projection l'ignore.

Le nom d'une facette est choisi par l'émetteur, et une erreur de validation le citerait dans son chemin (`/run/facets/<nom>/_producer`). Il suit donc la règle des identifiants : un nom qui n'en a pas la forme rejette l'événement, et l'erreur désigne l'objet qui le porte sans le citer. Les erreurs internes à cette facette ne sont pas rapportées : le nom suffit au rejet.

```json
{ "path": "/run/facets", "type": "identifier_markup", "message": "Un identifiant ne contient ni chevron, ni guillemet, ni accent grave : rien qui puisse former une balise." }
```

Les noms des émetteurs réels ont cette forme : `spark.logicalPlan`, `environment-properties`, `answerlineage_toolCall`. Aucune autre clé reçue n'apparaît dans un chemin : les champs d'une facette sont admis tels quels, sans être validés un à un, et une clé inconnue d'un objet de l'événement est ignorée.

### Forme normalisée

Le contrôle porte sur la forme normalisée NFC de l'identifiant, et c'est elle qui entre dans le graphe : `société` écrit avec un `é` composé ou avec un `e` suivi d'un accent combinant désigne un seul jeu de données. L'événement brut, lui, reste enregistré tel qu'il a été reçu, et son identifiant reste l'empreinte de cette forme reçue. Les [workers](workers.md) normalisent chaque identifiant qu'ils recopient dans le graphe.

### Signaux de qualité de service

La facette `answerlineage_qos` d'un run ([facettes](facettes.md#answerlineage_qos)) porte des signaux qu'un émetteur a mesurés : le proxy y met la latence d'un appel d'outil, celle qu'il y ajoute, son issue et ses reprises. Ils restent dans l'événement reçu : le graphe ne les stocke pas encore. Chacun, quand il est présent, est borné ; un signal hors de ses bornes rejette l'événement :

| Champ | Admis | Code d'un refus |
| --- | --- | --- |
| `latencyMs`, `addedLatencyMs` | Un nombre fini, de 0 à 1 000 000 000 (millisecondes : plus de onze jours) | `float_type`, `finite_number`, `greater_than_equal`, `less_than_equal` |
| `errorCount`, `retryCount` | Un entier, de 0 à 1 000 000 000 | `int_type`, `greater_than_equal`, `less_than_equal` |
| `outcome` | `success`, `jsonrpc_error`, `http_error`, `transport_error`, `timeout` ou `client_abort` | `literal_error` |
| `errorCode` | Un entier signé sur 32 bits | `int_type`, `greater_than_equal`, `less_than_equal` |
| `readKeys`, dans la facette `answerlineage_toolCall` | `fingerprinted`, `no_fingerprint_key`, `request_too_large`, `missing_argument`, `unsupported_argument` ou `too_many_keys` : ce qu'est devenue la clé lue d'après une [règle de lecture](proxy.md#ce-que-dit-lévénement) | `literal_error` |

Un booléen n'est pas un nombre, ni `null` une valeur. Les versions 1-0-0 et 1-1-0 de la facette sont acceptées toutes deux : la première n'a ni `addedLatencyMs`, ni `outcome`, ni `errorCode`. Les autres champs de la facette (`model`, `inputTokens`, `estimatedCost`…) ne sont pas contrôlés, et la facette posée ailleurs que sur le run non plus. Comme pour tout rejet, l'erreur donne le chemin et un motif fixe, jamais la valeur reçue :

```json
{ "path": "/run/facets/answerlineage_qos/outcome", "type": "literal_error", "message": "Input should be 'success', 'jsonrpc_error', 'http_error', 'transport_error', 'timeout' or 'client_abort'" }
```

### Assertions de qualité

La facette standard `dataQualityAssertions` d'une entrée, qu'émettent dbt et d'autres outils de tests de données, n'est pas contrôlée à l'ingestion : un événement n'est jamais rejeté pour elle, et son texte libre (nom, description, corps SQL d'un test, valeurs attendue et observée) reste dans l'événement reçu. Les workers la lisent ensuite avec tolérance, quatre champs seulement, chacun recontrôlé, pour [proposer un incident](incidents.md#incidents-proposés-par-un-test-de-qualité) ; une facette mal formée est ignorée.

## Contrat

La spécification publiée est un contrat. Des tests générés depuis la spécification vérifient que chaque route répond avec un statut, un type de contenu et un corps conformes à ce qu'elle déclare, sans jamais d'erreur du serveur. Le pipeline compare le contrat d'une merge request à celui de la branche cible et échoue sur un changement incompatible : route ou champ de réponse retiré, type modifié, paramètre devenu obligatoire. Une rupture demande une nouvelle version de l'API.

`make openapi` met à jour le fichier publié, `make contract` rejoue la comparaison sur le poste.

## Hôtes et origines attendus

L'API, ingestion et lecture, n'accepte une requête que si son en-tête `Host` désigne un hôte attendu, et son en-tête `Origin`, quand il est présent, une origine attendue. Une page web ouverte sur le poste ne peut ainsi ni lire le lignage ni envoyer de faux événements, même par réassociation DNS.

| Variable | Rôle | Défaut |
| --- | --- | --- |
| `ANSWERLINEAGE_ALLOWED_HOSTS` | Noms d'hôte sous lesquels l'API est jointe, séparés par des virgules | aucun |
| `ANSWERLINEAGE_ALLOWED_ORIGINS` | Origines de pages autorisées à appeler l'API, sous la forme `https://console.exemple.fr` | aucune |

La boucle locale (`127.0.0.1`, `localhost`, `[::1]`) est toujours acceptée, et une origine dont l'hôte est accepté l'est aussi. Le port d'un hôte n'est pas comparé. `*` accepte tout hôte, toute origine et toute requête inter-sites : à réserver à une API placée derrière un autre contrôle.

Une autre page peut aussi viser l'API par une balise `<img>` ou `<script>`, qui n'envoie pas `Origin`. Elle ne lit pas la réponse, mais en apprendrait le statut : `200` ou `404` sur `/api/v1/impact?namespace=…&name=…` dit si un jeu de données existe. Le navigateur dit d'où vient une requête (`Sec-Fetch-Site`) et ce qu'elle charge (`Sec-Fetch-Mode`, `Sec-Fetch-Dest`). Sans origine acceptée, une requête venue d'une autre page est refusée, qu'elle vienne d'un autre site ou d'un autre nom du même site, un sous-domaine voisin par exemple. Deux cas restent admis : la page elle-même et son utilisateur qui saisit l'adresse (`same-origin`, `none`), et la navigation d'un lien suivi depuis une autre page (`navigate` vers `document`), pour qu'une adresse de l'explorateur partagée dans une messagerie web ou un wiki s'ouvre ; une page chargée dans un cadre n'en fait pas partie. Une console déclarée dans `ANSWERLINEAGE_ALLOWED_ORIGINS` reste admise, et un client qui n'est pas un navigateur, qui n'envoie pas ces en-têtes, ne voit aucun changement. Toute réponse porte en plus `Cross-Origin-Resource-Policy: same-origin` : le navigateur ne la livre à aucune page d'une autre origine qui la chargerait par une balise.

Une requête refusée reçoit `403` avec `{"error":"host_not_allowed"}`, `{"error":"origin_not_allowed"}` ou `{"error":"cross_site_not_allowed"}`. Un réglage mal formé empêche l'application de démarrer.

Toute réponse de l'application, route de l'API, fichier de l'explorateur, refus ou erreur, porte les mêmes en-têtes de sécurité : la politique de sécurité du contenu de l'[explorateur](explorateur.md), `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cross-Origin-Opener-Policy: same-origin` et `Cross-Origin-Resource-Policy: same-origin`.

## Idempotence

Un événement rejoué n'a aucun effet : les reprises d'envoi sont sans risque. Trois choses le garantissent.

**Identité.** L'identifiant d'un événement est l'empreinte SHA-256 de sa forme canonique (clés triées, sans espace superflu). Deux envois du même contenu sont le même événement, quel que soit l'ordre des clés. Un événement dont un seul champ diffère est un autre événement.

**Rejeu.** Un événement déjà reçu n'est pas enregistré une seconde fois et n'entre pas dans la file de traitement : le graphe ne change pas, il garde une seule arête. Le rejeu est compté, jamais ignoré en silence. Dans un lot, un événement présent *n* fois et inconnu jusque-là compte *n* − 1 doublons ; déjà connu, il en compte *n*. La réponse (`duplicates` pour un lot, `duplicate` pour un événement seul) dit la même chose que le compteur.

**Ordre d'arrivée.** Les événements d'un même run peuvent arriver dans n'importe quel ordre, rejeux intercalés : l'écriture du graphe est commutative (voir [Schéma du graphe](schema.md)) et le graphe obtenu est le même. Les [workers](workers.md) projettent chaque événement nouveau, une fois.

## Compteur de doublons

Les services sont sans état et PostgreSQL est la seule dépendance : le compteur est la table `ingest_duplicate`, mise à jour dans la transaction qui reçoit l'événement. Il se tient par **émetteur** et par **jour** (date UTC de réception, posée par la base). L'émetteur est l'espace de noms du job de l'événement : `mcp://entrepot` pour le proxy placé devant ce serveur, `agent://ventes` pour un adaptateur d'agents. C'est la maille à laquelle chercher d'où vient une tempête de rejeux.

Le chemin normal ne paie rien : un envoi sans doublon ne lit ni n'écrit la table, aucune ligne chaude n'est disputée par tous les envois. Une ligne n'existe qu'à partir du premier doublon de l'émetteur ce jour-là.

L'émetteur est recopié sous la forme que l'ingestion admet et que le graphe retient : un [identifiant](#identifiants) borné, normalisé (NFC). Un événement dont l'espace de noms n'a pas la forme d'un nom est rejeté avant d'être compté.

```bash
curl "http://127.0.0.1:8000/api/v1/ingestion/duplicates?limit=30"
```

```json
{
  "duplicates": [
    { "emitter": "agent://ventes", "day": "2026-11-15", "count": 1 },
    { "emitter": "mcp://entrepot", "day": "2026-11-15", "count": 3 }
  ]
}
```

| Paramètre | Rôle |
| --- | --- |
| `limit` | Nombre maximal de lignes (émetteur et jour), de 1 à 1 000, 100 par défaut |

Les lignes vont du jour le plus récent au plus ancien, puis par émetteur. Un paramètre invalide répond `422` avec son chemin et son motif, jamais la valeur reçue. Comme toute lecture, cette route s'exécute sous le délai maximal des lectures : au-delà, elle répond `503` et `query_timeout` ([bornes et délais](lecture.md#bornes-et-délais)).

Chaque événement nouveau entre dans la file de traitement, d'où un [worker](workers.md) le projette dans le graphe.

## Outils à décrire

Un point de capture qui voit l'appel d'un outil sans règle de lecture ni SQL déclaré le marque : la lecture de l'appel est non résolue, motif `undescribed_tool` ([le proxy](proxy.md#les-outils-à-décrire)). Cette route rassemble, à partir des événements reçus, les outils ainsi marqués : un exploitant sait quelles règles écrire sans ouvrir le journal de chaque proxy. Elle lit les événements bruts, pas le graphe.

```bash
curl "http://127.0.0.1:8000/api/v1/ingestion/undescribed-tools?days=7"
```

```json
{
  "tools": [
    { "emitter": "mcp://catalogue", "tool": "get_weather", "calls": 12, "last_seen": "2026-11-15T10:42:07.120000Z" }
  ]
}
```

| Paramètre | Rôle |
| --- | --- |
| `days` | Période lue, en jours de réception jusqu'à maintenant, de 1 à 31, 7 par défaut |
| `limit` | Nombre maximal d'outils (émetteur et outil), de 1 à 1 000, 100 par défaut |

`calls` compte les événements de la période qui marquent l'outil ; `last_seen` est la réception du plus récent. Les outils vont du plus récemment vu au plus ancien, puis par émetteur et par nom, sous la forme normalisée (NFC) du nom. Un outil décrit depuis disparaît de la liste quand ses derniers appels non décrits sortent de la période. Un paramètre invalide répond `422` avec son chemin et son motif ; comme toute lecture, la route s'exécute sous le délai maximal des lectures.

## Relevés de couverture

Un outil de rappel qui ne dit pas ce qu'il ignore rassure à tort : « aucune réponse touchée » ne vaut que si l'on sait quelle part des appels a été vue. Chaque émetteur publie donc, fenêtre par fenêtre, ce qu'il a vu :

```bash
curl -X POST http://127.0.0.1:8000/api/v1/coverage -H 'content-type: application/json' -d '{
  "emitter": "mcp://entrepot",
  "instance": "proxy-entrepot-7f9c",
  "window": { "from": "2026-11-11T09:00:00Z", "to": "2026-11-11T09:01:00Z" },
  "calls_seen": 1000,
  "events_emitted": 990,
  "events_lost": 10,
  "uninspected": 0
}'
```

| Champ | Contenu |
| --- | --- |
| `emitter` | L'émetteur, sous la même définition que le [compteur de doublons](#compteur-de-doublons) : l'espace de noms de ses jobs, `mcp://entrepot` pour le proxy placé devant ce serveur |
| `instance` | Facultatif : l'instance de l'émetteur qui publie. Plusieurs proxys placés devant un même serveur partagent l'émetteur ; chacun publie sous son instance, et leurs relevés s'additionnent ([Fusion](#fusion)). Un relevé sans instance compte pour une instance à part |
| `window` | La fenêtre du relevé, début inclus, fin exclue (RFC 3339, avec fuseau) |
| `calls_seen` | Appels d'outils vus pendant la fenêtre |
| `events_emitted` | Événements remis à leur destination |
| `events_lost` | Événements que l'émetteur n'a pas pu remettre |
| `uninspected` | Facultatif, zéro par défaut : requêtes relayées sans pouvoir être inspectées, qui cachent peut-être des appels (voir le [proxy](proxy.md#ce-que-le-proxy-ne-peut-pas-inspecter)). Un adaptateur n'en a pas |

Un appel compte dans la fenêtre où tombe l'heure de son événement. Une fenêtre sans appel se publie aussi : c'est le battement qui prouve que l'émetteur n'est pas muet ([contrôle des émetteurs muets](workers.md#émetteurs-muets)).

### Validation

Le relevé est une entrée hostile comme une autre. Chaque faute est rapportée par son chemin, un code et un motif fixe, jamais la valeur reçue, et toutes le sont ensemble :

| Code | Motif |
| --- | --- |
| `identifier_…` | L'émetteur n'a pas la forme d'un [identifiant](#identifiants) d'espace de noms (512 caractères au plus), ou l'instance celle d'un identifiant de 255 caractères au plus (`/instance`) |
| `window_inverted` | La fin de la fenêtre ne suit pas son début |
| `window_too_long` | La fenêtre dure plus d'un jour |
| `window_out_of_range` | La fenêtre commence avant le 1er janvier 1970 |
| `window_in_future` | La fenêtre s'achève plus d'un jour après la réception du relevé : la tolérance de l'[heure des événements](#heure-de-lévénement) |
| `int_type`, `greater_than_equal`, `less_than_equal` | Un compteur n'est pas un entier (ni booléen, ni nombre à virgule), est négatif ou dépasse un milliard |
| `counts_inconsistent` | Les événements émis et perdus dépassent les appels vus |
| `missing` | Un champ obligatoire manque |

Un champ inconnu est ignoré. Le corps est lu comme celui des événements : mêmes [limites](#limites) de taille, de compression et de profondeur, mêmes refus `400`, `413` et `415`.

### Fusion

Les relevés se rangent dans la table `coverage_report` ([schéma](schema.md#couverture-du-lignage)), un par émetteur, instance et fenêtre. Deux relevés de la même instance et de la même fenêtre se fusionnent compteur par compteur, au plus grand : un relevé rejoué ne change rien, deux relevés arrivés dans un ordre ou dans l'autre donnent le même état, et un émetteur peut republier une fenêtre complétée. La réponse rend l'état de la fenêtre de cette instance après fusion ; elle ne porte `instance` que si le relevé en portait une.

Deux instances ne se fusionnent jamais : leurs relevés d'une même fenêtre restent deux lignes, et la [lecture de la couverture](lecture.md#couverture-du-lignage) les additionne pour mesurer l'émetteur. Un relevé sans instance est rangé sous une instance à part : un émetteur qui ne connaît pas d'instance, le seul de son espace de noms, est mesuré comme avant.

Un émetteur republie ainsi une fenêtre déjà close quand il apprend plus tard ce qu'il est advenu de ses événements : la [boîte d'envoi du proxy](proxy.md#boîte-denvoi) compte un événement émis une fois accepté par l'API, perdu s'il se perd, et corrige la fenêtre de l'événement. Un relevé corrigé ne fait que grandir les compteurs du précédent, appels vus inchangés : la fusion au plus grand rend le dernier, quel que soit l'ordre d'arrivée des corrections, rejeux compris. Un émetteur qui republierait une fenêtre en diminuant un compteur verrait garder l'ancienne valeur : ses compteurs doivent être monotones.

Un relevé qui ouvre une fenêtre ou fait grandir un compteur date le dernier relevé de son émetteur, quelle que soit son instance : c'est ce que surveille le contrôle des émetteurs muets, émetteur par émetteur. Un rejeu ne ranime pas un émetteur muet.

La couverture qui s'en déduit se lit par `GET /api/v1/coverage` et dans chaque résultat d'impact ([API de lecture](lecture.md#couverture-du-lignage)).

## File de rebut

Un événement accepté que les [workers](workers.md#file-de-rebut) ne parviennent pas à intégrer au graphe va à la file de rebut, avec son émetteur et le motif de l'échec, un code d'une [liste fermée](workers.md#motifs). Deux routes la consultent et la rejouent ; depuis le terminal, [`answerlineage dead-letters`](cli.md#answerlineage-dead-letters).

### Consulter le rebut

```bash
curl "http://127.0.0.1:8000/api/v1/ingestion/dead-letters?emitter=mcp://catalogue&limit=50"
```

```json
{
  "dead_letters": [
    {
      "event_id": "2a8b9477e2742f3eaeb18f4910603feb800eff1bc80f577f9d0e9cc4851eab10",
      "run_id": "0193a9c5-7a10-7c3e-9d5e-2f1b6c8a0001",
      "emitter": "mcp://catalogue",
      "reason": "constraint_violation",
      "attempts": 1,
      "received_at": "2026-11-16T09:00:00.120000Z",
      "first_failed_at": "2026-11-16T09:00:01.500000Z",
      "last_failed_at": "2026-11-16T09:00:01.500000Z"
    }
  ],
  "counts": [
    { "reason": "invalid_identifier", "count": 0 },
    { "reason": "invalid_event", "count": 0 },
    { "reason": "constraint_violation", "count": 1 },
    { "reason": "invalid_value", "count": 0 },
    { "reason": "database_error", "count": 0 },
    { "reason": "projection_error", "count": 0 }
  ],
  "truncated": false,
  "more": false,
  "next_cursor": null
}
```

| Paramètre | Rôle |
| --- | --- |
| `emitter` | Seulement cet émetteur, sous l'une ou l'autre écriture Unicode : un identifiant d'espace de noms, 512 caractères au plus |
| `reason` | Seulement ce motif, de la liste fermée |
| `limit` | Événements par page, de 1 à 500, 100 par défaut |
| `cursor` | La page suivante : la valeur de `next_cursor` |

| Champ | Contenu |
| --- | --- |
| `dead_letters` | Les plus récemment mis au rebut d'abord : l'événement (`event_id`, `run_id`), son émetteur, le motif, les essais en échec, la réception de l'événement et ses heures d'échec. Aucun contenu |
| `counts` | Les événements au rebut par motif, chaque motif de la liste fermée dans son ordre, zéro compris. Ils suivent le filtre d'émetteur, pas celui de motif : ils disent la répartition des motifs |
| `truncated` | Vrai au-delà de 100 000 événements au rebut sous le filtre d'émetteur : les décomptes sont alors des minimums |
| `more`, `next_cursor` | La suite de la liste, page par page, sans perte ni doublon, comme les autres listes de l'API ([pagination](lecture.md#pagination)) |

Un paramètre invalide répond `422` avec son chemin et son motif, jamais la valeur reçue ; un émetteur qui n'a pas la forme d'un [identifiant](#identifiants) est refusé avec le code de l'ingestion (`identifier_…`). Comme toute lecture, la route s'exécute sous le délai maximal des lectures : au-delà, elle répond `503` et `query_timeout`.

### Rejouer le rebut

```bash
curl -X POST http://127.0.0.1:8000/api/v1/ingestion/dead-letters/replay -H 'content-type: application/json' -d '{"reason": "constraint_violation"}'
```

```json
{ "replayed": 1, "more": false }
```

| Champ | Contenu |
| --- | --- |
| `emitter` | Seulement les événements de cet émetteur |
| `reason` | Seulement ceux mis au rebut pour ce motif |
| `event_ids` | Seulement ces événements, par leur identifiant, de 1 à 1 000 |
| `limit` | Événements remis en file au plus, les plus anciens échecs d'abord, de 1 à 10 000, 1 000 par défaut |

Les filtres donnés se cumulent ; un corps vide, `{}`, rejoue tout le rebut, `limit` événements à la fois. Les événements choisis sont retirés du rebut et remis en file dans une seule transaction. La réponse, `202`, dit combien (`replayed`) et s'il en reste sous ces filtres (`more`) : la borne est atteinte, ou un autre rejeu les tenait. Relancer jusqu'à `more: false`. Deux rejeux simultanés ne remettent jamais un événement en file deux fois.

Rejouer est sans risque : la projection est idempotente. Un événement qui échoue encore revient au rebut, avec son nouveau motif.

Le corps est lu borné, compressé ou non, comme celui des événements, 256 Kio au plus : mêmes refus `400`, `413` et `415`. Chaque faute est rapportée par son chemin, un code et un motif fixe, jamais la valeur reçue :

| Code | Motif |
| --- | --- |
| `unknown_field` | Le corps porte un autre champ que `emitter`, `reason`, `event_ids` et `limit`. Il n'est pas nommé, et rien n'est rejoué : un filtre mal orthographié ne doit pas rejouer tout le rebut |
| `not_an_object` | Le corps n'est pas un objet JSON |
| `identifier_…` | L'émetteur n'a pas la forme d'un identifiant d'espace de noms |
| `literal_error` | Le motif n'est pas de la liste fermée |
| `string_pattern_mismatch` | Un identifiant d'événement n'est pas une empreinte de 64 chiffres hexadécimaux |
| `too_many_items`, `too_few_items` | `event_ids` nomme plus de 1 000 événements, ou aucun |
| `int_type`, `greater_than_equal`, `less_than_equal` | `limit` n'est pas un entier de 1 à 10 000 |

Le rejeu s'exécute sous le délai maximal des lectures : au-delà, il répond `503` et `query_timeout`, et rien n'est remis en file.

Ces routes n'authentifient pas encore leurs appelants, comme le reste de l'API. Rejouer est une action d'exploitation : quand l'API authentifiera ses appelants et distinguera leurs rôles, le rejeu sera réservé à l'administrateur. D'ici là, l'API ne s'expose pas hors d'un réseau de confiance.

## Limites

Tout ce que l'ingestion reçoit est borné, pour qu'une entrée piégée ne puisse pas épuiser le service : la mémoire et le temps d'une requête restent proportionnels à la taille maximale du corps.

| Limite | Valeur par défaut | Réglage | Réponse |
| --- | --- | --- | --- |
| Taille du corps, en octets reçus | 16 Mio | `ANSWERLINEAGE_MAX_BODY_BYTES`, `create_app(max_body_bytes=…)` | `413` `body_too_large` |
| Taille du corps, en octets déployés d'un corps gzip | La même | La même | `413` `body_too_large` |
| Encodage du corps | Aucun, ou `gzip` seul | Fixe | `415` `unsupported_content_encoding` ; `400` `invalid_gzip` pour un flux corrompu ou tronqué |
| Profondeur d'imbrication, objets et tableaux confondus | 64 niveaux | Fixe | `400` `json_too_deep` |
| Événements d'un lot | 1 000 | `create_app(max_batch_events=…)` | `413` `batch_too_large` |
| Jeux de données en entrée (`inputs`) | 1 000 | Fixe | Événement rejeté, `too_many_items` |
| Jeux de données en sortie (`outputs`) | 1 000 | Fixe | Événement rejeté, `too_many_items` |
| Facettes d'un objet (`run.facets`, `job.facets`, `facets`, `inputFacets`, `outputFacets`) | 100 | Fixe | Événement rejeté, `too_many_items` |
| Champs d'une facette `schema` d'un jeu de données | 10 000 | Fixe | Événement rejeté, `too_many_items` |
| Destinataires d'une facette `answerlineage_delivery` | 10 000 | Fixe | Événement rejeté, `too_many_items` |
| Longueur d'un nom de facette | 128 caractères | Fixe | Événement rejeté, `identifier_too_long` |

**Taille.** Le corps est lu morceau par morceau. Un `Content-Length` qui annonce plus que le maximum est refusé sans qu'un octet soit lu ; sinon la lecture s'arrête dès que le maximum est franchi, que le corps arrive par morceaux ou que son `Content-Length` mente. Un lot de 1 000 événements réels pèse de 1 à 3 Mio : les événements du corpus synthétique font de 0,6 à 2,5 Kio, et un appel d'outil qui lit vingt tables de trente colonnes environ 30 Kio. 16 Mio laissent plus de 16 Kio par événement d'un lot plein, soit cinq fois le plus lourd du corpus, ou plus de 500 de ces appels larges en un lot, et bornent la mémoire d'une requête : le document analysé occupe quelques fois la taille du corps. Un réglage qui n'est pas un nombre d'octets positif empêche l'application de démarrer.

**Compression.** Le transport HTTP d'OpenLineage sait compresser ses envois : `Content-Encoding: gzip` est accepté, plusieurs membres gzip à la suite formant un seul corps. Le déploiement suit la lecture et s'arrête dès que le maximum est dépassé : une bombe (un gigaoctet de zéros tient en un mégaoctet) est refusée après avoir produit au plus un octet de plus que le maximum. La limite vaut pour les octets reçus comme pour les octets déployés. Tout autre encodage, `identity` compris, ou plusieurs encodages empilés sont refusés avant toute lecture.

**Profondeur.** L'analyseur JSON est récursif. Avant qu'il ne s'y engage, un parcours linéaire des octets mesure l'imbrication, en écartant les chaînes et leurs échappements : un crochet écrit dans un texte ne compte pas. Un lot de RunEvents atteint 7 niveaux pour les colonnes d'une facette `schema`, 12 pour la facette standard `columnLineage` ; 64 laissent encore 28 niveaux de structures imbriquées dans un schéma, au-delà des 15 qu'admet BigQuery. Le corps est de l'UTF-8 (RFC 8259), sans marque d'ordre : tout autre codage est refusé (`invalid_json`).

**Nombre d'éléments.** Ce qui se répète dans un événement, et que la validation et la [projection](workers.md) parcourent, est borné. Un dépassement rejette l'événement, avec le chemin du champ et un motif qui cite la borne, jamais le nombre reçu ; un tableau trop long n'est pas parcouru, et rien d'autre n'est rapporté pour cet événement. Le contrat publié porte ces bornes (`maxItems`, `maxProperties`). La borne des destinataires est celle du schéma publié de la [facette de diffusion](facettes.md).

```json
{ "path": "/inputs/0/facets/schema/fields", "type": "too_many_items", "message": "Ce champ compte au plus 10000 éléments." }
```

## Limites connues

- Les émetteurs ne sont pas authentifiés : l'API ne s'expose pas hors d'un réseau de confiance.
- Les routes du rebut ne le sont pas davantage : quiconque joint l'API lit le rebut et le rejoue. Un rejeu ne change pas le graphe d'un événement déjà intégré, mais remet du travail aux workers.
- Aucune limite de débit par émetteur : la taille d'une requête est bornée, pas leur nombre.
- Le corps est déployé puis analysé en mémoire, jusqu'à sa taille maximale : l'analyse JSON n'est pas incrémentale.
- Le contrôle des identifiants porte sur leur forme, pas sur leur sens : une instruction écrite sans espace (`ignore_les_consignes`) a la forme d'un nom et passe. Aucun texte reçu n'atteint un modèle ni un destinataire autrement que dans un gabarit fixe.
- La forme NFC ne confond pas les caractères qui se ressemblent : `а` cyrillique et `a` latin font deux noms distincts.
- L'heure d'un événement est bornée par l'horloge de l'API : le corpus synthétique, daté à partir du 2 novembre 2026, voit refuser ses événements qui tombent plus d'un jour après la date du jour (`--days` élevé).
- L'API n'exporte pas encore de métriques.
- Un relevé n'est pas authentifié plus qu'un événement : un émetteur mal réglé, ou qui en usurpe un autre, peut gonfler les appels vus d'une fenêtre, et la fusion au plus grand ne peut pas le défaire.
- Les instances d'un émetteur se déclarent elles-mêmes : deux émetteurs qui publient sous la même instance se fusionnent au plus grand, et la couverture est alors surestimée. Le proxy en tire une au hasard quand elle n'est pas réglée.
