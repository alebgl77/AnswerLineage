# Incidents de données

Un responsable de données constate qu'une donnée est fausse : une table mal chargée, une référence périmée. Il déclare l'incident : quels jeux de données, pendant quelle fenêtre, avec quelle gravité. AnswerLineage enregistre l'incident et lance aussitôt son analyse d'impact : les réponses d'agents qui s'appuient sur ces données pendant la fenêtre, et leurs destinataires.

Un test de données en échec, émis en OpenLineage par dbt ou un autre outil, [propose un incident](#incidents-proposés-par-un-test-de-qualité) sur la table et la colonne qu'il accuse : la proposition attend un humain, rien n'en part avant qu'il la confirme.

La déclaration se fait par l'API (`POST /api/v1/incidents`) ou par la [ligne de commande](cli.md#answerlineage-incident) (`answerlineage incident declare`), avec le rôle de responsable de données, de gestionnaire d'incident ou d'administrateur ; toute personne qui a un rôle lit les incidents ([Authentification et rôles](securite/authentification.md#la-matrice-des-droits)). Les [workers](workers.md#analyse-des-incidents) font l'analyse ; elle se lit sur l'incident (`GET /api/v1/incidents/{id}`, `answerlineage incident show`).

Pendant l'incident, le responsable peut aussi le [mettre en quarantaine](#quarantaine) : chaque proxy apprend alors le périmètre de l'incident, en moins de 30 secondes, pour agir sur les lectures de ces données.

Chaque changement d'état, la déclaration, l'analyse faite, l'analyse en échec, la quarantaine activée, la quarantaine levée, s'annonce aussi par un [webhook signé](webhooks.md) (`incident.declared`, `incident.analysed`, `incident.analysis_failed`, `incident.quarantined`, `incident.quarantine_lifted`) quand les workers en ont un, dans la transaction qui le fait.

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
| `quarantine` | La dernière quarantaine de l'incident, active ou levée, ou `null` ([plus bas](#quarantaine)) |
| `proposal_id` | La [proposition](#incidents-proposés-par-un-test-de-qualité) dont la confirmation a déclaré l'incident, ou `null` pour un incident déclaré à la main |

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
  },
  "quarantine": null
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
          "hmac-sha256:821743431def23772bbc6215495e30c713a83d09547d8e16bad01627950ea076",
          "hmac-sha256:a71b66a14ed697845fee1dc093de55a03ba82be3e176ea2b57e58d874685884d",
          "hmac-sha256:d3675030f72fc8499edef4ab380c2c44c57435cd81e28cadda49905bedbb1f1e"
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
- **Les destinataires** sont ceux de chaque réponse touchée, par leur pseudonyme ; `counts.recipients` compte les pseudonymes distincts de tout l'incident. Un gestionnaire d'incident [révèle](securite/pseudonymes.md#la-révélation) celui qu'il doit prévenir, parmi des identités qu'il apporte ; l'action est journalisée.
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

## Quarantaine

Mettre un incident en quarantaine, c'est dire aux proxys : les données de ce périmètre sont fausses, agissez sur leurs lectures. Chaque proxy apprend le périmètre des quarantaines actives en interrogeant l'API, 10 secondes par défaut, 30 au plus ([synchronisation](proxy.md#périmètres-en-quarantaine)) : moins de 30 secondes après l'activation, tous le connaissent. Aujourd'hui, ils le connaissent sans s'en servir encore : aucune lecture n'est annotée ni bloquée.

Le périmètre est celui de l'incident, ses jeux de données, sans texte libre ; il ne se modifie pas, puisque l'incident ne se modifie pas.

### Activer

```bash
curl -X POST http://127.0.0.1:8000/api/v1/incidents/974e7c80-6643-4065-a541-e4c029abbdab/quarantine \
  -H 'content-type: application/json' -d '{"mode": "warn"}'
```

```json
{
  "incident_id": "974e7c80-6643-4065-a541-e4c029abbdab",
  "changed": true,
  "quarantine": {
    "state": "active",
    "mode": "warn",
    "activated_at": "2026-11-16T08:55:01.204517Z",
    "lifted_at": null
  }
}
```

| `mode` | Ce que les proxys feront des lectures du périmètre |
| --- | --- |
| `warn` | Les signaler. C'est le seul mode aujourd'hui ; la liste est fermée, dans l'API comme dans la base (`ck_incident_quarantine_mode`) |

Le mode est obligatoire : le jour où d'autres modes existeront, aucune quarantaine ne prendra par défaut un mode plus fort que celui que l'on a demandé. Par la ligne de commande : [`answerlineage incident quarantine`](cli.md#answerlineage-incident).

### Lever

```bash
curl -X POST http://127.0.0.1:8000/api/v1/incidents/974e7c80-6643-4065-a541-e4c029abbdab/quarantine/lift
```

La réponse a la même forme, la quarantaine à l'état `lifted` avec l'heure de sa levée. Les proxys cessent de recevoir le périmètre à leur synchronisation suivante.

La levée est une action, `POST …/quarantine/lift`, et non un `DELETE` : la quarantaine levée n'est pas effacée, elle reste dans l'histoire de l'incident, avec ses heures ; et une levée pourra porter un corps, son motif d'une liste fermée, que `DELETE` ne promet pas de transmettre. Elle existe dès maintenant, sans attendre la clôture des incidents : une quarantaine qu'on ne pourrait pas lever deviendrait dangereuse le jour où les proxys agiront.

### Idempotence et états

Activer deux fois, lever deux fois : la seconde demande ne change rien, rend la quarantaine telle qu'elle est (`changed` vaut `false`) et n'annonce rien. Une quarantaine levée ne revient pas : activer de nouveau ouvre une nouvelle quarantaine, avec sa propre heure d'activation.

| `state` | Sens |
| --- | --- |
| `active` | Les proxys reçoivent son périmètre. Au plus une par incident (`uq_incident_quarantine_active`) |
| `lifted` | Levée, définitivement. Elle porte l'heure de sa levée, et elle seule (`ck_incident_quarantine_lifted`), qui suit l'activation (`ck_incident_quarantine_lifted_after`) |

Les transitions forment elles aussi une liste fermée, que la base tient (`ck_incident_quarantine_transition`) : une quarantaine naît active ; seule une quarantaine active se lève, sans changer d'incident, de mode ni d'heure d'activation ; rien d'autre. Les heures sont celles de la base.

### Un objet à part, rattaché à l'incident

La quarantaine n'est pas un état de l'incident (`ck_incident_state`) : c'est un objet à part, une ligne par activation (`incident_quarantine`, [schéma](schema.md#incidents)). Ce qui vient après le demande :

- **elle se croise avec le cycle de vie.** Un incident passera par les étapes de son rappel, et peut être en quarantaine à chacune : en faire un état de l'incident multiplierait ses états (qualifié, qualifié en quarantaine…) ;
- **elle a ses propres attributs** : un mode, aujourd'hui `warn`, demain d'autres, qui n'ont pas de sens pour l'incident ;
- **la levée à la clôture** se fera dans la transaction qui clôt l'incident, en levant sa quarantaine active ;
- **le journal** des activations, avec leur auteur une fois l'authentification en place, leur mode, leur périmètre et leur durée (de `activated_at` à `lifted_at`), est déjà la liste de ces lignes.

`GET /api/v1/incidents/{id}` et la liste des incidents rendent la dernière quarantaine de chaque incident (`quarantine`), active ou levée, ou `null`.

### Refus

| Statut | Code | Cas |
| --- | --- | --- |
| `404` | `unknown_incident` | Aucun incident ne porte cet identifiant ; il n'est pas recopié |
| `409` | `too_many_quarantines` | 100 quarantaines sont déjà actives, tous incidents confondus : en lever une d'abord. La borne tient la liste que reçoivent les proxys dans une seule lecture bornée |
| `409` | `not_quarantined` | Levée d'un incident qui n'a jamais été mis en quarantaine : rien n'est à lever |
| `422` | `literal_error` (`/mode`), `missing`, `unknown_field`, `not_an_object` | Un mode hors de la liste, absent, un champ inconnu ; la valeur reçue n'est jamais recopiée. Un identifiant mal formé est refusé de même |
| `400`, `413` | `invalid_json`, `body_too_large` | Le corps n'est pas du JSON, ou dépasse 4 Kio |

Rien n'est enregistré ni annoncé pour une demande refusée. Une activation prend un verrou sur la table des quarantaines le temps de sa transaction : deux activations simultanées ne dépassent pas ensemble la borne, et les lectures ne l'attendent pas.

### Ce que reçoivent les proxys

`GET /api/v1/quarantine` rend les quarantaines actives, chacune avec le périmètre de son incident, et une version :

```json
{
  "version": "5f0c…",
  "quarantines": [
    {
      "incident_id": "974e7c80-6643-4065-a541-e4c029abbdab",
      "mode": "warn",
      "activated_at": "2026-11-16T08:55:01.204517Z",
      "datasets": [{ "namespace": "bigquery", "name": "ventes.suivi.commandes" }]
    }
  ]
}
```

- **La version** est l'empreinte SHA-256 de la liste : elle ne change que si la liste change. C'est aussi l'`ETag` de la réponse : un proxy la rend dans `If-None-Match` et reçoit `304`, sans corps, tant que rien n'a changé. Une valeur d'`If-None-Match` de plus de 1 024 caractères n'est pas lue, et n'est jamais recopiée.
- **La lecture est bornée** : 100 quarantaines au plus, que l'activation garantit, de 20 jeux chacune, sous le [délai maximal des lectures](lecture.md#délai-maximal).
- **Pas de `known_at`.** La route sert l'état d'aujourd'hui aux proxys ; une quarantaine telle qu'elle était à une date se lit dans les heures d'activation et de levée de l'incident. Le jour où le périmètre comprendra les tables dérivées connues du graphe, la question se posera de nouveau.

Plusieurs proxys l'interrogent chacun de leur côté, sans s'accorder : la route est faite pour cela.

#### Signée et datée

Avec un secret partagé avec les proxys, l'API signe et date chaque réponse de la route, `200` comme `304`, pour qu'un tiers ne puisse ni faire croire à une quarantaine, ni en prolonger une levée. Un proxy qui connaît le secret n'adopte qu'une diffusion signée, et récente ([proxy](proxy.md#une-diffusion-signée-et-datée)).

| Variable | Rôle | Défaut |
| --- | --- | --- |
| `ANSWERLINEAGE_QUARANTINE_SECRET` | Le secret partagé avec les proxys : de l'ASCII imprimable sans espace, de 32 à 512 caractères | aucun : la diffusion n'est pas signée |
| `ANSWERLINEAGE_QUARANTINE_PREVIOUS_SECRET` | Le secret précédent, pendant une rotation : chaque réponse est signée sous les deux | aucun |

```text
AnswerLineage-Signature: t=1794819310,v1=af95e461978721608f0a4d807e0e080afbfb17033c6d2af8fed3cb46905b6369
```

- **`t`** est l'heure de la signature, en secondes depuis 1970 ; **`v1`**, HMAC-SHA-256 en hexadécimal, sous le secret, de l'étiquette `answerlineage/quarantine/v1`, d'un octet nul, de `t`, d'un point, puis de la charge. L'étiquette sépare ce domaine de celui des [webhooks](webhooks.md) : une signature de l'un ne vaut jamais pour l'autre.
- **La charge** d'un `200` est son corps, octet pour octet ; celle d'un `304`, qui n'a pas de corps, est la version courante, celle que le proxy a donnée dans `If-None-Match`. Un `304` ne vaut ainsi que pour cette version.
- **Pendant une rotation**, l'en-tête porte une signature `v1` par secret, le secret puis le précédent : un proxy qui connaît l'un ou l'autre accepte. Régler le nouveau secret à l'API avec l'ancien comme précédent, puis le nouveau chez chaque proxy, puis retirer le précédent de l'API.
- **Un secret refusé** (trop court, trop long, hors de l'ASCII imprimable, un précédent sans secret) arrête l'API au démarrage, avec un message qui nomme la variable, jamais sa valeur. Seule la signature part dans la réponse.

Le format, sa vérification et des vecteurs (réponses valides, rotation, signatures fausses, réponses rejouées hors de la tolérance ou venues du futur) sont publiés dans [`spec/quarantine/v1`](../spec/quarantine/v1/README.md). L'en-tête est décrit dans la spécification OpenAPI.

## Incidents proposés par un test de qualité

Une donnée fausse se voit souvent d'abord dans les tests de données : un `not_null` qui échoue sur la colonne `email` des clients, un `unique` sur un identifiant. Quand l'outil de tests émet ses résultats en OpenLineage, dbt par exemple (dbt-ol), AnswerLineage en fait une **proposition d'incident** : un incident signalé, sur la table et la colonne en échec, qu'un humain examine. Rien ne part d'une proposition : ni analyse, ni annonce, ni quarantaine. L'humain la [confirme](#confirmer-ou-écarter), et l'incident est déclaré comme à la main, ou l'écarte.

### Ce qui est lu

L'outil de tests émet un run par modèle testé ; son entrée est le jeu de données testé, et la facette standard d'OpenLineage `dataQualityAssertions` (version 1-1-0, [copiée dans le dépôt](../spec/openlineage/README.md)) y liste les assertions, chacune avec sa réussite. Les [workers](workers.md#ce-que-la-projection-lit) en lisent quatre champs, et rien d'autre :

| Champ de l'assertion | Ce qu'il devient |
| --- | --- |
| `success` | Un booléen strict ; une assertion dont la réussite n'en est pas un est ignorée. `false` est un échec, quelle que soit la gravité : dbt met `false` aussi pour un test en `warn`, en erreur ou sauté |
| `assertion` | Sa classe, d'une liste fermée : `not_null`, `unique`, `accepted_values`, `relationships`, `row_count`, `freshness`, `custom_sql`, et `other` pour tout le reste, un test singulier par exemple |
| `severity` | `error`, `warn`, ou `unspecified` quand l'émetteur n'en dit rien de connu |
| `column` | La colonne, contrôlée comme un [nom](ingestion.md#identifiants) ; sans colonne, ou quand elle n'a pas la forme d'un nom (une expression comme `lower(email)`), l'échec porte sur la table entière |

Le nom du test, sa description, son corps SQL, les valeurs attendue et observée, ses paramètres ne sont jamais lus : aucun texte libre d'un émetteur n'entre dans la base, le journal, les compteurs ni les réponses de l'API. L'événement brut, archivé tel qu'il a été reçu comme tout événement, les garde. La facette est lue dans `inputFacets`, sinon dans `facets` (dbt écrit les deux) ; mal formée, elle est ignorée, et l'événement est projeté dans le graphe comme les autres : le run de test y lit le modèle qu'il teste. `TestRunFacet` n'est pas lue : elle n'attribue un test ni à un jeu ni à une colonne, et un test qui joint plusieurs modèles les accuserait tous.

**Great Expectations** n'émet pas la facette standard, mais la sienne, `greatExpectations_assertions`, dans les facettes d'entrée de l'action OpenLineage d'un point de contrôle. De chaque assertion, trois champs sont lus : `success` et `column`, comme plus haut, et `expectationType`, ramené à la même liste fermée par une table écrite d'avance (`expect_column_values_to_not_be_null` donne `not_null`, `expect_column_values_to_be_unique` donne `unique`, `expect_column_values_to_be_in_set` donne `accepted_values`, `expect_table_row_count_to_be_between` donne `row_count`, `unexpected_rows_expectation` donne `custom_sql`… et `other` pour le reste). La facette ne dit pas de gravité : `unspecified`. Les deux facettes d'un même jeu partagent sa borne de 1 000 assertions, et une même colonne en échec dans les deux ne fait qu'un signal. Le schéma de cette facette n'est publié que sur une branche, sans version figée : il n'est pas copié dans le dépôt.

### Une proposition par jeu de données

- **Plusieurs assertions d'un même run sur une même colonne** font un seul signal, qui réunit leurs classes et garde la plus forte gravité.
- **Plusieurs colonnes, plusieurs runs sur un même jeu** : une seule proposition ouverte, qui s'enrichit de leurs colonnes, de leurs classes et de leur dernier échec.
- **Plusieurs jeux** : une proposition par jeu.
- **Rejouer** un événement, [reconstruire le graphe](workers.md#reconstruire-le-graphe), rejouer le rebut, ou recevoir le début et la fin d'un run dans le désordre ne propose rien deux fois.

### La proposition

| Champ | Contenu |
| --- | --- |
| `id` | Identifiant de la proposition, tiré par la base |
| `state` | `signaled` : signalée, elle attend un humain et s'enrichit des échecs suivants ; `confirmed` : confirmée, l'incident est déclaré ; `dismissed` : écartée ; `expired` : expirée, sans nouvel échec depuis 7 jours. Une décision est définitive |
| `dataset` | Le jeu de données, par son espace de noms et son nom |
| `columns`, `whole_table` | Les colonnes en échec, nommées, 100 au plus ; `whole_table` vaut `true` quand un échec porte sur la table entière, ou au-delà de 100 colonnes |
| `assertions`, `severity` | Les classes des assertions en échec, et leur plus forte gravité. Ce n'est pas la gravité d'un incident, que seul un humain donne |
| `emitter` | L'émetteur du premier échec : l'espace de noms de son job |
| `first_failed_at`, `last_failed_at` | Le premier et le dernier échec, à l'heure des événements |
| `resolved_at` | Quand toutes les colonnes en échec réussissent de nouveau : la plus récente de leurs réussites. Nulle tant que l'une échoue encore, ou au-delà de 100 colonnes, quand toutes ne sont pas connues |
| `window` | Le début de la fenêtre déduite (`from`) et ce qui le fonde (`basis`, plus bas) |
| `proposed_at`, `decided_at`, `incident_id` | L'heure de la proposition et celle de sa décision ou de son expiration, posées par la base ; l'incident déclaré à la confirmation. Les deux derniers sont nuls tant qu'elle est signalée |
| `counts` | Sur `GET /api/v1/incident-proposals/{id}` seulement : les runs de test en échec qui s'y rattachent (`failed_runs`), comptés jusqu'à 10 000 (`lower_bound`) |

**La fenêtre déduite** commence à la dernière réussite connue des assertions de la colonne avant le premier échec (`basis` vaut `last_pass`) : la donnée était juste alors. Elle remonte au plus 31 jours avant le premier échec. Sans réussite connue avant lui, ou quand la seule connue le suit (des événements reçus dans le désordre), elle commence 7 jours avant le premier échec (`lookback`) : ce repli peut être trop court ou trop long, et `basis` le dit. Elle finit à la fin de l'échec, `resolved_at`, quand elle est connue. Ni la dernière écriture du jeu, lue dans le graphe, ni la valeur observée d'une assertion ne la fondent : la première manquerait des écritures faites sans test, la seconde est du texte libre.

**Colonne et table.** La proposition nomme ses colonnes ; la [propagation](lecture.md#propager-un-incident), elle, s'arrête aux tables : un incident déclaré sur la table rappelle toute réponse qui l'a lue, celles qui ont lu la colonne fausse comprises.

### Lire les propositions

```bash
curl http://127.0.0.1:8000/api/v1/incident-proposals
curl http://127.0.0.1:8000/api/v1/incident-proposals/5b0f6a52-2c1e-4f0e-9d7b-8b4ea1d0c3f2
```

| Paramètre | Rôle | Défaut |
| --- | --- | --- |
| `state` | Seulement les propositions dans cet état | toutes |
| `limit` | Propositions par page, de 1 à 100 ; `more` vaut `true` quand d'autres suivent | 20 |
| `cursor` | La page suivante : la valeur de `next_cursor`, rendue telle quelle | aucun |

Lire les propositions demande le rôle de lecteur, de responsable de données, de gestionnaire d'incident ou d'auditeur ; les confirmer ou les écarter, celui de responsable de données ou de gestionnaire d'incident ([la matrice des droits](securite/authentification.md#la-matrice-des-droits)). La liste rend les plus récentes d'abord ; comme toute lecture du graphe, ces routes s'exécutent sous le [délai maximal des lectures](lecture.md#délai-maximal). Un identifiant inconnu répond `404` (`unknown_proposal`), un identifiant, un état ou un curseur mal formés `422`, sans être recopiés.

### Confirmer ou écarter

```bash
curl -X POST http://127.0.0.1:8000/api/v1/incident-proposals/5b0f6a52-2c1e-4f0e-9d7b-8b4ea1d0c3f2/confirm \
  -H 'content-type: application/json' -d '{"severity": "high"}'
curl -X POST http://127.0.0.1:8000/api/v1/incident-proposals/5b0f6a52-2c1e-4f0e-9d7b-8b4ea1d0c3f2/dismiss
```

**Confirmer** déclare l'incident, dans une seule transaction : la proposition est verrouillée ; l'incident est déclaré sur sa table, à l'état `qualified`, avec la gravité que donne l'humain, une appréciation que le test n'a pas ; son périmètre, sa demande d'analyse et l'annonce `incident.declared`, quand un [webhook](webhooks.md) est en service, s'écrivent comme pour une [déclaration](#déclarer) ; la proposition passe `confirmed`, avec son incident (`incident_id`), et l'incident désigne sa proposition (`proposal_id`). La fenêtre est la fenêtre déduite, de son début à la fin de l'échec, ou à la confirmation si l'échec dure ; le confirmeur peut donner la sienne (`window`), aux règles de la déclaration. Une fenêtre déduite de plus de 31 jours est refusée : la donner.

| Champ | Contenu |
| --- | --- |
| `severity` | La gravité de l'incident, d'une [liste fermée](#gravité) ; obligatoire |
| `window` | Facultative : `from` inclus, `to` exclu, en RFC 3339 avec fuseau, 31 jours au plus |

**Écarter** ne déclare ni n'annonce rien : la proposition passe `dismissed`. Ni l'une ni l'autre ne porte de texte libre, ni motif ni commentaire.

La réponse `200` est la proposition telle qu'elle est désormais, et `changed` : faux quand la proposition était déjà dans l'état demandé. Confirmer deux fois ne déclare qu'un incident ; écarter deux fois ne change rien. Les heures sont celles de la base.

**Après une décision**, les échecs du même jeu pendant 24 heures se rattachent à la proposition décidée, en sourdine, sans en ouvrir une nouvelle : le test qui échoue encore pendant qu'on corrige la donnée ne redemande pas une décision déjà prise. Au-delà, un échec qui dure ouvre une nouvelle proposition : c'est une information.

| Statut | Code | Cas |
| --- | --- | --- |
| `404` | `unknown_proposal` | Aucune proposition ne porte cet identifiant ; il n'est pas recopié |
| `409` | `proposal_dismissed`, `proposal_confirmed`, `proposal_expired` | La proposition est déjà décidée autrement : écartée, elle ne se confirme plus ; confirmée, elle ne s'écarte plus ; expirée, elle ne se décide plus |
| `422` | `literal_error` (`/severity`), `missing`, `unknown_field`, `not_an_object` | Une gravité hors de la liste, absente, un champ inconnu ; la valeur reçue n'est jamais recopiée. Un identifiant mal formé est refusé de même |
| `422` | `window_inverted`, `window_too_long`, `window_out_of_range`, `value_error` (`/window`…) | Une fenêtre donnée fautive, aux codes de la [déclaration](#refus) ; ou une fenêtre déduite de plus de 31 jours (`window_too_long`) |
| `400`, `413`, `415` | `invalid_json`, `body_too_large`, … | Le corps n'est pas du JSON, ou dépasse 4 Kio |

Rien n'est enregistré ni annoncé pour une demande refusée. Une décision prend un verrou sur la table des propositions le temps de sa transaction, comme les workers qui les créent : une décision ne croise ni une création ni une autre décision. Par la ligne de commande : [`answerlineage incident proposals`, `confirm` et `dismiss`](cli.md#answerlineage-incident-proposals).

### Expiration

Une proposition à laquelle aucun nouvel échec ne s'est rattaché depuis 7 jours expire : elle passe `expired`, sans rien déclarer ni annoncer, et ne se décide plus. Un échec qui revient ensuite ouvre une nouvelle proposition. Les workers en font l'entretien chaque minute ; le journal nomme une proposition expirée par son identifiant. Pour qu'une proposition ne s'oublie pas en silence, la [route de santé](cli.md#la-route-de-santé) dit combien attendent et depuis quand la plus ancienne, et `answerlineage doctor` signale celles qui attendent depuis plus de 24 heures (`proposals_waiting`).

Les signaux d'échec reçus il y a plus de 62 jours sont purgés, par lots : un vieil événement rejoué ne fait pas renaître une proposition, puisqu'il est trop ancien pour en proposer. Une proposition garde ce qu'elle a réuni.

### Bornes

Un émetteur authentifié n'est pas encore lié à l'espace de noms de ses événements : il peut envoyer des échecs en nombre, sur n'importe quel jeu. Chaque borne dit ce qu'elle écarte, par un compteur, jamais par une valeur.

| Borne | Au-delà |
| --- | --- |
| 1 000 assertions lues par jeu d'entrée, 10 000 par événement | Les suivantes ne sont pas lues, comptées (`assertions_truncated`) |
| 1 000 colonnes relevées par événement, échecs d'abord | Perdues, comptées (`dropped`) |
| 100 000 signaux en attente d'un worker | Les nouveaux sont perdus, comptés (`dropped`) |
| 100 000 colonnes suivies (dernière réussite, dernier échec) | Seules celles déjà suivies sont mises à jour : la fenêtre d'une colonne nouvelle se replie sur `lookback` |
| 100 propositions signalées à la fois | Le signal est refusé (`refused`), et l'épisode dit une fois au journal des workers, jusqu'à ce qu'une proposition soit décidée ou expire. Un émetteur hostile peut ainsi masquer les vraies : c'est une [acceptation du modèle de menaces](securite/modele-de-menaces.md#acceptations) |
| Un événement reçu ou daté de plus de 31 jours | Il ne propose rien (`stale`) |

## Ce qui n'existe pas encore

- **Une signature asymétrique de la diffusion.** Chaque route exige un jeton et un rôle ([Authentification et rôles](securite/authentification.md)) : déclarer, mettre en quarantaine et lever reviennent au responsable de données, au gestionnaire d'incident et à l'administrateur ; les proxys lisent les périmètres avec le jeton d'un émetteur, et vérifient la [signature](#signée-et-datée) de la diffusion. Mais elle est faite sous un secret partagé : qui le lit sur l'hôte de l'API ou d'un proxy signe comme l'API ([modèle de menaces](securite/modele-de-menaces.md#acceptations)).
- **Ce que la quarantaine fait des lectures.** Les proxys connaissent le périmètre, sans encore annoter ni bloquer une lecture. Le périmètre se limite aux jeux de l'incident, sans les vues ni les tables dérivées connues du graphe.
- **Le journal des quarantaines** : qui a activé ou levé. L'API vérifie l'identité de l'appelant, mais ne la garde pas encore. Les heures, le mode et le périmètre sont déjà gardés.
- **L'interface.** L'explorateur ne déclare pas encore d'incident ; l'API et la ligne de commande le font.
- **La suite du cycle de vie.** Un incident reste `qualified` : ni modification, ni clôture, ni liste de rappel pour l'instant. Une déclaration fautive ne se corrige pas, elle se refait. Sa quarantaine se lève à la main.
- **Les colonnes.** Le périmètre s'arrête aux tables, comme la propagation. Une [proposition](#incidents-proposés-par-un-test-de-qualité) nomme ses colonnes en échec, sans que la propagation s'en serve.
- **Le motif d'un écartement**, d'une liste fermée, et l'écartement en masse des propositions d'un émetteur : une proposition s'écarte une à une, sans motif.
- **Ce qu'un run touché a écrit.** Comme la propagation, l'analyse ne suit pas une table ou une note dérivée de la donnée fausse.
- **Relancer une analyse par l'API**, par exemple quand des événements arrivent après elle : seule la base le permet ([plus haut](#rejouer-une-analyse)).
