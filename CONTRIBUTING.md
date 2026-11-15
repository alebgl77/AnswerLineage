# Contribuer à AnswerLineage

Merci de l'intérêt que vous portez au projet. Ce guide dit comment proposer un changement et ce qui est attendu avant sa fusion.

Pour une faille de sécurité, n'ouvrez pas d'issue : suivez la [politique de sécurité](SECURITY.md).

Le projet suit un [code de conduite](CODE_OF_CONDUCT.md).

## Avant d'écrire du code

Ouvrez une issue pour tout changement qui dépasse une correction évidente : un nouveau réglage, une route, un format d'événement, un composant. Le périmètre et l'ordre des travaux sont tenus par le mainteneur ; un échange court évite un travail qui ne pourrait pas être fusionné.

## Mise en place

Prérequis : [uv](https://docs.astral.sh/uv/), Python 3.13 ou 3.14, Docker pour les tests d'intégration, et Node 22 pour l'explorateur.

```bash
make setup
```

Le dépôt fournit aussi un conteneur de développement (`.devcontainer/`), sans identifiant ni jeton, utilisable avec VS Code ou GitHub Codespaces.

## La porte

```bash
make check
```

Toute contribution passe `make check` : politique du dépôt, compatibilité de l'API, format, lint, typage strict, tests et couverture. Le pipeline rejoue la même porte sur Python 3.13 et 3.14, avec PostgreSQL 17 et 18. [`docs/ci.md`](docs/ci.md) décrit chaque contrôle.

| Commande | Usage |
| --- | --- |
| `make format` | Reformate et applique les corrections automatiques |
| `make test` | Tests seuls, avec le rapport de couverture |
| `uv run pytest -m "not integration"` | Tests sans PostgreSQL |
| `make openapi` | Met à jour le contrat OpenAPI publié, quand une route change |
| `make contract` | Compare l'API et les schémas de facettes à ceux de la branche cible : un changement incompatible échoue |
| `make ui` | Construit l'explorateur dans `ui/dist` |
| `make e2e` | Tests de l'explorateur dans Chromium, après `make ui` et `uv run playwright install chromium` |
| `make abuse` | Rejoue le corpus d'abus seul : chaque attaque doit être bloquée, un échec nomme le cas |
| `make image` | Construit l'image, produit son SBOM et l'analyse : une vulnérabilité haute ou critique bloque |

## Ce qui est attendu d'un changement

- **Les tests d'abord.** Un changement arrive avec les tests qui le justifient. La couverture tient 85 % par composant, en branches comme en lignes.
- **Un changement cohérent par commit**, avec un message au format des [commits conventionnels](https://www.conventionalcommits.org/fr/), en français : `feat(proxy): …`, `fix(api): …`, `docs: …`.
- **Pas de commit de correction dans une merge request.** Un défaut trouvé pendant la revue se corrige dans le commit qui l'a introduit, en réécrivant votre branche.
- **La documentation suit le code.** Un comportement visible se décrit dans `docs/`, et ses limites dans la section « Limites connues » du document concerné.
- **Le métier se dit en français** (rappel, piste, écart, criticité, quarantaine), les identifiants techniques en anglais.

## Les garanties à ne pas rompre

Un changement qui rompt l'une d'elles est refusé, même si ses tests passent :

1. Aucun contenu de prompt, de résultat d'outil ou de réponse n'est stocké, journalisé ni mis en métrique. Des empreintes seulement.
2. Aucun modèle dans AnswerLineage : tout résultat est déterministe.
3. La capture ne bloque ni ne retarde jamais un agent.
4. Le proxy ne termine pas OAuth et ne conserve aucun jeton.
5. PostgreSQL est la seule dépendance obligatoire.
6. Toute entrée est hostile : un identifiant reçu n'est recopié que s'il a une forme admise, et toute entrée est bornée.
7. Tout texte produit suit un gabarit fixe : aucun texte libre d'un émetteur n'atteint un modèle ou un destinataire.
8. Aucun rappel ne part sans validation humaine. Les destinataires sont pseudonymisés.

## Ce qui demande l'accord du mainteneur

- **Une nouvelle dépendance.** Elle se discute dans une issue. Une fois acceptée, elle s'ajoute au `pyproject.toml` du composant, puis `uv lock` ; pour l'explorateur, à `ui/package.json` en version exacte, puis `npm install --package-lock-only`. Une version publiée depuis moins de 7 jours n'est pas adoptée.
- **Les chemins protégés** : workflows, `scripts/`, `.devcontainer/`, configuration d'agents de code. Une merge request qui en modifie un échoue au contrôle `agent-config` tant que le mainteneur n'a pas posé le label `agent-config-approved`. Signalez-le dans la description plutôt que de contourner le contrôle.
- **Un changement incompatible de l'API.** Le pipeline le refuse : une rupture demande une nouvelle version de l'API.

Les actions GitHub s'épinglent par empreinte de commit et les images par empreinte `@sha256:`.

## Agents de code

Une contribution écrite avec un agent de code suit les mêmes règles et engage son auteur de la même façon. Le fichier `CLAUDE.md` donne à l'agent le contexte du dépôt.

## Licence des contributions

AnswerLineage est distribué sous la licence [Apache 2.0](LICENSE). En proposant un changement, vous acceptez qu'il le soit aussi.
