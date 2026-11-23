# Liste de rappel

Quand l'analyse d'un [incident](incidents.md) est faite, AnswerLineage en dresse la liste de rappel : un message par destinataire et par incident, avec toutes les réponses touchées qu'il a reçues, de la personne la plus exposée à la moins exposée. Une personne qui a reçu cinq réponses touchées y figure une fois, avec ses cinq réponses : on ne la prévient qu'une fois.

Les destinataires y sont sous leur [pseudonyme](securite/pseudonymes.md), comme partout : la liste ne révèle personne, et ne porte aucun texte.

## Quand la liste est faite

Les [workers](workers.md#analyse-des-incidents) font la liste dans la transaction qui enregistre l'analyse : une analyse faite a sa liste, à la même heure, ou n'en a pas.

| L'analyse | La liste |
| --- | --- |
| En attente, ou en échec | Aucune |
| Faite, non tronquée | Faite, de son résultat |
| Faite, mais tronquée (`analysis.truncated`) | Aucune : une analyse qui a dépassé une de ses bornes ne dit pas tous les destinataires ([bornes](incidents.md#bornes)), et une liste partielle laisserait croire qu'elle les dit. Déclarer de nouveau l'incident sur une fenêtre plus courte |
| [Rejouée](incidents.md#rejouer-une-analyse) | Refaite, de son nouveau résultat ; tronquée, elle perd sa liste |

Faire la liste remplace la précédente : la même analyse donne la même liste, rang pour rang. Une analyse faite avant la révision `0018` de la base n'a pas de liste : la rejouer en fait une.

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
  "reference_at": "2026-11-16T08:53:16.814594Z",
  "counts": { "recipients": 13, "answers": 16 },
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
| `reference_at` | L'heure de référence de l'exposition : celle du graphe lu par l'analyse |
| `counts` | Les destinataires de la liste, et les réponses touchées distinctes qu'ils ont reçues |
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

Toute personne qui a un rôle lit la liste, comme elle lit les incidents ([matrice des droits](securite/authentification.md#la-matrice-des-droits)). Comme toute lecture du graphe, la route s'exécute sous le [délai maximal des lectures](lecture.md#délai-maximal) : au-delà, `503` (`query_timeout`).

| Statut | Code | Cas |
| --- | --- | --- |
| `404` | `unknown_incident` | Aucun incident ne porte cet identifiant ; il n'est pas recopié |
| `404` | `no_recall_list` | L'incident n'a pas de liste : son analyse attend, a échoué, ou est tronquée ([plus haut](#quand-la-liste-est-faite)) |
| `422` | `string_pattern_mismatch`, `greater_than_equal`… | Un identifiant, un curseur ou une taille de page mal formés ; la valeur reçue n'est jamais recopiée |

## Bornes

La liste a les bornes de l'analyse dont elle est faite : 200 000 liens d'une réponse touchée vers un destinataire au plus, au-delà desquels l'analyse est tronquée et n'a pas de liste. Une page rend au plus 200 entrées de 100 réponses chacune.

## Ce qui n'existe pas encore

- **La validation de la liste et l'envoi des rappels.** La liste se lit ; rien n'en part.
- **Une liste par agent.** Une liste réunit tous les destinataires de l'incident, quel que soit l'agent qui leur a répondu.
- **Le chemin de la ligne de commande et de l'explorateur** : la liste se lit par l'API.
