#!/usr/bin/env bash
# Vérifie la démonstration lancée par docker compose.
#
# Depuis un clone neuf : l'explorateur affiche un premier graphe en moins de
# cinq minutes, rien n'écoute hors de la boucle locale, une requête sous un
# autre nom d'hôte est refusée, et les données sont les mêmes après un arrêt
# et une relance.
set -euo pipefail

COMPOSE=${COMPOSE:-docker compose}
URL=http://127.0.0.1:8000
BUDGET_SECONDS=${BUDGET_SECONDS:-300}

fail() {
  echo "ÉCHEC : $1" >&2
  $COMPOSE logs --tail 40 >&2 || true
  exit 1
}

fetch() {
  curl --silent --fail --max-time 5 "$@"
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

started=$(date +%s)
$COMPOSE up --build --detach

# La vérité du premier incident, établie par le générateur sans passer par l'API.
truth=$($COMPOSE run --rm --no-deps seed answerlineage-corpus truth)
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

wait_for_the_graph
fetch "$URL/" | grep --quiet 'id="root"' || fail "l'explorateur n'est pas servi"
elapsed=$(($(date +%s) - started))
echo "Premier graphe en ${elapsed} s : ${EXPECTED_COUNTS} (réponses et destinataires du premier incident)."
[ "$elapsed" -lt "$BUDGET_SECONDS" ] || fail "premier graphe en ${elapsed} s, pour ${BUDGET_SECONDS} s au plus"

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
address=$(hostname -I 2>/dev/null | cut -d ' ' -f 1)
if [ -n "$address" ] && fetch --output /dev/null "http://$address:8000/"; then
  fail "l'API répond sur $address, hors de la boucle locale"
fi

# Une page qui joindrait l'API sous un autre nom d'hôte est refusée.
status=$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 5 \
  --header 'Host: attaquant.example' "$URL/api/v1/graph")
[ "$status" = 403 ] || fail "un nom d'hôte inattendu a reçu le statut $status, et non 403"

# Arrêtée puis relancée, la démonstration retrouve les mêmes données.
before=$(graph_digest)
$COMPOSE down
$COMPOSE up --detach
wait_for_the_graph
# La relance renvoie l'entreprise à l'API : rien ne doit s'y ajouter.
sleep 5
after=$(graph_digest)
[ "$before" = "$after" ] || fail "les données ont changé entre l'arrêt et la relance"
echo "Données identiques après un arrêt et une relance."

$COMPOSE down --volumes
echo "Démonstration vérifiée."
