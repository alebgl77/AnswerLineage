# Explorateur

L'explorateur montre le lignage en cinq colonnes, des données aux personnes : jeux de données, outils, agents, réponses, destinataires. Il se lit dans un navigateur, sans compte ni installation.

![L'explorateur en mode Propager : une table fausse, les réponses et les destinataires touchés](images/explorateur-propager.png)

## Le lancer

L'interface est faite de fichiers statiques, que l'API sert à sa racine. Elle se construit une fois, avec Node 22 :

```bash
make ui
```

Puis l'API reçoit le dossier construit :

```bash
ANSWERLINEAGE_UI_DIR=ui/dist uv run uvicorn --factory answerlineage_api.app:create_app
```

L'explorateur est alors sur `http://127.0.0.1:8000/`. Sans `ANSWERLINEAGE_UI_DIR`, l'API ne sert aucune page.

## Ce qu'il montre

| Vue | Adresse | Contenu |
| --- | --- | --- |
| Vue d'ensemble | `#/` | Les 60 réponses les plus récentes et tout ce qu'elles touchent |
| Jeu de données | `#/donnees?namespace=…&name=…` | La vue d'ensemble, ce jeu de données choisi |
| Réponse | `#/reponse/<identifiant>` | Une réponse et sa chaîne : ce qu'elle a lu, par quels outils, pour qui |
| Propager | `#/propager?namespace=…&name=…&from=…&to=…` | Un incident : les réponses et les destinataires touchés |

Survoler ou choisir un nœud met sa chaîne en évidence. Depuis une donnée, on suit ce qui en dépend : les outils qui l'ont lue, les agents, les réponses, les destinataires. Depuis une réponse ou un destinataire, on remonte à ce qui a été lu.

**Propager.** Le mode Propager prend un jeu de données et la période pendant laquelle il a été faux. Les réponses et les destinataires touchés s'allument, et trois compteurs s'affichent : réponses touchées, destinataires, couverture du lignage. La couverture dit ce que le résultat peut manquer ; elle n'est pas encore mesurée, et l'explorateur l'écrit.

**Adresses.** L'adresse de la page dit ce que l'explorateur montre. Copiée dans un ticket ou un message, elle rouvre le même incident, la même réponse ou le même jeu de données, dans le même mode. L'identifiant d'une réponse reste valable si le graphe est reconstruit.

## Ce qu'il ne fait pas

L'explorateur ne lit que l'[API de lecture](lecture.md) : il n'affiche aucun contenu de prompt, de résultat d'outil ou de réponse, parce que l'API n'en a pas. Une réponse est une heure et une empreinte, un destinataire est un pseudonyme.

Il ne charge rien d'ailleurs. L'API le sert sous une politique de sécurité du contenu qui n'autorise que ses propres fichiers : ni script ni style en ligne, aucune ressource tierce, aucune inclusion dans une autre page. L'application pose cette politique et ses autres en-têtes de sécurité sur toute réponse, page d'erreur comprise (fichier absent, méthode refusée, plage d'octets impossible) ; seule la durée de conservation est propre aux fichiers : les fichiers à empreinte de `assets/` sont gardés un an, la page n'est jamais gardée.

## Développer l'interface

```bash
npm ci --prefix ui && npm run --prefix ui dev
```

Le serveur de développement écoute sur `http://127.0.0.1:5173` et relaie `/api` vers une API lancée sur le port 8000.

| Commande | Usage |
| --- | --- |
| `make ui` | Installe les paquets verrouillés, contrôle les types, construit `ui/dist` |
| `make e2e` | Pilote l'explorateur construit dans Chromium, sur l'entreprise synthétique |

Les tests de bout en bout comparent ce que la page affiche à l'entreprise générée : les cinq colonnes et leurs liens en moins de 2 secondes, rien qui déborde sur un écran de 400 pixels de large, la chaîne allumée d'un jeu de données, les compteurs d'un incident, les adresses rouvertes dans un nouvel onglet. Une erreur dans la console du navigateur, refus de la politique de contenu compris, fait échouer le test.

Les sources sont dans `ui/src/` : `graph.ts` déduit les liens et la chaîne d'un nœud, `route.ts` lit et écrit l'adresse, `Graph.tsx` dessine les colonnes et trace les liens en SVG.

## Limites connues

- La vue d'ensemble montre 200 réponses au plus, et une propagation les 200 réponses touchées les plus récentes ; les compteurs, eux, portent sur tout l'incident.
- Les heures sont en UTC.
- L'interface est en français, en thème clair, et n'a pas encore passé d'audit d'accessibilité.
- L'explorateur n'a pas de contrôle d'accès : il est fait, à ce stade, pour la boucle locale.
