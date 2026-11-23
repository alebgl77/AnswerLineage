# Modèle de menaces

Ce document présente le modèle de menaces d'AnswerLineage : les frontières de confiance, les actifs, et pour chaque composant du dépôt les menaces connues, avec leur parade ou leur acceptation. Le modèle lui-même est un fichier de données, [`modele-de-menaces.toml`](modele-de-menaces.toml), à côté de ce document ; il est revu pour la version `0.0.0`, le 2026-11-16.

Il complète la [politique de sécurité](../../SECURITY.md), qui dit comment signaler une faille, et l'[architecture](../architecture.md#où-chaque-invariant-est-tenu), qui dit où chaque invariant du produit est tenu.

## Ce que dit le modèle, et comment il est tenu

Chaque menace a un identifiant stable, `composant/nom-court`, une catégorie, la frontière qu'elle franchit, les actifs qu'elle vise, et :

- une ou plusieurs **parades**, chacune avec le fichier du code qui la porte et le test qui la prouve, écrits `chemin` ou `chemin::fonction` ;
- ou une **acceptation** : son motif, sa conséquence, et la signature du mainteneur, qui l'accepte et quand (`accepted_by`, `accepted_on`). Sans signature, l'acceptation est « à signer ».

Une menace peut avoir les deux : des parades qui couvrent une partie du risque, et l'acceptation de ce qui reste.

`scripts/check_threat_model.py` contrôle le modèle, en bibliothèque standard seule :

| Quand | Ce qui est vérifié | Où |
| --- | --- | --- |
| À chaque merge request | Le modèle est bien formé : aucune clé inconnue, aucun champ manquant, des identifiants de la bonne forme et uniques, des catégories, frontières, actifs et composants connus. Chaque menace a une parade ou une acceptation. Le code et le test de chaque parade existent, et la fonction nommée y est définie. Une acceptation signée l'est par un propriétaire du modèle dans `.github/CODEOWNERS`. Chaque composant du dépôt figure au modèle avec au moins une menace : un composant absent fait échouer le contrôle, qui le nomme | `make policy`, job `static` |
| À chaque merge request | La publication est éprouvée avec l'étiquette fictive de la version déclarée : le modèle doit avoir été revu pour elle, et sa revue refuser une autre version. Les acceptations à signer sont listées, sans faire échouer la répétition | `make packages`, job `static` |
| À la publication d'une version | En plus de tout ce qui précède : le modèle a été revu pour la version de l'étiquette, et aucune acceptation ne reste à signer. Sinon le job `prepare` échoue et rien n'est publié | `python3 -m scripts.check_release tag`, workflow `release` |

Les composants du dépôt sont les membres de l'espace de travail uv (`pyproject.toml`), les paquets npm d'un dossier de premier niveau (`ui/`) et le pipeline (`.github/workflows/`).

Le fichier du modèle est un chemin protégé (`scripts/check_agent_config.py`, `.github/CODEOWNERS`) : le modifier demande la revue du mainteneur et le label `agent-config-approved`. Seul un propriétaire de ce fichier signe une acceptation ; un agent de code ou un contributeur n'en signe aucune. Le modèle de merge request du dépôt rappelle de le mettre à jour.

## Méthode

Les menaces sont classées selon STRIDE :

| Catégorie | Dans le modèle | Ce qui est craint |
| --- | --- | --- |
| Usurpation | `spoofing` | Se faire passer pour un autre : émetteur, éditeur d'un paquet, agent appelé |
| Altération | `tampering` | Modifier une donnée, le lignage, une mesure ou un artefact |
| Répudiation | `repudiation` | Agir sans que le lignage ou la revue en garde trace |
| Divulgation | `information-disclosure` | Faire sortir un contenu, un jeton, une clé ou le lignage |
| Déni de service | `denial-of-service` | Épuiser ou retarder un service, ou l'agent |
| Élévation de privilège | `elevation-of-privilege` | Obtenir un droit que l'on n'a pas : appeler un outil, écrire dans le dépôt, publier |

## Frontières de confiance

| Frontière | Ce qui la traverse |
| --- | --- |
| Agent, proxy et serveur MCP | Le client MCP d'un agent appelle des outils à travers le proxy, en HTTP ou en stdio, et le proxy relaie au serveur MCP. Le corps, les en-têtes et la réponse viennent de part et d'autre ; une page web ouverte sur le poste peut aussi viser un proxy qui écoute sur la boucle locale. |
| Émetteur et ingestion | Le proxy, l'entreprise synthétique ou tout émetteur OpenLineage envoie événements et relevés de couverture à l'API d'ingestion. Les émetteurs ne sont pas authentifiés : tout ce qu'ils déclarent est une entrée hostile. |
| Lecteur, API de lecture et explorateur | Un navigateur, la ligne de commande ou un script lit le lignage, déclare un incident ou ouvre une adresse partagée de l'explorateur. L'appelant n'est pas authentifié ; l'adresse, les paramètres et les corps sont hostiles, et les noms affichés viennent des émetteurs. |
| Workers et catalogue externe | Les workers réémettent les événements projetés vers un consommateur OpenLineage externe (Marquez, DataHub, OpenMetadata, Dataplex), à l'adresse que règle l'exploitant. Le catalogue, le réseau qui y mène et ses réponses sont hors du contrôle d'AnswerLineage. |
| Workers et récepteur de webhooks | Les workers annoncent les changements d'état des incidents, par des messages signés, au récepteur de webhooks que règle l'exploitant. Le récepteur, le réseau qui y mène et ses réponses sont hors du contrôle d'AnswerLineage ; quiconque joint le récepteur peut lui envoyer une requête, ou rejouer une requête captée. |
| API et proxys | Chaque proxy interroge l'API d'AnswerLineage, à l'adresse que règle l'exploitant, pour apprendre les périmètres en quarantaine. Le proxy n'authentifie pas encore l'API : le réseau qui y mène et ce qu'elle répond sont hostiles. |
| Pipeline et publication | Une merge request, écrite par un contributeur ou un agent de code, fait tourner le pipeline ; une étiquette de version publie l'image et les paquets. Le contenu de la merge request, les actions, les images et les dépendances tierces sont hors de confiance. |
| Données au repos | PostgreSQL tient le graphe, les événements bruts, la file, les relevés et les incidents ; le poste ou le serveur du proxy tient la clé des empreintes et les règles de lecture. Qui y accède lit ce qui y est écrit. |

## Actifs

| Actif | Ce qu'il vaut |
| --- | --- |
| Contenus des agents | Prompts, arguments et résultats d'outils, réponses : ils traversent le proxy et ne sont jamais conservés. |
| Jetons d'accès | Les jetons OAuth et en-têtes d'autorisation que l'agent présente au serveur MCP. |
| Clé des empreintes | La clé HMAC des empreintes du proxy : sans elle, une empreinte ne se retourne pas en valeur. |
| Lignage | Le graphe, les événements bruts et la file : leur justesse fait celle du rappel. Une lecture manquante écarte à tort une réponse touchée, une lecture inventée en rappelle une saine. |
| Destinataires | Les pseudonymes des destinataires des réponses, sans table de correspondance. |
| Continuité des agents | La disponibilité et la latence des appels d'outils, que la capture ne doit ni bloquer ni retarder. |
| Disponibilité du service | Le proxy, l'API, les workers et la base, qui doivent tenir devant une entrée démesurée. |
| Poste et serveurs | Les machines qui font tourner AnswerLineage et ses outils : leurs fichiers, leur terminal, leurs identifiants. |
| Artefacts publiés | L'image, les paquets, leurs signatures et attestations, et le pipeline qui les produit. |
| Mesures et jugements | Le corpus de vérité, le corpus d'abus et le kit de conformité, qui disent si le moteur rappelle ce qu'il doit, si une attaque est bloquée, si un émetteur ne fuit rien. |
| Secret des webhooks | Le secret HMAC que les workers et le récepteur de webhooks partagent : qui le détient signe des messages que le récepteur accepte. |
| Annonces d'incident | Les messages qui disent au récepteur de webhooks qu'un incident est déclaré, analysé ou en échec, avec ses décomptes : une alerte ou un rappel peut en partir. |
| Périmètres en quarantaine | Les jeux de données qu'un incident met en quarantaine, que chaque proxy apprend de l'API : demain, l'annotation ou le blocage des lectures de ces jeux en dépendra. |

## Menaces par composant

Le détail de chaque menace, son scénario, ses parades avec leur code et leur test, et les cas du [corpus d'abus](../corpus.md#corpus-dabus) qui l'éprouvent, est dans le fichier du modèle.

### Proxy MCP

| Menace | Ce qui est craint | Catégorie | Traitement |
| --- | --- | --- | --- |
| `proxy/jeton-conserve` | Un jeton de l'agent est conservé, journalisé ou rejoué par le proxy | Divulgation | 2 parades |
| `proxy/contenu-dans-l-evenement` | Un contenu d'appel sort du proxy dans un événement, un journal ou une métrique | Divulgation | 4 parades |
| `proxy/empreinte-retournee` | Une empreinte est retournée en valeur par dictionnaire | Divulgation | 3 parades |
| `proxy/page-web-hostile` | Une page web ouverte sur le poste appelle des outils à travers le proxy local | Élévation de privilège | 2 parades |
| `proxy/appel-cache-au-lignage` | Un appel d'outil échappe au lignage par des en-têtes ou un corps ambigus | Répudiation | 2 parades |
| `proxy/agent-retarde` | La capture retarde ou bloque un appel d'agent | Déni de service | 6 parades |
| `proxy/entree-demesuree` | Une entrée démesurée épuise la mémoire ou le temps du proxy | Déni de service | 4 parades |
| `proxy/sortie-du-prefixe` | Une requête sort du préfixe du serveur MCP | Élévation de privilège | 1 parade |
| `proxy/requete-relayee-alteree` | La requête relayée est altérée par l'étiquetage de son run | Altération | 5 parades |
| `proxy/nom-piege` | Un nom piégé entre dans l'événement | Altération | 2 parades |
| `proxy/lecture-omise` | L'analyse du SQL omet une colonne lue | Altération | 2 parades |
| `proxy/regle-de-lecture-trompee` | Un outil non SQL lit autre chose que ce que sa règle de lecture déclare | Altération | 1 parade, acceptation à signer |
| `proxy/perimetre-usurpe` | Une API compromise ou usurpée pousse aux proxys un faux périmètre en quarantaine | Usurpation | 4 parades, acceptation à signer |
| `proxy/jeton-hors-audience` | Une passerelle pousse à accepter un jeton hors de son audience | Élévation de privilège | 3 parades |
| `proxy/metadonnees-hostiles` | Les métadonnées OAuth du serveur MCP, lues au démarrage, retiennent ou trompent le proxy | Déni de service | 4 parades |
| `proxy/port-d-administration` | Le port d'administration fait sortir l'état du proxy ou sert à le gêner | Divulgation | 2 parades |
| `proxy/arret-sous-charge` | L'arrêt ou le redéploiement d'un proxy fait échouer des appels d'agents | Déni de service | 3 parades |

### API : ingestion, lecture, incidents, service de l'explorateur

| Menace | Ce qui est craint | Catégorie | Traitement |
| --- | --- | --- | --- |
| `api/emetteur-usurpe` | Un émetteur envoie des événements au nom d'un autre | Usurpation | 1 parade, acceptation à signer |
| `api/releve-usurpe` | Un relevé de couverture est publié au nom d'un autre émetteur | Usurpation | 1 parade, acceptation à signer |
| `api/appelant-non-authentifie` | Un appelant qui joint l'API lit le lignage et déclare des incidents | Divulgation | 1 parade, acceptation à signer |
| `api/rejeu-non-authentifie` | Un appelant qui joint l'API lit la file de rebut et la rejoue | Élévation de privilège | 2 parades, acceptation à signer |
| `api/page-web-hostile` | Une page web ouverte sur le poste se sert de l'API locale | Élévation de privilège | 3 parades |
| `api/corps-demesure` | Un corps démesuré épuise l'ingestion | Déni de service | 4 parades |
| `api/debit-non-borne` | Un émetteur inonde l'ingestion de requêtes valides | Déni de service | Acceptation à signer |
| `api/identifiant-piege` | Un identifiant piégé entre dans le graphe | Altération | 2 parades |
| `api/valeur-renvoyee` | Une erreur renvoie la valeur reçue | Divulgation | 2 parades |
| `api/lecture-epuisante` | Une lecture du graphe épuise la base | Déni de service | 3 parades |
| `api/injection-sql` | Une valeur reçue est exécutée comme du SQL | Altération | 1 parade |
| `api/rejeu` | Un rejeu ou un désordre des événements change le graphe | Altération | 2 parades |
| `api/fichier-hors-explorateur` | L'API sert un fichier hors de l'explorateur construit | Divulgation | 1 parade |
| `api/donnees-au-repos` | Un accès à la base livre ce qu'elle tient | Divulgation | 1 parade, acceptation à signer |

### Workers

| Menace | Ce qui est craint | Catégorie | Traitement |
| --- | --- | --- | --- |
| `workers/evenement-empoisonne` | Un événement qui fait échouer la projection bloque la file | Déni de service | 7 parades |
| `workers/double-traitement` | Deux workers traitent un événement deux fois, ou un arrêt en perd un | Altération | 2 parades |
| `workers/identifiant-non-revalide` | Un événement brut enregistré avant un contrôle plus strict projette un identifiant piégé | Altération | 1 parade |
| `workers/analyse-epuisante` | L'analyse d'un incident déclaré épuise les workers | Déni de service | 2 parades |
| `workers/partition-manquante` | Une partition manquante fait refuser l'ingestion | Déni de service | 2 parades |
| `workers/emetteur-muet` | Un émetteur se tait et le trou de lignage passe inaperçu | Répudiation | 1 parade |
| `workers/reconstruction-destructrice` | Une reconstruction du graphe détruit du lignage qu'elle ne peut pas rendre | Altération | 5 parades |
| `workers/graphe-partiel` | Une analyse ou une lecture conclut sur un graphe en cours de reconstruction | Altération | 2 parades, acceptation à signer |
| `workers/reemission-fuite` | La réémission livre le lignage à une destination mal réglée ou écoutée | Divulgation | 4 parades |
| `workers/reemission-detournee` | La destination de la réémission est détournée vers une autre adresse | Élévation de privilège | 4 parades, acceptation à signer |
| `workers/reemission-file-epuisante` | Une destination en panne fait grossir la file sans fin ou retarde la projection | Déni de service | 5 parades |
| `workers/webhook-usurpe` | Un tiers se fait passer pour AnswerLineage auprès du récepteur des webhooks | Usurpation | 3 parades, acceptation à signer |
| `workers/webhook-rejoue` | Un message capté est rejoué au récepteur des webhooks | Altération | 2 parades, acceptation à signer |
| `workers/webhook-secret-divulgue` | Le secret des webhooks, ou ce que tient un incident, sort par le réglage, le journal, la base ou le message | Divulgation | 4 parades |
| `workers/webhook-file-epuisante` | Un récepteur en panne ou un afflux d'incidents fait grossir la file des webhooks ou retarde les workers | Déni de service | 5 parades |
| `workers/webhook-detourne` | L'adresse du récepteur des webhooks est détournée vers une autre adresse | Élévation de privilège | 3 parades, acceptation à signer |
| `workers/assertion-texte-libre` | Le texte libre d'une assertion de qualité gagne la base, l'API, le journal ou un message | Divulgation | 2 parades |
| `workers/propositions-inondees` | Un émetteur inonde les workers de tests en échec et masque les vraies propositions | Déni de service | 3 parades, acceptation à signer |

### Ligne de commande

| Menace | Ce qui est craint | Catégorie | Traitement |
| --- | --- | --- | --- |
| `cli/reponse-piegee` | Une API piégée injecte du texte dans le terminal | Altération | 2 parades |
| `cli/corps-demesure` | Une réponse démesurée épuise la commande | Déni de service | 2 parades |
| `cli/redirection` | Une redirection emmène la commande vers une autre adresse | Divulgation | 1 parade |
| `cli/journal` | La bibliothèque HTTP journalise les requêtes et les réponses de la commande | Divulgation | 1 parade |
| `cli/adresse-avec-secret` | Une adresse d'API porteuse d'un secret est recopiée ou suivie | Divulgation | 1 parade |

### Entreprise synthétique, corpus de vérité et corpus d'abus

| Menace | Ce qui est craint | Catégorie | Traitement |
| --- | --- | --- | --- |
| `corpus/corpus-d-abus-affaibli` | Le corpus d'abus est affaibli pour laisser passer une régression | Altération | 3 parades |
| `corpus/verite-faussee` | La vérité qui mesure le moteur est faussée | Altération | 3 parades |
| `corpus/donnees-reelles` | Le corpus porte des noms de personnes ou des contenus | Divulgation | 3 parades |

### Kit de conformité

| Menace | Ce qui est craint | Catégorie | Traitement |
| --- | --- | --- | --- |
| `conformance/fuite-non-vue` | Un émetteur qui fuit du contenu passe le kit | Divulgation | 3 parades |
| `conformance/rapport-qui-fuit` | Le rapport du kit recopie le contenu qu'il a trouvé | Divulgation | 1 parade |
| `conformance/fichier-hostile` | Un fichier jugé épuise le kit ou piège son terminal | Déni de service | 3 parades |
| `conformance/schema-substitue` | Le kit juge contre un schéma substitué | Altération | 2 parades |
| `conformance/resume-a2a-mensonger` | Un agent appelé en A2A déclare des lectures fausses dans son résumé | Usurpation | 2 parades, acceptation à signer |

### Explorateur

| Menace | Ce qui est craint | Catégorie | Traitement |
| --- | --- | --- | --- |
| `ui/script-injecte` | Un nom venu d'un émetteur exécute un script dans l'explorateur | Élévation de privilège | 2 parades |
| `ui/adresse-piegee` | Une adresse partagée piégée fait agir l'explorateur | Altération | 2 parades |
| `ui/cadre-detourne` | Une page tierce encadre l'explorateur pour détourner ses clics | Altération | 1 parade |

### Pipeline et publication

| Menace | Ce qui est craint | Catégorie | Traitement |
| --- | --- | --- | --- |
| `pipeline/action-deplacee` | Une action ou une image tierce change sous la même étiquette | Altération | 3 parades |
| `pipeline/merge-request-privilegiee` | Le code d'une merge request obtient un droit d'écriture ou un secret | Élévation de privilège | 3 parades |
| `pipeline/agent-de-code-piege` | Un agent de code suit une instruction piégée avec un secret ou un droit d'écriture | Élévation de privilège | 2 parades |
| `pipeline/garde-fou-modifie` | Une merge request affaiblit un garde-fou sans revue du mainteneur | Élévation de privilège | 3 parades |
| `pipeline/reglages-du-depot` | Une protection du dépôt manque : une étiquette, une fusion ou un label viennent d'un autre que le mainteneur | Élévation de privilège | Acceptation à signer |
| `pipeline/secret-dans-l-historique` | Un secret entre dans l'historique public | Divulgation | 1 parade |
| `pipeline/image-vulnerable` | Une image est publiée avec une vulnérabilité haute ou critique connue | Altération | 2 parades |
| `pipeline/artefact-contrefait` | Un artefact contrefait passe pour une version du projet | Usurpation | 3 parades |
| `pipeline/version-incoherente` | Une version publiée installe des composants d'une autre version | Altération | 2 parades |
| `pipeline/nom-usurpe` | Un tiers publie sous un nom du projet sur PyPI ou npm | Usurpation | 1 parade, acceptation à signer |
| `pipeline/dependance-compromise` | Une dépendance compromise entre au verrou, dans l'image ou dans l'explorateur | Altération | Acceptation à signer |
| `pipeline/conteneur-de-developpement` | Le conteneur de développement expose les identifiants de l'hôte | Divulgation | 2 parades |
| `pipeline/modele-perime` | Un composant ou une version sort sans que ses menaces aient été revues | Répudiation | 2 parades |

## Acceptations

Un risque accepté l'est par le mainteneur, pour un motif dit et une conséquence dite. Les acceptations ci-dessous sont écrites, pas signées : la publication d'une version est refusée tant que l'une d'elles reste à signer. Les [cas ouverts du corpus d'abus](../corpus.md#cas-ouverts), attaques que le produit ne bloque pas encore, sont cités par la menace qui les accepte ; un test le vérifie.

| Menace | Motif | Conséquence | Signature |
| --- | --- | --- | --- |
| `proxy/regle-de-lecture-trompee` | Une règle de lecture se fie à l'outil : le proxy relaie l'appel sans en lire le résultat, et ne peut savoir ce que l'outil a lu au-delà de ce que déclare son exploitant. | Une réponse fondée sur une lecture non déclarée n'est pas rappelée, et la couverture ne le montre pas, puisque l'appel est vu. | à signer |
| `proxy/perimetre-usurpe` | Le proxy n'authentifie pas encore l'API : il se fie au TLS de l'adresse que règle l'exploitant, et le périmètre n'agit encore sur aucun appel. Avant que le proxy annote ou bloque des lectures, la diffusion devra être authentifiée, par un périmètre signé ou l'identité vérifiée de l'API. | Qui compromet l'API, ou la joint en http sur un réseau qu'il écoute, fait croire aux proxys une quarantaine fausse ou en tait une vraie : aujourd'hui sans effet sur les appels ; le jour où le proxy agira, il annotera ou bloquera à tort, ou laissera passer des lectures en quarantaine. | à signer |
| `api/emetteur-usurpe` | Les émetteurs ne sont pas encore authentifiés : l'API est faite pour la boucle locale ou un réseau de confiance, comme le dit la politique de sécurité. | Un tiers qui joint l'ingestion peut inventer des lectures, donc faire rappeler des réponses saines, fausser le décompte des doublons d'un émetteur, et faire proposer des incidents qu'un humain devra écarter. | à signer |
| `api/releve-usurpe` | Un relevé n'est pas authentifié plus qu'un événement, et deux relevés d'une même fenêtre se fusionnent au plus grand. | Un tiers qui joint l'ingestion peut faire croire à des appels que le graphe n'a pas : la couverture d'une fenêtre se lit alors partielle à tort. | à signer |
| `api/appelant-non-authentifie` | L'API n'authentifie pas encore ses appelants : elle est faite pour la boucle locale ou derrière un contrôle d'accès. | Exposée sans contrôle d'accès, elle livre le lignage à qui la joint et le laisse déclarer des incidents, donc lancer des analyses, et mettre un incident en quarantaine ou la lever. | à signer |
| `api/rejeu-non-authentifie` | Le rebut se lit et se rejoue sans authentification, comme le reste de l'API : elle est faite pour la boucle locale ou derrière un contrôle d'accès. Le rejeu reviendra à l'administrateur quand l'API distinguera les rôles de ses appelants. | Exposée sans contrôle d'accès, l'API laisse qui la joint lire les émetteurs et les identifiants des événements au rebut, et les remettre en file autant de fois qu'il le veut : les workers refont un travail sans effet sur le graphe, et un événement qui échoue encore revient au rebut. | à signer |
| `api/debit-non-borne` | L'ingestion n'a pas de limite de débit par émetteur ; sans authentification, une telle limite se contournerait en changeant de nom d'émetteur. | Un tiers qui joint l'ingestion peut retarder la projection des événements légitimes et faire grossir la base jusqu'à sa purge. | à signer |
| `api/donnees-au-repos` | AnswerLineage ne chiffre pas la base : son accès, le chiffrement du disque et des sauvegardes reviennent à l'exploitant. Le mot de passe de la démonstration est écrit dans compose.yaml, pour une base que la démonstration ne publie pas. | Qui obtient un accès à la base lit les noms des jeux de données, des agents et des outils, les empreintes et les pseudonymes, sans aucun contenu ; avec la clé des empreintes en plus, il peut éprouver des valeurs candidates. | à signer |
| `workers/graphe-partiel` | La reconstruction ne bloque pas les lectures de l'API, pour qu'elle reste disponible : un événement est à tout instant dans la file ou dans le graphe, et la couverture du lignage compte comme trous les appels d'outil encore en file. | Pendant une reconstruction, une lecture voit un graphe partiel : une réponse dont l'événement d'agent attend encore dans la file manque sans que la couverture le dise. | à signer |
| `workers/reemission-detournee` | Les adresses internes autres que les métadonnées et le lien local sont admises : le catalogue d'une entreprise est souvent sur son réseau privé, et l'adresse ne vient que de l'exploitant. | Un exploitant qui règle par erreur l'adresse d'un service interne y envoie les événements projetés, sans contenu ; le contrôle ne le distingue pas d'un catalogue. | à signer |
| `workers/webhook-usurpe` | Le secret est symétrique : le récepteur le garde, en clair, pour vérifier, et peut donc signer comme les workers. Sa rotation est manuelle : AnswerLineage accepte deux secrets le temps d'une rotation, mais ne garde pas le secret du récepteur et ne le fait pas tourner à sa place. | Qui lit le secret chez le récepteur ou dans l'environnement des workers forge des messages que le récepteur accepte, jusqu'à la rotation suivante. | à signer |
| `workers/webhook-rejoue` | La vérification de référence ne garde aucun état : écarter, par son identifiant de livraison, un message rejoué dans la fenêtre de tolérance revient au récepteur. | Un récepteur qui ne garde pas les identifiants vus accepte, pendant cinq minutes, un message capté et renvoyé : il annonce deux fois le même changement d'état. | à signer |
| `workers/webhook-detourne` | Comme pour la réémission, les adresses internes autres que les métadonnées et le lien local sont admises : le récepteur d'une entreprise est souvent sur son réseau privé, et l'adresse ne vient que de l'exploitant. | Un exploitant qui règle par erreur l'adresse d'un service interne y envoie les annonces d'incident, signées, sans contenu ; le contrôle ne le distingue pas d'un récepteur. | à signer |
| `workers/propositions-inondees` | Les émetteurs ne sont pas authentifiés, et une borne par émetteur se contournerait en changeant d'espace de noms : seule la borne globale des propositions ouvertes tient la table et la liste. | Un émetteur hostile peut remplir les 100 propositions ouvertes et faire refuser les signaux des vrais tests en échec, jusqu'à ce que des propositions soient décidées ; le refus se compte et se dit au journal. | à signer |
| `conformance/resume-a2a-mensonger` | Le résumé A2A n'est pas signé dans la version 1 de l'extension, et aucun adaptateur A2A n'est encore fourni : le lignage d'un agent appelé se prend sur sa parole. | Une lecture tue par un agent appelé manque au lignage de son appelant : une réponse qui en dépend n'est pas rappelée. | à signer |
| `pipeline/reglages-du-depot` | Ces réglages ne se versionnent pas : aucun contrôle du dépôt ne peut les vérifier. Ils sont listés pour le mainteneur dans la documentation du pipeline et de la publication. | Si l'un d'eux manque, les artefacts restent signés et attestés, mais par une identité qu'un tiers a pu mettre en mouvement. | à signer |
| `pipeline/nom-usurpe` | PyPI et npm ne réservent un nom qu'à sa première publication : le mainteneur réserve ceux du projet avant la première étiquette, comme le dit le guide de publication. | Tant qu'un nom n'est pas réservé, un tiers peut le prendre, et qui installe le projet sous ce nom installe son paquet. | à signer |
| `pipeline/dependance-compromise` | Les dépendances sont verrouillées, attendent sept jours avant d'être adoptées (exclude-newer, minimumReleaseAge), et l'explorateur s'installe sans exécuter de script ; aucun test ne prouve ces parades, et une version malveillante plus ancienne que le délai passe. | Une dépendance compromise depuis plus de sept jours entre au verrou à la mise à jour suivante, puis dans l'image et les paquets ; l'analyse des vulnérabilités ne voit que ce qui est déjà connu. | à signer |

## La revue d'une version

La revue se fait dans la merge request qui [prépare la version](../publication.md#comment-sort-une-version), celle qui change la version des composants publiables. Le mainteneur :

1. relit chaque menace devant le code de la version : ses parades tiennent-elles encore, une frontière, une entrée ou un composant nouveaux en appellent-ils d'autres ;
2. pour chaque acceptation retenue, la signe dans le fichier du modèle, de son compte et du jour :

   ```toml
   [threat.acceptance]
   reason = "…"
   consequence = "…"
   accepted_by = "@alebgl77"
   accepted_on = 2026-12-01
   ```

   Une acceptation qui n'est plus retenue devient une parade, ou attend la version suivante : la publication la refuse tant qu'elle n'est pas signée ;
3. porte dans `[review]` la version des paquets préparée et le jour de la revue, et les reporte en tête de ce document, avec la signature de chaque acceptation dans le tableau ci-dessus : un test vérifie que le document et le modèle disent la même chose.

`make packages` refuse une version déclarée pour laquelle le modèle n'a pas été revu, et liste les acceptations qui restent à signer ; l'étiquette posée, le job `prepare` refuse les deux.

## Faire évoluer le modèle

- **Un composant nouveau**, membre de l'espace de travail uv, paquet npm ou pipeline, entre au modèle dans la merge request qui l'ajoute : une entrée `[[component]]` et ses menaces. Sans elles, `make policy` échoue et nomme le composant. Un composant retiré du dépôt sort du modèle de même.
- **Une menace** garde son identifiant : un commit, une issue ou un avis de sécurité peuvent le citer. Elle porte au moins une parade ou une acceptation ; la parade nomme le code et le test, au besoin la fonction (`chemin::fonction`), et le contrôle échoue si l'un des deux disparaît ou est renommé.
- **Un cas ouvert du corpus d'abus** est cité (`abuse_cases`) par une menace qui l'accepte. Le jour où le cas est bloqué, l'acceptation se réécrit : ce qui reste, ou une parade.
- **Une acceptation** s'écrit avec son motif et sa conséquence, sans signature : seul le mainteneur la signe.

## Limites connues

- Le modèle dit les menaces connues et ce qui les pare ; il ne prouve pas qu'il n'y en a pas d'autres.
- Le contrôle vérifie que le code et le test d'une parade existent et que la fonction nommée y est définie, pas que le test prouve la parade : c'est l'objet de la revue.
- Les propriétaires du modèle se lisent dans `.github/CODEOWNERS` par un chemin exact ou un dossier ; une règle à jokers n'est pas interprétée.
- Une acceptation signée le reste d'une version à l'autre : la revue la relit, rien ne l'y oblige.
- Seuls les paquets npm d'un dossier de premier niveau sont reconnus comme composants.
