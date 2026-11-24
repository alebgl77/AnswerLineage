# Liste de rappel

Quand l'analyse d'un [incident](incidents.md) est faite, AnswerLineage en dresse la liste de rappel : un message par destinataire et par incident, avec toutes les réponses touchées qu'il a reçues, de la personne la plus exposée à la moins exposée. Une personne qui a reçu cinq réponses touchées y figure une fois, avec ses cinq réponses : on ne la prévient qu'une fois.

Les destinataires y sont sous leur [pseudonyme](securite/pseudonymes.md), comme partout : la liste ne révèle personne, et ne porte aucun texte.

Aucun rappel ne part sans validation humaine. Un responsable d'agent [valide la liste, la modifie ou la refuse](#décider-du-rappel), et sa décision est journalisée ; sans décision au bout de 48 heures, la liste [remonte au responsable de données](#la-remontée). Tant qu'elle n'est pas validée, [la base refuse qu'un message parte](#aucun-envoi-sans-validation-la-base-le-tient), même à qui la joint directement.

## Quand la liste est faite

Les [workers](workers.md#analyse-des-incidents) font la liste dans la transaction qui enregistre l'analyse : une analyse faite a sa liste, à la même heure, ou n'en a pas.

| L'analyse | La liste |
| --- | --- |
| En attente, ou en échec | Aucune |
| Faite, non tronquée | Faite, de son résultat |
| Faite, mais tronquée (`analysis.truncated`) | Aucune : une analyse qui a dépassé une de ses bornes ne dit pas tous les destinataires ([bornes](incidents.md#bornes)), et une liste partielle laisserait croire qu'elle les dit. Déclarer de nouveau l'incident sur une fenêtre plus courte |
| [Rejouée](incidents.md#rejouer-une-analyse) | Refaite, de son nouveau résultat ; tronquée, elle perd sa liste. Une liste déjà décidée est gardée telle quelle |

Faire la liste remplace la précédente : la même analyse donne la même liste, rang pour rang. Une analyse faite avant la révision `0018` de la base n'a pas de liste : la rejouer en fait une. Une liste décidée ne change plus : ce qui a été validé est ce qui partira ([plus bas](#une-liste-décidée-ne-change-plus)).

## Ce qu'elle tient

- **Une entrée par destinataire et par incident**, sous son pseudonyme, avec chacune des réponses touchées qu'il a reçues, par leur identifiant stable, leur empreinte et leur heure. Une réponse diffusée deux fois à la même personne, par deux canaux, ne compte qu'une fois.
- **Seulement les réponses qui ont un destinataire connu.** Une réponse touchée diffusée sans destinataire, dans un canal ou gardée, n'a personne à prévenir : l'analyse la compte, la liste ne la porte pas. Une liste peut donc être vide : il n'y a personne à rappeler.
- **Un instantané.** La liste tient ses propres réponses : la relire ne relit pas le graphe, et un événement arrivé après l'analyse n'y entre qu'avec une analyse rejouée.

## L'ordre : l'exposition d'un destinataire

Les entrées vont de la plus exposée à la moins exposée. L'exposition d'un destinataire reprend les poids du [score d'exposition](lecture.md#score-dexposition) : chaque réponse touchée qu'il a reçue pèse 1, et ce poids diminue de moitié tous les 30 jours depuis l'heure de la réponse.

```text
exposition d'un destinataire = somme, sur ses réponses touchées, de 2^(−âge / 30 jours)
```

C'est exactement ce que le destinataire ajoute au score d'exposition des données de l'incident. Le score d'un jeu de données pèse chaque réponse « 1 + destinataires » : le 1 est la réponse elle-même, chaque destinataire en ajoute un, et ce qu'il ajoute est son exposition. Une personne qui a reçu des réponses récentes vient avant celle qui n'en a reçu que d'anciennes, comme une donnée lue récemment vient avant une donnée lue il y a longtemps.

- **L'heure de référence** est celle du graphe lu par l'analyse (`known_at`), rappelée par la liste dans `reference_at` : la même analyse donne la même exposition, quel que soit le jour où la liste est refaite.
- **Le calcul est celui du score d'exposition** : chaque poids en billionièmes entiers, leur somme exacte, arrondie au millionième, la moitié vers le haut.
- **Un ordre total.** À exposition égale au millionième : celui qui a reçu le plus de réponses touchées, puis celui dont la réponse touchée la plus récente l'est le plus, puis le pseudonyme, dans l'ordre de ses caractères. Deux entrées n'ont jamais le même rang.

Deux facteurs ont été écartés de l'ordre. **La gravité** de l'incident est la même pour toutes les entrées de sa liste : elle ne départage pas deux destinataires. **Le niveau de précision des lectures** ne pèse pas : le score d'exposition ne s'en sert pas, la propagation s'arrête aux tables, et une lecture d'un sous-ensemble a pu lire justement les lignes fausses ; abaisser son poids repousserait une personne touchée, à l'inverse du côté sûr que tient la propagation.

## Lire la liste

```bash
curl http://127.0.0.1:8000/api/v1/incidents/974e7c80-6643-4065-a541-e4c029abbdab/recall-list
```

```json
{
  "incident_id": "974e7c80-6643-4065-a541-e4c029abbdab",
  "severity": "high",
  "built_at": "2026-11-16T08:53:16.814594Z",
  "counts": { "recipients": 13, "answers": 16 },
  "state": "pending",
  "decision": null,
  "escalated_at": null,
  "dispatched": 0,
  "reference_at": "2026-11-16T08:53:16.814594Z",
  "entries": [
    {
      "rank": 1,
      "recipient": "hmac-sha256:7b610b7f4a9cbdc6834286107e9880f34f87954fe21d1335ed374305b6414eed",
      "exposure": 4.504771,
      "answer_count": 5,
      "latest_answer_at": "2026-11-12T09:21:10Z",
      "answers": [
        {
          "id": "01a1cf0ef39076a1a2d32ae717979aaa588a8305ce3bb598",
          "fingerprint": "sha256:264b99abc8f7a4170628fab29e7ae12bc327b71347d2a9a10a9e48f4af98c879",
          "produced_at": "2026-11-12T09:21:10Z"
        },
        "…"
      ],
      "more_answers": false
    },
    {
      "rank": 2,
      "recipient": "hmac-sha256:a71b66a14ed697845fee1dc093de55a03ba82be3e176ea2b57e58d874685884d",
      "exposure": 4.499475,
      "answer_count": 5,
      "latest_answer_at": "2026-11-12T10:47:56Z",
      "answers": ["…"],
      "more_answers": false
    },
    "…"
  ],
  "more": false,
  "next_cursor": null
}
```

Cet exemple est la liste du premier incident de l'[entreprise synthétique](corpus.md), déclaré sur la démonstration : ses treize destinataires, chacun avec ses réponses touchées. Les deux premiers ont reçu chacun cinq réponses ; le premier passe devant, ses réponses étant un peu plus récentes. `tests/test_recall_end_to_end.py` compare cette liste à la vérité de l'entreprise, et recalcule l'exposition de chaque destinataire sans le produit.

| Champ | Contenu |
| --- | --- |
| `severity` | La gravité de l'incident |
| `built_at` | L'heure de la transaction qui a fait la liste avec l'analyse, posée par la base |
| `counts` | Les destinataires de la liste, et les réponses touchées distinctes qu'ils ont reçues |
| `state` | `pending` : sans décision ; `validated`, `modified` ou `refused` : décidée ([plus bas](#décider-du-rappel)) |
| `decision` | La décision, son auteur, son heure, son gabarit ou son motif ; nulle tant qu'elle attend |
| `escalated_at` | L'heure à laquelle la liste est [remontée](#la-remontée) au responsable de données ; nulle sinon |
| `dispatched` | Les messages de rappel mis à l'envoi : aucun tant que la liste n'est pas validée ou modifiée |
| `reference_at` | L'heure de référence de l'exposition : celle du graphe lu par l'analyse |
| `entries[].rank` | Le rang, à partir de 1, dans l'ordre décrit plus haut |
| `entries[].recipient` | Le pseudonyme du destinataire |
| `entries[].exposure` | Son exposition, au millionième |
| `entries[].answer_count`, `latest_answer_at` | Ses réponses touchées, toutes comptées, et l'heure de la plus récente |
| `entries[].answers` | Ses réponses touchées, les plus récentes d'abord, 100 au plus ; `more_answers` vaut `true` au-delà |
| `more`, `next_cursor` | Des entrées moins exposées suivent ; le curseur de la page suivante |

| Paramètre | Rôle | Défaut |
| --- | --- | --- |
| `limit` | Entrées par page, de 1 à 200 | 100 |
| `cursor` | La page suivante : la valeur de `next_cursor`, rendue telle quelle | aucun |

Toute personne qui a un rôle lit la liste, sa décision et son auteur compris, comme elle lit les incidents ([matrice des droits](securite/authentification.md#la-matrice-des-droits)). Comme toute lecture du graphe, la route s'exécute sous le [délai maximal des lectures](lecture.md#délai-maximal) : au-delà, `503` (`query_timeout`).

| Statut | Code | Cas |
| --- | --- | --- |
| `404` | `unknown_incident` | Aucun incident ne porte cet identifiant ; il n'est pas recopié |
| `404` | `no_recall_list` | L'incident n'a pas de liste : son analyse attend, a échoué, ou est tronquée ([plus haut](#quand-la-liste-est-faite)) |
| `422` | `string_pattern_mismatch`, `greater_than_equal`… | Un identifiant, un curseur ou une taille de page mal formés ; la valeur reçue n'est jamais recopiée |

## Décider du rappel

Un responsable d'agent garde la main sur ce qui est dit aux utilisateurs des agents : il valide la liste telle quelle, la modifie en choisissant un autre gabarit de message, ou la refuse pour un motif. Il ne l'écrit jamais : chaque champ vient d'une liste fermée, et aucun texte libre ne peut atteindre un destinataire.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/incidents/974e7c80-6643-4065-a541-e4c029abbdab/recall-list/decision \
  -H 'content-type: application/json' -d '{"decision": "validated"}'
```

```json
{
  "incident_id": "974e7c80-6643-4065-a541-e4c029abbdab",
  "changed": true,
  "state": "validated",
  "decision": {
    "decision": "validated",
    "decided_at": "2026-11-16T09:12:40.218733Z",
    "author": { "issuer": "https://idp.interne/realms/lignage", "subject": "8d1f…" },
    "template": "standard",
    "reason": null
  },
  "escalated_at": null,
  "dispatched": 0
}
```

| `decision` | Ce qu'elle dit | Champ qui l'accompagne |
| --- | --- | --- |
| `validated` | La liste part telle quelle, sous le gabarit standard | aucun |
| `modified` | La liste part sous le gabarit choisi | `template`, obligatoire |
| `refused` | Rien ne part | `reason`, obligatoire |

| `template` | Ce que dira le message |
| --- | --- |
| `standard` | La réponse reçue s'appuyait sur une donnée reconnue fausse : ne plus s'y fier. C'est le gabarit d'une liste validée |
| `information` | Elle a pu s'appuyer sur une donnée reconnue fausse : pour information, sans action attendue |
| `decision_review` | Si elle a fondé une décision, la revoir |

| `reason` | Motif du refus |
| --- | --- |
| `not_affected` | Les réponses n'ont pas repris la donnée fausse |
| `already_informed` | Les destinataires sont déjà prévenus, par un autre moyen |
| `no_impact` | L'écart est sans effet sur ce qui leur a été dit |
| `duplicate_incident` | Un autre incident les rappelle déjà |

Le message lui-même n'est pas écrit par AnswerLineage aujourd'hui : un gabarit désigne ce qu'il dira, et l'envoi, à venir, le remplira de champs fixes, les réponses par leur date et leur canal.

- **Définitive.** Une liste a au plus une décision. La même, redemandée, est rendue telle quelle, `changed` à `false`, sans rien journaliser de plus ; une autre est refusée (`409`). Une erreur se corrige par un nouvel incident.
- **Journalisée.** La décision, l'émetteur OIDC et le sujet (`sub`) du jeton de son auteur, l'heure de la base, le gabarit ou le motif s'écrivent dans `recall_decision`, en ajout seul ([plus bas](#le-journal-des-décisions)). L'auteur vient du jeton, jamais du corps : un champ `author` est refusé comme tout champ inconnu.
- **Verrouillée.** La décision prend un verrou sur la liste le temps de sa transaction, comme les workers qui la refont ou la font remonter : une décision ne croise ni une liste refaite, ni sa remontée.

| Statut | Code | Cas |
| --- | --- | --- |
| `403` | `insufficient_role` | Le jeton n'a pas le rôle de responsable d'agent ni d'administrateur ([qui décide](#qui-décide)) |
| `403` | `author_unidentified` | Le jeton ne porte pas de sujet admis : 255 caractères ASCII imprimables au plus, sans blanc ni rien qui forme une balise. Une décision se journalise sous le nom de son auteur ; l'authentification désactivée de la démonstration n'en a pas |
| `404` | `unknown_incident`, `no_recall_list` | Aucun incident ne porte cet identifiant, ou il n'a pas de liste |
| `409` | `recall_list_decided` | La liste porte déjà une autre décision |
| `422` | `literal_error` (`/decision`, `/template`, `/reason`), `missing`, `unexpected_field`, `unknown_field`, `not_an_object` | Une valeur hors de sa liste, un gabarit ou un motif manquant, donné pour une décision qui n'en prend pas, un champ inconnu ; la valeur reçue n'est jamais recopiée, ni le nom d'un champ inconnu |
| `400`, `413`, `415` | `invalid_json`, `body_too_large`, … | Le corps n'est pas du JSON, ou dépasse 4 Kio |

Rien n'est journalisé pour une demande refusée.

### Qui décide

Le responsable d'agent décide, et l'administrateur, qui a ce qu'ont les rôles des personnes. Le lecteur, le responsable de données, le gestionnaire d'incident et l'auditeur lisent la liste et sa décision, sans décider ([matrice des droits](securite/authentification.md#la-matrice-des-droits)).

Les rôles ne portent pas de périmètre par agent : un responsable d'agent décide de la liste de tout incident, quel que soit l'agent qui a répondu à ses destinataires. Rien ne le restreint aux utilisateurs de son agent ; sa décision reste journalisée, à son nom. C'est une [acceptation du modèle de menaces](securite/modele-de-menaces.md#acceptations) (`api/decision-de-rappel-usurpee`).

### Le journal des décisions

Chaque décision laisse une ligne dans `recall_decision`, et chaque remontée aussi, sans auteur ([schéma](schema.md#listes-de-rappel)). La base tient l'ajout seul, comme pour le [journal des révélations](securite/pseudonymes.md#le-journal) : l'heure est posée par elle à l'insertion, quoi que l'insertion porte, et une modification, une suppression ou une troncature est refusée, propriétaire de la table compris (`ck_recall_decision_append_only`). Le journal se lit avec la liste (`decision`, `escalated_at`) et dans la liste des listes ; il désigne l'incident, et survit à une liste qu'une analyse rejouée retire.

## La remontée

Une liste qui a des destinataires et reste sans décision 48 heures après avoir été faite remonte au responsable de données. Les [workers](workers.md#remontée-des-listes-de-rappel) le constatent chaque minute ; le délai se règle (`--recall-decision-hours`, d'une heure à 720).

- **Journalisée.** La remontée est une ligne du journal des décisions, à l'heure de la base, sans auteur : `escalated_at` sur la liste.
- **Une fois.** Une liste remonte une fois, quel que soit le nombre de workers. Une liste décidée ne remonte pas, ni une liste vide, qui n'a rien à décider.
- **Elle ne décide rien.** La liste reste en attente : un responsable d'agent la décide ensuite, et le journal garde l'une et l'autre. Le responsable de données ne décide pas à la place du responsable d'agent.

Le responsable de données la voit par l'API :

```bash
curl "http://127.0.0.1:8000/api/v1/recall-lists?state=pending&escalated=true"
```

```json
{
  "lists": [
    {
      "incident_id": "974e7c80-6643-4065-a541-e4c029abbdab",
      "severity": "high",
      "built_at": "2026-11-16T08:53:16.814594Z",
      "counts": { "recipients": 13, "answers": 16 },
      "state": "pending",
      "decision": null,
      "escalated_at": "2026-11-18T08:54:02.513250Z",
      "dispatched": 0
    }
  ],
  "more": false,
  "next_cursor": null
}
```

`GET /api/v1/recall-lists` rend les listes sans leurs entrées, de l'incident le plus grave au moins grave, puis la plus ancienne d'abord : c'est là que la gravité d'un incident ordonne les rappels. Toute personne qui a un rôle les lit.

| Paramètre | Rôle | Défaut |
| --- | --- | --- |
| `state` | Seulement les listes dans cet état : `pending`, `validated`, `modified`, `refused` | toutes |
| `escalated` | `true` : seulement les listes remontées ; `false` : seulement les autres | toutes |
| `limit` | Listes par page, de 1 à 100 ; `more` vaut `true` quand d'autres suivent | 20 |
| `cursor` | La page suivante : la valeur de `next_cursor`, rendue telle quelle | aucun |

Un paramètre mal formé, ou un curseur qui ne désigne aucune page (`cursor_invalid`), est refusé en `422`, sans être recopié.

## Aucun envoi sans validation, la base le tient

L'envoi des rappels n'existe pas encore ; la table où un message se met à l'envoi, `recall_dispatch`, existe, et reste vide. C'est la base, pas seulement le code, qui y refuse un message sans validation :

- **Une contrainte de déclencheur** (`ck_recall_dispatch_validated`) refuse toute écriture d'un message dont la liste n'est pas validée ou modifiée, quelle que soit la connexion, propriétaire de la table compris. Ni une remontée ni un refus n'ouvrent l'envoi. Un code qui contournerait l'API, une requête écrite à la main, sont refusés de même : `api/tests/test_recall_decisions.py` le vérifie par une écriture directe.
- **Un message par destinataire et par incident**, un destinataire de la liste : la table n'en admet pas deux, ni un pseudonyme hors de la liste.
- `dispatched`, sur la liste, compte ces messages : `0` tant qu'elle n'est pas validée.

### Une liste décidée ne change plus

Ce qui a été validé est ce qui partira. Dès qu'une décision existe, la base refuse toute écriture dans la liste, ses entrées et leurs réponses (`ck_recall_list_frozen`) ; une remontée ne la fige pas. Une analyse rejouée ensuite, même tronquée, ne la refait ni ne la retire, et le journal des workers le dit.

## Bornes

La liste a les bornes de l'analyse dont elle est faite : 200 000 liens d'une réponse touchée vers un destinataire au plus, au-delà desquels l'analyse est tronquée et n'a pas de liste. Une page rend au plus 200 entrées de 100 réponses chacune.

## Ce qui n'existe pas encore

- **L'envoi des rappels.** Une liste validée peut partir, la base le permet ; rien ne l'envoie encore, ni ne révèle ses destinataires à l'envoi.
- **Une liste, une décision par agent.** Une liste réunit tous les destinataires de l'incident, quel que soit l'agent qui leur a répondu, et tout responsable d'agent la décide.
- **La remontée poussée.** Elle s'écrit au journal et se lit par l'API ; aucun [webhook](webhooks.md) ne l'annonce, ni une décision, et `answerlineage doctor` ne la signale pas.
- **Défaire une décision.** Une décision est définitive : une erreur se corrige par un nouvel incident.
- **Le chemin de la ligne de commande et de l'explorateur** : la liste se lit et se décide par l'API.
- **L'ajout seul contre un superutilisateur.** Le journal des décisions, la liste figée et le refus d'un envoi sans validation tiennent contre l'API et son rôle, pas contre un superutilisateur de la base, qui peut désactiver les déclencheurs.
