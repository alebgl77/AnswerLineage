# Proxy MCP

Le proxy se place devant un serveur MCP en HTTP, sans toucher à l'agent ni au serveur. Il relaie chaque requête à l'identique et émet un événement OpenLineage par appel d'outil.

```bash
uv run answerlineage-proxy --upstream http://127.0.0.1:9000 --server-name entrepot --sql-tool execute_sql=query:bigquery
```

| Réglage | Option | Variable | Défaut |
| --- | --- | --- | --- |
| Adresse du serveur MCP | `--upstream` | `ANSWERLINEAGE_PROXY_UPSTREAM` | obligatoire |
| Nom du serveur dans le lignage | `--server-name` | `ANSWERLINEAGE_PROXY_SERVER_NAME` | hôte du serveur |
| Adresse d'écoute | `--host` | `ANSWERLINEAGE_PROXY_HOST` | `127.0.0.1` |
| Port d'écoute | `--port` | `ANSWERLINEAGE_PROXY_PORT` | `8717` |
| En-tête `Host` du serveur MCP | `--no-preserve-host` | | celui du client |
| Taille de corps lue | `--max-inspected-bytes` | | 1 Mo |
| Outil dont un argument porte du SQL | `--sql-tool` | | aucun |

L'adresse du serveur MCP ne porte ni identifiant ni paramètre : elle peut apparaître dans la configuration.

## Ce qui est relayé

- Toutes les méthodes HTTP, le chemin et les paramètres tels que reçus. Les révisions 2026-07-28 (sans session) et 2025-11-25 (session, flux `GET`, clôture par `DELETE`) de MCP fonctionnent sans réglage.
- Tous les en-têtes, dans leur ordre, sauf ceux qui sont propres à une connexion (RFC 9110, section 7.6.1). Le proxy n'en ajoute aucun : ni `Server`, ni `Date`, ni `Forwarded`.
- Le corps de la réponse, octet pour octet et au fil de l'eau : rien n'est décompressé ni retenu, un flux SSE arrive événement par événement.
- La fermeture du flux par le client : le proxy ferme aussitôt sa connexion au serveur MCP, ce qui annule l'appel.

Si le serveur MCP est injoignable, le proxy répond `502` avec un corps fixe. Si sa réponse s'interrompt, la connexion au client est fermée sans être terminée : une réponse tronquée ne passe pas pour complète.

## Ce qui est lu

Le proxy lit le corps des requêtes `POST` pour reconnaître un appel d'outil. Il décide sur le corps, jamais sur les seuls en-têtes :

- un en-tête `Mcp-Method`, `Mcp-Name` ou `MCP-Protocol-Version` contredit par le corps est signalé dans l'événement (`headerMismatch`) et dans le journal, par son nom seulement ;
- l'événement décrit l'outil du corps ;
- la requête est relayée telle quelle : c'est le serveur MCP qui la refuse, comme la révision 2026-07-28 l'y oblige.

Un nom d'outil hors de la forme recommandée par MCP (1 à 128 lettres, chiffres, `_`, `-`, `.`) n'est pas recopié : l'événement porte `invalidToolName`. Un corps illisible, ou plus grand que la taille lue, est relayé sans être décrit et compté.

## Analyse du SQL

`--sql-tool outil=argument:dialecte[:espace-de-noms]` déclare un outil dont un argument porte une requête SQL. Les dialectes sont `bigquery`, `snowflake` et `postgres`. L'espace de noms est celui des jeux de données dans le lignage ; par défaut, le nom du dialecte.

Quand un appel à cet outil a réussi, le proxy analyse la requête après avoir rendu la réponse, hors du chemin de l'appel, et ajoute à l'événement les tables lues. Chaque table porte la liste des colonnes lues, colonnes de filtre et de jointure comprises.

L'analyse est prudente : elle ne liste des colonnes que si la liste est sûre d'être complète.

| Cas | Résultat |
| --- | --- |
| Colonnes préfixées, ou une seule table dans la requête | Table et colonnes lues |
| `SELECT *`, `t.*`, ligne entière, jointure naturelle, pivot | Table, sans liste de colonnes |
| Colonne sans préfixe quand plusieurs tables sont possibles | Tables concernées, sans liste de colonnes |
| Syntaxe inconnue, SQL dynamique, procédure, fonction de table, `UPDATE`, `DELETE`, `MERGE`, requête de plus de 100 Ko | Lecture « non résolue » (`readResolution`), l'appel passe |

Une vue est rapportée sous son nom : les tables qu'elle lit ne sont pas connues du proxy. La requête et ses littéraux n'atteignent ni l'événement ni les journaux.

## Ce qui n'est jamais conservé

Aucun argument, résultat, en-tête ni jeton n'est stocké ou journalisé. Les en-têtes `Mcp-Param-*`, qui recopient des arguments d'outil, ne sont pas lus. Le journal d'accès du serveur HTTP est coupé. Les journaux des bibliothèques qui citeraient une URL complète, des en-têtes ou une requête SQL (`httpx`, `httpcore`, `sqlglot`) sont coupés pour tout le processus.

## L'événement émis

Un `RunEvent` OpenLineage par appel d'outil, une fois l'échange terminé : `COMPLETE` si le serveur a répondu sans erreur HTTP, `FAIL` sinon, `ABORT` si le client est parti. Le job est `mcp://<serveur>` / `<outil>`. La facette `answerlineage_toolCall` porte le serveur, l'outil, le transport, la révision du protocole, le statut HTTP, les écarts relevés et la résolution de la lecture. Les tables lues sont les jeux de données d'entrée ; leurs colonnes sont dans la facette standard `schema`.

## Limites connues

- Les événements sont comptés mais ne partent pas encore vers l'API d'ingestion.
- L'identifiant de run est aléatoire : il n'est pas encore dérivé du contexte de trace W3C.
- Le résultat de l'appel n'est pas lu : une erreur d'outil rendue en `200` compte comme `COMPLETE`.
- Les en-têtes `Origin` et `Host` ne sont pas contrôlés, et le transport stdio n'est pas pris en charge.
- L'analyse du SQL n'a pas de budget de temps, seulement une taille maximale.
- La latence ajoutée par le proxy n'est pas encore mesurée.
