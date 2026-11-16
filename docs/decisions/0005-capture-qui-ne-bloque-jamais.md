# 0005. Une capture qui ne bloque jamais et reste ouverte en panne, par une boîte d'envoi en mémoire

Statut : en vigueur.

## Contexte

Le proxy est sur le chemin de chaque appel d'outil d'un agent. Si AnswerLineage tombe, ralentit ou refuse ses événements, l'agent ne doit rien en voir : un outil de lignage qui arrête le travail qu'il observe serait débranché au premier incident.

Mais une capture qui ne bloque jamais peut perdre : un événement que l'API ne reçoit pas est un appel que le lignage ignore, et « aucune réponse touchée » ne vaut que si l'on sait ce qui a été vu.

## Décision

**Rien ne retarde un appel.** Le relais rend la réponse au fil de l'eau, octet pour octet. L'événement est construit après l'échange, l'analyse du SQL tourne ensuite à part, et les empreintes de la réponse se calculent morceau par morceau, après l'envoi de chacun au client.

**Une boîte d'envoi en mémoire.** Avec `--api-url`, les événements et les relevés de couverture partent vers l'API par une boîte locale, bornée (10 000 événements et 32 Mio par défaut), hors du chemin des appels : prendre un événement en charge est un ajout en mémoire, sans entrée-sortie. L'envoi est fait par lots compressés ; livraison au moins une fois, que l'ingestion idempotente ramène à une seule. Une panne de l'API est reprise avec une attente croissante (de 0,5 s à 30 s, tirée au hasard) ; pleine, la boîte abandonne le plus ancien. À l'arrêt, elle se vide pendant 5 s au plus.

**La perte se compte, elle ne se tait pas.** Chaque événement abandonné, rejeté ou perdu à l'arrêt est compté dans la fenêtre de couverture de son heure, et le relevé est publié ou corrigé ([décision 0010](0010-couverture-publiee-par-chaque-emetteur.md)).

## Alternatives écartées

**Une capture bloquante : l'appel attend que l'API ait accepté l'événement.** Elle ne perdrait rien tant que l'API répond, mais chaque panne ou lenteur d'AnswerLineage deviendrait une panne ou une lenteur des agents, et l'ajout de latence se paierait à chaque appel. C'est le contraire de ce qu'on demande à un observateur.

**Une file sur disque, pour survivre à un redémarrage.** Elle perdrait moins, mais elle fait du proxy un composant qui a un état : un disque à dimensionner et à protéger (la file contient des noms de tables, des empreintes, des pseudonymes), un nettoyage, un nouveau mode de panne (disque plein) sur l'hôte de l'agent. Le proxy reste sans état : on le lance, on l'arrête, on le multiplie sans rien migrer. Ce que la mémoire perd, la couverture le dit.

## Conséquences

- Un redémarrage ou un arrêt brutal perd ce qui n'était pas parti. La perte se voit comme un trou : un relevé manquant est un silence, un événement qui manque au graphe est `missing_from_graph`. Elle n'est pas comptée parmi les événements perdus ([Proxy MCP](../proxy.md#bornes-et-ce-qui-se-perd)).
- Un lot interrompu à l'arrêt est compté perdu même si l'API l'avait reçu : le trou est surestimé, jamais caché.
- Une API qui refuse l'hôte du proxy (`403`) est prise pour une panne et reprise sans fin : l'hôte de l'adresse réglée doit être un hôte attendu de l'API.
- La boîte ne s'authentifie pas auprès de l'API, comme tout émetteur aujourd'hui.
- Le budget est tenu par des mesures : un banc de latence (100 000 appels, p99 de la latence ajoutée au plus 5 ms) tourne chaque nuit sur une machine au repos ; un test de chaos fait passer 10 000 appels l'API coupée, muette puis rétablie, dans la porte de contribution, avec un seuil tolérant.
- Un événement perdu par une boîte pleine est perdu pour de bon : le rattrapage n'existe pas. C'est la couverture, pas la boîte, qui dit si un résultat d'impact peut être conclu.
