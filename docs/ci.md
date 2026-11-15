# Pipeline d'intégration continue

Le pipeline est décrit dans `.github/workflows/ci.yml`. Il tourne sur chaque merge request et sur chaque fusion dans `main`.

| Job | Ce qu'il vérifie | Commande locale |
| --- | --- | --- |
| `static` | Format, lint (ruff), typage strict (mypy) | `make format-check lint typecheck` |
| `tests` | Tests et couverture de branches, sur la matrice Python 3.13 et 3.14, PostgreSQL 17 et 18 | `make test` |
| `tests` | Porte de couverture : 85 % par composant, en branches et en lignes et branches (ENF-MAIN-01) | `make coverage-gate` |
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
