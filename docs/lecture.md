# API de lecture

L'API de lecture interroge le graphe de lignage. Elle est versionnée sous `/api/v1`, décrite par la même spécification OpenAPI que l'API d'ingestion, et ne renvoie jamais de contenu : des noms, des empreintes, des heures.

| Route | Réponse |
| --- | --- |
| `GET /api/v1/answers?channel=…&message_ref=…` | Les réponses d'agents diffusées par un message |
| `GET /api/v1/answers/{id}/sources` | Les sources d'une réponse : jeux de données, versions, outils, agents |

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

## Limites connues

- L'API n'authentifie pas ses appelants : elle est faite, à ce stade, pour écouter sur la boucle locale ou derrière un contrôle d'accès.
- Un appel d'outil n'est rattaché à une réponse que si son événement désigne le run de l'agent comme parent.
