# Facettes OpenLineage d'AnswerLineage

AnswerLineage reçoit des `RunEvent` OpenLineage ([schéma 2-0-2](../spec/openlineage/README.md)) et les enrichit de cinq facettes qui lui sont propres. Chacune a un schéma JSON versionné : on écrit un émetteur à partir de ces schémas, sans lire le code d'AnswerLineage.

Aucune facette ne porte de contenu : ni prompt, ni argument, ni résultat, ni réponse. Seulement des noms, des empreintes, des tailles et des pseudonymes. D'une requête SQL, le proxy ne garde que la forme sans littéraux, dans la facette de job standard `sql`.

## Où sont les schémas

Un fichier par facette et par version, dans [`spec/facets/`](../spec/facets/1-0-0) :

```text
spec/facets/<version>/<NomDeLaFacette>.json
```

La première version est `1-0-0` ; une facette qui s'étend publie une nouvelle version à côté de la précédente, qui reste servie. Le fichier suit la convention d'OpenLineage pour les facettes personnalisées :

- `$schema` est JSON Schema 2020-12 ;
- `$id` est l'adresse du fichier brut sur la branche principale :
  `https://raw.githubusercontent.com/alebgl77/AnswerLineage/main/spec/facets/<version>/<NomDeLaFacette>.json` ;
- la définition est sous `$defs/<NomDeLaFacette>` et prolonge par `allOf` la définition de base d'OpenLineage (`RunFacet`, `InputDatasetFacet` ou `OutputDatasetFacet`, selon la portée) : `_producer` et `_schemaURL` restent obligatoires ;
- le `_schemaURL` de la facette émise est l'adresse du fichier suivie de `#/$defs/<NomDeLaFacette>`.

Suivre le `_schemaURL` d'une facette donne donc le schéma exact de la version dont elle se réclame.

## Les cinq facettes

Aucune facette ne ferme ses objets : un champ inconnu est admis, comme dans OpenLineage. Les champs de la colonne « Facultatifs » peuvent être absents.

| Facette | Versions | Clé | Portée | Où la placer | Obligatoires |
| --- | --- | --- | --- | --- | --- |
| `AgentRunFacet` | [1-0-0](../spec/facets/1-0-0/AgentRunFacet.json) | `answerlineage_agent` | Run | `run.facets` | aucun |
| `ToolCallRunFacet` | [1-0-0](../spec/facets/1-0-0/ToolCallRunFacet.json), [1-1-0](../spec/facets/1-1-0/ToolCallRunFacet.json) | `answerlineage_toolCall` | Run | `run.facets` | `server` |
| `QosRunFacet` | [1-0-0](../spec/facets/1-0-0/QosRunFacet.json), [1-1-0](../spec/facets/1-1-0/QosRunFacet.json) | `answerlineage_qos` | Run | `run.facets` | aucun |
| `DeliveryOutputDatasetFacet` | [1-0-0](../spec/facets/1-0-0/DeliveryOutputDatasetFacet.json) | `answerlineage_delivery` | Jeu de données de sortie | `outputs[].outputFacets` | `fingerprint`, `channel`, `messageRef` |
| `KeysInputDatasetFacet` | [1-0-0](../spec/facets/1-0-0/KeysInputDatasetFacet.json) | `answerlineage_keys` | Jeu de données d'entrée | `inputs[].inputFacets` | `hmacs` ou `bloomFilter` |

### `answerlineage_agent`

Décrit l'agent qui exécute le run. Le graphe reconnaît un run d'agent à cette facette ; l'agent est celui que nomme le job.

Facultatifs : `agentId`, `framework`, `agentVersion`, `owner` (le responsable), `taskClass` (la classe de tâche déclarée).

### `answerlineage_toolCall`

Décrit un appel d'outil MCP, posé sur le run enfant de l'appel. Le graphe reconnaît un run d'outil à cette facette. C'est ce que le proxy émet.

Facultatifs émis aujourd'hui : `tool` (absent quand le nom reçu n'a pas une forme admise), `transport` (`http`, ou `stdio` pour un serveur que le proxy [lance lui-même](proxy.md#mode-stdio)), `protocolVersion`, `httpStatus` (absent en stdio), `proxyError`, `headerMismatch`, `invalidToolName`, `readResolution` (`resolved` ou `unresolved`), `unresolvedReason` (`undescribed_tool` pour un outil sans règle de lecture ni SQL déclaré, voir [le proxy](proxy.md#les-outils-à-décrire)).

La version 1-1-0 ajoute un champ facultatif, `readKeys` : ce qu'est devenue la clé lue quand une [règle de lecture](proxy.md#règles-de-lecture-des-outils-non-sql) nomme l'argument qui la porte, d'une liste fermée (`fingerprinted`, `no_fingerprint_key`, `request_too_large`, `missing_argument`, `unsupported_argument`, `too_many_keys`). Hors de `fingerprinted`, la lecture vaut pour tout le jeu de données. Le proxy ne se réclame de cette version que pour une facette qui porte `readKeys`.

Le contexte de trace W3C de l'appel, d'où dérive l'identifiant de run (voir [le proxy](proxy.md#identifiant-de-run-et-contexte-de-trace)), pour passer du lignage à la trace dans un outil d'observabilité :

- `traceId` : le `trace-id`, 32 hexadécimaux minuscules, jamais tous nuls ;
- `spanId` : le `parent-id` vu, c'est-à-dire le span qui a fait l'appel, 16 hexadécimaux minuscules, jamais tous nuls ;
- `traceOrigin` : d'où vient le contexte, `meta` (`params._meta.traceparent` de l'appel), `header` (l'en-tête HTTP `traceparent`) ou `proxy` (créé par le proxy, faute de contexte reçu).

Les empreintes à clé de l'appel et la taille de son résultat (voir [le proxy](proxy.md#empreintes-à-clé) pour la clé et le calcul à l'octet près) :

- `argumentsFingerprint` : empreinte des arguments, `hmac-sha256:` suivi de 64 hexadécimaux minuscules. HMAC-SHA-256, avec la clé du locataire, de l'étiquette `answerlineage/fingerprint/arguments/v1`, d'un octet nul et de la forme canonique JSON (RFC 8785) de `params.arguments` ; `{}` pour un appel sans arguments ;
- `resultFingerprint` : empreinte du résultat, de même forme, sur l'étiquette `answerlineage/fingerprint/result/v1` et les octets du corps de la réponse tels que relayés (en stdio, ceux du message de réponse, sans sa fin de ligne) ;
- `resultSize` : taille de ces octets.

Sans clé, les deux empreintes sont absentes : un émetteur n'écrit jamais un hachage sans clé, qu'une énumération inverserait, et le schéma refuse toute autre forme que `hmac-sha256:`. Quand le corps de la réponse n'a pas été relayé en entier, ni `resultFingerprint` ni `resultSize` ne sont émis. Les vecteurs de test sont dans [`spec/fingerprint/vectors-v1.json`](../spec/fingerprint/vectors-v1.json).

Facultatifs prévus, pas encore émis : `readOnly`, `definitionFingerprint` (l'empreinte de la définition de l'outil).

Pour un outil SQL déclaré, la requête n'est pas dans cette facette : sa forme sans littéraux va dans la facette de job standard d'OpenLineage `sql` ([`SQLJobFacet` 1-1-0](../spec/openlineage/facets/1-1-0/SQLJobFacet.json)), sous `job.facets`, avec `query` (la forme, chaque constante remplacée par `?`) et `dialect`. Elle est absente quand l'analyse n'a pas établi de forme sûre (voir [le proxy](proxy.md#le-sql-conservé-sans-ses-littéraux)).

### `answerlineage_qos`

Qualité de service d'un run : chaque émetteur porte ce qu'il voit de son étape. Tous les champs sont facultatifs : `model`, `route`, `inputTokens`, `outputTokens`, `latencyMs`, `estimatedCost`, `errorCount`, `retryCount`, `skill`.

La version 1-1-0 ajoute trois champs facultatifs, que le proxy émet sur chaque appel d'outil avec `latencyMs`, `errorCount` et `retryCount` (voir [le proxy](proxy.md#qualité-de-service) pour leur mesure) :

- `addedLatencyMs` : la latence que l'émetteur ajoute lui-même sur le chemin du run, en millisecondes ;
- `outcome` : l'issue du run, d'une liste fermée : `success`, `jsonrpc_error`, `http_error`, `transport_error`, `timeout`, `client_abort` ;
- `errorCode` : le code numérique de l'erreur, code JSON-RPC ou statut HTTP, entier signé sur 32 bits. Une erreur se dit par sa catégorie et son code, jamais par son message.

Elle borne les durées et les compteurs à un milliard, comme l'[ingestion](ingestion.md#signaux-de-qualité-de-service). `retryCount` absent signifie que l'émetteur ne sait pas reconnaître une reprise, pas qu'il n'y en a pas eu.

Aucune des deux versions ne dit dans quelle monnaie est `estimatedCost` : c'est l'unité de l'émetteur.

### `answerlineage_delivery`

Diffusion d'une réponse d'agent. La sortie qui la porte est la réponse, décrite par son empreinte (`fingerprint`) ; `channel` et `messageRef` disent où elle est partie, `recipients` à qui, sous des pseudonymes sans table de correspondance. Sans cette facette, une sortie est comptée comme une écriture ordinaire.

### `answerlineage_keys`

Clés lues dans un jeu de données, que le proxy émet pour un outil décrit par une règle de lecture. Jamais en clair : `hmacs` liste des empreintes HMAC calculées avec une clé secrète, `bloomFilter` porte un filtre de Bloom à clé encodé en base64. L'une des deux formes au moins est présente. Les paramètres du filtre ne sont pas encore décrits.

Le proxy n'émet que `hmacs` : une empreinte par clé lue, `hmac-sha256:` suivi de HMAC-SHA-256, avec la clé du locataire, de l'étiquette `answerlineage/fingerprint/key/v1`, d'un octet nul et de la forme canonique JSON (RFC 8785) de la valeur, comme ses [autres empreintes](proxy.md#le-calcul-à-loctet-près).

## Écrire un émetteur

Un appel d'outil, tel que le proxy l'émet, avec la facette sur le run :

```json
{
  "eventType": "COMPLETE",
  "eventTime": "2026-11-02T09:00:00.000000Z",
  "producer": "https://exemple.org/mon-emetteur",
  "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent",
  "run": {
    "runId": "0193a9c5-7a10-7c3e-9d5e-2f1b6c8a0000",
    "facets": {
      "answerlineage_toolCall": {
        "_producer": "https://exemple.org/mon-emetteur",
        "_schemaURL": "https://raw.githubusercontent.com/alebgl77/AnswerLineage/main/spec/facets/1-0-0/ToolCallRunFacet.json#/$defs/ToolCallRunFacet",
        "server": "entrepot",
        "tool": "execute_sql"
      }
    }
  },
  "job": {"namespace": "mcp://entrepot", "name": "execute_sql"},
  "inputs": [],
  "outputs": []
}
```

Une réponse diffusée, sur la sortie du run de l'agent :

```json
{
  "namespace": "agent://ventes",
  "name": "analyste-ventes/reponses",
  "outputFacets": {
    "answerlineage_delivery": {
      "_producer": "https://exemple.org/mon-emetteur",
      "_schemaURL": "https://raw.githubusercontent.com/alebgl77/AnswerLineage/main/spec/facets/1-0-0/DeliveryOutputDatasetFacet.json#/$defs/DeliveryOutputDatasetFacet",
      "fingerprint": "sha256:5b1d…",
      "channel": "mail",
      "messageRef": "m-1",
      "recipients": ["8f3a…"]
    }
  }
}
```

Pour valider ses événements, on charge le schéma d'OpenLineage depuis [sa copie locale](../spec/openlineage/README.md) et ceux des facettes depuis `spec/facets/`, puis on résout les références sans réseau. `tests/test_facet_schemas.py` le fait avec `jsonschema` et `referencing`.

Le [kit de conformité](conformite.md) juge ensuite l'émetteur sur ce qu'il a émis : la forme des événements et des facettes, ce qui les relie, ce que le graphe en retiendrait, et l'absence de contenu, prouvée par des canaris.

## Versions et immutabilité

Un fichier de version publié ne change plus jamais, pas d'un octet : l'adresse d'un `_schemaURL` désigne toujours le même schéma. Une évolution passe par une nouvelle version, dans un nouveau dossier (`1-1-0`).

Une nouvelle version d'une facette, par rapport à la précédente :

- ne retire aucun champ ;
- ne rend aucun champ obligatoire, et n'en laisse pas un champ obligatoire cesser de l'être : ce que les lecteurs tenaient pour garanti le reste ;
- ne change le type d'aucun champ.

Elle peut ajouter des champs facultatifs. Une rupture demande une nouvelle facette, sous un autre nom.

`make contract` fait respecter ces règles (`scripts/check_facets_compat.py`), avec le contrôle de l'API. Il compare la branche de travail à la branche cible (`BASE_SHA`, à défaut `origin/main`) : un fichier de version déjà publié qui a changé ou disparu échoue, de même qu'une nouvelle version qui casse la précédente. Il vérifie aussi le rangement : chemin, `$id`, définition sous `$defs`, base d'OpenLineage. Les chemins de `scripts/` sont protégés : une modification du contrôle demande l'approbation d'un mainteneur.

## Limites connues

- Le contrôle compare les champs, leur caractère obligatoire et leur type. Il ne compare ni les bornes (`maxLength`, `maxItems`, `minimum`) ni les valeurs admises (`enum`) : les resserrer dans une nouvelle version casserait des émetteurs sans le faire échouer.
- La facette `answerlineage_agent` n'émet encore aucun champ propre ; de `answerlineage_qos` et `answerlineage_keys`, seul le proxy émet, et seulement ce qu'il voit. Leurs champs sont facultatifs et décrits sobrement ; ils s'étendront.
- Les schémas sont servis par le fichier brut du dépôt sur la branche principale : une adresse n'existe qu'une fois la version fusionnée.
