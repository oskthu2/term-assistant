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
| `TX_TOKEN` | valfri statisk bearer-token |
| `TX_TOKEN_URL`, `TX_CLIENT_ID`, `TX_CLIENT_SECRET`, `TX_SCOPE` | OAuth2 client credentials: token hämtas och förnyas automatiskt. Hemligheten läggs som miljövariabel eller Codespaces-hemlighet, aldrig i en fil |
| `TX_SNOMED_EDITION` | SNOMED CT-edition när ingen version anges: `se` (svenska, 45991000052106), `no`, `dk`, `fi`, `int`, ett modul-id eller en hel edition-URI med datum |
| `TX_LANGUAGE` | språk för displaytexter (standard `sv`) |
| `TX_PROPOSAL_BASE` | bas för utkastens canonical URL:er när ingen anges (standard `http://example.org/fhir`) |
| `FHIR_VERSION` | `R4` (standard) eller `R5`, styr ConceptMap-formatet i utkast |

Servern **läser bara**. Den behöver ingen skrivrätt på terminologitjänsten, och ingen
inloggning alls om tjänsten är öppen (tx-nordics, tx.fhir.org).

| Verktyg | FHIR-operation |
|---|---|
| `server_info` | `GET /metadata` |
| `list_codesystems`, `list_valuesets`, `list_conceptmaps`, `find_codesystem` | sökning |
| `lookup_code` | `CodeSystem/$lookup` |
| `validate_code` | `CodeSystem/$validate-code`, `ValueSet/$validate-code` (hämtar `system` ur expansionen om servern kräver det) |
| `expand_valueset` | `ValueSet/$expand` |
| `search_concepts` | `$expand` av implicit ValueSet med `filter` |
| `snomed_ecl` | `$expand` med ECL (`constraint`), valfritt med sökterm: SNOMED CT avgränsat till en del av hierarkin |
| `concept_hierarchy` | `$lookup` (parent/child), med fallback till `$expand` + `is-a` |
| `subsumes` | `CodeSystem/$subsumes` |
| `translate_code` | `ConceptMap/$translate` |
| `validate_valueset` | `$validate-code` för varje uttryckligen listad kod i ett ValueSet |
| `draft_valueset` *(utkast)* | urval: ValueSet med uttryckligen listade koder och/eller ett ECL-uttryck för SNOMED CT (med expansionen som ögonblicksbild) |
| `draft_conceptmap` *(utkast)* | mappning: ConceptMap från ett kodsystem till ett annat, med relation per rad och täckning mot källvärdemängden |
| `draft_translation` *(utkast)* | översättning: CodeSystem-supplement med översatt text och designation på målspråket |
| `list_drafts`, `get_draft`, `export_drafts`, `discard_draft` | utkasten i sessionen: lista, hämta en, alla som FHIR-Bundle, ta bort |

| MCP-resurs | Innehåll |
|---|---|
| `utkast://bundle` | alla utkast som FHIR-Bundle (collection) |
| `utkast://{typ}/{id}` | ett utkast som FHIR-resurs |

`lookup_code` tar också med översättningar från supplement på tjänsten (`translations`), även
på servrar som inte använder supplement själva (HAPI).

### Utkast

MCP-servern skriver ingenting. Urval, mappningar och översättningar blir **utkast** som sparas i
serverns minne och hämtas som fil:

- **Märkning:** utkasten är `status = draft`, `experimental = true`, har taggen
  `urn:term-assistant:arbetsflode#forslag` och utgivaren "Förslag från term-mcp (AI-stött, ej granskat)".
- **Kontroll av koder:** alla koder kontrolleras med `$validate-code` (ECL med `$expand`) innan
  utkastet skapas. En okänd kod stoppar det.
- **Motivering:** mappningar där målet är smalare, eller bara närliggande, kräver en kommentar.
  Källkoder utan motsvarighet anges med relationen `unmatched` (R4) respektive `noMap` (R5).
- **Granskat innehåll är skyddat:** ett utkast får inte ta en canonical URL som en granskad resurs
  på terminologitjänsten redan har.
- **Egna URL:er:** översättningar får URL:er i den egna namnrymden (`TX_PROPOSAL_BASE`), aldrig under
  kodsystemets ägare.
- **Livslängd:** utkasten finns så länge MCP-servern kör. I webbgränssnittet och demo-klienten är
  det ett samtal. Hämta dem innan samtalet avslutas.

Utkasten hämtas som **FHIR-paket (.tgz)** eller **FHIR-Bundle (.json)** och tas in i
administrationsverktyget under **Förslag och utkast → Ta in fil**, där urval, mappningar och
översättningar blir projekt som granskas i den redaktionella kedjan.

Fel från terminologitjänsten kommer fram till modellen som verktygsfel med
serverns meddelande, så att den kan rätta sig, till exempel byta canonical URL.

### SNOMED CT på Ontoserver (Inera, tx-nordics)

```bash
export TX_BASE_URL=https://tx-nordics.fhir.org/fhir   # Ontoserver
export TX_SNOMED_EDITION=se                           # svensk edition: svenska termer
term-mcp-webb --host 0.0.0.0
```

- **Svenska termer:** utan `TX_SNOMED_EDITION` svarar Ontoserver med sin standardedition, ofta
  den internationella. Editionen skickas i alla SNOMED-anrop där ingen version anges.
- **ECL:** `snomed_ecl` söker inom en del av hierarkin, t.ex. `<< 404684003` (kliniska fynd)
  med söktermen "typ 2". `concept_hierarchy` visar föräldrar och barn med svensk text.
- **Urval med ECL:** `draft_valueset` med `ecl` skapar ett intensionellt ValueSet. Expansionen
  sparas i utkastet (upp till 1000 begrepp), med edition och datum, och administrationsverktyget
  tar in det som kodlista.
- **Bara läsning:** inget skrivs till Ontoserver. Utkasten hämtas som fil och tas in i
  administrationsverktyget.
- **Inloggning:** Ineras tjänst och andra skyddade Ontoserver-installationer använder OAuth2.
  Sätt `TX_TOKEN_URL`, `TX_CLIENT_ID` och `TX_CLIENT_SECRET`, så hämtas och förnyas tokenen.
- **Licens:** SNOMED CT får användas i Sverige under Socialstyrelsens medlemslicens. Stäm av
  med Socialstyrelsen innan SNOMED-innehåll skickas till en extern AI-tjänst i drift.

`../.mcp.json` registrerar servern i Claude Code mot tx-nordics med svensk SNOMED CT-edition.

## Webbgränssnittet (`term-mcp-webb`)

För demo: ett chattgränssnitt där du instruerar MCP-servern och ser varje steg medan det sker.
Du ser Claudes text, verktygsanropen med argument och svar, och utkasten som assistenten har gjort.

```bash
pip install -e '.[webb]'
term-mcp-webb                       # http://127.0.0.1:8001
term-mcp-webb --host 0.0.0.0        # i Codespaces – låt port 8001 vara privat
```

- Varje samtal har en egen MCP-server (underprocess) och en egen dialog, så att man kan fråga vidare.
- Panelen **Utkast** listar urval, mappningar och översättningar i samtalet. **Hämta FHIR-paket
  (.tgz)** och **Hämta FHIR-Bundle (.json)** laddar ner alla, och varje utkast kan hämtas för sig.
  Filen tas in i administrationsverktyget under **Förslag och utkast**.
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
term-mcp-demo --spara utkast.tgz "Gör ett utkast till mappning från norska civilstånd till HL7 v3 MaritalStatus"
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
