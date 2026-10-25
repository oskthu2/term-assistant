"""Urval ur befintliga kodsystem -> ValueSet.

En lista med t.ex. NPU-koder är inget nytt kodverk utan ett urval. Den blir:

* ett ValueSet med hela urvalet (koderna listas explicit, med listans egna
  visningsnamn som ``concept.display``)
* ett ValueSet per grupp om listan har en grupperingskolumn ("Grupp",
  "Kategori"). En kod kan förekomma i flera grupper.

Ett blad kan också vara en *värdemängdsdefinition*: kodsystem och version per
rad, inkludera/exkludera och en regel (exakt kod, med eller utan underordnade,
eller ett ECL-uttryck för SNOMED CT). Exakta koder blir ``compose.include.concept``,
regler blir ``filter`` och exkluderade rader hamnar i ``compose.exclude``.

Koderna kan valideras mot terminologitjänsten (``validate_valueset_codes``)
så att okända, inaktiva eller felstavade koder upptäcks före publicering.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date
from typing import Any

from .external import classify_code
from .mapping import ColumnRole, ImportTemplate
from .suggest import to_constant
from .transform import _fhir_name, _resource_id


COMMENT_EXT = "http://hl7.org/fhir/StructureDefinition/valueset-concept-comments"
SNOMED = "http://snomed.info/sct"
SNOMED_MODULES = {"45991000052106": "Sverige", "900000000000207008": "Internationell",
                  "554471000005108": "Danmark", "51000202101": "Norge", "11000229106": "Finland"}


class _Step:
    """Ett urvalssteg: en kod (eventuellt med filter) i ett kodsystem/version, inkluderad eller exkluderad."""

    __slots__ = ("system", "version", "include", "code", "display", "rule", "expression", "comment")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


def build_valuesets(template: ImportTemplate, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    from .external import resolve_system_name
    from .suggest import parse_include, parse_rule

    meta = template.codesystem
    col = {role: template.column(role) for role in ColumnRole}
    code_col = col[ColumnRole.CODE].source_column
    comment_cols = [c for c in template.columns if c.role == ColumnRole.COMMENT]
    warnings: list[str] = []
    steps: list[_Step] = []
    groups: dict[str, list[_Step]] = {}

    def value(role, row):
        c = col[role]
        v = row.get(c.source_column) if c else None
        return str(v).strip() if v is not None and str(v).strip() else None

    for line, row in enumerate(rows, start=template.source.header_row + 1):
        code = value(ColumnRole.CODE, row)
        if code is None:
            continue
        if col[ColumnRole.SYSTEM]:
            label = value(ColumnRole.SYSTEM, row)
            if not label:
                warnings.append(f"Rad {line}: '{code}' utelämnad – kodsystem saknas")
                continue
            key, default_uri = resolve_system_name(label)
            uri = template.external_systems.get(key) or default_uri
            if not uri or uri.startswith("urn:term-assistant:unresolved"):
                warnings.append(f"Rad {line}: '{code}' utelämnad – ingen URL angiven för kodsystemet '{label}'")
                continue
        else:
            system, anomaly = classify_code(code)
            if system is None:
                warnings.append(f"Rad {line}: '{code}' utelämnad – "
                                + (anomaly.problem if anomaly else "tillhör inget känt kodsystem"))
                continue
            uri = template.external_systems.get(system.key)
            if not uri:
                warnings.append(f"Rad {line}: '{code}' utelämnad – ingen URL angiven för {system.name}")
                continue
        rule, expression = parse_rule(value(ColumnRole.RULE, row), code)
        if rule == "unknown":
            warnings.append(f"Rad {line}: regeln '{expression}' känns inte igen – koden tas med som exakt kod")
            rule, expression = "concept", None
        if rule == "ecl" and uri != "http://snomed.info/sct":
            warnings.append(f"Rad {line}: ECL-uttryck fungerar bara för SNOMED CT – raden hoppas över")
            continue
        comments = [f"{c.ui.label if c.ui else c.source_column}: {row.get(c.source_column)}" if len(comment_cols) > 1
                    else str(row.get(c.source_column)).strip()
                    for c in comment_cols if row.get(c.source_column) not in (None, "")]
        version = value(ColumnRole.SYSTEM_VERSION, row)
        if version and uri == SNOMED and re.fullmatch(r"\d{8}", version):
            # FHIR anger SNOMED CT-versioner som utgåve-URI: http://snomed.info/sct/<modul>/version/<datum>
            if template.snomed_module:
                version = f"{SNOMED}/{template.snomed_module}/version/{version}"
            else:
                warnings.append(f"Rad {line}: SNOMED CT-versionen '{version}' saknar utgåva – ange SNOMED CT-utgåva "
                                "i projektet (t.ex. 45991000052106 för Sverige)")
        step = _Step(system=uri, version=version,
                     include=parse_include(value(ColumnRole.INCLUDE, row)), code=code,
                     display=value(ColumnRole.DISPLAY, row), rule=rule, expression=expression,
                     comment=" – ".join(comments) or None)
        duplicate = next((s for s in steps if (s.system, s.code, s.rule, s.include) ==
                          (step.system, step.code, step.rule, step.include)), None)
        if duplicate and duplicate.display != step.display:
            warnings.append(f"Rad {line}: '{code}' förekommer flera gånger med olika namn "
                            f"('{duplicate.display}' / '{step.display}')")
        if duplicate is None:
            steps.append(step)
        group = value(ColumnRole.GROUP, row)
        if group:
            if any((s.system, s.code, s.rule) == (step.system, step.code, step.rule) for s in groups.get(group, [])):
                warnings.append(f"Rad {line}: '{code}' finns redan i gruppen '{group}'")
            else:
                groups.setdefault(group, []).append(step)

    resources = [_valueset(meta.url, meta.name, meta.title or meta.name, steps, meta)]
    for group, group_steps in groups.items():
        resources.append(_valueset(f"{meta.url}-{to_constant(group).lower().replace('_', '-')}",
                                   f"{meta.name}_{to_constant(group)}",
                                   f"{meta.title or meta.name} – {group}", group_steps, meta))
    return resources, warnings


def _compose(steps: list[_Step]) -> dict[str, Any]:
    """Bygg compose. Exakta koder samlas per kodsystem/version; varje filter blir ett eget element
    (FHIR tillåter inte concept och filter i samma include, och flera filter i ett element är OCH)."""
    out: dict[str, list] = {"include": [], "exclude": []}
    plain: dict[tuple[bool, str, str | None], dict] = {}
    for s in steps:
        target = out["include" if s.include else "exclude"]
        if s.rule == "concept":
            key = (s.include, s.system, s.version)
            if key not in plain:
                plain[key] = {"system": s.system, **({"version": s.version} if s.version else {}), "concept": []}
                target.append(plain[key])
            concept = {"code": s.code, **({"display": s.display} if s.display else {})}
            if s.comment:
                concept["extension"] = [{"url": COMMENT_EXT, "valueString": s.comment}]
            plain[key]["concept"].append(concept)
        else:
            flt = ({"property": "constraint", "op": "=", "value": s.expression} if s.rule == "ecl"
                   else {"property": "concept", "op": s.rule, "value": s.code})
            target.append({"system": s.system, **({"version": s.version} if s.version else {}), "filter": [flt]})
    return {k: v for k, v in out.items() if v}


def _valueset(url: str, name: str, title: str, steps: list[_Step], meta) -> dict[str, Any]:
    vs = {
        "resourceType": "ValueSet",
        "id": _resource_id(name),
        "url": url,
        "version": meta.version,
        "name": _fhir_name(name),
        "title": title,
        "status": "draft",
        "experimental": False,
        "date": date.today().isoformat(),
        "publisher": meta.publisher,
        "description": meta.description or f"Urval: {title}.",
        "language": meta.language,
        "compose": _compose(steps),
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
