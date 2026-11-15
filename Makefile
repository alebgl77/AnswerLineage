.DEFAULT_GOAL := help
.PHONY: help setup check policy contract openapi format format-check lint typecheck test coverage-gate secrets

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
	uv run pytest --cov --cov-report=term-missing --cov-report=json

coverage-gate: ## Vérifie le seuil de couverture par composant (après make test)
	uv run python -m scripts.coverage_gate

secrets: ## Détection de secrets sur tout l'historique (gitleaks, Docker requis)
	scripts/gitleaks.sh selftest
	scripts/gitleaks.sh scan
