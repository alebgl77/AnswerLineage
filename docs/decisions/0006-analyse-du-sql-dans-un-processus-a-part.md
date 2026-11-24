# 0006. Analyse du SQL dans le proxy, qui échoue du côté sûr, dans un processus à part

Statut : en vigueur.

## Contexte

Un outil SQL ne dit pas ce qu'il lit : c'est la requête qui le dit. Pour savoir quelles tables et quelles colonnes un appel a lues, il faut lire la requête. Le proxy est le seul point qui voit à la fois l'appel d'outil, son contexte de trace et sa requête : c'est là que la lecture se rattache à un run, donc à une réponse.

Deux contraintes s'opposent. L'analyse peut coûter cher : une requête pathologique de quelques dizaines de Ko occupe l'analyseur (sqlglot) plusieurs secondes, et le proxy ne doit retarder aucun appel. Et une analyse qui se trompe a un prix asymétrique : une colonne de trop fait rappeler une réponse qui n'était pas touchée, une colonne manquante en fait écarter à tort une qui l'était.

## Décision

**Dans le proxy, après l'appel.** Quand un appel à un outil déclaré par `--sql-tool` a réussi, le proxy analyse la requête une fois la réponse rendue et ajoute les tables lues à l'événement ([Proxy MCP](../proxy.md#analyse-du-sql)).

**Du côté sûr.** L'analyse ne liste les colonnes d'une table que si la liste est sûre d'être complète. Sinon la table est rendue en entier (`SELECT *`, jointure naturelle, colonne sans préfixe quand plusieurs tables sont possibles), ou la lecture est « non résolue », avec un motif d'une liste fermée (`unsupported_statement`, `table_function`, `budget_exceeded`…) : un événement est toujours émis, et il dit ce qu'il ne sait pas. Une vue est rapportée sous son nom.

**Dans un processus à part.** L'analyseur est un interpréteur Python démarré à neuf (`python -I -m answerlineage_proxy.analyzer`) au premier appel à analyser, auquel le proxy confie les requêtes une à une sur son entrée standard, sans jamais bloquer sa boucle. Chaque requête a un budget (50 ms par défaut) ; passé le budget, le processus est tué, et un nouveau démarre à la requête suivante. La file des analyses en attente est bornée (32) : pleine, la lecture est « non résolue » sur-le-champ. Un processus qui meurt, refuse de démarrer ou répond n'importe quoi donne une lecture « non résolue ». Le résultat est mis en cache sous une clé qui n'est pas le texte de la requête.

## Alternatives écartées

**S'en tenir aux journaux de l'entrepôt.** Ils disent qu'une requête a tourné, pas quel appel d'outil, quel run d'agent ni quelle réponse l'a produite : le lien avec une réponse, qui est ce que le produit apporte, se fait à l'appel, là où se trouvent le contexte de trace et le run. Ils ne couvrent d'ailleurs que cet entrepôt. Le graphe réserve déjà une origine « réconciliation » pour des lectures établies autrement que par l'analyse ; aucun chemin ne l'écrit aujourd'hui.

**Analyser dans un fil du proxy.** Un fil Python ne s'interrompt pas, et il disputerait l'interpréteur à la boucle qui relaie les appels : une requête pathologique retarderait les autres appels, sans qu'on puisse l'arrêter. Dans un processus, le budget est réel : on tue le calcul.

## Conséquences

- Aucun appel n'attend l'analyse. Dans le pire cas (requête pathologique, file pleine, processus en panne), la lecture est « non résolue » et l'appel passe.
- Une lecture « non résolue » est un trou du lignage, que le résultat d'impact doit dire plutôt que taire ; une table rendue en entier fait rappeler plus qu'il ne faut (propagation au niveau table), jamais moins.
- Le budget se mesure en temps écoulé : sur une machine très chargée, une requête qui tient d'ordinaire dans le budget peut le dépasser et rester « non résolue » tant qu'elle est en cache. Une analyse abandonnée est elle aussi mise en cache, pour que répéter une requête pathologique ne devienne pas un moyen d'occuper l'analyse.
- Le démarrage d'un interpréteur, et le tuer, ont un coût : il est payé par la requête qui dépasse son budget, pas par les appels qui suivent.
- La sortie d'erreur du processus est fermée : un message d'erreur pourrait citer la requête.
- Le dialecte doit être déclaré par outil (`bigquery`, `snowflake`, `postgres`) ; une syntaxe qu'il ne connaît pas est « non comprise ». Les procédures, les fonctions de table, `UPDATE`, `DELETE`, `MERGE` ne sont pas résolus.
- Seul le SQL de l'outil déclaré est analysé. Un outil qui lit sans SQL se décrit par une [règle de lecture](0014-regles-de-lecture-en-yaml.md).
