# 0012. Partitions par mois de réception

Statut : en vigueur.

## Contexte

Trois tables grossissent avec le trafic, pas avec le graphe : `event_raw` (chaque événement reçu, tel quel), `coverage_report` (un relevé par émetteur, instance et minute : 1 440 lignes par jour et par instance) et `ingest_duplicate`. Elles doivent pouvoir être purgées selon une durée de conservation, sans que la purge ralentisse l'ingestion ni verrouille longtemps des tables que les workers et l'API écrivent.

Le produit garde ce qu'il reçoit comme preuve et comme source de reconstruction du graphe : la durée de conservation est un réglage de l'exploitant, pas un détail interne.

## Décision

Ces trois tables sont partitionnées par mois UTC, créées à l'avance et supprimées entières une fois plus anciennes que la conservation (13 mois par défaut), par les workers, sans intervention ([Workers](../workers.md#partitions-mensuelles), [Schéma](../schema.md#partitions-et-conservation)).

**La clé de partition est le mois de réception, jamais l'heure de l'événement.** Elle est posée par la base à l'insertion, avec `recorded_at`, et une contrainte nommée la tient égale au mois UTC de `recorded_at`. Aucune mise à jour ne la déplace.

- Les migrations créent le mois courant et les deux suivants ; la maintenance garde ces mois d'avance (`--partitions-ahead`) et signale, sept jours avant, un mois qui commence sans partition (`partition_alert`) : aucune écriture n'échoue faute de partition.
- La purge ne touche ni le mois courant (elle prend la plus prudente de deux horloges, celle du worker et celle de la base), ni une partition qu'une partie seulement de la conservation couvre, ni une partition par défaut créée à la main.
- Plusieurs workers entretiennent sans double travail, par un verrou consultatif qu'ils n'attendent pas ; créer ou supprimer une partition n'attend pas plus d'une seconde le verrou de sa table.

## Alternative écartée

**Partitionner par heure d'événement.**

L'heure d'événement est la plus naturelle pour qui lit : elle dit quand les choses se sont passées. Mais elle vient de l'émetteur. Un émetteur peut dater un événement de 1970 ou d'un jour à venir : l'événement daté de 1970 irait dans une partition de 1970, et la purge dépendrait d'une valeur que n'importe quel émetteur peut forger : un événement daté d'un mois ancien serait purgé aussitôt, un événement daté d'un mois lointain ne le serait pas de sitôt. La réception est posée par la base : elle suit toujours le temps réel, et une partition se supprime entière, sans parcourir de lignes.

Le graphe, lui, n'est pas partitionné : ses écritures fusionnent les événements d'un run sur des clés naturelles qui ne contiennent aucune heure, et un partitionnement par heure d'enregistrement obligerait à l'ajouter à ces clés, de sorte que deux événements d'une même lecture reçus deux mois différents donneraient deux arêtes au lieu d'une.

## Conséquences

- La conservation s'applique aux événements bruts, aux relevés et aux doublons, pas au graphe. Passé la durée, les événements ne permettent plus de reconstruire ce qui a été purgé, et la couverture d'une période purgée est inconnue plutôt que fausse. Le graphe garde toutes ses lignes ; leur heure d'enregistrement permettra de le purger le jour où le produit aura fixé quand une réponse est trop ancienne pour être rappelée, ce qui est une décision du produit.
- PostgreSQL exige que la clé primaire d'une table partitionnée contienne sa clé de partition : un événement déjà reçu est donc cherché dans toutes les partitions avant d'être enregistré, et la clé primaire départage deux réceptions simultanées du même mois. La file de traitement n'a pas de clé étrangère vers `event_raw` (elle empêcherait de supprimer une partition) : elle désigne l'événement par son identifiant et son mois.
- Deux réceptions simultanées d'un même événement, à cheval sur deux mois à quelques millisecondes près, ne se voient pas : l'événement est enregistré deux fois. Le graphe n'en est pas changé (la projection est idempotente) ; le doublon n'est pas compté.
- Un relevé reçu juste après un changement de mois survit un mois de plus que les événements de sa fenêtre : pendant ce mois, la couverture de cette fenêtre compte ses appels comme manquants.
- Une conservation absurde (nulle, négative, plus courte que l'avance) empêche le worker de démarrer.
- La migration `0008` a recréé les tables existantes : leurs lignes sont copiées avec leur mois de réception. Sa descente réunit ce qui avait été reçu à cheval sur deux mois.
