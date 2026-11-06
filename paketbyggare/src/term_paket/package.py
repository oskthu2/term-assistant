"""FHIR-paket i NPM-format (``.tgz``) – läsa, skriva och normalisera resurser.

Layout (samma som inläsningstjänstens releaser)::

    package/package.json        namn, version, fhirVersions, beroenden
    package/.index.json         index över resurserna
    package/<Typ>-<id>.json     en fil per resurs
    package/other/changes.json  vad som ändrats sedan föregående version
    package/other/CHANGES.md    samma sak som läsbar text
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import re
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import FHIR_VERSIONS, RESOURCE_TYPES

_VOLATILE_META = ("versionId", "lastUpdated", "source")


@dataclass
class Package:
    manifest: dict[str, Any]
    resources: list[dict[str, Any]]
    other: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.manifest["name"]

    @property
    def version(self) -> str:
        return self.manifest["version"]


def normalize(resource: dict[str, Any]) -> dict[str, Any]:
    """Ta bort det som bara beskriver serverns lagring (meta.versionId/lastUpdated/source), inte innehållet."""
    r = copy.deepcopy(resource)
    meta = r.get("meta")
    if isinstance(meta, dict):
        for key in _VOLATILE_META:
            meta.pop(key, None)
        meta.pop("tag", None)  # servrarnas egna taggar (t.ex. SUBSETTED vid _summary)
        if not meta:
            r.pop("meta")
    return r


def resource_hash(resource: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(normalize(resource), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def concept_count(cs: dict[str, Any]) -> int:
    def walk(concepts):
        return sum(1 + walk(c.get("concept", [])) for c in concepts)
    return cs.get("count") or walk(cs.get("concept", []))


def assign_ids(resources: list[dict[str, Any]]) -> None:
    """Giltiga och unika id per resurstyp. HAPI kräver minst en bokstav i klienttilldelade id."""
    used: set[tuple[str, str]] = set()
    for r in resources:
        base = r.get("id") or r.get("name") or r.get("url", "").rstrip("/").rsplit("/", 1)[-1] or "resurs"
        rid = re.sub(r"[^A-Za-z0-9\-.]+", "-", str(base)).strip("-.")[:60] or "resurs"
        if not re.search(r"[A-Za-z]", rid):
            rid = f"r-{rid}"
        candidate, n = rid, 2
        while (r["resourceType"], candidate) in used:
            candidate, n = f"{rid[:57]}-{n}", n + 1
        used.add((r["resourceType"], candidate))
        r["id"] = candidate


def manifest(name: str, version: str, fhir_version: str, *, title: str, description: str, date: str,
             author: str = "", classification: dict | None = None) -> dict[str, Any]:
    m = {
        "name": name,
        "version": version,
        "title": title,
        "description": description or title,
        "fhirVersions": [fhir_version],
        "dependencies": {FHIR_VERSIONS[fhir_version]: fhir_version},
        "type": "fhir.ig",
        "date": date,
        "author": author,
        "license": "SEE LICENSE IN resources (copyright per resurs)",
    }
    if classification:
        m["x-classification"] = classification
    return m


def write_package(pkg: Package) -> bytes:
    files: dict[str, bytes] = {}
    index = []
    for r in sorted(pkg.resources, key=lambda r: (r["resourceType"], r["id"])):
        filename = f"{r['resourceType']}-{r['id']}.json"
        files[f"package/{filename}"] = json.dumps(r, ensure_ascii=False, indent=2).encode("utf-8")
        index.append({k: v for k, v in {"filename": filename, "resourceType": r["resourceType"], "id": r["id"],
                                        "url": r.get("url"), "version": r.get("version")}.items() if v})
    files["package/package.json"] = json.dumps(pkg.manifest, ensure_ascii=False, indent=2).encode("utf-8")
    files["package/.index.json"] = json.dumps({"index-version": 2, "files": index}, indent=2).encode("utf-8")
    for name, content in pkg.other.items():
        data = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=2)
        files[f"package/other/{name}"] = data.encode("utf-8")
    buf = io.BytesIO()
    # Fast tidsstämpel: samma innehåll ger samma fil (och samma kontrollsumma).
    mtime = _mtime(pkg.manifest.get("date", ""))
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=9) as tar:
        for path, data in sorted(files.items()):
            info = tarfile.TarInfo(path)
            info.size, info.mtime, info.mode = len(data), mtime, 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def read_package(data: bytes) -> Package:
    manifest_: dict[str, Any] | None = None
    resources, other = [], {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            name = member.name
            content = tar.extractfile(member).read()
            if name == "package/package.json":
                manifest_ = json.loads(content)
            elif name.startswith("package/other/"):
                text = content.decode("utf-8")
                other[name.rsplit("/", 1)[-1]] = json.loads(text) if name.endswith(".json") else text
            elif name.endswith(".json") and name.count("/") == 1 and not name.endswith(".index.json"):
                r = json.loads(content)
                if r.get("resourceType") in RESOURCE_TYPES:
                    resources.append(r)
    if manifest_ is None:
        raise ValueError("Paketet saknar package/package.json")
    return Package(manifest_, resources, other)


def read_package_file(path: str | Path) -> Package:
    return read_package(Path(path).read_bytes())


def version_key(version: str) -> tuple:
    """Sorteringsnyckel för versioner: '1.10.0' > '1.9.0'; för-releaser ('-rc.1', '-ci') sorteras före."""
    main, _, pre = version.partition("-")
    nums = tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.]", main))
    return nums + ((1,) if not pre else (0, pre))


def latest_packages(directory: str | Path, name: str | None = None) -> dict[str, Path]:
    """Senaste ``.tgz`` per paketnamn i en katalog (namnet läses ur package.json)."""
    best: dict[str, tuple[tuple, Path]] = {}
    for path in Path(directory).glob("*.tgz"):
        try:
            pkg = read_package_file(path)
        except (OSError, ValueError, tarfile.TarError):
            continue
        if name and pkg.name != name:
            continue
        key = version_key(pkg.version)
        if pkg.name not in best or key > best[pkg.name][0]:
            best[pkg.name] = (key, path)
    return {n: p for n, (_, p) in best.items()}


def _mtime(date: str) -> int:
    import calendar
    import time
    m = re.match(r"(\d{4})(\d{2})(\d{2})", date)
    if not m:
        return 0
    return calendar.timegm(time.struct_time((int(m[1]), int(m[2]), int(m[3]), 0, 0, 0, 0, 0, 0)))
