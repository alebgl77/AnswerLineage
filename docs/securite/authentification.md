# Authentification et rôles

Chaque route de l'API exige une identité OIDC et un rôle : personne ne lit le lignage, ne déclare un incident ni n'envoie d'événement sans y être autorisé. L'API ne gère aucun compte : elle croit un émetteur OIDC, celui de l'entreprise (Keycloak, Entra ID, Okta, Auth0…), et vérifie les jetons qu'il signe. Ce document dit comment le régler, ce que l'API vérifie, les rôles et la matrice des droits, puis comment le proxy, la ligne de commande et l'explorateur présentent leur jeton, et ce que fait la démonstration.

Le code est dans `api/src/answerlineage_api/auth/` : `matrix.py` tient la matrice, `oidc.py` vérifie les jetons, `access.py` applique l'une et l'autre avant toute route.

## Régler l'émetteur

| Variable | Rôle | Défaut |
| --- | --- | --- |
| `ANSWERLINEAGE_OIDC_ISSUER` | L'émetteur, tel qu'il s'écrit dans la revendication `iss` de ses jetons : `https://…` (en `http`, sur la boucle locale seulement) | aucun : sans lui, l'API ne démarre pas |
| `ANSWERLINEAGE_OIDC_AUDIENCE` | L'audience de l'API : la valeur que la revendication `aud` d'un jeton doit nommer | aucune : sans elle, l'API ne démarre pas |
| `ANSWERLINEAGE_OIDC_ROLES_CLAIM` | La revendication qui porte les rôles, ou un chemin de membres séparés par des points (`realm_access.roles`, `groups`) | `roles` |
| `ANSWERLINEAGE_OIDC_ROLE_MAP` | La table de correspondance, en JSON : une valeur de l'émetteur, puis un rôle ou une liste de rôles (`{"lignage-lecteurs": "reader", "lignage-admins": ["administrator", "auditor"]}`). Sans table, une valeur qui est le nom d'un rôle donne ce rôle ; avec une table, seules les valeurs qu'elle nomme comptent | aucune |
| `ANSWERLINEAGE_OIDC_EMITTER_CLIENTS` | Les identifiants des clients qui ont le rôle d'émetteur, séparés par des virgules : ceux des proxys et des autres émetteurs | aucun |
| `ANSWERLINEAGE_OIDC_EXPLORER_CLIENT_ID` | Le client public de l'explorateur chez l'émetteur ; sans lui, l'explorateur ne se connecte pas | aucun |
| `ANSWERLINEAGE_OIDC_EXPLORER_SCOPE` | La portée que l'explorateur demande, par exemple `openid api://lignage/acces` pour Entra ID | `openid` |

```bash
export ANSWERLINEAGE_OIDC_ISSUER=https://idp.interne/realms/lignage
export ANSWERLINEAGE_OIDC_AUDIENCE=answerlineage-api
export ANSWERLINEAGE_OIDC_ROLES_CLAIM=realm_access.roles
export ANSWERLINEAGE_OIDC_EMITTER_CLIENTS=proxy-entrepot,proxy-crm
export ANSWERLINEAGE_OIDC_EXPLORER_CLIENT_ID=answerlineage-explorateur
ANSWERLINEAGE_UI_DIR=ui/dist uv run uvicorn --factory answerlineage_api.app:create_app
```

Un réglage illisible arrête l'API au démarrage, avec un message qui nomme le réglage, jamais sa valeur. L'API refuse aussi deux réglages dangereux : un client de l'explorateur qui serait un émetteur (toute personne connectée en aurait le rôle), et une audience égale au client de l'explorateur (le jeton d'identité de l'explorateur passerait pour un jeton d'accès).

Chez l'émetteur :

- **l'API** est une ressource dont l'identifiant est l'audience ; ses jetons d'accès doivent être des JWT signés (Keycloak, Entra ID, Okta et Auth0 le font quand une audience est demandée), et porter les rôles dans la revendication réglée ;
- **chaque proxy** est un client confidentiel, avec l'octroi `client_credentials` ;
- **l'explorateur** est un client public, avec le flux d'autorisation et PKCE (S256), sans secret. Son adresse de retour, et de retour après déconnexion, est l'adresse de l'explorateur, `https://<hôte de l'API>/`. L'émetteur doit permettre à cette origine de lire sa découverte et son point de jeton (CORS : « web origins » de Keycloak, plateforme « application monopage » d'Entra ID).

## Ce que l'API vérifie

Avant toute route, et avant la validation de ses paramètres ou la lecture de son corps :

- **l'en-tête** : un seul `Authorization`, de la forme `Bearer <jeton>` (le schéma sans égard à la casse), le jeton de 16 Kio au plus et du jeu de caractères d'un jeton porteur ;
- **le jeton** est un JWS compact. Son algorithme est asymétrique, RS256, ES256 ou EdDSA ; jamais `none` ni HMAC, ce qui ferme la confusion d'algorithme (un HMAC signé avec la clé publique). Un en-tête `crit` est refusé, et la clé n'est jamais prise du jeton lui-même (`jwk`, `jku`, `x5u` sont ignorés) ;
- **sa clé** : le `kid` de l'en-tête désigne une clé du JWKS de l'émetteur, publique, de signature, du type de l'algorithme (RSA de 2048 bits au moins, EC P-256, Ed25519 ou Ed448), et qui n'en déclare pas un autre ;
- **sa signature**, puis ses revendications : `iss` est l'émetteur réglé, `aud` nomme l'audience, `exp` est présent et à venir, `nbf` et `iat`, s'ils sont présents, ne sont pas à venir. Une tolérance d'horloge de 60 secondes s'applique aux trois heures : assez pour l'écart de deux horloges synchronisées par NTP, trop peu pour prolonger un jeton volé ;
- **le rôle** : l'un des rôles du jeton ouvre la route ([matrice](#la-matrice-des-droits)).

Le sujet du jeton (`sub`) est l'identité de la personne. L'API ne le garde que pour une action tracée, la [révélation d'un destinataire](pseudonymes.md#révéler), qui le journalise comme auteur avec l'émetteur ; il n'est lu que s'il a la forme qu'OpenID Connect lui donne, 255 caractères ASCII imprimables au plus, sans blanc ni rien qui forme une balise. Sans sujet admis, la révélation répond `403 author_unidentified`.

| Refus | Statut | Défi `WWW-Authenticate` |
| --- | --- | --- |
| `missing_token` : aucun en-tête `Authorization` | `401` | `Bearer` |
| `invalid_token` : en-tête répété ou mal formé, autre schéma, jeton démesuré, mal formé, d'un algorithme refusé, d'une clé inconnue, mal signé, d'un autre émetteur ou d'une autre audience, sans expiration, pas encore valide | `401` | `Bearer error="invalid_token"` |
| `expired_token` : un jeton juste, mais expiré | `401` | `Bearer error="invalid_token"` |
| `insufficient_role` : aucun rôle du jeton n'ouvre la route | `403` | `Bearer error="insufficient_scope"` |
| `authentication_unavailable` : l'émetteur ne donne pas la clé qui vérifierait le jeton, et aucune n'est connue | `503` | aucun |

Aucune réponse ne recopie l'en-tête ni le jeton, et rien n'en est journalisé ni gardé. Le refus ne dit pas pourquoi un jeton est invalide au-delà de son code : qui forge un jeton n'apprend pas quelle vérification l'arrête.

### Les clés de l'émetteur

L'API lit la découverte de l'émetteur (`<émetteur>/.well-known/openid-configuration`), qui doit le nommer exactement, puis le JWKS qu'elle désigne, en `https` (en `http` sur la boucle locale seulement). Chaque lecture se fait sans redirection suivie, sans mandataire ni identifiant de l'environnement, certificat vérifié, sous 5 secondes et 256 Kio au plus ; un JWKS de plus de 64 clés est refusé en entier, et une clé dont le `kid` revient deux fois n'est pas crue.

Les clés se gardent en mémoire. Un jeton au `kid` inconnu relit la découverte et le JWKS, au plus une fois toutes les 10 secondes, quel que soit le nombre de jetons : une rotation de clés chez l'émetteur se suit d'elle-même, et un appelant ne fait pas marteler l'émetteur. Au bout d'une heure, les clés se relisent à la vérification suivante ; tant que l'émetteur ne répond pas, les clés gardées servent encore. Sans aucune clé connue, une route protégée répond `503`.

## Les rôles

| Rôle | Identifiant | Ce qu'il ouvre |
| --- | --- | --- |
| Lecteur | `reader` | Lire le lignage, la couverture, les incidents, les incidents proposés et les périmètres en quarantaine |
| Responsable de données | `data_steward` | Le lecteur, et déclarer un incident, mettre un incident en quarantaine et la lever, confirmer ou écarter un incident proposé |
| Responsable d'agent | `agent_owner` | Ce que lit le lecteur, et les doublons par émetteur et les outils sans règle de lecture |
| Gestionnaire d'incident | `incident_manager` | Comme le responsable de données, et révéler un destinataire parmi des identités candidates, action journalisée ([pseudonymes](pseudonymes.md)) |
| Auditeur | `auditor` | Tout lire, l'exploitation comprise (état détaillé, rebut, doublons) et le journal des révélations, sans rien écrire |
| Administrateur | `administrator` | Tout ce qu'ouvrent les rôles de personnes, sauf révéler un destinataire, et rejouer le rebut |
| Émetteur | `emitter` | Rôle technique : envoyer des événements et des relevés de couverture, lire les périmètres en quarantaine |

Les six premiers sont ceux des personnes : ils se lisent dans la revendication réglée, à travers la table de correspondance. L'administrateur ne révèle pas de destinataire : il exploite la plateforme, et peut en approcher la base et le coffre à clés ; un seul compte, ou un seul jeton volé, réunirait sinon les pseudonymes, la clé et le droit de révéler. Il lit le journal des révélations, avec l'auditeur. Le rôle d'émetteur ne se lit que de l'identifiant du client (`azp`, à défaut `client_id`) : une revendication de rôles ne le donne jamais, et une personne qui se connecte à l'explorateur ne l'a pas. L'administrateur n'émet pas d'événements : tout événement vient d'un client identifié comme émetteur.

Le lien entre un émetteur et l'espace de noms de ses événements n'est pas encore fait : un émetteur authentifié peut envoyer un événement au nom d'un autre ([limites](#limites-connues)).

## La matrice des droits

La matrice est déclarée une seule fois, dans le code (`matrix.py`). La dépendance d'accès l'applique, l'application ne se construit pas si une route en manque, la spécification OpenAPI la publie (le schéma de sécurité `bearer`, et pour chaque opération les rôles qui l'ouvrent, un seul suffisant), et le tableau ci-dessous est celui que rend `answerlineage_api.auth.matrix.render_markdown()` : un test vérifie qu'il est recopié tel quel.

| Route | lecteur | responsable de données | responsable d'agent | gestionnaire d'incident | auditeur | administrateur | émetteur | Pourquoi |
| --- | :-: | :-: | :-: | :-: | :-: | :-: | :-: | --- |
| `POST /api/v1/lineage` |  |  |  |  |  |  | oui | Envoyer des événements : les émetteurs seuls. |
| `POST /api/v1/lineage/batch` |  |  |  |  |  |  | oui | Envoyer des événements : les émetteurs seuls. |
| `POST /api/v1/coverage` |  |  |  |  |  |  | oui | Publier un relevé de couverture : les émetteurs seuls. |
| `GET /api/v1/ingestion/duplicates` |  |  | oui |  | oui | oui |  | Les doublons par émetteur : qui répond des agents et de l'exploitation. |
| `GET /api/v1/ingestion/undescribed-tools` |  |  | oui |  | oui | oui |  | Les outils sans règle de lecture : qui répond des agents et de l'exploitation. |
| `GET /api/v1/ingestion/dead-letters` |  |  |  |  | oui | oui |  | Le rebut : l'exploitation. |
| `POST /api/v1/ingestion/dead-letters/replay` |  |  |  |  |  | oui |  | Rejouer le rebut : l'administrateur seul. |
| `GET /api/v1/coverage` | oui | oui | oui | oui | oui | oui |  | Lire le lignage : toute personne qui a un rôle. |
| `GET /api/v1/answers` | oui | oui | oui | oui | oui | oui |  | Lire le lignage : toute personne qui a un rôle. |
| `GET /api/v1/answers/{answer_id}/sources` | oui | oui | oui | oui | oui | oui |  | Lire le lignage : toute personne qui a un rôle. |
| `GET /api/v1/graph` | oui | oui | oui | oui | oui | oui |  | Lire le lignage : toute personne qui a un rôle. |
| `GET /api/v1/datasets` | oui | oui | oui | oui | oui | oui |  | Lire le lignage : toute personne qui a un rôle. |
| `GET /api/v1/impact` | oui | oui | oui | oui | oui | oui |  | Lire le lignage : toute personne qui a un rôle. |
| `GET /api/v1/exposure` | oui | oui | oui | oui | oui | oui |  | Lire le lignage : toute personne qui a un rôle. |
| `POST /api/v1/incidents` |  | oui |  | oui |  | oui |  | Déclarer un incident : qui répond des données ou des incidents. |
| `GET /api/v1/incidents` | oui | oui | oui | oui | oui | oui |  | Lire les incidents et leurs réponses touchées : toute personne qui a un rôle. |
| `GET /api/v1/incidents/{incident_id}` | oui | oui | oui | oui | oui | oui |  | Lire les incidents et leurs réponses touchées : toute personne qui a un rôle. |
| `POST /api/v1/incidents/{incident_id}/quarantine` |  | oui |  | oui |  | oui |  | Mettre en quarantaine : qui répond des données ou des incidents. |
| `POST /api/v1/incidents/{incident_id}/quarantine/lift` |  | oui |  | oui |  | oui |  | Lever une quarantaine : qui répond des données ou des incidents. |
| `GET /api/v1/incident-proposals` | oui | oui | oui | oui | oui | oui |  | Lire les incidents proposés : toute personne qui a un rôle. |
| `GET /api/v1/incident-proposals/{proposal_id}` | oui | oui | oui | oui | oui | oui |  | Lire les incidents proposés : toute personne qui a un rôle. |
| `POST /api/v1/incident-proposals/{proposal_id}/confirm` |  | oui |  | oui |  | oui |  | Confirmer un incident proposé : qui répond des données ou des incidents. |
| `POST /api/v1/incident-proposals/{proposal_id}/dismiss` |  | oui |  | oui |  | oui |  | Écarter un incident proposé : qui répond des données ou des incidents. |
| `POST /api/v1/recipients/reveal` |  |  |  | oui |  |  |  | Révéler un destinataire, action tracée : qui instruit les incidents, pas l'administrateur qui exploite la plateforme. |
| `GET /api/v1/recipients/revelations` |  |  |  |  | oui | oui |  | Le journal des révélations : l'audit et l'exploitation. |
| `GET /api/v1/quarantine` | oui | oui | oui | oui | oui | oui | oui | Les périmètres en quarantaine : les proxys, et toute personne qui a un rôle. |
| `GET /api/v1/health` |  |  |  |  | oui | oui |  | L'état détaillé, ses files et ses compteurs : l'exploitation. |
| `GET /api/v1/live` | ouvert | ouvert | ouvert | ouvert | ouvert | ouvert | ouvert | La sonde de vie : ouverte, elle ne lit pas la base et ne dit rien. |
| `GET /api/v1/authentication` | ouvert | ouvert | ouvert | ouvert | ouvert | ouvert | ouvert | Ce que l'explorateur doit savoir pour se connecter : ouvert, rien du lignage. |

Des tests générés depuis la spécification publiée (`api/tests/test_authorization.py`, Schemathesis) vérifient, opération par opération, qu'un appel sans jeton reçoit `401` et qu'un jeton sans aucun des rôles de l'opération, ou sans rôle du tout, reçoit `403` ; une opération absente de la matrice fait échouer les tests.

### Ce qui reste ouvert sans jeton

- **`GET /api/v1/live`**, la sonde de vie : `{"status": "alive"}`, sans lire la base. Un orchestrateur, qui n'a pas de jeton, sait que le processus répond, et rien d'autre ; la démonstration s'en sert pour son contrôle de santé.
- **`GET /api/v1/authentication`** : le mode, l'émetteur, le client public de l'explorateur et sa portée. Rien de cela n'est secret ; l'explorateur le lit avant de se connecter.
- **La spécification OpenAPI** (`/api/v1/openapi.json`) : le contrat, publié dans le dépôt.
- **Les fichiers de l'explorateur** : une page sans donnée, qui se connecte avant de lire l'API.

L'état détaillé de l'installation (`GET /api/v1/health`) ne l'est plus : il dit les files, le rebut, les émetteurs muets, les partitions et les compteurs de la réémission et des webhooks, ce qu'un tiers n'a pas à savoir. Il revient à l'auditeur et à l'administrateur ; `answerlineage doctor` le lit avec leur jeton.

## Les jetons des clients de l'API

| Client | Comment il obtient son jeton | Où |
| --- | --- | --- |
| Proxy MCP | Octroi `client_credentials`, auprès du point de jeton découvert ; gardé en mémoire, renouvelé avant d'expirer ; secret lu d'un fichier ou d'une variable, jamais transmis au serveur MCP en stdio | [Le jeton du proxy](../proxy.md#le-jeton-du-proxy-envers-lapi) |
| Ligne de commande | Un jeton donné par `ANSWERLINEAGE_TOKEN` ou un fichier (`ANSWERLINEAGE_TOKEN_FILE`, `--token-file`) ; jamais sur la ligne de commande | [Le jeton](../cli.md#le-jeton) |
| Chargeur du corpus | Le jeton d'un émetteur, par les mêmes variables que la ligne de commande | [Entreprise synthétique](../corpus.md) |
| Explorateur | Flux d'autorisation avec PKCE, dans le navigateur | [ci-dessous](#lexplorateur) |

Aucun ne fait partir un jeton en clair : l'adresse de l'API est en `https`, ou en `http` vers la boucle locale.

Le jeton du proxy n'est pas visé par l'invariant 4 (« le proxy ne termine pas OAuth et ne conserve aucun jeton ») : celui-ci porte sur les jetons du trafic MCP des agents, que le proxy relaie sans les lire ni les garder. Le jeton propre du proxy est son identité d'émetteur auprès de l'API, et ne sert à aucun appel relayé ([architecture](../architecture.md#où-chaque-invariant-est-tenu)).

## L'explorateur

L'explorateur se connecte par le flux d'autorisation OIDC avec PKCE, écrit avec WebCrypto, sans dépendance :

1. il lit `GET /api/v1/authentication`, puis la découverte de l'émetteur ;
2. il envoie la personne au point d'autorisation de l'émetteur, avec un défi PKCE (S256), un état et un nom occasionnel tirés au hasard ;
3. au retour, il vérifie l'état, échange le code contre un jeton au point de jeton, retire le code de l'adresse et rouvre la vue d'avant la connexion ;
4. il présente le jeton à chaque lecture de l'API.

- **Le jeton reste en mémoire** : ni `localStorage`, ni cookie. Recharger la page en demande un nouveau, que l'émetteur donne sans rien redemander tant que sa session tient. Seuls le vérificateur PKCE, l'état, le nom occasionnel et l'adresse à rouvrir passent l'aller-retour dans `sessionStorage`, et en sont effacés au retour.
- **La politique de sécurité du contenu est gardée** : aucun script ni style en ligne. L'API n'y ajoute que l'origine de l'émetteur, dans `connect-src`, quand un client de l'explorateur est réglé. Le point de jeton doit donc être sur l'origine de l'émetteur ; l'explorateur refuse une découverte qui l'en sortirait.
- **La déconnexion** oublie le jeton, puis passe par le point de déconnexion de l'émetteur s'il en publie un ; l'explorateur attend alors qu'on lui demande de se reconnecter.
- **L'expiration** : la session prend fin 30 secondes avant le jeton, et à tout refus `401` de l'API ; la page le dit et propose de se reconnecter. Un rôle qui n'ouvre pas la vue se dit par un message fixe.

## La démonstration

La démonstration n'a pas d'émetteur : son authentification est désactivée par un réglage nommé, `ANSWERLINEAGE_AUTH=disabled`, que `compose.yaml` pose sur le service `api`. Chaque appel a alors tous les rôles, mais aucune identité : aucun appel ne peut révéler un destinataire.

- **Seulement sur la boucle locale.** Désactivée, l'authentification interdit tout nom et toute origine au-delà de la boucle locale : avec `ANSWERLINEAGE_ALLOWED_HOSTS` ou `ANSWERLINEAGE_ALLOWED_ORIGINS` qui en nomment un autre, ou `*`, l'API refuse de démarrer. Le contrôle des en-têtes `Host` et `Origin` refuse ensuite tout autre nom ([hôtes et origines attendus](../ingestion.md#hôtes-et-origines-attendus)), et `compose.yaml` ne publie le port que sur `127.0.0.1`, ce que `make policy` vérifie.
- **Un bandeau** le dit en haut de l'explorateur : « Démonstration : l'authentification est désactivée. Cette API ne doit être jointe que par la boucle locale de ce poste. »
- **Rien d'autre ne l'ouvre** : sans `ANSWERLINEAGE_OIDC_ISSUER` et sans ce réglage, l'API ne démarre pas. Une valeur autre que `oidc` ou `disabled` l'arrête aussi.

L'API ne voit pas l'adresse sur laquelle le serveur HTTP écoute : le serveur ouvre son port après avoir construit l'application, et Docker publie le port d'un conteneur hors de sa vue. Le refus au démarrage porte donc sur ce que l'API règle elle-même, les noms et les origines qu'elle accepte ; l'écoute sur la boucle locale est tenue par la publication du port, vérifiée par `make policy` et par `scripts/check_demo.sh`. Lancée hors de la démonstration, en `--host 0.0.0.0` et sans émetteur, l'API répondrait encore à qui forge un en-tête `Host` de la boucle locale : ce réglage n'est fait que pour un poste.

## Limites connues

- Les jetons d'accès opaques (Google, Auth0 sans audience) ne se vérifient pas : l'API attend des JWT signés.
- Le `kid` est exigé ; un émetteur qui n'en met pas n'est pas pris en charge.
- Un jeton reste valable jusqu'à son expiration : l'API ne consulte ni l'introspection ni la révocation de l'émetteur. Des jetons courts, de quelques minutes, bornent ce qu'un jeton volé permet.
- Un émetteur authentifié n'est pas encore lié à l'espace de noms de ses événements : il peut envoyer un événement, un rejeu ou un relevé au nom d'un autre émetteur.
- L'API ne garde pas encore qui a fait quoi, hors de la révélation d'un destinataire : la déclaration d'un incident ou sa mise en quarantaine ne retient pas encore son auteur.
- Le point de jeton de l'émetteur doit être sur son origine, pour que la politique de sécurité du contenu de l'explorateur le laisse joindre.
- L'API et le proxy ne lisent ni mandataire ni autorité de certification de l'environnement : un émetteur derrière une autorité privée n'est pas encore joignable.
