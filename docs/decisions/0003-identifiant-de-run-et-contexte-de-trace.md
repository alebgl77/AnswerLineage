# 0003. Identifiant de run dérivé du contexte de trace W3C

Statut : en vigueur.

## Contexte

Un run a besoin d'un identifiant stable que tout émetteur qui voit le même appel puisse calculer. Deux proxys en série devant un même serveur, ou un proxy et un adaptateur d'agent, voient le même appel d'outil : s'ils lui donnent chacun un identifiant tiré au hasard, le graphe en a deux, et rien ne dit que ce sont les mêmes. Il faut aussi pouvoir passer du lignage à la trace d'un outil d'observabilité, et inversement.

Un proxy ne doit pas pour autant modifier ce qu'il relaie : ajouter un en-tête, ou en inventer un, change la requête que reçoit le serveur MCP.

## Décision

L'identifiant de run d'un appel d'outil est une fonction pure de son contexte de trace W3C (`traceparent`). Le proxy le lit, dans cet ordre, dans `params._meta.traceparent` du corps, puis dans l'en-tête HTTP `traceparent` s'il est unique et bien formé ; sinon il crée un contexte, qui ne sert qu'à l'identifiant (et qu'il ajoute à la requête relayée seulement quand l'appel n'en portait aucun).

L'identifiant est un UUID version 8 (RFC 9562) : les 16 premiers octets de SHA-256 de `answerlineage/run-id/v1`, du `trace-id` et du `parent-id`, avec les bits de version et de variante posés. Le calcul tient en cinq étapes, publié à l'octet près ([Proxy MCP](../proxy.md#la-dérivation-à-loctet-près)), avec des vecteurs de test dans `spec/run-id/vectors-v1.json` ; le [kit de conformité](../conformite.md) le refait pour juger un émetteur. `traceId` et `spanId` sont aussi gardés avec le run, pour passer du graphe à la trace.

## Alternative écartée

**Un identifiant propre à AnswerLineage, propagé par un en-tête nouveau.**

Il aurait donné un identifiant à la forme choisie, mais il aurait fallu le faire adopter : chaque client MCP, chaque serveur et chaque relais auraient dû le transmettre, et le proxy aurait dû l'ajouter à toute requête qui n'en portait pas, c'est-à-dire modifier ce qu'il relaie pour toutes. Un en-tête de plus se perd à la première étape qui ne le connaît pas.

Le contexte de trace W3C est déjà là, dans les clients instrumentés, dans les relais et dans les outils d'observabilité. S'y accrocher donne à tout émetteur qui voit le contexte le même identifiant sans se concerter : leurs événements fusionnent, parce que les écritures du graphe sont idempotentes. Et lignage et traces se corrèlent sans nouvel en-tête.

Un UUID version 5 aurait aussi convenu. Il impose SHA-1, qu'on n'emploie plus dans une construction nouvelle, et un UUID d'espace de noms à publier en plus : la version 8, que la RFC recommande pour un nom haché par un autre algorithme, laisse SHA-256.

## Conséquences

- Le proxy n'ajoute un en-tête `traceparent` que pour un appel arrivé sans contexte ; il ne remplace ni ne double jamais un en-tête existant, et ne modifie jamais le corps.
- Un client qui réutilise le même span pour deux appels d'outil leur donne le même run : le graphe les fusionne en un seul, qui porte les lectures des deux mais l'outil du premier événement projeté ([limites du proxy](../proxy.md#limites-connues)).
- Une reprise HTTP garde son run quand le contexte vient de `_meta`. Quand il vient de l'en-tête d'un client qui ouvre un span par tentative, ou quand le proxy le crée, chaque tentative est un run distinct.
- Sans authentification des émetteurs, un identifiant dérivé n'est pas une preuve d'origine : qui connaît le contexte d'un appel peut émettre un événement pour son run.
- Une autre dérivation, demain, change d'étiquette (`…/v2`) et de fichier de vecteurs : l'étiquette versionnée est faite pour cela.
- Le proxy ne connaît pas le run de l'agent : il ne rattache pas lui-même un appel au run de l'agent. `traceId` relie déjà les appels d'une même trace, et un émetteur qui connaît l'agent peut poser le `parent`.
