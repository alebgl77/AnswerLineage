# Schéma OpenLineage

`2-0-2/OpenLineage.json` est la copie exacte du schéma publié par OpenLineage 1.55.0 (`spec/OpenLineage.json`, commit `5ec09f180d0857327a58b85531b7447d069abf07`, licence Apache 2.0). C'est le contrat de l'API d'ingestion : les tests comparent la validation de l'API à ce schéma. Ne pas le modifier à la main.

`facets/1-1-0/SQLJobFacet.json` est la copie exacte, au même commit, du schéma de la facette de job standard `sql` (`spec/facets/SQLJobFacet.json`). Le proxy y écrit la forme sans littéraux d'une requête SQL ; les tests valident ce qu'il émet contre cette copie.

`facets/1-1-0/DataQualityAssertionsDatasetFacet.json` est la copie exacte, au même commit, du schéma de la facette d'entrée standard `dataQualityAssertions` (`spec/facets/DataQualityAssertionsDatasetFacet.json`). Les outils de tests de données, dbt par exemple, y listent leurs assertions et leur réussite ; les workers en lisent quatre champs pour [proposer un incident](../../docs/incidents.md#incidents-proposés-par-un-test-de-qualité). Les événements de tests de données que rejouent les tests du dépôt sont validés contre cette copie. L'ingestion ne la contrôle pas : la projection la lit avec tolérance.
