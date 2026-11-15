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

## Limites connues

- Les agents ne se lisent pas entre eux : aucune réponse ne dépend d'une sortie d'un autre agent.
- Tous les appels sont captés et toutes les lectures sont résolues : l'entreprise n'a pas de trou de lignage.
- Les facettes `answerlineage_agent` et `answerlineage_delivery` n'ont pas encore de schéma publié ; leur forme peut changer.
