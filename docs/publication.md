# Publication

Ce document s'adresse à qui installe AnswerLineage : ce que chaque version signe et atteste, comment le vérifier avant d'installer, et ce que la vérification prouve.

## Ce qui est signé et attesté

Une version sort quand le mainteneur pose une étiquette `v1.2.3` ; le workflow `release` (`.github/workflows/release.yml`) construit alors les artefacts, les signe et les atteste.

| Artefact | Preuve | Se vérifie avec |
| --- | --- | --- |
| Image `ghcr.io/alebgl77/answerlineage` | Signature cosign, par l'empreinte de l'image | `cosign verify` |
| | Attestation de provenance SLSA | `gh attestation verify` |
| | SBOM CycloneDX, attesté | `gh attestation verify --predicate-type https://cyclonedx.org/bom` |
| Paquets Python des composants (roues et sources) | Attestation de provenance SLSA | `gh attestation verify` |

Toutes ces preuves portent la même identité : le workflow `release` de ce dépôt, lancé par l'étiquette de la version. Elles sont faites sans clé : le workflow obtient de GitHub un jeton OIDC, Fulcio, l'autorité de Sigstore, lui délivre en échange un certificat de quelques minutes au nom de cette identité, et la signature est inscrite au journal de transparence public de Sigstore (Rekor). Aucune clé de signature n'existe, ni dans le dépôt ni dans ses secrets.

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

Les paquets construits à la publication d'une version sont gardés avec l'exécution du workflow `release` (artefact `paquets-v1.2.3`). Chaque fichier se vérifie avant d'être installé :

```bash
gh attestation verify answerlineage_api-1.2.3-py3-none-any.whl --repo alebgl77/AnswerLineage \
  --cert-identity "$IDENTITY" --deny-self-hosted-runners
```

## Ce que la vérification prouve

- L'artefact vérifié a été produit par le workflow `release` de `alebgl77/AnswerLineage`, lancé par l'étiquette de la version, sur un runner hébergé par GitHub ; la provenance nomme en plus le commit des sources et l'exécution du workflow.
- Il n'a pas changé depuis : la signature et les attestations portent sur son empreinte, qu'un seul octet modifié change.
- La signature est publique : elle est inscrite au journal de transparence de Sigstore, où chacun peut retrouver tout ce que cette identité a signé.

## Ce qu'elle ne prouve pas

- Que le code est sain : la vérification dit d'où vient l'artefact, pas ce qu'il fait. L'image a été analysée par Grype le jour de sa publication, pas depuis.
- Que l'étiquette a été posée par le mainteneur : quiconque peut pousser une étiquette `v*` déclenche une publication valablement signée. La protection des étiquettes est un réglage du dépôt ([Réglages du dépôt](ci.md#réglages-du-dépôt)).
- Que le workflow de publication est celui que l'on croit : son contenu à l'étiquette fait partie de l'identité signée, et une modification fusionnée produirait elle aussi des signatures valables. Les workflows sont des chemins protégés ([Agents de code](ci.md#agents-de-code)).
- Que les dépendances sont saines : les versions verrouillées et les images de base épinglées sont celles qui ont été construites, rien de plus.
- Qu'une autre construction des mêmes sources donnerait les mêmes octets : personne n'a reconstruit l'artefact de son côté.
- Qu'une version signée est toujours sûre : une signature ne se révoque pas. Une version reconnue vulnérable reste valablement signée ; elle est signalée par un avis de sécurité ([Politique de sécurité](../SECURITY.md)).

Une vérification ne vaut que par l'identité exigée : une expression régulière trop large (`--certificate-identity-regexp '.*'`) accepterait la signature de n'importe quel workflow de GitHub.

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
