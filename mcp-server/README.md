# MCP-servern (`term-mcp`) och demo-klienten (`term-mcp-demo`)

En av tre fristående komponenter i [term-assistant](../README.md). Servern ligger
framför **valfri FHIR-terminologitjänst** (Ineras, tx-nordics, tx.fhir.org eller den
mockade HAPI-servern i [../mock-tx](../mock-tx/README.md)) och använder bara
standardoperationer. Den är inspirerad av
[Echidnas MCP-gränssnitt](https://fhir.ohdsi.org/docs/getting-started/mcp). Den känner
inte till inläsningstjänsten eller paketbyggaren, utan ser bara det som finns på
terminologitjänsten.

```bash
cd mcp-server
pip install -e '.[demo]'          # demo-klienten behöver anthropic[mcp]
pytest                            # (med .[dev])
```

## Servern

```bash
export TX_BASE_URL=http://localhost:8080/fhir   # eller https://tx-nordics.fhir.org/fhir …
term-mcp                                        # stdio (Claude Desktop/Code, demo-klienten)
term-mcp --http                                 # streamable HTTP
```

| Miljövariabel | Betydelse |
|---|---|
| `TX_BASE_URL` | terminologitjänstens FHIR-bas |
| `TX_TOKEN` | valfri bearer-token |
| `TX_LANGUAGE` | språk för displaytexter (standard `sv`) |
| `TX_ALLOW_WRITE=1` | slår på förslagsverktygen. Avstängt som standard: använd det mot en testserver |
| `TX_PROPOSAL_BASE` | bas för förslagens canonical URL:er när ingen anges (standard `http://example.org/fhir`) |
| `FHIR_VERSION` | `R4` (standard) eller `R5`, styr ConceptMap-formatet i förslag |

| Verktyg | FHIR-operation |
|---|---|
| `server_info` | `GET /metadata` |
| `list_codesystems`, `list_valuesets`, `list_conceptmaps`, `find_codesystem` | sökning |
| `lookup_code` | `CodeSystem/$lookup` |
| `validate_code` | `CodeSystem/$validate-code`, `ValueSet/$validate-code` (hämtar `system` ur expansionen om servern kräver det) |
| `expand_valueset` | `ValueSet/$expand` |
| `search_concepts` | `$expand` av implicit ValueSet med `filter` |
| `concept_hierarchy` | `$lookup` (parent/child), med fallback till `$expand` + `is-a` |
| `subsumes` | `CodeSystem/$subsumes` |
| `translate_code` | `ConceptMap/$translate` |
| `validate_valueset` | `$validate-code` för varje uttryckligen listad kod i ett ValueSet |
| `list_proposals` | sökning på förslagstaggen: ogranskade förslag |
| `propose_valueset` *(förslag)* | urval: ValueSet med uttryckligen listade koder |
| `propose_conceptmap` *(förslag)* | mappning: ConceptMap från ett kodsystem till ett annat, med relation per rad och täckning mot källvärdemängden |
| `propose_translation` *(förslag)* | översättning: CodeSystem-supplement med översatt text och designation på målspråket |
| `withdraw_proposal` *(förslag)* | ta bort ett eget förslag |

`lookup_code` tar också med översättningar från supplement på tjänsten (`translations`), även
på servrar som inte använder supplement själva (HAPI).

### Förslag

MCP-servern kan inte ändra granskat innehåll, bara skapa **förslag**:

- **Märkning:** förslagen skapas som utkast (`status = draft`, `experimental = true`) med taggen
  `urn:term-assistant:arbetsflode#forslag` och utgivaren "Förslag från term-mcp (AI-stött, ej granskat)".
- **Kontroll av koder:** alla koder kontrolleras med `$validate-code` innan något skrivs. En okänd
  kod stoppar förslaget.
- **Motivering:** mappningar där målet är smalare, eller bara närliggande, kräver en kommentar.
  Källkoder utan motsvarighet anges med relationen `unmatched` (R4) respektive `noMap` (R5).
- **Granskat innehåll är skyddat:** ett förslag får aldrig ta en canonical URL eller ett id som en
  granskad resurs redan har. Bara utkast med taggen räknas som förslag.
- **Egna URL:er:** översättningar får URL:er i den egna namnrymden (`TX_PROPOSAL_BASE`), aldrig under
  kodsystemets ägare.

Förslagen granskas i administrationsverktyget (**Förslag från terminologitjänsten**). Där tas
mappningar och urval in som projekt. När den granskade versionen publiceras till
terminologitjänsten ersätter den förslaget.

Fel från terminologitjänsten kommer fram till modellen som verktygsfel med
serverns meddelande, så att den kan rätta sig, till exempel byta canonical URL.

`../.mcp.json` registrerar servern i Claude Code mot tx-nordics.

## Webbgränssnittet (`term-mcp-webb`)

För demo: ett chattgränssnitt där du instruerar MCP-servern och ser varje steg medan det sker.
Du ser Claudes text, verktygsanropen med argument och svar, och förslagen som skapats på
terminologitjänsten, med länkar till resursen och till administrationsverktyget.

```bash
pip install -e '.[webb]'
term-mcp-webb                       # http://127.0.0.1:8001
term-mcp-webb --host 0.0.0.0        # i Codespaces – låt port 8001 vara privat
```

- Varje samtal har en egen MCP-server (underprocess) och en egen dialog, så att man kan fråga vidare.
- Kryssrutan **Förslag tillåtna** startar om samtalet med eller utan förslagsverktygen.
- Exempelinstruktionerna fylls i med ett klick.
- Länken till administrationsverktyget hämtas från `TERM_ADMIN_URL`. I Codespaces räknas den fram
  från den vidarebefordrade porten 8000.

## Demo-klienten

Demo-klienten startar servern som underprocess (stdio), hämtar dess verktyg och
låter Claude (`claude-opus-5-5`) anropa dem i en loop tills instruktionen är utförd.
Allt går genom MCP: klienten pratar aldrig själv med terminologitjänsten.

```bash
term-mcp-demo "Lista civilstånden i HL7 Sveriges värdemängd, med svensk text."
term-mcp-demo                                  # dialog: flera instruktioner, historiken sparas
term-mcp-demo --forslag "Föreslå en mappning från norska civilstånd till HL7 v3 MaritalStatus"
term-mcp-demo --tx https://tx-nordics.fhir.org/fhir "Vad betyder NPU-koden NPU10599?"
```

Utan instruktion startar en dialog med exempel. Varje verktygsanrop visas
(`→ lookup_code(system=…, code=…)`) med en kort sammanfattning av svaret.

- **Terminologitjänst:** `--tx URL`, annars `TX_BASE_URL`, annars `http://localhost:8080/fhir`.
- **Claude:** kräver `ANTHROPIC_API_KEY`, eller en profil från `ant auth login`. Server-side
  fallback är påslaget (`fallbacks: "default"`): om modellen avböjer en begäran tar en annan
  modell över i samma anrop.
- **Utan API-nyckel:** `--verktyg` listar verktygen och `--anropa VERKTYG '{"arg": …}'` anropar
  ett verktyg direkt.

```bash
term-mcp-demo --verktyg
term-mcp-demo --anropa expand_valueset '{"url": "http://hl7.se/fhir/ig/base/ValueSet/SEBaseMaritalStatusVS"}'
term-mcp-demo --anropa translate_code '{"code": "44054006", "system": "http://snomed.info/sct"}'
```

Testerna (`tests/test_demo.py`) kör hela kedjan utan nätverk: en fejkad Claude-API, SDK:ns
tool runner, en riktig MCP-server som underprocess och en fejkad FHIR-server.
