"""Ersatta koder: egenskaper på begreppen och en ConceptMap gammal -> ny.

När ett begrepp avvecklas och ersätts av ett annat registreras det på två sätt:

* på begreppet: ``inactive = true`` och egenskapen ``replacedBy`` (koden som
  ersätter); det syns i ``$lookup``
* i en ConceptMap per projekt, som mottagande system kan använda med
  ``$translate`` för att migrera sina data automatiskt

ConceptMap-formatet följer ``FHIR_VERSION``: R4 använder ``equivalence``, R5
``relationship``.
"""

from __future__ import annotations

from datetime import date
from typing import Any

REPLACED_BY = "replacedBy"

# Relationen mellan avvecklad och ersättande kod, och hur den uttrycks i R4/R5.
RELATIONS = {
    "equivalent": ("Likvärdig", "equivalent", "equivalent"),
    "wider": ("Ersättaren är bredare", "wider", "source-is-narrower-than-target"),
    "narrower": ("Ersättaren är smalare", "narrower", "source-is-broader-than-target"),
    "related": ("Närliggande", "relatedto", "related-to"),
}


def replacement_conceptmap(codesystems: list[dict[str, Any]], replacements: dict[str, list[dict[str, Any]]], *,
                           url: str, name: str, version: str, publisher: str | None,
                           fhir_version: str = "R4") -> dict[str, Any] | None:
    """``replacements``: CodeSystem-url -> [{code, target, relation, comment}]."""
    by_url = {cs["url"]: cs for cs in codesystems}
    groups = []
    for cs_url, items in replacements.items():
        cs = by_url.get(cs_url)
        if not cs or not items:
            continue
        displays = {c["code"]: c.get("display") for c in _walk(cs.get("concept", []))}
        elements = []
        for item in items:
            _, r4, r5 = RELATIONS.get(item.get("relation") or "equivalent", RELATIONS["equivalent"])
            target = {"code": item["target"], "display": displays.get(item["target"])}
            target.update({"relationship": r5} if fhir_version == "R5" else {"equivalence": r4})
            if item.get("comment"):
                target["comment"] = item["comment"]
            elements.append({"code": item["code"], "display": displays.get(item["code"]),
                             "target": [{k: v for k, v in target.items() if v is not None}]})
        groups.append({"source": cs_url, "sourceVersion": cs.get("version"), "target": cs_url,
                       "targetVersion": cs.get("version"), "element": elements})
    if not groups:
        return None
    cm = {
        "resourceType": "ConceptMap",
        "id": name.replace("_", "-")[:64],
        "url": url,
        "version": version,
        "name": name,
        "title": f"Ersatta koder ({name})",
        "status": "draft",
        "experimental": False,
        "date": date.today().isoformat(),
        "publisher": publisher,
        "description": "Avvecklade koder och de koder som ersätter dem.",
        "group": groups,
    }
    return {k: v for k, v in cm.items() if v is not None}


def _walk(concepts):
    for c in concepts:
        yield c
        yield from _walk(c.get("concept", []))
