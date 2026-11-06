"""MCP-server framför en valfri FHIR-terminologitjänst.

Inspirerad av Echidnas MCP-gränssnitt, men fristående: servern ligger framför
vilken FHIR-terminologitjänst som helst (Inera, tx-nordics, tx.fhir.org, en
lokal HAPI-server …) och använder bara standardoperationer. Den känner inte
till inläsningstjänsten eller paketbyggaren – de pratar med samma
terminologitjänst via FHIR, inte med varandra.

Miljövariabler:

    TX_BASE_URL     terminologitjänstens FHIR-bas (standard https://tx.fhir.org/r4)
    TX_TOKEN        valfri bearer-token
    TX_LANGUAGE     föredraget språk för displaytexter (standard sv)
    TX_ALLOW_WRITE  "1" aktiverar förslagsverktygen (propose_valueset, propose_conceptmap,
                    propose_translation, withdraw_proposal). Avstängt som standard – använd mot
                    en testserver. Förslag skriver aldrig över granskat innehåll.

Kör:  term-mcp           (stdio, för Claude Desktop/Code och demo-klienten)
      term-mcp --http    (streamable HTTP)
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import logging
import re
from typing import Any

import httpx2
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .config import ServerConfig
from . import proposals
from .fhir.client import TerminologyClient, TerminologyError

INSTRUCTIONS = """\
Verktyg för terminologiarbete mot en FHIR-terminologitjänst.

Börja med list_codesystems/list_valuesets/find_codesystem för att hitta
canonical URL:er, använd sedan lookup_code, validate_code, expand_valueset,
search_concepts, concept_hierarchy, subsumes och translate_code. Hitta aldrig på
koder – slå upp eller validera dem. validate_valueset kontrollerar alla koder i
en värdemängd.

Om förslagsverktygen finns (propose_valueset, propose_conceptmap,
propose_translation) skapar de förslag på tjänsten: utkast som granskas i
administrationsverktyget innan de släpps. Hämta koderna och deras texter med
verktygen innan du föreslår något, motivera val som inte är självklara, och
säg vad som skapades (canonical URL, antal koder). list_proposals visar
ogranskade förslag."""

mcp = MCPServer("term-mcp", instructions=INSTRUCTIONS)

_client: TerminologyClient | None = None


def tool(fn):
    """Registrera ett verktyg. Fel från terminologitjänsten blir ett verktygsfel med meddelandet kvar,
    så att modellen ser vad som gick fel (andra undantag döljs av MCP-servern)."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (TerminologyError, ValueError) as exc:  # ProposalError är en ValueError
            raise ToolError(str(exc)) from exc
        except httpx2.HTTPError as exc:
            raise ToolError(f"Terminologitjänsten svarar inte ({type(exc).__name__}: {exc})") from exc
    return mcp.tool()(wrapper)


def client() -> TerminologyClient:
    global _client
    if _client is None:
        _client = TerminologyClient(ServerConfig.from_env())
    return _client


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


# -- läsa -----------------------------------------------------------------------------------------

@tool
async def server_info() -> dict:
    """Visa vilken terminologitjänst som används, dess programvara/FHIR-version och om skrivning är på."""
    cfg = client().config
    return {"base_url": cfg.base_url, "write_enabled": cfg.allow_write, **await client().capabilities()}


@tool
async def list_codesystems(query: str | None = None, count: int = 20) -> list[dict]:
    """Lista kodsystem. `query` söker i titel, namn och URL."""
    return [_summary(r) for r in await _search("CodeSystem", query, count)]


@tool
async def find_codesystem(name_or_url: str) -> list[dict]:
    """Hitta canonical URL för ett kodsystem utifrån URL, namn eller titel (t.ex. 'NPU', 'civilstånd')."""
    if re.match(r"^(https?|urn):", name_or_url):
        hits = await client().search("CodeSystem", count=5, url=name_or_url)
        if hits:
            return [_summary(r) for r in hits]
    by_name = await client().search("CodeSystem", count=20, **{"name:contains": name_or_url})
    by_title = await _search("CodeSystem", name_or_url, 20)
    seen, out = set(), []
    for r in by_name + by_title:
        key = (r.get("url"), r.get("version"))
        if key not in seen:
            seen.add(key)
            out.append(_summary(r))
    return out[:20]


@tool
async def list_valuesets(query: str | None = None, count: int = 20) -> list[dict]:
    """Lista värdemängder (ValueSet). `query` söker i titel, namn och URL."""
    return [_summary(r) for r in await _search("ValueSet", query, count)]


@tool
async def list_conceptmaps(query: str | None = None, count: int = 20) -> list[dict]:
    """Lista ConceptMaps (mappningar mellan kodsystem)."""
    hits = await _search("ConceptMap", query, count)
    return [_summary(r) | {k: r.get(k) for k in ("sourceUri", "targetUri", "sourceCanonical", "targetCanonical")
                           if k in r} for r in hits]


@tool
async def lookup_code(system: str, code: str, version: str | None = None) -> dict:
    """Slå upp ett begrepp: display, designations och egenskaper ($lookup). Översättningar i
    CodeSystem-supplement på tjänsten (t.ex. svenska språkpaket och förslag) tas med under `translations`."""
    result = await client().lookup(system, code, version)
    translations = await _supplement_translations(system, code)
    if translations:
        result["translations"] = translations
    return result


async def _supplement_translations(system: str, code: str) -> list[dict]:
    """Alla servrar använder inte supplement i $lookup (HAPI gör det inte), så de läses direkt."""
    try:
        supplements = await client().search("CodeSystem", count=50, supplements=system, _summary="false")
    except TerminologyError:
        return []
    out = []
    for cs in supplements:
        for concept in _flat_concepts(cs.get("concept", [])):
            if concept.get("code") != code:
                continue
            texts = [(d.get("language") or cs.get("language"), d.get("value")) for d in concept.get("designation", [])]
            if not texts and concept.get("display"):
                texts = [(cs.get("language"), concept["display"])]
            for language, value in dict.fromkeys(texts):
                out.append({"language": language, "value": value, "source": cs.get("url"),
                            **({"proposal": True} if proposals.is_proposal(cs) else {})})
    return out


def _flat_concepts(concepts: list[dict]):
    for c in concepts:
        yield c
        yield from _flat_concepts(c.get("concept", []))


@tool
async def validate_code(code: str, system: str | None = None, valueset: str | None = None,
                        display: str | None = None) -> dict:
    """Kontrollera om en kod är giltig i ett kodsystem eller ingår i ett ValueSet ($validate-code)."""
    result = await client().validate_code(code, system=system, valueset=valueset, display=display)
    if valueset and not system and not result.get("result"):
        # Vissa servrar (t.ex. HAPI) kräver system även mot ett ValueSet: hämta det ur expansionen och försök igen.
        try:
            expansion = await client().expand(url=valueset, filter=None, count=1000)
        except TerminologyError:
            return result
        systems = {c.get("system") for c in _flat(expansion["contains"]) if c.get("code") == code}
        if len(systems) == 1:
            retry = await client().validate_code(code, system=systems.pop(), valueset=valueset, display=display)
            return retry | {"note": "system hämtades ur värdemängdens expansion"}
    return result


def _flat(concepts: list[dict]):
    for c in concepts:
        yield c
        yield from _flat(c.get("contains", []))


@tool
async def expand_valueset(url: str, filter: str | None = None, count: int = 50, offset: int = 0) -> dict:
    """Expandera ett ValueSet, valfritt med textfilter och sidning ($expand)."""
    return await client().expand(url=url, filter=filter, count=count, offset=offset)


@tool
async def search_concepts(system: str, text: str, count: int = 20) -> dict:
    """Fritextsök begrepp i ett helt kodsystem (implicit ValueSet + $expand med filter)."""
    vs = {"resourceType": "ValueSet", "compose": {"include": [{"system": system}]}}
    return await client().expand(valueset=vs, filter=text, count=count)


@tool
async def concept_hierarchy(system: str, code: str, version: str | None = None) -> dict:
    """Navigera hierarkin: föräldrar och barn (eller ättlingar) för ett begrepp."""
    return await client().hierarchy(system, code, version)


@tool
async def subsumes(system: str, code_a: str, code_b: str) -> str:
    """Testa subsumption mellan två koder ($subsumes): equivalent, subsumes, subsumed-by, not-subsumed."""
    return await client().subsumes(system, code_a, code_b)


@tool
async def translate_code(code: str, system: str, conceptmap: str | None = None,
                         target_system: str | None = None) -> dict:
    """Översätt en kod till ett annat kodsystem via ConceptMap ($translate)."""
    return await client().translate(code, system, conceptmap=conceptmap, target_system=target_system)


@tool
async def validate_valueset(url: str | None = None, valueset: dict | None = None) -> dict:
    """Validera varje uttryckligen listad kod i ett ValueSet mot sitt kodsystem ($validate-code).
    Ange `url` för ett ValueSet på tjänsten eller `valueset` som en hel resurs."""
    if valueset is None:
        if not url:
            raise ValueError("Ange url eller valueset.")
        valueset = await client().read_canonical("ValueSet", url)
        if valueset is None:
            raise ValueError(f"ValueSet {url} finns inte på tjänsten.")
    return await validate_codes(client(), valueset)


async def validate_codes(tx: TerminologyClient, valueset: dict, concurrency: int = 8) -> dict:
    sem = asyncio.Semaphore(concurrency)

    async def check(system: str, version: str | None, concept: dict) -> dict:
        entry = {"system": system, "code": concept["code"], "local_display": concept.get("display")}
        async with sem:
            try:
                r = await tx.validate_code(concept["code"], system=system, version=version,
                                           display=concept.get("display"))
            except TerminologyError as exc:
                return entry | {"status": "error", "message": str(exc)}
        if not r.get("result"):
            return entry | {"status": "invalid", "message": r.get("message")}
        if r.get("inactive"):
            return entry | {"status": "inactive", "display": r.get("display")}
        return entry | {"status": "ok", "display": r.get("display")}

    results = await asyncio.gather(*(check(inc["system"], inc.get("version"), c)
                                     for inc in valueset.get("compose", {}).get("include", [])
                                     if inc.get("system") for c in inc.get("concept", [])))
    summary: dict[str, int] = {}
    for r in results:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    includes = valueset.get("compose", {}).get("include", [])
    filters = sum(len(inc.get("filter", [])) for inc in includes)
    whole = [inc["system"] for inc in includes if inc.get("system") and not inc.get("concept") and not inc.get("filter")]
    return {"url": valueset.get("url"), "summary": summary, "filters_not_checked": filters,
            "whole_code_systems": whole, "problems": [r for r in results if r["status"] != "ok"]}


# -- förslag (skapas bara med TX_ALLOW_WRITE=1) -------------------------------------------------------

@tool
async def list_proposals() -> list[dict]:
    """Lista förslag (urval, mappningar, översättningar) som skapats på tjänsten och ännu inte granskats."""
    return await proposals.list_all(client())


async def propose_valueset(url: str, name: str, title: str, concepts: list[dict],
                           rationale: str | None = None, version: str = "0.1.0") -> dict:
    """Föreslå ett urval (ValueSet) med uttryckligen listade koder. `concepts`: [{system, code, display?}].
    Koderna kontrolleras först; okända koder gör att inget skapas. Skapas som utkast märkt "förslag"
    och granskas sedan i administrationsverktyget. Samma namn ersätter ett tidigare förslag."""
    return await proposals.valueset(client(), url, name, title, concepts, rationale, version)


async def propose_conceptmap(url: str, name: str, title: str, source_system: str, target_system: str,
                             mappings: list[dict], rationale: str | None = None,
                             source_valueset: str | None = None, target_valueset: str | None = None,
                             version: str = "0.1.0") -> dict:
    """Föreslå en mappning (ConceptMap) från ett kodsystem till ett annat.
    `mappings`: [{source_code, target_code, relation, comment?}] där relation är equivalent (likvärdig),
    wider (målet är bredare), narrower (målet är smalare – kräver comment), related (närliggande – kräver
    comment) eller unmatched (ingen motsvarighet, utan target_code). En källkod kan ha flera mål.
    Med `source_valueset` rapporteras vilka källkoder som saknar mappning. Alla koder kontrolleras först."""
    return await proposals.conceptmap(client(), url, name, title, source_system, target_system, mappings,
                                      rationale, source_valueset, target_valueset, version,
                                      client().config.fhir_version)


async def propose_translation(codesystem: str, language: str, translations: list[dict],
                              url: str | None = None, name: str | None = None, title: str | None = None,
                              rationale: str | None = None) -> dict:
    """Föreslå en översättning av ett kodsystem som finns på tjänsten, som CodeSystem-supplement.
    `translations`: [{code, display}] där display är texten på målspråket `language` (BCP-47, t.ex. sv).
    Översätt utifrån kodsystemets egna displaytexter och definitioner, inte från minnet."""
    return await proposals.translation(client(), codesystem, language, translations, url, name, title, rationale,
                                       proposal_base=client().config.proposal_base)


async def withdraw_proposal(resource_type: str, id: str) -> dict:
    """Ta bort ett förslag (bara resurser märkta som förslag kan tas bort)."""
    return await proposals.withdraw(client(), resource_type, id)


if ServerConfig.from_env().allow_write:
    for _fn in (propose_valueset, propose_conceptmap, propose_translation, withdraw_proposal):
        tool(_fn)


def run(transport: str = "stdio") -> None:
    for name in ("httpx", "httpx2"):  # varje FHIR-anrop loggas annars på stderr
        logging.getLogger(name).setLevel(logging.WARNING)
    mcp.run(transport=transport)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="term-mcp", description="MCP-server framför en FHIR-terminologitjänst")
    parser.add_argument("--http", action="store_true", help="Streamable HTTP i stället för stdio")
    args = parser.parse_args(argv)
    run("streamable-http" if args.http else "stdio")


if __name__ == "__main__":
    main()
