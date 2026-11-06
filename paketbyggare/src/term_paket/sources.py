"""Källor för paketets resurser: FHIR-servrar (t.ex. tx-nordics) och kataloger med FHIR-paket."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx2

from .config import Include, PackageConfig, Source
from .package import concept_count, latest_packages, normalize, read_package_file, version_key

FHIR_JSON = "application/fhir+json"


@dataclass
class Collected:
    resources: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)       # vad som hämtades/hoppades över och varför
    sources: list[str] = field(default_factory=list)     # för ändringsloggen


class FhirServer:
    """Läser resurser från en FHIR-server. Tål tillfälliga nätverksfel (några försök med backoff)."""

    def __init__(self, base: str, transport: httpx2.BaseTransport | None = None, timeout: float = 120.0,
                 retries: int = 4):
        self.base = base.rstrip("/")
        self.retries = retries
        self._http = httpx2.Client(base_url=self.base + "/", headers={"Accept": FHIR_JSON}, timeout=timeout,
                                   follow_redirects=True, transport=transport)

    def close(self) -> None:
        self._http.close()

    def get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        for attempt in range(self.retries + 1):
            try:
                response = self._http.get(path, params={k: v for k, v in (params or {}).items() if v is not None})
            except httpx2.TransportError:
                if attempt == self.retries:
                    raise
                time.sleep(2 ** attempt)
                continue
            if response.status_code >= 500 and attempt < self.retries:
                time.sleep(2 ** attempt)
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError("unreachable")

    def search(self, resource_type: str, **params: Any) -> list[dict[str, Any]]:
        """Alla träffar (följer next-länkar)."""
        out = []
        bundle = self.get(resource_type, {"_count": 200, **params})
        while True:
            out += [e["resource"] for e in bundle.get("entry", []) if "resource" in e]
            nxt = next((link["url"] for link in bundle.get("link", []) if link.get("relation") == "next"), None)
            if not nxt:
                return out
            bundle = self.get(nxt)

    def canonical(self, resource_type: str, url: str, version: str | None = None) -> dict[str, Any] | None:
        """Resursen med en viss canonical URL; utan version den senaste (högsta version, sedan senast ändrad)."""
        # Många servrar (bl.a. tx-nordics) svarar i sammanfattat läge utan _summary=false.
        hits = [r for r in self.search(resource_type, url=url, version=version, _summary="false")
                if r.get("url") == url]
        for r in hits:
            if any(t.get("code") == "SUBSETTED" for t in r.get("meta", {}).get("tag", [])):
                raise ValueError(f"{self.base} returnerade {resource_type} {url} i sammanfattat läge (SUBSETTED)")
        if version:
            hits = [r for r in hits if r.get("version") == version]
        if not hits:
            return None
        return max(hits, key=lambda r: (version_key(r.get("version") or "0"), r.get("meta", {}).get("lastUpdated", "")))


def collect(config: PackageConfig, servers: dict[str, FhirServer] | None = None) -> Collected:
    """Hämta alla resurser som paketbeskrivningen pekar ut. Dubbletter (samma url och version) tas bort."""
    result = Collected()
    servers = servers if servers is not None else {}
    for source in config.sources:
        if source.type == "fhir":
            server = servers.get(source.base) or servers.setdefault(source.base, FhirServer(source.base))
            _collect_fhir(server, source, config, result)
        else:
            _collect_packages(source, result)
    seen: dict[tuple, dict] = {}
    for r in result.resources:
        key = (r["resourceType"], r.get("url"), r.get("version"))
        if key in seen and seen[key] != r:
            result.notes.append(f"{r['resourceType']} {r.get('url')}|{r.get('version')} finns i flera källor – "
                                "den första används")
        seen.setdefault(key, r)
    result.resources = list(seen.values())
    return result


def _collect_fhir(server: FhirServer, source: Source, config: PackageConfig, result: Collected) -> None:
    result.sources.append(f"FHIR-server {server.base}")
    for inc in source.include:
        resource = server.canonical(inc.resource_type, inc.url, inc.version)
        if resource is None:
            result.notes.append(f"Saknas på {server.base}: {inc.resource_type} {inc.url}"
                                + (f"|{inc.version}" if inc.version else ""))
            continue
        if not _accept(resource, inc, config, result):
            continue
        result.resources.append(normalize(resource))
        result.notes.append(f"Hämtad: {_label(resource)}")
        if inc.with_code_systems and resource["resourceType"] == "ValueSet":
            _dependencies(server, resource, config, result)


def _dependencies(server: FhirServer, valueset: dict, config: PackageConfig, result: Collected) -> None:
    compose = valueset.get("compose", {})
    systems = {(i["system"], i.get("version")) for part in ("include", "exclude")
               for i in compose.get(part, []) if i.get("system")}
    for system, version in sorted(systems, key=lambda s: (s[0], s[1] or "")):
        if any(r["resourceType"] == "CodeSystem" and r.get("url") == system for r in result.resources):
            continue
        cs = server.canonical("CodeSystem", system, version)
        if cs is None:
            result.notes.append(f"Kodsystemet {system} (för {valueset.get('name')}) finns inte på servern – "
                                "räknas som externt beroende")
            continue
        if cs.get("content") not in ("complete", "fragment", "supplement"):
            result.notes.append(f"Kodsystemet {system} har content={cs.get('content')} – tas inte med")
            continue
        if _accept(cs, Include("CodeSystem", system, version), config, result):
            result.resources.append(normalize(cs))
            result.notes.append(f"Hämtad (beroende): {_label(cs)}")


def _accept(resource: dict, inc: Include, config: PackageConfig, result: Collected) -> bool:
    if resource["resourceType"] == "CodeSystem" and not inc.allow_large:
        n = concept_count(resource)
        if n > config.max_concepts:
            result.notes.append(f"För stort: {_label(resource)} har {n} begrepp (maxConcepts {config.max_concepts}; "
                                "sätt allowLarge för att ta med det)")
            return False
    return True


def _collect_packages(source: Source, result: Collected) -> None:
    if not source.path.is_dir():
        if source.optional:
            result.notes.append(f"Paketkatalogen {source.path} finns inte – hoppas över")
            return
        raise FileNotFoundError(f"Paketkatalogen {source.path} finns inte")
    for name, path in sorted(latest_packages(source.path).items()):
        pkg = read_package_file(path)
        result.sources.append(f"paket {name}#{pkg.version}")
        for r in pkg.resources:
            result.resources.append(normalize(r))
        result.notes.append(f"Paket {name}#{pkg.version}: {len(pkg.resources)} resurser")


def _label(r: dict) -> str:
    size = f", {concept_count(r)} begrepp" if r["resourceType"] == "CodeSystem" else ""
    return f"{r['resourceType']} {r.get('url')}|{r.get('version', '-')}{size}"
