# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Dépôt public

Ce dépôt est public et s'adresse à des professionnels. Rien d'interne n'y figure, ni dans les fichiers, ni dans les messages de commit, ni dans les merge requests :

- aucun identifiant de suivi interne (story, exigence, sprint, risque) et aucune référence au cahier des charges ou au backlog, qui sont privés : le code et la documentation disent le pourquoi en clair ;
- pas de commit de correction : un défaut trouvé avant fusion se corrige dans le commit qui l'a introduit, en réécrivant la branche de travail, jamais `main` ;
- pas de test qui recopie une configuration, pas de dossier vide ni de composant annoncé : un fichier entre dans le dépôt quand il sert.

Le périmètre et l'ordre des travaux sont fixés par le cahier des charges et le backlog. Les demander au mainteneur plutôt que de les deviner.

## Commandes

```bash
make setup    # uv sync --all-packages --locked, puis installation des hooks pre-commit
make check    # porte de toute contribution : politique des workflows, format, lint, typage strict, tests, couverture
make format   # reformate et applique les corrections automatiques de ruff
make test     # tests seuls, avec le rapport de couverture
make policy   # contrôle de politique des workflows et des scripts
uv run pytest proxy/tests/test_sql.py::test_select_star_reads_the_whole_table   # un test isolé
uv run pytest -m "not integration"                                              # sans PostgreSQL
```

Les tests marqués `integration` démarrent un PostgreSQL réel avec Testcontainers : Docker doit tourner. `docs/ci.md` montre comment rejouer une case de la matrice (Python 3.13 et 3.14, PostgreSQL 17 et 18).

Une dépendance s'ajoute dans le `pyproject.toml` du composant, puis `uv lock`. Le `pyproject.toml` racine impose un délai de carence de 7 jours (`exclude-newer`).

## Ce que fait le produit

AnswerLineage retrouve, pour une donnée reconnue fausse, les réponses d'agents qui en dépendent et leurs destinataires, puis organise leur rappel après validation humaine. Il fait aussi le chemin inverse, d'une réponse vers ses sources.

## Architecture

Des services sans état autour d'un seul PostgreSQL. Le flux : capture → événements OpenLineage → ingestion → graphe → lecture.

| Composant | Rôle | Technologie |
| --- | --- | --- |
| `proxy/` | Proxy MCP transparent : relaie à l'identique, émet un événement par appel d'outil, analyse le SQL à côté de l'appel | ASGI, httpx, sqlglot |
| `api/` | Schéma du graphe, migrations, écriture du graphe, API d'ingestion OpenLineage | FastAPI, SQLAlchemy Core, Alembic, psycopg |

Chaque composant est un membre de l'espace de travail uv : sources dans `<composant>/src/answerlineage_<composant>/`, tests dans `<composant>/tests/` (importés sous `<composant>.tests`). Les tests qui croisent plusieurs composants sont dans `tests/`. Un nouveau composant s'ajoute au `pyproject.toml` racine à six endroits : membres de l'espace de travail, `src` de ruff, `mypy_path` et `files` de mypy, `testpaths` de pytest, `source` de coverage, chemins de la porte de couverture.

Ce qui demande de lire plusieurs fichiers :

- **Contrat d'événements.** Le RunEvent d'OpenLineage 1.55 (schéma 2-0-2, copié dans `spec/openlineage/`). Une requête d'agent est un run ; un appel d'outil est un run enfant ; les données lues sont les `inputs`. Le proxy construit ses événements dans `proxy/.../events.py`, l'API les valide dans `api/.../ingest/models.py`, et `tests/test_contract_proxy_ingestion.py` vérifie que les deux s'accordent avec le schéma officiel.
- **Le proxy décide sur le corps de la requête**, jamais sur les en-têtes MCP (`inspection.py`). Les en-têtes ne servent qu'à être comparés au corps.
- **Rien ne retarde un appel.** Le relais (`app.py`) rend la réponse au fil de l'eau ; l'événement est construit après l'échange, et l'analyse du SQL (`sql.py`) tourne ensuite dans un fil à part.
- **L'analyse du SQL échoue du côté sûr.** Elle ne liste les colonnes d'une table que si la liste est sûre d'être complète ; sinon la table est rendue en entier, ou la lecture est « non résolue ». Une colonne manquante ferait écarter à tort une réponse touchée.
- **Écritures du graphe idempotentes et commutatives** (`api/.../store/graph.py`) : rejouer un événement, ou recevoir deux événements d'un run dans le désordre, donne le même graphe.
- **Schéma et migrations.** `store/schema.py` déclare le schéma, les migrations Alembic le créent, et un test compare la base migrée au schéma déclaré. Une migration nomme ses contraintes et fournit sa descente.
- **Fixtures PostgreSQL.** `conftest.py` à la racine fournit `database` (base migrée, clonée d'un modèle) et `empty_database`, une base par test.

## Invariants du produit

Un changement qui en viole un est faux, même si ses tests passent.

1. Aucun contenu de prompt, de résultat d'outil ou de réponse n'est stocké, journalisé ni mis en métrique : des empreintes seulement. Les bibliothèques journalisent parfois ce contenu à notre place (URL complète, requête SQL) : chaque composant a un test canari qui le vérifie.
2. Aucun modèle dans AnswerLineage : tout résultat est déterministe.
3. La capture ne bloque ni ne retarde jamais un agent ; elle reste ouverte en cas de panne.
4. Le proxy ne termine pas OAuth et ne conserve aucun jeton.
5. PostgreSQL est la seule dépendance obligatoire.
6. Toute entrée est hostile : un identifiant reçu n'est recopié que s'il a une forme admise, et toute entrée est bornée.
7. Tout texte produit suit un gabarit fixe : aucun texte libre d'un émetteur n'atteint un modèle ou un destinataire.
8. Aucun rappel ne part sans validation humaine. Les destinataires sont pseudonymisés, sans table de correspondance.

## Façon de travailler

- Tests écrits avant le code. Un changement cohérent par commit, message au format des commits conventionnels, en français.
- La porte est `make check`. La couverture tient 85 % par composant, en branches comme en lignes et branches.
- Le métier se dit en français (rappel, piste, écart, criticité, quarantaine), les identifiants techniques en anglais. « Rappel » désigne à la fois la notification des destinataires et la métrique *recall*.
- Les actions GitHub s'épinglent par empreinte de commit et les images par empreinte `@sha256:` ; `make policy` le vérifie.
- Reviennent au mainteneur : fusionner, publier, poser un label, lire un secret, ajouter une dépendance, modifier la configuration d'agents (`.claude/`, `.mcp.json`), ce fichier ou les garde-fous du pipeline.
