.DEFAULT_GOAL := help
.PHONY: help setup check policy contract openapi format format-check lint typecheck test coverage-gate secrets ui e2e

help: ## Liste les cibles
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

setup: ## Installe l'environnement (uv) et les hooks pre-commit
	uv sync --all-packages --locked
	uv run pre-commit install

check: policy contract format-check lint typecheck test coverage-gate ## Porte de toute contribution : politique, contrat de l'API, format, lint, typage strict, tests, couverture

policy: ## Politique du dépôt (actions et images épinglées, permissions minimales, agents de code)
	uv run python -m scripts.check_policy

contract: ## Compatibilité de l'API avec le contrat publié sur la branche cible
	uv run python -m scripts.check_openapi_compat

openapi: ## Met à jour la spécification OpenAPI publiée
	uv run python -m answerlineage_api.openapi > spec/openapi/answerlineage-v1.json

format: ## Reformate le code
	uv run ruff format .
	uv run ruff check --fix .

format-check:
	uv run ruff format --check .

lint:
	uv run ruff check .

typecheck:
	uv run mypy

test: ## Lance les tests avec la couverture de branches
	uv run pytest $(PYTEST_ARGS) --cov --cov-report=term-missing --cov-report=json

coverage-gate: ## Vérifie le seuil de couverture par composant (après make test)
	uv run python -m scripts.coverage_gate

secrets: ## Détection de secrets sur tout l'historique (gitleaks, Docker requis)
	scripts/gitleaks.sh selftest
	scripts/gitleaks.sh scan

ui: ## Construit l'explorateur dans ui/dist (Node requis)
	npm ci --prefix ui --ignore-scripts --no-audit --no-fund
	npm run --prefix ui typecheck
	npm run --prefix ui build

e2e: ## Tests de bout en bout de l'explorateur dans Chromium (après make ui)
	uv run pytest -m e2e
