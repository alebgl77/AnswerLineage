# Destinataires pseudonymisés et révélation journalisée

Un destinataire, la personne qui a reçu la réponse d'un agent, n'entre dans AnswerLineage que sous son pseudonyme : un HMAC de son identité, sous une clé que la base ne contient pas. Aucune table ne relie un pseudonyme à une personne. Quand il faut savoir qui c'est, pour le prévenir d'un rappel ou répondre à sa demande, un gestionnaire d'incident le révèle : il apporte des identités candidates, l'API dit laquelle a ce pseudonyme, et l'action est journalisée, avec son auteur et son motif. C'est l'invariant 8 du produit : les destinataires sont pseudonymisés, sans table de correspondance.

Ce document dit la forme du pseudonyme et son calcul, la clé et sa rotation, la révélation, son journal, et les limites connues. Le code est dans `api/src/answerlineage_api/pseudonyms/`.

## Le pseudonyme

### Sa forme

`hmac-sha256:` suivi de 64 chiffres hexadécimaux minuscules :

```text
hmac-sha256:95ed34146f303882fcaf60a3490bf4e89eb0875290df1c636d9c4849016557a1
```

C'est la seule forme admise, partout :

- **à l'ingestion**, un destinataire de la facette [`answerlineage_delivery`](../facettes.md#answerlineage_delivery) qui n'a pas cette forme rejette l'événement, avec le code `recipient_not_pseudonym` : une adresse, un nom, un numéro (en texte ou en nombre), un objet, une liste de destinataires qui n'est pas une liste. La valeur n'est recopiée ni dans la réponse, ni dans la base, ni dans un journal ([ingestion](../ingestion.md#identifiants)) ;
- **à la projection**, un destinataire qui n'a pas cette forme est ignoré : un événement reçu avant que la forme soit exigée, rejoué par une [reconstruction](../workers.md#reconstruire-le-graphe), n'en fait pas entrer ;
- **à la montée vers la révision `0017`**, ce qu'un émetteur avait pu envoyer sous une autre forme est retiré de la base : des événements bruts, du graphe et des résultats des incidents ([schéma](../schema.md#journal-des-révélations)). Une telle valeur est peut-être une identité en clair, et ne se convertit pas sans elle.

### Son calcul, à l'octet près

C'est l'émetteur qui calcule le pseudonyme, là où il connaît l'identité : l'adaptateur de l'agent, ou le service qui diffuse la réponse. L'identité ne traverse jamais l'API.

1. **La forme canonique de l'identité** : l'identité mise en minuscules (correspondance par défaut d'Unicode), puis normalisée en NFC, encodée en UTF-8. `Camille.Martin@Exemple.FR` et `camille.martin@exemple.fr` ont donc le même pseudonyme, comme deux écritures d'un même accent.
2. **Le message** : l'étiquette `answerlineage/pseudonym/recipient/v1` en ASCII, un octet nul, puis la forme canonique.
3. **Le pseudonyme** : `hmac-sha256:` suivi de HMAC-SHA-256(`clé`, `message`) en hexadécimal minuscule.

Une identité vide, de plus de 320 caractères une fois canonique, ou qui porte un blanc, un séparateur, un caractère de contrôle, de formatage ou un demi-codet n'a pas de pseudonyme : une adresse, un identifiant de messagerie ou un numéro n'en portent pas, et un caractère invisible ferait deux pseudonymes d'une même personne. L'émetteur ne l'envoie pas.

En Python, sans rien d'AnswerLineage :

```python
import hashlib, hmac, unicodedata


def pseudonym(key: bytes, identity: str) -> str:
    canonical = unicodedata.normalize("NFC", identity.lower()).encode("utf-8")
    message = b"answerlineage/pseudonym/recipient/v1\x00" + canonical
    return "hmac-sha256:" + hmac.new(key, message, hashlib.sha256).hexdigest()
```

Les vecteurs de test sont publiés dans [`spec/pseudonym/vectors-v1.json`](../../spec/pseudonym/vectors-v1.json) : deux clés, des identités, leur forme canonique et leurs pseudonymes, et des identités qui n'en ont pas. `answerlineage_api.pseudonyms.vault.pseudonym_of` est l'implémentation de référence.

## La clé et le coffre à clés

### Une clé de locataire, propre aux pseudonymes

Le HMAC se calcule sous une clé secrète, propre au locataire : la même personne a chez deux locataires deux pseudonymes sans rapport, et qui ne détient pas la clé ne retrouve pas une identité en essayant celles d'un annuaire. Un hachage sans clé se retournerait par cette simple énumération.

Ce n'est pas la [clé des empreintes du proxy](../proxy.md#la-clé) : qui tiendrait celle-ci dans l'API pourrait aussi éprouver les arguments des appels d'outils. Les deux clés sont distinctes, et l'API ne tient que celle des pseudonymes.

La clé ne vit jamais dans la base : ni en clair, ni chiffrée, ni dans une table de réglages. Elle vit dans le **coffre à clés**, que tiennent l'émetteur (pour calculer) et l'API (pour révéler). Les workers n'en ont pas besoin.

### Le régler

| Variable | Rôle |
| --- | --- |
| `ANSWERLINEAGE_PSEUDONYM_KEYS_FILE` | Un fichier qui ne contient que les clés, une par ligne |
| `ANSWERLINEAGE_PSEUDONYM_KEYS` | Les clés, séparées par des virgules ou des blancs |

L'un ou l'autre, pas les deux. Chaque clé s'écrit en hexadécimal, 32 à 64 octets (64 à 128 caractères) ; le coffre en tient de une à huit, la courante d'abord, puis les précédentes gardées pour révéler ce qu'elles ont pseudonymisé. Pour en tirer une :

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Un réglage illisible arrête l'API au démarrage, avec un message qui nomme le réglage, jamais une clé ni le chemin du fichier. La clé n'apparaît dans aucun journal, aucune réponse, aucune représentation du coffre ; elle ne se sérialise pas. Sans réglage, l'API démarre, et toute révélation répond `503 pseudonym_keys_unavailable`.

### Une interface prête pour un KMS

Le coffre (`PseudonymVault`) a une seule opération : le pseudonyme d'une identité canonique sous chacune de ses clés. Il ne rend jamais une clé. Le coffre d'aujourd'hui (`LocalVault`) lit les clés au démarrage et les garde en mémoire ; un KMS qui calcule un HMAC sans livrer sa clé (une clé HMAC dans un module matériel ou un service de gestion de clés) se branche à sa place, par `create_app(pseudonym_vault=…)`, sans rien changer d'autre. L'opération est asynchrone pour lui laisser le temps d'un appel.

### La rotation

Changer de clé change le pseudonyme de chaque personne. Une rotation se fait donc à une date connue :

1. tirer la nouvelle clé, et la placer **en tête** du coffre de l'API, l'ancienne derrière ;
2. faire passer les émetteurs à la nouvelle clé, à la date choisie ;
3. garder l'ancienne clé dans le coffre de l'API tant qu'il faut pouvoir révéler les destinataires d'avant la date ;
4. la retirer ensuite : les pseudonymes calculés sous elle ne se révèlent plus, par personne. Retirer une clé, c'est oublier qui étaient ces destinataires.

Conséquence : une personne qui a reçu des réponses de part et d'autre de la date a deux pseudonymes, et compte pour deux destinataires dans une propagation ou une analyse d'incident qui chevauche la date. La révélation, elle, essaie chaque clé du coffre.

## La révélation

### Pourquoi sans table de correspondance

L'invariant interdit de garder l'identité, même chiffrée à côté du pseudonyme. Deux façons de révéler ont été pesées :

- **L'identité chiffrée sous une clé du coffre**, gardée à côté du pseudonyme. La révélation serait un simple déchiffrement, mais la base deviendrait une table de correspondance différée : qui obtient la base (une sauvegarde, un volume, un accès de lecture) puis, un jour, la clé ou le droit de déchiffrer, lit **toutes** les identités d'un coup, sans rien demander à personne. L'identité serait aussi une donnée personnelle de plus dans la base, ses sauvegardes et sa conservation. Le critère « la base seule, sans le coffre, ne relie aucun pseudonyme à une personne » ne tiendrait plus que par la clé.
- **Des identités candidates**, retenue. La personne qui révèle apporte les identités qu'elle soupçonne, tirées de son annuaire ou du canal de la diffusion (la liste des destinataires d'un message, que connaît la messagerie) ; l'API recalcule leur HMAC avec la clé du coffre, et dit laquelle a le pseudonyme visé. Rien n'est gardé que le journal de l'action. La base, même avec la clé, ne livre aucune identité : il faut encore les essayer, une à une, et l'API borne et journalise chaque essai.

### Révéler

`POST /api/v1/recipients/reveal`, réservé au **gestionnaire d'incident** ([matrice des droits](authentification.md#la-matrice-des-droits)).

```json
{
  "pseudonym": "hmac-sha256:95ed34146f303882fcaf60a3490bf4e89eb0875290df1c636d9c4849016557a1",
  "reason": "recall",
  "candidates": ["dominique.durand@exemple.fr", "camille.martin@exemple.fr"]
}
```

| Champ | Ce qu'il porte |
| --- | --- |
| `pseudonym` | Le pseudonyme visé, tel que l'API le donne (une réponse touchée, la vue en cinq colonnes) |
| `reason` | Le motif, d'une liste fermée, jamais un texte libre : `recall` (prévenir le destinataire d'un rappel), `data_subject_request` (répondre à une demande de la personne concernée), `legal_obligation` (répondre à une obligation légale), `incident_investigation` (instruire un incident) |
| `candidates` | De 1 à 100 identités candidates, de 320 caractères au plus chacune, sous les règles de la [forme canonique](#son-calcul-à-loctet-près) |

La réponse dit le résultat et le **rang** de la candidate, jamais l'identité, que l'appelant a déjà :

```json
{ "revelation": 42, "result": "matched", "candidate": 1 }
```

| Statut | Quand | Journalisé |
| --- | --- | --- |
| `200`, `matched` | Une candidate a ce pseudonyme, sous l'une des clés du coffre ; `candidate` est son rang, à partir de zéro | oui |
| `200`, `no_match` | Aucune candidate ne l'a ; `candidate` est nul | oui |
| `404 unknown_recipient` | Aucun destinataire connu, du graphe ou d'un incident, ne porte ce pseudonyme | oui, `unknown_recipient` |
| `403 insufficient_role` | Le jeton n'a pas le rôle de gestionnaire d'incident : un lecteur, un responsable de données, un administrateur | non |
| `403 author_unidentified` | Le jeton ne porte pas de sujet (`sub`) admis : 255 caractères ASCII imprimables au plus, sans blanc ni rien qui forme une balise. Une révélation se journalise sous le nom de son auteur | non |
| `503 pseudonym_keys_unavailable` | Le coffre à clés n'est pas réglé | non |
| `400`, `413`, `415`, `422` | Un corps illisible, de plus de 256 Kio, d'un autre type, ou une demande invalide. Une erreur cite le chemin et un code (`pseudonym_malformed`, `candidate_not_identity`, `no_candidate`, `too_many_items`, `unknown_field`…), jamais la valeur reçue | non |
| `429 revelation_rate_limited` | L'auteur a fait 20 révélations dans l'heure glissante ; `Retry-After` dit dans combien de secondes une place se libère | non |

L'ordre des contrôles : le jeton et le rôle, l'auteur, le coffre, puis le corps ; le HMAC des candidates se calcule hors de la base, et s'arrête à la première qui correspond. Puis, dans une seule transaction, un verrou consultatif propre à l'auteur, le décompte de ses révélations de l'heure, la recherche du destinataire, et la ligne du journal : deux révélations simultanées d'une même personne se comptent l'une après l'autre, et la borne tient quel que soit le nombre de réplicas de l'API, puisqu'elle se lit dans le journal lui-même.

Les candidates ne sont gardées nulle part : ni dans la base, ni dans un journal, ni dans la réponse, ni dans une erreur. Le journal n'en garde que le nombre.

### Qui révèle

Le gestionnaire d'incident instruit les incidents et prévient leurs destinataires : il révèle. Le lecteur, le responsable de données et le responsable d'agent ne révèlent pas. L'**administrateur non plus**, alors qu'il a d'ordinaire tout ce qu'ont les rôles de personnes : il exploite la plateforme, et peut en approcher la base et le coffre à clés ; s'il révélait aussi, un seul compte, ou un seul jeton volé, réunirait l'accès aux pseudonymes, à la clé et au droit de révéler. Il lit le journal, avec l'auditeur, pour contrôler qui révèle.

## Le journal

Chaque révélation laisse une ligne dans `recipient_revelation`, et rien d'autre :

| Colonne | Ce qu'elle tient |
| --- | --- |
| `id` | Numéro de la révélation, tiré par la base |
| `revealed_at` | L'heure, posée par la base à l'insertion, quoi que l'insertion porte |
| `author_issuer`, `author_subject` | L'auteur : l'émetteur OIDC et le sujet (`sub`) de son jeton |
| `reason` | Le motif, de la liste fermée |
| `pseudonym` | Le pseudonyme visé |
| `candidates` | Le nombre d'identités candidates, de 1 à 100 |
| `result` | `matched`, `no_match` ou `unknown_recipient` |

Jamais l'identité révélée, ni les candidates. Le pseudonyme est une valeur, sans clé étrangère : le journal survit à la [reconstruction du graphe](../workers.md#reconstruire-le-graphe). Il n'est ni partitionné, ni purgé.

**En ajout seul.** La base le tient elle-même, pour toute connexion, propriétaire de la table compris : un déclencheur pose l'heure à l'insertion ; une modification, une suppression ou une troncature est refusée comme une contrainte (`ck_recipient_revelation_append_only`). Les droits de modifier, supprimer et tronquer sont retirés à tous, propriétaire compris. En production, l'API se connecte de préférence avec un rôle qui n'a, sur cette table, que `INSERT` et `SELECT`, et les migrations avec un autre, propriétaire du schéma.

**Le lire.** `GET /api/v1/recipients/revelations`, réservé à l'auditeur et à l'administrateur : une page de révélations, la plus récente d'abord, `limit` (de 1 à 100, 20 par défaut) et `cursor` (celui que la page précédente donne dans `next_cursor`).

```json
{
  "revelations": [
    {
      "id": 42,
      "revealed_at": "2026-11-23T10:14:03.512Z",
      "author": { "issuer": "https://idp.interne/realms/lignage", "subject": "8d1f…" },
      "reason": "recall",
      "pseudonym": "hmac-sha256:95ed3414…",
      "candidates": 2,
      "result": "matched"
    }
  ],
  "next_cursor": "41"
}
```

## La démonstration

La démonstration ne révèle rien : son authentification est désactivée (aucun appel n'a d'auteur identifié, la révélation répond `403 author_unidentified`) et aucun coffre n'est réglé. Les destinataires de l'[entreprise synthétique](../corpus.md) ont la forme d'un pseudonyme, tirée de la graine : il n'y a personne derrière.

## Limites connues

- **Qui détient la clé peut parcourir un annuaire.** Le processus de l'API, un émetteur, ou qui administre le coffre, peut calculer hors de toute route le pseudonyme de chaque entrée d'un annuaire et retrouver ainsi les destinataires : sans borne ni journal. C'est la limite de toute pseudonymisation par HMAC ; un KMS qui ne livre pas la clé la réduit à qui a le droit de l'appeler.
- **Par la route, un gestionnaire d'incident peut éprouver 2 000 candidates par heure** (20 révélations de 100), chaque essai journalisé. La borne est par auteur : plusieurs comptes la multiplient. Une révélation refusée pour le débit n'est pas journalisée.
- **Le motif est déclaratif** : il vient d'une liste fermée, mais rien ne vérifie qu'il est vrai, ni que le destinataire visé est celui d'un incident. Le journal permet de le contrôler après coup.
- **Un pseudonyme est stable sous une clé** : il relie entre elles les réponses reçues par une même personne. C'est ce qui permet de compter les destinataires d'un incident ; c'est aussi un profil, sous pseudonyme.
- **La forme canonique ne réunit que la casse et la normalisation** : deux adresses d'une même personne, un alias, une adresse avec un suffixe (`camille+lignage@…`) font deux pseudonymes.
- **L'ajout seul tient contre l'API et son rôle, pas contre un superutilisateur de la base**, qui peut désactiver les déclencheurs ou se rendre les droits retirés.
- **Le schéma publié de la facette de diffusion** (version 1-0-0) n'écrit pas la forme du pseudonyme : il admet un texte de 1 à 255 caractères. L'API l'exige ; une version suivante du schéma pourra la porter.
- **Un champ libre d'une facette**, ou une facette inconnue, n'est pas contrôlé : un émetteur qui y écrirait une identité la ferait entrer dans l'événement brut. Le [kit de conformité](../conformite.md) le cherche par des canaris.
- **Aucune commande de la ligne de commande ne révèle encore** : la route s'appelle directement, avec le jeton d'un gestionnaire d'incident.
- **La révélation à l'envoi d'un rappel** n'existe pas encore, faute de rappel : elle s'appuiera sur le même coffre.
