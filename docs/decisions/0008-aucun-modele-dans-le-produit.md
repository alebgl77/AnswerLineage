# 0008. Aucun modèle dans le produit

Statut : en vigueur.

## Contexte

Le produit existe pour que quelqu'un puisse agir : rappeler des destinataires, dire à un auditeur ce qu'on savait au moment d'une décision. Ce qu'il affirme doit pouvoir se vérifier, se rejouer et se contester. Un résultat qui sert de preuve se reproduit : mêmes événements, même résultat.

Les modèles de langage tentent à deux endroits : classer les réponses touchées (lesquelles comptent le plus ?) et résumer un incident en quelques phrases pour ceux qui doivent le lire. Mais le produit reçoit des textes que personne ne maîtrise : noms d'outils, noms de jeux de données, identifiants, tout ce que des émetteurs hostiles ou maladroits peuvent écrire.

## Décision

Aucun modèle dans AnswerLineage : tout résultat est déterministe. Aucun composant ne dépend d'une bibliothèque de modèle, d'un service de modèle ni d'un jeu de poids.

- La propagation est une requête SQL bornée sur le graphe ; la remontée aussi. Les mêmes lectures dans la même fenêtre donnent les mêmes réponses, dans le même ordre.
- La capture est faite de règles : l'analyse du SQL, l'identifiant de run dérivé par un calcul publié, les règles de lecture déclarées. Quand une règle ne sait pas, elle le dit (lecture « non résolue », outil « non décrit ») au lieu de deviner.
- Tout texte que produit le produit suit un gabarit fixe : les messages de la ligne de commande, de `doctor` et des erreurs de l'API sont écrits d'avance, avec des nombres et des codes, jamais un texte reçu. Un identifiant reçu n'est recopié que s'il a une forme admise.
- Les jeux de données d'essai, eux aussi, se génèrent d'une graine : deux générations donnent les mêmes événements et la même vérité, sur toute machine.

Le moteur se mesure comme un modèle le serait, mais sur une vérité connue : le [corpus de vérité](../corpus.md#mesure-du-moteur-dimpact) donne, pour chaque incident, les réponses touchées, et une porte du pipeline exige un rappel (*recall*) d'au moins 0,99 au niveau table, sans aucune réponse hors d'atteinte renvoyée.

## Alternative écartée

**Classer ou résumer par un modèle de langage.**

Un classement par un modèle donnerait un ordre de priorité qu'on ne peut pas expliquer ni rejouer, et un résumé, un texte que personne n'a écrit à partir de contenus venus d'émetteurs que le produit tient pour hostiles : une instruction glissée dans un nom, que le contrôle des identifiants laisserait passer parce qu'elle a la forme d'un nom, atteindrait un lecteur ou une action. Un résultat qui peut changer d'un jour à l'autre n'est pas une preuve, et une mesure comme le rappel de 0,99 n'a pas de sens sur un moteur qui n'est pas reproductible.

Ce qu'un modèle apporterait, ordonner et résumer, est ici obtenu par des gabarits et des listes triées de façon stable (par heure, puis par identifiant). L'ordre de présentation est déterministe : les plus récentes d'abord.

## Conséquences

- Le produit ne hiérarchise pas les réponses touchées et n'écrit pas de commentaire sur un incident : il rend la liste, les compteurs, la couverture et les trous, que le lecteur interprète.
- Un lien de dépendance que personne n'a déclaré n'est pas deviné. Une réponse fondée sur une note écrite à partir de la table fausse n'est pas retrouvée tant que le graphe ne dit pas ce que le run touché a écrit ; la propagation ne suit pas encore ce qu'un run touché a écrit ([limites](../lecture.md#limites-connues)).
- Les erreurs et les anomalies sont dites dans un français fixe, avec un remède écrit d'avance : elles sont moins fines qu'un texte composé à la demande, et se traduisent ou s'automatisent sans surprise (un code stable accompagne chaque message).
- Un outil extérieur peut lire les résultats de l'API, et rester libre de s'appuyer sur un modèle : la frontière est que ce qu'AnswerLineage affirme ne dépend pas d'un modèle, et que rien de ce qu'il rend n'est un texte libre d'émetteur, seulement des identifiants de forme contrôlée.
- Si un modèle entrait un jour dans le produit, ce serait une autre décision, avec ses garde-fous : cette décision-ci en fait un invariant, et un changement qui le viole est faux même si ses tests passent.
