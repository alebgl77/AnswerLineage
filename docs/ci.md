# Pipeline d'intégration continue

Le pipeline est décrit dans `.github/workflows/ci.yml`. Il tourne sur chaque merge request et sur chaque fusion dans `main`.

| Job | Ce qu'il vérifie | Commande locale |
| --- | --- | --- |
| `static` | Politique des workflows, format, lint (ruff), typage strict (mypy) | `make policy format-check lint typecheck` |
| `tests` | Tests et couverture de branches, sur la matrice Python 3.13 et 3.14, PostgreSQL 17 et 18 | `make test` |
| `tests` | Porte de couverture : 85 % par composant, en branches et en lignes et branches | `make coverage-gate` |
| `secrets` | Aucun secret dans tout l'historique (gitleaks), après un canari qui prouve que le détecteur bloque | `make secrets` |
| `codeql` | Analyse de sécurité du code Python et des workflows (CodeQL, requêtes étendues) | |
| `agent-config` | Une merge request qui modifie la configuration d'agents ou un garde-fou porte l'approbation d'un mainteneur | |
| `devcontainer` | Le conteneur de développement se construit, installe le projet et passe les tests | |
| `ci-ok` | Toutes les étapes bloquantes sont vertes | |

`make check` enchaîne sur le poste les contrôles des jobs `static` et `tests`.

## Rejouer une case de la matrice sur le poste

Les tests d'intégration démarrent PostgreSQL avec Testcontainers : Docker doit tourner. L'image se choisit par variable d'environnement, la version de Python par `UV_PYTHON`, dans un environnement séparé pour ne pas toucher à `.venv` :

```bash
ANSWERLINEAGE_TEST_POSTGRES_IMAGE=postgres:17 UV_PYTHON=3.14 UV_PROJECT_ENVIRONMENT=.venv-314 uv run --all-packages --locked pytest
```

Un serveur PostgreSQL déjà disponible s'utilise à la place avec `ANSWERLINEAGE_TEST_DATABASE_URL`. Sans Docker ni serveur, les tests d'intégration sont sautés sur le poste ; en CI, ils échouent.

## Seuil de couverture

Le seuil et la liste des chemins suivis sont dans `pyproject.toml`, section `[tool.answerlineage.coverage_gate]`. Un chemin listé dont aucun fichier n'est mesuré fait échouer la porte : une faute de frappe ne peut pas la désactiver.

## Chaîne d'approvisionnement

Le contrôle `make policy` (`scripts/check_policy.py`) échoue dès que :

- une action est référencée par balise ou par branche au lieu d'une empreinte de commit ;
- une image de conteneur est utilisée sans empreinte `@sha256:`, dans un workflow, un script ou un Dockerfile ;
- un workflow ne déclare pas de permissions, ou accorde une écriture à tout le workflow ;
- un workflow se déclenche sur `pull_request_target` ;
- `actions/checkout` conserve le jeton du pipeline ;
- le job `ci-ok` ne dépend pas de tous les autres jobs.

Le jeton du pipeline est en lecture seule. Seul le job `codeql` reçoit `security-events: write`, pour déposer ses résultats.

Les dépendances attendent 7 jours avant d'être adoptées, à deux endroits : `exclude-newer` dans `pyproject.toml` pour `uv lock`, et `minimumReleaseAge` dans `renovate.json`. Avec `internalChecksFilter: strict`, Renovate n'ouvre aucune merge request tant qu'aucune version n'a passé le délai. Renovate maintient aussi les empreintes des actions et de l'image gitleaks.

## Agents de code

Une issue, un commentaire ou une merge request peuvent porter des instructions piégées. Trois garde-fous en tiennent compte.

**Aucun agent de code sur une entrée non fiable avec un secret ou un droit d'écriture.** `make policy` refuse un workflow déclenché par une issue, un commentaire, une discussion ou une merge request qui lance un agent de code (action ou ligne de commande) en lui donnant un secret ou une permission d'écriture.

**Revue obligatoire de la configuration d'agents et des garde-fous.** Les fichiers qui pilotent ce qu'un agent a le droit de faire sont protégés : `.claude/`, `.mcp.json`, `CLAUDE.md` et leurs équivalents pour d'autres agents, ainsi que `.github/workflows/`, `.github/CODEOWNERS`, `.devcontainer/`, `.pre-commit-config.yaml`, `renovate.json` et `scripts/`. Deux mécanismes se complètent :

- `.github/CODEOWNERS` leur donne un propriétaire, dont la revue peut être exigée ; `make policy` vérifie que chaque chemin protégé y figure ;
- le job `agent-config` échoue quand une merge request modifie l'un d'eux, jusqu'à ce qu'un mainteneur lui pose le label `agent-config-approved`.

Ce job lit le workflow et le script de la merge request elle-même : il signale une modification, il ne remplace pas la revue. C'est la revue exigée des propriétaires du code qui fait foi.

**Un conteneur de développement sans identifiant.** `.devcontainer/` fournit un environnement prêt à l'emploi, sans jeton de publication ni identifiant de l'hôte : aucun montage, et les variables de jetons usuelles sont vidées. `make policy` refuse un montage d'un emplacement d'identifiants (`.ssh`, `.aws`, `.config/gh`, `docker.sock`…) et la transmission d'un jeton de l'hôte. Le conteneur n'embarque pas Docker : les tests d'intégration y sont sautés et tournent en CI.

VS Code relaie par défaut au conteneur l'agent SSH et l'assistant d'identification Git de l'hôte. Ce réglage est celui du poste, pas du dépôt : mettre `dev.containers.gitCredentialHelperConfigLocation` à `none` pour ne rien relayer.

## Réglages du dépôt

Ces réglages ne se versionnent pas. Ils reviennent à un administrateur du dépôt :

- protéger `main` : merge request obligatoire, contrôle `ci-ok` exigé sur une branche à jour, historique linéaire, sans contournement pour les administrateurs ;
- exiger la revue des propriétaires du code, pour que `.github/CODEOWNERS` ait un effet ;
- créer le label `agent-config-approved`, que seul un mainteneur pose ;
- exiger les résultats de l'analyse de code, pour qu'une alerte CodeQL de niveau élevé bloque la fusion ;
- ne pas activer la configuration par défaut de l'analyse de code, qui refuserait les résultats du job `codeql` ;
- installer l'application Renovate, sans quoi `renovate.json` reste sans effet.

`ci-ok` est le seul contrôle à exiger : il échoue dès qu'un autre job échoue, est annulé ou est sauté, y compris une case de la matrice.
