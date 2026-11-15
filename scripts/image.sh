#!/usr/bin/env bash
# Image d'AnswerLineage : construction, inventaire des composants (SBOM) et
# analyse de vulnérabilités. Le pipeline de chaque merge request et celui de
# publication d'une version enchaînent les mêmes sous-commandes.
#
#   scripts/image.sh            tout, dans l'ordre : build, sbom, selftest, scan
#   scripts/image.sh build      construit l'image de deploy/Dockerfile et l'exporte en archive
#   scripts/image.sh sbom       inventorie ses composants avec Syft, en CycloneDX JSON
#   scripts/image.sh selftest   vérifie que l'analyse bloque une vulnérabilité critique connue
#   scripts/image.sh scan       analyse le SBOM avec Grype : une vulnérabilité haute ou
#                               critique sans exception valable fait échouer la commande
#
# Syft et Grype tournent depuis leurs images, épinglées par empreinte, sans
# accès au démon Docker : l'image leur est passée en archive, en lecture seule.
# Syft tourne sans réseau ; Grype télécharge sa base de vulnérabilités à chaque
# exécution. Les exceptions se déclarent, avec leur motif et leur date de
# revue, dans vulnerability-exceptions.toml.
#
# IMAGE (le nom local de l'image), IMAGE_VERSION (la version inscrite dans le
# SBOM) et IMAGE_OUT (le dossier des résultats) se règlent par l'environnement.
set -euo pipefail

cd "$(dirname "$0")/.."

SYFT_IMAGE="ghcr.io/anchore/syft:v1.57.0@sha256:3c70e6d5979364cefdc22b51617a78fd264574d3bd6b4f84a351204147025e2a"
GRYPE_IMAGE="ghcr.io/anchore/grype:v0.121.2@sha256:6a36234da13f9faa327306ac0b3dab3405cfa2b4eb20c8f904125a8707863913"

IMAGE=${IMAGE:-answerlineage:local}
IMAGE_VERSION=${IMAGE_VERSION:-local}
mkdir -p "${IMAGE_OUT:-build/image}"
OUT=$(cd "${IMAGE_OUT:-build/image}" && pwd)
ARCHIVE="$OUT/image.tar"
SBOM="$OUT/sbom.cdx.json"
REPORT="$OUT/grype.json"
EXCEPTIONS=vulnerability-exceptions.toml
# Dossier du canari : hors de la fonction, pour que le nettoyage à la sortie le retrouve.
CANARY_DIR=""

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
  trap 'rm -rf "$CANARY_DIR"' EXIT
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

scan() {
  [ -f "$SBOM" ] || fail "SBOM absent ($SBOM) : lancer d'abord « $0 sbom »"
  echo "Analyse du SBOM avec Grype ($GRYPE_IMAGE), base de vulnérabilités du jour."
  grype "$SBOM" >"$REPORT" || fail "Grype n'a pas analysé le SBOM"
  python3 -m scripts.check_vulnerabilities report "$REPORT" --exceptions "$EXCEPTIONS"
}

case "${1:-all}" in
  build | sbom | selftest | scan) "$1" ;;
  all)
    build
    sbom
    selftest
    scan
    ;;
  *)
    echo "Usage : $0 [all | build | sbom | selftest | scan]" >&2
    exit 2
    ;;
esac
