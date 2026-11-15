.DEFAULT_GOAL := help
.PHONY: help setup check policy contract openapi format format-check lint typecheck test coverage-gate abuse secrets image ui e2e demo

help: ## Liste les cibles
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

setup: ## Installe l'environnement (uv) et les hooks pre-commit
	uv sync --all-packages --locked
	uv run pre-commit install

check: policy contract format-check lint typecheck test coverage-gate ## Porte de toute contribution : politique, contrat de l'API, format, lint, typage strict, tests, couverture

policy: ## Politique du dépôt (actions et images épinglées, permissions minimales, agents de code)
	uv run python -m scripts.check_policy

contract: ## Compatibilité de l'API et des schémas de facettes avec ceux publiés sur la branche cible
	uv run python -m scripts.check_openapi_compat
	uv run python -m scripts.check_facets_compat

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

abuse: ## Rejoue le corpus d'abus seul : chaque cas d'attaque doit être bloqué (Docker requis)
	uv run answerlineage-corpus check
	uv run pytest tests/test_abuse_corpus.py

secrets: ## Détection de secrets sur tout l'historique (gitleaks, Docker requis)
	scripts/gitleaks.sh selftest
	scripts/gitleaks.sh scan

image: ## Construit l'image, produit son SBOM et l'analyse : hautes et critiques bloquantes (Docker requis)
	scripts/image.sh

ui: ## Construit l'explorateur dans ui/dist (Node requis)
	npm ci --prefix ui --ignore-scripts --no-audit --no-fund
	npm run --prefix ui typecheck
	npm run --prefix ui build

e2e: ## Tests de bout en bout de l'explorateur dans Chromium (après make ui)
	uv run pytest -m e2e

demo: ## Lance la démonstration : l'explorateur sur http://127.0.0.1:8000/ (Docker requis)
	docker compose up --build
