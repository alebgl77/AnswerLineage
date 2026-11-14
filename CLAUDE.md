# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## État du dépôt

Aucun code n'existe encore. Ce fichier est dérivé du PRD et du backlog, pas d'un code lu : tout ce qui suit est une **cible de conception**, à corriger dès que le code la contredit.

Le PRD prévoit un kit de démarrage (`answerlineage-kit.zip` : racine du dépôt, `Makefile`, `.claude/`, `backlog/`, `scripts/`, `docs/`) qui contient son propre `CLAUDE.md`. Quand il sera décompressé ici, son `CLAUDE.md` fait foi et remplace celui-ci.

## Sources de vérité

- PRD (onglets PRD, Audit, Développement) : https://claude.ai/artifact/Q8LoLptQypma5RAWyD8cko — copie prévue dans `docs/`.
- Backlog v3 (161 stories, 138 exigences, 24 risques) : https://docs.google.com/spreadsheets/d/1ssVf76ko4vkuo7N4t9x2frqo3MJGSrpZzQBd-F7v2QM — copie générée prévue dans `backlog/`.
- Identifiants : `E01`–`E20` epics, `US-EESS` stories, `EF-XXX-NN` exigences fonctionnelles, `ENF-XXX-NN` non fonctionnelles, `ADR-001`–`ADR-012`, `R01`–`R24` risques. Citer l'identifiant de story dans la branche, la merge request et le commit.
- PRD, backlog et code sont en français pour le métier (rappel, piste, écart, criticité, quarantaine), en anglais pour les identifiants techniques. « Rappel » désigne à la fois la notification des destinataires et la métrique *recall* du moteur.

## Commandes

Fournies par le kit, absentes tant qu'il n'est pas dans le dépôt :

```bash
make setup            # environnement uv, pre-commit
make check            # format, lint (ruff), typage strict (mypy), tests (pytest) — porte de toute story
make sprint           # état du sprint, lu dans git (story finie = son identifiant dans un commit de main)
make next             # stories prêtes, une par piste (composant) libre
scripts/story US-0401 # ouvre la session d'une story : modèle, effort, mode plan dès 5 points, worktree .claude/worktrees/US-XXXX
```

Test isolé : outillage `uv` + `pytest`, donc a priori `uv run pytest chemin/test_x.py::test_nom` — à confirmer sur le `Makefile` du kit.
Démonstration locale : `docker compose up` (entreprise synthétique à vérité connue, premier lignage en moins de 5 min depuis un clone, ENF-DOC-01).

## Ce que fait le produit

AnswerLineage retrouve, pour une donnée reconnue fausse, les réponses d'agents qui en dépendent et leurs destinataires, puis organise leur rappel après validation humaine. Il fait aussi le chemin inverse (d'une réponse vers ses sources) et tire du même graphe une mémoire de pistes qui recommande des routages. Versions : v0.1 Tracer, v0.2 Rappeler, v0.3 Prévenir, v0.4 Optimiser, v1.0 Durcir.

## Architecture cible

Sept services sans état autour d'un seul PostgreSQL. Flux : capture → événements OpenLineage → ingestion → workers → graphe → API de lecture → usages.

| Dossier | Rôle | Technologie |
| --- | --- | --- |
| `proxy/` | Proxy MCP transparent (HTTP et stdio) : relaie à l'identique, émet un événement par appel d'outil, analyse le SQL hors du chemin de l'appel, applique la quarantaine | Python, ASGI, sqlglot |
| `adapters/` | Captent réponse, diffusion, skills, sous-agents dans le processus de l'agent (Claude Agent SDK, LangGraph, puis Pydantic AI, Google ADK) ; extension A2A | Bibliothèques Python |
| `collector/` | Pont OTLP (spans GenAI) et journaux d'audit d'entrepôt → événements | Python |
| `api/` | Ingestion (valide, déduplique, écrit `event_raw`) et lecture (impact, incidents, rappel, écart, criticité, recommandations) | FastAPI, Pydantic |
| `workers/` | Alimentent le graphe, réconcilient avec l'entrepôt, déposent les traces de pistes, calculent écart et criticité | Python, file sur PostgreSQL |
| `mcp-server/` | Outils `tracer_reponse`, `impact_donnee`, `recommander` | FastMCP |
| `ui/` | Explorateur : graphe en cinq colonnes (données, outils, agents, réponses, destinataires), modes Remonter, Propager, Pistes, Écart, Criticité | TypeScript, React, SVG |
| `deploy/`, `corpus/`, `spec/facets/`, `docs/` | docker compose et chart Helm ; corpus de vérité et corpus d'abus ; schémas JSON des facettes ; PRD, ADR, runbooks | |

Points qui demandent plusieurs fichiers pour être compris :

- **Contrat d'événements** : RunEvent OpenLineage 1.55. Requête d'agent = run ; appel d'outil et délégation A2A = runs enfants (facette `parent`) ; données lues = `inputs` ; écritures, mémoire et réponse = `outputs` versionnés. Facettes standard partout où elles existent, cinq facettes propres seulement : `answerlineage_agent`, `answerlineage_toolCall`, `answerlineage_qos`, `answerlineage_delivery`, `answerlineage_keys`.
- **Corrélation** : l'identifiant de run est dérivé du contexte de trace W3C (`traceparent` dans `_meta` MCP ou en-tête HTTP), jamais d'un en-tête propre. Un même appel vu par le proxy et par l'adaptateur fusionne en un seul run.
- **Découplage** : boîte d'envoi locale bornée côté capture, émission asynchrone, ingestion idempotente et indifférente à l'ordre. Une perte produit un marqueur de trou de lignage ; chaque résultat d'impact affiche sa couverture.
- **Tables** : `event_raw` (partition mensuelle, rejouable), `run`, `dataset`/`dataset_version`, `read_edge` (niveau de précision : table, colonnes, sous-ensemble, clés ; heure d'événement et heure d'enregistrement), `write_edge` (porte la transitivité), `answer`/`delivery`, `declaration`, `incident`/`incident_scope`, `impact`, `trail`, `recommendation_log`, `coverage`, `audit_log`.
- **Moteur d'impact** : lectures candidates dans la fenêtre de validité → classe de confiance (certaine, probable, possible, écartée) par intersection avec le périmètre → remontée jusqu'aux destinataires → transitivité par `write_edge`, uniquement vers l'avant dans le temps, une dépendance indirecte étant au plus « probable ».
- **Seul retour vers le chemin de l'appel** : les périmètres en quarantaine, transmis au proxy (modes observer, avertir, bloquer). C'est le seul cas où le proxy modifie un résultat d'outil.
- **Pistes** : trace par transition (contexte, étape précédente, ressource), oubli exponentiel calculé à la lecture, pénalité ciblée sur la seule ressource fautive, choix par tirage de Thompson à graine fixée.

## Invariants du produit

Une story qui en viole un est fausse, même si ses tests passent.

1. Aucun contenu de prompt, de résultat d'outil ou de réponse n'est stocké, journalisé ni mis en métrique : empreintes HMAC seulement (ADR-004, ENF-CONF-01, test canari).
2. Aucun modèle dans AnswerLineage : tout résultat est déterministe (ADR-012).
3. La capture ne bloque jamais un agent ; elle reste ouverte en cas de panne (ADR-005, ENF-DISP-03). Latence ajoutée par le proxy : p99 ≤ 5 ms (ENF-PERF-01).
4. Le proxy ne termine pas OAuth, ne stocke ni ne journalise aucun jeton, et décide sur le corps de la requête, jamais sur les seuls en-têtes (ADR-011, ENF-SEC-05, EF-SEC-11).
5. PostgreSQL est la seule dépendance obligatoire : graphe, file et pistes (ADR-001). Pas de service cloud propriétaire, installation hors ligne possible.
6. Toute entrée est hostile : identifiants validés (jeu de caractères, longueur, normalisation Unicode), limites sur toute entrée (EF-SEC-01, ENF-SEC-06).
7. Toute sortie de texte suit un gabarit fixe à champs structurés ; aucun texte libre d'un émetteur n'atteint un modèle ou un destinataire (EF-SEC-02).
8. Aucun rappel ne part sans validation humaine tracée (EF-INC-04). Destinataires pseudonymisés par HMAC par locataire, sans table de correspondance (ENF-CONF-02).
9. Les recommandations sont consultatives et s'appliquent par merge request ; la liste d'outils vue par un modèle n'est jamais réordonnée en session (ADR-009).

## Façon de travailler

- Une story, une session, une branche, une merge request. Tests écrits avant le code. Développement sur tronc, branches de moins de deux jours, historique linéaire, commits conventionnels, fusion en un seul commit dont le message est le titre de la merge request.
- Une piste est un composant (`ci`, `api`, `workers`, `proxy`, `adaptateurs`, `collecteur`, `mcp`, `ui`, `deploy`, `corpus`, `docs`) : jamais deux stories de la même piste en parallèle. Les stories d'une même vague de sprint sont indépendantes.
- Fini = critères d'acceptation Gherkin verts en CI, tests au bon niveau (unitaire, intégration, contrat, corpus, e2e, performance, sécurité), couverture de branches ≥ 85 % sur le moteur et le proxy, typage strict sans erreur, documentation et notes de version à jour.
- Portes bloquantes propres au projet, au-delà du lint et des tests : précision et rappel du moteur sur le corpus de vérité (rappel ≥ 0,99 au niveau table, précision ≥ 0,90 aux niveaux colonne et sous-ensemble), corpus d'abus bloqué à 100 %, canaris de contenu et de jeton absents partout.
- Tests d'intégration sur PostgreSQL réel (Testcontainers), migrations Alembic réversibles en trois temps : étendre, migrer, contracter.
- Dépendances : délai de carence de 7 jours, actions GitHub épinglées par empreinte de commit, toute nouvelle dépendance demande un accord.
- Réservé à l'humain : fusionner, publier, poser un label, lire un secret, modifier `.claude/`, `.mcp.json`, `CLAUDE.md`, le backlog, le PRD ou les tests du kit. Une fiche de story dont un critère est faux se signale, elle ne se réécrit pas.

## Versions visées

Python 3.13 (compatibilité 3.14), PostgreSQL 17 et 18, OpenLineage 1.55, MCP 2026-07-28 et 2025-11-25, A2A 1.0. Licence proposée : Apache 2.0 (non tranchée).
