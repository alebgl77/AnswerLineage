# Publication

Ce document dit ce que chaque version d'AnswerLineage publie et comment le vérifier avant de l'installer ; puis, pour le mainteneur, comment une version sort et ce qu'il règle une fois pour toutes.

## Ce que publie une version

Une version sort quand le mainteneur pose une étiquette `v1.2.3` ; le workflow `release` (`.github/workflows/release.yml`) construit alors les artefacts, les publie, les signe et les atteste.

| Artefact | Où | Ce qui le prouve |
| --- | --- | --- |
| Image `ghcr.io/alebgl77/answerlineage:1.2.3` | Registre de conteneurs de GitHub | Signature cosign, par l'empreinte de l'image ; attestation de provenance SLSA ; SBOM CycloneDX attesté |
| Paquets Python des composants, roues et sources | PyPI | Attestation de provenance SLSA ; attestation de publication de PyPI (PEP 740) |
| Version GitHub `v1.2.3` : notes de version, paquets, SBOM | Page des versions du dépôt | `SHA256SUMS`, l'empreinte de chaque fichier joint |

Toutes ces preuves portent la même identité : le workflow `release` de ce dépôt, lancé par l'étiquette de la version. Elles sont faites sans clé : le workflow obtient de GitHub un jeton OIDC, Fulcio, l'autorité de Sigstore, lui délivre en échange un certificat de quelques minutes au nom de cette identité, et la signature est inscrite au journal de transparence public de Sigstore (Rekor). PyPI accepte les paquets sur la même identité, par publication de confiance. Aucune clé de signature ni aucun jeton de publication n'existe, ni dans le dépôt ni dans ses secrets.

L'image est signée par son empreinte (`sha256:…`), jamais par son étiquette : une étiquette peut être déplacée vers une autre image, pas une empreinte.

## Vérifier une image

Il faut [cosign](https://docs.sigstore.dev/cosign/system_config/installation/) 3 ou plus, qui lit la signature au format de Sigstore où elle est rangée, la [ligne de commande GitHub](https://cli.github.com/) (`gh`, connectée à un compte GitHub) et `jq`.

```bash
VERSION=1.2.3
IMAGE=ghcr.io/alebgl77/answerlineage
IDENTITY=https://github.com/alebgl77/AnswerLineage/.github/workflows/release.yml@refs/tags/v$VERSION

# La signature : faite par le workflow de publication de ce dépôt, pour cette étiquette.
# cosign résout l'étiquette, vérifie l'image qu'elle désigne et en rend l'empreinte.
DIGEST=$(cosign verify "$IMAGE:$VERSION" \
  --certificate-identity "$IDENTITY" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  | jq -r '.[0].critical.image."docker-manifest-digest"')

# La provenance (SLSA), attestée par le même workflow, sur un runner de GitHub.
gh attestation verify "oci://$IMAGE@$DIGEST" --repo alebgl77/AnswerLineage \
  --cert-identity "$IDENTITY" --deny-self-hosted-runners

# Le SBOM, attesté de même ; la seconde commande l'écrit dans sbom.cdx.json.
gh attestation verify "oci://$IMAGE@$DIGEST" --repo alebgl77/AnswerLineage \
  --cert-identity "$IDENTITY" --predicate-type https://cyclonedx.org/bom
gh attestation verify "oci://$IMAGE@$DIGEST" --repo alebgl77/AnswerLineage \
  --cert-identity "$IDENTITY" --predicate-type https://cyclonedx.org/bom \
  --format json --jq '.[0].verificationResult.statement.predicate' > sbom.cdx.json

# L'image vérifiée se tire par son empreinte, jamais par son étiquette.
docker pull "$IMAGE@$DIGEST"
```

Une vérification réussie se termine sans erreur ; un échec s'arrête avec un message et un code de sortie non nul, et l'image ne doit pas être installée.

**Imposer la vérification à l'installation.** Dans un cluster Kubernetes, un contrôleur d'admission (le `policy-controller` de Sigstore, Kyverno…) refuse un pod dont l'image n'a pas de signature valable ; il se règle avec la même identité (`$IDENTITY`, ou l'expression régulière `^https://github\.com/alebgl77/AnswerLineage/\.github/workflows/release\.yml@refs/tags/v`) et le même émetteur (`https://token.actions.githubusercontent.com`).

## Vérifier un paquet

pip ne vérifie pas d'attestation : un paquet se télécharge, se vérifie, puis s'installe depuis les fichiers vérifiés, sans retourner au réseau.

```bash
VERSION=1.2.3
IDENTITY=https://github.com/alebgl77/AnswerLineage/.github/workflows/release.yml@refs/tags/v$VERSION

# Le paquet et ses dépendances, téléchargés sans être installés.
pip download --dest=paquets "answerlineage-api==$VERSION"

# Chaque fichier du projet : attesté par le workflow de publication, sur un runner de GitHub…
for fichier in paquets/answerlineage_*; do
  gh attestation verify "$fichier" --repo alebgl77/AnswerLineage \
    --cert-identity "$IDENTITY" --deny-self-hosted-runners
done
# … et identique au fichier de la version GitHub.
gh release download "v$VERSION" --repo alebgl77/AnswerLineage --pattern SHA256SUMS
(cd paquets && sha256sum --check --ignore-missing ../SHA256SUMS)

# Installer ce qui a été vérifié.
pip install --no-index --find-links=paquets "answerlineage-api==$VERSION"
```

Les dépendances tierces (FastAPI, SQLAlchemy…) ne sont pas attestées par ce projet : seuls les fichiers `answerlineage_*` le sont. Sur PyPI, la page de chaque fichier montre aussi son attestation de publication (PEP 740), signée par la même identité ; l'outil `pypi-attestations` de PyPA la vérifie sans compte GitHub.

## Ce que la vérification prouve

- L'artefact vérifié a été produit par le workflow `release` de `alebgl77/AnswerLineage`, lancé par l'étiquette de la version, sur un runner hébergé par GitHub ; la provenance nomme en plus le commit des sources et l'exécution du workflow.
- Il n'a pas changé depuis : la signature et les attestations portent sur son empreinte, qu'un seul octet modifié change.
- La signature est publique : elle est inscrite au journal de transparence de Sigstore, où chacun peut retrouver tout ce que cette identité a signé.

## Ce qu'elle ne prouve pas

- Que le code est sain : la vérification dit d'où vient l'artefact, pas ce qu'il fait. L'image a été analysée par Grype le jour de sa publication, pas depuis.
- Que l'étiquette a été posée par le mainteneur : quiconque peut pousser une étiquette `v*` déclenche une publication valablement signée. D'où la protection des étiquettes, plus bas.
- Que le workflow de publication est celui que l'on croit : son contenu à l'étiquette fait partie de l'identité signée, et une modification fusionnée produirait elle aussi des signatures valables. Les workflows sont des chemins protégés ([Agents de code](ci.md#agents-de-code)).
- Que les dépendances sont saines : les versions verrouillées et les images de base épinglées sont celles qui ont été construites, rien de plus.
- Qu'une autre construction des mêmes sources donnerait les mêmes octets : personne n'a reconstruit l'artefact de son côté.
- Qu'une version signée est toujours sûre : une signature ne se révoque pas. Une version reconnue vulnérable reste valablement signée ; elle est signalée par un avis de sécurité ([Politique de sécurité](../SECURITY.md)).

Une vérification ne vaut que par l'identité exigée : une expression régulière trop large (`--certificate-identity-regexp '.*'`) accepterait la signature de n'importe quel workflow de GitHub.

## Comment sort une version

**Préparer.** Une merge request change la version des composants publiables, dans le `pyproject.toml` de chacun : leur `version`, et la contrainte qui épingle un composant frère (`answerlineage-api==…` dans `workers/`, `answerlineage-workers==…` dans `conformance/`). Les paquets publiés s'installent ainsi toujours ensemble, à la même version. La même merge request porte la [revue du modèle de menaces](securite/modele-de-menaces.md#la-revue-dune-version) pour cette version, et le mainteneur y signe les acceptations qu'il retient. `make packages` vérifie l'accord sur le poste, et le pipeline à chaque merge request ; il refuse une version pour laquelle le modèle n'a pas été revu, et liste les acceptations qui restent à signer.

**Étiqueter.** Après la fusion, le mainteneur pose l'étiquette sur le commit de `main` et la pousse. Une version s'écrit `v1.2.3`, ou `v1.2.3-alpha.1`, `v1.2.3-beta.1`, `v1.2.3-rc.1` pour une préversion ; toute autre forme est refusée.

| Étiquette | Paquets | Image | Version GitHub |
| --- | --- | --- | --- |
| `v1.2.3` | `1.2.3` | `1.2.3` | Version |
| `v1.2.3-rc.1` | `1.2.3rc1` | `1.2.3-rc.1` | Préversion |

**Publier.** Le workflow `release` enchaîne ses jobs :

| Job | Ce qu'il fait | Ce qu'il publie |
| --- | --- | --- |
| `prepare` | Vérifie que l'étiquette est la version déclarée par chaque composant, et que le modèle de menaces a été revu pour elle sans acceptation à signer, sinon s'arrête ; construit les paquets (`uv build`) et contrôle leurs métadonnées ; rédige les notes de version | Rien |
| `image` | Construit l'image, produit son SBOM, l'analyse ; la pousse, la signe sans clé, vérifie la signature, attache le SBOM et la provenance | L'image et ses preuves |
| `pypi` | Atteste la provenance des paquets ; les publie sur PyPI par publication de confiance, dans l'environnement `pypi`, avec leurs attestations de publication | Les paquets |
| `github-release` | Crée la version en brouillon, y joint les paquets, le SBOM et `SHA256SUMS`, puis la publie | La version GitHub |
| `report` | Toujours, même après un échec : dit ce qui est publié et ce qui ne l'est pas, dans le résumé de l'exécution | Rien |

L'ordre est voulu. Tout ce qui peut échouer sans rien publier passe d'abord : l'étiquette, les métadonnées, les notes, puis l'analyse de l'image. L'image, qui se republie, précède les paquets, que PyPI ne laisse jamais remplacer. La version GitHub vient en dernier : elle n'apparaît qu'une fois tout le reste publié. Chaque job reçoit ses seules permissions, et aucun ne lit de secret ; `make policy` refuse un job qui publie avec un secret.

**Les notes de version** sont tirées des commits conventionnels depuis la version précédente (`scripts/release_notes.py`) : la précédente version finale pour une version finale, la précédente étiquette pour une préversion. Elles sont groupées par type (nouveautés, corrections…), les ruptures en tête. Elles ne citent que la ligne de résumé des commits et le texte de leurs ruptures : ni auteur, ni empreinte, ni commit de fusion, donc jamais un nom de branche ; une clé de ticket entre parenthèses est retirée, et un message qui renvoie aux documents de cadrage, privés, est écarté. Les entrées sont triées : les mêmes commits donnent les mêmes notes. Le workflow y ajoute la référence de l'image par son empreinte et un renvoi vers ce document.

**Une publication interrompue** se lit dans le bilan du job `report`, puis se reprend :

| Job en échec | Ce qui est publié | Reprendre |
| --- | --- | --- |
| `prepare` | Rien | Corriger, puis poser une nouvelle étiquette |
| `image` | Peut-être l'image, sans toutes ses preuves | Relancer les jobs en échec : l'image est reconstruite, repoussée, signée et attestée |
| `pypi` | L'image, et peut-être une partie des paquets | Si aucun fichier n'est parti, relancer. Sinon, PyPI refuse un fichier déjà reçu : une version incomplète se remplace par la suivante |
| `github-release` | L'image et les paquets | Relancer : un brouillon laissé par l'échec est remplacé, une version publiée ne l'est jamais |

**Ce qui est éprouvé à chaque merge request**, sans rien publier : `make packages` (job `static`) construit les paquets de chaque composant, contrôle leurs métadonnées (nom, version, licence et son texte, description, classifieurs, adresse des sources, épinglage des frères), vérifie qu'une étiquette fictive, celle de la version déclarée, est acceptée et qu'une autre est refusée, que le modèle de menaces a été revu pour elle, en listant les acceptations qui restent à signer, et rédige les notes qu'aurait cette étiquette. `scripts/image.sh signtest` (job `image`) éprouve la signature, comme le dit la section suivante. Ce qui ne s'éprouve qu'à la publication : la poussée de l'image, sa signature sans clé, les attestations, l'envoi sur PyPI et la version GitHub.

## La signature éprouvée à chaque merge request

Une vraie signature se fait à la publication d'une version, jamais avant. Le job `image` du pipeline éprouve pourtant à chaque merge request ce qu'elle doit garantir, sans rien publier (`scripts/image.sh signtest`) :

1. un registre éphémère démarre, sur un port libre de la boucle locale ;
2. l'image construite y est poussée, puis signée par son empreinte avec une paire de clés jetable ;
3. la signature se vérifie avec la clé publique ;
4. une image modifiée (l'image construite, une couche de plus) est poussée sous la même étiquette : sa vérification doit échouer, faute de signature ;
5. la même image modifiée, signée par une autre clé jetable, doit échouer aussi : aucune de ses signatures ne vient de la clé attendue.

Une vérification qui réussirait sur l'image modifiée fait échouer le job, comme une vérification refusée pour une autre raison que l'absence de signature valable : un registre injoignable ne doit pas passer pour une preuve. cosign n'a pour réseau qu'un réseau Docker interne où le registre éphémère est la seule machine joignable, et sa configuration de signature ne nomme aucun journal de transparence : aucune signature n'atteint le journal public depuis une merge request.

Sur le poste, avec Docker : `scripts/image.sh build` puis `scripts/image.sh signtest`, ou `make image`, qui enchaîne tout.

cosign et le registre éphémère tournent depuis leurs images, épinglées par empreinte dans `scripts/image.sh` et tenues à jour par Renovate, comme Syft et Grype.

## Ce que le mainteneur règle une fois

Ces réglages ne se versionnent pas ; aucun workflow ne peut les faire à sa place.

1. **Réserver les noms** de la section suivante, avant la première étiquette.
2. **Créer l'environnement `pypi`** du dépôt (Settings, Environments) : déploiement limité aux étiquettes `v*`, et, au choix, l'approbation d'un mainteneur avant chaque envoi sur PyPI.
3. **Déclarer un éditeur de confiance sur PyPI** pour chaque composant que publie le job `pypi` (section suivante), avant sa première publication sous forme d'éditeur en attente (« pending publisher ») : propriétaire `alebgl77`, dépôt `AnswerLineage`, workflow `release.yml`, environnement `pypi`. Sans lui, PyPI refuse l'envoi ; avec lui, il n'accepte que ce workflow, dans cet environnement.
4. **Protéger les étiquettes `v*`** (règles du dépôt) : seul un mainteneur les crée, personne ne les déplace ni ne les supprime.
5. **Rendre public le paquet `answerlineage`** du registre de conteneurs après la première publication (Packages, Package settings) et le lier au dépôt, pour que l'image, sa signature et ses attestations se tirent sans authentification.
6. Au choix, **activer les versions immuables** du dépôt : une version publiée ne change plus, ni son étiquette ni ses fichiers. Le job `github-release` y est prêt : il joint les fichiers au brouillon, avant de publier.

## Noms de distribution

Le nom de distribution du projet est `answerlineage`, celui de son `pyproject.toml` racine et de son image. Les composants publiables en dérivent le leur, et l'explorateur le sien. La documentation n'en cite aucun autre : un test le vérifie, puisqu'un nom cité mais non réservé, comme un ancien nom de travail ou celui d'un composant abandonné, pourrait être pris par un tiers et installé à la place du projet.

| Registre | Nom | Publié par |
| --- | --- | --- |
| PyPI | `answerlineage` | Aucun job : le nom de distribution, à réserver |
| PyPI | `answerlineage-api` | Le job `pypi` |
| PyPI | `answerlineage-cli` | Le job `pypi` |
| PyPI | `answerlineage-conformance` | Le job `pypi` |
| PyPI | `answerlineage-corpus` | Le job `pypi` |
| PyPI | `answerlineage-proxy` | Le job `pypi` |
| PyPI | `answerlineage-workers` | Le job `pypi` |
| npm | `answerlineage` | Aucun job : le nom de distribution, à réserver |
| npm | `answerlineage-ui` | Aucun job : l'explorateur est privé (`"private": true`), son nom est à réserver |
| Registre de conteneurs de GitHub | `ghcr.io/alebgl77/answerlineage` | Le job `image` |

PyPI et npm ne réservent un nom qu'à sa première publication, et un éditeur de confiance en attente ne réserve rien : un tiers peut prendre le nom avant. Les noms des composants sont pris par la première version ; les noms qu'aucun job ne publie le sont par le mainteneur, par une première publication à part qui ne fait que renvoyer au dépôt. Le nom de l'image ne peut être pris que par le propriétaire du dépôt.
