# 0015. La requête SQL relayée porte l'identifiant de run, sur option et par outil

Statut : en vigueur. Elle étend la [décision 0007](0007-proxy-transparent-sans-oauth.md), qui faisait du proxy un relais transparent.

## Contexte

Le lignage dit qu'un appel d'outil a lu telles tables, d'après l'analyse de sa requête ([décision 0006](0006-analyse-du-sql-dans-un-processus-a-part.md)). L'entrepôt, lui, garde ce qu'il a réellement exécuté, dans ses journaux d'audit et ses vues d'historique. Pour vérifier l'un par l'autre, ou pour retrouver dans l'entrepôt les lectures d'un run, il faut que la requête exécutée porte l'identifiant de run de l'appel.

Deux points peuvent l'y mettre. Le serveur MCP, qui crée le job : il peut poser un label BigQuery ou le `QUERY_TAG` de Snowflake, que les journaux gardent à côté de la requête, mais AnswerLineage n'écrit pas les serveurs MCP des autres. Et le proxy, qui voit passer l'appel et connaît son run, mais qui s'interdisait jusqu'ici de toucher au corps de la requête : un proxy qui réécrit change ce que l'agent a demandé, et toute réécriture est une surface où une donnée hostile, un contexte de trace piégé par exemple, peut atteindre le serveur.

## Décision

**Côté serveur, une convention et une fonction de référence.** Le label BigQuery `answerlineage_run_id` porte l'identifiant de run ; `QUERY_TAG` vaut `{"answerlineage_run_id":"<run>"}`. `proxy/src/answerlineage_proxy/warehouse.py` calcule l'identifiant depuis le contexte de trace reçu, comme le proxy le choisit (`_meta`, puis l'en-tête `traceparent`), et rend les trois formes ; il n'importe que la bibliothèque standard, pour se recopier dans un serveur ([Proxy MCP](../proxy.md#étiquetage-des-requêtes-de-lentrepôt)).

**Côté proxy, un commentaire, sur option et par outil.** `--sql-run-comment OUTIL` nomme un outil déclaré par `--sql-tool` ; sa requête relayée commence alors par `/* answerlineage_run_id=<run> */`. L'option est désactivée par défaut. Le commentaire vient d'un gabarit fixe, où n'entre que l'identifiant de run sous sa forme canonique vérifiée. Seule la chaîne de la requête change : le proxy trouve dans le corps la position du premier caractère de la requête, et recopie à l'octet près ce qui précède et ce qui suit (`tagging.py`) ; en HTTP, la longueur du corps suit (`app.py`) ; en stdio, le fil du client rassemble la ligne avant de l'écrire, et passe à l'inspection la ligne d'origine avec le contexte de trace qu'il a choisi (`stdio.py`). Ce que le proxy ne lit pas avec certitude (corps ambigu, compressé, trop grand, lié par un en-tête d'empreinte ou de signature, requête qui commence par une directive) part tel quel et se compte. L'analyse porte sur la requête d'origine ; l'événement dit seulement, par `taggedWithRunId`, si la requête a reçu le commentaire.

## Alternative écartée

**Réécrire le corps : le lire en JSON, ajouter le commentaire à la requête, le sérialiser de nouveau.**

C'est l'écriture la plus courte, et la plus fausse. Une nouvelle sérialisation change l'ordre des clés, les blancs, les échappements et l'écriture des nombres : le serveur ne recevrait plus le message de l'agent avec un commentaire en plus, mais un autre message, que deux lecteurs JSON, ou un en-tête qui en recopie une partie, pourraient ne pas lire pareil. Insérer le commentaire à sa place, sans rien relire, borne la modification à ce qu'elle doit être, et la rend vérifiable : le corps relayé, privé du commentaire, est le corps reçu.

Écrire le commentaire à la fin de la requête, comme le fait sqlcommenter, aurait laissé intact le début de la requête, que certains serveurs contrôlent. Mais un commentaire de fin de ligne (`-- …`) en fin de requête l'avalerait, un point-virgule final le laisserait hors de l'instruction, là où un pilote qui découpe les instructions peut l'écarter, et un journal qui tronque les requêtes longues le perdrait. En tête, il reste lisible, et les deux débuts qu'il dérangerait, la directive de dialecte de BigQuery et l'indice d'optimiseur, font renoncer à l'étiquetage.

Activer l'option pour tous les outils SQL, par défaut, aurait étiqueté plus de requêtes ; mais le proxy serait sorti de la transparence sans qu'on le lui demande, et le cache de résultats de l'entrepôt aurait cessé de servir les requêtes de tous les outils.

## Conséquences

- Le proxy n'est plus strictement transparent pour un outil qui a l'option : le serveur reçoit la requête précédée du commentaire. Un serveur qui n'accepte une requête que d'après ses premiers mots peut la refuser ; l'option s'essaie outil par outil. Le proxy journalise au démarrage chaque outil concerné.
- Une requête étiquetée ne profite plus du cache de résultats de BigQuery ni de Snowflake : son texte change à chaque run. Le label et `QUERY_TAG`, posés par le serveur, ne touchent pas au texte.
- La modification du corps est une surface nouvelle, inscrite au [modèle de menaces](../securite/modele-de-menaces.md) : un test vérifie que seul le commentaire diffère de ce que reçoit le serveur, un autre qu'un contexte de trace piégé n'atteint pas la requête.
- Le proxy relit le corps d'un appel de l'outil avant de le relayer : une vingtaine de microsecondes pour une requête ordinaire, quelques millisecondes pour un corps de la taille lue maximale, comptées dans la latence ajoutée. En stdio, une ligne attend sa fin avant d'être écrite.
- En stdio sans `_meta`, le serveur ne peut pas connaître le run : le commentaire, que le proxy écrit, le porte seul.
- Le rapprochement des lectures avec les journaux d'audit de l'entrepôt n'est pas fait : la convention fixe ce que la requête porte, pour qu'il puisse l'être.
