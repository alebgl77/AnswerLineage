#!/usr/bin/env bash
# Lance une commande et la retente deux fois si elle échoue.
#
#   scripts/retry.sh COMMANDE [ARGUMENT…]
#
# Un registre public limite parfois le débit des tirages anonymes (statut 429) :
# une construction d'image échoue alors sans que rien du dépôt soit en cause. La
# commande est retentée après 30 puis 60 secondes (RETRY_DELAY_SECONDS règle la
# première attente, les suivantes en sont des multiples) ; au troisième échec, son
# code de sortie est rendu tel quel. Ses arguments lui sont passés sans changement.
set -euo pipefail

if [ $# -eq 0 ]; then
  echo "Usage : $0 COMMANDE [ARGUMENT…]" >&2
  exit 2
fi

delay=${RETRY_DELAY_SECONDS:-30}
attempt=1
while true; do
  status=0
  "$@" || status=$?
  [ "$status" -ne 0 ] || exit 0
  [ "$attempt" -lt 3 ] || exit "$status"
  echo "Échec (essai $attempt sur 3, code $status), nouvel essai dans $((attempt * delay)) s." >&2
  sleep $((attempt * delay))
  attempt=$((attempt + 1))
done
