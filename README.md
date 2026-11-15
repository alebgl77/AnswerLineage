# AnswerLineage

Lignage et rappel des réponses d'agents : pour une donnée reconnue fausse, retrouver les réponses d'agents qui en dépendent et leurs destinataires, puis organiser leur correction.

Le projet démarre. Seul le socle du monorepo existe (story US-0101).

## Démarrer

Prérequis : [uv](https://docs.astral.sh/uv/) et Python 3.13.

```bash
make setup
```

```bash
make check
```

`make check` enchaîne format, lint (ruff), typage strict (mypy) et tests (pytest, couverture de branches). Un test isolé :

```bash
uv run pytest tests/test_workspace.py::test_mypy_checks_every_component
```

## Composants

| Dossier | Rôle |
| --- | --- |
| `proxy/` | Proxy MCP : relaie les appels d'outils et émet les événements de lignage |
| `adapters/` | Adaptateurs natifs des frameworks d'agents |
| `collector/` | Pont OpenTelemetry et journaux d'audit d'entrepôt |
| `api/` | Ingestion OpenLineage et API de lecture |
| `workers/` | Alimentation du graphe, réconciliation, pistes |
| `mcp-server/` | Serveur MCP d'AnswerLineage |
| `ui/` | Explorateur visuel |
| `deploy/` | docker compose et chart Helm |
| `corpus/` | Corpus de vérité et corpus d'abus |
| `spec/facets/` | Schémas JSON des facettes |
| `docs/` | PRD, ADR, runbooks |
