"""Utkast som MCP-servern bygger: urval, mappningar och översättningar – utan att skriva något.

MCP-servern behöver bara läsrätt på terminologitjänsten. Ett utkast byggs som en
FHIR-resurs, koderna kontrolleras med ``$validate-code`` (och ECL med ``$expand``)
och resursen sparas i serverns minne. Därifrån hämtas den som MCP-resurs
(``utkast://…``), som FHIR-Bundle eller som FHIR-paket (.tgz) och tas in i
administrationsverktyget för granskning. Inget skrivs till terminologitjänsten.

Utkasten är märkta som förslag: ``status = draft``, ``experimental = true`` och
taggen ``urn:term-assistant:arbetsflode#forslag``. Ett utkast får inte ta en
canonical URL som en granskad resurs på terminologitjänsten redan har.

==================  ===================================================================
utkast              FHIR
==================  ===================================================================
urval               ValueSet med uttryckligen listade koder och/eller ett ECL-uttryck för SNOMED CT
                    (``filter`` med ``constraint``); expansionen sparas som ögonblicksbild
mappning            ConceptMap (R4: equivalence, R5: relationship); källkoder utan
                    motsvarighet blir ``unmatched`` (R4) / ``noMap`` (R5)
översättning        CodeSystem-supplement (``content = supplement``) med översatt
                    display och en designation på målspråket
==================  ===================================================================
"""

from __future__ import annotations

import io
import json
import re
import tarfile
import time
from datetime import date, datetime, timezone

from .config import SNOMED
from .fhir.client import TerminologyClient, TerminologyError

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


class DraftStore:
    """Utkasten i den här MCP-serverns minne, i den ordning de skapades. Samma typ och id ersätter."""

    def __init__(self):
        self._drafts: dict[tuple[str, str], dict] = {}

    def put(self, resource: dict) -> str:
        key = (resource["resourceType"], resource["id"])
        self._drafts.pop(key, None)
        self._drafts[key] = resource
        return uri(resource)

    def get(self, resource_type: str, rid: str) -> dict | None:
        return self._drafts.get((resource_type, rid))

    def remove(self, resource_type: str, rid: str) -> bool:
        return self._drafts.pop((resource_type, rid), None) is not None

    def all(self) -> list[dict]:
        return list(self._drafts.values())

    def summaries(self) -> list[dict]:
        return [summary(r) for r in self.all()]


def uri(resource: dict) -> str:
    return f"utkast://{resource['resourceType']}/{resource['id']}"


def summary(r: dict) -> dict:
    out = {k: r.get(k) for k in ("resourceType", "id", "url", "version", "title") if r.get(k)} | {"uri": uri(r)}
    if r["resourceType"] == "ValueSet":
        out["codes"] = sum(len(i.get("concept", [])) for i in r.get("compose", {}).get("include", []))
        ecl = [f["value"] for i in r.get("compose", {}).get("include", []) for f in i.get("filter", [])]
        if ecl:
            out["ecl"] = ecl
            out["ecl_concepts"] = r.get("expansion", {}).get("total")
    elif r["resourceType"] == "ConceptMap":
        groups = r.get("group", [])
        out["source"] = groups[0].get("source") if groups else None
        out["target"] = groups[0].get("target") if groups else None
        out["mappings"] = sum(len(g.get("element", [])) for g in groups)
    elif r["resourceType"] == "CodeSystem":
        out |= {"supplements": r.get("supplements"), "language": r.get("language"),
                "translated": len(r.get("concept", []))}
    return out


def bundle(resources: list[dict]) -> dict:
    """Utkasten som en FHIR-Bundle (collection) – kan tas in i administrationsverktyget."""
    return {"resourceType": "Bundle", "type": "collection",
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "entry": [{"fullUrl": f"{r['url']}|{r.get('version', '')}".rstrip("|"), "resource": r}
                      for r in resources]}


def package(resources: list[dict], name: str = "utkast.term-mcp", version: str | None = None,
            fhir_version: str = "R4") -> bytes:
    """Utkasten som FHIR-paket (NPM-format, .tgz): package/package.json, .index.json och en fil per resurs."""
    version = version or "0.1.0-utkast." + time.strftime("%Y%m%d%H%M%S")
    fhir = "5.0.0" if fhir_version == "R5" else "4.0.1"
    files = {f"{r['resourceType']}-{r['id']}.json": r for r in resources}
    manifest = {"name": name, "version": version, "description": "Utkast från term-mcp (AI-stött, ej granskat)",
                "fhirVersions": [fhir], "type": "fhir.ig", "dependencies": {}}
    index = {"index-version": 2, "files": [
        {"filename": f, **{k: r.get(k) for k in ("resourceType", "id", "url", "version") if r.get(k)}}
        for f, r in files.items()]}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path, content in [("package/package.json", manifest), ("package/.index.json", index),
                              *((f"package/{f}", r) for f, r in files.items())]:
            data = json.dumps(content, ensure_ascii=False, indent=2).encode()
            info = tarfile.TarInfo(path)
            info.size, info.mtime, info.mode = len(data), int(time.time()), 0o644
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


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


async def guard(tx: TerminologyClient, resource_type: str, url: str) -> None:
    """Ett utkast får inte ta en canonical URL som en granskad resurs på terminologitjänsten redan har."""
    for existing in await tx.search(resource_type, count=5, url=url):
        if not is_proposal(existing):
            raise ProposalError(f"{resource_type} {url} finns redan som granskad resurs på terminologitjänsten "
                                f"(id {existing.get('id')}). Välj en ny URL för utkastet.")


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


SNAPSHOT_LIMIT = 1000  # ECL-urval med fler begrepp sparas utan kodlista


async def _ecl_include(tx: TerminologyClient, ecl: str) -> tuple[dict, dict]:
    """Kontrollera ECL-uttrycket mot tjänsten och ta en ögonblicksbild av expansionen."""
    try:
        first = await tx.ecl(ecl, count=SNAPSHOT_LIMIT)
    except TerminologyError as exc:
        raise ProposalError(f"ECL-uttrycket '{ecl}' godtogs inte av terminologitjänsten: {exc}") from exc
    total = first.get("total") if first.get("total") is not None else len(first["contains"])
    if not total:
        raise ProposalError(f"ECL-uttrycket '{ecl}' ger inga begrepp – inget utkast skapades.")
    include = {"system": SNOMED, "filter": [{"property": "constraint", "op": "=", "value": ecl}]}
    used = first.get("used_codesystems") or [f"{SNOMED}|{v}" for v in
                                              sorted({c["version"] for c in first["contains"] if c.get("version")})]
    expansion = {"timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"), "total": total,
                 "parameter": [{"name": "used-codesystem", "valueUri": u} for u in used]}
    if total <= SNAPSHOT_LIMIT:
        expansion["contains"] = [{"system": SNOMED, "code": c["code"], "display": c.get("display")}
                                 for c in first["contains"]]
    return include, expansion


async def valueset(tx: TerminologyClient, url: str, name: str, title: str, concepts: list[dict],
                   rationale: str | None = None, version: str = "0.1.0", ecl: str | None = None) -> dict:
    """{created: True, resource, …} eller {created: False, reason, unknown} – inget skrivs."""
    if not concepts and not (ecl or "").strip():
        raise ProposalError("Ange koder (concepts) eller ett ECL-uttryck (ecl).")
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
        return {"created": False, "reason": "Okända koder – inget utkast skapades.", "unknown": unknown}
    expansion = None
    if (ecl or "").strip():
        ecl_include, expansion = await _ecl_include(tx, ecl.strip())
        include.append(ecl_include)
    vs = _base("ValueSet", url, name, title, version, rationale) | {"compose": {"include": include}}
    if expansion:
        vs["expansion"] = expansion
    await guard(tx, "ValueSet", url)
    result = {"created": True, "resource": vs, "resourceType": "ValueSet", "id": vs["id"], "url": url,
              "version": version, "codes": len(concepts), "inactive": inactive}
    if expansion:
        result |= {"ecl": ecl.strip(), "ecl_concepts": expansion["total"],
                   "snapshot": "contains" in expansion,
                   "edition": [p["valueUri"] for p in expansion["parameter"]]}
    return result


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
        return {"created": False, "reason": "Okända koder – inget utkast skapades.", "unknown": unknown}

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
    await guard(tx, "ConceptMap", url)
    coverage = None
    if source_valueset:
        expansion = await tx.expand(url=source_valueset, count=1000)
        all_codes = {c["code"] for c in expansion["contains"]}
        coverage = {"source_codes": len(all_codes), "mapped": len(all_codes & set(elements)),
                    "not_mapped": sorted(all_codes - set(elements))}
    return {"created": True, "resource": cm, "resourceType": "ConceptMap", "id": cm["id"], "url": url,
            "version": version,
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
        return {"created": False, "reason": "Okända koder – inget utkast skapades.", "unknown": unknown}
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
    await guard(tx, "CodeSystem", url)
    total = base.get("count") or len(base.get("concept", []))
    return {"created": True, "resource": cs, "resourceType": "CodeSystem", "id": cs["id"], "url": url,
            "supplements": codesystem,
            "language": language, "translated": len(translations), "codes_in_codesystem": total,
            "base_displays": {c: r["display"] for c, r in result.items()}}
