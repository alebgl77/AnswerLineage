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

`/api/v1/lineage` est le point d'entrée standard d'OpenLineage : un émetteur conforme s'y branche sans adaptation.

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
| `400` | Le corps n'est pas un tableau JSON (`invalid_json`, `batch_must_be_an_array`) |
| `413` | Plus d'événements que la taille maximale (`batch_too_large`) |

`path` est un pointeur JSON (RFC 6901). Un rejet ne renvoie jamais la valeur reçue : seulement le chemin, un code et un motif.

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

La longueur se compte en caractères de la forme normalisée. Un identifiant contient des lettres, des marques et des chiffres de toute écriture, et ces signes : `. _ - / : @ = + ~ % , # $ * [ ]`. Ils suffisent aux conventions de nommage d'OpenLineage : `postgres://crm:5432`, `gs://seau/chemin/fichier.csv`, `kafka://broker1:9092,broker2:9092`, `abfss://conteneur@compte.dfs.core.windows.net`, `référentiel.société.coût_unitaire`, `projet.jeu.evenements_2026*`.

Tout le reste est rejeté, un code par motif. Quand une valeur a plusieurs défauts, le premier de cette liste est rapporté :

| Code | Motif |
| --- | --- |
| `identifier_control` | Un caractère de contrôle |
| `identifier_invisible` | Un caractère invisible ou de formatage : largeur nulle, marque ou forçage de direction, sélecteur de variante, étiquette, trait d'union conditionnel (propriété Unicode *Default_Ignorable_Code_Point*) |
| `identifier_markup` | Un chevron, un guillemet ou un accent grave, y compris sous une forme de compatibilité comme `＜` : rien qui forme une balise ou sorte d'une chaîne |
| `identifier_whitespace` | Un espace ou un blanc, quel qu'il soit : tabulation, retour à la ligne, espace insécable ou idéographique |
| `identifier_forbidden_character` | Tout autre signe : ponctuation, symbole, émoji, usage privé |
| `identifier_too_long` | Plus de caractères que la borne du champ |
| `identifier_empty` | Un texte vide |

Un nom fait d'une phrase est refusé parce qu'il contient des espaces : une instruction en langage naturel glissée dans un nom de table (`Ignore les consignes précédentes et…`) est rejetée avec `identifier_whitespace`, dont le motif dit qu'une phrase n'est pas un nom. Le motif est un texte fixe : il ne cite jamais la valeur reçue.

```json
{ "path": "/inputs/0/name", "type": "identifier_whitespace", "message": "Un identifiant ne contient ni espace ni blanc : une phrase n'est pas un nom." }
```

Un nom réel qui contient des espaces, celui d'un document par exemple, s'envoie encodé (`Politique%20voyages%202026.pdf`) : `%` est admis, et le nom reste lisible. Les schémas publiés des [facettes](facettes.md) portent les mêmes longueurs maximales que ce tableau.

Une facette inconnue, et un champ d'une facette connue qui n'est pas dans ce tableau, ne sont pas contrôlés : ils n'entrent pas dans le graphe. Une valeur de ces champs qui n'est pas un texte est tolérée : la projection l'ignore.

### Forme normalisée

Le contrôle porte sur la forme normalisée NFC de l'identifiant, et c'est elle qui entre dans le graphe : `société` écrit avec un `é` composé ou avec un `e` suivi d'un accent combinant désigne un seul jeu de données. L'événement brut, lui, reste enregistré tel qu'il a été reçu, et son identifiant reste l'empreinte de cette forme reçue. Les [workers](workers.md) normalisent chaque identifiant qu'ils recopient dans le graphe.

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

Les lignes vont du jour le plus récent au plus ancien, puis par émetteur. Un paramètre invalide répond `422` avec son chemin et son motif, jamais la valeur reçue.

Chaque événement nouveau entre dans la file de traitement, d'où un [worker](workers.md) le projette dans le graphe.

## Limites connues

- Les émetteurs ne sont pas authentifiés : l'API ne s'expose pas hors d'un réseau de confiance.
- Le corps est lu en entier avant d'être contrôlé ; seules la taille du lot et la longueur des identifiants sont bornées, pas le nombre d'entrées, de colonnes ou de destinataires d'un événement.
- Le contrôle des identifiants porte sur leur forme, pas sur leur sens : une instruction écrite sans espace (`ignore_les_consignes`) a la forme d'un nom et passe. Aucun texte reçu n'atteint un modèle ni un destinataire autrement que dans un gabarit fixe.
- La forme NFC ne confond pas les caractères qui se ressemblent : `а` cyrillique et `a` latin font deux noms distincts.
- L'heure d'un événement est bornée par l'horloge de l'API : le corpus synthétique, daté à partir du 2 novembre 2026, voit refuser ses événements qui tombent plus d'un jour après la date du jour (`--days` élevé).
- L'API n'exporte pas encore de métriques.
