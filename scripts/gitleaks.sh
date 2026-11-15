#!/usr/bin/env bash
# Détection de secrets avec gitleaks (US-0103, ENF-SEC-02).
#
#   scripts/gitleaks.sh scan      analyse tout l'historique du dépôt
#   scripts/gitleaks.sh selftest  vérifie que le détecteur bloque bien un secret
#
# L'image est épinglée par empreinte. Elle tourne sans réseau, le dépôt monté
# en lecture seule.
set -euo pipefail

IMAGE="ghcr.io/gitleaks/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f"

gitleaks_git() {
  docker run --rm --network none \
    --volume "$1:/repo:ro" \
    --env GIT_CONFIG_COUNT=1 \
    --env GIT_CONFIG_KEY_0=safe.directory \
    --env GIT_CONFIG_VALUE_0='*' \
    "$IMAGE" git --no-banner --redact --exit-code 1 /repo
}

scan() {
  gitleaks_git "$(git rev-parse --show-toplevel)"
}

# Un détecteur muet rendrait le contrôle vert pour de mauvaises raisons : ce
# canari, fabriqué à l'exécution, doit faire échouer l'analyse.
selftest() {
  local workdir status=0
  workdir="$(mktemp -d)"
  trap 'rm -rf "$workdir"' EXIT
  git -C "$workdir" init --quiet
  local suffix
  suffix="$(head -c 256 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 36)"
  printf 'token = "%s_%s"\n' "ghp" "$suffix" > "$workdir/config.py"
  git -C "$workdir" add config.py
  git -C "$workdir" -c user.name=canari -c user.email=canari@example.invalid \
    commit --quiet --message "canari"
  chmod -R a+rX "$workdir"
  gitleaks_git "$workdir" > /dev/null 2>&1 || status=$?
  if [ "$status" -ne 1 ]; then
    echo "Le canari n'a pas été détecté (code de sortie $status) : le contrôle ne bloque rien."
    exit 1
  fi
  echo "Canari détecté : un secret poussé par erreur bloque le pipeline."
}

case "${1:-}" in
  scan) scan ;;
  selftest) selftest ;;
  *)
    echo "Usage : $0 scan|selftest" >&2
    exit 2
    ;;
esac
