# 0009. Un dépôt, une publication par composant

Statut : en vigueur.

## Contexte

AnswerLineage tient en composants qui se déploient séparément : un proxy placé devant des serveurs MCP, une API, des workers, une commande, un kit de conformité, un corpus, un explorateur. Un proxy est installé chez celui qui capte, l'API et les workers chez celui qui héberge : il faut pouvoir installer l'un sans l'autre, sans tirer l'autre composant ni sa dépendance de base de données.

Mais ces composants se tiennent par des contrats : le proxy construit des événements que l'API valide, l'API et le proxy contrôlent tous deux l'hôte et l'origine, la forme des identifiants s'écrit des deux côtés. Un changement d'un côté doit se voir de l'autre avant d'être fusionné.

## Décision

Un seul dépôt et un seul espace de travail uv, où chaque composant est un membre : ses sources (`<composant>/src/answerlineage_<composant>/`), ses tests (`<composant>/tests/`), son `pyproject.toml` et son paquet. Les tests qui croisent plusieurs composants sont dans `tests/`.

Chaque composant est publié comme son propre paquet : `answerlineage-proxy`, `answerlineage-api`, `answerlineage-workers`, `answerlineage-cli`, `answerlineage-corpus`, `answerlineage-conformance` sur PyPI ([Publication](../publication.md)), et l'image de la démonstration. Une version sort d'une étiquette `v1.2.3` posée par le mainteneur, et tous les paquets sortent à la même version : les composants frères se référencent par une version exacte (`answerlineage-api==…` dans `workers/`, `answerlineage-workers==…` dans `conformance/`), et `make packages` vérifie l'accord sur le poste comme dans le pipeline.

Les dépendances entre composants sont rares et dirigées : `workers` dépend de `api` (le schéma et l'écriture du graphe), `conformance` de `workers`. Le proxy, le corpus et la commande ne dépendent d'aucun autre ; la commande n'a que `httpx`.

## Alternative écartée

**Un dépôt par composant.**

Chaque dépôt aurait eu son cycle, sa version et son pipeline. Mais les contrats entre composants se seraient alors tenus par la discipline plutôt que par un test : un changement du format d'événement du proxy et de la validation de l'API demanderait deux merge requests dans deux dépôts, fusionnées dans le bon ordre, avec une fenêtre où les deux ne s'accordent pas. Les doublons voulus (le contrôle de l'hôte, la forme des identifiants) et les accords implicites (la façon dont l'entreprise synthétique décrit un appel d'outil, qui doit être celle du proxy) n'y auraient aucun test qui les tienne.

Dans un dépôt, `tests/test_contract_guard.py`, `tests/test_contract_identifiers.py`, `tests/test_contract_proxy_ingestion.py` et `tests/test_contract_corpus.py` échouent dans la merge request qui fait diverger deux composants. Une seule porte (`make check`), un seul verrou de dépendances, un seul pipeline.

## Conséquences

- Du code est volontairement écrit deux fois (le contrôle de l'hôte et de l'origine dans le proxy et dans l'API, la forme des identifiants, qu'un fichier de règles du proxy doit respecter), pour qu'aucun service ne dépende de l'autre. Une règle changée d'un côté doit l'être de l'autre : les tests de contrat l'imposent.
- Les composants se versionnent ensemble. Un composant ne peut pas sortir seul, et un correctif du proxy publie aussi de nouveaux paquets des autres.
- Ajouter un composant demande de l'enregistrer à six endroits du `pyproject.toml` racine : membres de l'espace de travail, `src` de ruff, `mypy_path` et `files` de mypy, `testpaths` de pytest, `source` de coverage, chemins de la porte de couverture.
- La couverture de 85 % se tient par composant, en branches comme en lignes et branches : un composant bien testé ne rachète pas un autre.
- Les noms de distribution sont à réserver : `answerlineage` sur PyPI et sur npm, et `answerlineage-ui` (l'explorateur est privé) n'ont aucun job qui les publie ([Publication](../publication.md#ce-que-publie-une-version)).
- L'explorateur, écrit en TypeScript, est dans le même dépôt mais hors de l'espace de travail Python : il se construit et se teste à part (`make ui`, `make e2e`) et l'API le sert.
