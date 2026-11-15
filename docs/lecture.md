# API de lecture

L'API de lecture interroge le graphe de lignage. Elle est versionnée sous `/api/v1`, décrite par la même spécification OpenAPI que l'API d'ingestion, et ne renvoie jamais de contenu : des noms, des empreintes, des heures.

| Route | Réponse |
| --- | --- |
| `GET /api/v1/answers?channel=…&message_ref=…` | Les réponses d'agents diffusées par un message |
| `GET /api/v1/answers/{id}/sources` | Les sources d'une réponse : jeux de données, versions, outils, agents |
| `GET /api/v1/impact?namespace=…&name=…&from=…&to=…` | Les réponses et les destinataires touchés par un jeu de données faux pendant une fenêtre |
| `GET /api/v1/graph` | Le graphe en cinq colonnes des réponses les plus récentes |

## Remonter d'une réponse à ses sources

Le destinataire d'un message veut savoir sur quelles données la réponse s'appuie. Il part de ce qu'il a sous les yeux, le canal et la référence du message :

```bash
curl "http://127.0.0.1:8000/api/v1/answers?channel=slack&message_ref=34f209664c2955dd"
```

```json
{
  "answers": [
    {
      "id": "01a1c9e63a187c5cb8b5fce292591000424d79573035c170",
      "fingerprint": "sha256:c13d010082a2c2680884a8cc9766dae0296edf2a64480b9064a8c033795b00ad",
      "produced_at": "2026-11-11T09:18:36Z",
      "run_id": "01a1c9e6-3a18-7c5c-b8b5-fce292591000",
      "agent": { "namespace": "agent://ventes", "name": "assistant-commercial" }
    }
  ]
}
```

Puis il demande les sources de cette réponse :

```bash
curl http://127.0.0.1:8000/api/v1/answers/01a1c9e63a187c5cb8b5fce292591000424d79573035c170/sources
```

```json
{
  "answer": { "id": "01a1c9e63a187c5cb8b5fce292591000424d79573035c170", "…": "…" },
  "agents": [{ "namespace": "agent://ventes", "name": "assistant-commercial" }],
  "tools": [
    { "server": "crm", "name": "lister_opportunites", "calls": 1 },
    { "server": "entrepot", "name": "execute_sql", "calls": 2 }
  ],
  "datasets": [
    {
      "namespace": "bigquery",
      "name": "produit.ref.catalogue",
      "kind": "table",
      "versions": ["2026-11-11"],
      "precision": "columns",
      "columns": ["categorie", "libelle"],
      "first_read_at": "2026-11-11T09:18:17Z",
      "last_read_at": "2026-11-11T09:18:17Z"
    },
    { "namespace": "bigquery", "name": "ventes.suivi.commandes", "…": "…" },
    { "namespace": "postgres://crm:5432", "name": "crm.public.comptes", "…": "…" },
    { "namespace": "postgres://crm:5432", "name": "crm.public.opportunites", "…": "…" }
  ],
  "truncated": false
}
```

Ces exemples viennent de l'[entreprise synthétique](corpus.md) fournie avec le dépôt, avec sa graine par défaut.

### Ce qui compte comme source

Les sources d'une réponse sont les lectures de son run et de tous ses runs enfants : appels d'outils, sous-agents, à toute profondeur. Le run parent et les runs voisins n'en font pas partie.

Un jeu de données lu plusieurs fois n'apparaît qu'une fois :

- `versions` réunit les versions lues, quand l'émetteur les a déclarées ;
- `precision` garde la lecture la moins précise. Un jeu lu en entier une seule fois est une source en entier : `precision` vaut `table` et `columns` est vide ;
- `columns` réunit les colonnes lues quand toutes les lectures les donnent.

### Identifiant d'une réponse

L'identifiant d'une réponse se déduit de son run et de son empreinte. Il reste le même si le graphe est reconstruit à partir des événements, et peut donc être conservé dans un ticket ou un lien.

### Bornes et erreurs

| Statut | Cas |
| --- | --- |
| `200` | Recherche faite ; la liste est vide quand aucun message ne correspond |
| `404` | Aucune réponse ne porte cet identifiant (`unknown_answer`) |
| `422` | Un paramètre est absent, trop long ou mal formé |

Une recherche renvoie 20 réponses au plus, 100 avec `limit`. L'arbre des runs parcouru pour une réponse est borné à 10 000 runs : au-delà, `truncated` vaut `true` et les sources sont partielles.

Une erreur `422` donne le chemin du paramètre fautif, un code et un motif, jamais la valeur reçue :

```json
{ "errors": [{ "path": "/query/message_ref", "type": "missing", "message": "Field required" }] }
```

## Propager un incident

Une table a été fausse du chargement de nuit jusqu'à sa correction. Quelles réponses s'appuient sur elle, et qui les a reçues ?

```bash
curl "http://127.0.0.1:8000/api/v1/impact?namespace=bigquery&name=ventes.suivi.commandes&from=2026-11-11T06:00:00Z&to=2026-11-12T12:00:00Z"
```

```json
{
  "dataset": { "namespace": "bigquery", "name": "ventes.suivi.commandes", "kind": "table" },
  "window": { "from": "2026-11-11T06:00:00Z", "to": "2026-11-12T12:00:00Z" },
  "counts": { "answers": 16, "recipients": 13 },
  "coverage": { "status": "unknown" },
  "graph": { "answers": ["…"], "recipients": ["…"], "…": "…" },
  "truncated": false
}
```

Cet exemple est le premier incident de l'[entreprise synthétique](corpus.md) : sa vérité annonce les mêmes 16 réponses et 13 destinataires.

| Champ | Contenu |
| --- | --- |
| `counts` | Réponses touchées et destinataires distincts, sur tout l'incident |
| `graph` | Les réponses touchées, les plus récentes d'abord, sous la forme du [graphe en cinq colonnes](#le-graphe-en-cinq-colonnes) : `graph.answers` porte chaque réponse et sa chaîne, `graph.recipients` les pseudonymes des destinataires |
| `coverage` | Ce que l'on sait de la part des appels captés pendant la fenêtre. `unknown` : elle n'est pas encore mesurée, le résultat peut donc ignorer des réponses dont la lecture n'a pas été captée |
| `truncated` | `true` quand la propagation dépasse 20 000 runs : le résultat est partiel |

### Ce qui compte comme touché

Une réponse est touchée quand un run de son arbre a lu le jeu de données pendant la fenêtre : le run de l'agent lui-même, un appel d'outil, un sous-agent à toute profondeur.

- **C'est l'heure de la lecture qui compte.** Une réponse rendue après la correction, mais fondée sur une lecture faite avant, est touchée. Une lecture faite après la correction ne l'est pas.
- **La fenêtre inclut son début et exclut sa fin.**
- **Un run qui a lu sans répondre n'a rien à rappeler.**

`limit` (200 au plus) et `before` parcourent la liste des réponses comme pour le graphe ; `counts` reste celui de tout l'incident. Un jeu de données inconnu répond `404` (`unknown_dataset`), une fenêtre vide `422`.

### Mesure

La propagation est mesurée sur un [corpus de vérité](corpus.md#corpus-de-vérité) : pour chaque scénario, toute réponse attendue au niveau table doit être renvoyée. Sur les incidents de l'entreprise synthétique, le résultat est exactement la vérité.

## Le graphe en cinq colonnes

`GET /api/v1/graph` donne une vue d'ensemble du lignage, faite pour être dessinée : cinq colonnes, des données aux destinataires.

```bash
curl "http://127.0.0.1:8000/api/v1/graph?limit=60"
```

La vue part des réponses les plus récentes et ne montre que ce qu'elles touchent : les jeux de données lus pour les produire, les outils appelés, les agents qui ont répondu, les destinataires.

| Champ | Contenu |
| --- | --- |
| `datasets`, `tools`, `agents`, `recipients` | Les nœuds de quatre colonnes, chacun avec un `id` |
| `answers` | Les réponses, les plus récentes d'abord. Chacune porte sa chaîne : `agent`, `tools`, `recipients`, et `reads`, la liste des jeux de données lus avec l'outil qui les a lus |
| `more` | `true` quand des réponses plus anciennes existent |
| `truncated` | `true` quand les arbres de runs dépassent la borne : la vue est partielle |

Les liens d'une colonne à l'autre se déduisent de la chaîne de chaque réponse. Suivre une donnée jusqu'aux personnes revient à garder les réponses dont `reads` la cite.

| Paramètre | Rôle | Défaut |
| --- | --- | --- |
| `limit` | Nombre de réponses dans la vue, 200 au plus | 60 |
| `before` | Ne garder que les réponses produites avant cet instant (RFC 3339, avec fuseau) | aucun |

L'`id` d'une réponse est son identifiant stable. Celui des autres nœuds ne vaut que dans la réponse qui le donne : un jeu de données se désigne par son espace de noms et son nom.

## Limites connues

- L'API n'authentifie pas ses appelants : elle est faite, à ce stade, pour écouter sur la boucle locale ou derrière un contrôle d'accès. Elle refuse déjà les requêtes dont l'hôte ou l'origine n'est pas attendu ([réglages](ingestion.md#hôtes-et-origines-attendus)).
- Un appel d'outil n'est rattaché à une réponse que si son événement désigne le run de l'agent comme parent.
- La propagation s'arrête au niveau table : elle renvoie tout lecteur de la fenêtre, y compris ceux qui n'ont lu que des colonnes, des lignes ou une version saines.
- Elle ne suit pas encore ce qu'un run touché a écrit : une réponse fondée sur une note ou une table dérivée de la donnée fausse n'est pas renvoyée.
- La couverture du lignage sur la fenêtre n'est pas encore mesurée.
