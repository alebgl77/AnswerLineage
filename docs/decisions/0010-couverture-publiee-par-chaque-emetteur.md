# 0010. La couverture : ce que chaque émetteur a vu, publié et comparé au graphe

Statut : en vigueur.

## Contexte

Le produit répond à « quelles réponses cette donnée fausse a-t-elle touchées ? ». Une réponse dont l'appel n'a pas été capté ne peut pas être trouvée : l'émetteur était arrêté, l'événement s'est perdu en route, une requête n'a pas pu être lue. Dans tous ces cas, le résultat « aucune réponse touchée » ou « 16 réponses touchées » est faux ou incomplet sans que rien le montre, et un rappel qui s'appuierait dessus rassurerait à tort : celui qui a lu la réponse manquante ne sera pas prévenu.

Le produit ne peut pas, seul, savoir ce qu'il n'a pas reçu.

## Décision

Chaque émetteur publie ce qu'il a vu, fenêtre par fenêtre (une minute pour le proxy), dans un relevé : appels vus, événements émis, événements perdus, requêtes non inspectées (`POST /api/v1/coverage`, [Relevés de couverture](../ingestion.md#relevés-de-couverture)). Une fenêtre sans appel est publiée aussi : c'est le battement qui prouve que l'émetteur n'est pas muet.

L'API compare ces relevés au graphe, et le graphe fait foi : un événement perdu en route se voit même si l'émetteur croit l'avoir émis (1 000 appels vus, 1 000 émis, 990 arrivés donnent 99 %). Le résultat d'une propagation ou d'une remontée dit sa couverture, sur la période qui le concerne :

| État | Sens |
| --- | --- |
| `measured` | Des relevés couvrent toute la période : le taux et les trous disent ce qui a été capté |
| `partial` | Des relevés n'en couvrent qu'une part |
| `unknown` | Aucun relevé ne recoupe la période : la couverture n'est pas mesurée |

Chaque trou connu est nommé (événements perdus, événements absents du graphe, requêtes non inspectées, silence, début avant le premier relevé), avec son émetteur et sa fenêtre. Une période sans relevé n'est jamais présentée comme couverte. Un worker contrôle chaque minute les émetteurs et signale muet celui qui n'a rien publié depuis dix minutes ([Workers](../workers.md#émetteurs-muets)).

Les consommateurs en tirent leur verdict : le code de sortie de `answerlineage impact` distingue « aucune réponse touchée, et le résultat le prouve » (`1`) de « résultat incomplet » (`3`) ; l'explorateur écrit « partielle » ou « non mesurée » plutôt que 100 %, et arrondit le taux par défaut (99,96 % s'écrit 99,9 %, jamais 100 %).

## Alternative écartée

**Ne rien dire de ce qui manque.**

C'est le plus simple : le graphe dit ce qu'il contient. Mais « il y a 16 réponses » ne dit rien sur la dix-septième, et un produit de rappel est jugé sur celles qu'il oublie. Se taire sur l'incomplétude transforme chaque panne de capture en fausse assurance. Il vaut mieux un résultat qui dit « je ne peux pas conclure » qu'un résultat net et faux.

## Conséquences

- Un émetteur qui ne publie pas de relevé rend sa période « non mesurée » : écrire un émetteur, c'est aussi publier ses relevés ; le [kit de conformité](../conformite.md) le contrôle.
- Les relevés se fusionnent au plus grand (idempotents, commutatifs), et plusieurs instances d'un même émetteur s'additionnent au lieu de se confondre : un émetteur ne peut donc que corriger ses compteurs à la hausse.
- Les relevés ne sont pas authentifiés : un émetteur mal réglé ou qui en usurpe un autre peut gonfler les appels vus d'une fenêtre, et la fusion au plus grand ne le défait pas.
- La couverture compte les appels d'outils, pas les runs d'agents, et ne voit que ce que voient les émetteurs qui publient : un appel qui ne passe par aucun d'eux n'est compté nulle part.
- Un émetteur retiré pour de bon reste connu, et son silence rend partielle la couverture des périodes qui suivent : rien ne permet encore de le déclarer retiré.
- La couverture lue avec `known_at` est celle d'aujourd'hui : l'état passé des relevés n'est pas gardé.
- Les relevés et les événements bruts sont purgés avec la conservation : la couverture d'une période purgée est inconnue plutôt que fausse.
- Un événement reçu mais pas encore projeté n'est pas dans le graphe : juste après un envoi, la dernière fenêtre peut paraître incomplète le temps qu'un worker passe.
