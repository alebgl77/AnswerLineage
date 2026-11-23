# Documentation

## Comprendre le projet

- [Démarrage](demarrage.md) : du clone au premier lignage visible, vérifié par le pipeline
- [Concepts](concepts.md) : le vocabulaire du produit, un exemple de l'entreprise synthétique pour chaque mot
- [Architecture](architecture.md) : les composants, le flux de la capture à la lecture, le modèle de données, les invariants
- [Décisions d'architecture](decisions/README.md) : le contexte, l'alternative écartée et les conséquences de chaque choix structurant

## Les composants

- [Proxy MCP](proxy.md)
- [Adaptateurs d'agents](adaptateurs.md) : l'adaptateur Claude Agent SDK, branché par les crochets de l'agent
- [API d'ingestion](ingestion.md)
- [API de lecture](lecture.md) : remonter d'une réponse à ses sources, propager un incident, classer les données par exposition
- [Facettes OpenLineage d'AnswerLineage](facettes.md)
- [Kit de conformité des émetteurs](conformite.md)
- [Extension A2A de lignage](a2a.md)
- [Modèle de menaces](securite/modele-de-menaces.md) : frontières de confiance, actifs, menaces de chaque composant, parades et acceptations, revu à chaque version
- [Authentification et rôles](securite/authentification.md) : fournisseur OIDC, rôles, matrice des droits, jetons des clients, explorateur et démonstration
- [Incidents](incidents.md) : déclaration, analyse d'impact, états
- [Workers](workers.md)
- [Schéma du graphe](schema.md)
- [Ligne de commande](cli.md)
- [Explorateur](explorateur.md)
- [Entreprise synthétique, corpus de vérité et corpus d'abus](corpus.md)
- [Réémission vers un catalogue OpenLineage](reemission.md)
- [Webhooks signés](webhooks.md) : les changements d'état des incidents annoncés à un récepteur, format publié et vérification de la signature

## Faire tourner et publier

- [Démonstration](demonstration.md)
- [Pipeline d'intégration continue](ci.md)
- [Publication](publication.md)
