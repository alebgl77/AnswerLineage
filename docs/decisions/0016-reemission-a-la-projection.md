# 0016. Réémission vers un catalogue OpenLineage : mise en file à la projection, dans PostgreSQL

Statut : en vigueur.

## Contexte

Un responsable de données veut voir les agents dans le catalogue OpenLineage que l'entreprise a déjà (Marquez, DataHub, OpenMetadata, Dataplex) : AnswerLineage doit réémettre les événements qu'il reçoit vers ce consommateur externe. Le catalogue est un tiers : il tombe, ralentit, refuse, et rien de ce qui lui arrive ne doit ralentir l'ingestion ni la projection, ni faire perdre un événement sans le dire. PostgreSQL est la seule dépendance ([décision 0001](0001-postgresql-seul.md)) : la file de réémission y tient, comme la file de traitement.

Reste à choisir quand un événement entre dans cette file. Deux moments se présentent : l'ingestion, dans la transaction qui enregistre l'événement brut, ou la projection, dans celle qui l'intègre au graphe. Ce qui est réémis doit être ce qui a été accepté, et un événement au rebut ne doit pas partir.

## Décision

**Un événement entre en file de réémission dans la transaction qui le projette** et le retire de la file de traitement (`workers/src/answerlineage_workers/queue.py`, `_DONE_AND_REEMITTED`), quand le worker a une destination. Ce qui part est exactement ce qui est entré au graphe : ni un événement refusé, ni un doublon, ni un événement au rebut ; un événement rejoué du rebut part une fois projeté. La file de traitement dit si un événement est déjà entré au graphe (`ingest_queue.reemit`) : la reconstruction du graphe remet les événements en file à faux, et ils ne repartent pas (`rebuild.py`).

**La file est une table, `reemission_queue`, vidée par une boucle des workers** à côté de celle qui projette (`reemission.py`). Elle réclame un lot avec `FOR UPDATE SKIP LOCKED`, l'envoie un événement après l'autre sous des délais bornés, et le retire de la file avec ses compteurs (`reemission_stats`) dans la même transaction : au moins une fois, sans double envoi entre workers. Une panne arrête le lot au premier échec et la reprise attend de plus en plus, au hasard ; la file est bornée en âge et en nombre, et ce qui déborde est compté et dit.

**La destination est réglée par l'exploitant** et contrôlée au démarrage puis à chaque connexion (`destination.py`) : `https`, ou `http` sur la boucle locale ; ni adresse de métadonnées ni lien local ; aucune redirection suivie ; un en-tête d'authentification lu de l'environnement et jamais journalisé. Le client n'emploie que la bibliothèque standard, dans un fil à lui.

## Alternatives écartées

**Mettre l'événement en file à l'ingestion**, dans la transaction qui l'enregistre. C'est le moment le plus tôt, et il ne dépend pas des workers : l'API saurait tout ce qui est à réémettre. Mais l'ingestion ne sait pas encore si l'événement entrera au graphe : un événement que la projection mettra au rebut serait parti, ou en route, avant que le rebut le retienne, et le catalogue décrirait un lignage qu'AnswerLineage n'a pas. Il faudrait alors, à l'envoi, demander à la projection ce qu'elle en a fait, et attendre qu'elle l'ait fait. L'ingestion paierait aussi une écriture de plus pour chaque événement, et l'API devrait connaître le réglage de la destination, qui est celui des workers. La projection, elle, sait ce qui est entré au graphe, dans la transaction même où elle l'y met.

**Envoyer depuis la transaction de la projection**, sans file : chaque lot attendrait le catalogue, et une destination en panne arrêterait le graphe. **Une file en mémoire dans le worker**, comme la boîte d'envoi du proxy ([décision 0005](0005-capture-qui-ne-bloque-jamais.md)) : un worker arrêté perdrait ce qu'il n'avait pas envoyé, sans rien qui le compte, et deux workers ne se partageraient pas le travail. Le proxy est sur le chemin des agents et doit rester sans état ; le worker a déjà la base.

## Conséquences

- Un événement part après sa projection : le catalogue suit le graphe d'AnswerLineage, avec le retard de la file de traitement. Une projection arrêtée arrête aussi la réémission.
- Le réglage de la destination doit être le même pour tous les workers, et pour `answerlineage-worker rebuild` : un worker sans destination projette sans mettre en file, et ce qu'il projette n'est pas réémis.
- Rien n'est rattrapé : ce qui a été projeté avant le réglage de la destination, ou perdu, ne repart pas.
- La transaction d'un lot reste ouverte le temps de son envoi, borné : elle ne tient que des lignes de la file de réémission, que la borne de la file et les autres workers sautent.
- Au moins une fois : un catalogue reçoit parfois deux fois le même événement. Le protocole d'OpenLineage le tolère ; le test de bout en bout le vérifie sur Marquez.
- La réémission est une requête sortante vers un tiers, inscrite au [modèle de menaces](../securite/modele-de-menaces.md) : fuite vers une destination mal réglée, détournement de la destination, déni de service par la file.
- Voir [Réémission vers un catalogue OpenLineage](../reemission.md).
