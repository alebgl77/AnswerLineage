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
| Hôtes attendus dans `Host` | `--allow-host` | `ANSWERLINEAGE_PROXY_ALLOWED_HOSTS` | boucle locale et serveur MCP |
| Origines attendues dans `Origin` | `--allow-origin` | `ANSWERLINEAGE_PROXY_ALLOWED_ORIGINS` | celles des hôtes attendus |
| En-tête `Host` du serveur MCP | `--no-preserve-host` | | celui du client |
| Taille de corps lue | `--max-inspected-bytes` | | 1 Mo |
| Outil dont un argument porte du SQL | `--sql-tool` | | aucun |

L'adresse du serveur MCP ne porte ni identifiant ni paramètre : elle peut apparaître dans la configuration. Son hôte peut être une adresse IPv6, entre crochets (`http://[2001:db8::1]:9000`) ; une adresse IPv6 ne faisant pas un nom de serveur, `--server-name` est alors obligatoire. Un réglage invalide arrête le proxy avec le message « Configuration invalide : … » et le code de sortie 2.

## Ce qui est relayé

- Toutes les méthodes HTTP, le chemin et les paramètres tels que reçus, le chemin à la suite de celui de l'adresse du serveur MCP. Un chemin qui contient un segment `.` ou `..`, même encodé (`%2e`), sortirait de ce préfixe une fois résolu : il est refusé en `400` avec `{"error":"invalid_request_target"}`, sans être relayé. Les révisions 2026-07-28 (sans session) et 2025-11-25 (session, flux `GET`, clôture par `DELETE`) de MCP fonctionnent sans réglage.
- Tous les en-têtes, dans leur ordre, sauf ceux qui sont propres à une connexion (RFC 9110, section 7.6.1). Le proxy n'en ajoute aucun (ni `Server`, ni `Date`, ni `Forwarded`), à une exception près : le `traceparent` qu'il crée pour un appel d'outil arrivé sans contexte de trace (voir [Identifiant de run et contexte de trace](#identifiant-de-run-et-contexte-de-trace)).
- Le corps de la requête, octet pour octet : le proxy ne le modifie jamais.
- Le corps de la réponse, octet pour octet et au fil de l'eau : rien n'est décompressé ni retenu, un flux SSE arrive événement par événement.
- La fermeture du flux par le client : le proxy ferme aussitôt sa connexion au serveur MCP, ce qui annule l'appel.

Si le serveur MCP est injoignable, le proxy répond `502` avec un corps fixe. Si sa réponse s'interrompt, la connexion au client est fermée sans être terminée : une réponse tronquée ne passe pas pour complète.

## Hôtes et origines attendus

Le proxy écoute par défaut sur la boucle locale. Une page web ouverte sur le même poste peut pourtant l'atteindre : par réassociation DNS, un nom d'hôte de l'attaquant finit par désigner `127.0.0.1`. Le navigateur envoie alors ce nom dans l'en-tête `Host`, et l'adresse de la page dans `Origin`.

Une autre page peut aussi le viser par une balise `<img>` ou `<script>`, qui n'envoie pas `Origin` : elle ne lit pas la réponse, mais en apprend le statut. Le navigateur dit d'où vient une requête (`Sec-Fetch-Site`) et ce qu'elle charge (`Sec-Fetch-Mode`, `Sec-Fetch-Dest`).

Le proxy refuse donc, avant tout relais, une requête dont l'un de ces en-têtes n'est pas attendu. Rien n'atteint le serveur MCP et aucun événement n'est émis.

| En-tête | Valeurs acceptées |
| --- | --- |
| `Host` | `127.0.0.1`, `localhost`, `[::1]`, l'hôte du serveur MCP (entre crochets pour une adresse IPv6) et les noms ajoutés par `--allow-host`. Le port n'est pas comparé. |
| `Origin` | Absent, ou une origine dont l'hôte est accepté, ou une origine ajoutée par `--allow-origin` |
| `Sec-Fetch-Site` | Absent, `same-origin` ou `none`. Toute autre valeur (`cross-site`, `same-site`) désigne une autre page : sans origine acceptée, elle n'est admise que pour la navigation d'un lien suivi (`Sec-Fetch-Mode: navigate`, `Sec-Fetch-Dest: document`). |

Un client qui n'est pas un navigateur n'envoie ni `Origin` ni ces en-têtes : rien ne change pour lui.

Une requête refusée reçoit `403` avec `{"error":"host_not_allowed"}`, `{"error":"origin_not_allowed"}` ou `{"error":"cross_site_not_allowed"}`. Le journal cite le nom de l'en-tête, jamais sa valeur.

Un proxy joint sous un autre nom, par exemple derrière un service Kubernetes, déclare ce nom :

```bash
uv run answerlineage-proxy --upstream http://mcp.interne:9000 --host 0.0.0.0 --allow-host proxy.interne
```

`--allow-host '*'` accepte tout hôte, toute origine et toute requête inter-sites : à réserver à un proxy placé derrière un autre contrôle. Les variables d'environnement prennent une liste séparée par des virgules.

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

## Identifiant de run et contexte de trace

L'identifiant de run d'un appel d'outil vient de son contexte de trace W3C ([Trace Context](https://www.w3.org/TR/trace-context/), en-tête `traceparent`). Lignage et traces se corrèlent sans nouvel en-tête, et tout émetteur qui voit le même contexte (deux proxys en série, un proxy et un adaptateur d'agent) émet pour le même run : les écritures du graphe, idempotentes, fusionnent leurs événements.

### D'où vient le contexte

Dans cet ordre, pour une requête `tools/call` :

1. `params._meta.traceparent` du corps. Il désigne l'appel d'outil lui-même et survit aux reprises HTTP : c'est lui qui l'emporte.
2. Sinon, l'en-tête HTTP `traceparent`, s'il est unique et bien formé. Il diffère légitimement de celui de `_meta` (un client HTTP instrumenté ouvre son propre span) : ce n'est pas un écart signalé.
3. Sinon, le proxy crée un contexte : `trace-id` et `parent-id` tirés d'une source cryptographique (`secrets`), jamais nuls, version `00`, drapeaux `00` (« non échantillonné » : le proxy ne décide pas de l'échantillonnage des traces d'autrui).

Une valeur est bien formée quand elle suit le format W3C : version (2 hexadécimaux, pas `ff`), `trace-id` (32), `parent-id` (16), drapeaux (2), séparés par des tirets, en hexadécimal minuscule, sans identifiant nul. Une version `00` s'arrête après les drapeaux ; une version future peut être suivie d'un tiret et de champs qui ne sont pas lus. Au-delà de 512 caractères, la valeur n'est pas lue. Une valeur mal formée est une entrée hostile : elle est ignorée comme si elle manquait, sans être recopiée ni journalisée.

### Ce que le proxy ajoute à la requête relayée

| Appel reçu | Identifiant de run dérivé de | Requête relayée |
| --- | --- | --- |
| `_meta.traceparent` bien formé | `_meta` | inchangée, en-tête `traceparent` compris |
| en-tête `traceparent` unique et bien formé | l'en-tête | inchangée |
| aucun contexte | contexte créé | un en-tête `traceparent` ajouté, à la fin, qui porte le contexte créé |
| en-tête `traceparent` mal formé ou en double, sans `_meta` valable | contexte créé | inchangée : l'en-tête reçu est relayé tel quel, rien n'est ajouté |
| requête qui n'est pas un appel d'outil, corps illisible ou plus grand que la taille lue | aucun (pas d'événement) | inchangée |

Un en-tête `traceparent` déjà présent n'est donc jamais remplacé ni doublé. Quand il est mal formé, le contexte créé ne sert qu'à l'identifiant de run : le transmettre ferait deux en-têtes `traceparent`, que le serveur ne saurait départager. Le corps n'est jamais modifié.

Rien ne retarde l'appel : `_meta` est lu par l'inspection déjà faite du corps, et la création du contexte ne fait ni entrée-sortie ni attente.

### La dérivation, à l'octet près

L'identifiant de run est un UUID version 8 (RFC 9562), fonction pure du `trace-id` et du `parent-id` :

1. `entrée` = les 23 octets ASCII de `answerlineage/run-id/v1`, suivis des 16 octets du `trace-id` puis des 8 octets du `parent-id`, décodés de l'hexadécimal (47 octets en tout).
2. `empreinte` = les 16 premiers octets de SHA-256(`entrée`).
3. L'octet 6 (en partant de 0) devient `(octet & 0x0f) | 0x80` : version 8.
4. L'octet 8 devient `(octet & 0x3f) | 0x80` : variante RFC 9562.
5. L'identifiant est l'écriture canonique de ces 16 octets : `8-4-4-4-12` hexadécimaux minuscules.

La version et les drapeaux du `traceparent` n'entrent pas dans le calcul : le même appel, échantillonné ou non, a le même run. En Python, sans rien d'AnswerLineage :

```python
import hashlib
import uuid

octets = bytearray(
    hashlib.sha256(
        b"answerlineage/run-id/v1" + bytes.fromhex(trace_id) + bytes.fromhex(parent_id)
    ).digest()[:16]
)
octets[6] = (octets[6] & 0x0F) | 0x80
octets[8] = (octets[8] & 0x3F) | 0x80
run_id = uuid.UUID(bytes=bytes(octets))
```

Exemple : `00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01` donne `a7e19836-59d9-8033-bd72-8a4bccb7271f`. Les vecteurs de test, valeurs acceptées avec leur identifiant et valeurs refusées, sont publiés dans [`spec/run-id/vectors-v1.json`](../spec/run-id/vectors-v1.json) pour vérifier tout autre émetteur.

Un UUID version 5 aurait aussi convenu. La version 8 est retenue parce que la version 5 impose SHA-1, que l'on n'emploie plus dans une construction nouvelle, et un UUID d'espace de noms à publier en plus ; la RFC 9562 recommande la version 8 pour un nom haché par un autre algorithme, et en donne un exemple avec SHA-256. Tout langage a SHA-256 ; le calcul tient en cinq étapes. L'API d'ingestion accepte tout UUID de forme canonique. Une autre dérivation, demain, changerait d'étiquette (`…/v2`) et de fichier de vecteurs.

## Ce qui n'est jamais conservé

Aucun argument, résultat, en-tête ni jeton n'est stocké ou journalisé. Seuls le `trace-id` et le `parent-id` d'un contexte de trace bien formé, qui ne disent rien du contenu, sont recopiés dans l'événement ; ils ne sont jamais journalisés. Les en-têtes `Mcp-Param-*`, qui recopient des arguments d'outil, ne sont pas lus. Le journal d'accès du serveur HTTP est coupé. Les journaux des bibliothèques qui citeraient une URL complète, des en-têtes ou une requête SQL (`httpx`, `httpcore`, `sqlglot`) sont coupés pour tout le processus.

## Autorisation

Le proxy ne prend aucune part à l'autorisation. Le défi `401` d'un serveur MCP protégé, ses métadonnées OAuth (`/.well-known/…`) et les échanges d'un client avec le serveur d'autorisation passent tels quels : le proxy n'y répond jamais lui-même, ne s'authentifie pas à la place du client et ne garde aucun jeton d'un appel au suivant.

Un test fait traverser un jeton, un cookie, un paramètre recopié en en-tête et un argument d'outil sur tout le parcours, du proxy au graphe, puis les cherche dans les événements, la base entière, les réponses de l'API, les compteurs et tous les journaux : il n'en reste rien.

## L'événement émis

Un `RunEvent` OpenLineage par appel d'outil, une fois l'échange terminé : `COMPLETE` si le serveur a répondu sans erreur HTTP, `FAIL` sinon, `ABORT` si le client est parti. Le job est `mcp://<serveur>` / `<outil>`, l'identifiant de run celui que dérive le contexte de trace. La facette `answerlineage_toolCall` porte le serveur, l'outil, le transport, la révision du protocole, le statut HTTP, les écarts relevés, la résolution de la lecture et le contexte de trace (`traceId`, `spanId`, `traceOrigin`) ; son schéma est publié (voir [Facettes OpenLineage](facettes.md)). Les tables lues sont les jeux de données d'entrée ; leurs colonnes sont dans la facette standard `schema`.

## Limites connues

- Les événements sont comptés mais ne partent pas encore vers l'API d'ingestion.
- Un client qui réutilise le même span pour deux appels d'outil leur donne le même identifiant de run : le graphe fusionne alors les deux appels en un seul run, qui porte les lectures des deux mais l'outil du premier événement projeté, donc selon l'ordre d'arrivée (`tests/test_trace_context_graph.py` le constate).
- Sans authentification des émetteurs, un identifiant dérivé n'est pas une preuve d'origine : qui connaît le contexte d'un appel peut émettre un événement pour son run.
- Une reprise HTTP garde son run quand le contexte vient de `_meta`. Quand il vient de l'en-tête d'un client HTTP qui ouvre un span par tentative, ou quand le proxy le crée, chaque tentative est un run distinct.
- Le proxy ne rattache pas un appel au run de l'agent : la facette standard `parent` exige le job de l'agent, que le proxy ne connaît pas. Un émetteur qui connaît l'agent peut le faire ; `traceId` relie déjà les appels d'une même trace.
- Le graphe ne range pas encore `traceId` et `spanId` dans le run : ils restent dans l'événement brut.
- Le résultat de l'appel n'est pas lu : une erreur d'outil rendue en `200` compte comme `COMPLETE`.
- Le transport stdio n'est pas pris en charge.
- L'analyse du SQL n'a pas de budget de temps, seulement une taille maximale.
- La latence ajoutée par le proxy n'est pas encore mesurée.
