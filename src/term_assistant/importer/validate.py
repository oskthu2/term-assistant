"""Profilvalidering av de resurser som byggs (före paketering och publicering).

Kontrollerna följer kraven i FHIR:s Shareable-profiler (ShareableCodeSystem,
ShareableValueSet, ShareableConceptMap) och resursernas invarianter, för R4
eller R5 enligt ``FHIR_VERSION``:

* obligatoriska metadata: url, version, name, status, experimental, publisher,
  description
* format: id, name (maskinläsbart), url (absolut), datum
* CodeSystem: unika koder, deklarerade egenskaper med rätt värdetyp,
  föräldrar som finns, giltiga designations, ``count`` som stämmer
* ValueSet: varje include har system eller valueSet, unika koder per system
* ConceptMap: equivalence (R4) respektive relationship (R5) på varje mål

För en fullständig validering kan HL7:s validator köras på paketet
(``FHIR_VALIDATOR_JAR``); den här modulen fångar de fel som brukar stoppa
inläsning i en terminologitjänst.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel

_ID = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")
_NAME = re.compile(r"^[A-Z]([A-Za-z0-9_]){0,254}$")
_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2}))?)?)?$")
_LANG = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
_STATUS = {"draft", "active", "retired", "unknown"}
_VALUE_TYPES = {
    "code": "valueCode", "Coding": "valueCoding", "string": "valueString", "integer": "valueInteger",
    "boolean": "valueBoolean", "dateTime": "valueDateTime", "decimal": "valueDecimal",
}
_R4_EQUIVALENCE = {"relatedto", "equivalent", "equal", "wider", "subsumes", "narrower", "specializes", "inexact",
                   "unmatched", "disjoint"}
_R5_RELATIONSHIP = {"related-to", "equivalent", "source-is-narrower-than-target", "source-is-broader-than-target",
                    "not-related-to"}


class Issue(BaseModel):
    severity: Literal["error", "warning"]
    resource: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.resource} {self.path}: {self.message}"


def validate_resources(resources: list[dict[str, Any]], fhir_version: str = "R4") -> list[Issue]:
    issues: list[Issue] = []
    urls: dict[tuple[str, str], int] = {}
    for r in resources:
        issues += validate_resource(r, fhir_version)
        key = (r.get("url", ""), r.get("version", ""))
        urls[key] = urls.get(key, 0) + 1
    for (url, version), n in urls.items():
        if n > 1:
            issues.append(Issue(severity="error", resource=url, path="url",
                                message=f"{n} resurser har samma url och version ({version})"))
    return issues


def validate_resource(r: dict[str, Any], fhir_version: str = "R4") -> list[Issue]:
    rt = r.get("resourceType", "?")
    label = f"{rt}/{r.get('id') or r.get('name') or '?'}"
    out: list[Issue] = []

    def err(path: str, msg: str) -> None:
        out.append(Issue(severity="error", resource=label, path=path, message=msg))

    def warn(path: str, msg: str) -> None:
        out.append(Issue(severity="warning", resource=label, path=path, message=msg))

    # Gemensamt (Shareable-profilerna)
    if r.get("id") is not None and not _ID.match(str(r["id"])):
        err("id", f"'{r['id']}' är inget giltigt id (A–Z, a–z, 0–9, '-', '.', högst 64 tecken)")
    url = r.get("url")
    if not url:
        err("url", "saknas")
    elif not re.match(r"^(https?://|urn:(oid|uuid):)", url):
        err("url", f"'{url}' är inte en absolut URL")
    for field in ("version", "name", "status", "publisher", "description"):
        if not r.get(field):
            err(field, "saknas (krävs av Shareable-profilen)")
    if "experimental" not in r:
        err("experimental", "saknas (krävs av Shareable-profilen)")
    if r.get("name") and not _NAME.match(r["name"]):
        warn("name", f"'{r['name']}' bör vara maskinläsbart (inled med versal, bara A–Z, a–z, 0–9, _)")
    if r.get("status") and r["status"] not in _STATUS:
        err("status", f"ogiltigt värde '{r['status']}'")
    if r.get("date") and not _DATE.match(str(r["date"])):
        err("date", f"'{r['date']}' är inget giltigt datum")
    if r.get("language") and not _LANG.match(r["language"]):
        warn("language", f"'{r['language']}' ser inte ut som en BCP-47-kod")

    if rt == "CodeSystem":
        _codesystem(r, err, warn)
    elif rt == "ValueSet":
        _valueset(r, err, warn)
    elif rt == "ConceptMap":
        _conceptmap(r, fhir_version, err, warn)
    return out


def _codesystem(r, err, warn) -> None:
    if not r.get("content"):
        err("content", "saknas")
    if "caseSensitive" not in r:
        warn("caseSensitive", "bör anges (krävs av ShareableCodeSystem)")
    declared = {p.get("code"): p.get("type") for p in r.get("property", [])}
    concepts = list(_walk(r.get("concept", [])))
    codes = [c.get("code") for c in concepts]
    seen: set[str] = set()
    for i, c in enumerate(concepts):
        code = c.get("code")
        path = f"concept[{code}]"
        if not code:
            err(f"concept[{i}].code", "saknas")
            continue
        if code in seen:
            err(path, "koden förekommer flera gånger")
        seen.add(code)
        if code != code.strip() or "\n" in code:
            err(path, "koden innehåller blanksteg i början/slutet eller radbrytning")
        if not c.get("display"):
            warn(path, "saknar display")
        for p in c.get("property", []):
            pcode = p.get("code")
            if pcode not in declared:
                err(f"{path}.property[{pcode}]", "egenskapen är inte deklarerad i CodeSystem.property")
                continue
            expected = _VALUE_TYPES.get(declared[pcode])
            present = [k for k in p if k.startswith("value")]
            if expected and present and present[0] != expected:
                err(f"{path}.property[{pcode}]", f"värdet är {present[0]}, men egenskapen har typen {declared[pcode]}")
            if pcode in ("parent", "replacedBy") and p.get("valueCode") and p["valueCode"] not in codes:
                err(f"{path}.property[{pcode}]", f"'{p['valueCode']}' finns inte i kodverket")
        for d in c.get("designation", []):
            if not d.get("value"):
                err(f"{path}.designation", "saknar value")
            use = d.get("use")
            if use and not (use.get("system") and use.get("code")):
                err(f"{path}.designation.use", "måste ha system och code")
            if d.get("language") and not _LANG.match(d["language"]):
                warn(f"{path}.designation.language", f"'{d['language']}' ser inte ut som en BCP-47-kod")
    if r.get("content") == "complete" and "count" in r and r["count"] != len(concepts):
        err("count", f"är {r['count']} men kodverket har {len(concepts)} begrepp")
    if r.get("hierarchyMeaning") and r["hierarchyMeaning"] not in ("grouped-by", "is-a", "part-of",
                                                                  "classified-with"):
        err("hierarchyMeaning", f"ogiltigt värde '{r['hierarchyMeaning']}'")


def _valueset(r, err, warn) -> None:
    includes = r.get("compose", {}).get("include", [])
    if not includes:
        err("compose.include", "saknas – urvalet är tomt")
    for i, inc in enumerate(includes):
        if not inc.get("system") and not inc.get("valueSet"):
            err(f"compose.include[{i}]", "måste ha system eller valueSet")
        if inc.get("system", "").startswith("urn:term-assistant:unresolved"):
            err(f"compose.include[{i}].system", "kodsystemets canonical URL är inte angiven")
        codes = [c.get("code") for c in inc.get("concept", [])]
        dups = {c for c in codes if codes.count(c) > 1}
        if dups:
            err(f"compose.include[{i}].concept", f"dubbletter: {', '.join(sorted(dups)[:10])}")


def _conceptmap(r, fhir_version, err, warn) -> None:
    for gi, g in enumerate(r.get("group", [])):
        if not g.get("source") or not g.get("target"):
            warn(f"group[{gi}]", "bör ange source och target")
        for e in g.get("element", []):
            for t in e.get("target", []):
                path = f"group[{gi}].element[{e.get('code')}]"
                if fhir_version == "R5":
                    if t.get("relationship") not in _R5_RELATIONSHIP:
                        err(path, f"relationship '{t.get('relationship')}' är inte giltigt i R5")
                    if "equivalence" in t:
                        err(path, "equivalence finns inte i R5 (använd relationship)")
                else:
                    if t.get("equivalence") not in _R4_EQUIVALENCE:
                        err(path, f"equivalence '{t.get('equivalence')}' är inte giltigt i R4")
                    if "relationship" in t:
                        err(path, "relationship finns inte i R4 (använd equivalence)")


def _walk(concepts):
    for c in concepts:
        yield c
        yield from _walk(c.get("concept", []))


def validate_package_file(data: bytes, fhir_version: str = "R4", jar: str | None = None,
                          tx: str = "n/a") -> dict[str, Any]:
    """Packa upp ett paket (.tgz) och kör både den inbyggda kontrollen och HL7-validatorn (om den finns)."""
    import io
    import json
    import os
    import tarfile
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            tar.extractall(tmp, filter="data")
        folder = os.path.join(tmp, "package")
        files = sorted(os.path.join(folder, f) for f in os.listdir(folder)
                       if f.endswith(".json") and f[0].isupper())
        resources = [json.load(open(f, encoding="utf-8")) for f in files]
        builtin = [str(i) for i in validate_resources(resources, fhir_version) if i.severity == "error"]
        result: dict[str, Any] = {"files": len(files), "builtin_errors": builtin}
        if jar or os.environ.get("FHIR_VALIDATOR_JAR"):
            hl7 = run_hl7_validator(files, fhir_version, jar, tx)
            result.update(errors=hl7["errors"], warnings=hl7["warnings"])
        return result


def run_hl7_validator(files: list[str], fhir_version: str = "R4", jar: str | None = None,
                      tx: str = "n/a", timeout: int = 900) -> dict[str, Any]:
    """Kör HL7:s officiella validator (validator_cli.jar) på filer eller en paketkatalog.

    Kräver Java och sökvägen till jar-filen i ``jar`` eller ``FHIR_VALIDATOR_JAR``.
    ``tx='n/a'`` validerar utan terminologiserver; ange t.ex. TX_BASE_URL för att
    även kontrollera koder. Returnerar {errors, warnings, output}.
    """
    import os
    import subprocess

    from ..config import FHIR_VERSIONS
    jar = jar or os.environ.get("FHIR_VALIDATOR_JAR")
    if not jar or not os.path.exists(jar):
        raise FileNotFoundError("HL7-validatorn saknas: ange FHIR_VALIDATOR_JAR (sökväg till validator_cli.jar).")
    cmd = ["java", "-jar", jar, "-version", FHIR_VERSIONS[fhir_version], "-tx", tx, *files]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    output = proc.stdout + proc.stderr
    errors = [line.strip() for line in output.splitlines() if line.strip().startswith(("Error @", "Fatal @"))]
    warnings = [line.strip() for line in output.splitlines() if line.strip().startswith("Warning @")]
    return {"errors": errors, "warnings": warnings, "output": output[-20000:], "returncode": proc.returncode}
