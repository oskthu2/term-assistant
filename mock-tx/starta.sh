#!/usr/bin/env bash
# Starta den mockade terminologitjänsten (HAPI) och grundladda den med terminologipaket.
#
#   mock-tx/starta.sh                      # paketen i paketbyggare/dist (byggs från tx-nordics om de saknas)
#   mock-tx/starta.sh paket.tgz katalog/   # egna paket: filer, kataloger eller URL:er
#   mock-tx/starta.sh --stoppa             # stoppa och ta bort containern (data försvinner)
set -euo pipefail
cd "$(dirname "$0")/.."
COMPOSE="docker compose -f mock-tx/docker-compose.yml"
SERVER="${TX_MOCK_URL:-http://localhost:8080/fhir}"

if [ "${1:-}" = "--stoppa" ]; then
  $COMPOSE down
  exit 0
fi

$COMPOSE up -d
printf "Väntar på HAPI "
for _ in $(seq 1 90); do
  if curl -sf "$SERVER/metadata?_summary=true" > /dev/null; then echo " klar."; break; fi
  printf "."; sleep 5
done
curl -sf "$SERVER/metadata?_summary=true" > /dev/null || { echo " HAPI svarar inte – se: $COMPOSE logs"; exit 1; }

if [ $# -eq 0 ]; then
  if ! ls paketbyggare/dist/*.tgz > /dev/null 2>&1; then
    echo "Inga paket i paketbyggare/dist – bygger startpaketet från tx-nordics."
    term-paket bygg paketbyggare/paket/nordiskt-startpaket.json --ut paketbyggare/dist
  fi
  set -- paketbyggare/dist
fi
term-paket ladda "$@" --server "$SERVER" --vanta 300
echo
echo "Mock-tjänsten: $SERVER"
echo "Prova:  term-mcp-demo --tx $SERVER --verktyg"
