# Extension A2A d'AnswerLineage, version 1

URI de l'extension : `https://github.com/alebgl77/AnswerLineage/tree/main/spec/a2a/v1`, l'adresse de ce dossier.

Un agent appelé par le protocole A2A rattache son run au run de l'appel, et renvoie à l'agent qui l'appelle un résumé de lignage borné et sans contenu. La spécification est dans [`docs/a2a.md`](../../../docs/a2a.md).

| Fichier | Ce qu'il décrit | Où il se place |
| --- | --- | --- |
| [`AgentExtension.json`](AgentExtension.json) | La déclaration de l'extension et ses paramètres | `capabilities.extensions[]` de l'Agent Card de l'appelé |
| [`CallerContext.json`](CallerContext.json) | Ce que l'appelant envoie | `message.metadata`, sous la clé de l'URI |
| [`LineageSummary.json`](LineageSummary.json) | Ce que l'appelé renvoie | `metadata` de la tâche ou du message rendu, sous la clé de l'URI |

Les fichiers de ce dossier ne changent plus. Une évolution publie une autre version, sous une autre URI.
