# Pipeline d'intégration continue

Le pipeline est décrit dans `.github/workflows/ci.yml`. Il tourne sur chaque merge request et sur chaque fusion dans `main`.

| Job | Ce qu'il vérifie | Commande locale |
| --- | --- | --- |
| `static` | Politique des workflows, format, lint (ruff), typage strict (mypy) | `make policy format-check lint typecheck` |
| `tests` | Tests et couverture de branches, sur la matrice Python 3.13 et 3.14, PostgreSQL 17 et 18 | `make test` |
| `tests` | Porte de couverture : 85 % par composant, en branches et en lignes et branches (ENF-MAIN-01) | `make coverage-gate` |
| `secrets` | Aucun secret dans tout l'historique (gitleaks), après un canari qui prouve que le détecteur bloque | `make secrets` |
| `codeql` | Analyse de sécurité du code Python et des workflows (CodeQL, requêtes étendues) | |
| `ci-ok` | Toutes les étapes bloquantes sont vertes | |

`make check` enchaîne les mêmes commandes sur le poste.

## Rejouer une case de la matrice sur le poste

Les tests d'intégration démarrent PostgreSQL avec Testcontainers : Docker doit tourner. L'image se choisit par variable d'environnement, la version de Python par `UV_PYTHON`, dans un environnement séparé pour ne pas toucher à `.venv` :

```bash
ANSWERLINEAGE_TEST_POSTGRES_IMAGE=postgres:17 UV_PYTHON=3.14 UV_PROJECT_ENVIRONMENT=.venv-314 uv run --all-packages --locked pytest
```

## Ce qui bloque la fusion

Le pipeline ne bloque une fusion que si la branche `main` est protégée. Ce réglage se fait dans GitHub, par un administrateur du dépôt :

- exiger le contrôle `ci-ok` avant fusion, sur une branche à jour ;
- exiger une merge request, sans contournement pour les administrateurs ;
- imposer un historique linéaire.

`ci-ok` est le seul contrôle à exiger : il échoue dès qu'un autre job échoue, est annulé ou est sauté, y compris une case de la matrice.

## Seuil de couverture

Le seuil et la liste des composants suivis sont dans `pyproject.toml`, section `[tool.answerlineage.coverage_gate]`. Un composant listé dont aucun fichier n'est mesuré fait échouer la porte : une faute de frappe ne peut pas la désactiver.

## Chaîne d'approvisionnement

Le contrôle `make policy` (`scripts/check_workflows.py`) échoue dès qu'un workflow ou une action composite du dépôt :

- référence une action par balise ou par branche au lieu d'une empreinte de commit ;
- utilise une image de conteneur sans empreinte `@sha256:` ;
- ne déclare pas de permissions, ou accorde une écriture à tout le workflow ;
- se déclenche sur `pull_request_target` ;
- laisse `actions/checkout` conserver le jeton du pipeline.

Le jeton du pipeline est en lecture seule. Seul le job `codeql` reçoit `security-events: write`, pour déposer ses résultats.

Les dépendances attendent 7 jours avant d'être adoptées, à deux endroits : `exclude-newer` dans `pyproject.toml` pour `uv lock`, et `minimumReleaseAge` dans `renovate.json`. Avec `internalChecksFilter: strict`, Renovate n'ouvre aucune merge request tant qu'aucune version n'a passé le délai. Renovate maintient aussi les empreintes des actions et de l'image gitleaks.

## Réglages à faire dans GitHub

Ces réglages ne se versionnent pas. Ils reviennent à un administrateur du dépôt :

- installer l'application Renovate sur le dépôt, sans quoi `renovate.json` reste sans effet ;
- exiger les résultats de l'analyse de code dans la protection de `main`, pour qu'une alerte CodeQL de niveau élevé bloque la fusion ;
- ne pas activer la configuration par défaut de l'analyse de code : elle refuserait les résultats du job `codeql`.
