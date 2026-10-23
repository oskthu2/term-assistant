#!/usr/bin/env bash
# Körs en gång när Codespace/devcontainern skapas.
set -euo pipefail

echo "== Installerar term-assistant med AI-, PDF- och teststöd"
python -m pip install --upgrade pip >/dev/null
python -m pip install -e '.[dev,ai,pdf]'

echo "== Skapar exempelfiler (examples/*.xlsx, *.pdf)"
python examples/make_examples.py

echo "== Förbereder arbetsytan ./arbetsyta"
mkdir -p arbetsyta/paket
if [ ! -f arbetsyta/settings.json ]; then
  python - <<'PY'
from pathlib import Path
from term_assistant.admin.settings import Editorial, Namespace, Settings, Target

root = Path("arbetsyta")
Settings(
    namespaces=[Namespace(key="exempel", label="Exempel (testmiljö)", base_url="http://example.org/fhir",
                          oid_root="1.2.752.999", publisher="Exempelorganisationen",
                          package_prefix="se.exempel.kodverk")],
    targets=[
        Target(key="paket", label="Inläsningskatalog (arbetsyta/paket)", kind="package-dir",
               directory=str(root.resolve() / "paket"), max_classification="intern"),
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

echo "== Kör testerna"
python -m pytest -q

cat <<'MSG'

Klart! Starta administrationsvyn med:

    term-assistant admin --workspace arbetsyta --host 0.0.0.0 --port 8000

Byt användare (anna/bertil) uppe till höger för att prova granskning och fyra ögon.
MSG
