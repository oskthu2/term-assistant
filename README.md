# term-assistant

AI-stödd terminologihantering mot **valfri FHIR-terminologitjänst** (Ineras, tx-nordics,
tx.fhir.org eller en lokal HAPI-server). Lösningen består av tre **helt fristående
komponenter**. De delar ingen kod, installeras och testas var för sig och pratar med
varandra bara via FHIR: terminologitjänstens API och FHIR-paket.

| Komponent | Gör | Kommando |
|---|---|---|
| [**Inläsningstjänsten**](inlasning/README.md) | Läser in kodverk och urval från Excel, CSV eller PDF till mallar. Förvaltar dem i en webbapp med redaktionell kedja och releaser, och publicerar releaser som FHIR-paket | `term-inlasning` |
| [**MCP-servern**](mcp-server/README.md) | Terminologiverktyg för AI-assistenter framför valfri FHIR-terminologitjänst, med en demo-klient där Claude utför instruktioner på svenska | `term-mcp`, `term-mcp-demo` |
| [**Paketbyggaren**](paketbyggare/README.md) | Bygger versionerade terminologipaket från tx-nordics och inläsningstjänstens releaser, som schemalagt jobb, och grundladdar terminologitjänster | `term-paket` |

Till det kommer en [**mockad terminologitjänst**](mock-tx/README.md) (HAPI FHIR i Docker)
som grundladdas med paketen.

```
 Excel ─► Inläsningstjänsten ──release (.tgz)──► arbetsyta/paket ─┐
                                                                    ├─► Paketbyggaren ──► GitHub Release
 tx-nordics ────────────────────────────────────────────────────────┘    (schemalagt)        │
                                                                                              │ term-paket ladda
 Claude ◄─► term-mcp-demo ◄─MCP─► MCP-servern ◄──FHIR──► terminologitjänst (mock-tx/HAPI) ◄──┘
```

Designen beskrivs i [docs/arkitektur.md](docs/arkitektur.md).

## Prova i GitHub Codespaces

1. Lägg in din Claude-nyckel som Codespaces-hemlighet: **GitHub → Settings → Codespaces →
   Secrets → New secret**, namn `ANTHROPIC_API_KEY`, och ge den åtkomst till repot. Klistra
   aldrig in nyckeln i en fil eller i koden.
2. **Code → Codespaces → Create codespace** på grenen. `.devcontainer/setup.sh` installerar de
   tre komponenterna, skapar exempelfiler och en arbetsyta, hämtar HL7-validatorn och
   HAPI-avbildningen, och kör testerna.
3. Starta den mockade terminologitjänsten. Startpaketet byggs från tx-nordics och laddas in:

   ```bash
   mock-tx/starta.sh
   term-paket bygg paketbyggare/paket/npu.json --ut paketbyggare/dist && mock-tx/starta.sh   # valfritt: hela NPU
   ```

4. Instruera MCP-servern via demo-klienten (`TX_BASE_URL` pekar redan på mock-tjänsten):

   ```bash
   term-mcp-demo "Lista civilstånden i HL7 Sveriges värdemängd och förklara de svenska tilläggen."
   term-mcp-demo "Översätt SNOMED CT-koden 44054006 till ICD-10."
   term-mcp-demo --skriv "Skapa en värdemängd http://example.org/fhir/ValueSet/demo-civilstand med gift och ogift."
   term-mcp-demo          # dialog
   ```

5. Starta inläsningstjänsten och låt porten vara **privat**, eftersom vyn saknar egen
   inloggning:

   ```bash
   term-inlasning admin --workspace arbetsyta --host 0.0.0.0 --port 8000
   ```

   Läs in `inlasning/examples/eget_kodverk.xlsx`, skapa en release (byt användare anna/bertil
   uppe till höger för fyra ögon) och publicera den till **Paketkatalog**. Bygg sedan
   `paketbyggare/paket/egna-kodverk.json` och kör `mock-tx/starta.sh` igen. Nu hittar
   MCP-servern ditt kodverk:

   ```bash
   term-paket bygg paketbyggare/paket/egna-kodverk.json --ut paketbyggare/dist
   mock-tx/starta.sh
   term-mcp-demo "Vilka besökstyper finns i KV_BESOKSTYP, och vad ersätter telefonkontakt?"
   ```

## Utveckla

Varje komponent är ett eget Python-paket med egna tester:

```bash
for k in inlasning mcp-server paketbyggare; do (cd $k && pip install -e '.[dev]' && pytest -q); done
```

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) testar komponenterna var för sig.
Det schemalagda paketbygget ligger i
[`.github/workflows/terminologipaket.yml`](.github/workflows/terminologipaket.yml).

```
inlasning/       inläsningstjänsten: importer/, admin/ (webbapp), examples/, tests/
mcp-server/      MCP-servern och demo-klienten
paketbyggare/    paketbyggaren, paketbeskrivningar (paket/) och mall för distributionsrepo (mall-repo/)
mock-tx/         HAPI FHIR i Docker + startskript för grundladdning
docs/            arkitektur och designbeslut
```
