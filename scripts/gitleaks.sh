#!/usr/bin/env bash
# Détection de secrets avec gitleaks.
#
#   scripts/gitleaks.sh scan      analyse tout l'historique du dépôt
#   scripts/gitleaks.sh selftest  vérifie que le détecteur bloque bien un secret
#
# L'image est épinglée par empreinte. Elle tourne sans réseau, le dépôt monté
# en lecture seule. Une trouvaille est affichée avec son fichier, sa ligne et
# sa règle ; sa valeur est masquée. Une exception se déclare, avec son motif,
# dans .gitleaksignore.
set -euo pipefail

CANARY_REPOSITORY=""
IMAGE="ghcr.io/gitleaks/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f"

gitleaks_git() {
  docker run --rm --network none \
    --volume "$1:/repo:ro" \
    --env GIT_CONFIG_COUNT=1 \
    --env GIT_CONFIG_KEY_0=safe.directory \
    --env GIT_CONFIG_VALUE_0='*' \
    "$IMAGE" git --no-banner --redact --verbose --exit-code 1 \
    --gitleaks-ignore-path /repo /repo
}

scan() {
  gitleaks_git "$(git rev-parse --show-toplevel)"
}

# Un détecteur muet rendrait le contrôle vert pour de mauvaises raisons : ce
# canari, fabriqué à l'exécution, doit faire échouer l'analyse.
selftest() {
  local status=0 suffix
  CANARY_REPOSITORY="$(mktemp -d)"
  trap 'rm -rf "$CANARY_REPOSITORY"' EXIT
  git -C "$CANARY_REPOSITORY" init --quiet
  suffix="$(head -c 256 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 36)"
  printf 'token = "%s_%s"\n' "ghp" "$suffix" > "$CANARY_REPOSITORY/config.py"
  git -C "$CANARY_REPOSITORY" add config.py
  git -C "$CANARY_REPOSITORY" -c user.name=canari -c user.email=canari@example.invalid \
    commit --quiet --message "canari"
  chmod -R a+rX "$CANARY_REPOSITORY"
  gitleaks_git "$CANARY_REPOSITORY" > /dev/null 2>&1 || status=$?
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
