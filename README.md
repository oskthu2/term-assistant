# term-assistant

AI-stödd terminologihantering mot **valfri FHIR-terminologitjänst** (Ineras, tx-nordics,
tx.fhir.org eller en lokal HAPI-server). Lösningen består av tre **helt fristående
komponenter**. De delar ingen kod, installeras och testas var för sig och pratar med
varandra bara via FHIR: terminologitjänstens API och FHIR-paket.

| Komponent | Gör | Kommando |
|---|---|---|
| [**Inläsningstjänsten**](inlasning/README.md) | Administrationsverktyget. Läser in kodverk och urval från Excel, CSV eller PDF. Förvaltar kodverk, urval, mappningar (ConceptMap) och översättningar (CodeSystem-supplement) med redaktionell kedja och releaser, granskar förslag från MCP-servern och publicerar releaser som FHIR-paket eller till terminologitjänsten | `term-inlasning` |
| [**MCP-servern**](mcp-server/README.md) | Terminologiverktyg för AI-assistenter framför valfri FHIR-terminologitjänst. Kan skapa *förslag* på urval, mappningar och översättningar. Har ett webbgränssnitt och en terminalklient där Claude utför instruktioner på svenska | `term-mcp`, `term-mcp-webb`, `term-mcp-demo` |
| [**Paketbyggaren**](paketbyggare/README.md) | Bygger versionerade terminologipaket från tx-nordics och inläsningstjänstens releaser, som schemalagt jobb, och grundladdar terminologitjänster | `term-paket` |

Till det kommer en [**mockad terminologitjänst**](mock-tx/README.md) (HAPI FHIR i Docker)
som grundladdas med paketen.

```
 Excel ─► Inläsningstjänsten ──release (.tgz)──► arbetsyta/paket ─┐
                                                                    ├─► Paketbyggaren ──► GitHub Release
 tx-nordics ────────────────────────────────────────────────────────┘    (schemalagt)        │
                                                                                              │ term-paket ladda
 Claude ◄─► term-mcp-webb ◄─MCP─► MCP-servern ◄──FHIR──► terminologitjänst (mock-tx/HAPI) ◄──┘
                                     förslag (utkast) ──►        ▲            │ förslag
                                                                 └─publicera─ Administrationsverktyget ◄┘
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

4. Starta de två webbgränssnitten i varsin terminal och låt portarna vara **privata**, eftersom
   verktygen saknar egen inloggning:

   ```bash
   term-mcp-webb --host 0.0.0.0                                                       # port 8001
   TX_PUBLISH=1 term-inlasning admin --workspace arbetsyta --host 0.0.0.0 --port 8000  # port 8000
   ```

5. **Demo, del 1 – låt AI:n föreslå.** Instruera i terminologiassistenten (port 8001), till exempel:
   - "Föreslå en mappning från norska civilstånd (no-basis-marital-status) till HL7 v3 MaritalStatus.
     Visa tabellen först." Svara sedan "Skapa förslaget."
   - "Föreslå en svensk översättning av de norska familjerelationerna."
   - "Föreslå ett urval av civilstånd för ett vårdregister: gift, ogift, skild och änka/änkling."

   Förslagen skapas som utkast på HAPI och syns i panelen till höger.

6. **Demo, del 2 – granska och förvalta.** I administrationsverktyget (port 8000):
   1. Öppna **Förslag från terminologitjänsten** och välj **Ta in som projekt**.
   2. Rätta mappningarna. Kodfälten söker i målkodsystemet.
   3. Kör **Kontrollera mappningen**.
   4. Skapa en releasekandidat som anna och godkänn den som bertil (byt användare uppe till höger).
   5. Publicera till **Mockad terminologitjänst**.

   Den granskade versionen ersätter då förslaget på HAPI, och assistenten använder den direkt:
   "Översätt den norska koden gift till HL7 v3."

   Översättningsförslag tas in på samma sätt. Översätt de koder som saknas, släpp och publicera.
   Därefter visar assistenten den svenska texten: "Vad heter søsken på svenska?".

   Mappningar och översättningar kan också skapas direkt i verktyget med **Ny mappning** och
   **Ny översättning**. Då läggs en rad upp per käll- respektive kod.

7. **Egna kodverk från Excel.** Läs in `inlasning/examples/eget_kodverk.xlsx` och publicera releasen
   till **Paketkatalog**. Bygg sedan paketet och ladda om mock-tjänsten:

   ```bash
   term-paket bygg paketbyggare/paket/egna-kodverk.json --ut paketbyggare/dist && mock-tx/starta.sh
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
