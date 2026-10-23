"""Urval ur befintliga kodsystem -> ValueSet.

En lista med t.ex. NPU-koder är inget nytt kodverk utan ett urval. Den blir:

* ett ValueSet med hela urvalet (koderna listas explicit, med listans egna
  visningsnamn som ``concept.display``)
* ett ValueSet per grupp om listan har en grupperingskolumn ("Grupp",
  "Kategori"). En kod kan förekomma i flera grupper.

Koderna kan valideras mot terminologitjänsten (``validate_valueset_codes``)
så att okända, inaktiva eller felstavade koder upptäcks före publicering.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

from .external import classify_code
from .mapping import ColumnRole, ImportTemplate
from .suggest import to_constant
from .transform import _fhir_name, _resource_id


def build_valuesets(template: ImportTemplate, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    meta = template.codesystem
    code_col = template.column(ColumnRole.CODE).source_column
    display_col = template.column(ColumnRole.DISPLAY)
    group_col = template.column(ColumnRole.GROUP)
    warnings: list[str] = []
    all_concepts: dict[tuple[str, str], str | None] = {}
    groups: dict[str, dict[tuple[str, str], str | None]] = {}

    for line, row in enumerate(rows, start=template.source.header_row + 1):
        raw = row.get(code_col)
        if raw is None or not str(raw).strip():
            continue
        code = str(raw).strip()
        system, anomaly = classify_code(code)
        if system is None:
            warnings.append(f"Rad {line}: '{code}' utelämnad – "
                            + (anomaly.problem if anomaly else "tillhör inget känt kodsystem"))
            continue
        uri = template.external_systems.get(system.key)
        if not uri:
            warnings.append(f"Rad {line}: '{code}' utelämnad – ingen URL angiven för {system.name}")
            continue
        display = str(row.get(display_col.source_column)).strip() if display_col and row.get(
            display_col.source_column) else None
        key = (uri, code)
        if key in all_concepts and all_concepts[key] != display:
            warnings.append(f"Rad {line}: '{code}' förekommer flera gånger med olika namn "
                            f"('{all_concepts[key]}' / '{display}')")
        all_concepts.setdefault(key, display)
        if group_col and row.get(group_col.source_column):
            group = str(row.get(group_col.source_column)).strip()
            if key in groups.setdefault(group, {}):
                warnings.append(f"Rad {line}: '{code}' finns redan i gruppen '{group}'")
            groups[group].setdefault(key, display)

    resources = [_valueset(meta.url, meta.name, meta.title or meta.name, all_concepts, meta, template)]
    for group, concepts in groups.items():
        resources.append(_valueset(f"{meta.url}-{to_constant(group).lower().replace('_', '-')}",
                                   f"{meta.name}_{to_constant(group)}",
                                   f"{meta.title or meta.name} – {group}", concepts, meta, template))
    return resources, warnings


def _valueset(url: str, name: str, title: str, concepts: dict[tuple[str, str], str | None], meta,
              template: ImportTemplate) -> dict[str, Any]:
    includes: dict[str, list[dict]] = {}
    for (system, code), display in concepts.items():
        includes.setdefault(system, []).append({"code": code, **({"display": display} if display else {})})
    vs = {
        "resourceType": "ValueSet",
        "id": _resource_id(name),
        "url": url,
        "version": meta.version,
        "name": _fhir_name(name),
        "title": title,
        "status": "draft",
        "date": date.today().isoformat(),
        "publisher": meta.publisher,
        "description": meta.description,
        "language": meta.language,
        "compose": {"include": [{"system": s, "concept": c} for s, c in includes.items()]},
    }
    return {k: v for k, v in vs.items() if v is not None}


async def validate_valueset_codes(client, valueset: dict[str, Any], concurrency: int = 8) -> dict[str, Any]:
    """Validera varje kod i ett ValueSet mot terminologitjänsten ($validate-code)."""
    sem = asyncio.Semaphore(concurrency)
    results: list[dict[str, Any]] = []

    async def check(system: str, concept: dict) -> None:
        async with sem:
            try:
                r = await client.validate_code(concept["code"], system=system)
            except Exception as exc:  # okänt kodsystem på servern m.m.
                results.append({"system": system, "code": concept["code"], "status": "error", "message": str(exc)})
                return
        entry = {"system": system, "code": concept["code"]}
        if not r.get("result"):
            entry.update(status="invalid", message=r.get("message"))
        elif r.get("inactive"):
            entry.update(status="inactive", display=r.get("display"))
        else:
            entry.update(status="ok", display=r.get("display"))
        entry["local_display"] = concept.get("display")
        results.append(entry)

    await asyncio.gather(*(check(inc["system"], c)
                           for inc in valueset.get("compose", {}).get("include", [])
                           for c in inc.get("concept", [])))
    summary: dict[str, int] = {}
    for r in results:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    return {"summary": summary, "problems": [r for r in results if r["status"] != "ok"]}
