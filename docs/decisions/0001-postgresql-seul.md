# 0001. PostgreSQL seul pour le graphe et la file

Statut : en vigueur.

## Contexte

Le produit relie des données, des appels d'outils, des réponses et des personnes : un graphe, qu'il faut alimenter à partir d'un flux d'événements que rien ne garantit ordonné ni unique, puis interroger dans les deux sens (d'une réponse vers ses sources, d'une donnée fausse vers les réponses touchées). Il doit s'installer dans une entreprise sans lui imposer un service cloud propriétaire, et fonctionner hors ligne : la démonstration tient dans un `docker compose up`.

Deux besoins semblent appeler chacun un service à part : un stockage de graphe pour l'interroger, et un bus de messages pour décorréler la réception des événements de leur traitement.

## Décision

PostgreSQL est la seule dépendance obligatoire. Il tient :

- le graphe de lignage, en tables relationnelles ([schéma](../schema.md)) ;
- les événements reçus tels quels (`event_raw`), qui permettent de reconstruire le graphe en les rejouant ;
- la file de traitement (`ingest_queue`) : l'ingestion écrit l'événement brut et sa ligne de file dans la même transaction, un worker réclame un lot avec `FOR UPDATE SKIP LOCKED`, le projette et le retire de la file dans une seule transaction (`workers/src/answerlineage_workers/queue.py`) ;
- les verrous qui évitent un double travail entre workers (verrous consultatifs, pour le contrôle des émetteurs muets et la maintenance des partitions).

Les parcours du graphe sont des requêtes récursives bornées (`WITH RECURSIVE`, dans `lineage/trace.py`, `impact.py`, `overview.py`), sous un délai maximal que PostgreSQL impose lui-même (`statement_timeout`).

## Alternative écartée

**Une base de graphe pour le graphe, un bus de messages pour la file.**

Le graphe du produit est étroit : un arbre de runs (une demande, ses appels, ses sous-agents) dont les feuilles lisent des jeux de données et dont la racine répond à des personnes. Les deux questions posées en sont des remontées d'arbre, que des requêtes récursives bornées expriment sans langage de graphe. Une base de graphe ajouterait un second service à installer, à sauvegarder et à surveiller, pour des parcours que PostgreSQL fait.

Un bus aurait séparé la réception du traitement, mais au prix de deux écritures qu'aucune transaction ne lie : un événement reçu et perdu entre le bus et la base, ou écrit deux fois. Dans PostgreSQL, l'événement brut et sa ligne de file entrent ensemble ou pas du tout, et le lot d'un worker est traité et retiré de la file ensemble ou pas du tout : ni perte, ni double traitement.

## Conséquences

- Un seul service à installer et à exploiter : une base. L'API, les workers, la démonstration et les tests d'intégration n'ont besoin que d'elle.
- Les écritures du graphe sont idempotentes et commutatives (`INSERT … ON CONFLICT`, [schéma](../schema.md#écrire-dans-le-graphe)) : c'est ce qui rend sûr le rejeu depuis `event_raw`, l'envoi au moins une fois des émetteurs et les workers concurrents.
- Le débit d'ingestion est celui d'une base : la file et le graphe se partagent ses ressources. Il n'y a pas de bus à mettre à l'échelle séparément.
- Un événement toujours fautif reste dans la file, de plus en plus espacé (5 s, puis le double, jusqu'à une heure) ; il n'y a pas encore de file de rebut à consulter ([Workers](../workers.md#limites-connues)).
- Un parcours du graphe n'a pas de borne naturelle : chaque lecture porte la sienne (10 000 runs par réponse, 20 000 lectures pour une propagation…) et dit `truncated` quand elle la franchit, au lieu de rendre un résultat incomplet sans le dire ([bornes](../lecture.md#bornes)).
- Si les parcours devaient devenir ceux d'un graphe général (dépendances transitives entre tables dérivées), il faudrait réévaluer cette décision : la propagation ne suit pas encore ce qu'un run touché a écrit ([limites](../lecture.md#limites-connues)).
