# Pipeline d'intégration continue

Le pipeline est décrit dans `.github/workflows/ci.yml`. Il tourne sur chaque merge request et sur chaque fusion dans `main`.

| Job | Ce qu'il vérifie | Commande locale |
| --- | --- | --- |
| `static` | Politique du dépôt, compatibilité de l'API et des schémas de facettes avec ceux publiés, format, lint (ruff), typage strict (mypy) ; publication d'une version éprouvée sans rien publier : paquets de chaque composant et leurs métadonnées, étiquette fictive de la version déclarée, notes de version | `make policy contract format-check lint typecheck packages` |
| `tests` | Tests et couverture de branches, sur la matrice Python 3.13 et 3.14, PostgreSQL 17 et 18 | `make test` |
| `tests` | Porte de couverture : 85 % par composant, en branches et en lignes et branches | `make coverage-gate` |
| `tests` | Porte de qualité du moteur d'impact : sur le [corpus de vérité](corpus.md#mesure-du-moteur-dimpact), un rappel au niveau table d'au moins 0,99 et aucune réponse hors d'atteinte ; un échec nomme les réponses manquées | `make measure` |
| `tests`, `explorer` | Corpus d'abus : chaque cas d'attaque est rejoué et doit être bloqué. Détail plus bas | `make abuse` |
| `secrets` | Aucun secret dans tout l'historique (gitleaks), après un canari qui prouve que le détecteur bloque. Une trouvaille est affichée avec son fichier, sa ligne et sa règle, valeur masquée ; une exception se déclare dans `.gitleaksignore`, chemin protégé | `make secrets` |
| `image` | L'image de `deploy/Dockerfile` se construit ; son SBOM est produit (Syft, CycloneDX) et gardé comme artefact du job ; Grype l'analyse, après un canari qui prouve que l'analyse bloque. Une vulnérabilité haute ou critique sans exception valable fait échouer le job. La signature est éprouvée sans rien publier : l'image signée se vérifie, une image modifiée sous la même étiquette ne passe pas. Détail plus bas | `make image` |
| `codeql` | Analyse de sécurité du code Python et des workflows (CodeQL, requêtes étendues) | |
| `agent-config` | Une merge request qui modifie la configuration d'agents ou un garde-fou porte l'approbation d'un mainteneur | |
| `devcontainer` | Le conteneur de développement se construit, installe le projet et passe les tests | |
| `explorer` | L'explorateur se construit, ses types sont justes, et ses tests de bout en bout passent dans Chromium | `make ui e2e` |
| `demo` | `docker compose up` donne un premier graphe en moins de cinq minutes, sur la boucle locale seulement ; les parcours Remonter et Propager passent dans Chromium contre la démonstration lancée ; après une relance sans renvoi de l'entreprise, les données viennent du volume | `scripts/check_demo.sh` |
| `ci-ok` | Toutes les étapes bloquantes sont vertes | |

`make check` enchaîne sur le poste les contrôles des jobs `static` et `tests`.

Les tests marqués `e2e` pilotent l'explorateur dans Chromium. Ils demandent l'interface construite (`make ui`, avec Node 22) et le navigateur (`uv run playwright install chromium`). Sur le poste, ils sont sautés quand l'un des deux manque ; en CI, ils échouent.

Les tests marqués `demo` jouent les parcours Remonter et Propager dans Chromium contre la démonstration lancée, dont l'adresse est donnée par `ANSWERLINEAGE_DEMO_URL`. Sans elle, ils sont sautés ; `scripts/check_demo.sh browser` la fournit toujours, et le job `demo` les joue avant d'arrêter la démonstration. Le détail est dans [Démonstration](demonstration.md#rejouer-la-vérification-sur-le-poste).

## Rejouer une case de la matrice sur le poste

Les tests d'intégration démarrent PostgreSQL avec Testcontainers : Docker doit tourner. L'image se choisit par variable d'environnement, la version de Python par `UV_PYTHON`, dans un environnement séparé pour ne pas toucher à `.venv` :

```bash
ANSWERLINEAGE_TEST_POSTGRES_IMAGE=postgres:17 UV_PYTHON=3.14 UV_PROJECT_ENVIRONMENT=.venv-314 uv run --all-packages --locked pytest
```

Un serveur PostgreSQL déjà disponible s'utilise à la place avec `ANSWERLINEAGE_TEST_DATABASE_URL`. Sans Docker ni serveur, les tests d'intégration sont sautés sur le poste ; en CI, ils échouent.

## Corpus d'abus

Le [corpus d'abus](corpus.md#corpus-dabus) tient des cas d'attaque : injections dans les identifiants, SQL pathologique, lots surdimensionnés, caractères invisibles, usurpation d'émetteur, écart entre en-têtes et corps, requêtes qui sortiraient du lignage. Une régression de sécurité bloque ainsi la fusion.

Il n'a pas de job à lui. Ses cas sont des tests ordinaires, que `ci-ok` exige déjà :

| Job | Tests | Ce qui est joué |
| --- | --- | --- |
| `tests`, sur les quatre cases de la matrice | `tests/test_abuse_corpus.py` | Les cas qui visent le proxy, l'ingestion et la lecture, devant le vrai proxy, la vraie API, un vrai PostgreSQL et un worker |
| `explorer` | `tests/test_abuse_explorer.py` | Les adresses hostiles, ouvertes dans Chromium devant l'explorateur construit |
| `tests` | `corpus/tests/test_abuse.py` | Le contrôle du corpus lui-même : format, listes fermées, familles exigées, version |

**Lire un échec.** Chaque cas est un test, dont l'identifiant est le nom du cas. Son message commence par ce nom, dit l'attendu et l'obtenu, et ne cite jamais la valeur hostile :

```
FAILED tests/test_abuse_corpus.py::test_every_case_of_the_abuse_corpus_is_blocked[lots/lot-1001]
lots/lot-1001 : 413 batch_too_large attendu, 202 obtenu
```

Le cas se rejoue seul avec son identifiant, puis se lit dans le fichier de sa famille.

**Cas ouverts.** Un cas que le produit ne bloque pas encore est un échec attendu : il apparaît à chaque exécution dans le résumé des tests (`XFAIL`), avec ce qui a été obtenu et son motif, et ne fait pas échouer le job. S'il se trouve bloqué, son test échoue tant que son exception n'est pas retirée de `abuse_open.json`. Les cas ouverts et leurs motifs sont listés dans [Cas ouverts](corpus.md#cas-ouverts).

**Rejouer sur le poste.** `make abuse` contrôle le corpus puis rejoue tous ses cas ; ceux de l'explorateur sont sautés tant que l'interface n'est pas construite (`make ui`).

## Image : SBOM, vulnérabilités et signature

Le job `image` et la publication d'une version (`.github/workflows/release.yml`) enchaînent les mêmes sous-commandes de `scripts/image.sh`. La merge request éprouve donc tout ce que la publication fera, sauf pousser l'image, la signer sans clé et lui attacher ses attestations ; la signature s'y éprouve avec un registre éphémère et des clés jetables.

| Sous-commande | Ce qu'elle fait |
| --- | --- |
| `build` | Construit l'image de `deploy/Dockerfile` et l'exporte en archive (`docker save`) |
| `sbom` | Syft inventorie les composants de l'archive : `build/image/sbom.cdx.json` |
| `selftest` | Canari : un SBOM écrit à la main qui déclare PyYAML 5.3 (CVE-2020-14343, critique) doit faire échouer l'analyse |
| `signtest` | Signe l'image dans un registre éphémère avec une clé jetable et la vérifie ; une image modifiée sous la même étiquette, puis signée par une autre clé, doit être refusée. Détail dans [Publication](publication.md#la-signature-éprouvée-à-chaque-merge-request) |
| `scan` | Grype analyse le SBOM ; `scripts/check_vulnerabilities.py` rend le verdict |

**Format du SBOM.** CycloneDX en JSON : un format standard pensé pour l'analyse de sécurité, où chaque composant porte son identifiant de paquet (purl). Grype le lit sans perte, l'attestation de GitHub l'accepte, et un SBOM s'y écrit à la main en quelques lignes, comme celui du canari. Grype analyse ce SBOM, pas l'image : ce qui est analysé est exactement ce qui est publié, et quiconque tire l'image peut rejouer l'analyse sur son SBOM.

**Outils.** Syft et Grype tournent depuis leurs images officielles, épinglées par empreinte dans `scripts/image.sh` et tenues à jour par Renovate. Ils n'ont ni le socket Docker ni aucun privilège : l'image leur est passée en archive, montée en lecture seule. Syft tourne sans réseau. Grype télécharge sa base de vulnérabilités à chaque exécution : une vulnérabilité publiée hier fait échouer la merge request d'aujourd'hui, même si rien n'a changé dans le dépôt.

**La règle.** Une vulnérabilité de gravité haute (`High`) ou critique (`Critical`) sans exception valable fait échouer le job. Les autres gravités sont comptées dans le journal, sans bloquer.

**Lire une trouvaille.** Le journal de l'étape d'analyse, et le résumé du job, donnent une ligne par vulnérabilité haute ou critique :

```
  bloquante  High      libexemple 1.2.3-r0 (apk)  CVE-2025-0001  corrigée dans : 1.2.4-r0
```

soit son statut (bloquante, ou sous exception avec sa date de revue et son motif), sa gravité, le paquet, sa version et son type (`apk` pour un paquet Alpine de l'image de base, `python` pour une dépendance Python…), l'identifiant de la vulnérabilité et ses alias, et la version qui la corrige. Le rapport complet de Grype est dans `build/image/grype.json`. Une version corrigée existe : mettre à jour la dépendance dans le verrou, ou l'empreinte de l'image de base de `deploy/Dockerfile`. Aucune n'existe : déclarer une exception.

**Déclarer une exception.** Dans `vulnerability-exceptions.toml`, à la racine, une entrée par vulnérabilité :

```toml
[[exception]]
vulnerability = "CVE-2025-0001"   # l'identifiant affiché, ou l'un de ses alias
package = "libexemple"            # facultatif : restreint l'exception à ce paquet
reason = "La fonction vulnérable n'est jamais appelée par les services de l'image."
review-by = 2026-12-31
```

Le motif dit pourquoi l'image n'est pas exposée, ou ce qui est attendu pour la corriger. Le contrôle refuse une exception sans motif, dont la date de revue est passée, qui nomme un motif d'identifiant plutôt qu'une vulnérabilité, ou qui porte une clé inconnue : aucune règle globale (« sans correctif », une gravité entière…) ne peut écarter une vulnérabilité sans examen. Une exception refusée ne s'applique plus : sa vulnérabilité redevient bloquante. Une exception qui ne correspond plus à rien est signalée, à retirer.

Ce fichier est un chemin protégé : le modifier demande la revue d'un mainteneur (`.github/CODEOWNERS`) et le label `agent-config-approved`. C'est le mainteneur qui valide une exception.

**Rejouer sur le poste.** Docker suffit, avec Python 3.12 ou plus :

```bash
make image                   # ou scripts/image.sh : build, sbom, selftest, signtest, scan
scripts/image.sh scan        # rejoue l'analyse du dernier SBOM, avec la base du jour
python3 -m scripts.check_vulnerabilities exceptions   # vérifie les exceptions seules
```

## Publication d'une version

Le mainteneur publie une version en posant une étiquette `v1.2.3` (ou `v1.2.3-rc.1`). Le workflow `release` :

1. vérifie que l'étiquette est la version déclarée par chaque composant publiable, construit leurs paquets, contrôle leurs métadonnées et rédige les notes de version, avant de rien publier ;
2. construit l'image, produit son SBOM, joue le canari et l'analyse, comme le job `image` : une vulnérabilité haute ou critique sans exception valable arrête la publication avant que l'image soit poussée ;
3. pousse l'image analysée dans le registre de conteneurs de GitHub, `ghcr.io/alebgl77/answerlineage:1.2.3`, la signe sans clé par son empreinte (cosign, identité OIDC du workflow, journal de transparence public), vérifie la signature publiée, et lui attache son SBOM et sa provenance (SLSA) ;
4. atteste la provenance des paquets et les publie sur PyPI par publication de confiance, avec leurs attestations de publication ;
5. crée la version GitHub, avec ses notes, les paquets, le SBOM et l'empreinte de chaque fichier ;
6. dit dans tous les cas, dans le résumé de l'exécution, ce qui est publié et ce qui ne l'est pas.

Il ne tourne jamais sur une merge request et ne lit aucun secret : il publie par l'identité OIDC du workflow ou par son jeton éphémère, avec les seules permissions de chaque job.

Comment sort une version, comment se reprend une publication interrompue, ce que le mainteneur règle une fois, et comment vérifier une image ou un paquet publiés : voir [Publication](publication.md).

## Seuil de couverture

Le seuil et la liste des chemins suivis sont dans `pyproject.toml`, section `[tool.answerlineage.coverage_gate]`. Un chemin listé dont aucun fichier n'est mesuré fait échouer la porte : une faute de frappe ne peut pas la désactiver.

## Banc de latence du proxy

Le budget de performance du proxy, un p99 de la latence ajoutée à un appel d'outil d'au plus 5 ms, se vérifie chaque nuit, et à la demande, dans un workflow à lui : `.github/workflows/bench.yml`. Son job lance `make bench` : 100 000 appels directs et autant à travers le proxy, sur une machine qui ne fait que cela. Le banc et ses deux mesures, par comparaison et par le proxy lui-même, sont décrits avec [le proxy](proxy.md#le-banc-de-latence).

Le workflow n'a que le droit de lire le dépôt et n'utilise aucun secret. Il ne bloque pas une merge request : un seuil de temps n'a de sens que sur une machine au repos. Dans `make check`, et donc dans le job `tests`, un banc de 2 000 appels vérifie seulement qu'il fonctionne et que sa mesure interne est cohérente.

## Chaîne d'approvisionnement

Le contrôle `make policy` (`scripts/check_policy.py`) échoue dès que :

- une action est référencée par balise ou par branche au lieu d'une empreinte de commit ;
- une image de conteneur est utilisée sans empreinte `@sha256:`, dans un workflow, un script ou un Dockerfile ;
- un workflow ne déclare pas de permissions, ou accorde une écriture à tout le workflow ;
- un workflow se déclenche sur `pull_request_target` ;
- `actions/checkout` conserve le jeton du pipeline ;
- un job qui publie (permission `packages` ou `id-token` en écriture, envoi sur PyPI) reçoit un secret ou un mot de passe : il publie par l'identité OIDC du workflow ou par son jeton éphémère, jamais par un jeton de longue durée ;
- un fichier compose publie un port ailleurs que sur la boucle locale, branche un service sur le réseau de l'hôte (directement ou par un réseau externe), tire une image sans empreinte, ou inclut ou étend un fichier absent du dépôt. Tout fichier YAML qui décrit des services compose est contrôlé, quel que soit son nom, fichiers de surcharge compris ;
- le job `ci-ok` ne dépend pas de tous les autres jobs.

Le jeton du pipeline est en lecture seule. Seul le job `codeql` reçoit `security-events: write`, pour déposer ses résultats. Hors du pipeline des merge requests, le workflow `release` reçoit les permissions de publier, job par job, chacun les seules dont il a besoin.

Les dépendances attendent 7 jours avant d'être adoptées, à deux endroits : `exclude-newer` dans `pyproject.toml` pour `uv lock`, et `minimumReleaseAge` dans `renovate.json`. Avec `internalChecksFilter: strict`, Renovate n'ouvre aucune merge request tant qu'aucune version n'a passé le délai. Renovate maintient aussi les empreintes des actions et des images des outils : gitleaks, Syft, Grype, cosign et le registre éphémère de la signature éprouvée.

## Agents de code

Une issue, un commentaire ou une merge request peuvent porter des instructions piégées. Trois garde-fous en tiennent compte.

**Aucun agent de code sur une entrée non fiable avec un secret ou un droit d'écriture.** `make policy` refuse un workflow déclenché par une issue, un commentaire, une discussion ou une merge request qui lance un agent de code (action ou ligne de commande) en lui donnant un secret ou une permission d'écriture.

**Revue obligatoire de la configuration d'agents et des garde-fous.** Les fichiers qui pilotent ce qu'un agent a le droit de faire sont protégés : `.claude/`, `.mcp.json`, `CLAUDE.md` et leurs équivalents pour d'autres agents, ainsi que `.github/workflows/`, `.github/CODEOWNERS`, `.devcontainer/`, `.pre-commit-config.yaml`, `renovate.json`, `scripts/`, les exceptions des analyses (`.gitleaksignore`, `vulnerability-exceptions.toml`) et celles du corpus d'abus (`corpus/src/answerlineage_corpus/abuse_open.json`). Deux mécanismes se complètent :

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
- installer l'application Renovate, sans quoi `renovate.json` reste sans effet ;
- protéger les étiquettes `v*`, pour que seul un mainteneur publie une version ;
- après la première publication, rendre public le paquet `answerlineage` du registre de conteneurs (Packages, Package settings), pour que l'image et son SBOM se tirent sans authentification ;
- réserver les noms de distribution, créer l'environnement `pypi` et déclarer les éditeurs de confiance de PyPI : voir [Ce que le mainteneur règle une fois](publication.md#ce-que-le-mainteneur-règle-une-fois).

`ci-ok` est le seul contrôle à exiger : il échoue dès qu'un autre job échoue, est annulé ou est sauté, y compris une case de la matrice.
