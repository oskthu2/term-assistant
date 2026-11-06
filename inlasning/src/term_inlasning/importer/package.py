"""FHIR-paket (NPM-format, ``.tgz``) för inläsning i en terminologitjänst.

Layout::

    package/package.json          namn, version, fhirVersions, beroenden, informationsklass
    package/.index.json           index över resurserna (index-version 1)
    package/<Typ>-<id>.json       en fil per CodeSystem/ValueSet/ConceptMap
    package/other/release-notes.md
    package/other/provenance.json Bundle med Provenance – den redaktionella kedjan

Resurserna märks med informationsklassen i ``meta.security`` så att märkningen
följer med även när en resurs plockas ur paketet.
"""

from __future__ import annotations

import copy
import io
from html import escape
import json
import tarfile
import time
from typing import Any

from ..config import FHIR_VERSIONS

CORE_PACKAGES = {"R4": "hl7.fhir.r4.core", "R5": "hl7.fhir.r5.core"}


def label_resources(resources: list[dict[str, Any]], security: dict | None) -> list[dict[str, Any]]:
    """Kopior av resurserna med informationsklassen i meta.security och en enkel narrativ."""
    out = []
    for r in resources:
        r = copy.deepcopy(r)
        r.setdefault("text", narrative(r))
        if security:
            meta = r.setdefault("meta", {})
            labels = [s for s in meta.get("security", []) if s.get("system") != security["system"]]
            meta["security"] = labels + [security]
        out.append(r)
    return out


def narrative(r: dict[str, Any]) -> dict[str, str]:
    """Minimal genererad narrativ (FHIR dom-6 rekommenderar att varje resurs har en)."""
    title = escape(str(r.get("title") or r.get("name") or r.get("id")))
    parts = [f"<p><b>{title}</b> ({escape(r['resourceType'])}, version {escape(str(r.get('version', '')))})</p>"]
    if r.get("description"):
        parts.append(f"<p>{escape(str(r['description']))}</p>")
    if r["resourceType"] == "CodeSystem":
        parts.append(f"<p>{len(r.get('concept', []))} begrepp.</p>")
    elif r["resourceType"] == "ValueSet":
        n = sum(len(i.get("concept", [])) for i in r.get("compose", {}).get("include", []))
        parts.append(f"<p>{n} koder.</p>")
    lang = f' lang="{escape(r["language"])}" xml:lang="{escape(r["language"])}"' if r.get("language") else ""
    return {"status": "generated", "div": f'<div xmlns="http://www.w3.org/1999/xhtml"{lang}>{"".join(parts)}</div>'}


def package_manifest(name: str, version: str, fhir_version: str, *, title: str, description: str = "",
                     author: str | None = None, classification: dict | None = None,
                     license_note: str | None = None, canonical: str | None = None) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "name": name,
        "version": version,
        "title": title,
        "description": description or title,
        "fhirVersions": [FHIR_VERSIONS[fhir_version]],
        "dependencies": {CORE_PACKAGES[fhir_version]: FHIR_VERSIONS[fhir_version]},
        "type": "fhir.ig",
        "date": time.strftime("%Y%m%d%H%M%S"),
        "author": author or "",
        "license": "SEE LICENSE IN other/license.md" if license_note else "CC0-1.0",
    }
    if canonical:
        manifest["canonical"] = canonical
    if classification:
        manifest["x-classification"] = classification
    return manifest


def build_package(resources: list[dict[str, Any]], manifest: dict[str, Any], *,
                  release_notes: str = "", provenance: dict | None = None,
                  license_note: str | None = None) -> bytes:
    files: dict[str, bytes] = {}
    index = []
    for r in resources:
        filename = f"{r['resourceType']}-{r.get('id') or r['name']}.json"
        files[f"package/{filename}"] = json.dumps(r, ensure_ascii=False, indent=2).encode("utf-8")
        index.append({k: v for k, v in {"filename": filename, "resourceType": r["resourceType"], "id": r.get("id"),
                                        "url": r.get("url"), "version": r.get("version")}.items() if v})
    files["package/package.json"] = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    files["package/.index.json"] = json.dumps({"index-version": 1, "files": index}, indent=2).encode("utf-8")
    if release_notes:
        files["package/other/release-notes.md"] = release_notes.encode("utf-8")
    if provenance:
        files["package/other/provenance.json"] = json.dumps(provenance, ensure_ascii=False, indent=2).encode("utf-8")
    if license_note:
        files["package/other/license.md"] = license_note.encode("utf-8")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, data in sorted(files.items()):
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mtime = int(time.time())
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def read_package(data: bytes) -> dict[str, Any]:
    """Läs ett paket (för test och kontroll): {sökväg: innehåll}."""
    out = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            content = tar.extractfile(member).read()
            out[member.name] = json.loads(content) if member.name.endswith(".json") else content.decode("utf-8")
    return out
