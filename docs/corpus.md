# Entreprise synthétique

Le corpus invente une entreprise : ses jeux de données, ses outils, ses agents, deux semaines de demandes et quelques incidents de données. Il sert à essayer AnswerLineage et à le tester sans aucune donnée réelle.

Tout découle d'une graine. Deux générations avec la même graine donnent exactement les mêmes événements et la même vérité, sur toute machine et toute version de Python.

```bash
uv run answerlineage-corpus load --url http://127.0.0.1:8000
```

| Commande | Effet |
| --- | --- |
| `answerlineage-corpus events` | Écrit les événements OpenLineage de l'entreprise, en un tableau JSON |
| `answerlineage-corpus truth` | Écrit la vérité : pour chaque incident, les réponses et les destinataires touchés |
| `answerlineage-corpus load --url <API>` | Envoie les événements à l'API d'ingestion, par lots, puis les [relevés de couverture](#relevés-de-couverture) de ses serveurs MCP |

Les options `--seed`, `--requests`, `--days` et `--incidents` choisissent la graine et la taille. Par défaut : graine 1, 240 demandes sur 10 jours ouvrés, 6 incidents.

Devant une API qui demande un jeton porteur, `load` envoie celui de `ANSWERLINEAGE_TOKEN`, ou du fichier que nomme `ANSWERLINEAGE_TOKEN_FILE` : le jeton d'un émetteur, jamais sur la ligne de commande, et seulement en `https` ou vers la boucle locale. La [démonstration](demonstration.md) n'en demande pas.

## Ce que contient l'entreprise

| | |
| --- | --- |
| Agents | 5 : assistant commercial, assistant du support, analyste de trésorerie, planificateur de stocks, synthèse hebdomadaire |
| Outils | 4, sur 3 serveurs MCP : `entrepot/execute_sql`, `crm/rechercher_comptes`, `crm/lister_opportunites`, `support/lister_tickets` |
| Jeux de données | 13 tables : 10 dans l'entrepôt (`bigquery`), 3 dans des bases opérationnelles (`postgres://…`) |
| Destinataires | Une vingtaine de pseudonymes, répartis entre les audiences des agents : la forme d'un [pseudonyme](securite/pseudonymes.md) HMAC, tirée de la graine, sans personne derrière |

Une demande est un run d'agent. L'agent appelle un à trois outils, chaque appel lit une ou plusieurs tables, puis l'agent répond à un, deux ou trois destinataires de son audience. Environ une demande sur douze échoue avant de répondre.

Un outil SQL lit les tables que la requête choisit, parfois en entier, parfois quelques colonnes. Les autres outils lisent toujours les mêmes tables, en entier. Les tables de l'entrepôt sont rechargées chaque nuit : leur lecture porte la version du jour.

Rien de tout cela n'a de contenu. Une réponse est une empreinte, un message est une référence, un destinataire est un pseudonyme.

## Relevés de couverture

`load` envoie aussi, après les événements, les [relevés de couverture](ingestion.md#relevés-de-couverture) des émetteurs de l'entreprise : ses serveurs MCP, `mcp://entrepot`, `mcp://crm` et `mcp://support`, comme le proxy placé devant chacun les publierait. Un relevé par serveur et par jour UTC, du premier jour des demandes au lendemain du dernier, jours sans appel compris : les appels vus sont les appels d'outils de ce serveur ce jour-là, tous émis, aucun perdu, aucune requête non inspectée. L'entreprise ne perd rien : la couverture lue après chargement est mesurée, à 100 %, sans trou, et la démonstration l'affiche ainsi.

Renvoyés, les relevés ne changent rien : l'API les fusionne au plus grand. La vérité de l'entreprise n'en dépend pas, et ne change pas. Une fois chargée, l'entreprise ne publie plus : dix minutes plus tard, le [contrôle des émetteurs muets](workers.md#émetteurs-muets) signale ses trois serveurs, une fois, et la couverture d'une période postérieure à ses relevés n'est pas mesurée.

## Les événements

Chaque demande devient ce que des émetteurs réels enverraient :

| Moment | Événement |
| --- | --- |
| La demande arrive | `START` du run de l'agent, avec la facette `answerlineage_agent` |
| Chaque appel d'outil | `COMPLETE` d'un run enfant, tel que le proxy l'émet : facettes `parent` et `answerlineage_toolCall`, tables lues dans `inputs`, colonnes dans la facette `schema`, version dans la facette `version` |
| L'agent répond | `COMPLETE` du run de l'agent ; la réponse est une sortie qui porte la facette `answerlineage_delivery` : empreinte, canal, référence du message, destinataires |
| L'agent échoue | `FAIL` du run de l'agent, sans sortie |

Les schémas des facettes `answerlineage_*` sont publiés (voir [Facettes OpenLineage](facettes.md)) : un test valide contre eux chaque événement du corpus.

Un test vérifie que chaque événement respecte le schéma officiel d'OpenLineage, que l'API d'ingestion l'accepte, et qu'un appel d'outil est décrit exactement comme le proxy le décrit. Un autre envoie l'entreprise par l'API et les workers, puis compare le graphe obtenu à l'entreprise générée.

## La vérité

Un incident désigne une table et une fenêtre de temps : le chargement de nuit est faux à 6 h, et corrigé de quelques heures à trois jours plus tard.

Une réponse est touchée quand un appel de sa demande a lu la table pendant la fenêtre, début inclus et fin exclue. Une demande restée sans réponse n'a rien à rappeler. C'est l'heure de la lecture qui compte, pas celle de la réponse.

Le générateur établit cette liste lui-même, à partir de ce qu'il a généré, sans passer par AnswerLineage. C'est à elle que se mesure la propagation d'un incident : toute réponse attendue doit être retrouvée.

```json
{
  "name": "incident-01",
  "dataset": {"namespace": "bigquery", "name": "ventes.suivi.commandes"},
  "from": "2026-11-11T06:00:00Z",
  "to": "2026-11-12T12:00:00Z",
  "answers": [
    {
      "fingerprint": "sha256:…",
      "channel": "slack",
      "messageRef": "…",
      "recipients": ["…"],
      "agent": {"namespace": "agent://ventes", "name": "assistant-commercial"}
    }
  ],
  "recipients": ["…"]
}
```

## Corpus de vérité

À côté de l'entreprise générée, le corpus tient des scénarios d'incident écrits à la main, chacun avec sa vérité. Ils servent à mesurer le moteur d'impact comme on mesure un modèle : rappel sur les réponses touchées, précision sur les réponses écartées. Certains tiennent aussi l'[ordre d'exposition](#lordre-dexposition) attendu de leurs jeux de données.

```bash
uv run answerlineage-corpus check
```

| Scénario | Niveau | Ce qu'il éprouve |
| --- | --- | --- |
| `table-fenetre` | table | Les bornes de la fenêtre, la réponse rendue après la correction, le run sans réponse |
| `table-sous-agent` | table | La lecture faite par un sous-agent, ou par un appel parmi d'autres |
| `fenetre-sans-lecture` | table | Un incident qui ne touche personne |
| `version-corrigee` | table | La table rechargée avant la fin déclarée de l'incident |
| `colonnes` | colonnes | Une seule colonne fausse : colonnes communes, disjointes, table lue en entier |
| `sous-ensemble` | sous-ensemble | Une partition fausse : sous-ensemble commun, disjoint, inconnu |
| `cles` | clés | Quelques lignes fausses : clés communes, disjointes, inconnues |
| `transitivite-memoire` | table | Une réponse fondée sur une note écrite à partir de la table fausse |
| `transitivite-chaine` | table | La table fausse alimente une table dérivée, puis une note, puis une réponse |
| `exposition-anciennes-recentes` | table | Six réponses de trois mois contre une réponse de la veille, trois d'un mois ; un run sans réponse |
| `exposition-destinataires` | table | Une réponse à dix destinataires contre trois réponses à un seul, cinq à aucun |
| `exposition-colonne-unique` | colonnes | Une colonne lue par une seule réponse récente, deux lues par trois réponses plus anciennes, la table lue en entier |

Un scénario est un fichier JSON de `corpus/src/answerlineage_corpus/scenario_data/`. Il décrit quelques runs d'agents, un incident et sa vérité :

| Rubrique de la vérité | Sens |
| --- | --- |
| `touched` | Réponses à rappeler, avec la confiance attendue (`certaine`, `probable`, `possible`). `path` vaut `indirect` quand la réponse dépend d'une sortie d'un run touché et non de la table elle-même |
| `discarded` | Réponses qui ont lu la table pendant la fenêtre sans dépendre de ce qui est faux, avec le motif : colonnes, sous-ensembles, clés ou version disjoints |
| `untouched` | Réponses sans rapport avec l'incident |
| `exposure` | Facultatif : l'ordre d'exposition attendu des jeux de données, et de leurs colonnes, à une heure de référence ([plus bas](#lordre-dexposition)) |

Le contrôle refuse un scénario sans vérité, une réponse que la vérité oublie ou classe deux fois, et une vérité que la description contredit : une réponse dite touchée alors que rien n'a lu la table pendant la fenêtre, une dépendance indirecte qui remonterait le temps. Il exige que le corpus couvre les quatre niveaux de précision, la transitivité, les réponses écartées et un ordre d'exposition.

Le corpus porte un numéro de version, lié à son contenu : changer un scénario ou sa vérité demande une nouvelle version, pour que deux mesures restent comparables. L'empreinte de chaque version est enregistrée (`_DIGESTS` dans `scenarios.py`) ; un contenu changé sans nouvelle version fait échouer les tests.

| Version | Ce qui a changé |
| --- | --- |
| 1 | Les neuf scénarios d'incident : table, colonnes, sous-ensemble, clés, transitivité |
| 2 | Trois scénarios d'exposition, et la vérité de leur ordre d'exposition |

Les sous-ensembles et les clés d'une lecture sont décrits dans le scénario, mais pas encore dans ses événements : le contrat d'événements ne les porte pas.

### L'ordre d'exposition

Un scénario peut écrire, dans sa vérité, l'ordre attendu du [score d'exposition](lecture.md#score-dexposition) : ses jeux de données, du plus exposé au moins exposé, à une heure de référence, et pour chaque jeu dont des lectures nomment des colonnes, l'ordre de ces colonnes.

```json
"exposure": {
  "at": "2026-11-01T12:00:00Z",
  "datasets": ["taux", "factures"],
  "columns": {
    "taux": ["date_valeur", "devise", "taux"],
    "factures": ["montant", "client_id"]
  }
}
```

Les jeux de données se nomment par leur alias dans le scénario. Trois scénarios tiennent un ordre qui ne va pas de soi :

| Scénario | Ordre attendu | Ce qu'il éprouve |
| --- | --- | --- |
| `exposition-anciennes-recentes` | stocks, commandes, factures | Les factures ont le plus de réponses, les commandes la plus récente : ni l'une ni l'autre n'est en tête. Six réponses de trois mois pèsent moins qu'une réponse de la veille. Les encaissements, lus par un run qui n'a pas répondu, ne sont pas classés |
| `exposition-destinataires` | effectifs, commandes, tickets | Une seule réponse, à dix destinataires, passe devant trois réponses à un destinataire et cinq à aucun : l'ordre inverse du nombre de réponses |
| `exposition-colonne-unique` | taux, factures ; date de valeur, devise, taux | La date de valeur, lue par une seule réponse, est la colonne la plus exposée ; la réponse qui a lu la table entière compte pour chaque colonne ; la devise et le taux, à égalité, se suivent par leur nom |

Le contrôle recoupe cet ordre avec la description du scénario, sans passer par le produit : il recalcule le score de chaque jeu et de chaque colonne à partir des runs, des lectures, des heures et des destinataires que le scénario décrit, selon la règle publiée. Il refuse un ordre qui oublie un jeu dont une réponse dépend, qui en nomme un dont aucune ne dépend, qui en nomme un deux fois ou qui les range autrement ; de même pour les colonnes, dont la liste doit être exactement celle que les lectures nomment. L'ordre écrit à la main reste la vérité : le contrôle n'en est qu'une relecture.

`tests/test_truth_corpus_exposure.py` charge chaque scénario qui tient un ordre d'exposition par le chemin réel, dans sa propre base, puis demande le classement à l'API à l'heure de référence du scénario. L'ordre des jeux de données et de leurs colonnes doit être celui de la vérité, sans troncature ; la commande `answerlineage exposure` doit lire le même classement.

### Mesure du moteur d'impact

```bash
make measure                           # rejoue la mesure et imprime son tableau
make measure REPORT=/tmp/mesure.json   # écrit aussi son détail en JSON, hors du dépôt
```

Chaque scénario passe par le chemin réel, dans sa propre base : ingestion, file, workers, puis propagation de son incident par l'API. Ce que le moteur renvoie est comparé à la vérité du scénario, scénario par scénario puis en tout : réponses attendues, retrouvées, manquées, en trop, rappel et précision. La mesure reçoit les réponses renvoyées et lit la vérité dans le corpus (`answerlineage_corpus.measure`) : la vérité ne passe jamais par le produit.

La même vérité se lit de trois façons :

| Lecture | Réponses attendues | Réponses en trop |
| --- | --- | --- |
| Au niveau table | Toute réponse dont un run a lu la table pendant la fenêtre : les touchées directement, et les écartées, qui l'ont lue aussi | Les réponses hors d'atteinte |
| À la précision déclarée | Les réponses touchées directement, par scénario puis par niveau (table, colonnes, sous-ensemble, clés) | Les réponses écartées et les réponses hors d'atteinte |
| Transitivité | Les réponses touchées par une sortie d'un run touché | |

**La porte.** Elle joue à chaque merge request, dans le job `tests` (`tests/test_truth_corpus_impact.py`). Elle échoue quand le rappel au niveau table, sur l'ensemble du corpus, passe sous 0,99 (`MIN_TABLE_RECALL`), ou quand une réponse que le corpus dit hors d'atteinte est renvoyée. Son message nomme chaque réponse en cause par son scénario et son identifiant dans le scénario, puis donne la mesure obtenue :

```
Réponses manquées au niveau table :
  table-sous-agent/synthese
Rappel au niveau table : 0,950 (19/20), sous le seuil de 0,99.
```

Taire une réponse touchée est le pire défaut d'un rappel : celui qui l'a lue ne sera pas prévenu. Sur les quelques dizaines de réponses attendues du corpus, le seuil n'en tolère aucune manquée ; quand le corpus grandira, une sur cent au plus. Un test affaiblit réellement le moteur, qui ne remonte plus qu'au run parent immédiat d'une lecture, et vérifie que la porte échoue en nommant la réponse que le sous-agent a fait perdre.

**Ce que la porte ne retient pas encore.** Le moteur propage au niveau table : il renvoie les réponses écartées, et la précision déclarée est mesurée et imprimée, sans seuil. Elle dit ce que gagnerait une propagation par colonnes, sous-ensembles, clés ou versions. Les réponses touchées par transitivité sont imprimées comme manquées : le moteur ne suit pas encore ce qu'un run touché a écrit ([limites](lecture.md#limites-connues)).

## Corpus d'abus

Le corpus tient aussi des cas d'attaque : une requête piégée, et ce que le produit doit en faire. Ils sont rejoués à chaque merge request devant le vrai proxy, la vraie API, un vrai PostgreSQL, un worker et, pour l'explorateur, un vrai navigateur. Un cas qui n'est plus bloqué fait échouer le pipeline, qui le nomme :

```
lots/lot-1001 : 413 batch_too_large attendu, 202 obtenu
```

```bash
make abuse                          # contrôle le corpus, puis rejoue tous ses cas
uv run answerlineage-corpus check   # contrôle seul, avec le corpus de vérité
uv run pytest "tests/test_abuse_corpus.py::test_every_case_of_the_abuse_corpus_is_blocked[lots/lot-1001]"
```

| Famille | Ce qu'elle éprouve |
| --- | --- |
| `identifiants` | Une instruction, du SQL ou une balise là où un nom est attendu : nom d'outil, chaque champ que la projection recopie, nom de facette, mot de passe dans une adresse, jeu de données et gravité d'un incident, identifiant d'un incident ou d'un événement au rebut, curseur, adresse de l'explorateur |
| `sql` | Une requête trop grande, trop imbriquée, trop longue à analyser, dynamique, ou qui nomme une table hostile : la lecture reste « non résolue », l'appel passe |
| `lots` | Trop d'événements, d'octets, de niveaux ou d'éléments ; bombe gzip ; runs parents l'un de l'autre ; lecture qui dépasse son délai ; périmètre, nom ou fenêtre d'un incident au-delà de leur borne ; rejeu qui demande trop d'événements ; page trop grande |
| `invisibles` | Largeur nulle, sens d'écriture forcé, sélecteur de variante, caractère nul, deux écritures Unicode d'un même nom |
| `emetteur` | Un autre hôte, une autre page, un faux proxy, les en-têtes d'identité, l'événement ou le relevé d'un autre émetteur, un relevé de couverture aux compteurs incohérents |
| `entetes` | Un en-tête MCP contredit par le corps : le corps décide, l'écart est signalé |
| `capture` | Une requête que le proxy ne peut pas lire sans ambiguïté : elle passe, sans événement, et se compte |
| `champs` | Un champ que le contrat ne prévoit pas : du texte libre dans un incident, qui n'en porte aucun ; des colonnes dans son périmètre ; un filtre de rejeu mal orthographié, qui ne doit pas rejouer tout le rebut ; un champ au nom piégé, que le refus ne nomme pas ; un paramètre inconnu d'une lecture, ignoré ; un message, un auteur, un motif ou un gabarit écrits dans la décision sur une liste de rappel, et une décision qui demande l'envoi lui-même |
| `jetons` | L'[authentification](securite/authentification.md) de l'API : aucun jeton ; un jeton sans signature, signé en HMAC avec la clé publique, d'un autre émetteur ou d'une autre audience, expiré, pas encore valide, d'une clé que l'émetteur ne publie pas, à la signature altérée, démesuré ; le rôle de lecteur sur une route qu'il n'ouvre pas, dont la validation d'un rappel |

### Lire un cas

Un fichier JSON par famille, dans `corpus/src/answerlineage_corpus/abuse_data/`. Un cas se nomme `famille/nom` :

```json
{
  "name": "outil-largeur-nulle",
  "title": "Un espace de largeur nulle (U+200B) dans un nom d'outil",
  "surface": "proxy",
  "base": "proxy/appel-outil",
  "alter": {"field": "/body/params/name", "value": "rechercher\u200bcanari7c1d"},
  "expect": {"verdict": "neutralise", "code": "invalidToolName"}
}
```

| Champ | Sens |
| --- | --- |
| `surface` | Ce que le cas vise : `proxy`, `ingestion`, `lecture` ou `explorateur`. `ingestion` désigne les routes de l'API qui écrivent : les événements et les lots, les relevés de couverture, la déclaration d'un incident, le rejeu du rebut |
| `base` | La requête saine dont il part, nommée dans `abuse_bases.json` avec ce à quoi se voit qu'elle passe (`healthy`). Le test le vérifie pour chaque base : un cas n'est bloqué que pour la raison qu'il déclare |
| `alter` | Une seule altération : la valeur d'un champ, désigné par un pointeur JSON dans la requête (`/path`, `/body/…`, `/query/…`, `/seed/…`, `/reports/…` et `/incidents/…` pour les événements, les relevés de couverture et les incidents envoyés avant une lecture), celle d'un en-tête (`header`), ou une recette |
| `expect` | L'attente, écrite à la main : un verdict et son code. Elle ne se calcule jamais avec les validateurs du produit, que le corpus mesure |
| `canary` | La marque que porte la valeur hostile, `canari7c1d` par défaut. Elle est cherchée après coup dans les réponses, toute la base, les événements, les compteurs et les journaux : un cas dont la valeur a fui est un échec, qui dit où sans dire quoi |

Les recettes forment une liste fermée :

| Recette | Ce qu'elle fait de la requête saine |
| --- | --- |
| `repeter` | Répète le contenu d'un tableau, ou le premier membre d'un objet, jusqu'à en compter `count` ; avec `motif`, ajoute ce texte `count` fois à la suite d'un texte |
| `imbriquer` | Entoure un texte, ou le corps entier (`/body`), de `depth` ouvrants et fermants |
| `gonfler` | Porte le corps à `bytes` octets par des blancs, à la fin ou au début (`at`) : il reste du JSON, seule sa taille change. `announce: false` l'envoie en flux, sans longueur annoncée |
| `compresser` | Compresse le corps en gzip et l'annonce |
| `bombe-gzip` | Corps gzip de quelques Ko qui se déploie en `bytes` octets |
| `dupliquer-cle` | Écrit deux fois une clé du corps : sa valeur saine, puis `value` |
| `deplacer-hote` | Met un hôte étranger dans `Host`, et l'hôte attendu dans un en-tête de relais (`to`) |
| `encoder-base64` | Donne à un en-tête MCP une valeur sous sa forme `=?base64?…?=` |
| `ralentir` | Retient la lecture par un verrou et abaisse son délai maximal à `timeout_ms` : le cas ne dépend pas de la machine |
| `rejouer` | Renvoie un événement ou un relevé déjà envoyé, tel quel ou avec un champ changé |
| `forger-jeton` | Présente à l'API, à la place du jeton de l'appelant, la contrefaçon `forge`, dont le sujet est `subject` (le canari) : `absent`, `alg-none`, `hs256-cle-publique`, `emetteur-etranger`, `audience-etrangere`, `expire`, `pas-encore-valide`, `kid-inconnu`, `signature-alteree`, `demesure`, `lecteur-seul`. Le corpus ne fabrique pas le jeton : celui qui joue la requête le forge avec l'émetteur OIDC de test |

« Bloqué » ne veut pas dire la même chose partout :

| Surface | Verdict | Ce qui est vérifié |
| --- | --- | --- |
| `proxy` | `neutralise` | L'appel est relayé octet pour octet, parce que la capture ne bloque jamais un agent ; un seul événement, émis après la réponse, marque l'anomalie (`invalidToolName`, `headerMismatch` et ses en-têtes, `unresolved` et son motif) sans porter la valeur |
| `proxy` | `compte` | La requête est relayée, aucun événement ne la décrit, et elle est comptée comme non inspectée |
| `proxy` | `refuse` | `403` et son code, rien n'atteint le serveur MCP. Seulement pour `Host`, `Origin` et les requêtes d'une autre page : le contrôle du corpus refuse tout autre refus du proxy |
| `ingestion` | `refuse` | Le statut et le code, dont `401` et `403` pour un jeton refusé ; aucun événement enregistré, la file reste vide, aucune table ne change après le passage d'un worker, ni le graphe ni celles des incidents. `read: partial` : le corps n'a pas été lu en entier |
| `ingestion` | `rejete` | Le rang de l'événement, le chemin du champ et le code ; rien n'entre de cet événement. Sans rang, pour un événement envoyé seul, un relevé, une déclaration d'incident ou une demande de rejeu : rien n'est enregistré |
| `ingestion`, `lecture` | `sans_effet` | La requête altérée obtient exactement ce qu'obtient la requête saine : même réponse, même graphe. L'archive des événements reçus, qui garde un événement accepté tel qu'il est arrivé, n'est alors pas fouillée |
| `lecture` | `refuse` | `401` ou `403` et son code pour un jeton refusé, `404` et son code, ou `422` avec le chemin du paramètre ; jamais la valeur reçue |
| `lecture` | `borne` | Une réponse `200` dans le délai, qui dit si elle est tronquée et, complète, rend ce que rend la requête saine ; ou le refus `503` du délai. Jamais de `500` |
| `explorateur` | `inerte` | Aucun nœud ne porte la valeur, la page ne l'affiche pas et n'en tire aucun élément, aucune erreur de script ni refus de la politique de contenu, et le message fixe attendu s'affiche |

### Ajouter un cas

1. Choisir sa famille et sa base ; une nouvelle base se déclare dans `abuse_bases.json`, avec son issue saine. Une lecture part d'une base remplie par le chemin réel : ses événements (`seed`), passés par un worker, ses relevés de couverture (`reports`) puis ses incidents (`incidents`), déclarés par l'API, et, quand la base le demande (`analysed`), analysés par un worker, qui en fait la liste de rappel. L'API tire l'identifiant d'un incident à sa déclaration : `{incident}`, dans le chemin, est remplacé par celui du premier.
2. Écrire le cas : une seule altération, dont la valeur porte le canari, et l'attente lue dans la documentation du produit, pas dans son code. Un caractère invisible ne s'écrit jamais tel quel : toujours en échappement JSON, pour que la revue voie ce qui s'exécute.
3. Rejouer : `make abuse`. Le contrôle refuse un cas sans attente, hors des listes fermées (surface, verdict, base, recette), une famille exigée à moins de trois cas bloqués et un caractère invisible écrit tel quel.
4. Changer la version : l'empreinte du corpus est liée à son numéro de version (`ABUSE_VERSION` dans `abuse.py`). Le contrôle donne la nouvelle empreinte ; affaiblir un cas sans changer de version échoue.

| Version | Ce qui a changé |
| --- | --- |
| 1 | Le corpus : le proxy, l'ingestion, la lecture et l'explorateur |
| 2 | La lecture antidatée est bloquée : son exception est retirée |
| 3 | Les routes des incidents (déclarer, lister, lire) et de la file de rebut (lister, rejouer), et la famille `champs` |
| 4 | Les assertions de qualité : texte libre, colonne piégée, rafale |
| 5 | La confirmation d'un incident proposé, et les tests en échec qui le font naître |
| 6 | La famille `jetons` et la recette `forger-jeton` ; chaque requête porte désormais le jeton d'un appelant qui a tous les rôles, y compris celui d'émetteur, et les cas ouverts des émetteurs se jouent ainsi |
| 7 | Les destinataires pseudonymisés : un destinataire en clair refusé à l'ingestion, une identité candidate ou un pseudonyme en clair refusés à la révélation sans être recopiés, et la révélation refusée au lecteur |
| 8 | La décision sur une liste de rappel : refusée au lecteur, sans texte libre, sans auteur dans le corps, sans valeur hors de ses listes ; une décision qui demande l'envoi lui-même. Une base peut faire analyser ses incidents (`analysed`) |

Un cas qui révèle une attaque que le produit laisse passer ne se retire pas et ne se corrige pas en passant : il devient un cas ouvert.

### Cas ouverts

Les trois cas ouverts des émetteurs se jouent avec le jeton d'un émetteur authentifié : un appelant sans jeton, ou avec un jeton forgé, est refusé avant l'ingestion (famille `jetons`). Ce qui reste ouvert est le lien entre un émetteur et l'espace de noms de ses événements : un client qui a le rôle d'émetteur écrit encore au nom d'un autre.

Un cas que le produit ne bloque pas encore reste au corpus, déclaré avec son motif dans `corpus/src/answerlineage_corpus/abuse_open.json`. À l'exécution, c'est un échec attendu, nommé à chaque passage avec ce qui a été obtenu. Le jour où il se trouve bloqué, le pipeline échoue tant que son exception n'est pas retirée. Ce fichier est un chemin protégé : ouvrir un cas demande la revue d'un mainteneur. « Tous les cas sont bloqués » se lit sur les cas qui ne sont pas ouverts, et un cas ouvert ne compte pas dans les trois qu'une famille exige.

| Cas ouvert | Motif |
| --- | --- |
| `emetteur/evenement-au-nom-du-proxy` | Un émetteur authentifié n'est pas encore lié à l'espace de noms de ses événements : tout client qui a le rôle d'émetteur envoie un événement au nom du proxy, et la lecture inventée entre dans le graphe. |
| `emetteur/doublons-au-nom-d-un-autre` | Un émetteur authentifié n'est pas encore lié à l'espace de noms de ses événements : un rejeu est compté au nom de l'émetteur que l'événement déclare, pas du client qui l'envoie. |
| `emetteur/releve-au-nom-d-un-autre` | Un relevé de couverture n'est pas lié à son émetteur plus qu'un événement : tout client qui a le rôle d'émetteur en publie un au nom d'un autre, gonfle les appels vus d'une fenêtre, et la fusion au plus grand ne le défait pas. |

### Ce que le corpus ne dit pas

- Il prouve que des attaques connues sont bloquées, pas qu'aucune ne passe.
- Les cas de délai ne reposent pas sur la vitesse de la machine : la lecture lente est retenue par un verrou sous un délai abaissé à 50 ms, et la requête qui épuise le budget d'analyse demanderait une vingtaine de secondes sous un budget de 250 ms. Le budget de l'outil SQL des bases est relevé à 250 ms, contre 50 ms par défaut, pour que la requête saine ne dépende pas de la charge.
- Les bornes de taille sont éprouvées à leur valeur par défaut ; celles des lectures (nombre de runs, de lectures, de réponses) ne le sont pas ici : les atteindre demanderait des dizaines de milliers d'événements. Les tests de l'API de lecture les couvrent.
- Dans l'explorateur, le navigateur consigne lui-même comme une erreur le refus `404` ou `422` de l'API : ce refus, qui amène le message fixe, n'est pas compté parmi les erreurs de la page.
- Une valeur hostile qui est un nombre, comme un compteur incohérent, ne porte pas de canari : rien n'est cherché après coup pour ces cas.
- Le rebut est vide quand ses routes sont rejouées : aucune requête du corpus ne fait refuser un événement par la projection. Les cas éprouvent la validation, les bornes, le délai et le contrôle des hôtes et des origines de la liste et du rejeu, pas un rejeu qui remet des événements en file ; les tests de l'API et des workers le couvrent.
- L'analyse d'un incident déclaré ne tourne pas, sauf pour une base qui le demande (`analysed`) : la lecture d'un incident le rend en attente d'analyse, sans réponse touchée ; la décision sur une liste de rappel porte sur la liste que l'analyse a faite.
- L'envoi d'un rappel forcé sans validation ne s'éprouve ici que par l'API, qui n'envoie rien : la base qui refuse une écriture directe est éprouvée par `api/tests/test_recall_decisions.py`.

## Limites connues

- Les agents ne se lisent pas entre eux : aucune réponse ne dépend d'une sortie d'un autre agent.
- Tous les appels sont captés et toutes les lectures sont résolues : l'entreprise n'a pas de trou de lignage.
