"""Förslag som MCP-servern får skapa på terminologitjänsten: urval, mappningar och översättningar.

Allt som skrivs är *förslag*: ``status = draft``, ``experimental = true`` och
taggen ``urn:term-assistant:arbetsflode#forslag`` i ``meta.tag``. Ett förslag
får skrivas över av ett nytt förslag med samma id, men aldrig en resurs utan
taggen – granskat innehåll ändras bara via administrationsverktyget. Koder
kontrolleras mot tjänsten innan något skrivs; okända koder stoppar förslaget.

==================  ===================================================================
förslag             FHIR
==================  ===================================================================
urval               ValueSet med uttryckligen listade koder
mappning            ConceptMap (R4: equivalence, R5: relationship); källkoder utan
                    motsvarighet blir ``unmatched`` (R4) / ``noMap`` (R5)
översättning        CodeSystem-supplement (``content = supplement``) med översatt
                    display och en designation på målspråket
==================  ===================================================================
"""

from __future__ import annotations

import re
from datetime import date
from .fhir.client import TerminologyClient

PROPOSAL_TAG = {"system": "urn:term-assistant:arbetsflode", "code": "forslag", "display": "Förslag – ej granskat"}
PUBLISHER = "Förslag från term-mcp (AI-stött, ej granskat)"

# Relation från källkod till målkod -> (R4 equivalence, R5 relationship)
RELATIONS = {
    "equivalent": ("equivalent", "equivalent"),
    "wider": ("wider", "source-is-narrower-than-target"),      # målet är bredare än källan
    "narrower": ("narrower", "source-is-broader-than-target"),  # målet är smalare: information går förlorad
    "related": ("relatedto", "related-to"),
    "unmatched": ("unmatched", None),                           # ingen motsvarighet
}
_ALIASES = {"likvardig": "equivalent", "likvärdig": "equivalent", "equal": "equivalent", "bredare": "wider",
            "smalare": "narrower", "relatedto": "related", "related-to": "related", "narliggande": "related",
            "närliggande": "related", "inexact": "related", "ingen": "unmatched", "nomap": "unmatched",
            "source-is-narrower-than-target": "wider", "source-is-broader-than-target": "narrower"}


class ProposalError(ValueError):
    pass


def is_proposal(resource: dict) -> bool:
    """Utkast med förslagstaggen. En släppt resurs (status active) räknas aldrig som förslag, även om en
    server (t.ex. HAPI) behållit taggen när den granskade versionen ersatte förslaget."""
    tagged = any(t.get("system") == PROPOSAL_TAG["system"] and t.get("code") == PROPOSAL_TAG["code"]
                 for t in resource.get("meta", {}).get("tag", []))
    return tagged and resource.get("status") == "draft"


def resource_id(name: str) -> str:
    rid = re.sub(r"[^A-Za-z0-9\-.]+", "-", name).strip("-.")[:64] or "forslag"
    return rid if re.search(r"[A-Za-z]", rid) else f"r-{rid}"[:64]


def relation(value: str | None) -> str:
    key = (value or "equivalent").strip().lower()
    key = _ALIASES.get(key, key)
    if key not in RELATIONS:
        raise ProposalError(f"Okänd relation '{value}'. Använd en av: {', '.join(RELATIONS)}.")
    return key


def _base(resource_type: str, url: str, name: str, title: str, version: str, rationale: str | None) -> dict:
    return {
        "resourceType": resource_type, "id": resource_id(name), "meta": {"tag": [PROPOSAL_TAG]},
        "url": url, "version": version, "name": re.sub(r"\W", "_", name), "title": title,
        "status": "draft", "experimental": True, "date": date.today().isoformat(), "publisher": PUBLISHER,
        "description": f"Förslag: {title}." + (f" Motivering: {rationale}" if rationale else ""),
    }


async def guard_url(tx: TerminologyClient, resource_type: str, url: str, rid: str) -> None:
    """Ett förslag får inte ta en canonical URL som redan används av en granskad resurs eller ett annat förslag."""
    for existing in await tx.search(resource_type, count=20, url=url):
        if not is_proposal(existing):
            raise ProposalError(f"{resource_type} {url} finns redan som granskad resurs (id {existing.get('id')}) "
                                "och ändras bara via administrationsverktyget. Välj en ny URL för förslaget.")
        if existing.get("id") != rid:
            raise ProposalError(f"Förslaget {existing.get('id')} har redan URL:en {url}. Använd samma namn för att "
                                "ersätta det, eller en ny URL.")
    current = await tx.read(resource_type, rid)
    if current is not None and not is_proposal(current):
        raise ProposalError(f"{resource_type}/{rid} finns redan och är inget förslag. Välj ett annat namn.")


async def check_codes(tx: TerminologyClient, system: str, codes: list[str]) -> dict[str, dict]:
    """{kod: {ok, display, inactive, message}} via $validate-code."""
    import asyncio
    sem = asyncio.Semaphore(8)

    async def one(code: str):
        async with sem:
            r = await tx.validate_code(code, system=system)
        return code, {"ok": bool(r.get("result")), "display": r.get("display"), "inactive": bool(r.get("inactive")),
                      "message": r.get("message")}

    return dict(await asyncio.gather(*(one(c) for c in dict.fromkeys(codes))))


def _unknown(system: str, result: dict[str, dict]) -> list[dict]:
    return [{"system": system, "code": c, "message": r["message"]} for c, r in result.items() if not r["ok"]]


async def valueset(tx: TerminologyClient, url: str, name: str, title: str, concepts: list[dict],
                   rationale: str | None = None, version: str = "0.1.0") -> dict:
    by_system: dict[str, list[dict]] = {}
    for c in concepts:
        if not c.get("system") or not c.get("code"):
            raise ProposalError("Varje kod behöver system och code.")
        by_system.setdefault(c["system"], []).append(c)
    unknown, inactive, include = [], [], []
    for system, items in by_system.items():
        result = await check_codes(tx, system, [c["code"] for c in items])
        unknown += _unknown(system, result)
        inactive += [{"system": system, "code": c} for c, r in result.items() if r["ok"] and r["inactive"]]
        include.append({"system": system, "concept": [
            {"code": c["code"], "display": c.get("display") or result[c["code"]]["display"]} for c in items]})
    if unknown:
        return {"created": False, "reason": "Okända koder – inget förslag skapades.", "unknown": unknown}
    vs = _base("ValueSet", url, name, title, version, rationale) | {"compose": {"include": include}}
    await guard_url(tx, "ValueSet", url, vs["id"])
    await tx.put_resource(vs)
    return {"created": True, "resourceType": "ValueSet", "id": vs["id"], "url": url, "version": version,
            "codes": len(concepts), "inactive": inactive}


async def conceptmap(tx: TerminologyClient, url: str, name: str, title: str, source_system: str,
                     target_system: str, mappings: list[dict], rationale: str | None = None,
                     source_valueset: str | None = None, target_valueset: str | None = None,
                     version: str = "0.1.0", fhir_version: str = "R4") -> dict:
    rows = []
    for m in mappings:
        rel = relation(m.get("relation"))
        if not m.get("source_code"):
            raise ProposalError("Varje mappning behöver source_code.")
        if rel != "unmatched" and not m.get("target_code"):
            raise ProposalError(f"{m['source_code']}: target_code saknas (använd relation 'unmatched' om det inte "
                                "finns någon motsvarighet).")
        rows.append({**m, "relation": rel})
    missing_comment = [r["source_code"] for r in rows if r["relation"] in ("narrower", "related") and not r.get("comment")]
    if missing_comment:
        raise ProposalError("Mappningar där målet är smalare eller bara närliggande ska motiveras (comment): "
                            + ", ".join(missing_comment))
    src = await check_codes(tx, source_system, [r["source_code"] for r in rows])
    tgt = await check_codes(tx, target_system, [r["target_code"] for r in rows if r.get("target_code")])
    unknown = _unknown(source_system, src) + _unknown(target_system, tgt)
    if unknown:
        return {"created": False, "reason": "Okända koder – inget förslag skapades.", "unknown": unknown}

    elements: dict[str, dict] = {}
    for r in rows:
        el = elements.setdefault(r["source_code"], {"code": r["source_code"],
                                                    "display": src[r["source_code"]]["display"], "target": []})
        r4, r5 = RELATIONS[r["relation"]]
        if r["relation"] == "unmatched":
            if fhir_version == "R5":
                el["noMap"] = True
                continue
            target = {"equivalence": r4}
        else:
            target = {"code": r["target_code"], "display": tgt[r["target_code"]]["display"]}
            target |= {"relationship": r5} if fhir_version == "R5" else {"equivalence": r4}
        if r.get("comment"):
            target["comment"] = r["comment"]
        el["target"].append(target)
    for el in elements.values():
        if not el["target"]:
            el.pop("target")
    cm = _base("ConceptMap", url, name, title, version, rationale)
    if fhir_version == "R5":
        cm |= {k: v for k, v in {"sourceScopeUri": source_valueset, "targetScopeUri": target_valueset}.items() if v}
    else:
        cm |= {k: v for k, v in {"sourceUri": source_valueset, "targetUri": target_valueset}.items() if v}
    cm["group"] = [{"source": source_system, "target": target_system, "element": list(elements.values())}]
    await guard_url(tx, "ConceptMap", url, cm["id"])
    await tx.put_resource(cm)
    coverage = None
    if source_valueset:
        expansion = await tx.expand(url=source_valueset, count=1000)
        all_codes = {c["code"] for c in expansion["contains"]}
        coverage = {"source_codes": len(all_codes), "mapped": len(all_codes & set(elements)),
                    "not_mapped": sorted(all_codes - set(elements))}
    return {"created": True, "resourceType": "ConceptMap", "id": cm["id"], "url": url, "version": version,
            "mappings": len(rows), "coverage": coverage,
            "inactive": [{"system": source_system, "code": c} for c, r in src.items() if r["inactive"]]
            + [{"system": target_system, "code": c} for c, r in tgt.items() if r["inactive"]]}


async def translation(tx: TerminologyClient, codesystem: str, language: str, translations: list[dict],
                      url: str | None = None, name: str | None = None, title: str | None = None,
                      rationale: str | None = None, version: str = "0.1.0",
                      proposal_base: str = "http://example.org/fhir") -> dict:
    base = await tx.read_canonical("CodeSystem", codesystem)
    if base is None:
        raise ProposalError(f"Kodsystemet {codesystem} finns inte på tjänsten – det går bara att föreslå "
                            "översättningar av kodsystem som finns där.")
    result = await check_codes(tx, codesystem, [t["code"] for t in translations])
    unknown = _unknown(codesystem, result)
    if unknown:
        return {"created": False, "reason": "Okända koder – inget förslag skapades.", "unknown": unknown}
    if not re.fullmatch(r"[a-z]{2,3}(-[A-Za-z0-9]{2,8})*", language):
        raise ProposalError(f"'{language}' är ingen språkkod (BCP-47, t.ex. sv eller nb).")
    base_name = base.get("name") or resource_id(codesystem.rstrip("/").rsplit("/", 1)[-1])
    name = name or f"{base_name}_{language}"
    # Egen namnrymd – aldrig under kodsystemets ägares URL.
    url = url or f"{proposal_base.rstrip('/')}/CodeSystem/{resource_id(name)}"
    cs = _base("CodeSystem", url, name, title or f"{base.get('title') or base_name} ({language})", version,
               rationale) | {
        "language": language, "content": "supplement", "supplements": codesystem,
        "count": len(translations),
        "concept": [{"code": t["code"], "display": t["display"],
                     "designation": [{"language": language, "value": t["display"]}]} for t in translations],
    }
    await guard_url(tx, "CodeSystem", url, cs["id"])
    await tx.put_resource(cs)
    total = base.get("count") or len(base.get("concept", []))
    return {"created": True, "resourceType": "CodeSystem", "id": cs["id"], "url": url, "supplements": codesystem,
            "language": language, "translated": len(translations), "codes_in_codesystem": total,
            "base_displays": {c: r["display"] for c, r in result.items()}}


async def list_all(tx: TerminologyClient) -> list[dict]:
    out = []
    for rtype in ("ValueSet", "ConceptMap", "CodeSystem"):
        for r in await tx.search(rtype, count=100, _tag=f"{PROPOSAL_TAG['system']}|{PROPOSAL_TAG['code']}"):
            if r.get("status") != "draft":
                continue
            out.append({k: r.get(k) for k in ("resourceType", "id", "url", "version", "title", "date")
                        if r.get(k)} | ({"supplements": r["supplements"]} if r.get("supplements") else {}))
    return out


async def withdraw(tx: TerminologyClient, resource_type: str, rid: str) -> dict:
    current = await tx.read(resource_type, rid)
    if current is None:
        raise ProposalError(f"{resource_type}/{rid} finns inte.")
    if not is_proposal(current):
        raise ProposalError(f"{resource_type}/{rid} är inget förslag och kan inte tas bort härifrån.")
    await tx.delete(resource_type, rid)
    return {"withdrawn": True, "resourceType": resource_type, "id": rid, "url": current.get("url")}
