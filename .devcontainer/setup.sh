#!/usr/bin/env bash
# Körs en gång när Codespace/devcontainern skapas.
set -euo pipefail

echo "== Installerar de tre komponenterna (var för sig)"
python -m pip install --upgrade pip >/dev/null
python -m pip install -e './inlasning[dev,ai,pdf]' -e './mcp-server[dev]' -e './paketbyggare[dev]'

echo "== Skapar exempelfiler (inlasning/examples/*.xlsx, *.pdf)"
python inlasning/examples/make_examples.py

echo "== Förbereder inläsningstjänstens arbetsyta ./arbetsyta"
mkdir -p arbetsyta/paket
if [ ! -f arbetsyta/settings.json ]; then
  python - <<'PY'
from pathlib import Path
from term_inlasning.admin.settings import Editorial, Namespace, Settings, Target

root = Path("arbetsyta")
Settings(
    namespaces=[Namespace(key="exempel", label="Exempel (testmiljö)", base_url="http://example.org/fhir",
                          oid_root="1.2.752.999", publisher="Exempelorganisationen",
                          package_prefix="se.exempel.kodverk")],
    targets=[
        Target(key="paket", label="Paketkatalog (arbetsyta/paket – paketbyggaren läser härifrån)",
               kind="package-dir", directory=str(root.resolve() / "paket"), max_classification="intern"),
        Target(key="mock-tx", label="Mockad terminologitjänst (HAPI, localhost:8080)", kind="fhir-api",
               base_url="http://localhost:8080/fhir", max_classification="intern"),
        Target(key="tx-nordics", label="tx-nordics (publik testserver)", kind="fhir-api",
               base_url="https://tx-nordics.fhir.org/fhir", max_classification="öppen"),
    ],
    editorial=Editorial(review_manual_edits=True, four_eyes_release=True),
).save(root)
print("Skrev arbetsyta/settings.json")
PY
fi

echo "== Hämtar HL7:s FHIR-validator (valfritt, ~200 MB)"
JAR="${FHIR_VALIDATOR_JAR:-$HOME/.fhir/validator_cli.jar}"
mkdir -p "$(dirname "$JAR")"
if [ ! -f "$JAR" ]; then
  curl -fsSL -o "$JAR" https://github.com/hapifhir/org.hl7.fhir.core/releases/latest/download/validator_cli.jar \
    || { echo "Kunde inte hämta validatorn – appen fungerar ändå, med den inbyggda kontrollen."; rm -f "$JAR"; }
fi

echo "== Hämtar HAPI-avbildningen för den mockade terminologitjänsten"
docker pull -q hapiproject/hapi:v8.12.0-2 || echo "Kunde inte hämta HAPI nu – mock-tx/starta.sh försöker igen."

echo "== Kör testerna"
for komponent in inlasning mcp-server paketbyggare; do
  (cd "$komponent" && python -m pytest -q)
done

cat <<'MSG'

Klart! Tre fristående komponenter och en mockad terminologitjänst:

  1. Mockad terminologitjänst (HAPI, port 8080), grundladdad med paket från tx-nordics:
       mock-tx/starta.sh

  2. Terminologiassistenten – instruera MCP-servern i webbläsaren (port 8001, kräver ANTHROPIC_API_KEY):
       term-mcp-webb --host 0.0.0.0
     eller i terminalen:  term-mcp-demo --spara utkast.tgz

  3. Administrationsverktyget (port 8000) – granska förslag, förvalta kodverk, urval och mappningar:
       TX_PUBLISH=1 term-inlasning admin --workspace arbetsyta --host 0.0.0.0 --port 8000

  Paketbyggaren:
       term-paket bygg paketbyggare/paket/nordiskt-startpaket.json --ut paketbyggare/dist

Låt portarna vara privata – verktygen har ingen egen inloggning.
Byt användare (anna/bertil) uppe till höger i administrationsverktyget för granskning och fyra ögon.
MSG
