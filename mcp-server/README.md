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
| `TX_ALLOW_WRITE=1` | slår på skrivverktygen. Avstängt som standard: använd det mot en testserver |

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
| `create_valueset` *(skriv)* | validerar koderna, sedan `PUT ValueSet/<id>`. Okända koder stoppar skapandet |
| `upsert_resource` *(skriv)* | `PUT`/`POST` av CodeSystem, ValueSet eller ConceptMap |

Fel från terminologitjänsten kommer fram till modellen som verktygsfel med
serverns meddelande, så att den kan rätta sig, till exempel byta canonical URL.

`../.mcp.json` registrerar servern i Claude Code mot tx-nordics.

## Demo-klienten

Demo-klienten startar servern som underprocess (stdio), hämtar dess verktyg och
låter Claude (`claude-opus-5-5`) anropa dem i en loop tills instruktionen är utförd.
Allt går genom MCP: klienten pratar aldrig själv med terminologitjänsten.

```bash
term-mcp-demo "Lista civilstånden i HL7 Sveriges värdemängd, med svensk text."
term-mcp-demo                                  # dialog: flera instruktioner, historiken sparas
term-mcp-demo --skriv "Skapa en värdemängd http://example.org/fhir/ValueSet/demo-civilstand med gift och ogift"
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
