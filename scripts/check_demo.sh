#!/usr/bin/env bash
# Vérifie la démonstration lancée par docker compose, comme le pipeline.
#
#   scripts/check_demo.sh           tout, dans l'ordre : up, checks, browser, restart, down
#   scripts/check_demo.sh up        construit et lance la démonstration ; depuis un clone
#                                   neuf, le premier graphe arrive en moins de cinq minutes
#   scripts/check_demo.sh checks    rien n'écoute hors de la boucle locale, un autre nom
#                                   d'hôte est refusé, le journal ne garde aucune adresse
#   scripts/check_demo.sh browser   parcours Remonter et Propager dans Chromium
#   scripts/check_demo.sh restart   arrêt, puis relance sans le service seed : les données
#                                   viennent du volume ; renvoyée, l'entreprise n'ajoute rien
#   scripts/check_demo.sh down      arrête la démonstration et efface ses données
#
# checks, browser et restart visent une démonstration déjà lancée. browser
# demande l'environnement du dépôt (make setup) et Chromium
# (uv run playwright install chromium). COMPOSE (la commande compose),
# ANSWERLINEAGE_DEMO_URL (l'adresse de la démonstration) et BUDGET_SECONDS (le
# délai du premier graphe) se règlent par l'environnement.
set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE=${COMPOSE:-docker compose}
URL=${ANSWERLINEAGE_DEMO_URL:-http://127.0.0.1:8000}
PORT=${URL##*:}
PORT=${PORT%%/*}
BUDGET_SECONDS=${BUDGET_SECONDS:-300}

fail() {
  echo "ÉCHEC : $1" >&2
  $COMPOSE logs --tail 40 >&2 || true
  exit 1
}

fetch() {
  curl --silent --fail --max-time 5 "$@"
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

# La vérité du premier incident, établie par le générateur sans passer par l'API :
# la requête qui le propage, et les nombres de réponses et de destinataires touchés.
load_truth() {
  local truth
  truth=$($COMPOSE exec -T api answerlineage-corpus truth) || fail "la vérité de l'entreprise est illisible"
  INCIDENT_QUERY=$(printf '%s' "$truth" | python3 -c '
import json, sys
from urllib.parse import urlencode
incident = json.load(sys.stdin)["incidents"][0]
print(urlencode({
    "namespace": incident["dataset"]["namespace"],
    "name": incident["dataset"]["name"],
    "from": incident["from"],
    "to": incident["to"],
}))')
  EXPECTED_COUNTS=$(printf '%s' "$truth" | python3 -c '
import json, sys
incident = json.load(sys.stdin)["incidents"][0]
print(len(incident["answers"]), len(incident["recipients"]))')
}

# Réponses et destinataires touchés par le premier incident, « réponses destinataires ».
impact_counts() {
  fetch "$URL/api/v1/impact?$INCIDENT_QUERY" | python3 -c '
import json, sys
counts = json.load(sys.stdin)["counts"]
print(counts["answers"], counts["recipients"])'
}

# Attend que la propagation du premier incident donne la vérité de l'entreprise.
wait_for_the_graph() {
  local deadline=$(($(date +%s) + BUDGET_SECONDS))
  until [ "$(impact_counts 2>/dev/null || true)" = "$EXPECTED_COUNTS" ]; do
    [ "$(date +%s)" -lt "$deadline" ] || fail "le graphe n'est pas complet après ${BUDGET_SECONDS} s"
    sleep 2
  done
}

graph_digest() {
  fetch "$URL/api/v1/graph?limit=200" | sha256sum | cut -d ' ' -f 1
}

# Vrai dès qu'une connexion TCP aboutit, quelle que soit la réponse HTTP qui
# suivrait : un refus 403 prouve que le port est joignable.
connects() {
  python3 - "$1" "$2" <<'PYTHON'
import socket
import sys

try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=3).close()
except OSError:
    sys.exit(1)
PYTHON
}

# Première adresse du poste sur le réseau ; vide là où hostname -I n'existe pas (macOS).
network_address() {
  local addresses
  addresses=$(hostname -I 2>/dev/null) || return 0
  printf '%s' "${addresses%% *}"
}

up() {
  local started elapsed page
  started=$(date +%s)
  # shellcheck disable=SC2086 # COMPOSE est une commande et ses arguments.
  retry $COMPOSE build || fail "l'image de la démonstration ne se construit pas"
  # shellcheck disable=SC2086
  retry $COMPOSE pull --quiet postgres || fail "l'image de PostgreSQL ne se tire pas"
  $COMPOSE up --detach
  load_truth
  wait_for_the_graph
  page=$(fetch "$URL/") || fail "l'explorateur n'est pas servi"
  grep --quiet 'id="root"' <<<"$page" || fail "l'explorateur n'est pas servi"
  elapsed=$(($(date +%s) - started))
  echo "Premier graphe en ${elapsed} s : ${EXPECTED_COUNTS} (réponses et destinataires du premier incident)."
  [ "$elapsed" -lt "$BUDGET_SECONDS" ] || fail "premier graphe en ${elapsed} s, pour ${BUDGET_SECONDS} s au plus"
}

checks() {
  # Aucun port n'est publié hors de la boucle locale.
  $COMPOSE ps --format json | python3 -c '
import json, sys
text = sys.stdin.read().strip()
services = json.loads(text) if text.startswith("[") else [json.loads(line) for line in text.splitlines()]
published = [
    (service["Service"], port["URL"], port["PublishedPort"])
    for service in services
    for port in service.get("Publishers") or []
    if port.get("PublishedPort")
]
beyond = [port for port in published if port[1] not in ("127.0.0.1", "::1")]
if not published or beyond:
    sys.exit(f"ports publiés : {published}")
print("Ports publiés, tous sur la boucle locale :", published)' || fail "un port écoute hors de la boucle locale"

  # Et rien ne répond sur l'adresse du réseau, quel que soit le statut HTTP.
  local address
  address=$(network_address)
  if [ -z "$address" ]; then
    [ -z "${CI:-}" ] || fail "aucune adresse réseau trouvée : la sonde ne peut pas tourner"
    echo "Sonde de l'adresse réseau sautée : hostname -I n'existe pas sur ce poste."
  elif connects "$address" "$PORT"; then
    fail "une connexion aboutit sur $address:$PORT, hors de la boucle locale"
  else
    echo "Aucune connexion n'aboutit sur $address:$PORT."
  fi

  # Une page qui joindrait l'API sous un autre nom d'hôte est refusée.
  local status
  status=$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 5 \
    --header 'Host: attaquant.example' "$URL/api/v1/graph")
  [ "$status" = 403 ] || fail "un nom d'hôte inattendu a reçu le statut $status, et non 403"
  echo "Un nom d'hôte inattendu reçoit 403."

  # Le journal de l'API ne garde aucune adresse demandée : ses paramètres peuvent
  # porter une donnée.
  local canary logs
  canary="canari-journal-$$-$(date +%s)"
  curl --silent --output /dev/null --max-time 5 "$URL/api/v1/graph?canari=$canary" || true
  sleep 1
  logs=$($COMPOSE logs api) || fail "le journal de l'API est illisible"
  if grep --quiet --fixed-strings "$canary" <<<"$logs"; then
    fail "le journal de l'API écrit les adresses demandées"
  fi
  echo "Le journal de l'API ne garde pas les adresses demandées."
}

browser() {
  ANSWERLINEAGE_DEMO_URL=$URL uv run pytest -m demo tests/test_demonstration.py \
    || fail "un parcours de l'explorateur échoue"
}

restart() {
  local before after services sent
  load_truth
  wait_for_the_graph
  before=$(graph_digest)
  $COMPOSE down

  # Relancée sans le service seed, rien ne renvoie l'entreprise à l'API : les
  # données ne peuvent venir que du volume.
  $COMPOSE up --detach api worker
  services=$($COMPOSE ps --all --services)
  if grep --quiet --line-regexp seed <<<"$services"; then
    fail "le service seed a tourné : la relance ne prouve plus rien"
  fi
  wait_for_the_graph
  after=$(graph_digest)
  [ "$before" = "$after" ] || fail "les données ont changé entre l'arrêt et la relance"
  echo "Données retrouvées dans le volume après un arrêt et une relance."

  # Renvoyée une seconde fois, l'entreprise n'ajoute rien : chaque événement est déjà connu.
  sent=$($COMPOSE run --rm --no-deps -T seed) || fail "le renvoi de l'entreprise échoue"
  echo "$sent"
  python3 -c '
import re, sys
found = re.search(r"(\d+) événements acceptés, dont (\d+) déjà connus", sys.argv[1])
sys.exit(found is None or found[1] != found[2] or found[1] == "0")' "$sent" \
    || fail "renvoyée, l'entreprise apporte des événements nouveaux"
  sleep 5
  [ "$(graph_digest)" = "$before" ] || fail "renvoyée, l'entreprise a changé le graphe"
  echo "Renvoyée, l'entreprise ne change rien."
}

down() {
  $COMPOSE down --volumes
}

case "${1:-all}" in
  up | checks | browser | restart | down) "$1" ;;
  all)
    up
    checks
    browser
    restart
    down
    echo "Démonstration vérifiée."
    ;;
  *)
    echo "Usage : $0 [all | up | checks | browser | restart | down]" >&2
    exit 2
    ;;
esac
