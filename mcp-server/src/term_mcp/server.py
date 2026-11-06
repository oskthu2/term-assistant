"""MCP-server framför en valfri FHIR-terminologitjänst.

Inspirerad av Echidnas MCP-gränssnitt, men fristående: servern ligger framför
vilken FHIR-terminologitjänst som helst (Inera, tx-nordics, tx.fhir.org, en
lokal HAPI-server …) och använder bara standardoperationer. Den känner inte
till inläsningstjänsten eller paketbyggaren – de pratar med samma
terminologitjänst via FHIR, inte med varandra.

Miljövariabler:

    TX_BASE_URL     terminologitjänstens FHIR-bas (standard https://tx.fhir.org/r4)
    TX_TOKEN        valfri bearer-token; eller TX_TOKEN_URL/TX_CLIENT_ID/TX_CLIENT_SECRET/TX_SCOPE
                    för OAuth2 client credentials med automatisk förnyelse
    TX_SNOMED_EDITION  SNOMED CT-edition när ingen version anges (t.ex. se = 45991000052106)
    TX_LANGUAGE     föredraget språk för displaytexter (standard sv)

Servern behöver bara läsrätt. Urval, mappningar och översättningar byggs som *utkast*
(draft_valueset, draft_conceptmap, draft_translation): koderna kontrolleras mot tjänsten
och resursen sparas i serverns minne, där den hämtas som MCP-resurs (utkast://…), som
FHIR-Bundle eller som FHIR-paket och tas in i administrationsverktyget. Inget skrivs.

Kör:  term-mcp           (stdio, för Claude Desktop/Code och demo-klienten)
      term-mcp --http    (streamable HTTP)
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import logging
import re
from typing import Any

import httpx2
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .config import SNOMED, ServerConfig
from . import drafts
from .fhir.client import TerminologyClient, TerminologyError

INSTRUCTIONS = """\
Verktyg för terminologiarbete mot en FHIR-terminologitjänst.

Börja med list_codesystems/list_valuesets/find_codesystem för att hitta
canonical URL:er, använd sedan lookup_code, validate_code, expand_valueset,
search_concepts, concept_hierarchy, subsumes och translate_code. Hitta aldrig på
koder – slå upp eller validera dem. validate_valueset kontrollerar alla koder i
en värdemängd.

SNOMED CT (system http://snomed.info/sct): sök med snomed_ecl och avgränsa till
rätt del av hierarkin, t.ex. "<< 404684003" (kliniska fynd) eller "<< 71388002"
(procedurer), gärna med en sökterm. Fritext över hela SNOMED CT ger mycket brus.
Termerna kommer från den edition server_info visar (svenska om den är satt).
Servern skriver aldrig till terminologitjänsten. Urval, mappningar och
översättningar byggs som utkast (draft_valueset, draft_conceptmap,
draft_translation): koderna kontrolleras och resursen sparas här, så att
användaren kan hämta den som FHIR-Bundle eller FHIR-paket och ta in den i
administrationsverktyget för granskning. Hämta koderna och deras texter med
verktygen innan du gör ett utkast, motivera val som inte är självklara och säg
vad som skapades (canonical URL, antal koder). list_drafts visar utkasten,
export_drafts ger dem som Bundle. Ett urval ur SNOMED CT görs helst med ett
ECL-uttryck (draft_valueset med ecl) i stället för en lång kodlista."""

mcp = MCPServer("term-mcp", instructions=INSTRUCTIONS)

_client: TerminologyClient | None = None
DRAFTS = drafts.DraftStore()  # utkasten lever så länge MCP-servern (en per samtal i klienterna)


def tool(fn):
    """Registrera ett verktyg. Fel från terminologitjänsten blir ett verktygsfel med meddelandet kvar,
    så att modellen ser vad som gick fel (andra undantag döljs av MCP-servern)."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (TerminologyError, ValueError) as exc:  # drafts.ProposalError är en ValueError
            raise ToolError(str(exc)) from exc
        except httpx2.HTTPError as exc:
            raise ToolError(f"Terminologitjänsten svarar inte ({type(exc).__name__}: {exc})") from exc
    return mcp.tool()(wrapper)


def client() -> TerminologyClient:
    """Terminologitjänsten som läses och som koder kontrolleras mot."""
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
    """Visa vilken terminologitjänst som används, dess programvara/FHIR-version och SNOMED CT-edition."""
    cfg = client().config
    return {"base_url": cfg.base_url, "access": "endast läsning", "drafts": len(DRAFTS.all()),
            "snomed_edition": cfg.snomed_edition or "serverns standard", "language": cfg.language,
            **await client().capabilities()}


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
    if system == SNOMED:
        result = _compact_snomed(result)
    translations = await _supplement_translations(system, code)
    if translations:
        result["translations"] = translations
    return result


def _compact_snomed(result: dict) -> dict:
    """$lookup för SNOMED CT ger alla barn och samma term flera gånger (en per språkreferensmängd).
    Barnen räknas (concept_hierarchy visar dem med text) och termerna slås ihop per språk."""
    props = [p for p in result.get("property", []) if isinstance(p, dict)]
    children = [p.get("value") for p in props if p.get("code") == "child"]
    out = {k: v for k, v in result.items() if k not in ("property", "designation")}
    out["parents"] = [p.get("value") for p in props if p.get("code") == "parent"]
    out["children_count"] = len(children)
    for p in props:
        if p.get("code") not in ("parent", "child"):
            out.setdefault("properties", {})[p.get("code")] = p.get("value")
    terms, seen = [], set()
    for d in result.get("designation", []):
        if not isinstance(d, dict) or not d.get("value"):
            continue
        language = (d.get("language") or "").split("-x-")[0]
        use = d.get("use") or {}
        kind = "föredragen" if use.get("code") in ("preferredForLanguage", "display") else use.get("display")
        key = (language, d["value"])
        if key in seen:
            if kind == "föredragen":
                next(t for t in terms if (t["language"], t["value"]) == key)["use"] = kind
            continue
        seen.add(key)
        terms.append({"language": language, "value": d["value"], "use": kind})
    out["terms"] = terms
    return out


async def _supplement_translations(system: str, code: str) -> list[dict]:
    """Alla servrar använder inte supplement i $lookup (HAPI gör det inte), så de läses direkt.
    Översättningsutkast i den här sessionen tas också med (markerade som förslag)."""
    try:
        supplements = await client().search("CodeSystem", count=50, supplements=system, _summary="false")
    except (TerminologyError, httpx2.HTTPError):
        supplements = []
    supplements += [d for d in DRAFTS.all() if d.get("supplements", "").split("|")[0] == system]
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
                            **({"proposal": True} if drafts.is_proposal(cs) else {})})
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
    """Expandera ett ValueSet, valfritt med textfilter och sidning ($expand). Fungerar också med
    implicita värdemängder, t.ex. http://snomed.info/sct?fhir_vs=isa/73211009."""
    return await client().expand(url=url, filter=filter, count=count, offset=offset)


@tool
async def search_concepts(system: str, text: str, count: int = 20) -> dict:
    """Fritextsök begrepp i ett helt kodsystem (implicit ValueSet + $expand med filter).
    För SNOMED CT: använd hellre snomed_ecl med en avgränsning i hierarkin."""
    vs = {"resourceType": "ValueSet", "compose": {"include": [{"system": system}]}}
    return await client().expand(valueset=vs, filter=text, count=count)


@tool
async def snomed_ecl(ecl: str, text: str | None = None, count: int = 50, offset: int = 0) -> dict:
    """Sök i SNOMED CT med ECL (Expression Constraint Language), valfritt med en sökterm.
    Exempel: "<< 73211009" (diabetes och alla undertyper), "< 404684003" (kliniska fynd),
    "<! 73211009" (bara direkta barn), "<< 71388002 : 363704007 = << 80891009" (procedurer på hjärtat),
    "^ 721144007" (medlemmar i en referensmängd). `total` anger hur många begrepp uttrycket ger."""
    if not ecl.strip():
        raise ValueError("Ange ett ECL-uttryck, t.ex. << 404684003.")
    result = await client().ecl(ecl.strip(), filter=text, count=count, offset=offset)
    return {"ecl": ecl.strip(), "edition": client().config.snomed_edition or "serverns standard"} | result


@tool
async def concept_hierarchy(system: str, code: str, version: str | None = None) -> dict:
    """Navigera hierarkin: föräldrar och barn (eller ättlingar) för ett begrepp."""
    if system == SNOMED and not version:
        try:
            parents, children = await asyncio.gather(client().ecl(f">! {code}", count=50),
                                                     client().ecl(f"<! {code}", count=100))
        except TerminologyError:  # servern stöder inte ECL: föräldrar och barn ur $lookup i stället
            return await client().hierarchy(system, code, version)
        info = await client().lookup(system, code)
        return {"code": code, "display": info.get("display"), "version": info.get("version"),
                "parents": [{"code": c["code"], "display": c.get("display")} for c in parents["contains"]],
                "children": [{"code": c["code"], "display": c.get("display")} for c in children["contains"]],
                "children_total": children.get("total")}
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


# -- utkast (inget skrivs till terminologitjänsten) --------------------------------------------------

def _keep(result: dict) -> dict:
    """Spara utkastet och svara med en sammanfattning – resursen kan vara stor (t.ex. en ECL-expansion)."""
    resource = result.pop("resource", None)
    if resource is not None:
        result["draft"] = DRAFTS.put(resource)
        result["next"] = "Hämta som FHIR-Bundle eller paket i klienten, eller med export_drafts."
    return result


@tool
async def draft_valueset(url: str, name: str, title: str, concepts: list[dict] | None = None,
                         ecl: str | None = None, rationale: str | None = None, version: str = "0.1.0") -> dict:
    """Gör ett utkast till urval (ValueSet): uttryckligen listade koder (`concepts`: [{system, code, display?}])
    och/eller, för SNOMED CT, ett ECL-uttryck (`ecl`, t.ex. "<< 73211009") som definierar urvalet.
    Koderna och uttrycket kontrolleras först; okända koder eller ett ogiltigt uttryck gör att inget
    utkast skapas. Utkastet sparas här (inget skrivs till tjänsten) och granskas i administrations-
    verktyget. Samma namn ersätter ett tidigare utkast."""
    return _keep(await drafts.valueset(client(), url, name, title, concepts or [], rationale, version, ecl=ecl))


@tool
async def draft_conceptmap(url: str, name: str, title: str, source_system: str, target_system: str,
                           mappings: list[dict], rationale: str | None = None,
                           source_valueset: str | None = None, target_valueset: str | None = None,
                           version: str = "0.1.0") -> dict:
    """Gör ett utkast till mappning (ConceptMap) från ett kodsystem till ett annat.
    `mappings`: [{source_code, target_code, relation, comment?}] där relation är equivalent (likvärdig),
    wider (målet är bredare), narrower (målet är smalare – kräver comment), related (närliggande – kräver
    comment) eller unmatched (ingen motsvarighet, utan target_code). En källkod kan ha flera mål.
    Med `source_valueset` rapporteras vilka källkoder som saknar mappning. Alla koder kontrolleras först."""
    return _keep(await drafts.conceptmap(client(), url, name, title, source_system, target_system, mappings,
                                         rationale, source_valueset, target_valueset, version,
                                         client().config.fhir_version))


@tool
async def draft_translation(codesystem: str, language: str, translations: list[dict],
                            url: str | None = None, name: str | None = None, title: str | None = None,
                            rationale: str | None = None) -> dict:
    """Gör ett utkast till översättning av ett kodsystem som finns på tjänsten, som CodeSystem-supplement.
    `translations`: [{code, display}] där display är texten på målspråket `language` (BCP-47, t.ex. sv).
    Översätt utifrån kodsystemets egna displaytexter och definitioner, inte från minnet."""
    return _keep(await drafts.translation(client(), codesystem, language, translations, url, name, title,
                                          rationale, proposal_base=client().config.proposal_base))


@tool
async def list_drafts() -> list[dict]:
    """Lista utkasten i den här sessionen (urval, mappningar, översättningar) med sammanfattning."""
    return DRAFTS.summaries()


@tool
async def get_draft(resource_type: str, id: str) -> dict:
    """Hämta ett utkast som hel FHIR-resurs."""
    resource = DRAFTS.get(resource_type, id)
    if resource is None:
        raise ValueError(f"Inget utkast {resource_type}/{id}. list_drafts visar utkasten.")
    return resource


@tool
async def export_drafts() -> dict:
    """Alla utkast som en FHIR-Bundle (collection), att spara och ta in i administrationsverktyget."""
    if not DRAFTS.all():
        raise ValueError("Det finns inga utkast att exportera.")
    return drafts.bundle(DRAFTS.all())


@tool
async def discard_draft(resource_type: str, id: str) -> dict:
    """Ta bort ett utkast ur sessionen."""
    if not DRAFTS.remove(resource_type, id):
        raise ValueError(f"Inget utkast {resource_type}/{id}.")
    return {"discarded": True, "resourceType": resource_type, "id": id}


@mcp.resource("utkast://bundle", name="utkast", title="Alla utkast som FHIR-Bundle",
              mime_type="application/fhir+json")
def drafts_bundle() -> str:
    return json.dumps(drafts.bundle(DRAFTS.all()), ensure_ascii=False)


@mcp.resource("utkast://{resource_type}/{id}", name="utkast-resurs", title="Ett utkast som FHIR-resurs",
              mime_type="application/fhir+json")
def draft_resource(resource_type: str, id: str) -> str:
    resource = DRAFTS.get(resource_type, id)
    if resource is None:
        raise ValueError(f"Inget utkast {resource_type}/{id}.")
    return json.dumps(resource, ensure_ascii=False)


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
