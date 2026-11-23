#!/usr/bin/env bash
# Image d'AnswerLineage : construction, inventaire des composants (SBOM),
# analyse de vulnérabilités et signature. Le pipeline de chaque merge request
# et celui de publication d'une version enchaînent les mêmes sous-commandes.
#
#   scripts/image.sh            tout, dans l'ordre : build, sbom, selftest, signtest,
#                               vulnerabilities, scan
#   scripts/image.sh build      construit l'image de deploy/Dockerfile et l'exporte en archive
#   scripts/image.sh sbom       inventorie ses composants avec Syft, en CycloneDX JSON
#   scripts/image.sh selftest   vérifie que l'analyse bloque une vulnérabilité critique connue
#   scripts/image.sh signtest   éprouve la signature, sans réseau public : l'image signée se
#                               vérifie, une image modifiée sous la même étiquette ne se vérifie pas
#   scripts/image.sh vulnerabilities
#                               relève les vulnérabilités connues de la version, quelle que soit
#                               leur gravité : l'image (son SBOM), les paquets Python publiés
#                               (uv.lock) et l'explorateur (ui/package-lock.json), chacun analysé
#                               par Grype ; le relevé s'écrit en JSON et en Markdown
#   scripts/image.sh scan       analyse le SBOM avec Grype : une vulnérabilité haute ou
#                               critique sans exception valable fait échouer la commande
#
# À la publication d'une version seulement :
#
#   scripts/image.sh push NOM:ÉTIQUETTE      pousse l'image et écrit son empreinte
#   scripts/image.sh sign NOM@sha256:…       la signe sans clé, puis vérifie la signature
#                                            avec l'identité exacte du workflow
#
# Syft, Grype et cosign tournent depuis leurs images, épinglées par empreinte,
# sans privilège ni accès au démon Docker : l'image leur est passée en archive,
# en lecture seule, ou par un registre. Syft tourne sans réseau ; Grype
# télécharge sa base de vulnérabilités à chaque exécution. Les exceptions se
# déclarent, avec leur motif et leur date de revue, dans
# vulnerability-exceptions.toml.
#
# IMAGE (le nom local de l'image), IMAGE_VERSION (la version inscrite dans le
# SBOM) et IMAGE_OUT (le dossier des résultats) se règlent par l'environnement.
set -euo pipefail

cd "$(dirname "$0")/.."

SYFT_IMAGE="ghcr.io/anchore/syft:v1.57.0@sha256:3c70e6d5979364cefdc22b51617a78fd264574d3bd6b4f84a351204147025e2a"
GRYPE_IMAGE="ghcr.io/anchore/grype:v0.121.2@sha256:6a36234da13f9faa327306ac0b3dab3405cfa2b4eb20c8f904125a8707863913"
COSIGN_IMAGE="ghcr.io/sigstore/cosign/cosign:v3.1.4@sha256:0f06154836d2de34f887a5c273506d52ca43edaa1d9e53e6510fa78a48f69c2d"
# Registre éphémère de signtest.
REGISTRY_IMAGE="public.ecr.aws/docker/library/registry:3.1.1@sha256:325b4b29b041e82803abeb703e201655e4e23ab83264ec1a7c9ddb0a5b14a6e0"
# Émetteur des jetons OIDC des workflows GitHub : l'identité d'une signature sans clé.
GITHUB_OIDC_ISSUER="https://token.actions.githubusercontent.com"

IMAGE=${IMAGE:-answerlineage:local}
IMAGE_VERSION=${IMAGE_VERSION:-local}
mkdir -p "${IMAGE_OUT:-build/image}"
OUT=$(cd "${IMAGE_OUT:-build/image}" && pwd)
ARCHIVE="$OUT/image.tar"
SBOM="$OUT/sbom.cdx.json"
REPORT="$OUT/grype.json"
# Le relevé des vulnérabilités connues : KNOWN.json et KNOWN.md.
KNOWN="$OUT/known-vulnerabilities"
EXCEPTIONS=vulnerability-exceptions.toml

# Ressources temporaires : hors des fonctions, pour que le nettoyage à la sortie
# les retrouve, et retirées quoi qu'il arrive, sans changer le code de sortie.
CANARY_DIR=""
WORK_DIR=""
REGISTRY_CONTAINER=""
SIGN_NETWORK=""
SIGN_TAG=""

cleanup() {
  if [ -n "$SIGN_TAG" ]; then docker image rm "$SIGN_TAG" >/dev/null 2>&1 || true; fi
  if [ -n "$REGISTRY_CONTAINER" ]; then docker rm --force "$REGISTRY_CONTAINER" >/dev/null 2>&1 || true; fi
  if [ -n "$SIGN_NETWORK" ]; then docker network rm "$SIGN_NETWORK" >/dev/null 2>&1 || true; fi
  if [ -n "$CANARY_DIR" ]; then rm -rf "$CANARY_DIR" || true; fi
  if [ -n "$WORK_DIR" ]; then rm -rf "$WORK_DIR" || true; fi
}
trap cleanup EXIT

fail() {
  echo "ÉCHEC : $1" >&2
  exit 1
}

# Un registre public limite parfois le débit des tirages anonymes : la commande
# est retentée deux fois avant d'être tenue pour un échec.
retry() {
  local attempt
  for attempt in 1 2 3; do
    "$@" && return 0
    [ "$attempt" -lt 3 ] || break
    echo "Échec (essai $attempt sur 3), nouvel essai dans $((attempt * 20)) s." >&2
    sleep $((attempt * 20))
  done
  return 1
}

# Un outil lancé depuis son image : aucun privilège, aucun socket Docker.
tool() {
  docker run --rm --cap-drop ALL --security-opt no-new-privileges "$@"
}

grype() {
  tool --env GRYPE_CHECK_FOR_APP_UPDATE=false --env GRYPE_DB_CACHE_DIR=/tmp/grype-db \
    --volume "$1:/sbom.json:ro" "$GRYPE_IMAGE" sbom:/sbom.json --output json
}

# cosign tourne sous l'utilisateur du poste ; son seul dossier inscriptible est
# WORK_DIR, où il trouve ses clés et range son cache. Les options de docker run
# précèdent l'image, ses propres arguments la suivent.
cosign() {
  tool --user "$(id -u):$(id -g)" --env HOME=/work --volume "$WORK_DIR:/work" --workdir /work "$@"
}

build() {
  echo "Construction de l'image $IMAGE depuis deploy/Dockerfile."
  # Sans attestation de provenance : l'image poussée est celle-ci, telle qu'analysée.
  retry docker build --provenance=false --file deploy/Dockerfile --tag "$IMAGE" . \
    || fail "l'image ne se construit pas"
  docker save --output "$ARCHIVE" "$IMAGE"
  chmod a+r "$ARCHIVE"
  echo "Image $IMAGE exportée dans $ARCHIVE ($(du -h "$ARCHIVE" | cut -f 1))."
}

sbom() {
  [ -f "$ARCHIVE" ] || fail "archive de l'image absente ($ARCHIVE) : lancer d'abord « $0 build »"
  echo "Inventaire des composants de l'image avec Syft ($SYFT_IMAGE)."
  tool --network none --env SYFT_CHECK_FOR_APP_UPDATE=false \
    --volume "$ARCHIVE:/image.tar:ro" "$SYFT_IMAGE" \
    scan docker-archive:/image.tar \
    --source-name answerlineage --source-version "$IMAGE_VERSION" \
    --output cyclonedx-json >"$SBOM.partial"
  mv "$SBOM.partial" "$SBOM"
  chmod a+r "$SBOM"
  # Ce que l'attestation de la publication exigera (actions/attest) se vérifie dès la merge request.
  python3 - "$SBOM" <<'PYTHON'
import json
import os
import sys

if os.path.getsize(sys.argv[1]) > 16 * 1024 * 1024:
    sys.exit("le SBOM dépasse 16 Mo, la taille maximale d'une attestation")
document = json.load(open(sys.argv[1], encoding="utf-8"))
if document.get("bomFormat") != "CycloneDX" or not document.get("serialNumber"):
    sys.exit("le SBOM n'est pas un document CycloneDX avec son numéro de série")
components = document.get("components") or []
if not components:
    sys.exit("le SBOM ne liste aucun composant")
print(f"SBOM CycloneDX {document.get('specVersion')} : {len(components)} composants, dans {sys.argv[1]}.")
PYTHON
}

# Un détecteur muet rendrait le contrôle vert pour de mauvaises raisons : ce
# canari, un SBOM écrit à la main qui déclare PyYAML 5.3 (CVE-2020-14343,
# critique), doit faire échouer l'analyse, sans aucune exception.
selftest() {
  local workdir status=0
  CANARY_DIR=$(mktemp -d)
  workdir=$CANARY_DIR
  cat >"$workdir/sbom.cdx.json" <<'JSON'
{
  "bomFormat": "CycloneDX",
  "specVersion": "1.6",
  "version": 1,
  "metadata": {"component": {"type": "application", "name": "canari"}},
  "components": [
    {
      "type": "library",
      "bom-ref": "pkg:pypi/pyyaml@5.3",
      "name": "pyyaml",
      "version": "5.3",
      "purl": "pkg:pypi/pyyaml@5.3"
    }
  ]
}
JSON
  : >"$workdir/exceptions.toml"
  chmod -R a+rX "$workdir"
  grype "$workdir/sbom.cdx.json" >"$workdir/grype.json" || fail "Grype n'a pas analysé le canari"
  GITHUB_STEP_SUMMARY='' python3 -m scripts.check_vulnerabilities report "$workdir/grype.json" \
    --exceptions "$workdir/exceptions.toml" >"$workdir/verdict.txt" || status=$?
  if [ "$status" -ne 1 ] || ! grep --quiet --fixed-strings CVE-2020-14343 "$workdir/grype.json"; then
    cat "$workdir/verdict.txt"
    fail "le canari n'a pas été bloqué (code de sortie $status) : l'analyse ne bloque rien."
  fi
  echo "Canari bloqué : PyYAML 5.3 et sa vulnérabilité critique CVE-2020-14343 font échouer l'analyse."
}

# Le relevé des vulnérabilités connues de la version. Les paquets Python publiés et
# l'explorateur n'ont pas d'image : leur inventaire se tire de leur verrou, en SBOM
# CycloneDX, que Grype analyse comme celui de l'image. Le relevé dit l'état de chaque
# vulnérabilité ; c'est la publication qui refuse une vulnérabilité bloquante
# (python3 -m scripts.check_release vulnerabilities). Il se produit avant le verdict
# de « scan », pour exister même quand l'image est bloquée.
vulnerabilities() {
  local source
  [ -f "$SBOM" ] || fail "SBOM absent ($SBOM) : lancer d'abord « $0 sbom »"
  python3 -m scripts.known_vulnerabilities inventory python --output "$OUT/python.cdx.json" \
    || fail "l'inventaire des paquets Python ne se tire pas de uv.lock"
  python3 -m scripts.known_vulnerabilities inventory npm --output "$OUT/npm.cdx.json" \
    || fail "l'inventaire de l'explorateur ne se tire pas de ui/package-lock.json"
  chmod a+r "$OUT/python.cdx.json" "$OUT/npm.cdx.json"
  echo "Analyse de l'image, des paquets Python et de l'explorateur avec Grype ($GRYPE_IMAGE)."
  grype "$SBOM" >"$OUT/grype-image.json" || fail "Grype n'a pas analysé le SBOM de l'image"
  for source in python npm; do
    grype "$OUT/$source.cdx.json" >"$OUT/grype-$source.json" \
      || fail "Grype n'a pas analysé l'inventaire $source"
  done
  python3 -m scripts.known_vulnerabilities build --version "$IMAGE_VERSION" \
    --source image="$OUT/grype-image.json" \
    --source python="$OUT/grype-python.json" \
    --source npm="$OUT/grype-npm.json" \
    --exceptions "$EXCEPTIONS" --json "$KNOWN.json" --markdown "$KNOWN.md" \
    || fail "le relevé des vulnérabilités connues ne s'écrit pas"
}

scan() {
  [ -f "$SBOM" ] || fail "SBOM absent ($SBOM) : lancer d'abord « $0 sbom »"
  echo "Analyse du SBOM avec Grype ($GRYPE_IMAGE), base de vulnérabilités du jour."
  grype "$SBOM" >"$REPORT" || fail "Grype n'a pas analysé le SBOM"
  python3 -m scripts.check_vulnerabilities report "$REPORT" --exceptions "$EXCEPTIONS"
}

# Pousse une image et écrit sur la sortie standard l'empreinte que le registre
# lui a donnée : c'est elle, jamais l'étiquette, qui se signe et s'atteste.
push() {
  local reference=$1 output digest
  output=$(docker push "$reference") || fail "l'image $reference ne se pousse pas"
  printf '%s\n' "$output" >&2
  digest=$(printf '%s\n' "$output" | grep --only-matching --extended-regexp 'digest: sha256:[0-9a-f]{64}' \
    | tail -n 1 | cut -d ' ' -f 2)
  [ -n "$digest" ] || fail "empreinte de l'image poussée introuvable dans la sortie de docker push"
  echo "$digest"
}

# Ce que cosign verify répond quand l'image n'a aucune signature, et quand
# aucune de ses signatures ne vient de la clé attendue.
UNSIGNED="no signatures found"
FOREIGN="no matching (signatures|attestations)"

# Une vérification refusée ne prouve quelque chose que si elle l'est pour la
# bonne raison : un registre injoignable la ferait échouer aussi.
expect_refused() {
  local reason=$1 expected=$2 reference=$3 status=0
  cosign --network "$SIGN_NETWORK" "$COSIGN_IMAGE" verify --key projet.pub \
    --insecure-ignore-tlog=true --allow-http-registry "$reference" >"$WORK_DIR/verify.log" 2>&1 \
    || status=$?
  if [ "$status" -eq 0 ]; then
    cat "$WORK_DIR/verify.log" >&2
    fail "$reason passe la vérification : la signature ne protège rien."
  fi
  if ! grep --quiet --extended-regexp "$expected" "$WORK_DIR/verify.log"; then
    cat "$WORK_DIR/verify.log" >&2
    fail "$reason est refusée pour une autre raison que « $expected » (code $status) : le contrôle ne prouve rien."
  fi
}

# La signature éprouvée à chaque merge request, sans réseau public : un registre
# éphémère, une paire de clés jetable, l'image signée par son empreinte puis
# vérifiée ; puis une image modifiée poussée sous la même étiquette, sans
# signature puis signée par une autre clé, ne doit jamais se vérifier. cosign
# n'a pour réseau qu'un réseau interne où le registre est la seule machine
# joignable, et sa configuration de signature ne nomme aucun journal de
# transparence : aucune signature n'en sort.
signtest() {
  local address digest modified attempt inside="registre:5000/answerlineage"
  docker image inspect "$IMAGE" >/dev/null 2>&1 || fail "image $IMAGE absente : lancer d'abord « $0 build »"
  WORK_DIR=$(mktemp -d)
  SIGN_NETWORK="answerlineage-signature-$$-$RANDOM"
  docker network create --internal "$SIGN_NETWORK" >/dev/null
  # Le registre écoute sur un port libre de la boucle locale, pour le démon
  # Docker qui y pousse, et sur le réseau interne, pour cosign.
  REGISTRY_CONTAINER=$(retry tool --detach --publish 127.0.0.1::5000 "$REGISTRY_IMAGE") \
    || fail "le registre éphémère ne démarre pas"
  docker network connect --alias registre "$SIGN_NETWORK" "$REGISTRY_CONTAINER"
  address=$(docker port "$REGISTRY_CONTAINER" 5000/tcp | head -n 1)
  for attempt in $(seq 1 50); do
    curl --silent --fail --max-time 2 "http://$address/v2/" >/dev/null && break
    [ "$attempt" -lt 50 ] || fail "le registre éphémère ne répond pas sur $address"
    sleep 0.2
  done

  SIGN_TAG="$address/answerlineage:signature"
  docker tag "$IMAGE" "$SIGN_TAG"
  digest=$(push "$SIGN_TAG")
  echo "Image poussée dans le registre éphémère : $digest."

  cosign --network none --env COSIGN_PASSWORD= "$COSIGN_IMAGE" \
    generate-key-pair --output-key-prefix projet >/dev/null
  cosign --network none --env COSIGN_PASSWORD= "$COSIGN_IMAGE" \
    generate-key-pair --output-key-prefix intrus >/dev/null
  cosign --network none "$COSIGN_IMAGE" signing-config create --out sans-journal.json
  cosign --network "$SIGN_NETWORK" --env COSIGN_PASSWORD= "$COSIGN_IMAGE" sign --yes \
    --key projet.key --signing-config sans-journal.json --allow-http-registry "$inside@$digest" \
    || fail "l'image ne se signe pas"
  cosign --network "$SIGN_NETWORK" "$COSIGN_IMAGE" verify --key projet.pub \
    --insecure-ignore-tlog=true --allow-http-registry "$inside:signature" >/dev/null \
    || fail "l'image signée ne se vérifie pas"
  echo "Image signée par son empreinte, puis vérifiée par son étiquette avec la clé publique."

  # Une autre image sous la même étiquette : l'image publiée, une couche de plus.
  local container
  container=$(docker create "$IMAGE")
  docker commit --change "LABEL org.opencontainers.image.description=modifiee" "$container" "$SIGN_TAG" >/dev/null
  docker rm "$container" >/dev/null
  modified=$(push "$SIGN_TAG")
  [ "$modified" != "$digest" ] || fail "l'image modifiée a la même empreinte que l'image signée"
  expect_refused "l'image modifiée" "$UNSIGNED" "$inside:signature"
  echo "Image modifiée sous la même étiquette : refusée, elle n'a pas de signature."

  cosign --network "$SIGN_NETWORK" --env COSIGN_PASSWORD= "$COSIGN_IMAGE" sign --yes \
    --key intrus.key --signing-config sans-journal.json --allow-http-registry "$inside@$modified" \
    || fail "l'image modifiée ne se signe pas avec l'autre clé"
  expect_refused "l'image modifiée et signée par une autre clé" "$FOREIGN" "$inside:signature"
  echo "Image modifiée et signée par une autre clé : refusée, aucune signature ne vient de la clé du projet."
}

# Signature sans clé, à la publication : cosign obtient de Fulcio un certificat
# éphémère pour l'identité OIDC du workflow, signe l'image par son empreinte et
# inscrit la signature au journal de transparence public (Rekor). La signature
# publiée est aussitôt vérifiée avec l'identité exacte que l'on donnera à qui
# installe l'image.
sign() {
  local subject=$1 identity config
  [[ "$subject" =~ ^[a-z0-9.-]+(:[0-9]+)?(/[a-z0-9._-]+)+@sha256:[0-9a-f]{64}$ ]] \
    || fail "une image se signe par son empreinte (nom@sha256:…), jamais par son étiquette"
  if [ -z "${GITHUB_WORKFLOW_REF:-}" ] || [ -z "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ]; then
    fail "la signature sans clé se fait dans le workflow de publication, avec la permission id-token: write"
  fi
  identity="${GITHUB_SERVER_URL:-https://github.com}/$GITHUB_WORKFLOW_REF"
  config="${DOCKER_CONFIG:-$HOME/.docker}/config.json"
  [ -f "$config" ] || fail "identifiant du registre absent ($config) : docker login d'abord"
  WORK_DIR=$(mktemp -d)
  echo "Signature sans clé de $subject ($COSIGN_IMAGE), identité $identity."
  cosign --env ACTIONS_ID_TOKEN_REQUEST_URL --env ACTIONS_ID_TOKEN_REQUEST_TOKEN \
    --volume "$config:/docker/config.json:ro" --env DOCKER_CONFIG=/docker \
    "$COSIGN_IMAGE" sign --yes "$subject" || fail "l'image ne se signe pas"
  cosign --volume "$config:/docker/config.json:ro" --env DOCKER_CONFIG=/docker \
    "$COSIGN_IMAGE" verify "$subject" \
    --certificate-identity "$identity" --certificate-oidc-issuer "$GITHUB_OIDC_ISSUER" >/dev/null \
    || fail "la signature publiée ne se vérifie pas avec l'identité du workflow"
  echo "Signature publiée et vérifiée : $subject, signée par $identity."
}

case "${1:-all}" in
  build | sbom | selftest | signtest | vulnerabilities | scan) "$1" ;;
  push | sign)
    [ $# -eq 2 ] || fail "usage : $0 $1 IMAGE"
    "$1" "$2"
    ;;
  all)
    build
    sbom
    selftest
    signtest
    vulnerabilities
    scan
    ;;
  *)
    echo "Usage : $0 [all | build | sbom | selftest | signtest | vulnerabilities | scan | push IMAGE | sign IMAGE@sha256:…]" >&2
    exit 2
    ;;
esac
