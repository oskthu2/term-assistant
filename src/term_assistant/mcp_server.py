"""MCP-server som exponerar en valfri FHIR-terminologitjänst och inläsningsflödet.

Inspirerad av Echidnas MCP-gränssnitt, men fristående: servern ligger framför
vilken FHIR-terminologitjänst som helst (Inera, tx.nordics, tx.fhir.org ...)
och använder bara standardoperationer. Välj tjänst med ``TX_BASE_URL``.

Kör:  term-assistant mcp              (stdio, för Claude Desktop/Code)
      term-assistant mcp --http       (streamable HTTP)
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.mcpserver import MCPServer

from .config import ServerConfig
from .fhir.client import TerminologyClient
from .importer.assess import assess_workbook as _assess_workbook
from .importer.build import build_all
from .importer.external import resolve_system_uris
from .importer.mapping import ImportTemplate
from .importer.plan import plan_import
from .importer.registry import PropertyRegistry
from .importer.release import check_policy, diff
from .importer.subset import validate_valueset_codes
from .importer.workbook import read_workbook

INSTRUCTIONS = """\
Verktyg för terminologiarbete mot en FHIR-terminologitjänst och för att migrera
kodverk från Excel till FHIR CodeSystem.

Terminologi: börja med list_codesystems/list_valuesets för att hitta canonical
URL:er, använd sedan lookup_code, validate_code, expand_valueset,
concept_hierarchy, subsumes och translate_code. Hitta aldrig på koder – slå upp
eller validera dem.

Inläsning: assess_workbook (kvalitetsgrind: avvisar filer som inte uppfyller
minimikraven, hittar urval ur befintliga kodsystem, familjer av flikar, katalog
och regler) -> propose_import -> diskutera öppna frågor med användaren ->
confirm_mapping (lär registret) -> build_resources -> för urval:
validate_valueset. Föredra alltid befintliga registerposter framför nya
egenskaper; använd registry_near_duplicates för att hitta synonyma egenskaper."""

mcp = MCPServer("term-assistant", instructions=INSTRUCTIONS)

_client: TerminologyClient | None = None


def client() -> TerminologyClient:
    global _client
    if _client is None:
        _client = TerminologyClient(ServerConfig.from_env())
    return _client


def registry_path() -> str:
    return os.environ.get("TERM_REGISTRY", "registry.json")


def _summary(resource: dict[str, Any]) -> dict[str, Any]:
    keys = ("url", "version", "name", "title", "status", "publisher", "content", "count")
    return {k: resource[k] for k in keys if k in resource}


async def _search(resource_type: str, query: str | None, count: int) -> list[dict[str, Any]]:
    """Sök på titel. Alla servrar stöder inte title:contains, så resultatet filtreras även lokalt."""
    if not query:
        return await client().search(resource_type, count=count)
    hits = await client().search(resource_type, count=max(count, 100), **{"title:contains": query})
    q = query.lower()
    matching = [r for r in hits if any(q in str(r.get(k, "")).lower() for k in ("title", "name", "url"))]
    return matching[:count]


# -- terminologitjänsten ---------------------------------------------------

@mcp.tool()
async def server_info() -> dict:
    """Visa vilken terminologitjänst som används och dess programvara/FHIR-version."""
    return {"base_url": client().config.base_url, **await client().capabilities()}


@mcp.tool()
async def list_codesystems(query: str | None = None, count: int = 20) -> list[dict]:
    """Lista kodsystem. `query` söker i titel (title:contains)."""
    return [_summary(r) for r in await _search("CodeSystem", query, count)]


@mcp.tool()
async def list_valuesets(query: str | None = None, count: int = 20) -> list[dict]:
    """Lista värdemängder (ValueSet). `query` söker i titel."""
    return [_summary(r) for r in await _search("ValueSet", query, count)]


@mcp.tool()
async def list_conceptmaps(query: str | None = None, count: int = 20) -> list[dict]:
    """Lista ConceptMaps (mappningar mellan kodsystem)."""
    hits = await _search("ConceptMap", query, count)
    return [_summary(r) | {k: r.get(k) for k in ("sourceUri", "targetUri", "sourceCanonical", "targetCanonical") if k in r}
            for r in hits]


@mcp.tool()
async def lookup_code(system: str, code: str, version: str | None = None) -> dict:
    """Slå upp ett begrepp: display, designations och egenskaper ($lookup)."""
    return await client().lookup(system, code, version)


@mcp.tool()
async def validate_code(code: str, system: str | None = None, valueset: str | None = None,
                        display: str | None = None) -> dict:
    """Kontrollera om en kod är giltig i ett kodsystem eller ingår i ett ValueSet ($validate-code)."""
    return await client().validate_code(code, system=system, valueset=valueset, display=display)


@mcp.tool()
async def expand_valueset(url: str, filter: str | None = None, count: int = 50, offset: int = 0) -> dict:
    """Expandera ett ValueSet, valfritt med textfilter och sidning ($expand)."""
    return await client().expand(url=url, filter=filter, count=count, offset=offset)


@mcp.tool()
async def search_concepts(system: str, text: str, count: int = 20) -> dict:
    """Fritextsök begrepp i ett helt kodsystem (implicit ValueSet + $expand med filter)."""
    vs = {"resourceType": "ValueSet", "compose": {"include": [{"system": system}]}}
    return await client().expand(valueset=vs, filter=text, count=count)


@mcp.tool()
async def concept_hierarchy(system: str, code: str, version: str | None = None) -> dict:
    """Navigera hierarkin: föräldrar och barn (eller ättlingar) för ett begrepp."""
    return await client().hierarchy(system, code, version)


@mcp.tool()
async def subsumes(system: str, code_a: str, code_b: str) -> str:
    """Testa subsumption mellan två koder ($subsumes): equivalent, subsumes, subsumed-by, not-subsumed."""
    return await client().subsumes(system, code_a, code_b)


@mcp.tool()
async def translate_code(code: str, system: str, conceptmap: str | None = None,
                         target_system: str | None = None) -> dict:
    """Översätt en kod till ett annat kodsystem via ConceptMap ($translate)."""
    return await client().translate(code, system, conceptmap=conceptmap, target_system=target_system)


# -- inläsning av Excel --------------------------------------------------------

@mcp.tool()
def inspect_workbook(path: str, rows: int = 10) -> list[dict]:
    """Visa bladen i en Excel-/CSV-/PDF-fil: rubriker, metadata, grupprubriker, fotnoter och exempelrader."""
    return [{"sheet": s.name, "header_row": s.header_row, "headers": s.headers, "total_rows": s.total_rows,
             "metadata": s.metadata, "column_groups": s.column_groups, "footnotes": s.footnotes,
             "preamble": s.preamble, "sample": s.sample(rows)} for s in read_workbook(path)]


@mcp.tool()
def assess_workbook(path: str) -> dict:
    """Kvalitetsgrind: bedöm varje blad (kodverk, urval, referenslista, textkatalog, dokumentation,
    hjälplista, oanvändbart), med utfall, skäl, varningar och tips om minimikrav. Visar också familjer
    av likadana blad, urvalsrelationer mellan blad (med troliga skrivfel), katalog/OID, historik och regler."""
    wa, _ = _assess_workbook(path)
    return wa.model_dump(mode="json", exclude_none=True)


@mcp.tool()
def propose_import(path: str, base_url: str, sheet: str | None = None, language: str = "sv") -> dict:
    """Föreslå mallar för alla kodverk och urval i filen (en mall per familj av likadana blad).

    Returnerar bedömningen och per mall: mallen, kolumnbeslut, öppna frågor och föreslagna nya egenskaper."""
    plan, _ = plan_import(path, base_url, PropertyRegistry.load(registry_path()), sheet=sheet, language=language)
    return {
        "assessment": plan.assessment.model_dump(mode="json", exclude_none=True),
        "proposals": [p.model_dump(mode="json", exclude_none=True,
                                   exclude={"profiles": {"__all__": {"samples"}}}) for p in plan.proposals],
    }


@mcp.tool()
def build_resources(path: str, template: dict) -> dict:
    """Bygg FHIR-resurser (CodeSystem eller ValueSet, status draft) från fil + mall."""
    resources, warnings = build_all(ImportTemplate.model_validate(template), read_workbook(path))
    return {"resources": resources, "warnings": warnings[:100], "warning_count": len(warnings)}


@mcp.tool()
async def validate_valueset(valueset: dict) -> dict:
    """Validera varje kod i ett ValueSet (t.ex. ett urval ur NPU) mot terminologitjänsten."""
    return await validate_valueset_codes(client(), valueset)


@mcp.tool()
async def resolve_external_systems(keys: list[str]) -> dict:
    """Leta upp canonical URL för externa kodsystem (t.ex. 'npu', 'swe-npu', 'kva') på terminologitjänsten."""
    return await resolve_system_uris(client(), keys)


@mcp.tool()
def compare_versions(old: dict, new: dict, template: dict | None = None) -> dict:
    """Diff mellan två versioner av ett CodeSystem, kontrollerad mot mallens releasepolicy."""
    change = diff(old, new)
    if template:
        change["policy_errors"] = check_policy(change, ImportTemplate.model_validate(template).policy)
    return change


@mcp.tool()
def confirm_mapping(template: dict, add_new_properties: bool = True) -> dict:
    """Bekräfta en (ev. redigerad) mall: registret lär sig rubrikerna som alias och nya egenskaper läggs till."""
    tpl = ImportTemplate.model_validate(template)
    registry = PropertyRegistry.load(registry_path())
    learned, added = [], []
    for prop in tpl.properties:
        if registry.get(prop.code) is None and prop.code != "parent" and add_new_properties:
            registry.add_property(prop, aliases=[c.source_column for c in tpl.columns if c.property_code == prop.code])
            added.append(prop.code)
    for col in tpl.columns:
        if col.property_code and registry.get(col.property_code):
            registry.learn(col.source_column, col.property_code)
            learned.append(f"{col.source_column} -> {col.property_code}")
        elif col.designation and col.designation.use_code:
            key = next((e.key for e in registry.entries
                        if e.designation and e.designation.use_code == col.designation.use_code), None)
            if key:
                registry.learn(col.source_column, key)
                learned.append(f"{col.source_column} -> {key}")
    registry.save(registry_path())
    return {"learned": learned, "added_properties": added, "problems": tpl.validate_template()}


@mcp.tool()
def registry_list() -> list[dict]:
    """Lista egenskapsregistret (kanoniska egenskaper och designation-typer med alias)."""
    return [e.model_dump(exclude_none=True) for e in PropertyRegistry.load(registry_path()).entries]


@mcp.tool()
def registry_near_duplicates(threshold: float = 0.8) -> list[dict]:
    """Hitta registerposter som troligen är synonymer och borde slås ihop."""
    pairs = PropertyRegistry.load(registry_path()).near_duplicates(threshold)
    return [{"a": a, "b": b, "similarity": s} for a, b, s in pairs]


@mcp.tool()
def registry_merge(keep: str, drop: str) -> dict:
    """Slå ihop två synonyma registerposter; `drop` blir alias till `keep`."""
    registry = PropertyRegistry.load(registry_path())
    kept = registry.merge(keep, drop)
    registry.save(registry_path())
    return kept.model_dump(exclude_none=True)


@mcp.tool()
async def registry_harvest(count: int = 50) -> dict:
    """Skörda CodeSystem.property från kodverk på terminologitjänsten in i registret."""
    hits = await client().search("CodeSystem", count=count, _summary=None, _elements="url,name,property")
    registry = PropertyRegistry.load(registry_path())
    added = registry.harvest(hits)
    registry.save(registry_path())
    return {"codesystems_scanned": len(hits), "properties_added": added}


def run(transport: str = "stdio") -> None:
    mcp.run(transport=transport)
