# 0011. Double horodatage, et intervalle d'heures pour une lecture

Statut : en vigueur.

## Contexte

Un événement porte l'heure que son émetteur lui donne. Cette heure est la seule que le produit puisse attribuer à ce qui s'est passé, et c'est elle qui décide si une lecture tombe dans la fenêtre d'un incident. Mais elle vient de l'extérieur : une horloge réglée de travers, un émetteur qui rattrape son retard, ou un acteur hostile peut l'écrire comme il veut.

Deux questions en découlent. Un auditeur demande ce qu'on savait au moment d'une décision : quelles réponses un rappel décidé mardi pouvait-il trouver ? L'heure de l'émetteur ne le dit pas, puisqu'un émetteur peut envoyer ce qu'il veut, dans l'ordre qu'il veut, après coup. Et une lecture peut être déclarée plusieurs fois : si l'on ne garde que la première heure, un événement rejoué avec une heure plus ancienne la déplace hors de la fenêtre d'un incident, et la réponse qui en dépend disparaît d'une propagation sans que rien le dise.

## Décision

**Deux heures sur chaque ligne datée du graphe.** L'heure de l'événement, déclarée par l'émetteur (`eventTime`), et l'heure d'enregistrement : la réception, par AnswerLineage, du premier événement qui a fait connaître la ligne (`event_raw.recorded_at`, posée par la base). Ce n'est pas l'heure du passage du worker : rejouer un événement ou le projeter plus tard ne la change pas, et quand deux événements font connaître la même ligne, elle garde la plus ancienne des deux réceptions ([Deux heures](../schema.md#deux-heures)).

**Un intervalle pour une lecture.** Une lecture garde la première (`event_time`) et la dernière (`last_event_time`) heure d'événement reçues. La propagation retient une lecture dont l'intervalle recoupe la fenêtre de l'incident. Une contrainte nommée garde la première avant la dernière, et un index partiel range les seules lectures étalées sur plusieurs heures.

**Lire « tel que connu à ».** Le paramètre `known_at` des routes de lecture exclut ce qui a été enregistré après cette date.

## Alternative écartée

**L'heure d'événement la plus ancienne seule.**

C'est ce qu'on aurait de plus simple : une heure par ligne, la plus ancienne l'emporte, et l'écriture reste idempotente. Mais un événement rejoué avec une heure antérieure, par un émetteur mal réglé ou malveillant, déplace la lecture avant la fenêtre de l'incident, et la propagation la perd. Taire une réponse touchée est le pire défaut d'un produit de rappel.

Avec l'intervalle, un rejeu antidaté l'élargit sans le déplacer : la lecture recoupe toujours la fenêtre. Et sans heure d'enregistrement, aucune lecture « telle que connue à » n'est possible : l'heure de l'émetteur peut être n'importe laquelle.

## Conséquences

- L'écriture reste idempotente et commutative : la première heure garde la plus petite valeur, la dernière la plus grande, l'enregistrement la plus ancienne réception.
- L'intervalle peut sur-approximer : un rejeu antidaté élargit l'intervalle, donc une lecture peut être retenue par une fenêtre où elle n'a pas eu lieu. C'est le côté sûr : on rappelle trop plutôt que trop peu.
- Les écritures, les réponses et les diffusions gardent une seule heure d'événement, la plus ancienne : aucune lecture ne les retient par une fenêtre.
- `known_at` dit quelles lignes existaient, pas comment elles étaient remplies : une ligne connue à la date porte ce qu'elle porte aujourd'hui, avec les colonnes ou l'intervalle qu'un événement ultérieur a étendus. La couverture n'y est pas rejouée.
- L'heure d'un événement est bornée à l'entrée (après 1970, au plus un jour après la réception) : au-delà, elle fixerait pour toujours l'état d'un run et placerait sa réponse en tête des plus récentes. Les événements anciens restent admis, pour rattraper un retard.
- Une migration (`0007`) a rattaché les lignes existantes aux événements qui les font connaître ; une ligne qu'aucun événement reçu ne rattache garde l'heure du passage du worker, qui suit toujours la réception et ne la précède jamais.
