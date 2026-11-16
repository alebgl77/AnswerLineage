# Incidents de données

Un responsable de données constate qu'une donnée est fausse : une table mal chargée, une référence périmée. Il déclare l'incident : quels jeux de données, pendant quelle fenêtre, avec quelle gravité. AnswerLineage enregistre l'incident et lance aussitôt son analyse d'impact : les réponses d'agents qui s'appuient sur ces données pendant la fenêtre, et leurs destinataires.

La déclaration se fait par l'API (`POST /api/v1/incidents`) ou par la [ligne de commande](cli.md#answerlineage-incident) (`answerlineage incident declare`). Les [workers](workers.md#analyse-des-incidents) font l'analyse ; elle se lit sur l'incident (`GET /api/v1/incidents/{id}`, `answerlineage incident show`).

## Le modèle

| Champ | Contenu |
| --- | --- |
| `id` | Identifiant de l'incident, tiré par l'API à la déclaration (UUID) |
| `scope.datasets` | Le périmètre : de 1 à 20 jeux de données, chacun par son espace de noms et son nom, sans doublon |
| `window` | La fenêtre de validité : `from` inclus, `to` exclu, en RFC 3339 avec fuseau ; la fin suit le début, et la fenêtre dure au plus 31 jours |
| `severity` | La gravité, d'une liste fermée ([plus bas](#gravité)) |
| `state` | L'état de l'incident, d'une liste fermée ([plus bas](#états)) |
| `declared_at` | L'heure de la déclaration, posée par la base |
| `analysis` | L'état de l'analyse d'impact et ses décomptes ([plus bas](#lanalyse)) |

Un incident ne porte aucun texte libre : ni titre, ni description, ni commentaire. Ce qu'écrirait un déclarant pourrait finir dans un message de rappel ; AnswerLineage ne produit que des textes écrits d'avance. Le périmètre se dit par des noms, que la déclaration contrôle comme l'[ingestion](ingestion.md#identifiants) : lettres, chiffres et ponctuation des noms de données, sans espace, sans caractère de contrôle ou invisible, sans balise ni mot de passe ; un espace de noms compte au plus 512 caractères, un nom 1 024. Ils sont gardés sous leur forme normalisée NFC : deux écritures d'une même table n'en font qu'une.

Le périmètre ne descend pas encore aux colonnes : la [propagation](lecture.md#propager-un-incident) s'arrête au niveau table, et un incident sur une colonne se déclare sur sa table. Un champ `columns` est refusé comme tout champ inconnu.

### Gravité

La gravité est l'appréciation du responsable de données. Elle ne change pas l'analyse : elle ordonnera les rappels.

| `severity` | Quand l'employer |
| --- | --- |
| `critical` | La donnée fausse a pu fonder une décision engageante : financière, réglementaire, ou envers un client |
| `high` | Elle fausse des chiffres diffusés largement ou repris dans d'autres travaux |
| `medium` | L'écart est limité, ou les réponses qui la citent ont peu circulé |
| `low` | L'écart est sans effet attendu sur une décision |

### États

| `state` | Sens |
| --- | --- |
| `qualified` | Déclaré : son périmètre, sa fenêtre et sa gravité sont établis, son analyse est demandée |

C'est le seul état aujourd'hui. La liste est fermée, dans l'API comme dans la base (`ck_incident_state`) : les états suivants arriveront avec la liste de rappel, chacun par une révision de la base et du contrat.

## Déclarer

```bash
curl -X POST http://127.0.0.1:8000/api/v1/incidents \
  -H 'content-type: application/json' \
  -d '{
        "scope": {"datasets": [{"namespace": "bigquery", "name": "ventes.suivi.commandes"}]},
        "window": {"from": "2026-11-11T06:00:00Z", "to": "2026-11-12T12:00:00Z"},
        "severity": "high"
      }'
```

La réponse `201` est l'incident déclaré, à l'état `qualified`, son analyse en attente :

```json
{
  "id": "974e7c80-6643-4065-a541-e4c029abbdab",
  "state": "qualified",
  "severity": "high",
  "scope": { "datasets": [{ "namespace": "bigquery", "name": "ventes.suivi.commandes" }] },
  "window": { "from": "2026-11-11T06:00:00Z", "to": "2026-11-12T12:00:00Z" },
  "declared_at": "2026-11-16T08:53:16.793908Z",
  "analysis": {
    "state": "pending",
    "requested_at": "2026-11-16T08:53:16.793908Z",
    "attempts": 0,
    "error": null,
    "known_at": null,
    "finished_at": null,
    "counts": null,
    "truncated": null
  }
}
```

L'incident, son périmètre et sa demande d'analyse s'enregistrent dans une seule transaction : une demande n'existe jamais sans son incident, ni un incident sans sa demande.

### Refus

Une déclaration fautive est refusée en entier, rien n'est enregistré. Le refus `422` donne, pour chaque faute, le chemin du champ, un code stable et un motif écrit d'avance ; il ne recopie jamais la valeur reçue, ni le nom d'un champ inconnu.

```json
{ "errors": [{ "path": "/window", "type": "window_inverted", "message": "La fin de la fenêtre doit suivre son début." }] }
```

| `type` | Chemin | Cas |
| --- | --- | --- |
| `window_inverted` | `/window` | La fin de la fenêtre précède son début, ou lui est égale |
| `window_too_long` | `/window` | La fenêtre dure plus de 31 jours |
| `window_out_of_range` | `/window/from`, `/window/to` | Une heure tombe avant 1970 ou après l'an 9999 |
| `value_error` | `/window/from`, `/window/to` | Une heure n'est pas en RFC 3339 avec fuseau |
| `literal_error` | `/severity` | Une gravité hors de la liste |
| `identifier_…` | `/scope/datasets/{rang}/namespace`, `…/name` | Un espace de noms ou un nom qui n'a pas la forme d'un nom : les codes de l'[ingestion](ingestion.md#identifiants) |
| `dataset_duplicate` | `/scope/datasets/{rang}` | Le même jeu de données, après normalisation, figure déjà dans le périmètre |
| `too_many_items` | `/scope/datasets` | Plus de 20 jeux de données ; le motif cite la borne, pas le nombre reçu |
| `too_few_items` | `/scope/datasets` | Un périmètre vide |
| `unknown_field` | l'objet qui le porte | Un champ que le contrat ne connaît pas, à tout niveau |
| `missing`, `string_type`… | le champ | Un champ obligatoire absent, ou d'un autre type |

Le corps est lu borné comme celui de l'ingestion, à 256 Kio : au-delà, `413` (`body_too_large`) ; un corps qui n'est pas du JSON en UTF-8, `400` (`invalid_json`).

## Lire un incident

```bash
curl http://127.0.0.1:8000/api/v1/incidents/974e7c80-6643-4065-a541-e4c029abbdab
```

La réponse reprend l'incident et l'état de son analyse ; quand elle est faite, `result` porte une page des réponses touchées :

```json
{
  "id": "974e7c80-6643-4065-a541-e4c029abbdab",
  "…": "…",
  "analysis": {
    "state": "done",
    "requested_at": "2026-11-16T08:53:16.793908Z",
    "attempts": 0,
    "error": null,
    "known_at": "2026-11-16T08:53:16.814594Z",
    "finished_at": "2026-11-16T08:53:16.849328Z",
    "counts": { "answers": 16, "recipients": 13, "lower_bound": false },
    "truncated": false
  },
  "result": {
    "unknown_datasets": [],
    "coverage": { "status": "measured", "rate": 1.0, "holes": [], "…": "…" },
    "answers": [
      {
        "id": "01a1cf5e5bb078b991a908d485d5acfdb4c70313c6faa097",
        "fingerprint": "sha256:56a61da01004ddc4d94d3508b634a4969d96f8919b06504fa3b342f000de6c24",
        "produced_at": "2026-11-12T10:47:56Z",
        "recipients": [
          "821743431def23772bbc6215495e30c7",
          "a71b66a14ed697845fee1dc093de55a0",
          "d3675030f72fc8499edef4ab380c2c44"
        ]
      },
      "…"
    ],
    "more": false,
    "next_cursor": null
  }
}
```

Cet exemple est le premier incident de l'[entreprise synthétique](corpus.md), déclaré sur la démonstration : l'analyse trouve exactement sa vérité.

| Paramètre | Rôle | Défaut |
| --- | --- | --- |
| `limit` | Réponses touchées par page, de 1 à 200 | 200 |
| `cursor` | La page suivante : la valeur de `result.next_cursor`, rendue telle quelle | aucun |

Un identifiant inconnu répond `404` (`unknown_incident`), un identifiant mal formé `422`, sans être recopié.

## Lister les incidents

`GET /api/v1/incidents` rend les incidents déclarés, les plus récents d'abord, chacun avec son périmètre, sa fenêtre, sa gravité et l'état de son analyse, sans les réponses touchées.

| Paramètre | Rôle | Défaut |
| --- | --- | --- |
| `limit` | Incidents par page, de 1 à 100 ; `more` vaut `true` quand d'autres suivent | 20 |
| `cursor` | La page suivante : la valeur de `next_cursor`, rendue telle quelle | aucun |

Le curseur désigne le dernier incident de la page, par son heure de déclaration et son identifiant : parcourir la liste page par page rend chaque incident une fois et une seule. Comme toute lecture du graphe, ces deux routes s'exécutent sous le [délai maximal des lectures](lecture.md#délai-maximal) : au-delà, `503` (`query_timeout`).

## L'analyse

Un worker réclame la demande d'analyse, lance la [propagation](lecture.md#propager-un-incident) sur chaque jeu de données du périmètre et sur la fenêtre de validité, et enregistre le résultat. Il ne garde que des identifiants : les réponses touchées par leur run et leur empreinte, leurs destinataires par leur pseudonyme.

| `analysis.state` | Sens |
| --- | --- |
| `pending` | En attente d'un worker ; ou remise à plus tard après un échec, `attempts` et `error` le disent |
| `done` | Faite : `counts`, `truncated` et `result` se lisent |
| `failed` | En échec pour de bon, après 8 essais ; `error` dit pourquoi |

| `error` | Sens |
| --- | --- |
| `query_timeout` | Une lecture a dépassé le délai de l'analyse, une minute : la fenêtre ou le périmètre sont trop grands pour le graphe |
| `analysis_error` | Toute autre erreur ; le journal des workers en donne la classe, jamais le message |

### Ce que l'analyse trouve

- **Les réponses touchées** sont celles que la propagation renvoie pour l'un des jeux du périmètre : une réponse dont un run de l'arbre a lu le jeu de données pendant la fenêtre ([ce qui compte comme touché](lecture.md#ce-qui-compte-comme-touché)). Une réponse touchée par deux jeux du périmètre n'est comptée qu'une fois.
- **Les destinataires** sont ceux de chaque réponse touchée, par leur pseudonyme ; `counts.recipients` compte les pseudonymes distincts de tout l'incident.
- **La couverture du lignage** est celle de la fenêtre de l'incident, tous émetteurs confondus, mesurée à l'heure de l'analyse, avec ses trous connus ([couverture du lignage](lecture.md#couverture-du-lignage)).
- **Les jeux inconnus** (`unknown_datasets`) sont ceux du périmètre que le graphe ne connaissait pas à l'heure de l'analyse : aucune lecture n'en était connue, ou leur nom est mal écrit. Une analyse qui en compte ne permet pas de conclure qu'aucune réponse n'est touchée.

### Le graphe tel qu'il était connu

L'analyse lit le graphe tel qu'il était connu à son heure (`known_at`, comme le paramètre des [routes de lecture](lecture.md#le-graphe-tel-que-connu-à-une-date)) : ce qui a été enregistré après n'en fait pas partie. L'heure est celle de la base au premier essai, gardée pour les suivants : un essai repris après un échec, ou une analyse rejouée, lit le même graphe et donne le même résultat.

L'analyse est un instantané. Une réponse enregistrée après elle, parce que son événement est arrivé en retard ou qu'elle a été produite depuis, n'y figure pas : une fenêtre encore ouverte au moment de la déclaration, ou des événements encore dans la file d'ingestion, en sont la cause habituelle. La couverture le signale, par les trous `missing_from_graph` des fenêtres dont les événements ne sont pas encore projetés.

### Bornes

L'analyse a les bornes de la propagation, et les dit comme elle :

| Lecture | Borne |
| --- | --- |
| Par jeu du périmètre | 20 000 lectures dans la fenêtre, 20 000 runs parents |
| Sur tout le périmètre | 200 000 runs réunis |
| Résultat | 100 000 réponses gardées, les plus récentes, et 200 000 liens de diffusion vers leurs destinataires |

Quand une borne est dépassée, `analysis.truncated` et `counts.lower_bound` valent `true` : les décomptes sont des minimums, et une liste vide ne prouve pas qu'aucune réponse n'est touchée. Un rappel ne peut pas s'appuyer sur ce résultat pour conclure qu'il n'y a rien à rappeler : déclarer de nouveau l'incident sur une fenêtre plus courte.

### Rejouer une analyse

Rejouer une analyse remplace son résultat : ni réponse ni destinataire en double. Aucune route ne le demande encore ; un exploitant remet la demande en attente dans la base :

```sql
UPDATE incident_analysis
SET state = 'pending', available_at = now(), attempts = 0, error = NULL, finished_at = NULL,
    answers = NULL, recipients = NULL, truncated = NULL, coverage = NULL, unknown_datasets = NULL
WHERE incident_id = '974e7c80-6643-4065-a541-e4c029abbdab';
```

`known_at` reste : l'analyse rejouée lit le même graphe. Pour la faire porter sur le graphe d'aujourd'hui, mettre aussi `known_at = NULL`.

## Ce qui n'existe pas encore

- **L'authentification.** Ces routes n'authentifient pas leurs appelants, pas plus que le reste de l'API : elles sont faites pour la boucle locale ou un accès déjà contrôlé, et refusent les hôtes et les origines qu'elles n'attendent pas ([réglages](ingestion.md#hôtes-et-origines-attendus)). Les rôles et ce que chacun peut déclarer ou lire viendront avec l'authentification.
- **L'interface.** L'explorateur ne déclare pas encore d'incident ; l'API et la ligne de commande le font.
- **La suite du cycle de vie.** Un incident reste `qualified` : ni modification, ni clôture, ni liste de rappel pour l'instant. Une déclaration fautive ne se corrige pas, elle se refait.
- **Les colonnes.** Le périmètre s'arrête aux tables, comme la propagation.
- **Ce qu'un run touché a écrit.** Comme la propagation, l'analyse ne suit pas une table ou une note dérivée de la donnée fausse.
- **Relancer une analyse par l'API**, par exemple quand des événements arrivent après elle : seule la base le permet ([plus haut](#rejouer-une-analyse)).
