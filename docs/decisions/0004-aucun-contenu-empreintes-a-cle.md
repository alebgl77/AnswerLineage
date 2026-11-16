# 0004. Aucun contenu stocké : des empreintes à clé seulement

Statut : en vigueur.

## Contexte

Ce que les agents lisent et disent est ce qu'il y a de plus sensible : des prompts, des résultats d'outils, des réponses, des requêtes SQL pleines de valeurs, des jetons dans les en-têtes. Un produit qui retrouve les réponses touchées par une donnée fausse n'a pas besoin de ce contenu : il lui faut pouvoir dire que deux appels ont les mêmes arguments, que deux résultats sont identiques, qu'une réponse est celle-ci, qu'un appel a lu telle clé.

Un produit qui garde le contenu devient lui-même ce qu'il faut protéger, et un lieu de plus où le contenu peut fuir. Les bibliothèques journalisent d'ailleurs parfois ce contenu à notre place : une URL complète, une requête SQL, des en-têtes.

## Décision

Aucun contenu de prompt, de résultat d'outil ni de réponse n'est stocké, journalisé ni mis en métrique. Les événements ne portent que des noms, des empreintes, des tailles, des pseudonymes et des heures.

- **Empreintes à clé.** Celles des arguments et du résultat d'un appel sont des HMAC-SHA-256 calculés avec une clé secrète propre au locataire, avec une étiquette par usage (`answerlineage/fingerprint/arguments/v1`, `…/result/v1`, `…/key/v1`) et la forme canonique JSON (RFC 8785) des arguments. Sans clé, aucune empreinte n'est émise : jamais de repli sur un hachage sans clé, qu'une énumération inverserait. Les schémas des facettes n'acceptent que la forme `hmac-sha256:`. Les vecteurs de test sont publiés (`spec/fingerprint/vectors-v1.json`) et ont été établis par une implémentation indépendante ([Proxy MCP](../proxy.md#empreintes-à-clé)).
- **SQL sans littéraux.** D'une requête, l'événement ne garde que sa forme, construite sur l'arbre syntaxique : toute constante devient `?`. Au moindre doute, aucune forme n'est gardée.
- **Erreurs par catégorie et code**, jamais par message. **Clés lues** (règles de lecture) sous forme d'empreinte. **Identifiants** recopiés seulement s'ils ont la forme d'un nom.
- **Journaux.** Les bibliothèques qui citeraient une URL, des en-têtes ou du SQL (`httpx`, `httpcore`, `sqlglot`) sont coupées pour tout le processus, et le journal d'accès du serveur HTTP aussi. Chaque composant a un test canari qui le vérifie, et le [kit de conformité](../conformite.md) cherche des canaris dans les événements, les journaux et les métriques d'un émetteur.

## Alternative écartée

**Stocker les contenus, chiffrés.**

Un contenu chiffré reste un contenu : il faut en garder la clé, en décider la durée de conservation, répondre de sa fuite si la clé fuit, et le produire à qui peut l'exiger. Le produit y gagnerait de pouvoir montrer ce que l'agent a lu ou dit, ce que son besoin n'exige pas : retrouver les réponses touchées et leurs destinataires passe par des rapprochements, pas par des lectures.

Les empreintes à clé font ces rapprochements. Deux locataires ont deux clés : la même valeur donne chez eux deux empreintes que personne ne peut rapprocher. Qui n'a pas la clé ne retrouve pas une valeur peu variée en essayant des candidates ; qui l'a retrouve, parmi les appels reçus, ceux qui ont lu la ligne d'une donnée reconnue fausse, sans que la valeur entre dans AnswerLineage.

## Conséquences

- L'explorateur et les API n'affichent aucun contenu, parce qu'elles n'en ont pas : une réponse est une heure et une empreinte, un résultat d'outil une taille et une empreinte.
- La clé est un secret du locataire, fourni par fichier ou par variable d'environnement, jamais sur la ligne de commande ; elle n'est pas transmise au serveur MCP lancé en stdio. **Changer de clé rompt le rapprochement** : les nouvelles empreintes ne se comparent plus aux anciennes. Une rotation se fait à une date connue.
- Sans clé, on perd le rapprochement : le proxy le dit une fois au démarrage.
- Une empreinte du résultat porte sur les octets relayés : le même résultat compressé ou non, ou découpé autrement en événements SSE, a deux empreintes.
- Ce qui reste est limité mais réel : un nom d'un seul mot choisi par l'auteur d'une requête (un alias, `AS "Dupont"`) est conservé tel quel dans la forme du SQL ; un nom qui n'a pas la forme d'un identifiant fait renoncer à la forme. Le `trace-id` et le `parent-id` d'un contexte de trace bien formé sont recopiés.
- L'empreinte d'une réponse est celle que donne l'émetteur de la diffusion : le schéma de la facette de diffusion dit « jamais le contenu » sans imposer l'algorithme.
- Le principe se garde par des tests et des canaris plus que par un mécanisme : une bibliothèque nouvelle qui journaliserait du contenu doit être coupée, et un canari ajouté.
