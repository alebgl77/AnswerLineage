.DEFAULT_GOAL := help
.PHONY: help setup check format format-check lint typecheck test

help: ## Liste les cibles
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

setup: ## Installe l'environnement (uv) et les hooks pre-commit
	uv sync --all-packages --locked
	uv run pre-commit install

check: format-check lint typecheck test ## Porte de toute story : format, lint, typage strict, tests

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
	uv run pytest --cov --cov-report=term-missing
