# 0007. Un proxy transparent, qui décide sur le corps, ne termine pas OAuth et ne garde aucun jeton

Statut : en vigueur.

## Contexte

Pour capter sans modifier les agents, il faut se placer sur le chemin entre un agent et un serveur MCP. Deux risques en découlent. Celui de gêner : un proxy qui réécrit, retient ou refuse change le comportement de l'agent et du serveur. Et celui de devenir une cible : un composant qui voit tout le trafic, et qui détiendrait des identifiants, concentrerait ce qu'un attaquant cherche.

Un proxy peut aussi se faire tromper sur ce qu'il relaie : les en-têtes MCP (`Mcp-Method`, `Mcp-Name`) peuvent contredire le corps de la requête, que le serveur finira par exécuter.

## Décision

**Transparent.** Le proxy relaie toutes les méthodes, le chemin, les paramètres, les en-têtes et le corps de la requête tels que reçus, et le corps de la réponse octet pour octet, au fil de l'eau. Il n'ajoute rien, à une exception près : le `traceparent` d'un appel d'outil arrivé sans contexte de trace ([décision 0003](0003-identifiant-de-run-et-contexte-de-trace.md)). Il fonctionne en HTTP ou en stdio, sans toucher à l'agent ni au serveur ; en stdio, le client lance le proxy à la place du serveur.

**Il décide sur le corps, jamais sur les en-têtes.** Il lit le corps des requêtes `POST` pour reconnaître un appel d'outil. Les en-têtes MCP ne servent qu'à être comparés au corps : un écart est signalé dans l'événement (`headerMismatch`), et c'est le corps qui est décrit, comme le serveur l'exécutera. Ce que le proxy ne peut pas lire sans ambiguïté (corps illisible, clé répétée, lot JSON-RPC, corps compressé) est relayé tel quel et compté dans `uninspected` : il ne devine pas.

**Il ne termine pas OAuth et ne garde aucun jeton.** Le défi `401` d'un serveur protégé, ses métadonnées OAuth et les échanges d'un client avec le serveur d'autorisation passent tels quels : le proxy n'y répond jamais lui-même, ne s'authentifie pas à la place du client et ne retient aucun jeton d'un appel au suivant. Il refuse en revanche, avant tout relais, une requête dont l'hôte (`Host`) ou l'origine (`Origin`) n'est pas attendu, pour qu'une page web ouverte sur le même poste ne l'atteigne pas.

## Alternative écartée

**Une passerelle centrale, par laquelle tous les agents passent.**

Une passerelle verrait tout au même endroit et pourrait appliquer des règles, mais elle devrait s'insérer dans l'authentification : terminer OAuth ou tenir des jetons pour les serveurs qu'elle fronte. Elle concentrerait des identifiants de tout le parc, deviendrait un point de défaillance unique pour tous les agents, et demanderait de reconfigurer chaque agent pour la joindre. Avec un proxy transparent placé devant chaque serveur, ou lancé à sa place en stdio, ni l'agent ni le serveur ne changent, et plusieurs proxys devant un même serveur se partagent un émetteur, chacun sous son instance.

## Conséquences

- Un jeton, un cookie, un en-tête d'authentification traversent le proxy sans y laisser de trace : un test les fait traverser tout le parcours, du proxy au graphe, puis les cherche dans les événements, la base entière, les réponses de l'API, les compteurs et tous les journaux (`tests/test_no_token_anywhere.py`).
- Le proxy ne sait pas quel agent appelle : il décrit des appels d'outils, pas des agents. Il ne rattache pas un appel au run de l'agent ; un émetteur qui connaît l'agent (un adaptateur) le fait.
- Un client qui n'est pas un navigateur n'envoie ni `Origin` ni `Sec-Fetch-*` : rien ne change pour lui. Un proxy joint sous un autre nom, derrière un service par exemple, doit déclarer ce nom (`--allow-host`).
- Les requêtes que le proxy ne sait pas lire passent sans événement : la capture est ouverte, et ce que le lignage ignore se publie dans les [relevés de couverture](0010-couverture-publiee-par-chaque-emetteur.md).
- Le résultat de l'appel n'est pas lu, sauf pour y trouver une erreur JSON-RPC : une erreur d'outil rendue comme un résultat (`isError`) compte comme un succès.
- Ni le proxy ni aucun émetteur ne s'authentifie auprès de l'API aujourd'hui : l'API n'est pas faite pour être exposée hors d'un réseau de confiance.
