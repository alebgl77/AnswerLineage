# Proxy MCP

Le proxy se place devant un serveur MCP, en HTTP ou en stdio, sans toucher à l'agent ni au serveur. Il relaie chaque requête à l'identique et émet un événement OpenLineage par appel d'outil, que sa [boîte d'envoi](#boîte-denvoi) remet à l'API d'ingestion hors du chemin des appels. Pendant une quarantaine, en HTTP, il avertit le résultat d'une lecture des données en quarantaine ([Avertir](#avertir--lavertissement-dans-le-résultat)).

```bash
uv run answerlineage-proxy --upstream http://127.0.0.1:9000 --server-name entrepot --sql-tool execute_sql=query:bigquery --api-url http://127.0.0.1:8000
uv run answerlineage-proxy --server-name fichiers --api-url http://127.0.0.1:8000 -- /opt/mcp/serveur-fichiers --racine /srv/partage
```

La première ligne écoute devant un serveur HTTP. La seconde lance un serveur local en stdio et se place entre lui et le client : voir [Mode stdio](#mode-stdio). En HTTP, le proxy se place sous l'URL du serveur sans rien changer à ses jetons ([Modes transparent et passerelle](#modes-transparent-et-passerelle)), et peut tourner en plusieurs [réplicas](#plusieurs-réplicas). Sauf mention contraire, ce qui suit décrit le mode HTTP ; le mode stdio en reprend l'inspection, l'analyse du SQL, l'étiquetage des requêtes, les règles de lecture, les empreintes, la qualité de service, les relevés de couverture, la boîte d'envoi et la synchronisation des périmètres en quarantaine.

| Réglage | Option | Variable | Défaut |
| --- | --- | --- | --- |
| Adresse du serveur MCP | `--upstream` | `ANSWERLINEAGE_PROXY_UPSTREAM` | obligatoire |
| Nom du serveur dans le lignage | `--server-name` | `ANSWERLINEAGE_PROXY_SERVER_NAME` | hôte du serveur |
| Adresse d'écoute | `--host` | `ANSWERLINEAGE_PROXY_HOST` | `127.0.0.1` |
| Port d'écoute | `--port` | `ANSWERLINEAGE_PROXY_PORT` | `8717` |
| Hôtes attendus dans `Host` | `--allow-host` | `ANSWERLINEAGE_PROXY_ALLOWED_HOSTS` | boucle locale et serveur MCP |
| Origines attendues dans `Origin` | `--allow-origin` | `ANSWERLINEAGE_PROXY_ALLOWED_ORIGINS` | celles des hôtes attendus |
| Mode passerelle : les clients joignent le proxy à sa propre URL, et le serveur MCP reçoit son propre nom dans `Host` ([Modes](#modes-transparent-et-passerelle)) | `--no-preserve-host` | | mode transparent : le serveur reçoit le `Host` du client |
| Jetons du serveur MCP déclarés liés à son URL : le mode passerelle est refusé | `--audience-bound-tokens` | | non déclarés : en mode passerelle, lus dans ses métadonnées OAuth |
| Taille de corps lue | `--max-inspected-bytes` | | 1 Mo |
| Outil dont un argument porte du SQL | `--sql-tool` | | aucun |
| Outil SQL dont la requête relayée porte l'identifiant de run en commentaire | `--sql-run-comment` | | aucun : la requête n'est jamais modifiée |
| Budget de calcul de l'analyse d'une requête SQL | `--sql-budget-ms` | `ANSWERLINEAGE_PROXY_SQL_BUDGET_MS` | 50 ms (1 à 60 000) |
| Analyses du SQL en attente, au plus | `--sql-queue-size` | `ANSWERLINEAGE_PROXY_SQL_QUEUE_SIZE` | 32 (1 à 10 000) |
| Résultats d'analyse gardés en cache | `--sql-cache-size` | `ANSWERLINEAGE_PROXY_SQL_CACHE_SIZE` | 1 024 (0 pour aucun, jusqu'à 1 000 000) |
| Fichier de la clé d'empreinte | `--fingerprint-key-file` | `ANSWERLINEAGE_PROXY_FINGERPRINT_KEY_FILE` | aucun |
| Clé d'empreinte elle-même, en hexadécimal | | `ANSWERLINEAGE_PROXY_FINGERPRINT_KEY` | aucune : pas d'empreinte |
| Durée d'une fenêtre des relevés de couverture | `--coverage-window-seconds` | `ANSWERLINEAGE_PROXY_COVERAGE_WINDOW_SECONDS` | 60 s (1 à 86 400) |
| Identifiant d'instance des relevés de couverture | `--instance-id` | `ANSWERLINEAGE_PROXY_INSTANCE_ID` | tiré au démarrage |
| Fichier des règles de lecture des outils non SQL | `--read-rules-file` | `ANSWERLINEAGE_PROXY_READ_RULES_FILE` | aucun : aucun outil non SQL n'est décrit |
| Adresse de l'API d'AnswerLineage, où partent événements et relevés, et d'où viennent les périmètres en quarantaine | `--api-url` | `ANSWERLINEAGE_PROXY_API_URL` | aucune : rien n'est envoyé, aucun périmètre n'est reçu |
| Événements gardés au plus dans la boîte d'envoi | `--outbox-max-events` | `ANSWERLINEAGE_PROXY_OUTBOX_MAX_EVENTS` | 10 000 (1 à 1 000 000) |
| Octets gardés au plus dans la boîte d'envoi | `--outbox-max-bytes` | `ANSWERLINEAGE_PROXY_OUTBOX_MAX_BYTES` | 32 Mio (1 Mio à 1 Gio) |
| Délai de vidage de la boîte d'envoi à l'arrêt | `--outbox-flush-seconds` | `ANSWERLINEAGE_PROXY_OUTBOX_FLUSH_SECONDS` | 5 s (0 à 300) |
| Intervalle entre deux interrogations des [périmètres en quarantaine](#périmètres-en-quarantaine) | `--quarantine-interval-seconds` | `ANSWERLINEAGE_PROXY_QUARANTINE_INTERVAL_SECONDS` | 10 s (1 à 30) |
| Fichier du secret partagé avec l'API, qui vérifie la [diffusion des périmètres](#une-diffusion-signée-et-datée) | `--quarantine-secret-file` | `ANSWERLINEAGE_PROXY_QUARANTINE_SECRET_FILE` | aucun |
| Secret de la diffusion lui-même | | `ANSWERLINEAGE_PROXY_QUARANTINE_SECRET` | aucun : les périmètres sont appris sans être vérifiés |
| En HTTP, attente au plus de la décision sur l'appel d'un outil visé par une quarantaine, comptée depuis l'arrivée de la requête ([Avertir](#avertir--lavertissement-dans-le-résultat)) | `--quarantine-decision-ms` | `ANSWERLINEAGE_PROXY_QUARANTINE_DECISION_MS` | 50 ms (0, n'attendre jamais, à 50) |
| Port d'administration, sur la boucle locale : [sondes de santé](#sondes-de-santé) `/livez` et `/readyz` | `--admin-port` | `ANSWERLINEAGE_PROXY_ADMIN_PORT` | aucun : aucune sonde |
| Délai de retrait : au signal SIGTERM, le proxy, non prêt, sert encore ([Arrêt gracieux](#arrêt-gracieux)) | `--shutdown-delay-seconds` | `ANSWERLINEAGE_PROXY_SHUTDOWN_DELAY_SECONDS` | 5 s (0 à 300) |
| Délai de grâce : l'écoute fermée, temps laissé aux appels en cours, au-delà coupés | `--shutdown-grace-seconds` | `ANSWERLINEAGE_PROXY_SHUTDOWN_GRACE_SECONDS` | 15 s (0 à 3 600, en secondes entières) |

L'adresse du serveur MCP ne porte ni identifiant ni paramètre : elle peut apparaître dans la configuration. Son hôte peut être une adresse IPv6, entre crochets (`http://[2001:db8::1]:9000`) ; une adresse IPv6 ne faisant pas un nom de serveur, `--server-name` est alors obligatoire. Un réglage invalide arrête le proxy avec le message « Configuration invalide : … » et le code de sortie 2.

## Ce qui est relayé

- Toutes les méthodes HTTP, le chemin et les paramètres tels que reçus, le chemin à la suite de celui de l'adresse du serveur MCP. Un chemin qui contient un segment `.` ou `..`, même encodé (`%2e`), sortirait de ce préfixe une fois résolu : il est refusé en `400` avec `{"error":"invalid_request_target"}`, sans être relayé. Les révisions 2026-07-28 (sans session) et 2025-11-25 (session, flux `GET`, clôture par `DELETE`) de MCP fonctionnent sans réglage.
- Tous les en-têtes, dans leur ordre, sauf ceux qui sont propres à une connexion (RFC 9110, section 7.6.1). Le proxy n'en ajoute aucun (ni `Server`, ni `Date`, ni `Forwarded`), à une exception près : le `traceparent` qu'il crée pour un appel d'outil arrivé sans contexte de trace (voir [Identifiant de run et contexte de trace](#identifiant-de-run-et-contexte-de-trace)).
- Le corps de la requête, octet pour octet : le proxy ne le modifie jamais, sauf, sur option et pour un outil SQL que l'on nomme, la requête de cet outil, qui commence alors par le commentaire de son run ([Étiquetage des requêtes de l'entrepôt](#étiquetage-des-requêtes-de-lentrepôt)).
- Le corps de la réponse, octet pour octet et au fil de l'eau : rien n'est décompressé ni retenu, un flux SSE arrive événement par événement. Une seule exception : pendant une quarantaine active et vérifiée, le résultat d'un appel d'un outil qui lit la zone reçoit, en tête de son contenu, un bloc de texte à gabarit fixe ; le début de sa réponse attend la décision, au plus 50 ms après l'arrivée de la requête ([Avertir](#avertir--lavertissement-dans-le-résultat)).
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

## Modes transparent et passerelle

Le proxy se place devant un serveur MCP de deux façons, selon l'URL que les clients joignent.

| Mode | Les clients joignent | Le serveur MCP reçoit dans `Host` | Réglage |
| --- | --- | --- | --- |
| Transparent | L'URL du serveur MCP, celle qu'ils connaissaient : son nom mène désormais au proxy | Le nom que le client a joint, inchangé | Par défaut |
| Passerelle | L'URL du proxy, qui n'est pas celle du serveur | Le nom du serveur MCP, que le proxy substitue | `--no-preserve-host` |

Dans les deux modes, le chemin et les paramètres reçus suivent celui de `--upstream`, et rien d'autre ne change : l'en-tête `Authorization`, le défi `401` et les métadonnées OAuth passent tels quels, sans être lus ([Autorisation](#autorisation)). Le proxy dit son mode au démarrage (« écoute sur 127.0.0.1:8717, mode transparent. »).

### Un jeton lié à son URL ne passe pas une passerelle

Un serveur MCP protégé par OAuth publie ses métadonnées de ressource protégée (RFC 9728) : elles nomment sa ressource, l'URL pour laquelle un jeton lui est émis. Le client MCP demande un jeton pour l'URL qu'il joint (indicateur de ressource, RFC 8707), et le serveur n'accepte qu'un jeton dont l'audience est sa ressource : un jeton émis pour un autre service ne vaut rien chez lui.

En mode transparent, l'URL que joint le client est celle du serveur : le jeton qu'il obtient est le bon, et il passe tel quel. Derrière une passerelle, le client joint l'URL du proxy :

- les métadonnées du serveur, relayées par le proxy, désignent une autre ressource que l'URL jointe : le client doit les écarter (RFC 9728, section 3.3) ;
- un jeton obtenu pour l'URL du proxy n'a pas la bonne audience : le serveur le refuse, comme il le doit.

Pour passer, le proxy devrait échanger ce jeton contre un autre, émis pour le serveur : il terminerait OAuth et détiendrait les jetons des agents, ce qu'il ne fait jamais. Relâcher la vérification d'audience du serveur n'est pas une issue non plus : un jeton émis pour n'importe quel service y serait accepté.

### La passerelle refusée

Le mode passerelle est donc refusé pour un serveur dont les jetons sont liés à son URL. Le proxy le sait de deux façons, avant de servir :

- l'exploitant le déclare (`--audience-bound-tokens`) : rien n'est lu du serveur ;
- sinon, en mode passerelle seulement, le proxy lit les métadonnées du serveur aux adresses de la RFC 9728 : `/.well-known/oauth-protected-resource` suivi du chemin de `--upstream`, puis le même préfixe à la racine. Un document qui déclare sa ressource (`resource`, une URL absolue sans fragment) suffit.

Le proxy s'arrête alors avec le code 2, sans rien citer du serveur ni de ses métadonnées :

```text
Configuration invalide : Le mode passerelle (--no-preserve-host) est refusé : le serveur MCP publie des métadonnées de ressource protégée OAuth (RFC 9728), ses jetons sont donc liés à son URL ; un client qui joint le proxy à sa propre URL obtient un jeton pour une autre audience, que le serveur refuse, et le proxy ne termine pas OAuth pour en obtenir un autre. Placer le proxy en mode transparent, sous l'URL du serveur MCP.
```

En mode passerelle, `--upstream` est donc l'URL du point MCP du serveur, chemin compris (`https://mcp.interne/mcp`) : les adresses des métadonnées s'en déduisent.

La réponse du serveur est une entrée hostile. La lecture n'envoie que `Accept` et `Accept-Encoding: identity`, aucun identifiant ; elle ne suit aucune redirection, ne lit rien de l'environnement, ne déploie pas un corps compressé, lit 64 Kio au plus, en JSON strict (une clé répétée ou une constante non numérique écartent le document), et s'arrête au bout de 10 s pour les deux adresses. Elle n'a lieu qu'au démarrage, jamais sur le chemin d'un appel. Un serveur qui ne publie rien (`404`, `410`) n'a pas de jetons liés connus : la passerelle démarre. Une réponse qui ne dit rien de sûr ne bloque pas non plus le démarrage, la capture restant ouverte en cas de panne : le journal le dit par un motif, sans rien recopier de la réponse, et rappelle `--audience-bound-tokens`.

| Motif | Réponse |
| --- | --- |
| `unreachable` | Serveur injoignable |
| `timeout` | Aucune réponse complète dans les 10 s |
| `redirect` | Une redirection, qui n'est pas suivie |
| `status` | Un autre statut que `200`, `404`, `410` ou une redirection |
| `encoded` | Un corps compressé |
| `not_json` | Un autre type que `application/json`, ou un JSON qui n'est pas strict |
| `too_large` | Plus de 64 Kio |
| `invalid_document` | Un document sans ressource lisible : absente, autre chose qu'une URL `http` ou `https`, avec un fragment, ou de plus de 2 048 caractères |

```text
WARNING answerlineage_proxy Mode passerelle : métadonnées OAuth du serveur MCP illisibles (timeout) ; le proxy ne peut pas dire si ses jetons sont liés à son URL. S'ils le sont, le serveur refusera les appels : le déclarer avec --audience-bound-tokens, ou placer le proxy en mode transparent.
```

### Placer le proxy en mode transparent

Le nom du serveur MCP mène au proxy, qui joint le serveur à une autre adresse :

1. le nom public (`mcp.exemple.fr`) désigne désormais le proxy, dans le DNS ou dans l'équilibreur qui termine TLS devant lui ; le serveur reste joignable du proxy, à son adresse interne ;
2. `--upstream` est cette adresse interne, sans chemin : le chemin que joint le client arrive inchangé au serveur ;
3. `--allow-host` ajoute le nom public aux [hôtes attendus](#hôtes-et-origines-attendus) dans `Host`.

```bash
uv run answerlineage-proxy --upstream http://10.0.4.12:9000 --server-name entrepot --host 0.0.0.0 --allow-host mcp.exemple.fr
```

Le serveur reçoit le `Host`, le chemin et le jeton du client : rien ne change pour lui. Un test le vérifie devant un serveur MCP protégé par OAuth, qui contrôle la signature et l'audience du jeton et publie ses métadonnées : le jeton du client passe octet pour octet et le serveur l'accepte. Derrière une passerelle, le jeton obtenu pour l'URL du proxy est refusé par le même serveur, et la configuration l'est aussi.

## Plusieurs réplicas

Plusieurs réplicas du proxy, sans état, se placent derrière un répartiteur (un équilibreur de charge, un service Kubernetes) qui n'envoie les appels qu'aux réplicas prêts. Chacun dit sa santé sur un port d'administration, et s'arrête sans faire échouer d'appel.

```bash
uv run answerlineage-proxy --upstream http://10.0.4.12:9000 --server-name entrepot --host 0.0.0.0 --allow-host mcp.exemple.fr --admin-port 8718 --shutdown-delay-seconds 10
```

Chaque réplica tient sa boîte d'envoi, sa synchronisation des quarantaines et ses relevés de couverture, sous son propre identifiant d'instance, tiré au démarrage : les réplicas n'ont rien à s'accorder.

### Sondes de santé

Avec `--admin-port`, le proxy sert sa santé sur un second port, qui n'écoute que sur la boucle locale (`127.0.0.1`), jamais sur le port des appels, dont tout chemin appartient au serveur MCP relayé.

| Route | Réponse |
| --- | --- |
| `GET /livez` | `200` `{"status":"alive"}` tant que le proxy tourne : la réponse vient de la boucle qui relaie les appels, et dit qu'elle tourne |
| `GET /readyz` | `200` `{"status":"ready"}` quand le proxy accepte des appels ; sinon `503`, avec `starting` pendant le démarrage, `draining` dès que l'arrêt est demandé, `stopped` à la fin |

`HEAD` y répond de même, sans corps. Les réponses sont fixes, d'une liste fermée : ni donnée d'appel, ni compteur, ni en-tête `Server` ou `Date`. Le port d'administration contrôle `Host`, `Origin` et `Sec-Fetch-Site` comme le port des appels, avec la seule boucle locale pour hôte attendu : une page web ouverte sur le poste ne le lit pas. Tout autre chemin reçoit `404`, toute autre méthode `405`. Il admet 8 connexions à la fois ; au-delà, le serveur HTTP répond `503` sans appeler l'application. Rien ne s'y relaie au serveur MCP, et il ne voit rien du trafic.

Prêt ne dépend ni du serveur MCP ni de l'API d'AnswerLineage : un proxy dont le serveur est injoignable relaie le `502` comme le ferait un client seul, et une API en panne ne retient aucun appel ([Boîte d'envoi](#boîte-denvoi)). Retirer tous les réplicas de la rotation parce que le serveur ou l'API tombe fermerait la capture au lieu de la laisser ouverte.

Une sonde `httpGet` de Kubernetes joint l'adresse du pod, pas sa boucle locale : la sonde s'exécute dans le conteneur, où Python est installé avec le proxy. `urlopen` lève une erreur sur un `503` : la commande échoue, et le réplica n'est pas prêt.

```yaml
readinessProbe:
  exec:
    command: ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8718/readyz', timeout=1)"]
  periodSeconds: 2
  failureThreshold: 1
livenessProbe:
  exec:
    command: ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8718/livez', timeout=1)"]
  periodSeconds: 10
```

### Arrêt gracieux

Au signal SIGTERM, celui des orchestrateurs et de `docker stop` :

1. le proxy se déclare non prêt : `/readyz` répond `503` `{"status":"draining"}` ;
2. il sert encore pendant le **délai de retrait** (`--shutdown-delay-seconds`, 5 s par défaut), le temps que le répartiteur le voie et le retire de sa rotation : un appel qui arrive encore est relayé comme les autres ;
3. il ferme son écoute et ses connexions inactives ;
4. il laisse aux appels en cours le **délai de grâce** (`--shutdown-grace-seconds`, 15 s par défaut, en secondes entières) pour se terminer. Au-delà, ils sont coupés : leur réponse reste inachevée, le client sait qu'elle est tronquée, leur événement part en `FAIL`, issue `transport_error`, et ils se comptent dans `ProxyStats.cut_at_shutdown` ;
5. il émet les événements des derniers appels, termine les analyses du SQL, remet la dernière fenêtre de couverture et vide sa [boîte d'envoi](#à-larrêt) dans sa propre borne (`--outbox-flush-seconds`, 5 s par défaut) ;
6. il sort avec le code 0.

SIGINT, celui d'un Ctrl-C au terminal, saute le délai de retrait ; un second signal, SIGTERM ou SIGINT, l'écourte. Un proxy qui ne démarre pas, son port déjà pris par exemple, sort avec le code 3. Le journal suit chaque étape, en nombres :

```text
INFO answerlineage_proxy Prêt : appels sur 0.0.0.0:8717, santé sur 127.0.0.1:8718 (/livez, /readyz).
INFO answerlineage_proxy Arrêt demandé (SIGTERM) : non prêt ; le proxy sert encore 10 s, le temps d'être retiré de la rotation.
INFO answerlineage_proxy Écoute fermée : 1 appel(s) en cours, 15 s au plus pour se terminer.
WARNING answerlineage_proxy Arrêt : 1 appel(s) coupé(s) au-delà du délai de grâce.
INFO answerlineage_proxy Arrêt terminé en 25.4 s.
```

Les délais se règlent ensemble :

- **le délai de retrait** couvre le temps que met le répartiteur à retirer le réplica : la période de sa sonde multipliée par le nombre d'échecs qu'il attend, plus le temps de propager sa décision (dans Kubernetes, le retrait du pod des points de terminaison du service, qui suit sa suppression en quelques secondes). Trop court, des appels trouvent porte close ; trop long, l'arrêt dure ;
- **le délai de grâce** couvre les appels les plus longs que sert le serveur MCP : un outil qui interroge un entrepôt pendant une minute en demande une ;
- l'orchestrateur attend leur somme, plus le vidage de la boîte, avant de tuer le processus : `terminationGracePeriodSeconds` au moins égal dans Kubernetes, dont les 30 s par défaut tiennent les 25 s des réglages par défaut. `docker stop` n'attend que 10 s : `docker stop --time 30`.

Rien de cela n'est sur le chemin d'un appel : le relais ne lit ni l'état ni les délais, et le port d'administration ne voit pas le trafic.

### La fenêtre mesurée

Un test (`proxy/tests/test_replicas.py`) lance deux réplicas par la ligne de commande, chacun dans son processus, devant le même serveur MCP de test, derrière un répartiteur écrit pour le test qui fait ce que fait un équilibreur : il sonde `/readyz` de chaque réplica toutes les 100 ms, retire de sa rotation celui qui ne se dit pas prêt, répartit les appels entre les autres et ne reprend jamais un appel en échec. Huit clients y font sans relâche des appels de 20 ms, un neuvième des appels de 2 s. SIGTERM arrête un réplica au moment où un appel long vient d'y commencer, avec un délai de retrait de 1 s et un délai de grâce de 5 s. Aucun appel n'échoue.

Mesuré sur cinq passages, depuis l'envoi de SIGTERM, sur un poste de développement :

| Moment | Mesuré | Ce qui le fixe |
| --- | --- | --- |
| Le répartiteur retire le réplica | 0,01 à 0,10 s | La période de sa sonde, 0,1 s |
| Dernier appel envoyé au réplica | 0,01 à 0,10 s ; 2 à 15 appels reçus après SIGTERM, tous servis | Le même retrait |
| L'écoute se ferme | 1,0 s | Le délai de retrait |
| L'appel long est rendu | 2,0 s | Sa durée, sous le délai de grâce |
| Le processus sort | 2,27 à 2,39 s | La fin du dernier appel, puis 0,3 s pour arrêter le serveur HTTP et vider la capture |

Les appels arrivés après SIGTERM l'ont tous été pendant le délai de retrait, et y ont été servis : c'est lui qui évite qu'un appel trouve porte close entre le signal et le retrait.

## Ce qui est lu

Le proxy lit le corps des requêtes `POST` pour reconnaître un appel d'outil. Il décide sur le corps, jamais sur les seuls en-têtes :

- un en-tête `Mcp-Method`, `Mcp-Name` ou `MCP-Protocol-Version` contredit par le corps est signalé dans l'événement (`headerMismatch`) et dans le journal, par son nom seulement ;
- l'événement décrit l'outil du corps ;
- la requête est relayée telle quelle : c'est le serveur MCP qui la refuse, comme la révision 2026-07-28 l'y oblige.

De la réponse à un appel d'outil, le proxy ne lit qu'une copie bornée, une fois la réponse rendue, pour y trouver une erreur JSON-RPC et son code ([Qualité de service](#lissue)). D'un appel retenu par une quarantaine, il lit aussi le début du résultat, 16 Kio au plus, pour y placer l'avertissement : ce début n'est gardé que le temps de la décision, puis relayé ([Avertir](#avertir--lavertissement-dans-le-résultat)).

Un nom d'outil hors de la forme recommandée par MCP (1 à 128 lettres, chiffres, `_`, `-`, `.`) n'est pas recopié : l'événement porte `invalidToolName`.

### Ce que le proxy ne peut pas inspecter

Le proxy ne décrit que ce qu'il lit sans ambiguïté. Une requête qu'il ne peut pas lire ainsi est relayée telle quelle, parce que la capture ne bloque jamais un agent, mais elle se compte (`uninspected`) : un appel d'outil peut s'y cacher, que le serveur exécutera peut-être sans qu'aucun événement n'en sorte.

| Requête | Pourquoi elle n'est pas décrite |
| --- | --- |
| Corps qui n'est pas du JSON, ou pas une requête JSON-RPC | Rien à lire |
| Clé répétée dans un objet, à n'importe quel niveau | Deux analyseurs JSON ne retiennent pas la même valeur : le proxy pourrait lire `ping` là où le serveur exécute `tools/call` |
| Lot JSON-RPC (un tableau de requêtes) | Les révisions prises en charge n'en admettent plus ; un serveur plus ancien exécuterait chaque appel du lot |
| Corps compressé (`Content-Encoding`) | Le proxy ne le déploie pas |
| Corps plus grand que la taille lue, dont le début ne nomme pas l'outil | Voir ci-dessous |

Le proxy ne devine pas : décrire un appel à partir d'une lecture douteuse ferait entrer dans le graphe un outil ou une lecture que le serveur n'a pas vus. Ce compteur dit ce que le lignage ignore : il est publié, fenêtre par fenêtre, dans les [relevés de couverture](#relevés-de-couverture).

### Corps plus grand que la taille lue

Au-delà de `--max-inspected-bytes`, le corps est relayé en flux, sans attendre sa fin : le serveur MCP en reçoit le début pendant que le client envoie la suite. Le proxy ne lit alors que ses 16 premiers Ko, où les clients MCP écrivent la méthode et le nom de l'outil avant les arguments. Un lecteur JSON strict y cherche `method` au premier niveau et `params.name` :

- il saute les autres valeurs (chaînes et leurs échappements, nombres, objets et tableaux imbriqués) : une clé `method` dans une chaîne ou dans un objet imbriqué ne compte pas ;
- il ne rend une valeur que s'il en est certain : un début qui s'achève avant la clé, ou qui s'écarte de la syntaxe JSON, n'établit rien ; une clé vue deux fois non plus ;
- son temps de lecture est linéaire, sans retour en arrière.

Si ce début établit un appel d'outil et son nom, l'événement est émis comme pour tout appel, compté dans `inspected_from_prefix` ; pour un outil SQL, la lecture est « non résolue », motif `request_too_large`, sans que la requête soit analysée. Sinon, la requête est relayée sans être décrite et comptée dans `uninspected`. C'est toujours le corps qui décide, jamais les en-têtes.

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

Une vue est rapportée sous son nom : les tables qu'elle lit ne sont pas connues du proxy. La requête et ses littéraux n'atteignent ni l'événement ni les journaux : l'événement n'en garde que la forme sans littéraux.

### Le SQL conservé sans ses littéraux

L'événement porte la requête sous une forme qui permet d'en reconnaître la structure et de relire ce que l'agent a demandé, sans aucune donnée métier : la facette de job standard d'OpenLineage `sql` ([`SQLJobFacet` 1-1-0](../spec/openlineage/facets/1-1-0/SQLJobFacet.json)), champs `query` (la forme) et `dialect` (celui de l'outil).

```text
reçu   : SELECT c.nom FROM crm.clients c WHERE c.nom = 'Jeanne Dupont' AND c.ville IN ('Lyon', 'Lille') -- demande de Paul
gardé  : SELECT c.nom FROM crm.clients AS c WHERE c.nom = ? AND c.ville IN (?)
```

La forme se construit sur l'arbre syntaxique de la requête, lu par sqlglot dans le dialecte de l'outil, jamais par des expressions régulières sur le texte :

- toute constante devient le marqueur `?`, quelle que soit son écriture : chaîne (entre apostrophes ou guillemets selon le dialecte, triple, brute, échappée, Unicode, nationale, délimitée par dollars de PostgreSQL), nombre, booléen, hexadécimal, binaire, octets, date ou heure typée (`DATE '…'`), intervalle entier (`INTERVAL '3 days'`), paramètre numéroté (`$1`), motif `LIKE`, `ILIKE`, `SIMILAR TO` ou d'expression régulière, chaîne et chemin JSON (`j ->> 'cle'`, `v:cle`) ;
- une liste `IN` de constantes devient `IN (?)` : la forme ne dépend pas de sa longueur ;
- les commentaires disparaissent ;
- les noms restent : tables, colonnes, fonctions, alias, paramètres nommés (`@p`), quand ils ont la forme d'un identifiant (lettres, chiffres, `_`, `$`, `*`, `-`). Un nom entre guillemets est un texte libre : s'il porte un espace, une ponctuation, une balise ou un caractère sans rendu, la requête n'a pas de forme.

La forme se calcule avec l'analyse des lectures, dans le même processus, à partir du même découpage et dans le même budget : elle ne retarde aucun appel, et une analyse abandonnée ou en panne n'en donne pas. Elle franchit la même frontière que les lectures : le proxy ne la croit que bornée (32 768 caractères), imprimable et sans marque de commentaire. Elle est gardée en cache avec le reste du résultat.

Au moindre doute, aucune forme n'est gardée, et l'événement n'a pas de facette `sql` : mieux vaut pas de forme qu'une fuite.

| Cas | Pourquoi |
| --- | --- |
| Requête qui ne se lit pas en entier (syntaxe inconnue, repli de l'analyseur sur du texte brut : `CALL`, `EXECUTE IMMEDIATE`) | Un fragment non lu pourrait porter une constante |
| Requête de plus de 100 Ko, ou forme de plus de 32 768 caractères | Taille maximale |
| Analyse abandonnée au budget, file pleine, processus en panne | L'analyse n'a pas abouti |
| Imbrication trop profonde | L'arbre ne se réécrit pas |
| Forme qui contient encore une chaîne ou un nombre, ou un mot égal au texte d'une constante de la requête qui n'y figurait pas aussi comme nom | Une constante que l'analyseur aurait changée en nom (`DATE_TRUNC('dupont', d)` lu comme une unité) serait sinon recopiée |

Seule exception au contrôle : une unité de temps (`DAY`, `MONTH`, `YEAR`…, un vocabulaire clos) que le dialecte écrit entre apostrophes, comme `DATE_TRUNC('MONTH', d)` en PostgreSQL. Elle ne dit rien des données.

Une instruction dont les lectures ne sont pas connues (`UPDATE`, `INSERT … VALUES`) garde sa forme : elle se lit en entier, la lecture reste « non résolue ».

La limite qui reste : un nom d'un seul mot choisi par l'auteur de la requête (alias, nom de colonne, de table ou de paramètre) est conservé tel quel. `SELECT nom AS "Dupont"` garde `"Dupont"` ; `SELECT nom AS "Jeanne Dupont"`, dont le nom n'a pas la forme d'un identifiant, n'a pas de forme.

### Un processus à part, un budget borné

Une requête pathologique de quelques dizaines de Ko peut occuper sqlglot plusieurs secondes. Un fil Python ne s'interrompt pas, et il disputerait l'interpréteur à la boucle qui relaie les appels : l'analyse tourne donc dans un processus à part, un interpréteur démarré à neuf (`python -I -m answerlineage_proxy.analyzer`) au premier appel à analyser. Le proxy lui confie les requêtes une à une, sur son entrée standard, et lit ses réponses sans jamais bloquer la boucle.

- **Budget.** Chaque requête dispose de `--sql-budget-ms` (50 ms par défaut), comptées à partir du moment où le processus annonce qu'il commence le calcul : ni son démarrage ni l'attente dans la file n'en consomment. Passé le budget, le processus est tué, le calcul s'arrête donc réellement, et un nouveau processus est démarré à la requête suivante.
- **File.** Les requêtes qui attendent leur tour sont au plus `--sql-queue-size`. File pleine, la lecture est « non résolue » sur-le-champ : ni mémoire sans borne, ni attente sans fin.
- **Pannes.** Toute attente a un délai, et une réponse du processus n'est crue que si elle a exactement la forme attendue (noms de tables et de colonnes compris). Un processus qui meurt, refuse de démarrer ou répond n'importe quoi donne une lecture « non résolue » : l'événement de l'appel est toujours émis.
- **Arrêt.** `drain()` attend les analyses en cours ; `aclose()`, appelé aussi à l'arrêt du serveur HTTP, arrête ensuite le processus. Si le proxy disparaît sans l'arrêter, le processus s'arrête sur la fin de son entrée, ou, en plein calcul, par une alarme du système une seconde après le budget.
- **Silence.** La sortie d'erreur du processus est fermée : un message d'erreur pourrait citer la requête.

### Cache

Deux requêtes identiques, avec les mêmes réglages d'analyse (dialecte, catalogue et base par défaut), ne s'analysent qu'une fois, y compris quand elles arrivent ensemble. Le cache garde au plus `--sql-cache-size` résultats et 4 millions de caractères de noms ; les moins récemment servis sortent les premiers. Sa clé est une empreinte BLAKE2b, à clé tirée au démarrage du proxy, du texte et des réglages : le texte de la requête n'est pas conservé, même en mémoire, au-delà de l'appel. Seul le résultat de l'analyse, des noms de tables et de colonnes et la forme sans littéraux, est gardé.

Une analyse abandonnée au budget, ou perdue par un processus mort en plein calcul, est mise en cache comme les autres. L'analyse est déterministe : la relancer coûterait de nouveau le budget, un processus tué et un redémarrage, et répéter une requête pathologique deviendrait un moyen d'occuper l'analyse aux dépens des autres appels. Une requête refusée faute de place, ou qu'un processus n'a pas pu commencer, n'est pas mise en cache : l'échec ne tient pas à elle.

### Motifs d'une lecture non résolue

| Motif (`unresolvedReason`) | Cause |
| --- | --- |
| `not_understood` | Syntaxe inconnue, ou forme que l'analyse ne couvre pas |
| `unsupported_statement` | `UPDATE`, `DELETE`, `MERGE`, procédure, SQL dynamique |
| `table_function` | Fonction de table, qui peut lire n'importe quoi |
| `unsupported_identifier` | Nom de table ou de colonne hors de la forme admise |
| `too_deep` | Imbrication trop profonde pour l'analyseur |
| `too_large` | Requête de plus de 100 Ko |
| `empty` | Requête vide |
| `missing_argument` | L'argument déclaré manque ou n'est pas une chaîne |
| `request_too_large` | Corps de la requête plus grand que la taille lue : la requête n'est pas lue |
| `budget_exceeded` | Analyse abandonnée au budget |
| `queue_full` | File des analyses pleine |
| `analyzer_unavailable` | Processus d'analyse impossible à démarrer, ou analyse interrompue par l'arrêt du proxy |
| `analyzer_failed` | Processus mort en plein calcul, ou réponse illisible |

### Compteurs

`ProxyStats` compte, sans aucune valeur issue des requêtes :

| Compteur | Ce qu'il compte |
| --- | --- |
| `inspected_from_prefix` | Appels décrits d'après le seul début d'un corps trop grand |
| `sql_from_cache` | Analyses servies par le cache, ou par une analyse identique en cours |
| `sql_over_budget` | Analyses abandonnées au budget, processus tué |
| `sql_refused` | Analyses refusées, file pleine |
| `sql_analyzer_failures` | Analyses perdues par un processus d'analyse en panne |

## Règles de lecture des outils non SQL

Un outil d'API, de fichiers ou de recherche documentaire ne dit pas ce qu'il lit, et aucun SQL ne le trahit. Une règle de lecture le déclare : quel outil, éventuellement sur quel serveur ; quels jeux de données il lit, et éventuellement quelles colonnes ; quel argument porte la clé lue. Les règles sont dans un fichier YAML, donné par `--read-rules-file` :

```yaml
version: 1
rules:
  - tool: get_price
    server: catalogue
    datasets:
      - namespace: postgres://referentiel:5432
        name: ref.tarifs
        columns: [produit, prix]
    key: produit
  - tool: search_docs
    datasets:
      - { namespace: "gs://documents", name: "politique%20voyages.pdf" }
    key: [filtre, 0, id]
```

| Champ | Contenu |
| --- | --- |
| `version` | `1`, la version du format |
| `rules[].tool` | Le nom de l'outil, sous la forme que MCP recommande (1 à 128 lettres, chiffres, `_`, `-`, `.`) |
| `rules[].server` | Facultatif : le nom du serveur (`--server-name`) pour lequel la règle vaut. Une règle sans serveur vaut pour tous ; celle qui nomme le serveur l'emporte. Un même fichier peut ainsi servir plusieurs proxys |
| `rules[].datasets` | 1 à 32 jeux de données lus, chacun par son espace de noms (`namespace`) et son nom (`name`), qui ont la forme qu'exige l'[ingestion](ingestion.md#identifiants) |
| `rules[].datasets[].columns` | Facultatif : 1 à 1 000 colonnes lues, sans doublon. Sans elles, la lecture porte sur tout le jeu de données |
| `rules[].key` | Facultatif : l'argument qui porte la clé lue, par son nom, ou un chemin de 1 à 8 étapes dans les arguments, noms d'objets et rangs de listes (0 à 9 999) |

### Le fichier, une entrée hostile

Le fichier est lu une fois, au démarrage, et une règle invalide empêche le proxy de démarrer, avec « Configuration invalide : Règles de lecture invalides : … » et le code de sortie 2 : on ne capte pas à moitié en silence. Il ne se relit pas à chaud.

- **Bornes.** 256 Kio au plus, vérifiés avant toute lecture ; 1 000 règles, 8 niveaux d'imbrication. Le fichier est en UTF-8.
- **Chargeur sûr.** PyYAML lit le fichier avec son chargeur sûr, qui refuse en plus ancres (`&`), alias (`*`) et étiquettes explicites (`!!str`, `!python/…`, `!`) avant de rien composer : pas d'inclusion, pas de référence, pas de bombe d'alias. Le document n'est jamais construit en objets Python : il est parcouru nœud par nœud.
- **Types.** Une valeur qui n'a pas le type attendu est refusée, pas convertie : une date, un nombre ou un booléen là où un nom est attendu (`name: 2026-11-02`, `tool: yes`), un texte là où la version est attendue.
- **Champs.** Un champ inconnu, répété ou absent est refusé : une faute de frappe (`colums`) ne fait pas perdre des colonnes en silence. Deux règles pour le même outil sur le même serveur aussi.
- **Noms.** Un nom de jeu de données, d'espace de noms ou de colonne suit la forme des [identifiants de l'ingestion](ingestion.md#identifiants), écrite une seconde fois dans le proxy pour qu'il ne dépende pas de l'API (`tests/test_contract_identifiers.py` les tient d'accord) ; il est gardé sous sa forme normalisée NFC.
- **Erreurs.** Le message nomme la règle, le champ et leur position, jamais une valeur du fichier ; d'une erreur de syntaxe YAML, dont le message citerait le texte fautif, seuls la position et le genre sont gardés :

```text
Configuration invalide : Règles de lecture invalides : règle 2, champ datasets[0].name (ligne 9, colonne 15) : un nom ne contient ni espace ni blanc (identifier_whitespace).
```

### Ce que dit l'événement

Un appel réussi d'un outil décrit porte en entrée les jeux de données de sa règle, leurs colonnes dans la facette standard `schema`, et sa lecture est résolue. Un appel en échec n'a rien fait lire. Un outil déclaré par `--sql-tool` garde l'analyse de sa requête : sa règle est ignorée, et le proxy le dit au démarrage.

**La clé lue** est une valeur d'argument : elle n'est jamais conservée telle quelle. Elle devient une empreinte à clé, la même fonction que les autres [empreintes](#empreintes-à-clé) du proxy, sur l'étiquette `answerlineage/fingerprint/key/v1` et la forme canonique JSON de la valeur (`"P-1042"` et `1042` n'ont donc pas la même empreinte). Une liste de valeurs simples donne une empreinte par valeur distincte, 100 au plus. Les empreintes sont posées sur chaque jeu de données lu, dans la facette `answerlineage_keys` ([`KeysInputDatasetFacet`](../spec/facets/1-0-0/KeysInputDatasetFacet.json)), champ `hmacs`. Qui détient la clé du locataire retrouve ainsi les appels qui ont lu la ligne d'une donnée reconnue fausse, sans que la valeur entre dans AnswerLineage.

Quand la clé ne peut pas être gardée, la lecture est déclarée pour tout le jeu de données, et `readKeys`, dans la facette `answerlineage_toolCall` ([version 1-1-0](../spec/facets/1-1-0/ToolCallRunFacet.json)), dit pourquoi :

| `readKeys` | Cas |
| --- | --- |
| `fingerprinted` | Les empreintes des clés lues sont sur chaque jeu de données |
| `no_fingerprint_key` | Pas de clé de locataire : aucune empreinte n'est calculée |
| `request_too_large` | Corps plus grand que la taille lue : les arguments ne sont pas lus |
| `missing_argument` | L'argument manque, est nul ou une liste vide |
| `unsupported_argument` | La valeur n'est ni un scalaire ni une liste de scalaires, ou n'a pas de forme canonique |
| `too_many_keys` | Plus de 100 clés : en garder une partie ferait écarter à tort un appel qui a lu une clé oubliée |

Sans `key` dans la règle, `readKeys` est absent. La facette ne se réclame de la version 1-1-0 que lorsqu'elle porte `readKeys`.

### Les outils à décrire

Un appel d'un outil qui n'a ni règle de lecture ni SQL déclaré est marqué : sa lecture est « non résolue », motif `undescribed_tool`, et il n'a pas d'entrée. Le proxy le compte (`ProxyStats.undescribed_calls`) et le journalise une fois par nom d'outil, un nom validé, jamais un argument ; au-delà de 1 000 noms, il ne journalise plus. Un nom d'outil refusé n'est pas marqué : il porte déjà `invalidToolName`.

La liste des outils à décrire se lit par l'API, à partir des événements reçus : [`GET /api/v1/ingestion/undescribed-tools`](ingestion.md#outils-à-décrire) rend, par émetteur, chaque outil marqué, le nombre de ses appels et le dernier reçu. Ce choix ne touche pas au schéma du graphe, garde le proxy sans état, et rassemble en un lieu ce que voient tous les proxys, qu'une liste en mémoire de chacun perdrait à chaque redémarrage. Un proxy lancé sans adresse d'API (`--api-url`) n'envoie rien : la liste est vide pour lui, et son journal est la seule trace.

## Relevés de couverture

Le proxy publie ce qu'il a vu, pour que l'on sache quelle part du trafic le lignage a réellement captée. Il tient ses compteurs par fenêtre de temps, une minute par défaut (`--coverage-window-seconds`), et remet chaque fenêtre close à sa destination d'événements, au format du [relevé de couverture](ingestion.md#relevés-de-couverture) de l'API :

```json
{
  "emitter": "mcp://entrepot",
  "instance": "3f9a0c1b7d2e4a65",
  "window": { "from": "2026-11-11T09:00:00.000000Z", "to": "2026-11-11T09:01:00.000000Z" },
  "calls_seen": 42,
  "events_emitted": 42,
  "events_lost": 0,
  "uninspected": 1
}
```

| Champ | Ce que le proxy compte |
| --- | --- |
| `emitter` | Son identifiant d'émetteur : `mcp://<serveur>`, l'espace de noms des jobs de ses événements |
| `instance` | Son identifiant d'instance : celui de `--instance-id`, sinon seize chiffres hexadécimaux tirés au démarrage |
| `calls_seen` | Appels d'outils décrits, y compris d'après le seul début d'un corps trop grand |
| `events_emitted` | Événements remis : acceptés par l'API quand la [boîte d'envoi](#boîte-denvoi) les envoie, pris en charge par la destination sinon |
| `events_lost` | Événements perdus : refusés par la destination, abandonnés par la boîte d'envoi (pleine, rejet de l'API, arrêt), et, à l'arrêt, ceux d'appels dont l'analyse n'avait pas abouti ; en stdio, ceux d'appels dont la réponse n'a pas pu être rattachée |
| `uninspected` | Requêtes relayées sans pouvoir être inspectées ([ci-dessus](#ce-que-le-proxy-ne-peut-pas-inspecter)) |

- **Un appel compte dans la fenêtre où tombe l'heure de son événement.** L'événement d'un outil SQL part une fois la requête analysée, parfois après la fin de sa fenêtre : la fenêtre ne se clôt qu'une fois tous ses appels réglés, et les relevés partent dans l'ordre du temps.
- **Une fenêtre sans appel est publiée aussi** : c'est le battement qui prouve que le proxy n'est pas muet. Un proxy arrêté, bloqué ou coupé de l'API ne publie plus, et le [contrôle des émetteurs muets](workers.md#émetteurs-muets) le signale.
- **Les fenêtres sont alignées** sur des multiples de leur durée depuis l'origine des temps Unix. La première commence au démarrage du proxy ; à l'arrêt, la fenêtre en cours s'achève à l'heure de l'arrêt. Deux vies successives du proxy publient donc des fenêtres disjointes, et l'intervalle où il était arrêté se lit comme un silence.
- **Rien ne retarde un appel.** Compter est une addition faite là où l'appel est déjà décrit ; la clôture des fenêtres tourne à part, réveillée à la fin de chaque fenêtre, et un échec de remise est journalisé par sa classe, sans atteindre un appel.
- **Une fenêtre ne s'attarde pas sur ses événements en route.** Avec la boîte d'envoi, un appel compte parmi les vus dès que son événement est pris en charge, parmi les émis une fois l'événement accepté par l'API, parmi les perdus s'il se perd. La fenêtre se clôt sans attendre : son relevé dit ce qui est déjà su, et la différence, `calls_seen − events_emitted − events_lost`, est encore en route.
- **Une issue après la clôture corrige la fenêtre.** Quand un événement d'une fenêtre déjà close est remis ou perdu, le proxy republie la même fenêtre avec ses compteurs à jour. Un relevé corrigé ne fait que grandir les compteurs du précédent : l'API, qui [fusionne](ingestion.md#fusion) au plus grand, garde le dernier, dans quelque ordre qu'ils arrivent. Dans la boîte, la correction remplace le relevé de la même fenêtre qui attendait encore.

Plusieurs proxys placés devant un même serveur partagent l'identifiant d'émetteur : chacun publie sous son instance, et l'API les [additionne](lecture.md#instances) au lieu de les confondre. L'identifiant d'instance suit la forme des [identifiants de l'ingestion](ingestion.md#identifiants), 255 caractères au plus, gardé sous sa forme NFC ; un identifiant invalide arrête le proxy avec « Configuration invalide : … », sans le citer. Réglé, il sert à reconnaître une instance d'un redémarrage à l'autre (le nom d'un pod, par exemple) ; tiré, il change à chaque démarrage, ce qui ne change rien à la mesure. Le proxy le journalise au démarrage.

Les relevés ne portent que des nombres, des heures et les identifiants de l'émetteur et de l'instance : rien d'une requête.

Sans adresse d'API, la destination par défaut compte les événements et les relevés sans les envoyer. Une destination fournie au proxy (`EventSink`) les reçoit ; une destination qui remet plus tard (`DeferredSink`, comme la boîte d'envoi) dit ensuite, événement par événement, s'il a été remis ou perdu.

## Boîte d'envoi

Avec `--api-url`, les événements et les relevés de couverture partent vers l'API d'AnswerLineage par une boîte d'envoi locale, bornée, hors du chemin des appels : une panne d'AnswerLineage ne ralentit ni ne bloque un agent.

La boîte d'envoi, comme le [jeton du proxy](#le-jeton-du-proxy-envers-lapi), les [empreintes à clé](#empreintes-à-clé) et la [dérivation de l'identifiant de run](#la-dérivation-à-loctet-près), est celle de l'émetteur commun (`emitter/`), que partage tout émetteur d'AnswerLineage : ce qui suit vaut pour chacun. Le proxy lui donne son journal, `answerlineage_proxy`.

```bash
uv run answerlineage-proxy --upstream http://127.0.0.1:9000 --api-url https://answerlineage.interne
```

### Rien n'attend la boîte

Prendre en charge un événement est un ajout en mémoire, en temps constant, sans entrée-sortie ni attente : aucun appel n'attend la boîte, et l'événement d'un appel n'est construit qu'une fois sa réponse rendue. Tout le reste se fait à côté, dans deux tâches de fond :

- **la sérialisation**, dans un fil, qui rend l'interpréteur à la boucle des appels après chaque événement : la boucle n'attend jamais plus que la sérialisation d'un seul événement ;
- **l'envoi**, par lots d'au plus 500 événements et 1 Mio avant compression, compressés en gzip (dans un fil aussi), à la route des lots, `POST /api/v1/lineage/batch`. Un événement plus lourd qu'un lot part seul. Les relevés partent avant les événements, chacun à `POST /api/v1/coverage`.

Livraison au moins une fois : un lot dont l'issue n'est pas sûre est renvoyé, et l'ingestion, [idempotente](ingestion.md#idempotence), ne l'enregistre qu'une fois.

### L'adresse de l'API

L'adresse est réglée par l'exploitant au démarrage ; elle n'est jamais tirée d'un événement ni d'une réponse. Elle est en `http` ou `https`, sans identifiant (`utilisateur:motdepasse@`), paramètre ni fragment : elle peut apparaître dans la configuration. Un chemin est admis, pour une API servie sous un préfixe (`https://hote/answerlineage`). Une adresse invalide arrête le proxy avec « Configuration invalide : … », sans la citer.

L'API n'accepte dans l'en-tête `Host` que les hôtes qu'elle attend ([réglages](ingestion.md#hôtes-et-origines-attendus)) : l'hôte de l'adresse réglée doit en être, sinon elle répond `403`, que la boîte prend pour une indisponibilité et reprend sans fin.

Le client de la boîte vérifie le certificat TLS, ne suit aucune redirection et ne lit rien de l'environnement (ni mandataire, ni fichier d'identifiants) : seule l'adresse réglée est jointe. Il borne la connexion à 5 s, chaque lecture et écriture à 10 s, et chaque envoi à 30 s en tout, pour qu'une réponse servie goutte à goutte ne le retienne pas. Il n'envoie aucun en-tête au-delà du type et de l'encodage du corps, et du [jeton du proxy](#le-jeton-du-proxy-envers-lapi) quand il est réglé. La réponse de l'API est lue au plus sur 1 Mio, et n'est jamais journalisée.

### Le jeton du proxy envers l'API

L'API demande un jeton porteur OIDC sur chacune de ses routes ([Authentification et rôles](securite/authentification.md)). Le proxy obtient le sien auprès de l'émetteur OIDC de l'entreprise, par l'octroi `client_credentials`, sous l'identité d'un client qui lui est propre ; l'API lui donne le rôle d'émetteur. Le même jeton sert à la boîte d'envoi et à la [synchronisation des quarantaines](#périmètres-en-quarantaine).

```bash
export ANSWERLINEAGE_PROXY_OIDC_CLIENT_SECRET_FILE=/run/secrets/proxy-entrepot
uv run answerlineage-proxy --upstream http://127.0.0.1:9000 --api-url https://answerlineage.interne \
  --oidc-issuer https://idp.interne/realms/lignage --oidc-client-id proxy-entrepot
```

| Réglage | Rôle |
| --- | --- |
| `--oidc-issuer`, `ANSWERLINEAGE_PROXY_OIDC_ISSUER` | L'émetteur OIDC, en `https` (en `http` sur la boucle locale seulement). Sans lui, la boîte appelle l'API sans jeton |
| `--oidc-client-id`, `ANSWERLINEAGE_PROXY_OIDC_CLIENT_ID` | L'identifiant du client du proxy chez l'émetteur |
| `--oidc-client-secret-file`, `ANSWERLINEAGE_PROXY_OIDC_CLIENT_SECRET_FILE` | Le fichier qui contient son secret |
| `ANSWERLINEAGE_PROXY_OIDC_CLIENT_SECRET` | Le secret lui-même, à la place du fichier ; aucune option ne le prend |
| `--oidc-scope`, `ANSWERLINEAGE_PROXY_OIDC_SCOPE` | Une portée à demander, pour un émetteur qui en exige une (Entra ID : `api://…/.default`) |
| `--oidc-audience`, `ANSWERLINEAGE_PROXY_OIDC_AUDIENCE` | Une audience à demander, pour un émetteur qui l'exige (Auth0) |

- **Le point de jeton est découvert** (`/.well-known/openid-configuration`) : le document doit nommer l'émetteur réglé, et le point de jeton est en `https` (en `http` sur la boucle locale seulement), sans identifiant. Le proxy s'y présente par `client_secret_basic`. La réponse est lue au plus sur 64 Kio ; un jeton qui n'est pas porteur, illisible ou de plus de 16 Kio est refusé.
- **En mémoire seulement.** Le jeton n'est écrit nulle part, ni journalisé, ni dans les compteurs. Il se renouvelle avant d'expirer : une minute avant, ou au cinquième de sa vie quand elle est courte ; sans durée annoncée, il vit cinq minutes. Un refus `401` de l'API le fait oublier, et l'envoi repris en demande un autre.
- **Le secret ne sort pas.** Il vient d'un fichier ou d'une variable d'environnement, jamais de la ligne de commande, que la liste des processus montre ; aucun message ne le cite, ni le chemin de son fichier. En [stdio](#mode-stdio), le serveur MCP lancé ne reçoit ni le secret ni la variable qui nomme son fichier. Le jeton ne part qu'en `https`, ou en `http` vers la boucle locale : un réglage qui l'enverrait en clair arrête le proxy.
- **Rien n'attend le jeton.** Il n'est demandé que par les tâches de fond, jamais sur le chemin d'un appel. Un émetteur en panne, lent (coupé à 10 s) ou qui refuse le client se lit comme une API en panne : les événements attendent dans la boîte, dans ses bornes, et la synchronisation des quarantaines garde son dernier état. Après un échec, l'émetteur n'est pas redemandé avant 5 s. Le journal dit l'épisode une fois, par sa classe ou son statut, jamais l'émetteur, le client ni le jeton.
- **Un réglage partiel est refusé** : l'émetteur, le client et le secret vont ensemble, et demandent une adresse d'API.

Ce jeton n'a rien à voir avec ceux des agents. L'[invariant 4](architecture.md#où-chaque-invariant-est-tenu), « le proxy ne termine pas OAuth et ne conserve aucun jeton », vise le trafic MCP : les jetons qu'un agent présente au serveur MCP traversent le proxy sans être lus ni gardés ([Autorisation](#autorisation)). Le jeton propre du proxy est l'identité du proxy lui-même auprès de l'API d'AnswerLineage : il ne sert à aucun appel relayé, et aucun jeton d'agent n'y entre.

### Réponses de l'API

| Réponse | Ce que fait la boîte |
| --- | --- |
| `2xx` | Le lot est remis. Les événements que le bilan désigne par leur rang (`rejected[].index`) sont rejetés ; un bilan illisible ou trop long ne désigne rien |
| `422` | Aucun événement du lot n'est valide : tous sont rejetés |
| `400`, `413` | Le corps entier est refusé : le lot est coupé en deux, jusqu'à isoler l'événement fautif, qui est rejeté seul ; la taille trouvée sert aux lots suivants |
| `401` | Le jeton du proxy est refusé : il est oublié, et le lot repris plus tard avec un nouveau |
| `429`, `5xx`, tout autre statut (dont `403`, un rôle qui n'ouvre pas la route), une redirection, une erreur réseau, un délai dépassé, un jeton que l'émetteur ne donne pas | Le lot est remis en tête de file et repris plus tard |

Un événement rejeté est abandonné et compté, jamais retenté. Un événement que la boîte ne sait pas sérialiser en JSON strict (un nombre non fini, par exemple) l'est aussi, sans être envoyé.

Une reprise attend de plus en plus : 0,5 s, doublées à chaque échec, jusqu'à 30 s, chaque attente tirée au hasard entre sa moitié et son tout pour que des proxys ne reviennent pas ensemble. `Retry-After`, en secondes ou en date, est respecté jusqu'à 60 s ; une valeur illisible est ignorée. Un succès remet l'attente à zéro. Un relevé suit les mêmes règles, à ceci près qu'un relevé refusé (`400`, `413`, `422`) est abandonné.

### Bornes, et ce qui se perd

La boîte garde au plus 10 000 événements et 32 Mio d'événements sérialisés (`--outbox-max-events`, `--outbox-max-bytes`), plus le lot en cours d'envoi ; les événements qui attendent d'être sérialisés comptent dans la borne en nombre. Pleine, elle abandonne le plus ancien. Les relevés ont leur propre file, que les événements n'évincent jamais : 10 000 relevés, une semaine de fenêtres d'une minute, au-delà desquels le plus ancien se perd.

La boîte est en mémoire : le proxy reste sans état. Un redémarrage perd ce qui n'était pas parti, et cette perte se voit comme un trou.

| Perte | Compteur de la boîte | Comment elle est marquée |
| --- | --- | --- |
| Boîte pleine : le plus ancien est abandonné | `dropped_overflow` | `events_lost` de la fenêtre de l'événement, relevé corrigé si elle est close |
| Rejet par l'API, ou événement impossible à sérialiser | `rejected` | Idem |
| Arrêt : ce qui n'est pas parti dans le délai | `dropped_shutdown` | Idem, si le délai laisse le temps au dernier relevé |
| Relevé perdu : file pleine, refusé, arrêt | `reports_dropped` | Sa période se lit comme un [silence](lecture.md#lire-un-trou), jamais comme une période couverte |
| Arrêt brutal ou redémarrage, sans vidage | aucun | Les relevés de ses dernières fenêtres manquent : silence ; un relevé déjà remis compte ses événements en route comme manquants au graphe (`missing_from_graph`) |

Chaque perte d'un événement est dite au transport, qui la compte dans la [fenêtre de couverture](#relevés-de-couverture) de l'heure de l'événement. Quoi qu'il arrive au relevé, le graphe fait foi : un événement qui manque se voit comme `missing_from_graph`.

### À l'arrêt

À l'arrêt du proxy, la boîte se vide pendant 5 s au plus (`--outbox-flush-seconds`). Ce qui n'est pas parti est alors compté perdu, et la dernière part du délai, un cinquième, une seconde au plus, sert à remettre les relevés corrigés qui le disent, chacun une fois. Un lot interrompu à l'arrêt est compté perdu, même si l'API l'avait reçu : le trou est alors surestimé, jamais caché. Avec un délai nul, rien n'est envoyé à l'arrêt.

En HTTP, la boîte se vide une fois les appels en cours rendus ou coupés, à la fin de l'[arrêt gracieux](#arrêt-gracieux). En stdio, elle se vide après la fin du serveur MCP : le délai de vidage borne alors le temps que la sortie du proxy, et son code, peuvent suivre celle du serveur.

### Compteurs

`Outbox.stats` compte, sans aucune valeur issue des événements ; `queued`, `queued_bytes` et `oldest_age` se lisent sur la boîte elle-même :

| Compteur | Ce qu'il compte |
| --- | --- |
| `queued` | Événements en file, lot en cours d'envoi compris |
| `queued_bytes` | Octets des événements sérialisés en file |
| `oldest_age` | Âge du plus ancien événement en attente, en secondes |
| `sent` | Événements remis : l'API les a acceptés |
| `dropped_overflow` | Événements abandonnés, boîte pleine |
| `dropped_shutdown` | Événements perdus à l'arrêt |
| `rejected` | Événements rejetés par l'API, ou impossibles à sérialiser |
| `retries` | Envois repris après un échec, ou un lot coupé en deux |
| `reports_sent`, `reports_dropped` | Relevés de couverture remis, et perdus |

Le journal ne cite ni l'adresse de l'API, ni une réponse, ni rien d'un événement : un épisode d'indisponibilité se dit une fois, par la classe de son premier échec ou son statut, puis son retour ; une boîte pleine, une fois par épisode ; des rejets, par leur nombre.

## Périmètres en quarantaine

Quand un responsable de données [met un incident en quarantaine](incidents.md#quarantaine), chaque proxy apprend le périmètre de l'incident : il interroge l'API qu'il connaît déjà pour sa boîte d'envoi (`--api-url`), sur `GET /api/v1/quarantine`, toutes les 10 secondes par défaut (`--quarantine-interval-seconds`, de 1 à 30). En mode avertir, et quand la [diffusion est vérifiée](#une-diffusion-signée-et-datée), le proxy HTTP en [avertit les lectures](#avertir--lavertissement-dans-le-résultat) : le résultat d'un appel qui lit ce périmètre porte, en tête de son contenu, un avertissement à gabarit fixe. Aucun appel n'est bloqué. Un appel qui ne lit pas le périmètre est relayé comme sans quarantaine. Le mode stdio apprend le périmètre sans annoter.

```bash
uv run answerlineage-proxy --upstream http://127.0.0.1:9000 --api-url https://answerlineage.interne --quarantine-interval-seconds 10
```

**En moins de 30 secondes.** Chaque attente entre deux interrogations est tirée entre 80 % et 100 % de l'intervalle, jamais au-delà, pour que des proxys démarrés ensemble ne reviennent pas ensemble ; une interrogation est coupée à 10 s. Au réglage par défaut, une quarantaine activée juste après une interrogation est connue 20 s plus tard au pire, et une levée de même ; un test le vérifie, et le test de bout en bout mesure le délai réel. Au réglage maximal, l'attente seule atteint 30 s. Chaque proxy interroge de son côté : plusieurs proxys, en HTTP comme en stdio, n'ont rien à s'accorder.

**Rien n'attend la synchronisation.** Elle tourne dans une tâche de fond, démarrée sans attendre la première réponse : le proxy démarre et relaie aussitôt, l'API joignable ou non. La vérification de la [signature](#une-diffusion-signée-et-datée) se fait avec la lecture de la réponse, dans un fil à part. Elle n'envoie que `Accept`, le [jeton du proxy](#le-jeton-du-proxy-envers-lapi) quand il est réglé et, quand le proxy tient déjà une version, `If-None-Match` : une liste qui n'a pas changé ne se retélécharge pas (`304`). Sans jeton disponible, l'interrogation échoue comme une API en panne ; un refus `401` fait oublier le jeton. Son client suit les règles de celui de la boîte d'envoi : certificat vérifié, aucune redirection suivie, rien de l'environnement ; 5 s pour la connexion et chaque lecture.

### Une réponse hostile est écartée

Une réponse de l'API est une entrée comme une autre. Elle est écartée en entier, et le proxy garde son dernier état, quand :

- elle n'est pas un `200` (ou un `304` pour la version tenue), pas du JSON, ou arrive compressée : elle n'est jamais décompressée ;
- elle dépasse 2 Mio : sa lecture s'arrête là ;
- elle n'est pas du JSON strict : clé répétée, constante non numérique, échappement (ni l'API ni un nom admis n'en écrivent) ;
- elle compte plus de 20 000 éléments JSON : un premier passage les compte, fait de courtes recherches que l'interpréteur interrompt pour rendre la main aux appels, avant que l'analyseur JSON, qui ne la rend pas, ne lise une réponse ainsi bornée, dans un fil à part ;
- un identifiant d'incident n'a pas la forme d'un UUID, ou revient deux fois ; une heure d'activation n'est pas une heure RFC 3339 en UTC, à la seconde ou à la microseconde, suffixée de `Z` (`invalid_activation`) ; un nom de jeu de données n'a pas la forme que l'[ingestion](ingestion.md#identifiants) admet (contrôle écrit dans le proxy, `names.py`) ; plus de 100 quarantaines, ou un périmètre vide ou de plus de 20 jeux.

Le journal dit le motif par un code (`réponse écartée, invalid_identifier`), jamais la réponse ni un nom. Les champs que le proxy ne lit pas sont ignorés. Une quarantaine d'un mode que le proxy ne connaît pas, après une mise à jour de l'API, est comptée et laissée de côté, avec un avertissement : mettre le proxy à jour.

### Une diffusion signée et datée

Le proxy n'accorde foi qu'à une diffusion que l'API a signée et datée, quand il partage un secret avec elle : un tiers qui se place sur le chemin, ou se fait passer pour l'API, ne peut ni lui faire croire à une quarantaine, ni en prolonger une levée. Le secret se règle des deux côtés, celui de l'API par `ANSWERLINEAGE_QUARANTINE_SECRET` ([incidents](incidents.md#signée-et-datée)), celui du proxy par un fichier ou une variable, jamais sur la ligne de commande, que la liste des processus montre :

```bash
export ANSWERLINEAGE_PROXY_QUARANTINE_SECRET_FILE=/etc/answerlineage/secret-diffusion
uv run answerlineage-proxy --upstream http://127.0.0.1:9000 --api-url https://answerlineage.interne
```

Chaque réponse porte l'en-tête `AnswerLineage-Signature: t=<heure>,v1=<signature>` ; la signature est un HMAC-SHA-256 de l'étiquette `answerlineage/quarantine/v1`, de l'heure et de la charge : le corps d'un `200`, ou, pour un `304`, qui n'a pas de corps, la version que le proxy tient. Le format et ses vecteurs sont publiés dans [`spec/quarantine/v1`](../spec/quarantine/v1/README.md).

- **Avant d'être lue.** La signature est vérifiée avant que le JSON de la réponse soit lu : ce qu'un tiers a écrit n'atteint pas l'analyseur.
- **`200` comme `304`.** Un `304` n'est bon que signé sur la version que le proxy tient, et récent : des `304` rejoués à l'infini ne prolongent pas un état levé.
- **Datée.** L'heure de la signature doit être à 5 minutes au plus de l'horloge du proxy, avant comme après : une réponse captée ne se rejoue pas au-delà. Les horloges de l'API et des proxys doivent donc être synchronisées (NTP).
- **Écartée comme une réponse hostile.** Une réponse non signée, mal signée, trop ancienne ou venue du futur est écartée en entier ; le journal dit son code, jamais son contenu : `unsigned`, `malformed_signature` (en-tête illisible ou répété), `signature_mismatch` (charge altérée, autre secret, autre version), `signature_expired`, `signature_from_future`. Le proxy garde son dernier état valide, puis revient en mode observer au bout d'une heure sans diffusion valide, comme pour une [panne](#une-panne-garde-le-dernier-état-connu-une-heure).
- **La rotation.** L'API signe sous son secret et sous le précédent pendant une rotation : un proxy qui connaît l'un ou l'autre accepte. On règle le nouveau secret à l'API avec l'ancien comme précédent, puis chez chaque proxy, puis on retire l'ancien de l'API.
- **Sans secret**, le proxy apprend les périmètres sans les vérifier, comme avant : il n'agit jamais sur une lecture (`annotating` reste faux), et le dit une fois au journal, au démarrage. Avec un secret, il n'adopte aucune réponse que l'API n'a pas signée : une API réglée sans secret le laisse en mode observer.
- **Le secret ne sort pas.** Ni le journal, ni la représentation des réglages, ni l'état de la synchronisation ne le citent ; un réglage refusé ne cite ni le secret ni le chemin de son fichier ; le serveur MCP lancé en [stdio](#le-lancement) ne le reçoit pas. C'est de l'ASCII imprimable sans espace, de 32 à 512 caractères, le même qu'à l'API ; régler un secret sans `--api-url` est refusé.

### Une panne garde le dernier état connu, une heure

API injoignable, lente (coupée à 10 s), en erreur ou hostile : le proxy garde le dernier périmètre connu, une heure au plus après la dernière synchronisation réussie. Une heure couvre un redémarrage, une mise à jour ou une bascule de l'API sans perdre une quarantaine en cours ; au-delà, le proxy ne peut plus savoir si elle tient encore. Il oublie alors ce périmètre et revient en mode observer : une quarantaine levée pendant une longue panne ne survit pas indéfiniment chez lui, et la capture reste ouverte en cas de panne, comme le reste du proxy. Aucune panne ne l'arrête.

### Le mode observer

Tant qu'aucune synchronisation n'a réussi (API injoignable au démarrage, sans `--api-url`, ou périmètre oublié après une heure de panne), le proxy est en mode observer : il relaie et décrit les appels comme toujours. Il le dit :

- **au journal**, en nombres, à chaque changement : le démarrage en mode observer, chaque synchronisation qui change le périmètre, le premier échec d'un épisode avec l'âge de la dernière synchronisation, le retour de l'API, l'oubli du périmètre ;
- **dans son état**, `McpProxy.quarantine_status()` (et `StdioProxy`), à côté de `ProxyStats` : `mode` (`observe` ou `synced`), le nombre de périmètres et de jeux connus, l'âge de la dernière synchronisation réussie (`last_sync_age`, nul sans aucune), `failing` tant que les interrogations échouent, et `annotating`, vrai quand le périmètre connu vient d'une [diffusion vérifiée](#une-diffusion-signée-et-datée), jamais sans secret.

```text
INFO answerlineage_proxy Quarantaine : mode observer jusqu'à la première synchronisation avec l'API, interrogée toutes les 10 s.
INFO answerlineage_proxy Quarantaine : seule une diffusion signée par l'API, et datée de moins de 5 min, est adoptée.
WARNING answerlineage_proxy Quarantaine : synchronisation en échec (ConnectError) ; mode observer, aucun périmètre connu.
INFO answerlineage_proxy Quarantaine : API de nouveau joignable.
INFO answerlineage_proxy Quarantaine synchronisée : 1 périmètre(s) en quarantaine, 2 jeu(x) de données.
WARNING answerlineage_proxy Quarantaine : synchronisation en échec (réponse écartée, signature_expired) ; le dernier état connu est gardé (1 périmètre(s), synchronisé il y a 12 s), 1 h au plus.
```

L'état ne passe pas par les relevés de couverture : ils mesurent ce que le lignage a capté, et leur format est un contrat de l'ingestion. Le proxy n'a pas non plus de route à lui : en HTTP, tout chemin appartient au serveur MCP qu'il relaie, et en stdio il n'écoute pas. Son [port d'administration](#sondes-de-santé) ne dit que s'il est vivant et prêt.

### Avertir : l'avertissement dans le résultat

Pendant une quarantaine active en mode `warn`, et seulement si la diffusion est [vérifiée](#une-diffusion-signée-et-datée), le proxy HTTP insère en tête du contenu du résultat d'un appel qui lit la zone un bloc de texte à gabarit fixe. L'hôte de l'agent passe ce bloc au modèle avec le reste du résultat, et le montre à l'humain s'il affiche les blocs : la réponse que l'agent en tire peut dire que ses données sont en quarantaine. Le proxy ne bloque aucun appel et ne retire rien du résultat ; un appel hors de la zone n'en voit rien.

```bash
export ANSWERLINEAGE_PROXY_QUARANTINE_SECRET_FILE=/etc/answerlineage/secret-diffusion
uv run answerlineage-proxy --upstream http://127.0.0.1:9000 --api-url https://answerlineage.interne \
  --sql-tool execute_sql=query:bigquery --read-rules-file regles.yaml --quarantine-decision-ms 50
```

Au démarrage, le proxy le dit : `Quarantaine en mode avertir : le résultat d'un appel d'un outil visé porte un avertissement, après une attente de la décision de 50 ms au plus ; un appel hors de la zone n'attend jamais.` En stdio, il dit qu'aucun résultat n'est annoté.

#### La zone, et les appels retenus

La zone est faite des jeux de données des périmètres en quarantaine active, indexés par espace de noms, puis par nom, avec les incidents qui les y mettent : un instantané immuable, calculé quand la synchronisation adopte une nouvelle version, hors du chemin des appels. Chaque jeu y porte une profondeur : 0 pour un jeu d'un périmètre, de 1 à 3 pour une vue ou une table dérivée. La diffusion ne porte pas encore de jeux dérivés : la zone est aujourd'hui celle des seuls périmètres, et un jeu dérivé, le jour où la diffusion en portera, sera nommé « (dérivée) » dans l'avertissement.

À l'arrivée d'une requête, le proxy cherche dans la zone si l'appel d'outil, lu sans ambiguïté, est retenu :

- **un outil SQL** déclaré par `--sql-tool`, quand son espace de noms porte au moins un jeu de la zone : sa décision attend l'analyse de sa requête ;
- **un outil à règle de lecture**, quand sa [règle](#règles-de-lecture-des-outils-non-sql) déclare un jeu de la zone : sa décision est statique, sans analyse ni attente.

Ne sont jamais retenus : un outil sans règle ni SQL déclaré, dont on ne connaît pas les lectures ; une requête que le proxy ne lit pas sans ambiguïté ; un lot ; et tout appel quand la diffusion n'est pas vérifiée, en mode observer, ou en stdio. Un appel qui n'est pas retenu est relayé à l'octet près, sans attente : sa rétention n'a coûté qu'une recherche dans un dictionnaire.

#### La décision, et ce qu'elle attend

| Décision | Quand | Ce que reçoit le résultat |
| --- | --- | --- |
| `certain` | La lecture établie touche la zone : une table que la requête lit, ou un jeu que la règle déclare | L'avertissement, qui nomme les jeux lus |
| `outside` | La lecture établie ne touche pas la zone | Rien : le résultat passe tel quel |
| `uncertain` | La lecture n'a pas été établie, quel qu'en soit le motif (`request_too_large`, `missing_argument`, `unsupported_statement`, `budget_exceeded`, `queue_full`, `analyzer_failed`…), ou pas avant l'échéance de la décision | L'avertissement incertain, qui nomme l'espace de noms de l'outil |

Pour un outil SQL, l'analyse de la requête part dès son arrivée, en parallèle de son exécution par le serveur, qui dure presque toujours plus qu'elle sur un entrepôt. Elle tourne dans un second processus d'analyse, dédié aux appels retenus, sous un budget fixe de 50 ms : elle n'attend pas derrière les analyses menées après les appels. Ce processus démarre et se préchauffe dès qu'une version vérifiée de la zone vise un outil SQL déclaré.

La réponse attend la décision au plus jusqu'à **l'arrivée de la requête plus l'attente réglée** (`--quarantine-decision-ms`, 50 ms par défaut, de 0 à 50). L'échéance se compte depuis l'arrivée, pas depuis la réponse : l'attente ajoutée est au plus de 50 ms, et nulle quand le serveur met plus que cela à répondre. Sans analyse à l'échéance, la décision est incertaine (`decision_timeout`). À 0, le proxy n'attend jamais : une décision qui n'est pas prête quand la réponse arrive donne un avertissement incertain. Ce temps d'attente compte dans la [latence ajoutée](#rien-ne-retarde-lappel), et l'événement le dit (`waitMs`).

L'événement de l'appel réutilise l'analyse faite pendant l'appel. Abandonnée à son budget de 50 ms, elle est refaite par l'analyseur ordinaire, sous le budget réglé (`--sql-budget-ms`) : le lignage reste aussi complet que pour un autre appel.

La capture reste ouverte en cas de panne : une API muette plus d'une heure ramène le proxy en mode observer, sans annotation ; une diffusion qui n'est pas vérifiée n'annote rien ; un analyseur en panne donne une décision incertaine, et l'appel passe ; un avertissement qui ne se rend pas laisse le résultat tel quel.

#### Le gabarit

L'avertissement est une information, pas un ordre. Une lecture certaine :

```text
[AnswerLineage] Avertissement sur la qualité des données : ce résultat lit des données qu'un incident déclare fausses.
Incident : 974e7c80-6643-4065-a541-e4c029abbdab, en quarantaine depuis le 2026-11-16 08:55 UTC.
Périmètre lu : bigquery ventes.suivi.commandes.
Une réponse fondée sur ce résultat peut être fausse tant que la quarantaine n'est pas levée.
```

Une lecture incertaine :

```text
[AnswerLineage] Avertissement sur la qualité des données, incertain : ce résultat peut lire des données qu'un incident déclare fausses ; sa lecture n'a pas pu être établie à temps.
Incident : 974e7c80-6643-4065-a541-e4c029abbdab, en quarantaine depuis le 2026-11-16 08:55 UTC.
Périmètre possible : espace de noms bigquery.
Une réponse fondée sur ce résultat peut être fausse tant que la quarantaine n'est pas levée.
```

Rien d'autre que le gabarit n'y entre :

- des **identifiants d'incident**, des UUID contrôlés à la lecture de la diffusion ;
- l'**heure d'activation** de la quarantaine, relue strictement (RFC 3339 en UTC), puis réécrite par le proxy, à la minute ;
- des **noms que l'agent ou l'exploitant ont écrits** : les tables que lit la requête de l'agent, telles que l'analyse les rend ; les jeux d'une règle de lecture ; l'espace de noms de `--sql-tool`. Un nom de la zone n'entre dans le texte que s'il est égal à l'un d'eux.

Aucun texte de l'API ni d'un émetteur n'y a de place : l'API n'admet aucun texte libre dans un incident, la diffusion n'en porte pas, et les champs qu'elle porterait en plus sont ignorés. Un test le vérifie avec une diffusion hostile qui porte des descriptions en forme de consigne et un nom de jeu en forme d'instruction : aucun n'atteint l'avertissement. Le texte nomme 3 incidents au plus, les plus anciennes quarantaines d'abord, et 5 jeux au plus, puis « et N autre(s) » : quelques Kio au pire.

#### Où entre le bloc, à l'octet près

Le bloc est un bloc de contenu texte MCP, inséré **en tête** du tableau `result.content`, sérialisé en ASCII pur (`é` s'y écrit `é`) :

```json
{"type":"text","text":"[AnswerLineage] Avertissement sur la qualité des données : …","annotations":{"audience":["assistant","user"],"priority":1}}
```

- **Dans `content`**, parce que les hôtes passent au modèle les blocs de contenu ; `_meta` est une métadonnée de protocole qu'ils ne lui transmettent pas, et `annotations.audience` nomme aussi l'humain.
- **En tête**, parce que les hôtes tronquent une sortie longue par la fin, et qu'un résultat SQL volumineux est justement celui qui compte.
- **Pas ailleurs** : `isError` changerait le sens de l'appel, que l'agent prendrait pour un échec ; `structuredContent` doit suivre le schéma de sortie de l'outil, que les clients valident ; préfixer le texte d'un bloc existant casserait un texte qu'un client relit comme du JSON.

Rien n'est relu puis réécrit. Un lecteur strict cherche, dans les 16 premiers Kio de la réponse, la place qui suit le `[` de `result.content`, au premier niveau du résultat : un `content` imbriqué, dans `structuredContent` par exemple, ne compte pas. Il ne conclut pas devant une clé répétée, un `resultType` autre que `complete` vu avant `content`, une erreur JSON-RPC, ou un identifiant qui n'est pas celui de la requête. Les octets avant et après sont recopiés tels quels ; le bloc est suivi d'une virgule si le tableau n'est pas vide.

- **Réponse JSON** : le relais retient les en-têtes et le début du corps jusqu'à la place et la décision, puis augmente `Content-Length` de la taille du bloc ; le reste passe au fil de l'eau.
- **Flux SSE** : les autres événements, progression, requêtes du serveur, passent aussitôt ; seule la première ligne `data:` de l'événement qui porte la réponse est retenue, le temps de la place et de la décision. Une réponse dont le JSON s'étend sur plusieurs lignes `data:` sans que `content` soit sur la première n'est pas annotée.
- **Empreinte et taille** du résultat (`resultFingerprint`, `resultSize`) portent sur les octets que le serveur a rendus, avertissement exclu : deux résultats identiques se reconnaissent toujours.

Un test fait passer l'avertissement par le vrai client du SDK MCP, en révision 2026-07-28 et 2025-11-25, en réponse JSON et en flux SSE : le client lit l'avertissement en `content[0]`, puis le contenu d'origine, et le résultat structuré d'un outil qui déclare un schéma de sortie reste valide.

#### Rien n'est inséré, et l'événement dit pourquoi

Quand la place ne se trouve pas avec certitude, la réponse passe telle quelle, et l'événement dit pourquoi (`notAnnotatedReason`) :

| Motif | Quand |
| --- | --- |
| `encoded` | La réponse porte un `Content-Encoding` : le proxy ne déploie rien |
| `body_bound` | La réponse porte `Content-Digest`, `Repr-Digest`, `Digest`, `Content-MD5`, `Signature` ou `Signature-Input`, une longueur illisible, ou un flux SSE qui annonce sa longueur |
| `error_response` | Un statut HTTP de 400 ou plus, une erreur JSON-RPC, ou un serveur injoignable |
| `no_content` | Un résultat sans tableau `content` (un `resultType` autre que `complete`), une redirection, ou une réponse ni JSON ni flux SSE |
| `not_whole` | Un début de réponse qui ne se lit pas entier et sans ambiguïté : place introuvable dans les 16 premiers Kio, clé répétée, autre identifiant, réponse en plusieurs lignes `data:`, réponse interrompue |
| `client_gone` | Le client est parti avant que le bloc soit inséré |

Ne sont pas annotés non plus, et ce n'est pas compté : un résultat rendu hors de la réponse à `tools/call` (une tâche de la révision 2025-11-25, un flux SSE repris par `Last-Event-ID`), un résultat `InputRequiredResult` de la révision 2026-07-28 (le résultat final, rendu par un nouvel appel, l'est), et un lot JSON-RPC.

#### Ce que dit l'événement

L'événement d'un appel retenu porte la décision dans la facette `answerlineage_toolCall`, en [version 1-3-0](facettes.md#answerlineage_toolcall) :

```json
"quarantine": {
  "decision": "uncertain",
  "incidents": ["974e7c80-6643-4065-a541-e4c029abbdab"],
  "uncertainReason": "decision_timeout",
  "annotated": true,
  "waitMs": 49.731
}
```

Des codes, des identifiants d'incident et une durée : jamais l'avertissement. Les compteurs `retained_calls`, `annotated_results` et `unannotated_results` de `ProxyStats` disent les appels retenus, les résultats annotés, et ceux qui devaient l'être sans l'avoir été.

## Identifiant de run et contexte de trace

L'identifiant de run d'un appel d'outil vient de son contexte de trace W3C ([Trace Context](https://www.w3.org/TR/trace-context/), en-tête `traceparent`). Lignage et traces se corrèlent sans nouvel en-tête, et tout émetteur qui voit le même contexte (deux proxys en série, un proxy et un adaptateur d'agent) émet pour le même run : les écritures du graphe, idempotentes, fusionnent leurs événements.

L'[adaptateur du Claude Agent SDK](adaptateurs.md) ne voit pas ce contexte : les crochets du SDK ne le portent pas. Un appel vu par lui et par un proxy fait donc deux runs ([ses limites](adaptateurs.md#limites-connues)).

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
| appel d'outil au corps plus grand que la taille lue | l'en-tête `traceparent` s'il est unique et bien formé, sinon un contexte créé | inchangée : `_meta` n'a pas été lu, rien n'est ajouté |
| requête qui n'est pas un appel d'outil, corps illisible, ou corps trop grand dont le début n'établit pas un appel d'outil | aucun (pas d'événement) | inchangée |

Un en-tête `traceparent` déjà présent n'est donc jamais remplacé ni doublé. Quand il est mal formé, le contexte créé ne sert qu'à l'identifiant de run : le transmettre ferait deux en-têtes `traceparent`, que le serveur ne saurait départager. Le corps n'est jamais modifié pour porter le contexte ; seule l'option d'[étiquetage](#étiquetage-des-requêtes-de-lentrepôt) y écrit l'identifiant de run, dans la requête SQL.

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

Exemple : `00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01` donne `a7e19836-59d9-8033-bd72-8a4bccb7271f`. Les vecteurs de test, valeurs acceptées avec leur identifiant et valeurs refusées, sont publiés dans [`spec/run-id/vectors-v1.json`](../spec/run-id/vectors-v1.json) pour vérifier tout autre émetteur. Le [kit de conformité](conformite.md) refait ce calcul pour juger un point de capture, et cherche par des canaris ce qu'un émetteur laisserait sortir : le proxy le passe sous le profil `capture`.

Un UUID version 5 aurait aussi convenu. La version 8 est retenue parce que la version 5 impose SHA-1, que l'on n'emploie plus dans une construction nouvelle, et un UUID d'espace de noms à publier en plus ; la RFC 9562 recommande la version 8 pour un nom haché par un autre algorithme, et en donne un exemple avec SHA-256. Tout langage a SHA-256 ; le calcul tient en cinq étapes. L'API d'ingestion accepte tout UUID de forme canonique. Une autre dérivation, demain, changerait d'étiquette (`…/v2`) et de fichier de vecteurs.

## Étiquetage des requêtes de l'entrepôt

Une requête qu'un outil envoie à un entrepôt de données y laisse une trace : BigQuery garde chaque job, son texte et ses labels (`INFORMATION_SCHEMA.JOBS`), Snowflake chaque requête, son texte et son tag (`QUERY_HISTORY`). Quand la requête porte l'identifiant de run de l'appel d'outil, la lecture que le lignage en tire se rapproche de ce que l'entrepôt a réellement exécuté, dans ses journaux d'audit. Ce rapprochement n'est pas encore fait par AnswerLineage : ce qui suit fixe ce que la requête porte, et comment.

Deux voies portent le même identifiant :

| Qui l'écrit | Où | Forme |
| --- | --- | --- |
| Le serveur MCP, qui lit le contexte de trace de l'appel | Label du job BigQuery | Clé `answerlineage_run_id`, valeur l'identifiant de run |
| Le serveur MCP, qui lit le contexte de trace de l'appel | Paramètre de session `QUERY_TAG` de Snowflake | `{"answerlineage_run_id":"<run>"}`, sans blanc |
| Le proxy, sur option, pour un outil SQL déclaré | Commentaire en tête de la requête | `/* answerlineage_run_id=<run> */`, suivi d'une espace |

L'identifiant de run est celui de l'événement de l'appel ([dérivation](#la-dérivation-à-loctet-près)), sous la forme canonique d'un UUID : 36 caractères, chiffres, lettres de `a` à `f` et tirets. Pour le contexte `00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01` :

```text
label BigQuery    : answerlineage_run_id = a7e19836-59d9-8033-bd72-8a4bccb7271f
QUERY_TAG         : {"answerlineage_run_id":"a7e19836-59d9-8033-bd72-8a4bccb7271f"}
commentaire SQL   : /* answerlineage_run_id=a7e19836-59d9-8033-bd72-8a4bccb7271f */ SELECT …
```

### Ce que les entrepôts admettent

**BigQuery** ([Introduction to labels](https://cloud.google.com/bigquery/docs/labels-intro)) : 64 labels au plus par ressource ; une clé de 1 à 63 caractères, qui commence par une lettre minuscule ou un caractère international, unique sur la ressource ; une valeur de 0 à 63 caractères ; des lettres minuscules, des chiffres, des soulignés et des tirets seulement, en UTF-8. La clé `answerlineage_run_id` (20 caractères) et l'identifiant de run (36) les respectent. Un job de requête porte ses labels dans sa configuration, et la vue `INFORMATION_SCHEMA.JOBS` les rend avec le job.

**Snowflake** ([Parameters](https://docs.snowflake.com/en/sql-reference/parameters)) : `QUERY_TAG` est un paramètre de session, une chaîne, posée par `ALTER SESSION SET QUERY_TAG = '…'` ; chaque requête de la session la porte, et `QUERY_HISTORY` la rend avec la requête. Le tag d'AnswerLineage est un objet JSON de 61 caractères ASCII, sans apostrophe : un serveur qui pose déjà un tag JSON y ajoute la clé `answerlineage_run_id`.

### La fonction de référence

[`warehouse.py`](../proxy/src/answerlineage_proxy/warehouse.py) calcule, depuis le contexte de trace qu'un serveur MCP reçoit avec l'appel, l'identifiant de run et ses trois formes. Elle n'importe que la bibliothèque standard, et rien d'AnswerLineage : elle se recopie telle quelle dans un serveur écrit en Python, et se réécrit en quelques lignes ailleurs, vérifiée par les [vecteurs de l'identifiant de run](../spec/run-id/vectors-v1.json). Elle choisit le contexte comme le proxy : `params._meta.traceparent` d'abord, puis l'en-tête HTTP `traceparent` s'il est unique et bien formé. Sans contexte lisible, elle rend `None`, et le serveur ne pose rien. Un contexte mal formé n'est jamais recopié : les trois formes ne contiennent que l'identifiant de run, de forme vérifiée, dans un gabarit fixe.

```python
from answerlineage_proxy.warehouse import warehouse_tags  # ou sa copie dans le serveur

# Pour une requête tools/call : `params` est celui du message reçu ; en HTTP, `traceparents`
# est la liste des valeurs de l'en-tête traceparent, en stdio une liste vide.
tags = warehouse_tags(params.get("_meta"), traceparents)
```

BigQuery, avec sa bibliothèque cliente Python :

```python
from google.cloud import bigquery

config = bigquery.QueryJobConfig(labels=tags.bigquery_labels if tags else {})
rows = client.query(sql, job_config=config).result()
```

Snowflake, avec son connecteur Python. `QUERY_TAG` vaut pour la session : il se pose avant la requête et se retire après, et une session partagée par des appels concurrents mélangerait leurs tags.

```python
cursor = connection.cursor()
if tags:
    # Le tag vient d'un gabarit fixe, sans apostrophe : il s'écrit tel quel entre apostrophes.
    cursor.execute(f"ALTER SESSION SET QUERY_TAG = '{tags.snowflake_query_tag}'")
try:
    cursor.execute(sql)
finally:
    if tags:
        cursor.execute("ALTER SESSION UNSET QUERY_TAG")
```

En stdio, sans `_meta`, le contexte que le proxy crée ne le quitte pas : le serveur ne connaît pas le run et ne pose rien. Le commentaire, que le proxy écrit lui-même, porte alors seul l'identifiant.

### Le commentaire que le proxy écrit, sur option

`--sql-run-comment OUTIL`, répétable, nomme un outil déclaré par `--sql-tool` dont la requête relayée commence par le commentaire de son run, en HTTP comme en stdio :

```bash
uv run answerlineage-proxy --upstream http://127.0.0.1:9000 --server-name entrepot \
  --sql-tool execute_sql=query:bigquery --sql-run-comment execute_sql
```

L'option est désactivée par défaut, parce qu'elle fait sortir le proxy de la stricte transparence : le serveur ne reçoit plus exactement ce que l'agent a envoyé. Un outil qu'elle ne nomme pas n'est jamais touché. Nommer un outil qu'aucun `--sql-tool` ne déclare arrête le proxy avec « Configuration invalide : --sql-run-comment nomme un outil que --sql-tool ne déclare pas. » et le code 2. Au démarrage, le proxy journalise chaque outil dont il modifiera les requêtes.

**Seule la chaîne de la requête change.** Le commentaire est écrit juste après le guillemet ouvrant de l'argument que déclare `--sql-tool`. Le corps n'est pas lu puis réécrit : le proxy y trouve la position du premier caractère de la requête et recopie tels quels les octets qui la précèdent et ceux qui la suivent, ordre des clés, blancs et échappements compris. En HTTP, l'en-tête `Content-Length` prend la nouvelle longueur ; aucun autre en-tête ne change.

```text
reçu   : {"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"execute_sql","arguments":{"query":"SELECT nom FROM crm.clients"},"_meta":{"traceparent":"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"}}}
relayé : {"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"execute_sql","arguments":{"query":"/* answerlineage_run_id=a7e19836-59d9-8033-bd72-8a4bccb7271f */ SELECT nom FROM crm.clients"},"_meta":{"traceparent":"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"}}}
```

**Le gabarit est fixe.** `/* answerlineage_run_id=`, l'identifiant de run, ` */`, puis une espace : rien d'une valeur reçue n'y entre. L'identifiant dérive du contexte de trace, que le proxy a validé avant toute chose ; sa forme canonique est vérifiée de nouveau avant l'écriture. Le commentaire ne contient donc ni `*/` qui le fermerait plus tôt, ni `/*` qu'un dialecte à commentaires imbriqués (PostgreSQL) ouvrirait, ni guillemet ni barre oblique inverse à échapper dans la chaîne JSON : un contexte de trace piégé n'atteint pas la requête. `/* … */` est un commentaire dans les trois dialectes du proxy ; il s'ouvre sur une espace, ce qui le distingue d'un indice d'optimiseur (`/*+`) ou d'un commentaire exécutable (`/*!`).

**L'analyse porte sur la requête d'origine.** L'inspection, l'analyse du SQL, la forme sans littéraux et l'empreinte des arguments portent sur la requête que l'agent a envoyée. L'événement dit seulement si la requête relayée a reçu son commentaire, par un indicateur, `taggedWithRunId`, dans la facette [`answerlineage_toolCall`](facettes.md#answerlineage_toolcall) en version 1-2-0 : vrai ou faux pour un outil qui a l'option, absent pour les autres.

**Ce que le proxy ne lit pas avec certitude n'est jamais modifié.** La requête part telle quelle, et l'appel compte parmi les non étiquetés :

| Cas | Pourquoi | `taggedWithRunId` |
| --- | --- | --- |
| Corps qui n'est pas du JSON, clé répétée, lot, corps compressé | [Il ne se lit pas sans ambiguïté](#ce-que-le-proxy-ne-peut-pas-inspecter) : un appel de l'outil peut s'y cacher | pas d'événement |
| Corps plus grand que la taille lue | Il est relayé en flux, avant d'être lu en entier | `false` |
| Corps qui n'est pas de l'UTF-8 strict : marque d'ordre des octets en tête, demi-paire de substitution encodée | Un lecteur JSON l'accepte, un autre non | `false` |
| En-tête `Content-Encoding`, `Content-Digest`, `Repr-Digest`, `Digest`, `Content-MD5`, `Signature` ou `Signature-Input` | Le corps modifié démentirait son codage, son empreinte ou sa signature (RFC 9530, RFC 9421) | `false` |
| En-tête `Mcp-Param-*` | Il recopie un argument, que le serveur peut comparer au corps. Seul son nom est regardé | `false` |
| Argument SQL absent, qui n'est pas une chaîne, ou requête vide | Rien à étiqueter | `false` |
| Requête qui commence, après ses blancs, par `#` ou `/*+` | La directive de dialecte de BigQuery (`#standardSQL`, `#legacySQL`) n'est lue qu'en première ligne, et pg_hint_plan ne lit un indice que dans le premier commentaire : un commentaire devant eux les priverait d'effet | `false` |

Le proxy compte, sans aucune valeur des requêtes, `ProxyStats.tagged_queries`, les appels dont la requête a reçu son commentaire, et `ProxyStats.untagged_queries`, ceux d'un outil à commentaire relayés sans lui, plus, dès qu'un outil a l'option, toute requête comptée dans `uninspected`, qui pouvait en cacher un.

**En stdio**, le fil qui relaie le client rassemble chaque ligne avant de l'écrire au serveur, qui ne la lit de toute façon qu'entière : un message coupé en morceaux part d'un seul tenant, sa fin de ligne reçue. Ce fil lit la ligne comme l'inspection, choisit le contexte de trace de l'appel (celui de `_meta`, sinon un contexte créé) et écrit la ligne, étiquetée s'il y a lieu ; l'inspection reçoit la ligne d'origine avec ce choix, si bien que l'événement et le commentaire portent le même run. Une ligne plus longue que la taille lue passe au fil de l'eau, telle quelle. Une ligne qui ne contient pas `tools/call` n'est pas lue par ce fil : un appel dont la méthode s'écrit avec un échappement (`tools\/call`) part tel quel, et l'inspection le compte non étiqueté. Un appel sans identifiant, que l'inspection ne décrit pas, n'est pas étiqueté. La latence d'un appel se compte alors depuis la lecture de la fin de sa ligne. Sans l'option, le relais stdio reste celui [décrit plus bas](#le-relais), morceau par morceau.

### Ce que l'option coûte

- **La transparence.** Un serveur qui contrôle le début de la requête, par exemple pour n'accepter que ce qui commence par `SELECT`, peut refuser une requête étiquetée : l'option s'essaie outil par outil avant d'être activée.
- **Les caches de résultats.** BigQuery ne sert une requête depuis son cache que si son texte est identique à celui de la précédente, blancs et commentaires compris ([Using cached query results](https://cloud.google.com/bigquery/docs/cached-results)) ; Snowflake ne réutilise un résultat persistant que pour une requête qui correspond à la précédente ([Using persisted query results](https://docs.snowflake.com/en/user-guide/querying-persisted-results)). Le commentaire change à chaque run : une requête étiquetée ne profite plus du cache de résultats, ce qui se paie en temps et, à la demande, en coût de calcul. Le label et `QUERY_TAG`, eux, ne changent pas le texte de la requête.
- **La latence.** Le proxy relit le corps de l'appel, en entier et strictement, avant de le relayer : une vingtaine de microsecondes pour une requête ordinaire, de l'ordre de 5 ms pour un corps de 1 Mo, la taille lue par défaut ; ce temps compte dans `addedLatencyMs`. En stdio, une ligne attend sa fin avant d'être écrite.

## Empreintes à clé

Des arguments et du résultat d'un appel, l'événement ne garde que des empreintes : deux appels aux mêmes arguments, ou deux résultats identiques, se reconnaissent sans que leur contenu entre dans AnswerLineage.

### La clé

Une empreinte est un HMAC-SHA-256 calculé avec une clé secrète propre au locataire. Deux locataires ont deux clés : la même valeur donne chez eux deux empreintes différentes, personne ne peut rapprocher leurs événements, et qui ne détient pas la clé ne retrouve pas une valeur peu variée (un code client, un montant) en essayant des candidates.

- **Fourniture.** La clé s'écrit en hexadécimal, 32 à 64 octets (64 à 128 caractères). Elle se donne dans un fichier (`--fingerprint-key-file`, ou `ANSWERLINEAGE_PROXY_FINGERPRINT_KEY_FILE`), qui ne contient qu'elle, ou dans la variable `ANSWERLINEAGE_PROXY_FINGERPRINT_KEY` ; pas les deux. Jamais sur la ligne de commande, que la liste des processus montre. Pour en tirer une : `python -c "import secrets; print(secrets.token_hex(32))"`.
- **Refus.** Une clé trop courte, trop longue ou qui n'est pas de l'hexadécimal, un fichier illisible : le proxy s'arrête avec « Configuration invalide : … » et le code 2. Le message ne cite ni la clé ni le chemin du fichier.
- **Discrétion.** La clé n'apparaît dans aucun journal, aucun événement, aucune représentation des réglages ni aucun message d'erreur.
- **Sans clé**, aucune empreinte n'est émise : jamais de repli sur un hachage sans clé, qu'une énumération suffirait à inverser. Le proxy le dit une fois, au démarrage.
- **Rotation.** Changer de clé rompt le rapprochement : les empreintes calculées avec la nouvelle clé ne se comparent plus à celles des événements déjà reçus. Une rotation se fait donc à une date connue, et les comparaisons s'arrêtent à cette date. La même clé, partagée par plusieurs proxys du même locataire, leur donne les mêmes empreintes.

### Ce qui est empreint

| Champ de `answerlineage_toolCall` | Octets empreints | Absent quand |
| --- | --- | --- |
| `argumentsFingerprint` | Forme canonique JSON ([RFC 8785](https://www.rfc-editor.org/rfc/rfc8785)) de `params.arguments`, en UTF-8 ; `{}` pour un appel sans arguments | sans clé ; corps plus grand que la taille lue ; arguments sans forme canonique (nombre non fini, texte qui n'est pas de l'Unicode valide) |
| `resultFingerprint` | Corps de la réponse, octet pour octet, tel que le serveur l'a rendu : compressé s'il l'est, flux SSE compris, sans l'[avertissement de quarantaine](#avertir--lavertissement-dans-le-résultat) que le proxy y insère | sans clé ; réponse interrompue ; client parti avant la fin |
| `resultSize` | Taille de ces mêmes octets ; ce n'est pas une empreinte, elle est émise même sans clé | réponse interrompue ; client parti avant la fin |

La forme canonique ne dépend ni de l'ordre des clés ni des blancs du JSON reçu : clés triées sur leurs unités de code UTF-16, aucun blanc, chaînes avec les seuls échappements obligatoires, nombres écrits comme en ECMAScript (`2.50` et `2.5` donnent `2.5`, `1.0` donne `1`, `1E-7` donne `1e-7`).

### Le calcul, à l'octet près

1. `message` = l'étiquette de l'usage en ASCII, un octet nul, puis les octets empreints. Les étiquettes : `answerlineage/fingerprint/arguments/v1` et `answerlineage/fingerprint/result/v1`. Des arguments et un résultat identiques ont donc des empreintes différentes.
2. `empreinte` = `hmac-sha256:` suivi de HMAC-SHA-256(`clé`, `message`) en hexadécimal minuscule.

En Python, sans rien d'AnswerLineage :

```python
import hashlib
import hmac

empreinte = (
    "hmac-sha256:"
    + hmac.new(
        cle, b"answerlineage/fingerprint/result/v1" + b"\x00" + corps, hashlib.sha256
    ).hexdigest()
)
```

Les vecteurs de test, avec deux clés de locataire, sont publiés dans [`spec/fingerprint/vectors-v1.json`](../spec/fingerprint/vectors-v1.json) ; ils ont été établis par une implémentation indépendante, en JavaScript, et les tests du proxy les relisent. Un autre émetteur qui retrouve ces valeurs calcule les mêmes empreintes avec la même clé. Un autre calcul, demain, changerait d'étiquette (`…/v2`) et de fichier de vecteurs.

### Ce que coûte le calcul

L'empreinte du résultat se calcule au fil du relais, morceau par morceau, après l'envoi de chaque morceau au client : la réponse n'est ni retenue ni attendue (hors d'un appel retenu par une quarantaine, dont l'empreinte se calcule sur les octets du serveur à leur arrivée). Mesuré sur la boucle locale, HMAC-SHA-256 passe environ 1,5 Go/s, soit une quarantaine de microsecondes par morceau de 64 Ko : sur une réponse de 8 Mo relayée en 40 ms environ, quelques millisecondes de plus ; sur un appel ordinaire, rien de mesurable. Sans clé, seule la taille est comptée.

L'empreinte des arguments se calcule après l'échange, quand l'événement est construit : quelques microsecondes pour des arguments ordinaires, de l'ordre de 7 ms pour 1 Mo, la taille lue par défaut, pendant lesquelles la boucle ne relaie rien d'autre.

## Qualité de service

Chaque appel porte, dans la facette `answerlineage_qos` ([`QosRunFacet` 1-1-0](../spec/facets/1-1-0/QosRunFacet.json)), ce que le proxy voit de l'échange : sa latence, la latence que le proxy y ajoute, son issue et ses reprises. Le proxy ne voit ni modèle, ni jetons, ni coût : ces champs de la facette restent absents.

```json
"answerlineage_qos": {
  "_producer": "https://github.com/alebgl77/AnswerLineage/tree/0.0.0/proxy",
  "_schemaURL": "https://raw.githubusercontent.com/alebgl77/AnswerLineage/main/spec/facets/1-1-0/QosRunFacet.json#/$defs/QosRunFacet",
  "latencyMs": 3.217,
  "addedLatencyMs": 0.243,
  "outcome": "jsonrpc_error",
  "errorCode": -32602,
  "errorCount": 1,
  "retryCount": 1
}
```

| Champ | Ce que le proxy mesure |
| --- | --- |
| `latencyMs` | De l'arrivée de la requête à la réponse rendue au client, ou à l'échec de l'échange : la latence de l'appel vue du proxy |
| `addedLatencyMs` | Ce que le proxy dépense lui-même avant de relayer la requête et avant de rendre chaque partie de la réponse : les attentes du client et du serveur MCP n'y comptent pas |
| `outcome` | L'issue de l'échange, d'une liste fermée (ci-dessous) |
| `errorCode` | Le code numérique de l'erreur : celui de l'erreur JSON-RPC, ou le statut HTTP. Absent quand l'issue n'a pas de code |
| `errorCount` | 1 pour une erreur, 0 sinon ; un abandon par le client n'est pas une erreur |
| `retryCount` | Le nombre d'appels qui précèdent celui-ci dans sa chaîne de reprises. Absent quand une reprise ne peut pas être reconnue, jamais zéro par défaut |

Les durées sont en millisecondes, à la microseconde, prises sur une horloge monotone : un réglage de l'horloge du système ne les fausse pas.

### L'issue

Dans cet ordre, la première qui s'applique :

| `outcome` | Cas | `errorCode` |
| --- | --- | --- |
| `client_abort` | Le client est parti avant la fin de la réponse | absent |
| `timeout` | Un délai du client HTTP du proxy est dépassé : connexion au serveur MCP (10 s), ou, quand le client HTTP en a un, lecture de la réponse | absent |
| `transport_error` | Serveur MCP injoignable, réponse interrompue | absent |
| `http_error` | Statut HTTP 400 ou plus, rendu par le serveur MCP ou par le proxy lui-même (chemin refusé) | le statut |
| `jsonrpc_error` | Statut de succès, mais la réponse JSON-RPC porte une erreur (`error`) | le code JSON-RPC, s'il est un entier sur 32 bits |
| `success` | Tout le reste | absent |

Une erreur se dit par sa catégorie et son code, jamais par son message : un message d'erreur peut citer les arguments de l'appel. Un code reçu qui n'est pas un entier signé sur 32 bits n'est pas recopié.

Pour y lire une erreur JSON-RPC, le proxy garde de la réponse une copie bornée à 16 Ko, au fil du relais, après l'envoi de chaque morceau au client : le début d'un corps `application/json`, la fin d'un flux `text/event-stream`, où le serveur écrit la réponse avant de le fermer. Il la lit une fois la réponse rendue, puis l'oublie :

- un corps JSON qui tient dans la copie est lu en entier, strictement : une clé répétée, un document illisible n'établissent aucune erreur ;
- un corps plus grand est lu par son début, avec le lecteur strict des corps trop grands ([ci-dessus](#corps-plus-grand-que-la-taille-lue)) : `error` au premier niveau, puis `error.code` ;
- dans un flux SSE, c'est la dernière réponse JSON-RPC complète qui compte ; une requête ou une notification du serveur n'en est pas une, et le premier événement d'une fin tronquée n'est pas lu.

Un corps compressé, ou d'un autre type, n'est pas lu : son issue tient au seul statut HTTP. L'issue ne change pas le type de l'événement : une erreur JSON-RPC rendue avec un statut de succès reste un événement `COMPLETE`, et l'état du run dans le graphe ne change pas.

### Les reprises

MCP ne marque pas une reprise. Le proxy la reconnaît ainsi : un appel reprend le précédent quand

- il nomme le même outil, avec la même empreinte des arguments ;
- il vient de la même portée : la même session MCP (en-tête `Mcp-Session-Id`, s'il est unique), sinon la même trace transmise par le client (`trace-id` de `_meta` ou de l'en-tête `traceparent`), sinon le même proxy ;
- le précédent n'a pas réussi (toute issue autre que `success`, abandon compris : un client qui renonce puis recommence reprend son appel) ;
- il commence après la fin du précédent, et au plus 60 secondes après.

`retryCount` compte les appels qui le précèdent dans cette chaîne : 0 pour un premier appel, 1 pour la première reprise, 2 pour la deuxième. Un succès clôt la chaîne : le même appel ensuite n'est pas une reprise. Deux appels identiques menés en même temps ne se reprennent pas l'un l'autre.

Sans clé d'empreinte, ou sans empreinte des arguments (corps plus grand que la taille lue, arguments sans forme canonique), une reprise ne peut pas être reconnue : `retryCount` est absent, et non nul. Un nom d'outil refusé fait de même.

La mémoire des chaînes est bornée : 10 000 chaînes ouvertes au plus, oubliées 60 secondes après leur dernier échec ; au-delà, la plus ancienne est oubliée, et sa reprise compte pour un premier appel. L'identifiant de session n'y est gardé que sous une empreinte BLAKE2b à clé tirée au démarrage : il n'est conservé ni dans la mémoire, ni dans l'événement, ni dans le journal. Une session étant celle que le client déclare, un client qui en usurpe une autre ne fausse que le compte de ses reprises.

### Rien ne retarde l'appel

Sur le chemin de l'appel, le proxy ne prend que des horodatages et garde la copie bornée de la réponse, après l'envoi de chaque morceau. L'issue, les reprises et l'événement se calculent une fois la réponse rendue, dans l'ordre où les appels s'achèvent. Un calcul de signaux qui échoue est journalisé par sa classe ; l'événement part sans la facette.

Une seule exception, bornée : pendant une quarantaine active en mode avertir, la réponse d'un appel retenu attend sa décision au plus jusqu'à l'arrivée de la requête plus 50 ms (réglable jusqu'à 0), puis passe ([Avertir](#avertir--lavertissement-dans-le-résultat)). Un appel hors de la zone n'attend jamais.

La latence ajoutée compte le travail du proxy entre deux attentes : contrôle des en-têtes, lecture du corps, contexte de trace, préparation de la requête relayée, puis, pour chaque morceau de la réponse, son envoi préparé, l'empreinte et la copie qui le suivent. Elle ne compte pas ce que font, pendant ces attentes, le serveur HTTP qui lit la requête du client et la bibliothèque HTTP qui écrit vers le serveur MCP : c'est une borne basse de ce qu'ajoute le saut par le proxy. Le banc mesure aussi le saut entier, par comparaison.

### Le banc de latence

`make bench` lance un serveur MCP de test et le vrai proxy devant lui, chacun dans son processus, servis comme la ligne de commande sert le proxy, avec une clé d'empreinte et sa [boîte d'envoi](#boîte-denvoi), vers une API d'ingestion qui refuse la connexion : la boîte garde, abandonne le plus ancien et reprend pendant tout le banc. Il appelle l'outil 100 000 fois directement et 100 000 fois à travers le proxy, en alternance, après 50 appels de mise en route, et rend les p50, p99 et maximum de deux mesures de la latence ajoutée :

- **par comparaison** : chaque percentile des appels à travers le proxy, moins le même percentile des appels directs, vus du client ;
- **par la mesure interne** : `addedLatencyMs` de l'événement de chaque appel.

Il échoue quand le p99 de l'une ou de l'autre dépasse 5 ms, et quand la mesure interne n'est pas cohérente : un événement réussi par appel mesuré, une latence ajoutée qui ne dépasse pas la latence de l'échange, une latence vue par le proxy dont la médiane ne dépasse pas celle vue par le client. Le proxy prend son dernier horodatage après avoir rendu la fin de la réponse : sur une machine chargée, un appel isolé peut finir plus tard pour lui que pour le client, si bien que la queue de la distribution n'est pas comparée. `BENCH_ARGS` règle le nombre d'appels, les appels menés à la fois, le seuil et la variante de quarantaine (`make bench BENCH_ARGS='--calls 20000 --concurrency 4 --quarantine outside'`).

Trois variantes mesurent ce que coûte une [quarantaine en mode avertir](#avertir--lavertissement-dans-le-résultat), sur une zone vérifiée que le proxy du banc tient d'une synchronisation simulée, sans API (`--quarantine`) :

- `none`, par défaut : aucune quarantaine ;
- `outside` : une zone qui ne vise pas l'outil du banc. Chaque appel coûte une recherche dans la zone et n'attend jamais ;
- `retained` : l'outil du banc est décrit par une règle de lecture dont le jeu est dans la zone. Chaque appel est retenu, sa décision est statique, et son résultat reçoit l'avertissement : la variante mesure la retenue du début de la réponse, le rendu et l'insertion du bloc. Le banc vérifie aussi que le client reçoit le résultat annoté, et que chaque événement dit le résultat annoté.

Un outil SQL retenu n'est pas au banc : quand sa requête est dans le cache de l'analyseur de quarantaine, sa décision est prête avant la réponse du serveur, et le coût est celui de la variante `retained` ; hors du cache, l'attente est bornée par construction à l'échéance de la décision, 50 ms au plus, que le seuil de 5 ms ne juge pas.

Le banc tourne chaque nuit dans son propre workflow (`.github/workflows/bench.yml`), sur une machine qui ne fait que lui, dans ses trois variantes : c'est là que le seuil de 5 ms au p99 est strict, la boîte d'envoi comprise. Dans `make check`, un test de 2 000 appels par variante vérifie que le banc fonctionne et que la mesure interne est cohérente, sans seuil de temps : sur une machine partagée, la charge des autres tests le ferait échouer à tort.

Dans `make check` aussi, un test de chaos (`tests/test_outbox_chaos.py`) fait passer 10 000 appels l'API d'ingestion coupée (connexion refusée), puis en trou noir (elle accepte et ne répond jamais), puis rétablie, sur la vraie API, dans son propre processus, un vrai PostgreSQL et le worker. Il vérifie que tous les appels passent, qu'aucun n'approche le délai de lecture de la boîte (8 s au plus, quand un appel qu'elle bloquerait en attendrait au moins 10), que la prise en charge d'un événement reste sous 1 ms au p99, et que chaque état de panne garde un p99 au plus double de celui d'un proxy sans boîte, plus 20 ms. Ce seuil est tolérant à dessein : le proxy, le faux serveur MCP et le client y partagent un processus, sur une machine partagée. Le test dure une à trois minutes. Un autre fait passer un appel en stdio, par la ligne de commande, jusqu'au graphe. Puis que chaque événement arrive dans le graphe, une fois. D'autres tests y injectent les pertes, boîte pleine, rejet par l'API, arrêt, et vérifient que la couverture lue par l'API les montre toutes, chacune dans sa fenêtre.

Mesuré sur un poste à 4 cœurs, 100 000 appels un à la fois :

| Mesure | p50 | p99 | max |
| --- | --- | --- | --- |
| Appel direct, vu du client | 1,32 ms | 2,76 ms | 77,96 ms |
| À travers le proxy, vu du client | 2,75 ms | 5,31 ms | 53,56 ms |
| Latence ajoutée, par comparaison | 1,43 ms | 2,55 ms | — |
| Latence ajoutée, mesure interne | 0,24 ms | 0,49 ms | 14,13 ms |

L'écart entre les deux mesures est le saut lui-même : un serveur HTTP qui lit la requête et une bibliothèque HTTP qui la réécrit vers le serveur MCP, que la mesure interne ne voit pas. Un maximum est une valeur isolée, une pause du ramasse-miettes ou de l'ordonnanceur ; par comparaison, la différence de deux maxima n'a pas de sens, et le banc ne la juge pas.

Ce tableau précède la boîte d'envoi. Sur 20 000 appels un à la fois, mesurés coup sur coup sur une même machine partagée, la boîte, l'API injoignable et la file pleine, ne déplace pas ces percentiles au-delà du bruit : p99 de la latence ajoutée par comparaison 2,38 ms avec elle, 2,39 ms sans ; par la mesure interne 0,46 ms avec elle, 0,43 ms sans.

La quarantaine, mesurée de même : 20 000 appels un à la fois par mesure, sur une même machine partagée, en deux tours. Le code d'avant l'avertissement, `none` et `outside` sont lancés en alternance ; `retained`, dans une seconde série, en alternance avec `none`. Le p99 de la latence ajoutée, tour par tour :

| Variante | Par comparaison | Mesure interne |
| --- | --- | --- |
| Avant l'avertissement, sans quarantaine | 2,33 ms ; 2,30 ms | 0,56 ms ; 0,55 ms |
| `none` : sans quarantaine | 2,16 ms ; 2,46 ms | 0,54 ms ; 0,60 ms |
| `outside` : quarantaine hors de l'outil | 2,17 ms ; 2,30 ms | 0,54 ms ; 0,54 ms |
| `retained` : quarantaine sur l'outil, résultat annoté | 2,55 ms ; 2,28 ms | 0,73 ms ; 0,69 ms |

Hors de la zone, l'écart reste dans le bruit de la machine. Sur un outil retenu, la médiane de la mesure interne passe de 0,26 à 0,38 ms : la retenue du début de la réponse, le rendu et l'insertion du bloc, loin du seuil de 5 ms.

## Mode stdio

Un serveur MCP local se lance d'ordinaire en stdio : le client le démarre et lui parle par son entrée et sa sortie standard, un message JSON-RPC par ligne. Le proxy s'intercale en le lançant lui-même : le client lance le proxy à la place du serveur, avec la commande du serveur après `--`.

```bash
uv run answerlineage-proxy [réglages] -- <commande> [arguments…]
```

Dans la configuration d'un client MCP, la commande devient `answerlineage-proxy` et ses arguments `--server-name fichiers -- /opt/mcp/serveur-fichiers --racine /srv/partage`. La commande commune fait de même, `answerlineage proxy [réglages] -- <commande>`, quand le proxy est installé avec elle ([Ligne de commande](cli.md#answerlineage-proxy)). Tout ce qui suit le premier `--` est la commande, y compris un second `--`. Le mode stdio est réservé aux systèmes POSIX (Linux, macOS).

### Le lancement

- La commande est lancée directement, comme liste d'arguments, jamais par un shell : `$HOME`, `*` ou `|` y sont des caractères comme les autres.
- L'enfant reçoit l'environnement du proxy tel quel, sauf les secrets du proxy : `ANSWERLINEAGE_PROXY_FINGERPRINT_KEY`, la clé d'empreinte du locataire, le secret du client OIDC du proxy (`ANSWERLINEAGE_PROXY_OIDC_CLIENT_SECRET` et `…_FILE`) et celui de la diffusion des quarantaines (`ANSWERLINEAGE_PROXY_QUARANTINE_SECRET` et `…_FILE`) ne lui sont pas transmis. Un serveur MCP n'a pas à les connaître, et l'environnement d'un processus se lit plus facilement que sa mémoire.
- Sa sortie d'erreur est celle du proxy : le proxy ne la lit pas et ne la recopie pas, ce sont les journaux du serveur. Le journal du proxy y passe aussi. Sur sa sortie standard, le proxy n'écrit que ce que l'enfant écrit.
- Une commande qui ne démarre pas arrête le proxy avec un message fixe, qui ne cite ni la commande ni ses arguments : « Serveur MCP non lancé : commande introuvable. » (code 127) ou « Serveur MCP non lancé : commande non exécutable. » (code 126), comme un shell POSIX.
- Sans `--server-name` ni `ANSWERLINEAGE_PROXY_SERVER_NAME`, le nom du serveur dans le lignage est celui du programme lancé, sans son chemin (`serveur-fichiers` pour `/opt/mcp/serveur-fichiers`). Un lanceur (`npx`, `uvx`, `docker`, `python`) n'est pas un nom de serveur : il confondrait tous les serveurs qu'il lance, d'où `--server-name`.
- Les réglages de la capture valent comme en HTTP : `--server-name`, `--max-inspected-bytes`, `--sql-tool`, `--sql-run-comment`, `--sql-budget-ms`, `--sql-queue-size`, `--sql-cache-size`, la clé d'empreinte, `--read-rules-file`, `--coverage-window-seconds`, `--instance-id`, l'adresse de l'API et les bornes de la [boîte d'envoi](#boîte-denvoi), l'intervalle des [périmètres en quarantaine](#périmètres-en-quarantaine) et le secret de leur diffusion, et leurs variables. Ceux du réseau n'ont pas de sens : `--upstream`, `--host`, `--port`, `--allow-host`, `--allow-origin`, `--no-preserve-host`, `--audience-bound-tokens`, `--admin-port`, `--shutdown-delay-seconds` et `--shutdown-grace-seconds` arrêtent le proxy avec « Configuration invalide : L'option --upstream est sans objet en mode stdio. » et le code 2 ; leurs variables d'environnement sont ignorées.

### Le relais

Deux fils relaient les flux, un par sens. Chacun lit ce qui arrive, 64 Ko au plus à la fois, l'écrit aussitôt de l'autre côté et remet une copie à l'inspection. Rien n'est réécrit ni retenu : les octets que reçoit le client sont ceux qu'écrit l'enfant, et inversement, au fil de l'eau, un message coupé en plusieurs morceaux compris. Seule exception, sur option : pour un outil qui reçoit le [commentaire de son run](#le-commentaire-que-le-proxy-écrit-sur-option), le fil du client rassemble chaque ligne avant de l'écrire.

### Ce qui est lu

L'inspection découpe chaque flux en lignes, à côté du relais, sur une copie, et lit chaque message comme le mode HTTP lit un corps :

- jusqu'à `--max-inspected-bytes`, le message est lu en entier, strictement ; au-delà, seul son début (16 Ko) l'est, par le [lecteur strict des corps trop grands](#corps-plus-grand-que-la-taille-lue), qui y cherche aussi l'identifiant ;
- une ligne illisible, un lot, une clé répétée, un appel d'outil sans identifiant (une notification : rien ne dira comment il s'achève), un message inachevé quand le flux se ferme sont relayés et comptés dans `uninspected` ;
- un appel d'outil est rattaché à sa réponse par son identifiant (`id`, une chaîne ou un entier). Les requêtes en attente sont gardées sous une empreinte BLAKE2b de leur identifiant, à clé tirée au démarrage : 1 024 au plus, et 32 Mo des appels gardés pour être décrits (ou quatre fois la taille lue si elle est plus grande) ; au-delà, la plus ancienne est oubliée ;
- les requêtes et notifications du serveur, et les réponses du client à celles-ci, ne sont pas des appels : elles passent sans rien compter ;
- `notifications/cancelled` du client clôt l'appel qu'elle nomme : il est abandonné.

Une fois la réponse rendue au client, l'appel est décrit avec les règles du mode HTTP : analyse du SQL, règles de lecture, empreintes, signaux de qualité de service, relevés de couverture. L'empreinte et la taille du résultat portent sur les octets du message de réponse, sans sa fin de ligne.

### Ce que l'inspection ne devine pas

Une réponse dont l'identifiant ne se lit pas (ligne illisible, identifiant écrit après un résultat plus grand que le début lu), ou un identifiant repris avant sa réponse, ne permettent plus de savoir quel appel s'achève. Le proxy ne choisit pas : l'appel en attente n'a pas d'événement, il compte dans `events_lost` de sa fenêtre et dans `ProxyStats.unmatched_calls`, et le journal le signale sans rien citer du message. Le nouvel appel à l'identifiant repris compte dans `uninspected`. Un appel resté sans réponse quand le serveur s'arrête est en échec, sauf si une réponse illisible est passée depuis son départ : elle était peut-être la sienne, et son événement est perdu plutôt que faux.

### Contexte de trace

Il n'y a pas d'en-têtes : le contexte se lit dans `params._meta.traceparent`, avec la validation et la dérivation de l'identifiant de run du mode HTTP ([ci-dessus](#identifiant-de-run-et-contexte-de-trace)), et `traceOrigin` vaut `meta`. Sinon, le proxy en crée un (`traceOrigin` : `proxy`), qui ne sert qu'à l'identifiant de run : un message n'est jamais modifié pour le porter, et seule l'option d'[étiquetage](#étiquetage-des-requêtes-de-lentrepôt) écrit l'identifiant de run dans la requête SQL. `tracestate` voyage avec le message ; il n'entre pas dans l'identifiant de run, et le proxy ne le lit pas, pas plus qu'en HTTP.

### L'issue d'un appel

| Fin de l'appel | Événement | `outcome` |
| --- | --- | --- |
| Réponse rendue au client, avec un résultat | `COMPLETE` | `success` |
| Réponse rendue au client, avec une erreur JSON-RPC | `COMPLETE` | `jsonrpc_error`, et son code |
| Le serveur s'arrête, ferme sa sortie ou son entrée avant d'avoir répondu | `FAIL` | `transport_error` |
| Le client ferme son entrée, cesse de lire ou annule l'appel avant la réponse | `ABORT` | `client_abort` |

Il n'y a pas de statut HTTP : ni `httpStatus` ni `http_error`. La facette de l'appel porte `transport: "stdio"`. La latence va de la lecture du premier morceau de la requête à l'écriture du dernier morceau de la réponse. La latence ajoutée est le temps que le proxy passe sur chaque morceau entre sa lecture et son écriture : la remise à l'inspection de la copie d'une requête, faite avant son écriture pour que l'inspection voie toujours une requête avant sa réponse ; celle d'une réponse est faite après. Comme en HTTP, c'est une borne basse : elle ne compte ni le saut par un tube de plus, ni le temps où l'inspection occupe l'interpréteur. Sans contexte de trace transmis, la portée d'une reprise est le proxy, qui ne sert qu'un client.

### Rien ne retarde un message

Le relais n'attend jamais l'inspection ni l'émission : il remet sa copie et continue. Les copies en attente sont bornées (8 Mo, ou quatre fois la taille lue si elle est plus grande) ; au-delà, elles ne sont pas gardées. L'inspection compte les messages qu'elles portaient (`uninspected` pour ceux du client), reprend à la fin de ligne suivante, et le journal le signale une fois ; `ProxyStats.lagged_bytes` dit combien d'octets sont passés sans être inspectés.

### Le cycle de vie

- **Signaux.** SIGINT, SIGTERM et SIGHUP reçus par le proxy sont transmis à l'enfant. Le proxy ne s'arrête qu'avec lui : un enfant qui ignore le signal garde le proxy en vie jusqu'à ce qu'il s'arrête, par exemple à la fin de son entrée.
- **Code de sortie.** Celui de l'enfant, ou 128 + le numéro du signal qui l'a arrêté (143 pour SIGTERM).
- **Fermetures.** Chaque fermeture passe de l'autre côté, comme si le client et le serveur se parlaient directement : le client ferme son entrée, l'enfant voit la fin de la sienne ; l'enfant ferme sa sortie, le client voit la fin de celle du proxy ; le client cesse de lire, l'enfant le voit à sa prochaine écriture ; l'enfant ferme son entrée, celle du proxy se ferme aussi. L'enfant arrêté, le proxy rend ce qu'il a écrit sans attendre ce qu'un descendant écrirait encore sur la même sortie.
- **Pas d'orphelin.** Le proxy attend toujours son enfant ; interrompu par une erreur, il le tue avant de s'arrêter. L'enfant reste dans le groupe de processus du proxy : un client qui arrête ce groupe les arrête ensemble.
- **Arrêt.** L'enfant arrêté, le proxy attend les analyses du SQL en cours, remet la dernière fenêtre de couverture, vide sa boîte d'envoi dans son délai (`--outbox-flush-seconds`, 5 s par défaut), puis s'arrête : ce délai borne le temps que sa sortie suit celle de l'enfant.
- **Arrêt gracieux.** Il tient à l'enfant : SIGTERM lui est transmis, et c'est lui qui termine, ou non, les appels qu'il sert ; le proxy l'attend, rend leurs réponses, émet leurs événements, puis vide sa boîte d'envoi. Le reste de l'[arrêt gracieux](#arrêt-gracieux) du mode HTTP n'a pas d'objet : le proxy n'écoute pas, son seul client est celui qui l'a lancé, et aucun répartiteur ne le met en rotation. Il n'a donc ni sonde de santé, ni délai de retrait, ni délai de grâce, et un serveur qui ignore le signal garde le proxy en vie.

### Différences avec le mode HTTP

| | HTTP | stdio |
| --- | --- | --- |
| Lancement | Le proxy écoute sur un port | Le client lance le proxy, qui lance le serveur |
| Contrôle Host et Origin | Avant tout relais | Sans objet : rien n'écoute sur le réseau |
| En-têtes MCP comparés au corps | `headerMismatch` | Sans objet : pas d'en-têtes |
| Contexte de trace | `_meta`, puis l'en-tête `traceparent`, sinon créé et transmis | `_meta`, sinon créé, jamais transmis |
| Rattacher la réponse | L'échange HTTP | L'identifiant JSON-RPC |
| Statut HTTP | `httpStatus`, issue `http_error` | Aucun |
| Portée d'une reprise | Session MCP, puis trace, puis le proxy | Trace, puis le proxy, qui ne sert qu'un client |
| Requête étiquetée, sur option | Le corps lu en entier, sa longueur mise à jour | La ligne rassemblée avant d'être écrite |
| Avertissement de quarantaine | En tête du résultat d'un appel retenu, après une attente bornée de la décision | Aucun : le périmètre est appris, aucun résultat n'est annoté |
| Mode transparent ou passerelle | Selon l'URL que joint le client | Sans objet : le client lance le serveur |
| Sondes de santé | Port d'administration, sur option | Aucune |
| Arrêt | Retrait, grâce, puis vidage de la boîte | Celui du serveur, puis vidage de la boîte |

## Ce qui n'est jamais conservé

Aucun argument, résultat, en-tête ni jeton n'est stocké ou journalisé : des arguments et du résultat, l'événement ne garde que des empreintes à clé et une taille ; d'une requête SQL, sa forme sans littéraux ; d'une erreur, sa catégorie et son code ; d'une clé lue d'après une règle, son empreinte à clé. Seuls le `trace-id` et le `parent-id` d'un contexte de trace bien formé, qui ne disent rien du contenu, sont recopiés dans l'événement ; ils ne sont jamais journalisés. Les en-têtes `Mcp-Param-*`, qui recopient des arguments d'outil, ne sont pas lus. L'en-tête `Mcp-Session-Id` n'est lu que pour reconnaître une reprise, et oublié après l'appel, sauf son empreinte à clé. En stdio, l'identifiant d'une requête n'est gardé que sous une empreinte à clé, le temps d'attendre sa réponse, et la commande lancée n'apparaît ni dans le journal, ni dans un message d'erreur, ni dans la représentation des réglages. Le début du résultat d'un appel retenu par une quarantaine, 16 Kio au plus, n'est gardé que le temps de placer l'avertissement et d'attendre la décision, puis relayé ; l'avertissement ne cite rien du résultat, et l'événement ne dit que la décision. Le journal d'accès du serveur HTTP est coupé. Les journaux des bibliothèques qui citeraient une URL complète, des en-têtes ou une requête SQL (`httpx`, `httpcore`, `sqlglot`) sont coupés pour tout le processus.

## Autorisation

Le proxy ne prend aucune part à l'autorisation du trafic MCP. Le défi `401` d'un serveur MCP protégé, ses métadonnées OAuth (`/.well-known/…`) et les échanges d'un client avec le serveur d'autorisation passent tels quels : le proxy n'y répond jamais lui-même, ne s'authentifie pas à la place du client et ne garde aucun jeton d'un appel au suivant. Son propre jeton, envers l'API d'AnswerLineage ([ci-dessus](#le-jeton-du-proxy-envers-lapi)), est une autre chose : il ne touche à aucun appel relayé.

Un test fait traverser un jeton, un cookie, un paramètre recopié en en-tête et un argument d'outil sur tout le parcours, du proxy au graphe, puis les cherche dans les événements, la base entière, les réponses de l'API, les compteurs et tous les journaux : il n'en reste rien. Le proxy y est placé en mode transparent devant un serveur MCP protégé par OAuth, qui contrôle la signature et l'audience du jeton : le jeton lui arrive octet pour octet, et il l'accepte.

Le mode passerelle, où un jeton lié à l'URL du serveur ne peut pas passer, est refusé pour un tel serveur ([Modes transparent et passerelle](#modes-transparent-et-passerelle)). Pour le savoir, le proxy lit au démarrage les métadonnées publiques du serveur, sans s'authentifier.

## L'événement émis

Un `RunEvent` OpenLineage par appel d'outil, une fois l'échange terminé : `COMPLETE` si le serveur a répondu sans erreur HTTP, `FAIL` sinon, `ABORT` si le client est parti. Le job est `mcp://<serveur>` / `<outil>`, l'identifiant de run celui que dérive le contexte de trace. La facette `answerlineage_toolCall` porte le serveur, l'outil, le transport, la révision du protocole, le statut HTTP, les écarts relevés, la résolution de la lecture, le contexte de trace (`traceId`, `spanId`, `traceOrigin`), les empreintes à clé des arguments et du résultat et la taille du résultat, et, pour un outil dont la requête relayée porte l'identifiant de run, si elle l'a reçu (`taggedWithRunId`), pour un appel retenu par une quarantaine, sa décision et si son résultat a été annoté (`quarantine`) ; son schéma est publié (voir [Facettes OpenLineage](facettes.md)). La facette `answerlineage_qos` porte la latence, la latence ajoutée, l'issue et les reprises ([Qualité de service](#qualité-de-service)). Les tables lues, ou les jeux de données que déclare la règle de lecture de l'outil, sont les jeux de données d'entrée ; leurs colonnes sont dans la facette standard `schema`, les empreintes des clés lues dans la facette `answerlineage_keys`. La forme sans littéraux d'une requête SQL est dans la facette de job standard `sql`.

## Limites connues

- La boîte d'envoi est en mémoire : un arrêt brutal ou un redémarrage perd ce qui n'était pas parti. La perte se voit (silence, événements manquants au graphe), sans être comptée parmi les perdus.
- Un lot interrompu à l'arrêt est compté perdu même si l'API l'a reçu : le trou est surestimé.
- La boîte d'envoi et le client du jeton ne lisent ni mandataire ni autorité de certification de l'environnement : une API ou un émetteur OIDC derrière une autorité de certification privée ne sont pas encore joignables.
- Le proxy ne s'authentifie auprès de l'émetteur que par un secret partagé (`client_secret_basic`) : ni certificat client, ni assertion signée.
- La diffusion est signée sous un secret symétrique : qui le lit sur l'hôte de l'API ou d'un proxy signe comme l'API, et une API compromise signe ce qu'elle pousse. La signature date la réponse sans la lier à la requête : une diffusion captée se rejoue pendant les 5 minutes de la tolérance, et qui coupe le chemin fait garder le dernier état valide une heure. C'est une acceptation à signer du [modèle de menaces](securite/modele-de-menaces.md#acceptations) (`proxy/perimetre-usurpe`).
- Sans secret, le proxy se fie au TLS de l'adresse réglée et apprend le périmètre sans le vérifier : il n'annote alors aucun résultat.
- Une horloge décalée de plus de 5 minutes, sur l'API ou sur un proxy, fait écarter chaque diffusion : le proxy revient en mode observer au bout d'une heure, sans plus rien annoter, et le journal dit `signature_expired` ou `signature_from_future`.
- La zone d'une quarantaine est faite des seuls jeux de son périmètre : une vue ou une table bâtie sur eux n'est pas encore annotée, la diffusion ne portant pas encore les jeux dérivés connus du graphe.
- Un hôte qui ne donne au modèle que `structuredContent` ne lui montre pas l'avertissement : l'événement dit que le résultat a été annoté, pas ce que l'hôte en a montré. Un client qui relit `content[0]` comme le JSON de `structuredContent` lit l'avertissement à sa place.
- Le mode stdio n'annote pas : un serveur lancé en stdio voit ses lectures en quarantaine relayées sans avertissement.
- Un outil sans règle de lecture ni SQL déclaré n'est jamais retenu : ses lectures d'un jeu en quarantaine passent sans avertissement.
- Les premiers appels d'un outil SQL retenu après un démarrage, ou après une analyse abandonnée à son budget, peuvent être annotés comme incertains : l'analyseur de quarantaine démarre ou redémarre. Le préchauffage, dès qu'une zone vise un outil SQL, le limite.
- Un nom de table ne s'apparie au nom d'un jeu de la zone que s'il lui est égal : un émetteur qui écrit le même jeu avec une autre casse, ou autrement qualifié, n'est pas reconnu, comme dans la propagation.
- Une réponse compressée, liée par une empreinte ou une signature, une réponse JSON-RPC dont la place du bloc n'est pas dans ses 16 premiers Kio, ou un flux SSE dont la réponse s'étend sur plusieurs lignes `data:`, ne sont pas annotés ; l'événement dit pourquoi.
- Une liste de périmètres de plus de 2 Mio est écartée : cent quarantaines de vingt jeux aux noms de plusieurs centaines de caractères. Le proxy garde alors son dernier état, puis revient en mode observer au bout d'une heure, et le journal dit `too_large`.
- L'état de la synchronisation ne se lit qu'au journal et dans le processus (`quarantine_status()`) : ni relevé, ni route.
- Le seuil de 5 ms au p99 ne se vérifie qu'au banc de chaque nuit ; l'intégration continue, sur une machine partagée, n'en garde qu'un seuil tolérant.
- Deux proxys réglés par erreur sur le même identifiant d'instance se confondent : l'API fusionne leurs relevés d'une même fenêtre au plus grand.
- Un client qui réutilise le même span pour deux appels d'outil leur donne le même identifiant de run : le graphe fusionne alors les deux appels en un seul run, qui porte les lectures des deux mais l'outil du premier événement projeté, donc selon l'ordre d'arrivée (`tests/test_trace_context_graph.py` le constate).
- Un émetteur authentifié n'est pas encore lié à l'espace de noms de ses événements : un identifiant dérivé n'est pas une preuve d'origine, et un émetteur qui connaît le contexte d'un appel peut émettre un événement pour son run.
- Une reprise HTTP garde son run quand le contexte vient de `_meta`. Quand il vient de l'en-tête d'un client HTTP qui ouvre un span par tentative, ou quand le proxy le crée, chaque tentative est un run distinct.
- Le proxy ne rattache pas un appel au run de l'agent : la facette standard `parent` exige le job de l'agent, que le proxy ne connaît pas. Un émetteur qui connaît l'agent peut le faire ; `traceId` relie déjà les appels d'une même trace.
- Le résultat de l'appel n'est pas lu, sauf pour y trouver une erreur JSON-RPC : une erreur d'outil rendue comme un résultat (`isError`) compte comme un succès, et une erreur JSON-RPC rendue en `200` reste un événement `COMPLETE`, dont l'issue dit l'erreur.
- L'issue d'une réponse compressée tient à son seul statut HTTP : son corps n'est pas lu.
- Le proxy ne pose lui-même aucun délai de lecture : seul le délai de connexion au serveur MCP (10 s) fait une issue `timeout`, sauf avec un client HTTP qui en a un autre.
- Sans session MCP ni contexte de trace transmis par le client, la portée d'une reprise est le proxy entier : deux clients qui font le même appel, aux mêmes arguments, dans la minute qui suit un échec, sont comptés comme une reprise.
- Le mode stdio est réservé aux systèmes POSIX (Linux, macOS).
- Le port d'administration n'écoute que sur la boucle locale : une sonde `httpGet` de Kubernetes, qui joint l'adresse du pod, ne l'atteint pas, et la sonde s'exécute dans le conteneur. Un processus du même poste peut en occuper les 8 connexions : le réplica paraît alors non prêt.
- La santé ne dit pas si le serveur MCP ou l'API d'AnswerLineage répondent : un réplica reste prêt quand ils tombent, pour que la capture reste ouverte.
- Un appel plus long que le délai de grâce est coupé à l'arrêt. Son événement dit `transport_error` : la facette de qualité de service n'a pas d'issue propre à l'arrêt du proxy.
- Le délai de retrait se règle à la main, d'après le répartiteur : le proxy ne sait pas quand celui-ci l'a retiré.
- En mode passerelle, seules les adresses bien connues de la RFC 9728 sont lues : un serveur qui ne désigne ses métadonnées que dans son défi `401` n'est pas reconnu, et `--audience-bound-tokens` le déclare. Des métadonnées illisibles au démarrage, serveur injoignable par exemple, laissent démarrer la passerelle : si les jetons du serveur sont liés à son URL, il refusera ensuite les appels.
- Le mode transparent ne se règle pas dans le proxy seul : le nom du serveur doit mener au proxy, dans le DNS ou dans l'équilibreur qui le précède.
- Tué par SIGKILL, le proxy ne peut rien transmettre à son enfant : celui-ci voit la fin de son entrée, sur laquelle un serveur MCP s'arrête, mais un serveur qui l'ignore lui survit. Arrêter le groupe de processus les arrête ensemble.
- Un Ctrl-C au terminal atteint tout le groupe de processus : en stdio, l'enfant reçoit alors SIGINT deux fois, du terminal et du proxy.
- En stdio, une réponse plus grande que la taille lue, dont l'identifiant suit le résultat au-delà des 16 premiers Ko, ne se rattache pas à son appel : l'appel n'a pas d'événement et compte comme perdu. De même, un appel d'outil plus grand que la taille lue dont l'identifiant suit les arguments n'est pas décrit. Relever `--max-inspected-bytes` en réduit le nombre.
- En stdio, l'inspection partage l'interpréteur avec les fils de relais : lire un message de la taille maximale (1 Mo par défaut) l'occupe quelques millisecondes, que le relais peut attendre.
- Une règle de lecture se fie à l'outil : si l'outil lit autre chose que ce qu'elle déclare, le lignage le croit. Elle ne dit pas non plus quelles lignes d'un jeu de données correspondent à une clé : l'empreinte rapproche des appels, pas des lignes.
- Les règles de lecture ne se rechargent pas à chaud : un changement demande de relancer le proxy.
- Dans un corps plus grand que la taille lue, une clé `method` ou `name` répétée au-delà des 16 premiers Ko ne peut pas être vue : l'événement décrit la première.
- Le budget se mesure en temps écoulé : sur une machine très chargée, une requête qui tient d'ordinaire dans le budget peut le dépasser et rester « non résolue » tant qu'elle est en cache.
- Les signaux de qualité de service restent dans l'événement reçu : le graphe ne les stocke pas encore.
- La forme d'une requête garde les noms d'un seul mot choisis par son auteur : un alias qui serait une donnée métier (`AS "Dupont"`) est conservé tel quel. Un nom qui n'a pas la forme d'un identifiant fait renoncer à la forme.
- L'empreinte du résultat porte sur les octets que le serveur a rendus : le même résultat, compressé ou non, ou découpé autrement en événements SSE, a deux empreintes différentes.
- Les arguments d'un appel dont le corps dépasse la taille lue ne sont pas lus : pas d'empreinte des arguments.
- Le label, le tag et le commentaire qui portent l'identifiant de run sont posés, mais rien ne les lit encore : le rapprochement des lectures avec les journaux d'audit de l'entrepôt reste à faire.
- Le commentaire est écrit en tête de la requête : un serveur qui n'accepte une requête que d'après ses premiers mots peut refuser une requête étiquetée.
- Une requête étiquetée ne profite plus du cache de résultats de l'entrepôt, puisque son texte change à chaque run.
- En HTTP, pour un corps plus grand que la taille lue, le proxy dérive le run de l'en-tête `traceparent` sans lire `_meta` : un serveur qui suit la fonction de référence, et lit `_meta`, peut poser un autre run que celui de l'événement.
- En stdio, sans `_meta`, le serveur ne connaît pas le run : seul le commentaire du proxy, sur option, le porte.
- En stdio, une requête que l'inspection en retard n'a pas pu lire compte parmi les non étiquetées, même si le relais l'a étiquetée : le compte surestime ce qui manque, il ne le cache pas.
