# Entreprise synthétique

Le corpus invente une entreprise : ses jeux de données, ses outils, ses agents, deux semaines de demandes et quelques incidents de données. Il sert à essayer AnswerLineage et à le tester sans aucune donnée réelle.

Tout découle d'une graine. Deux générations avec la même graine donnent exactement les mêmes événements et la même vérité, sur toute machine et toute version de Python.

```bash
uv run answerlineage-corpus load --url http://127.0.0.1:8000
```

| Commande | Effet |
| --- | --- |
| `answerlineage-corpus events` | Écrit les événements OpenLineage de l'entreprise, en un tableau JSON |
| `answerlineage-corpus truth` | Écrit la vérité : pour chaque incident, les réponses et les destinataires touchés |
| `answerlineage-corpus load --url <API>` | Envoie les événements à l'API d'ingestion, par lots |

Les options `--seed`, `--requests`, `--days` et `--incidents` choisissent la graine et la taille. Par défaut : graine 1, 240 demandes sur 10 jours ouvrés, 6 incidents.

## Ce que contient l'entreprise

| | |
| --- | --- |
| Agents | 5 : assistant commercial, assistant du support, analyste de trésorerie, planificateur de stocks, synthèse hebdomadaire |
| Outils | 4, sur 3 serveurs MCP : `entrepot/execute_sql`, `crm/rechercher_comptes`, `crm/lister_opportunites`, `support/lister_tickets` |
| Jeux de données | 13 tables : 10 dans l'entrepôt (`bigquery`), 3 dans des bases opérationnelles (`postgres://…`) |
| Destinataires | Une vingtaine de pseudonymes, répartis entre les audiences des agents |

Une demande est un run d'agent. L'agent appelle un à trois outils, chaque appel lit une ou plusieurs tables, puis l'agent répond à un, deux ou trois destinataires de son audience. Environ une demande sur douze échoue avant de répondre.

Un outil SQL lit les tables que la requête choisit, parfois en entier, parfois quelques colonnes. Les autres outils lisent toujours les mêmes tables, en entier. Les tables de l'entrepôt sont rechargées chaque nuit : leur lecture porte la version du jour.

Rien de tout cela n'a de contenu. Une réponse est une empreinte, un message est une référence, un destinataire est un pseudonyme.

## Les événements

Chaque demande devient ce que des émetteurs réels enverraient :

| Moment | Événement |
| --- | --- |
| La demande arrive | `START` du run de l'agent, avec la facette `answerlineage_agent` |
| Chaque appel d'outil | `COMPLETE` d'un run enfant, tel que le proxy l'émet : facettes `parent` et `answerlineage_toolCall`, tables lues dans `inputs`, colonnes dans la facette `schema`, version dans la facette `version` |
| L'agent répond | `COMPLETE` du run de l'agent ; la réponse est une sortie qui porte la facette `answerlineage_delivery` : empreinte, canal, référence du message, destinataires |
| L'agent échoue | `FAIL` du run de l'agent, sans sortie |

Les schémas des facettes `answerlineage_*` sont publiés (voir [Facettes OpenLineage](facettes.md)) : un test valide contre eux chaque événement du corpus.

Un test vérifie que chaque événement respecte le schéma officiel d'OpenLineage, que l'API d'ingestion l'accepte, et qu'un appel d'outil est décrit exactement comme le proxy le décrit. Un autre envoie l'entreprise par l'API et les workers, puis compare le graphe obtenu à l'entreprise générée.

## La vérité

Un incident désigne une table et une fenêtre de temps : le chargement de nuit est faux à 6 h, et corrigé de quelques heures à trois jours plus tard.

Une réponse est touchée quand un appel de sa demande a lu la table pendant la fenêtre, début inclus et fin exclue. Une demande restée sans réponse n'a rien à rappeler. C'est l'heure de la lecture qui compte, pas celle de la réponse.

Le générateur établit cette liste lui-même, à partir de ce qu'il a généré, sans passer par AnswerLineage. C'est à elle que se mesure la propagation d'un incident : toute réponse attendue doit être retrouvée.

```json
{
  "name": "incident-01",
  "dataset": {"namespace": "bigquery", "name": "ventes.suivi.commandes"},
  "from": "2026-11-11T06:00:00Z",
  "to": "2026-11-12T12:00:00Z",
  "answers": [
    {
      "fingerprint": "sha256:…",
      "channel": "slack",
      "messageRef": "…",
      "recipients": ["…"],
      "agent": {"namespace": "agent://ventes", "name": "assistant-commercial"}
    }
  ],
  "recipients": ["…"]
}
```

## Corpus de vérité

À côté de l'entreprise générée, le corpus tient des scénarios d'incident écrits à la main, chacun avec sa vérité. Ils servent à mesurer le moteur d'impact comme on mesure un modèle : rappel sur les réponses touchées, précision sur les réponses écartées.

```bash
uv run answerlineage-corpus check
```

| Scénario | Niveau | Ce qu'il éprouve |
| --- | --- | --- |
| `table-fenetre` | table | Les bornes de la fenêtre, la réponse rendue après la correction, le run sans réponse |
| `table-sous-agent` | table | La lecture faite par un sous-agent, ou par un appel parmi d'autres |
| `fenetre-sans-lecture` | table | Un incident qui ne touche personne |
| `version-corrigee` | table | La table rechargée avant la fin déclarée de l'incident |
| `colonnes` | colonnes | Une seule colonne fausse : colonnes communes, disjointes, table lue en entier |
| `sous-ensemble` | sous-ensemble | Une partition fausse : sous-ensemble commun, disjoint, inconnu |
| `cles` | clés | Quelques lignes fausses : clés communes, disjointes, inconnues |
| `transitivite-memoire` | table | Une réponse fondée sur une note écrite à partir de la table fausse |
| `transitivite-chaine` | table | La table fausse alimente une table dérivée, puis une note, puis une réponse |

Un scénario est un fichier JSON de `corpus/src/answerlineage_corpus/scenario_data/`. Il décrit quelques runs d'agents, un incident et sa vérité :

| Rubrique de la vérité | Sens |
| --- | --- |
| `touched` | Réponses à rappeler, avec la confiance attendue (`certaine`, `probable`, `possible`). `path` vaut `indirect` quand la réponse dépend d'une sortie d'un run touché et non de la table elle-même |
| `discarded` | Réponses qui ont lu la table pendant la fenêtre sans dépendre de ce qui est faux, avec le motif : colonnes, sous-ensembles, clés ou version disjoints |
| `untouched` | Réponses sans rapport avec l'incident |

Le contrôle refuse un scénario sans vérité, une réponse que la vérité oublie ou classe deux fois, et une vérité que la description contredit : une réponse dite touchée alors que rien n'a lu la table pendant la fenêtre, une dépendance indirecte qui remonterait le temps. Il exige que le corpus couvre les quatre niveaux de précision, la transitivité et les réponses écartées.

Le corpus porte un numéro de version, lié à son contenu : changer un scénario ou sa vérité demande une nouvelle version, pour que deux mesures restent comparables.

Les sous-ensembles et les clés d'une lecture sont décrits dans le scénario, mais pas encore dans ses événements : le contrat d'événements ne les porte pas.

## Limites connues

- Les agents ne se lisent pas entre eux : aucune réponse ne dépend d'une sortie d'un autre agent.
- Tous les appels sont captés et toutes les lectures sont résolues : l'entreprise n'a pas de trou de lignage.
