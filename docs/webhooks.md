# Webhooks signés

Les workers annoncent chaque changement d'état d'un [incident](incidents.md) au récepteur que règle l'exploitant : l'incident est déclaré, son analyse d'impact est faite, son analyse est en échec, il est mis en quarantaine, sa quarantaine est levée. Chaque annonce est un message JSON à gabarit fixe, envoyé par une requête `POST` et signé avec son heure d'émission. Le récepteur vérifie la signature avec le secret qu'il partage avec les workers, puis déclenche ce qu'il veut : une alerte, un ticket, la suite d'un rappel.

```bash
export ANSWERLINEAGE_DATABASE_URL=postgresql://utilisateur:motdepasse@hote/base
export ANSWERLINEAGE_WEBHOOK_URL=https://alertes.interne/answerlineage
export ANSWERLINEAGE_WEBHOOK_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
uv run answerlineage-worker
```

Le webhook n'est pas une dépendance : sans lui, rien n'est mis en file, rien n'est envoyé, et AnswerLineage fonctionne comme avant. PostgreSQL reste la seule dépendance obligatoire.

## Réglage

| Réglage | Option du worker | Variable | Défaut |
| --- | --- | --- | --- |
| Adresse du récepteur | `--webhook-url` | `ANSWERLINEAGE_WEBHOOK_URL` | aucune : rien n'est annoncé |
| Secret partagé | | `ANSWERLINEAGE_WEBHOOK_SECRET` | aucun : obligatoire avec une adresse |
| Secret précédent, pendant une [rotation](#rotation-du-secret) | | `ANSWERLINEAGE_WEBHOOK_PREVIOUS_SECRET` | aucun |
| Messages gardés au plus dans la file des webhooks | `--webhook-max-messages` | | 10 000 (1 à 1 000 000) |
| Heures pendant lesquelles un message attend au plus, et pendant lesquelles le webhook reste [en service](#en-service) après le dernier passage d'un worker | `--webhook-max-age-hours` | | 168, une semaine (1 à 2 160) |

**Le secret** est un texte ASCII imprimable, sans espace, de 32 à 512 caractères : 32 octets tirés au hasard, écrits en base64 comme ci-dessus, en font un bon. Il ne se règle que par la variable : une option se lit dans la liste des processus. Il n'est ni journalisé, ni affiché, ni gardé en base, ni rendu par aucune route. Un secret absent, trop court ou qui n'est pas de l'ASCII imprimable empêche le worker de démarrer, avec « Configuration invalide : … » et le code de sortie 2, sans être cité ; un secret réglé sans adresse aussi. Une variable vide ne règle rien.

**L'adresse** obéit aux règles de la [réémission](reemission.md#ladresse-de-la-destination), par le même code : `https`, ou `http` sur la boucle locale seulement ; ni identifiant, ni paramètre, ni fragment ; ni adresse de métadonnées d'un cloud, ni lien local, ni adresse nulle, écrites ou désignées par un nom, contrôlées au démarrage puis à chaque connexion ; les autres adresses internes sont admises. Le client est celui de la réémission : TLS vérifié, aucune redirection suivie, aucun mandataire lu de l'environnement, 5 s pour la connexion, 10 s pour chaque lecture ou écriture, 30 s pour l'envoi entier ([le client](reemission.md#le-client)). Une adresse refusée empêche le worker de démarrer, sans être citée.

### En service

L'API déclare les incidents et les met en quarantaine, les workers les analysent : les deux mettent des messages en file, et l'API ne lit pas le réglage des workers. Un worker qui a un webhook le dit donc dans la base : à son démarrage, puis toutes les dix secondes, il repousse l'échéance de la mise en service (`webhook_stats.in_service_until`) de la borne d'âge de sa file. Tant qu'elle n'est pas passée, un changement d'état met son message en file ; au-delà, ou sans aucun worker qui ait jamais eu de webhook, rien n'est mis en file.

- **Un worker dédié suffit.** Les workers sans webhook n'y touchent pas : ils mettent en file les analyses qu'ils font, et un seul worker, ou quelques-uns, envoient. Tous ceux qui ont un webhook portent le même réglage.
- **Une panne des workers ne fait rien perdre.** Arrêtés quelques heures ou quelques jours, moins que la borne d'âge, ils retrouvent en file ce qui s'est passé entre-temps.
- **Retirer un webhook.** Sans worker qui le règle, il reste en service jusqu'à l'échéance : les messages s'accumulent en file, et [`answerlineage doctor`](cli.md#answerlineage-doctor) dit des webhooks en retard. Pour le retirer aussitôt, une fois le réglage retiré des workers :

  ```sql
  UPDATE webhook_stats SET in_service_until = NULL;
  DELETE FROM webhook_queue;
  ```

## Ce qui part

Un message par changement d'état, d'une liste fermée : celle des états qui existent aujourd'hui.

| `type` | Quand | `incident.analysis.state` | `incident.quarantine.state` |
| --- | --- | --- | --- |
| `incident.declared` | L'incident est déclaré, par l'API ou la ligne de commande, ou à la confirmation d'une proposition | `pending` | nul |
| `incident.analysed` | Son analyse d'impact est faite : les décomptes se lisent | `done` | celui de l'heure |
| `incident.analysis_failed` | Son analyse est en échec pour de bon, après ses essais : le code dit pourquoi | `failed` | celui de l'heure |
| `incident.quarantined` | Il est [mis en quarantaine](incidents.md#quarantaine) : les proxys reçoivent son périmètre | celui de l'heure | `active` |
| `incident.quarantine_lifted` | Sa quarantaine est levée | celui de l'heure | `lifted` |

Un [incident proposé](incidents.md#incidents-proposés-par-un-test-de-qualité) par un test de qualité en échec n'annonce rien : ni sa proposition, ni son écartement. Sa confirmation déclare l'incident, et annonce `incident.declared` comme toute déclaration, à l'heure de la confirmation, qui est celle de la déclaration ; le format ne change pas. Une analyse remise à plus tard après un échec ne change pas d'état et n'annonce rien. Une analyse [rejouée](incidents.md#rejouer-une-analyse) annonce de nouveau sa fin, sous un nouvel identifiant de livraison. Une activation ou une levée qui ne change rien, parce que l'incident était déjà dans l'état demandé, n'annonce rien.

```http
POST /answerlineage HTTP/1.1
Host: alertes.interne
Content-Type: application/json
AnswerLineage-Signature: t=1794819302,v1=2bf59d4b7b6c1f1d50e1616fe5fbc3f4eb9c153ce3aa8a51ed97ea7a99ddb4ce
```

```json
{
  "delivery_id": "1f7a9c3e-5b2d-4e8f-a6c4-0d2e4f6a8b1c",
  "emitted_at": "2026-11-16T08:55:02Z",
  "incident": {
    "analysis": {
      "counts": { "answers": 16, "lower_bound": false, "recipients": 13 },
      "error": null,
      "state": "done"
    },
    "id": "974e7c80-6643-4065-a541-e4c029abbdab",
    "quarantine": { "mode": "warn", "state": "active" },
    "severity": "high",
    "state": "qualified"
  },
  "occurred_at": "2026-11-16T08:55:01.204517Z",
  "type": "incident.quarantined",
  "version": 1
}
```

Le corps envoyé est ce message en JSON compact, clés triées, en ASCII ; il est présenté ici mis en forme. C'est l'exemple « quarantaine activée » des [vecteurs](#les-vecteurs), avec sa signature.

| Champ | Contenu |
| --- | --- |
| `version` | La version du format : `1` |
| `delivery_id` | Identifiant de livraison, unique par message et le même d'un envoi à l'autre : il écarte un doublon ou un rejeu |
| `emitted_at` | Heure d'émission de cet envoi, à la seconde, en UTC : celle que porte et que signe l'en-tête. Elle change d'un envoi à l'autre |
| `type` | Le changement d'état, de la liste ci-dessus |
| `occurred_at` | Heure du changement d'état, à la microseconde, en UTC : la déclaration, la fin de l'analyse, l'activation ou la levée de la quarantaine |
| `incident.id` | L'identifiant de l'incident, celui de `GET /api/v1/incidents/{id}` |
| `incident.state` | L'état de l'incident : `qualified` |
| `incident.severity` | La gravité déclarée : `low`, `medium`, `high`, `critical` |
| `incident.analysis.state` | `pending`, `done` ou `failed` |
| `incident.analysis.error` | Le code d'une analyse en échec, `query_timeout` ou `analysis_error` ; nul sinon |
| `incident.analysis.counts` | Pour une analyse faite : les réponses touchées, leurs destinataires distincts, et `lower_bound`, vrai quand une borne a été dépassée et que ces nombres sont des minimums ; nul sinon |
| `incident.quarantine` | La dernière quarantaine de l'incident à l'heure du changement : son état, `active` ou `lifted`, et son mode, `warn` ; nulle quand l'incident n'a jamais été mis en quarantaine |

**Aucun texte libre, aucun contenu.** Des identifiants, des codes de listes fermées, des heures et des nombres : ni le périmètre de l'incident, ni celui de sa quarantaine, ni les réponses touchées, ni les pseudonymes de leurs destinataires, qui ne sont que comptés. Le récepteur qui veut le détail le lit par l'API, `GET /api/v1/incidents/{id}`. Le message est figé à l'heure du changement d'état : un envoi tardif annonce ce qui était vrai alors.

**Seul ce qui est signé compte.** La requête ne porte que `Content-Type` et la signature ; tout ce que le récepteur doit savoir est dans le corps, que la signature couvre. L'adresse appelée est celle que règle l'exploitant.

Le format est décrit dans [`spec/webhooks/v1/Message.json`](../spec/webhooks/v1/Message.json), en JSON Schema 2020-12, objets fermés. La version 1 n'est pas encore publiée sur la branche principale : la quarantaine y est entrée, ses deux événements et le champ `incident.quarantine`, et ses vecteurs ont été signés de nouveau. Une fois publié, un fichier ne change plus : `make contract` refuse qu'il change ou disparaisse. Un nouvel état d'incident, une nouvelle sorte d'événement, comme les écarts, feront alors une nouvelle version, dans un autre dossier, avec son numéro dans `version`.

## La signature

```text
AnswerLineage-Signature: t=1794819302,v1=2bf59d4b7b6c1f1d50e1616fe5fbc3f4eb9c153ce3aa8a51ed97ea7a99ddb4ce
```

- `t` est l'heure d'émission, en secondes depuis le 1er janvier 1970 (UTC), en décimal ;
- `v1` est HMAC-SHA-256, en hexadécimal minuscule, sous le secret partagé encodé en UTF-8, de `t`, d'un point, puis du corps tel qu'il est envoyé, octet pour octet : `HMAC-SHA-256(secret, "1794819302." + corps)` ;
- pendant une [rotation](#rotation-du-secret), l'en-tête porte une signature `v1` par secret, le nouveau d'abord : `t=…,v1=…,v1=…`.

Chaque envoi est signé à son heure : un message renvoyé après une panne porte une nouvelle heure et une nouvelle signature, et le même identifiant de livraison.

## Vérifier un message

La fonction de vérification de référence est [`answerlineage_workers/webhook_signature.py`](../workers/src/answerlineage_workers/webhook_signature.py). Elle n'emploie que la bibliothèque standard de Python et n'importe rien d'AnswerLineage : un récepteur l'importe du paquet `answerlineage-workers`, ou copie le fichier tel quel.

```python
import json

from webhook_signature import HEADER, SignatureError, verify


def receive(headers, raw_body: bytes) -> int:
    try:
        verify(headers.get(HEADER), raw_body, [secret])  # [secret, ancien_secret] en rotation
    except SignatureError as refusal:
        journal.warning("webhook refusé : %s", refusal.code)
        return 401
    message = json.loads(raw_body)
    if deja_vu(message["delivery_id"]):
        return 200
    ...
    return 204
```

Dans un autre langage, la vérification suit ces pas :

1. Lire le corps brut, avant toute lecture du JSON : la moindre différence d'octets, un blanc, l'ordre des clés, change la signature.
2. Lire l'en-tête `AnswerLineage-Signature` : des éléments `clé=valeur` séparés par des virgules, sans espace ; `t` une fois, des chiffres seulement ; une à huit signatures `v1`, chacune de 64 caractères hexadécimaux minuscules ; les autres clés sont ignorées, pour qu'un schéma de signature à venir n'empêche pas de lire celui-ci. Un en-tête absent, de plus de 1 024 caractères, ou qui ne suit pas cette forme est refusé.
3. Pour chaque secret connu, calculer `HMAC-SHA-256(secret, t + "." + corps)` en hexadécimal minuscule, et le comparer à chaque signature `v1` en temps constant (`hmac.compare_digest`, `crypto.timingSafeEqual`, `MessageDigest.isEqual`…). Aucune ne correspond : refusé.
4. Comparer `t` à l'horloge du récepteur : plus de 300 secondes d'écart, avant comme après, et le message est refusé, même bien signé.
5. Écarter un `delivery_id` déjà vu : c'est un renvoi, ou un rejeu dans la fenêtre. Garder les identifiants vus au moins le temps de la tolérance suffit à écarter un rejeu ; les garder plus longtemps écarte aussi les renvois d'une panne.

| Code du refus | Cas |
| --- | --- |
| `malformed_header` | En-tête absent, trop long, ou qui ne suit pas la forme : sans `t`, `t` répété ou qui n'est pas un nombre, sans `v1`, signature tronquée ou en majuscules, espace après une virgule |
| `signature_mismatch` | Aucune signature n'est juste sous aucun des secrets : corps altéré, heure changée après la signature, secret autre ou retiré |
| `timestamp_outside_tolerance` | Bien signé, mais émis plus de 300 secondes avant ou après l'horloge du récepteur : un rejeu tardif, ou des horloges décalées |

La tolérance se règle chez le récepteur (`tolerance=`, en secondes) ; cinq minutes absorbent un décalage d'horloge ordinaire et le temps d'un envoi. Les horloges des workers et du récepteur se tiennent à l'heure, par NTP.

**Répondre vite.** Le récepteur répond `2xx` dès le message vérifié et enregistré, et le traite ensuite : au-delà de 30 secondes, l'envoi est coupé et refait. Il répond `400`, `413` ou `422` à un message qu'il n'acceptera jamais : il est alors abandonné ; tout autre statut, `401` et `403` compris, le fait renvoyer ([garanties](#garanties)).

### Les vecteurs

[`spec/webhooks/v1/vectors.json`](../spec/webhooks/v1/vectors.json) donne deux secrets de test, puis :

- `signing` : des messages de chaque sorte, l'heure d'émission, les secrets nommés, et l'en-tête qu'un émetteur doit produire, dont un pendant une rotation ;
- `verification` : un en-tête, un corps, les secrets que le récepteur connaît, son horloge (`now`), et la décision attendue, `valid`, ou le code du refus : un message valide, à la limite de la tolérance, pendant une rotation ; rejoué hors de la tolérance, venu du futur, signature fausse, corps altéré, heure changée, secret retiré, en-têtes mal formés.

Une vérification écrite dans un autre langage se juge en rejouant ces cas. La fonction de référence les passe tous, et ses signatures y ont été calculées sans elle.

## Rotation du secret

Les workers signent sous deux secrets le temps d'une rotation, et le récepteur en accepte autant qu'on lui en donne : changer de secret ne perd aucun message.

1. Tirer un nouveau secret.
2. Régler les workers qui ont le webhook avec le nouveau secret dans `ANSWERLINEAGE_WEBHOOK_SECRET` et l'ancien dans `ANSWERLINEAGE_WEBHOOK_PREVIOUS_SECRET`, et les redémarrer : chaque message porte deux signatures, et le récepteur, qui ne connaît encore que l'ancien, les accepte.
3. Passer le récepteur au nouveau secret.
4. Retirer `ANSWERLINEAGE_WEBHOOK_PREVIOUS_SECRET` des workers, et les redémarrer.

Dans l'autre ordre, le récepteur accepte d'abord les deux secrets (`verify(…, [nouveau, ancien])`), les workers passent au nouveau, puis le récepteur oublie l'ancien. Un message en file au moment du changement est signé à son envoi, sous les secrets du worker qui l'envoie.

La rotation reste à la main de l'exploitant : AnswerLineage ne la déclenche pas et n'impose aucune échéance. Le secret est symétrique : le récepteur le garde pour vérifier, et pourrait donc signer ; qui le lit chez lui, ou dans l'environnement des workers, forge des messages acceptés jusqu'à la rotation suivante. C'est un risque accepté du [modèle de menaces](securite/modele-de-menaces.md#acceptations) (`workers/webhook-usurpe`), à signer par le mainteneur.

## Garanties

**Rien ne part pour un changement annulé.** Le message entre en file dans la transaction qui change l'état : celle qui enregistre la déclaration, ou celle qui enregistre la fin de l'analyse. Une déclaration refusée, une transaction annulée, une analyse qu'un worker arrêté n'a pas validée ne laissent rien en file ; un changement validé y laisse son message.

**En moins d'une minute dans le cas normal.** La boucle des webhooks réclame un lot dès que le précédent est parti, et revient chaque seconde quand la file est vide : un message part une seconde environ après la validation de son changement d'état. Le test de bout en bout le mesure, l'incident déclaré par l'API, analysé par un worker, le récepteur vérifiant la signature : de l'ordre d'une seconde pour chacune des deux annonces (`tests/test_webhooks_end_to_end.py`). Une panne du récepteur, ou un worker à webhook arrêté, retarde l'envoi d'autant.

**Au moins une fois.** Un lot est réclamé, envoyé, puis retiré de la file avec ses compteurs, dans une seule transaction. Un envoi dont l'issue n'est pas sûre est refait : délai dépassé, connexion coupée, worker arrêté ou base perdue avant la validation. Le récepteur peut donc recevoir deux fois un message : sous le même `delivery_id`, qui l'écarte.

**Plusieurs workers, aucun double envoi.** Chaque worker réclame son lot avec `FOR UPDATE SKIP LOCKED` : un message réclamé par l'un est sauté par les autres jusqu'à la fin de sa transaction.

**Dans l'ordre de la file**, à un worker près : un worker envoie son lot dans l'ordre de mise en file ; deux workers envoient deux lots en même temps. L'annonce d'une analyse peut donc, rarement, arriver avant celle de sa déclaration : `occurred_at` les remet dans l'ordre.

**Un récepteur en panne ne ralentit ni l'API ni la projection.** La déclaration et la fin d'une analyse ajoutent une écriture à leur transaction. L'envoi tourne dans une boucle à part du worker, et ses entrées-sorties dans un fil à part : un récepteur qui ne répond pas retient cette boucle, jamais celle qui projette et analyse.

**Une panne arrête le lot au premier échec.** Le message en cause garde un essai de plus et la classe de l'échec, ou le statut de la réponse, jamais un message ; les suivants attendent sans essai compté. La reprise attend 1 s, puis le double à chaque échec, jusqu'à 5 minutes, chaque attente tirée au hasard entre sa moitié et son tout, pour que des workers ne reviennent pas ensemble. Un envoi réussi la remet à zéro.

| Réponse du récepteur | Ce que fait le worker |
| --- | --- |
| `2xx` | Remis : le message quitte la file, compté `sent` |
| `400`, `413`, `422` | Refusé : le récepteur ne l'acceptera pas mieux plus tard. Il quitte la file, compté perdu (`rejected`) |
| Tout autre statut (`401`, `403`, `404`, `429`, `5xx`), une redirection, une erreur réseau ou TLS, un délai dépassé, une adresse refusée | Échec : le lot s'arrête, le message reste en file et sera renvoyé après l'attente |

**Une file bornée.** Toutes les dix secondes, chaque worker qui a un webhook retire ce qui dépasse les bornes, les plus anciens d'abord : les messages en file depuis plus de `--webhook-max-age-hours`, puis ceux au-delà de `--webhook-max-messages`. Un message en cours d'envoi n'est pas retiré : il part, ou reste. Ce qui est retiré est compté perdu (`dropped`) dans la transaction qui le retire, et dit au journal. Un appelant de l'API qui déclarerait des incidents en nombre ne fait donc pas grossir la file au-delà de sa borne.

**À l'arrêt**, sur `SIGINT` ou `SIGTERM`, l'envoi en cours est coupé et son message reste en file, sans essai compté : il partira au prochain démarrage, au besoin une seconde fois.

## Ce qui se lit

La file est dans la base, `webhook_queue`, et ses compteurs dans `webhook_stats` ([schéma](schema.md#file-des-webhooks)). La [route de santé](cli.md#la-route-de-santé) les donne :

```json
{
  "webhooks": {
    "in_service": true,
    "queued": 1,
    "retrying": 1,
    "lower_bound": false,
    "oldest_age_seconds": 42,
    "sent": 37,
    "lost": 1,
    "rejected": 1
  }
}
```

| Champ | Contenu |
| --- | --- |
| `in_service` | Vrai tant qu'un worker qui a un webhook est passé depuis moins que la borne d'âge de sa file : un changement d'état est alors mis en file |
| `queued` | Messages qui attendent d'être envoyés |
| `retrying` | Parmi eux, ceux dont un envoi a échoué : ils seront renvoyés |
| `lower_bound` | Vrai au-delà de 10 000 messages : les deux décomptes précédents sont des minimums |
| `oldest_age_seconds` | Depuis combien de secondes le plus ancien attend ; nul quand la file est vide |
| `sent` | Messages remis depuis la mise en service |
| `lost` | Messages perdus depuis la mise en service : refusés, ou abandonnés par les bornes de la file |
| `rejected` | Parmi les perdus, ceux que le récepteur a refusés |

[`answerlineage doctor`](cli.md#answerlineage-doctor) signale des webhooks en retard (`webhooks_lagging`) quand le plus ancien message attend depuis plus de `--webhook-max-age` secondes, 300 par défaut, avec son remède : vérifier que le récepteur répond, que les workers le joignent, leur réglage et le secret. Sans webhook en service et sans message en file, la vérification n'est pas faite.

Le journal ne dit que des nombres, des statuts et des classes d'erreur : ni l'adresse du récepteur, ni le secret, ni rien d'un message. Une panne se dit une fois, à son premier échec, puis son retour :

```text
INFO answerlineage_workers Webhook en marche : destination en https, messages signés sous 1 secret(s), file bornée à 10000 messages et 168 h.
WARNING answerlineage_workers Webhook en échec (HTTP 503) : les messages attendent dans la file ; reprise dans 0.7 s, puis de plus en plus espacée.
INFO answerlineage_workers Webhook rétabli après 6 échec(s) : le récepteur accepte de nouveau les messages.
WARNING answerlineage_workers Webhook : 1 message(s) refusé(s) par le récepteur (HTTP 422) : abandonnés, comptés perdus.
WARNING answerlineage_workers Webhook : 3 message(s) abandonné(s), les plus anciens, au-delà des bornes de la file (10000 messages, 168 h) : comptés perdus.
```

## Les tests

- `workers/tests/test_webhook_signature.py` rejoue les vecteurs publiés contre la fonction de référence, refuse une signature rejouée hors de la fenêtre, une signature fausse et un corps altéré, accepte deux secrets pendant une rotation, et vérifie que le module n'importe que la bibliothèque standard.
- `workers/tests/test_webhooks.py`, devant un faux récepteur : rien en file sans webhook en service ni pour un changement annulé ; le message figé à l'heure du changement et conforme au format publié ; la quarantaine activée puis levée, annoncée sans son périmètre ; un récepteur en panne puis de retour, sans perte et sous le même identifiant de livraison ; un refus, le débordement, deux workers ; un récepteur qui ne répond pas ne retarde ni la projection ni l'analyse ; ni le secret, ni l'adresse, ni un contenu au journal ou en base ; le réglage refusé au démarrage sans être cité.
- `tests/test_webhooks_end_to_end.py` : l'incident déclaré par l'API, analysé par la boucle d'un worker ; les deux annonces arrivent signées au récepteur, qui les vérifie avec la fonction de référence, en moins d'une minute ; le récepteur refuse ensuite l'un des appels rejoué six minutes plus tard, une signature fausse et un corps altéré.

## Limites connues

- Une seule destination. Annoncer à deux récepteurs demande un relais devant eux.
- Rien n'est rattrapé : un changement d'état survenu hors service, avant le premier passage d'un worker à webhook ou après l'échéance, n'est jamais mis en file ; un message perdu par les bornes ou un refus n'est pas remis en file.
- Un webhook retiré des workers reste en service jusqu'à son échéance, une semaine par défaut, sauf à le retirer dans la base ([en service](#en-service)).
- Le secret est partagé et symétrique, sa rotation manuelle ; écarter un rejeu dans la fenêtre de tolérance revient au récepteur, par l'identifiant de livraison. Ce sont deux risques acceptés du modèle de menaces, à signer.
- La version 1 du format ne connaît que les états d'aujourd'hui. Les états que la liste de rappel ajoutera, et les écarts, feront une nouvelle version : un récepteur qui ne lit que la version 1 ne les recevra pas sans changement.
- Un envoi par message, une connexion par worker : le débit est celui du récepteur. Les incidents sont rares, la borne de la file est faite pour une panne, pas pour un flux.
- La transaction d'un lot reste ouverte pendant son envoi, 30 s au plus, plus le délai d'un envoi : elle ne tient que des lignes de la file des webhooks.
