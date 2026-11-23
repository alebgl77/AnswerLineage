# Réémission vers un catalogue OpenLineage

Les workers peuvent réémettre chaque événement qu'ils intègrent au graphe vers un consommateur OpenLineage externe : Marquez, DataHub, OpenMetadata, Dataplex. Les agents, leurs appels d'outils et les jeux de données qu'ils lisent apparaissent alors dans le catalogue que l'entreprise a déjà, à côté de ses autres traitements. Tous ces catalogues lisent le transport HTTP d'OpenLineage, un événement par requête `POST` : c'est lui que parle la réémission. Les tests l'éprouvent sur un vrai Marquez.

```bash
export ANSWERLINEAGE_DATABASE_URL=postgresql://utilisateur:motdepasse@hote/base
export ANSWERLINEAGE_REEMIT_URL=https://marquez.interne/api/v1/lineage
export ANSWERLINEAGE_REEMIT_AUTHORIZATION="Bearer …"   # facultatif
uv run answerlineage-worker
```

La réémission n'est pas une dépendance : sans destination, rien n'est mis en file, rien n'est envoyé, et AnswerLineage fonctionne comme avant. PostgreSQL reste la seule dépendance obligatoire.

## Réglage

| Réglage | Option du worker | Variable | Défaut |
| --- | --- | --- | --- |
| Point d'entrée OpenLineage de la destination | `--reemit-url` | `ANSWERLINEAGE_REEMIT_URL` | aucun : rien n'est réémis |
| Valeur de l'en-tête `Authorization` envoyé à la destination | | `ANSWERLINEAGE_REEMIT_AUTHORIZATION` | aucun en-tête |
| Événements gardés au plus dans la file de réémission | `--reemit-max-events` | | 1 000 000 (1 à 100 000 000) |
| Heures pendant lesquelles un événement attend au plus d'être réémis | `--reemit-max-age-hours` | | 168, une semaine (1 à 2 160) |

**Une seule destination.** Réémettre vers deux catalogues demande un relais devant eux, ou que l'un lise l'autre ; AnswerLineage n'envoie qu'à une adresse.

**Le même réglage partout.** Le worker qui projette un événement le met en file de réémission s'il a une destination ; un worker qui l'envoie lit la même file. Tous les workers d'une base, et `answerlineage-worker rebuild`, portent donc la même destination. Un worker sans destination projette sans rien mettre en file : ce qu'il projette n'est pas réémis.

**L'en-tête d'authentification** est envoyé tel quel, `Bearer …` ou `Basic …`. Il ne se règle que par la variable : une option se lit dans la liste des processus. Il n'est ni journalisé, ni affiché, ni gardé en base ; un texte qui n'est pas de l'ASCII imprimable, ou de plus de 8 192 caractères, empêche le worker de démarrer, sans être cité. Une variable vide ne règle rien ; un en-tête réglé sans destination empêche le worker de démarrer.

Le point d'entrée dépend du catalogue :

| Catalogue | Adresse | Authentification |
| --- | --- | --- |
| Marquez | `https://marquez.interne/api/v1/lineage` | Aucune dans Marquez lui-même : celle du mandataire placé devant lui |
| DataHub | `https://datahub.interne/openapi/openlineage/api/v1/lineage` | `Bearer` et un jeton d'accès |
| OpenMetadata | Le point d'entrée OpenLineage que donne sa documentation | `Bearer` et le jeton d'un robot |
| Dataplex | `https://datalineage.googleapis.com/v1/projects/PROJET/locations/REGION:processOpenLineageRunEvent` | `Bearer` et un jeton OAuth, qui expire ([limites](#limites-connues)) |

Seul Marquez est éprouvé par les tests ; pour les autres, l'adresse et l'authentification sont à vérifier dans la documentation du catalogue. L'image de Marquez pèse près d'un gigaoctet : son test ne tourne que si `ANSWERLINEAGE_TEST_CATALOG=1`, dans une seule case de la matrice du pipeline (Python 3.13, PostgreSQL 18) et sur le poste à la demande.

### L'adresse de la destination

L'adresse est réglée par l'exploitant au démarrage ; elle n'est jamais tirée d'un événement, d'une facette ni d'une réponse. Une adresse refusée empêche le worker de démarrer, avec « Configuration invalide : … » et le code de sortie 2, sans être citée.

| Adresse | Admise |
| --- | --- |
| `https://…`, nom ou adresse IP | Oui, sauf les adresses ci-dessous |
| `http://…` sur la boucle locale (`localhost`, `127.0.0.0/8`, `[::1]`) | Oui : pour les tests et la démonstration. Ailleurs, l'événement et l'en-tête d'authentification voyageraient en clair |
| Un identifiant (`utilisateur:motdepasse@`), des paramètres (`?…`), un fragment (`#…`) | Non : l'adresse peut apparaître dans la configuration, elle ne porte aucun secret |
| Lien local (`169.254.0.0/16`, `fe80::/10`), où les clouds servent les métadonnées et les identifiants d'une machine (`169.254.169.254`, `169.254.170.2`) | Non |
| Métadonnées hors du lien local : AWS en IPv6 (`fd00:ec2::254`), Alibaba Cloud (`100.100.100.200`), Azure (`168.63.129.16`) | Non |
| Adresse nulle (`0.0.0.0`, `::`), de diffusion ou de groupe | Non |
| Les mêmes, enveloppées dans une adresse IPv6 (`::ffff:169.254.169.254`, `64:ff9b::a9fe:a9fe`) | Non |
| Les autres adresses internes (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `fc00::/7`) | Oui |

Un nom est contrôlé deux fois. Au démarrage, ce qu'il désigne alors : un nom qui mène, ne serait-ce que par une de ses adresses, à une adresse refusée empêche le worker de démarrer ; un nom qui ne se résout pas encore est admis, pour qu'une destination injoignable n'empêche pas de projeter. Puis à chaque connexion : le nom est résolu, chaque adresse contrôlée, et la connexion se fait à l'adresse contrôlée, sans seconde résolution qui mènerait ailleurs. Une adresse refusée à la connexion est un échec de l'envoi (`ForbiddenAddressError` au journal), repris comme une panne.

**Pourquoi les adresses internes sont admises.** Le catalogue d'une entreprise est souvent sur son réseau privé, derrière son pare-feu : refuser `10.0.0.0/8` rendrait la réémission inutilisable là où elle sert. Le risque d'une requête sortante vers une adresse interne vient d'une adresse qu'un tiers choisit ; ici, ni un événement, ni une réponse, ni une redirection ne la changent : seul l'exploitant la règle. Les adresses de métadonnées restent refusées : aucun catalogue n'y est, et une erreur de réglage y enverrait le lignage à un service qui sert les identifiants de la machine.

### Le client

- TLS est vérifié, certificat et nom, TLS 1.2 au moins. Les autorités sont celles du système ; pour un catalogue servi sous une autorité interne, `SSL_CERT_FILE` ou `SSL_CERT_DIR` dans l'environnement du worker désignent les siennes.
- Aucune redirection n'est suivie : une réponse `3xx` est un échec, repris comme une panne.
- Aucun mandataire n'est lu de l'environnement : seule la destination réglée est jointe.
- Délais : 5 s pour la connexion, 10 s pour chaque lecture ou écriture, 30 s pour l'envoi entier, qu'une réponse servie goutte à goutte ne prolonge pas. Au-delà, la connexion est coupée.
- Une connexion est gardée d'un événement à l'autre. Le client ne lit de la réponse que son statut, et au plus 64 Kio de corps, qu'il ne garde ni ne journalise.
- Il n'emploie que la bibliothèque standard de Python, dans un fil à lui : la boucle des workers n'attend jamais la destination.

## Ce qui est réémis

**Ce que la projection a intégré au graphe, et seulement cela.** Un événement entre en file de réémission dans la transaction qui le projette et le retire de la file de traitement ([pourquoi à ce moment](decisions/0016-reemission-a-la-projection.md)). Ne partent donc jamais :

- un événement refusé à l'ingestion : il n'a pas été enregistré ;
- un doublon : l'ingestion ne l'enregistre qu'une fois, la projection ne le voit qu'une fois ;
- un événement au [rebut](workers.md#file-de-rebut) : il n'est pas dans le graphe. Rejoué puis projeté, il part à ce moment ;
- un événement que la [reconstruction du graphe](workers.md#reconstruire-le-graphe) projette de nouveau : il était déjà parti. Un événement reçu pendant la reconstruction, ou qui attendait dans la file, part une fois projeté par elle.

**Tel qu'il a été reçu et accepté, sans rien y ajouter.** Le corps envoyé est l'événement enregistré, sous sa forme canonique : clés triées, sans espace, en UTF-8. C'est la forme dont l'empreinte SHA-256 est l'identifiant de l'événement ([idempotence](ingestion.md#idempotence)) : un catalogue qui reçoit deux fois un événement reçoit deux fois les mêmes octets. L'ordre des clés et les blancs de l'envoi d'origine ne sont pas gardés par la base, ni réémis.

**Les facettes propres d'AnswerLineage partent aussi** (`answerlineage_toolCall`, `answerlineage_agent`, `answerlineage_delivery`, `answerlineage_qos`, `answerlineage_keys`, voir [facettes](facettes.md)) :

- l'événement part tel qu'il a été accepté : en retirer une partie en ferait un autre événement, d'une autre empreinte, que le graphe d'AnswerLineage et le catalogue ne décriraient plus de la même façon ;
- elles ne portent aucun contenu : des noms de serveur et d'outil, des contextes de trace, des empreintes à clé, des pseudonymes de destinataires, des nombres ([aucun contenu](decisions/0004-aucun-contenu-empreintes-a-cle.md)) ;
- OpenLineage demande à un consommateur d'ignorer une facette qu'il ne connaît pas. Marquez les garde parmi les facettes du run et les montre ; un catalogue qui les ignore lit le reste de l'événement, run, job, jeux de données, facettes standard.

Ce que le catalogue reçoit, il le garde selon ses propres règles : les empreintes et les pseudonymes de destinataires qu'AnswerLineage tient sans table de correspondance s'y trouvent aussi.

Marquez nomme le job d'un run qui a un run parent d'après le job parent : l'appel `execute_sql` d'un agent `analyste-ventes` y apparaît sous le job `analyste-ventes.execute_sql`, dans l'espace de noms du serveur MCP (`mcp://entrepot`). Le run garde son identifiant : `GET /api/v1/jobs/runs/{runId}` de Marquez le retrouve, avec ses jeux de données en entrée.

## Garanties

**Au moins une fois.** Un lot est réclamé, envoyé, puis retiré de la file avec ses compteurs, dans une seule transaction. Un envoi dont l'issue n'est pas sûre est refait : délai dépassé, connexion coupée, worker arrêté ou base perdue avant la validation. La destination peut donc recevoir deux fois un événement. Le protocole d'OpenLineage le tolère, et Marquez le vérifie : un événement renvoyé est accepté (`201`), et le run, ses jeux de données en entrée et les runs de son job restent ce qu'ils étaient (`tests/test_reemission_marquez.py`).

**Plusieurs workers, aucun double envoi.** Chaque worker réclame son lot avec `FOR UPDATE SKIP LOCKED` : un événement réclamé par l'un est sauté par les autres, jusqu'à la fin de sa transaction.

**Dans l'ordre de la projection**, à un worker près : un worker envoie son lot dans l'ordre de la file ; deux workers envoient deux lots en même temps. OpenLineage n'exige aucun ordre entre les événements.

**Une destination en panne ne ralentit ni l'ingestion ni la projection.** L'ingestion n'écrit rien de plus. La projection ajoute une écriture à la transaction de son lot, la mise en file. L'envoi tourne dans une boucle à part du worker, et ses entrées-sorties dans un fil à part : une destination qui ne répond pas retient cette boucle, jamais celle qui projette.

**Une panne arrête le lot au premier échec.** L'événement en cause garde un essai de plus et la classe de l'échec, ou le statut de la réponse, jamais un message ; les suivants attendent sans essai compté. La reprise attend 1 s, puis le double à chaque échec, jusqu'à 5 minutes, chaque attente tirée au hasard entre sa moitié et son tout, pour que des workers ne reviennent pas ensemble. Un envoi réussi la remet à zéro.

| Réponse de la destination | Ce que fait la réémission |
| --- | --- |
| `2xx` | Remis : l'événement quitte la file, compté `sent` |
| `400`, `413`, `422` | Refusé : la destination ne l'acceptera pas mieux plus tard. Il quitte la file, compté perdu (`rejected`) |
| Tout autre statut (`401`, `403`, `404`, `429`, `5xx`), une redirection, une erreur réseau ou TLS, un délai dépassé, une adresse refusée | Échec : le lot s'arrête, l'événement reste en file et sera renvoyé après l'attente |

**Une file bornée.** Toutes les dix secondes, chaque worker qui a une destination retire ce qui dépasse les bornes, les plus anciens d'abord : les événements en file depuis plus de `--reemit-max-age-hours`, puis ceux au-delà de `--reemit-max-events`. Un événement en cours d'envoi n'est pas retiré : il part, ou reste. Ce qui est retiré est compté perdu dans la transaction qui le retire, et dit au journal à chaque passage qui retire quelque chose.

| Perte | Compteur | Ce qui la dit |
| --- | --- | --- |
| Refus de la destination | `rejected`, compris dans `lost` | Journal : le nombre et le statut |
| Débordement de la file, en âge ou en nombre | `dropped`, compris dans `lost` | Journal : le nombre et les bornes |
| Événement brut purgé par la [conservation](workers.md#partitions-mensuelles) avant son envoi | `dropped`, compris dans `lost` | Journal : le nombre et la partition |

**À l'arrêt**, sur `SIGINT` ou `SIGTERM`, l'envoi en cours est coupé et son événement reste en file, sans essai compté : il partira au prochain démarrage, au besoin une seconde fois.

## Ce qui se lit

Les compteurs sont dans la base : `reemission_queue` tient ce qui attend, `reemission_stats` ce qui est parti et ce qui s'est perdu depuis la mise en service ([schéma](schema.md#file-de-réémission)). La [route de santé](cli.md#la-route-de-santé) les donne :

```json
{
  "reemission": {
    "queued": 12,
    "retrying": 1,
    "lower_bound": false,
    "oldest_age_seconds": 94,
    "sent": 15230,
    "lost": 3,
    "rejected": 1
  }
}
```

| Champ | Contenu |
| --- | --- |
| `queued` | Événements projetés qui attendent d'être réémis |
| `retrying` | Parmi eux, ceux dont un envoi a échoué : ils seront renvoyés |
| `lower_bound` | Vrai au-delà de 10 000 événements : les deux décomptes précédents sont des minimums |
| `oldest_age_seconds` | Depuis combien de secondes le plus ancien attend ; nul quand la file est vide |
| `sent` | Événements remis depuis la mise en service |
| `lost` | Événements perdus depuis la mise en service : refusés, abandonnés par les bornes ou par la purge |
| `rejected` | Parmi les perdus, ceux que la destination a refusés |

[`answerlineage doctor`](cli.md#answerlineage-doctor) signale une réémission en retard (`reemission_lagging`) quand le plus ancien événement attend depuis plus de `--reemission-max-age` secondes, 300 par défaut, avec son remède : vérifier que la destination répond, que les workers la joignent, et leur réglage. Sans destination, la vérification n'est pas faite.

Le journal ne dit que des nombres, des statuts et des classes d'erreur : ni l'adresse de la destination, ni son en-tête d'authentification, ni rien d'un événement ou d'une réponse. Une panne se dit une fois, à son premier échec, puis son retour :

```text
INFO answerlineage_workers Réémission en marche : destination en https avec en-tête d'authentification, file bornée à 1000000 événements et 168 h.
WARNING answerlineage_workers Réémission en échec (HTTP 503) : les événements attendent dans la file ; reprise dans 0.7 s, puis de plus en plus espacée.
INFO answerlineage_workers Réémission rétablie après 6 échec(s) : la destination accepte de nouveau les événements.
WARNING answerlineage_workers Réémission : 1 événement(s) refusé(s) par la destination (HTTP 422) : abandonnés, comptés perdus.
WARNING answerlineage_workers Réémission : 250 événement(s) abandonné(s), les plus anciens, au-delà des bornes de la file (1000000 événements, 168 h) : comptés perdus.
```

## Débit

Le débit est celui de la destination. Mesuré vers Marquez 0.51.1, servi sur la machine de test (4 processeurs virtuels Intel Xeon à 2,10 GHz, 15 Gio de mémoire, PostgreSQL 18 dans Docker), avec 1 591 événements de l'entreprise synthétique : une connexion remet de l'ordre de 16 événements par seconde, quatre connexions en même temps de l'ordre de 63. Chaque worker qui a une destination ouvre une connexion : le débit croît avec les workers, et une ingestion qui reste au-dessus le temps de remplir la file perd ses plus anciens événements, comptés. La projection, elle, va de l'ordre de 300 événements par seconde par worker ([débit](workers.md#débit)) : c'est elle qui fixe ce que la file doit absorber.

## Le test de bout en bout

`tests/test_reemission_marquez.py` lance Marquez dans son image officielle, épinglée par son empreinte (`marquezproject/marquez:0.51.1`), et sa propre base PostgreSQL, sur un réseau que Testcontainers crée pour eux. Des demandes de l'entreprise synthétique sont reçues par l'API, projetées par un worker qui a Marquez pour destination, puis lues par l'API de Marquez : chaque appel d'outil y est, avec les jeux de données qu'il a lus et les facettes d'AnswerLineage. Il est marqué `integration` comme les autres. Il dure une quinzaine de secondes une fois l'image présente ; l'image pèse près de 900 Mo, à tirer une fois par machine.

## Limites connues

- Une seule destination ; le réglage n'est pas partagé par la base : un worker sans destination ne met rien en file, et ce qu'il projette n'est pas réémis.
- Rien n'est rattrapé : les événements projetés avant le réglage de la destination, et ceux perdus par les bornes ou un refus, ne sont pas remis en file. Les événements bruts restent dans `event_raw` jusqu'à leur purge ; aucune commande ne les réémet encore.
- L'en-tête d'authentification est fixe pour la vie du worker : un jeton qui expire, comme celui de Dataplex (une heure), demande un relais qui le renouvelle, ou un redémarrage avec le nouveau. Ni OAuth ni certificat client ne sont pris en charge.
- Un envoi par événement, une connexion par worker : le débit est celui de la destination ([débit](#débit)). Le lot d'OpenLineage (`/api/v1/lineage/batch`) n'est pas employé, faute d'être lu par tous les catalogues.
- Un événement remis puis rejoué depuis le rebut, parce que la projection l'a refusé lors d'une reconstruction, repart : la destination le reçoit deux fois, ce que le protocole tolère.
- La transaction d'un lot reste ouverte pendant son envoi, 30 s au plus, plus le délai d'un envoi : elle ne tient que des lignes de la file de réémission. Une maintenance des partitions qui purge alors un mois dont un événement est en cours d'envoi renonce à cette partition et la purge au passage suivant.
