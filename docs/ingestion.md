# API d'ingestion

L'API reçoit des `RunEvent` OpenLineage (schéma 2-0-2, OpenLineage 1.55), les valide et les enregistre tels quels dans `event_raw`. Elle est versionnée sous `/api/v1`. Sa spécification OpenAPI est servie sur `/api/v1/openapi.json` et publiée dans [`spec/openapi/answerlineage-v1.json`](../spec/openapi/answerlineage-v1.json).

```bash
ANSWERLINEAGE_DATABASE_URL=postgresql://utilisateur:motdepasse@hote/base uv run uvicorn --factory answerlineage_api.app:create_app
```

| Route | Corps | Réponse |
| --- | --- | --- |
| `POST /api/v1/lineage` | Un événement | `201` avec son identifiant, `422` avec les champs fautifs |
| `POST /api/v1/lineage/batch` | Un tableau d'événements, 1 000 au plus | `202` avec le bilan du lot |

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

Deux textes que PostgreSQL ne peut pas conserver sont rejetés (`unstorable_text`) : le caractère nul et les demi-codets Unicode isolés.

## Contrat

La spécification publiée est un contrat. Des tests générés depuis la spécification vérifient que chaque route répond avec un statut, un type de contenu et un corps conformes à ce qu'elle déclare, sans jamais d'erreur du serveur. Le pipeline compare le contrat d'une merge request à celui de la branche cible et échoue sur un changement incompatible : route ou champ de réponse retiré, type modifié, paramètre devenu obligatoire. Une rupture demande une nouvelle version de l'API.

`make openapi` met à jour le fichier publié, `make contract` rejoue la comparaison sur le poste.

## Hôtes et origines attendus

L'API, ingestion et lecture, n'accepte une requête que si son en-tête `Host` désigne un hôte attendu, et son en-tête `Origin`, quand il est présent, une origine attendue. Une page web ouverte sur le poste ne peut ainsi ni lire le lignage ni envoyer de faux événements, même par réassociation DNS.

| Variable | Rôle | Défaut |
| --- | --- | --- |
| `ANSWERLINEAGE_ALLOWED_HOSTS` | Noms d'hôte sous lesquels l'API est jointe, séparés par des virgules | aucun |
| `ANSWERLINEAGE_ALLOWED_ORIGINS` | Origines de pages autorisées à appeler l'API, sous la forme `https://console.exemple.fr` | aucune |

La boucle locale (`127.0.0.1`, `localhost`, `[::1]`) est toujours acceptée, et une origine dont l'hôte est accepté l'est aussi. Le port d'un hôte n'est pas comparé. `*` accepte tout hôte et toute origine : à réserver à une API placée derrière un autre contrôle.

Une requête refusée reçoit `403` avec `{"error":"host_not_allowed"}` ou `{"error":"origin_not_allowed"}`. Un réglage mal formé empêche l'application de démarrer.

## Rejeu

L'identifiant d'un événement est l'empreinte SHA-256 de sa forme canonique. Un événement déjà reçu n'est pas enregistré une seconde fois : il est compté dans `duplicates`.

Chaque événement nouveau entre dans la file de traitement, d'où un [worker](workers.md) le projette dans le graphe.

## Limites connues

- Les émetteurs ne sont pas authentifiés : l'API ne s'expose pas hors d'un réseau de confiance.
- Le corps est lu en entier avant d'être contrôlé ; seule la taille du lot est bornée.
- Les identifiants reçus (noms de jeux de données, d'outils, d'agents) ne sont pas encore validés au-delà du schéma.
- L'API n'exporte pas encore de métriques.
