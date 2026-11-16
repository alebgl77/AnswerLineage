# Décisions d'architecture

Une décision d'architecture dit pourquoi le code est fait ainsi : le contexte qui l'a posée, ce qui a été décidé, l'alternative écartée et pourquoi, et ce qu'elle coûte. Chaque décision a son fichier, numéroté dans ce dossier, et chacune a ses quatre parties :

1. **Contexte** : le besoin et les contraintes.
2. **Décision** : ce que fait le code, avec les endroits où le voir.
3. **Alternative écartée** : ce qu'on aurait pu faire, et pourquoi on ne l'a pas fait.
4. **Conséquences** : ce que la décision rend possible, et les limites qu'elle crée.

Elles décrivent le code tel qu'il est : une décision qui n'est plus celle du code est révisée ici, dans la même modification que le code. Pour une vue d'ensemble, lire d'abord l'[architecture](../architecture.md).

| N° | Décision | Alternative écartée |
| --- | --- | --- |
| [0001](0001-postgresql-seul.md) | PostgreSQL seul pour le graphe et la file | Une base de graphe et un bus de messages |
| [0002](0002-openlineage-et-cinq-facettes.md) | OpenLineage comme contrat d'événements, et cinq facettes propres seulement | Un format d'événement propre |
| [0003](0003-identifiant-de-run-et-contexte-de-trace.md) | Identifiant de run dérivé du contexte de trace W3C | Un identifiant propre, propagé par un en-tête nouveau |
| [0004](0004-aucun-contenu-empreintes-a-cle.md) | Aucun contenu stocké : des empreintes à clé seulement | Des contenus chiffrés |
| [0005](0005-capture-qui-ne-bloque-jamais.md) | Une capture qui ne bloque jamais et reste ouverte en panne, par une boîte d'envoi en mémoire | Une capture bloquante ; une file sur disque |
| [0006](0006-analyse-du-sql-dans-un-processus-a-part.md) | Analyse du SQL dans le proxy, qui échoue du côté sûr, dans un processus à part | Les journaux de l'entrepôt seuls ; une analyse dans un fil |
| [0007](0007-proxy-transparent-sans-oauth.md) | Un proxy transparent, qui décide sur le corps, ne termine pas OAuth et ne garde aucun jeton | Une passerelle centrale |
| [0008](0008-aucun-modele-dans-le-produit.md) | Aucun modèle dans le produit | Un classement ou un résumé par un modèle de langage |
| [0009](0009-monodepot-publication-par-composant.md) | Un dépôt, une publication par composant | Un dépôt par composant |
| [0010](0010-couverture-publiee-par-chaque-emetteur.md) | La couverture : ce que chaque émetteur a vu, publié et comparé au graphe | Ne rien dire de ce qui manque |
| [0011](0011-double-horodatage-et-intervalle-des-lectures.md) | Double horodatage, et intervalle d'heures pour une lecture | L'heure d'événement la plus ancienne seule |
| [0012](0012-partitions-par-mois-de-reception.md) | Partitions par mois de réception | Des partitions par heure d'événement |
| [0013](0013-artefacts-signes-sans-cle.md) | Des artefacts signés sans clé et attestés | Une clé de signature stockée |
| [0014](0014-regles-de-lecture-en-yaml.md) | Règles de lecture des outils non SQL : un fichier YAML déclaratif | Un format de la bibliothèque standard (TOML, JSON) |

## Ajouter ou réviser une décision

Une décision nouvelle prend le numéro suivant, un nom de fichier `NNNN-sujet-en-quelques-mots.md`, un titre `# NNNN. …`, et une ligne dans le tableau ci-dessus. Elle écrit ce que le code fait réellement, avec les fichiers où le voir, et ne promet rien que le code ne tient pas. Une décision remplacée n'est pas supprimée : son statut dit par laquelle, et le nouveau fichier explique ce qui a changé.

`tests/test_documentation.py` vérifie que chaque décision a ses quatre parties, que l'index les liste toutes et que les liens de la documentation mènent quelque part.
