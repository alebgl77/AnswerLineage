# API de lecture

L'API de lecture interroge le graphe de lignage. Elle est versionnée sous `/api/v1`, décrite par la même spécification OpenAPI que l'API d'ingestion, et ne renvoie jamais de contenu : des noms, des empreintes, des heures.

| Route | Réponse |
| --- | --- |
| `GET /api/v1/answers?channel=…&message_ref=…` | Les réponses d'agents diffusées par un message |
| `GET /api/v1/answers/{id}/sources` | Les sources d'une réponse : jeux de données, versions, outils, agents, et la couverture du lignage sur la période de son run |
| `GET /api/v1/impact?namespace=…&name=…&from=…&to=…` | Les réponses et les destinataires touchés par un jeu de données faux pendant une fenêtre |
| `GET /api/v1/graph` | Le graphe en cinq colonnes des réponses les plus récentes, ou d'une réponse |
| `GET /api/v1/datasets` | Les jeux de données connus du graphe |
| `GET /api/v1/exposure` | Les jeux de données et leurs colonnes, les plus exposés d'abord : un score selon les réponses qui en dépendent et leurs destinataires |
| `GET /api/v1/coverage?from=…&to=…` | La couverture du lignage de chaque émetteur sur une période |
| `GET /api/v1/incidents` | Les incidents déclarés, les plus récents d'abord, et l'état de leur analyse |
| `GET /api/v1/incidents/{id}` | Un incident déclaré, l'état de son analyse d'impact et, quand elle est faite, les réponses touchées et leurs destinataires |

Les six premières routes lisent aussi le graphe tel qu'il était connu à une date, avec `known_at` ([plus bas](#le-graphe-tel-que-connu-à-une-date)). Les incidents se déclarent par `POST /api/v1/incidents` ; leurs deux routes de lecture sont décrites avec eux, dans [Incidents de données](incidents.md).

Depuis le terminal, `answerlineage impact` propage un incident et dit par son code de sortie si l'on peut conclure, `answerlineage exposure` classe les jeux de données par exposition, `answerlineage incident` déclare un incident et lit son analyse, et `answerlineage doctor` lit l'état de l'installation sur `GET /api/v1/health` : voir la [ligne de commande](cli.md).

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
  "period": { "from": "2026-11-11T09:17:51Z", "to": "2026-11-11T09:18:37Z" },
  "coverage": { "status": "measured", "rate": 1.0, "holes": [], "…": "…" },
  "truncated": false
}
```

Ces exemples viennent de l'[entreprise synthétique](corpus.md) fournie avec le dépôt, avec sa graine par défaut, chargée avec ses relevés de couverture.

### Ce qui compte comme source

Les sources d'une réponse sont les lectures de son run et de tous ses runs enfants : appels d'outils, sous-agents, à toute profondeur. Le run parent et les runs voisins n'en font pas partie.

Un jeu de données lu plusieurs fois n'apparaît qu'une fois :

- `versions` réunit les versions lues, quand l'émetteur les a déclarées ;
- `precision` garde la lecture la moins précise. Un jeu lu en entier une seule fois est une source en entier : `precision` vaut `table` et `columns` est vide ;
- `columns` réunit les colonnes lues quand toutes les lectures les donnent ;
- `first_read_at` et `last_read_at` sont la première et la dernière heure d'événement de ces lectures.

### Ce que la remontée peut manquer

Une lecture dont l'appel n'a pas été capté n'apparaît pas dans les sources. La remontée dit donc sa [couverture du lignage](#couverture-du-lignage), sous la même forme et du même calcul que la propagation :

| Champ | Contenu |
| --- | --- |
| `period` | La période du run de la réponse : de son premier instant connu à son dernier, à la seconde. Elle réunit les heures de son run et de ses runs enfants, de leurs lectures, et celle de la réponse ; la fin est exclue, la dernière seconde est donc comprise |
| `coverage` | La couverture sur cette période, par les émetteurs qui ont vu le run ou l'un de ses runs enfants : son état, son taux, les appels vus et les trous connus qui recoupent la période |

Un émetteur a vu le run quand il a émis l'un de ses événements. Les relevés des autres n'y comptent pas : le silence d'un serveur que la réponse n'a pas appelé ne la concerne pas. Quand aucun des émetteurs qui l'ont vue n'a publié de relevé, la couverture n'est pas mesurée (`unknown`).

### Identifiant d'une réponse

L'identifiant d'une réponse se déduit de son run et de son empreinte. Il reste le même si le graphe est reconstruit à partir des événements, et peut donc être conservé dans un ticket ou un lien.

### Bornes et erreurs

| Statut | Cas |
| --- | --- |
| `200` | Recherche faite ; la liste est vide quand aucun message ne correspond |
| `404` | Aucune réponse ne porte cet identifiant (`unknown_answer`) |
| `422` | Un paramètre est absent, trop long ou mal formé |
| `503` | La lecture a dépassé son délai maximal (`query_timeout`) : voir [Bornes et délais](#bornes-et-délais) |

Une recherche renvoie 20 réponses au plus, 100 avec `limit`. L'arbre des runs parcouru pour une réponse est borné à 10 000 runs, et ses lectures à 50 000 : au-delà, `truncated` vaut `true` et les sources sont partielles.

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
  "counts": { "answers": 16, "recipients": 13, "lower_bound": false },
  "coverage": { "status": "measured", "rate": 1.0, "holes": [], "…": "…" },
  "graph": { "answers": ["…"], "recipients": ["…"], "…": "…" },
  "truncated": false
}
```

Cet exemple est le premier incident de l'[entreprise synthétique](corpus.md) : sa vérité annonce les mêmes 16 réponses et 13 destinataires.

| Champ | Contenu |
| --- | --- |
| `counts` | Réponses touchées et destinataires distincts, sur tout l'incident. `lower_bound` vaut `true` quand le résultat est tronqué : ce sont alors des minimums |
| `graph` | Les réponses touchées, les plus récentes d'abord, sous la forme du [graphe en cinq colonnes](#le-graphe-en-cinq-colonnes) : `graph.answers` porte chaque réponse et sa chaîne, `graph.recipients` les pseudonymes des destinataires |
| `coverage` | La [couverture du lignage](#couverture-du-lignage) sur la fenêtre de l'incident, tous émetteurs confondus : son état, son taux, les appels vus et les trous connus qui recoupent la fenêtre. Une réponse dont l'appel n'a pas été capté ne peut pas être trouvée : hors d'une couverture mesurée et sans trou, une liste vide ne prouve pas que rien n'est touché |
| `truncated` | `true` quand la propagation dépasse une de ses bornes : le résultat est partiel ([Bornes et délais](#bornes-et-délais)) |

### Ce qui compte comme touché

Une réponse est touchée quand un run de son arbre a lu le jeu de données pendant la fenêtre : le run de l'agent lui-même, un appel d'outil, un sous-agent à toute profondeur.

- **C'est l'heure de la lecture qui compte.** Une réponse rendue après la correction, mais fondée sur une lecture faite avant, est touchée. Une lecture faite après la correction ne l'est pas.
- **Une lecture a une première et une dernière heure.** Plusieurs événements d'un même run peuvent la déclarer, à des heures différentes : elle est touchée dès que cet intervalle recoupe la fenêtre. Un événement rejoué avec une heure antérieure élargit l'intervalle et ne fait sortir aucune lecture de la fenêtre ([schéma](schema.md#intervalle-dune-lecture)).
- **La fenêtre inclut son début et exclut sa fin.**
- **Un run qui a lu sans répondre n'a rien à rappeler.**

`limit` (200 au plus) et `cursor` parcourent la liste des réponses comme pour le graphe ; `counts` reste celui de tout l'incident. Un jeu de données inconnu répond `404` (`unknown_dataset`), une fenêtre vide `422`.

### Déclarer l'incident

`GET /api/v1/impact` répond à une question posée sur un jeu de données, et ne garde rien. Un incident déclaré par `POST /api/v1/incidents` nomme son périmètre, un jeu de données ou plusieurs, sa fenêtre de validité et sa gravité ; les workers en font l'analyse : la même propagation, sur chaque jeu du périmètre, sur le graphe tel qu'il était connu à l'heure de l'analyse. Le résultat reste avec l'incident : les réponses touchées, leurs destinataires pseudonymisés, la couverture de la fenêtre et ses trous, et si une borne a été dépassée. Voir [Incidents de données](incidents.md).

### Mesure

La propagation est mesurée sur un [corpus de vérité](corpus.md#mesure-du-moteur-dimpact), comme un modèle : rappel et précision, scénario par scénario puis en tout. À chaque merge request, le rappel au niveau table sur l'ensemble du corpus doit atteindre 0,99, et aucune réponse hors d'atteinte ne doit être renvoyée ; sinon le pipeline échoue et nomme les réponses manquées. `make measure` rejoue la mesure et imprime son tableau. Sur les incidents de l'entreprise synthétique, le résultat est exactement la vérité.

## Score d'exposition

Quelles données faudrait-il surveiller d'abord, parce qu'une erreur y toucherait le plus de réponses et de personnes ? Le score d'exposition le dit pour chaque jeu de données et chaque colonne que les réponses lisent, selon les réponses qui en dépendent et leurs destinataires.

```bash
curl "http://127.0.0.1:8000/api/v1/exposure?at=2026-11-01T12:00:00Z"
```

```json
{
  "at": "2026-11-01T12:00:00Z",
  "datasets": [
    {
      "namespace": "bigquery",
      "name": "finance.ref.taux_change",
      "kind": "table",
      "score": 10.192078,
      "answers": 5,
      "recipients": 6,
      "columns": [
        { "name": "date_valeur", "score": 6.423125, "answers": 2 },
        { "name": "devise", "score": 5.315637, "answers": 4 },
        { "name": "taux", "score": 5.315637, "answers": 4 }
      ],
      "more_columns": false
    },
    {
      "namespace": "bigquery",
      "name": "finance.compta.factures",
      "kind": "table",
      "score": 4.380814,
      "answers": 2,
      "recipients": 3,
      "columns": [
        { "name": "montant", "score": 2.604141, "answers": 1 },
        { "name": "client_id", "score": 1.776673, "answers": 1 }
      ],
      "more_columns": false
    }
  ],
  "more": false,
  "next_cursor": null,
  "truncated": false
}
```

Cet exemple est le scénario `exposition-colonne-unique` du [corpus de vérité](corpus.md#corpus-de-vérité) : la date de valeur, lue par une seule réponse récente envoyée à quatre personnes, est plus exposée que la devise et le taux, lus par trois réponses de vingt jours.

### La formule

Chaque réponse qui dépend d'un jeu de données y pèse 1, plus un par destinataire distinct, et ce poids diminue de moitié tous les 30 jours depuis l'heure de la réponse :

```text
poids d'une réponse = 2^(−âge / 30 jours) × (1 + destinataires distincts)
score               = somme des poids des réponses qui en dépendent
```

À 30 jours, une réponse pèse la moitié d'une réponse de l'instant ; à 60 jours, le quart ; au-delà de deux ans environ, elle ne compte plus au millionième. Une réponse sans destinataire connu pèse 1 : diffusée dans un canal ou gardée, elle existe encore. Un destinataire qui a reçu deux réponses compte deux fois : il y a deux réponses à lui rappeler. Le même destinataire touché par deux canaux pour une même réponse ne compte qu'une fois.

La formule est faite pour être expliquée plutôt que fine : le score ne fait que croître avec une réponse ou un destinataire de plus, une réponse récente y pèse plus qu'une ancienne, et `answers` et `recipients` donnent les décomptes dont il est fait. Avancer l'heure de référence divise tous les scores par le même facteur : le temps seul ne change pas l'ordre, seules de nouvelles réponses le changent (à l'arrondi près, et hors réponses postérieures à l'heure de référence).

### Ce qui compte comme dépendance

- **Les sources d'une réponse**, comme pour la [remontée](#ce-qui-compte-comme-source) : les lectures de son run et de tous ses runs enfants, à toute profondeur, quelle que soit leur heure. Un run qui a lu sans répondre n'expose rien.
- **Une colonne** dépend des réponses dont une lecture la nomme, et de celles qui ont lu la table entière : une colonne fausse touche aussi ceux qui ont tout lu. C'est le côté sûr, celui de la [propagation](#propager-un-incident). Les colonnes d'un jeu sont celles qu'une lecture a nommées : un jeu toujours lu en entier n'en montre aucune. Une colonne ne pèse donc jamais plus que son jeu de données.
- **Pas de transitivité** : une réponse fondée sur une note ou une table dérivée d'un jeu n'en dépend pas, comme pour la propagation ([limites](#limites-connues)).
- Seuls les jeux dont au moins une réponse dépend sont classés ; un jeu absent du classement a un score nul.

### Une heure de référence, un même score

Le score se calcule à la lecture, à une heure de référence : l'âge de chaque réponse se compte jusqu'à elle. Par défaut, c'est la date de `known_at` quand elle est donnée (le score tel qu'on l'aurait lu ce jour-là), sinon l'heure de la base. Le résultat la rappelle dans `at`, en UTC. Une réponse postérieure à l'heure de référence pèse comme une réponse de cet instant.

Le calcul est déterministe : la même base et la même heure de référence donnent le même score, octet pour octet. Le poids de chaque réponse se compte en billionièmes entiers ; leur somme est donc exacte et ne dépend pas de l'ordre des réponses. Le score s'arrondit ensuite au millionième, la moitié vers le haut. Une réponse d'exactement 30 jours pèse exactement la moitié d'une réponse de l'instant, sans arrondi.

Les jeux de données vont du plus exposé au moins exposé, au millionième ; à égalité, par espace de noms puis par nom. Les colonnes d'un jeu de même, à égalité par leur nom.

| Champ | Contenu |
| --- | --- |
| `at` | L'heure de référence retenue, en UTC |
| `datasets[].score` | Le score du jeu de données, au millionième |
| `datasets[].answers` | Les réponses qui en dépendent |
| `datasets[].recipients` | Les destinataires distincts de ces réponses, tous canaux confondus |
| `datasets[].columns` | Les colonnes nommées par une lecture, les plus exposées d'abord, 100 au plus : `name`, `score` et `answers` |
| `datasets[].more_columns` | `true` quand d'autres colonnes, moins exposées, suivent les 100 rendues |
| `more`, `next_cursor` | Des jeux moins exposés suivent ; le curseur de la page suivante |
| `truncated` | `true` quand une lecture du graphe dépasse sa borne : les scores sont des minimums, et l'ordre peut différer de celui du graphe entier |

| Paramètre | Rôle | Défaut |
| --- | --- | --- |
| `limit` | Jeux de données par page, de 1 à 200 | 50 |
| `cursor` | La page suivante : la valeur de `next_cursor`, rendue telle quelle | aucun |
| `at` | Heure de référence (RFC 3339, avec fuseau), du 1er janvier 1970 à l'an 9999 | `known_at`, sinon l'heure de la base |
| `known_at` | Lit le graphe tel qu'il était connu à cette date ([plus bas](#le-graphe-tel-que-connu-à-une-date)) | aucune |

Le curseur garde l'heure de référence de la première page : les suivantes se calculent à la même heure, et `at`, s'il est redonné, doit être le même. `known_at` se redonne à chaque page, comme pour les autres routes. Un curseur mal formé est refusé en `422`, comme un curseur d'une autre heure de référence ou qui ne désigne aucun jeu du classement (`cursor_invalid`), et une heure de référence sans fuseau ou hors de ses bornes (`at_out_of_range`). Le refus donne le chemin du paramètre, jamais la valeur reçue. Sans `known_at`, une réponse reçue entre deux pages peut changer les scores : un jeu peut alors manquer ou revenir.

`answerlineage exposure` lit la même route depuis le terminal ([ligne de commande](cli.md#answerlineage-exposure)).

### Mesure de l'ordre

L'ordre d'exposition se mesure sur le [corpus de vérité](corpus.md#lordre-dexposition) : trois scénarios au moins écrivent à la main l'ordre attendu de leurs jeux de données et de leurs colonnes, à une heure de référence. `tests/test_truth_corpus_exposure.py` charge chacun par le chemin réel, dans sa propre base, et compare le classement de l'API à cet ordre.

## Couverture du lignage

« Aucune réponse touchée » ne vaut que si l'on sait quelle part des appels a été vue. La couverture du lignage est la part des appels relayés par les émetteurs qui sont présents dans le graphe : les compteurs que chaque émetteur publie dans ses [relevés](ingestion.md#relevés-de-couverture), comparés au graphe.

```bash
curl "http://127.0.0.1:8000/api/v1/coverage?from=2026-11-11T09:00:00Z&to=2026-11-11T09:01:00Z"
```

```json
{
  "window": { "from": "2026-11-11T09:00:00Z", "to": "2026-11-11T09:01:00Z" },
  "emitters": [
    {
      "emitter": "mcp://entrepot",
      "last_report_at": "2026-11-11T09:01:02Z",
      "muted_since": null,
      "coverage": {
        "status": "measured",
        "rate": 0.99,
        "calls_seen": 1000,
        "events_in_graph": 990,
        "events_lost": 10,
        "uninspected": 0,
        "instances": 2,
        "holes": [
          {
            "emitter": "mcp://entrepot",
            "kind": "events_lost",
            "window": { "from": "2026-11-11T09:00:00Z", "to": "2026-11-11T09:01:00Z" },
            "count": 10
          },
          {
            "emitter": "mcp://entrepot",
            "kind": "missing_from_graph",
            "window": { "from": "2026-11-11T09:00:00Z", "to": "2026-11-11T09:01:00Z" },
            "count": 10
          }
        ],
        "truncated": false
      }
    }
  ],
  "more": false
}
```

### Le taux

Pour chaque fenêtre d'un relevé, on compte les événements d'appel d'outil de l'émetteur (ceux qui portent la facette `answerlineage_toolCall`) reçus et déjà projetés par les workers, dont l'heure tombe dans la fenêtre. Ce nombre, plafonné aux appels vus de la fenêtre, est rapporté aux appels que l'émetteur déclare avoir vus. Le taux réunit les fenêtres : `events_in_graph / calls_seen`, de 0 à 1, nul quand aucun appel n'a été vu.

- **Le graphe fait foi, pas l'émetteur.** Un événement perdu en route se voit, même si l'émetteur croit l'avoir émis : 1 000 appels vus, 1 000 émis, 990 arrivés donnent 99 %.
- **Des événements, pas des runs.** Deux appels peuvent partager un run : chacun a son événement.
- **Une fenêtre ne comble pas une autre.** Des événements en excès dans une fenêtre, d'un émetteur à l'horloge décalée par exemple, ne compensent pas un manque ailleurs.
- **Un événement reçu mais pas encore projeté** n'est pas dans le graphe : juste après un envoi, la dernière fenêtre peut paraître incomplète le temps qu'un worker passe.
- **Un événement encore en route** non plus : pendant une panne de l'API, le relevé d'une fenêtre close peut précéder ses événements, qui attendent dans la [boîte d'envoi](proxy.md#boîte-denvoi) du proxy. La fenêtre montre des événements manquants jusqu'à leur arrivée ; ceux que la boîte perd y deviennent des événements perdus, par un relevé corrigé.
- Un relevé qui déborde de la période compte en entier : la mesure se fait à la fenêtre près.

### Instances

Un émetteur peut publier par plusieurs instances : des proxys placés devant un même serveur partagent l'émetteur, chacun publie sous son instance ([relevés](ingestion.md#fusion)). La mesure de l'émetteur les additionne :

- **Des fenêtres qui se recoupent ne font qu'une.** Les relevés de ses instances dont les fenêtres se recoupent forment une seule fenêtre de mesure, de la plus tôt à la plus tard, dont chaque compteur est la somme ; ses événements dans le graphe sont comptés sur toute cette fenêtre. Deux instances alignées sur les mêmes fenêtres s'additionnent fenêtre par fenêtre ; une instance qui démarre en cours de fenêtre, ou réglée sur une autre durée, élargit la fenêtre commune, et ses trous se disent à cette échelle.
- **Le silence reste celui de l'émetteur.** Un intervalle n'est muet que si aucune instance ne l'a couvert : une instance arrêtée par une mise à l'échelle n'ouvre pas de trou tant qu'une autre publie.
- **Un relevé sans instance** compte pour une instance à part.
- `instances` dit combien d'instances ont publié un relevé recoupant la période ; pour le résultat d'impact et les sources d'une réponse, la somme sur les émetteurs pris en compte.

### Trois états

| `status` | Sens |
| --- | --- |
| `measured` | Des relevés couvrent toute la période. Le taux et les trous disent ce qui a été capté |
| `partial` | Des relevés ne couvrent qu'une part de la période : les trous `silence` et `before_first_report` disent laquelle manque |
| `unknown` | Aucun relevé ne recoupe la période : la couverture n'est pas mesurée. Le résultat peut ignorer des réponses dont l'appel n'a pas été capté |

Une période sans relevé n'est jamais présentée comme couverte. La partie d'une période qui n'est pas encore passée n'est pas mesurable : elle n'est ni couverte ni un trou. Pour le résultat d'impact, les émetteurs pris en compte sont ceux qui ont publié un relevé commençant avant la fin de la fenêtre : un émetteur apparu après l'incident n'y compte pas, un émetteur muet pendant l'incident le rend partiel. Pour la remontée d'une réponse, ce sont, parmi eux, ceux qui ont vu son run ([ce que la remontée peut manquer](#ce-que-la-remontée-peut-manquer)).

### Lire un trou

Chaque trou nomme son émetteur, sa nature, sa fenêtre, et un nombre quand il y en a un. Les trous d'une même nature qui se suivent sont réunis.

| `kind` | Ce que le trou dit | `count` |
| --- | --- | --- |
| `events_lost` | L'émetteur déclare avoir perdu des événements dans cette fenêtre : c'est la fenêtre de la perte | Événements perdus |
| `missing_from_graph` | Le graphe contient moins d'événements que d'appels vus, que l'émetteur ait vu la perte ou non | Événements manquants |
| `uninspected` | L'émetteur a relayé des requêtes sans pouvoir les inspecter. Elles cachaient peut-être des appels, que le serveur a exécutés sans qu'aucun événement n'en sorte : on ne sait pas combien, le taux ne peut donc pas les compter, mais le trou est dit | Requêtes non inspectées |
| `silence` | Aucun relevé, entre deux relevés d'un émetteur connu ou après son dernier : l'émetteur était arrêté, muet, ou ses relevés se sont perdus | nul |
| `before_first_report` | La période commence avant le premier relevé de l'émetteur : rien n'est su de cette partie | nul |

Une réponse qui s'appuie sur une lecture faite pendant un trou peut manquer au résultat d'impact. Pour un rappel, un trou dans la fenêtre d'un incident appelle une vérification à la source de l'émetteur nommé.

### Émetteurs

`GET /api/v1/coverage` rend chaque émetteur connu, c'est-à-dire qui a publié au moins un relevé, par identifiant, avec :

| Champ | Contenu |
| --- | --- |
| `last_report_at` | Réception du dernier relevé qui a changé quelque chose |
| `muted_since` | Quand le [contrôle des émetteurs muets](workers.md#émetteurs-muets) l'a signalé : l'heure de son dernier relevé. Nul dès qu'il republie |
| `coverage` | Sa couverture sur la période, sous la même forme que dans le résultat d'impact et dans les sources d'une réponse |

| Paramètre | Rôle | Défaut |
| --- | --- | --- |
| `from`, `to` | La période, début inclus, fin exclue (RFC 3339, avec fuseau) ; une période vide répond `422` | obligatoires |
| `limit` | Nombre d'émetteurs, de 1 à 500 ; `more` vaut `true` quand d'autres suivent | 100 |

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
| `next_cursor` | Le curseur de la page suivante, `null` quand il n'y en a plus |
| `truncated` | `true` quand les chaînes des réponses dépassent une borne : la vue est partielle |

Les liens d'une colonne à l'autre se déduisent de la chaîne de chaque réponse. Suivre une donnée jusqu'aux personnes revient à garder les réponses dont `reads` la cite.

| Paramètre | Rôle | Défaut |
| --- | --- | --- |
| `limit` | Nombre de réponses dans la vue, 200 au plus | 60 |
| `cursor` | Reprendre après la dernière réponse d'une page : la valeur de `next_cursor`, rendue telle quelle | aucun |
| `answer` | Identifiant d'une réponse : la vue se réduit à sa chaîne, `404` si elle est inconnue | aucun |

`GET /api/v1/datasets` liste les jeux de données connus du graphe, par espace de noms puis par nom, 200 par défaut et 500 au plus avec `limit`.

L'`id` d'une réponse est son identifiant stable. Celui des autres nœuds ne vaut que dans la réponse qui le donne : un jeu de données se désigne par son espace de noms et son nom.

## Le graphe tel que connu à une date

Un auditeur veut savoir ce que l'on savait au moment d'une décision : quelles réponses un rappel décidé mardi pouvait-il trouver ? Chaque ligne du graphe porte son heure d'enregistrement, la réception de l'événement qui l'a fait connaître ([schéma](schema.md#deux-heures)). Avec `known_at`, une route lit le graphe tel qu'il était à cette date : ce qui a été enregistré après en est exclu.

```bash
curl "http://127.0.0.1:8000/api/v1/impact?namespace=bigquery&name=ventes.suivi.commandes&from=2026-11-11T06:00:00Z&to=2026-11-12T12:00:00Z&known_at=2026-11-12T08:00:00Z"
```

| Route | Avec `known_at` |
| --- | --- |
| `GET /api/v1/impact` | La propagation sur les lectures, les runs, les réponses et les diffusions enregistrés à cette date |
| `GET /api/v1/answers/{id}/sources` | Les sources connues à cette date ; `404` (`unknown_answer`) pour une réponse enregistrée après |
| `GET /api/v1/graph` | Les réponses connues à cette date et leurs chaînes ; avec `answer`, `404` pour une réponse enregistrée après |
| `GET /api/v1/answers` | Les réponses que le message portait à cette date |
| `GET /api/v1/datasets` | Les jeux de données connus à cette date |
| `GET /api/v1/exposure` | Le score des réponses et des lectures enregistrées à cette date ; l'heure de référence est cette date, sauf si `at` en donne une autre |

Le résultat rappelle la date retenue dans son champ `known_at`, tel qu'il a été demandé ; le graphe d'un résultat d'impact le rappelle aussi. Sans le paramètre, rien ne change : la lecture porte sur le graphe tel qu'il est, et le champ est absent. Pour parcourir les pages, `cursor` se rend avec la même date.

| Paramètre | Rôle | Défaut |
| --- | --- | --- |
| `known_at` | Date et heure, avec fuseau (RFC 3339), du 1er janvier 1970 à l'an 9999 | aucun |

Une date sans fuseau ou mal formée est refusée en `422` ; une date hors de ces bornes aussi, avec le code `known_at_out_of_range`. Le refus donne le chemin du paramètre (`/query/known_at`), jamais la valeur reçue.

Ce que la date ne remonte pas :

- **Les fusions.** Une ligne connue à la date porte ce qu'elle porte aujourd'hui : une lecture dont un événement reçu plus tard a élargi l'intervalle, ajouté des colonnes ou affiné la précision, apparaît avec ces valeurs-là. La date dit quelles lignes existaient, pas comment elles étaient remplies.
- **La couverture.** Celle d'un résultat d'impact est la couverture d'aujourd'hui : les relevés d'un émetteur se fusionnent au plus grand, et leur état passé n'est pas gardé. `GET /api/v1/coverage` ne prend pas `known_at`.

## Bornes et délais

Une lecture du graphe ne peut ni épuiser le service, ni rendre un résultat incomplet sans le dire.

### Délai maximal

Chaque route de lecture, celles de ce document comme le [compteur de doublons](ingestion.md#compteur-de-doublons) ou la [file de rebut](ingestion.md#file-de-rebut), s'exécute sous un délai maximal, 5 secondes par défaut. Le délai court dès l'arrivée de la requête ; avant chacune de ses requêtes SQL, PostgreSQL reçoit le temps qui en reste (`statement_timeout`, posé pour la transaction seulement). Une requête qui le dépasse est annulée dans la base elle-même, et la route répond :

```json
{ "error": "query_timeout" }
```

avec le statut `503`. Le refus ne dit rien de la requête ni des valeurs reçues. La connexion revient au pool sans garder le délai.

Une lecture bornée répond en quelques dizaines de millisecondes : 5 secondes laissent la marge d'un cache froid, rendent vite la connexion au pool, et restent sous le délai des relais HTTP usuels (30 à 60 secondes), pour que l'appelant reçoive le motif plutôt qu'une coupure. Le délai se règle par `ANSWERLINEAGE_READ_TIMEOUT_MS`, en millisecondes, de 1 à 600 000 ; une autre valeur empêche l'API de démarrer.

### Bornes

Toute requête porte une borne sur les lignes qu'elle lit. Un dépassement se dit : `truncated` vaut `true`.

| Lecture | Borne |
| --- | --- |
| Sources d'une réponse | 10 000 runs dans son arbre, 50 000 lectures |
| Graphe en cinq colonnes | 200 réponses par page ; pour leurs chaînes, 20 000 runs, 50 000 couples run et jeu de données lu, 50 000 liens de diffusion |
| Propagation | 20 000 lectures du jeu de données dans la fenêtre, 20 000 runs parents, 100 000 réponses et 200 000 liens de diffusion comptés |
| Jeux de données connus | 500 par page |
| Score d'exposition | 200 jeux de données par page, 100 colonnes rendues par jeu ; 20 000 réponses, les plus récentes d'abord, 100 000 liens vers leurs destinataires, 50 000 runs dans leurs arbres, 50 000 couples run et jeu de données lu, 100 000 colonnes nommées par ces lectures |
| Incidents | 100 par page ; pour un incident, 200 réponses touchées par page et 200 000 liens vers leurs destinataires |
| File de rebut | 500 événements par page ; décomptes par motif sur 100 000 événements au plus ; un rejeu remet en file 10 000 événements au plus |
| Couverture | 1 000 émetteurs pour un résultat d'impact ou une remontée, 500 par page ; 50 000 relevés lus ; 1 000 trous rendus ; dans chaque fenêtre, les événements comptés au plus jusqu'aux appels vus |

Une couverture tronquée (`coverage.truncated`) n'est jamais donnée pour `measured` : ses trous peuvent être incomplets. Réduire la période la rend complète.

### Lire un résultat d'impact tronqué

Quand `truncated` vaut `true`, la propagation n'a pas tout parcouru :

- d'autres réponses peuvent être touchées que celles renvoyées ;
- `counts.lower_bound` vaut `true` : les décomptes sont des **minimums** ;
- une liste vide ne veut **pas** dire qu'aucune réponse n'est touchée. Un rappel ne peut pas s'appuyer sur ce résultat pour conclure qu'il n'y a rien à rappeler : réduire la fenêtre de l'incident et relancer la propagation.

L'ordre du parcours fait passer d'abord ce qui mène aux réponses : les lectures les plus récentes de la fenêtre d'abord, par leur première heure, sans compter celles faites par un run sans parent ni réponse, qui ne touchent rien ; la remontée aux runs parents a sa propre borne, que les lecteurs ne peuvent pas épuiser, et va d'abord aux runs les plus proches des lectures.

### Pagination

Les réponses vont des plus récentes aux plus anciennes. Plusieurs réponses peuvent porter la même heure : elles se départagent par leur identifiant stable. Chaque page donne `next_cursor`, à rendre tel quel dans `cursor` pour la page suivante ; il vaut `null` à la dernière page. Le curseur est opaque : il désigne la dernière réponse de la page, et la page suivante commence juste après elle. Parcourir un graphe page par page rend ainsi chaque réponse une fois et une seule, quelle que soit la taille des pages. Un curseur mal formé, ou qui ne désigne aucun instant, est refusé en `422` sans être recopié.

## Limites connues

- L'API n'authentifie pas ses appelants : elle est faite, à ce stade, pour écouter sur la boucle locale ou derrière un contrôle d'accès. Elle refuse déjà les requêtes dont l'hôte ou l'origine n'est pas attendu ([réglages](ingestion.md#hôtes-et-origines-attendus)).
- Un appel d'outil n'est rattaché à une réponse que si son événement désigne le run de l'agent comme parent.
- La propagation s'arrête au niveau table : elle renvoie tout lecteur de la fenêtre, y compris ceux qui n'ont lu que des colonnes, des lignes ou une version saines.
- Elle ne suit pas encore ce qu'un run touché a écrit : une réponse fondée sur une note ou une table dérivée de la donnée fausse n'est pas renvoyée.
- Le score d'exposition suit les mêmes dépendances : ni ce qu'un run a écrit, ni ce qu'un appel non capté a lu. Au-delà de 20 000 réponses, les plus anciennes, qui pèsent le moins, n'y comptent pas, et le classement le dit (`truncated`). Une colonne y porte son score et ses réponses, pas ses destinataires. Deux lectures d'un même jeu, dans une même version, par un même run se fusionnent en une, la plus précise ([schéma](schema.md#écrire-dans-le-graphe)) : un run qui a lu la table entière et nommé des colonnes ne compte que pour celles-ci. Le score n'ordonne pas encore les réponses d'un incident à rappeler.
- La couverture du lignage ne voit que ce que voient les émetteurs qui publient des relevés : un appel qui ne passe par aucun d'eux n'est compté nulle part. Elle compte les appels d'outils, pas les runs d'agents.
- La couverture d'une remontée ne regarde que les émetteurs qui ont vu le run : si un émetteur a perdu le seul événement de ce run qu'il ait porté, rien ne le relie à la réponse, et son trou n'apparaît pas dans sa couverture. La couverture de chaque émetteur sur la période (`GET /api/v1/coverage`) le montre.
- Un émetteur retiré pour de bon reste connu : rien ne permet encore de le déclarer retiré, et son silence rend partielle la couverture de toute période qui suit son dernier relevé.
- Les relevés de couverture et les événements bruts sont purgés au-delà de leur [conservation](workers.md#partitions-mensuelles), 13 mois par défaut : la couverture d'une période plus ancienne n'est plus mesurée (`unknown`), alors que le graphe, lui, garde ses réponses.
- Sur un très grand graphe, une vue d'ensemble demandée à une date lointaine parcourt les réponses plus récentes pour les écarter : elle peut dépasser le délai maximal (`503`).
