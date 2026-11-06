"""Grundladdning: lägg in ett pakets resurser i en FHIR-server (t.ex. den mockade HAPI-servern).

Resurserna skickas med PUT på sina id i transaktionsbundlar, så att en ny
paketversion ersätter den gamla i stället för att skapa dubbletter. Stora
kodsystem skickas en och en. HAPI indexerar kodsystemens begrepp i bakgrunden;
``wait`` väntar tills ``$lookup`` svarar för ett begrepp per kodsystem.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx2

from .package import Package, concept_count, latest_packages, read_package, read_package_file

FHIR_JSON = "application/fhir+json"
_ORDER = {"CodeSystem": 0, "ValueSet": 1, "ConceptMap": 2, "NamingSystem": 3}


@dataclass
class LoadReport:
    package: str
    loaded: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    not_indexed: list[str] = field(default_factory=list)


def open_packages(source: str) -> list[Package]:
    """Ett paket (fil eller URL) eller senaste paketet per namn i en katalog."""
    if source.startswith(("http://", "https://")):
        response = httpx2.get(source, follow_redirects=True, timeout=300)
        response.raise_for_status()
        return [read_package(response.content)]
    path = Path(source)
    if path.is_dir():
        return [read_package_file(p) for _, p in sorted(latest_packages(path).items())]
    return [read_package_file(path)]


def load(pkg: Package, server: str, *, batch_size: int = 20, large: int = 2000, wait: float = 0,
         transport: httpx2.BaseTransport | None = None, log: Callable[[str], None] = print) -> LoadReport:
    report = LoadReport(f"{pkg.name}#{pkg.version}")
    with httpx2.Client(base_url=server.rstrip("/") + "/", timeout=600, transport=transport,
                       headers={"Accept": FHIR_JSON, "Content-Type": FHIR_JSON}) as http:
        resources = sorted(pkg.resources, key=lambda r: (_ORDER.get(r["resourceType"], 9), r["id"]))
        single = [r for r in resources if r["resourceType"] == "CodeSystem" and concept_count(r) > large]
        batches, batch = [], []
        for r in resources:
            if r in single:
                continue
            batch.append(r)
            if len(batch) == batch_size:
                batches.append(batch)
                batch = []
        if batch:
            batches.append(batch)
        for r in single:
            _put(http, r, report, log)
        for b in batches:
            bundle = {"resourceType": "Bundle", "type": "transaction", "entry": [
                {"resource": r, "request": {"method": "PUT", "url": f"{r['resourceType']}/{r['id']}"}} for r in b]}
            response = http.post("", content=json.dumps(bundle, ensure_ascii=False).encode("utf-8"))
            if response.status_code < 300:
                report.loaded += [_label(r) for r in b]
            else:  # en trasig resurs ska inte stoppa resten: försök en och en
                log(f"  transaktionen avvisades ({response.status_code}) – laddar resurserna en och en")
                for r in b:
                    _put(http, r, report, log)
        if wait:
            report.not_indexed = wait_for_index(http, [r for r in resources if r["resourceType"] == "CodeSystem"],
                                                wait, log)
    return report


def _put(http: httpx2.Client, r: dict[str, Any], report: LoadReport, log: Callable[[str], None]) -> None:
    response = http.put(f"{r['resourceType']}/{r['id']}", content=json.dumps(r, ensure_ascii=False).encode("utf-8"))
    if response.status_code < 300:
        report.loaded.append(_label(r))
    else:
        report.failed.append(f"{_label(r)}: {response.status_code} {_outcome(response)}")
        log(f"  ✗ {report.failed[-1]}")


def wait_for_index(http: httpx2.Client, codesystems: list[dict], timeout: float,
                   log: Callable[[str], None]) -> list[str]:
    """Vänta tills $lookup fungerar för första begreppet i varje kodsystem (HAPI indexerar i bakgrunden)."""
    pending = {cs["url"]: cs["concept"][0]["code"] for cs in codesystems
               if cs.get("content") == "complete" and cs.get("concept")}
    deadline = time.monotonic() + timeout
    while pending and time.monotonic() < deadline:
        for url, code in list(pending.items()):
            r = http.get("CodeSystem/$lookup", params={"system": url, "code": code})
            if r.status_code == 200:
                pending.pop(url)
        if pending:
            time.sleep(2)
    if pending:
        log(f"  {len(pending)} kodsystem är ännu inte indexerade: {', '.join(pending)}")
    return list(pending)


def _label(r: dict) -> str:
    return f"{r['resourceType']}/{r['id']} ({r.get('url')})"


def _outcome(response: httpx2.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    return "; ".join(i.get("diagnostics", "") for i in body.get("issue", []))[:300] or response.text[:200]
